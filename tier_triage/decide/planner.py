"""Stable migration-plan construction and plan-hash verification."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from tier_triage.lake.fs_ops import FsSafetyError, safe_join
from tier_triage.util import DataError, canonical_json, load_json, sha256_obj, write_json

ACTION_TYPES = ("move", "mark_purge", "noop")
STATUSES = ("ready", "blocked", "noop")
ACTION_RANK = {"move": 0, "mark_purge": 1, "noop": 2}
STATUS_RANK = {"ready": 0, "blocked": 1, "noop": 2}


class PlanIntegrityError(DataError):
    pass


def plan_hash_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items()
            if key not in {"plan_hash", "plan_id"}}


def compute_plan_hash(plan: dict[str, Any]) -> str:
    return sha256_obj(plan_hash_payload(plan))


def _target_path(decision: dict[str, Any]) -> str | None:
    tier = decision["tier"]
    if tier == "purge_candidate":
        return None
    source = decision["source_path"]
    if tier == decision["current_layer"]:
        return source
    pieces = decision.get("partition_parts") or []
    return "/".join([tier, decision["table"].split(".", 1)[0],
                     decision["table"].split(".", 1)[1], *pieces])


def _make_action(decision: dict[str, Any], lake_root: str | Path) -> dict[str, Any]:
    target = _target_path(decision)
    action_type = "noop" if target == decision["source_path"] else "move"
    if decision["tier"] == "purge_candidate":
        action_type = "mark_purge"
    action: dict[str, Any] = {
        "partition_id": decision["partition_id"],
        "table": decision["table"],
        "partition_spec": decision["partition_spec"],
        "action": action_type,
        "status": "ready",
        "current_layer": decision["current_layer"],
        "target_layer": (None if decision["tier"] == "purge_candidate"
                         else decision["tier"]),
        "source": decision["source_path"],
        "target": target,
        "bytes": int(decision.get("total_bytes", 0)),
        "file_count": int(decision.get("file_count", 0)),
        "reason": f"decision={decision['tier']}; score={decision['score']}",
    }
    try:
        source_abs = safe_join(lake_root, decision["source_path"])
        if not source_abs.exists():
            action["status"] = "blocked"
            action["reason"] = "source path is missing"
            return action
        if action_type == "move":
            target_abs = safe_join(lake_root, target or "")
            if target_abs.exists():
                action["status"] = "blocked"
                action["reason"] = "target path already exists"
                return action
        if action_type == "noop":
            action["status"] = "noop"
    except FsSafetyError as exc:
        action["status"] = "blocked"
        action["reason"] = f"unsafe path: {exc}"
    return action


def build_plan(evaluation: dict[str, Any], inventory: dict[str, Any],
               lake_root: str | Path) -> dict[str, Any]:
    """Build a deterministic plan; ordering and hash never use wall-clock time."""
    partitions = {row["partition_id"]: row for row in inventory.get("partitions", [])}
    actions: list[dict[str, Any]] = []
    for decision in evaluation.get("decisions", []):
        if decision["partition_id"] not in partitions:
            raise DataError(
                f"evaluation references missing partition: {decision['partition_id']}")
        actions.append(_make_action(decision, lake_root))
    actions.sort(key=lambda row: (
        ACTION_RANK[row["action"]], STATUS_RANK[row["status"]],
        row["table"], row["partition_spec"], row["partition_id"]))
    for sequence, action in enumerate(actions, start=1):
        action["seq"] = sequence
    action_counts = {action: sum(
        1 for row in actions if row["action"] == action) for action in ACTION_TYPES}
    status_counts = {status: sum(
        1 for row in actions if row["status"] == status) for status in STATUSES}
    projected_release = sum(
        row["bytes"] for row in actions
        if row["status"] == "ready" and (
            row["action"] == "mark_purge" or
            (row["action"] == "move" and row["target_layer"] in {"warm", "cold"})))
    purge_bytes = sum(row["bytes"] for row in actions
                      if row["action"] == "mark_purge" and row["status"] == "ready")
    plan: dict[str, Any] = {
        "schema_version": 1,
        "layout": inventory.get("layout", "tiered-v1"),
        "as_of": evaluation["as_of"],
        "generated_at": evaluation["as_of"],
        "inventory_hash": evaluation.get("inventory_hash"),
        "policy_fingerprint": evaluation.get("policy_fingerprint"),
        "actions": actions,
        "summary": {
            "partitions": len(actions),
            "action_counts": action_counts,
            "status_counts": status_counts,
            "projected_release_bytes": projected_release,
            "purge_candidate_bytes": purge_bytes,
            "ready_actions": status_counts["ready"],
            "blocked_actions": status_counts["blocked"],
        },
    }
    plan["plan_hash"] = compute_plan_hash(plan)
    plan["plan_id"] = f"plan-{plan['plan_hash'][:16]}"
    return plan


def validate_plan_integrity(plan: dict[str, Any]) -> None:
    if not isinstance(plan, dict):
        raise PlanIntegrityError("plan must be a JSON object")
    if plan.get("schema_version") != 1:
        raise PlanIntegrityError("unsupported plan schema_version")
    expected = plan.get("plan_hash")
    if not isinstance(expected, str) or len(expected) != 64:
        raise PlanIntegrityError("plan_hash is missing or malformed")
    actual = compute_plan_hash(plan)
    if actual != expected:
        raise PlanIntegrityError(
            f"plan hash mismatch: expected {expected}, recomputed {actual}")
    if not isinstance(plan.get("actions"), list):
        raise PlanIntegrityError("plan actions must be a list")
    seen: set[str] = set()
    for action in plan["actions"]:
        if action.get("action") not in ACTION_TYPES:
            raise PlanIntegrityError(f"unknown action: {action.get('action')!r}")
        if action.get("status") not in STATUSES:
            raise PlanIntegrityError(f"unknown action status: {action.get('status')!r}")
        partition_id = action.get("partition_id")
        if not isinstance(partition_id, str) or partition_id in seen:
            raise PlanIntegrityError(f"duplicate or invalid partition: {partition_id!r}")
        seen.add(partition_id)
        if not isinstance(action.get("source"), str):
            raise PlanIntegrityError(f"invalid source for {partition_id}")


def load_plan(path: str | Path) -> dict[str, Any]:
    plan = load_json(path)
    validate_plan_integrity(plan)
    return plan


def save_plan(path: str | Path, plan: dict[str, Any]) -> None:
    validate_plan_integrity(plan)
    write_json(path, plan)


def plan_canonical(plan: dict[str, Any]) -> str:
    return canonical_json(plan)
