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


def _capacity_annotation(decision: dict[str, Any]) -> dict[str, Any] | None:
    """Profile-only structured facts copied onto the action for explainability."""
    capacity = decision.get("capacity")
    if capacity is None:
        return None
    return {
        "size_band": decision.get("size_band"),
        "total_bytes": capacity["total_bytes"],
        "max_hot_bytes": capacity["max_hot_bytes"],
        "over_hot_limit": capacity["over_hot_limit"],
        "profile_status": capacity.get("profile_status"),
    }


def _size_reason_suffix(decision: dict[str, Any]) -> str | None:
    capacity = decision.get("capacity")
    if capacity is None or not capacity.get("over_hot_limit"):
        return None
    return (f"; size_forced=warm total_bytes={capacity['total_bytes']} "
            f"max_hot_bytes={capacity['max_hot_bytes']} "
            f"band={decision.get('size_band')}")


def _make_action(decision: dict[str, Any], lake_root: str | Path) -> dict[str, Any]:
    target = _target_path(decision)
    action_type = "noop" if target == decision["source_path"] else "move"
    if decision["tier"] == "purge_candidate":
        action_type = "mark_purge"
    reason = f"decision={decision['tier']}; score={decision['score']}"
    suffix = _size_reason_suffix(decision)
    if suffix:
        reason += suffix
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
        "reason": reason,
    }
    annotation = _capacity_annotation(decision)
    if annotation is not None:
        action["capacity"] = annotation
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


def _relief_rank(decision: dict[str, Any]) -> int:
    """Actions that reclaim space (purge) outrank pure tier-to-tier moves."""
    return 0 if decision["tier"] == "purge_candidate" else 1


def _relief_key(decision: dict[str, Any]) -> tuple:
    """Deterministic relief priority for capacity-pressure ordering.

    Rationale (see docs/CAPACITY_PROFILE.md):

    * A partition that frees space (``mark_purge``) outranks one that only
      moves bytes between tiers.
    * Within the same action class, *biggest first*: one large old partition
      relieves more pressure per move than many tiny ones, and cold old data
      is the cheapest to relocate (re-read risk is minimal).
    * Age then decides among equal sizes: the older partition moves first.
      Reads and finally the stable partition id make the total order total.
    * "Small and old" partitions therefore sink to the bottom of every queue
      rather than being swept first: they are cheap to keep, purge-ready via
      the normal age path, and never used as a blunt age-only axe.
    """
    age = decision.get("age_days")
    age_value = age if age is not None else -1
    return (
        _relief_rank(decision),
        -int(decision.get("total_bytes", 0)),
        -age_value,
        int(decision.get("reads_30d", 0)),
        decision["partition_id"],
    )


def _apply_warm_budget(actions: list[dict[str, Any]],
                       decisions_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Enforce per-table warm-layer capacity budgets (profile mode only).

    Budgets come from the optional policy field ``max_warm_bytes`` (0 =
    unconfigured, no enforcement). The starting balance per table is the warm
    bytes *projected to remain*: resident warm partitions that move out (to
    cold) or enter purge review are credited first. Inbound warm moves then
    consume the remaining budget in relief order (reclaimable first, then
    largest/oldest/least-read). Moves that would overflow the budget are
    blocked with an explicit capacity reason instead of crushing the warm tier.
    """
    budgets: dict[str, int] = {}
    for decision in decisions_by_id.values():
        budget = int(decision.get("policy", {}).get("max_warm_bytes", 0))
        if budget > 0:
            budgets[decision["table"]] = budget
    warm_resident: dict[str, int] = {}
    warm_departures: dict[str, int] = {}
    for decision in decisions_by_id.values():
        if decision["current_layer"] != "warm":
            continue
        size = int(decision.get("total_bytes", 0))
        table = decision["table"]
        warm_resident[table] = warm_resident.get(table, 0) + size
        if decision["tier"] in {"cold", "purge_candidate"}:
            # This warm resident leaves the tier (or enters reclaim review),
            # so its bytes must be credited back before admitting new moves.
            warm_departures[table] = warm_departures.get(table, 0) + size
    inbound = [
        action for action in actions
        if action["action"] == "move" and action.get("target_layer") == "warm"
        and action["status"] == "ready" and action["table"] in budgets
    ]
    inbound.sort(key=lambda action: _relief_key(decisions_by_id[action["partition_id"]]))
    used = {
        table: warm_resident.get(table, 0) - warm_departures.get(table, 0)
        for table in budgets
    }
    blocked_rows: list[dict[str, Any]] = []
    for action in inbound:
        table = action["table"]
        budget = budgets[table]
        size = int(action["bytes"])
        if used.get(table, 0) + size > budget:
            action["status"] = "blocked"
            action["reason"] = (
                f"warm capacity budget exceeded for {table}: "
                f"budget={budget} projected={used.get(table, 0) + size} "
                f"partition_bytes={size}; deferred per relief order "
                f"(reclaimable first, then largest-oldest; small-old "
                f"partitions are not swept)")
            blocked_rows.append({
                "partition_id": action["partition_id"],
                "table": table,
                "bytes": size,
                "reason": action["reason"],
            })
        else:
            used[table] = used.get(table, 0) + size
    table_budgets = [
        {"table": table,
         "max_warm_bytes": budgets[table],
         "resident_warm_bytes": warm_resident.get(table, 0),
         "departing_warm_bytes": warm_departures.get(table, 0),
         "projected_warm_bytes": used.get(table, 0),
         "within_budget": used.get(table, 0) <= budgets[table]}
        for table in sorted(budgets)
    ]
    return {
        "warm_budget_configured": bool(budgets),
        "table_budgets": table_budgets,
        "capacity_blocked": blocked_rows,
        "relief_order": [
            {"partition_id": action["partition_id"], "table": action["table"],
             "bytes": int(action["bytes"]), "action": action["action"]}
            for action in sorted(
                (a for a in actions
                 if a["action"] in {"move", "mark_purge"}
                 and a["table"] in budgets),
                key=lambda a: _relief_key(decisions_by_id[a["partition_id"]]))
        ],
    }


def build_plan(evaluation: dict[str, Any], inventory: dict[str, Any],
               lake_root: str | Path) -> dict[str, Any]:
    """Build a deterministic plan; ordering and hash never use wall-clock time."""
    partitions = {row["partition_id"]: row for row in inventory.get("partitions", [])}
    profile_attached = "capacity_profile" in evaluation
    actions: list[dict[str, Any]] = []
    decisions_by_id: dict[str, dict[str, Any]] = {}
    for decision in evaluation.get("decisions", []):
        if decision["partition_id"] not in partitions:
            raise DataError(
                f"evaluation references missing partition: {decision['partition_id']}")
        decisions_by_id[decision["partition_id"]] = decision
        actions.append(_make_action(decision, lake_root))

    capacity_section: dict[str, Any] | None = None
    if profile_attached:
        capacity_section = _apply_warm_budget(actions, decisions_by_id)

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
    summary: dict[str, Any] = {
        "partitions": len(actions),
        "action_counts": action_counts,
        "status_counts": status_counts,
        "projected_release_bytes": projected_release,
        "purge_candidate_bytes": purge_bytes,
        "ready_actions": status_counts["ready"],
        "blocked_actions": status_counts["blocked"],
    }
    plan: dict[str, Any] = {
        "schema_version": 1,
        "layout": inventory.get("layout", "tiered-v1"),
        "as_of": evaluation["as_of"],
        "generated_at": evaluation["as_of"],
        "inventory_hash": evaluation.get("inventory_hash"),
        "policy_fingerprint": evaluation.get("policy_fingerprint"),
        "actions": actions,
        "summary": summary,
    }
    if profile_attached:
        # Profile-only block; absent on every legacy plan so old hashes stand.
        plan["capacity_profile"] = {
            "profile_hash": evaluation["capacity_profile"].get("profile_hash"),
            "anomaly_count": evaluation["capacity_profile"].get("anomaly_count"),
            "oversize_partitions": list(
                evaluation["capacity_profile"].get("oversize_partitions", [])),
            "pressure": capacity_section,
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
