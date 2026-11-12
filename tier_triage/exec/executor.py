"""Transactional batch execution with filesystem rollback on failure."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tier_triage.audit.ledger import Ledger
from tier_triage.decide.planner import validate_plan_integrity
from tier_triage.lake.fs_ops import (
    PreflightError,
    atomic_move,
    check_free_space,
    disk_free_bytes,
    ensure_parent_dirs,
    prune_created_dirs,
    safe_join,
)


class ExecutionError(RuntimeError):
    pass


def _existing_ancestor(path: Path) -> Path:
    cursor = path
    while not cursor.exists():
        if cursor.parent == cursor:
            raise PreflightError(f"no existing ancestor for {path}")
        cursor = cursor.parent
    return cursor


def preflight_plan(plan: dict[str, Any], lake_root: str | Path,
                   reserve_bytes: int = 1024 * 1024) -> dict[str, Any]:
    """Validate every ready action before the first byte is moved."""
    validate_plan_integrity(plan)
    ready = [action for action in plan.get("actions", [])
             if action.get("status") == "ready"]
    targets: set[str] = set()
    space_by_device: dict[int, tuple[int, Path]] = {}
    for action in ready:
        source = safe_join(lake_root, action["source"])
        if not source.exists():
            raise PreflightError(
                f"preflight failed: source missing for {action['partition_id']}")
        if action["action"] == "move":
            if not action.get("target"):
                raise PreflightError(
                    f"preflight failed: move target missing for {action['partition_id']}")
            target = safe_join(lake_root, action["target"])
            if target.exists():
                raise PreflightError(
                    f"preflight failed: target exists for {action['partition_id']}")
            if action["target"] in targets:
                raise PreflightError(
                    f"preflight failed: duplicate target {action['target']}")
            targets.add(action["target"])
            ancestor = _existing_ancestor(target.parent)
            if not ancestor.is_dir():
                raise PreflightError(
                    f"preflight failed: target parent is not a directory for "
                    f"{action['partition_id']}")
            device = os.stat(ancestor).st_dev
            previous = space_by_device.get(device)
            required = int(action.get("bytes", 0))
            if previous:
                required += previous[0]
            space_by_device[device] = (required, ancestor)
        elif action["action"] not in {"mark_purge", "noop"}:
            raise PreflightError(f"unknown action {action['action']!r}")
    free_values: list[int] = []
    for _device, (required, candidate) in sorted(space_by_device.items()):
        check_free_space(candidate, required, reserve_bytes)
        free_values.append(disk_free_bytes(candidate))
    return {"ready_actions": len(ready),
            "required_bytes": sum(int(a.get("bytes", 0)) for a in ready),
            "reserve_bytes": reserve_bytes,
            "free_bytes": min(free_values) if free_values else None}


def _batch_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"batch-{stamp}-{uuid.uuid4().hex[:6]}"


def _result(mode: str, plan: dict[str, Any], **values: Any) -> dict[str, Any]:
    result = {
        "mode": mode,
        "plan_id": plan.get("plan_id"),
        "plan_hash": plan.get("plan_hash"),
        "as_of": plan.get("as_of"),
    }
    result.update(values)
    return result


def execute_plan(plan: dict[str, Any], lake_root: str | Path,
                 ledger_path: str | Path, apply: bool = False,
                 confirm: bool = False, actor: str = "cli",
                 reserve_bytes: int = 1024 * 1024) -> dict[str, Any]:
    """Dry-run by default. ``apply=True`` also requires ``confirm=True``."""
    validate_plan_integrity(plan)
    preflight = preflight_plan(plan, lake_root, reserve_bytes)
    if not apply:
        return _result("dry-run", plan, status="ready", **preflight)
    if not confirm:
        raise ExecutionError("refusing to apply without explicit confirmation")
    ledger = Ledger(ledger_path)
    batch_id = _batch_id()
    moved: list[dict[str, Any]] = []
    created_dirs: list[Path] = []
    moved_count = 0
    marked_count = 0
    current_seq: int | None = None
    try:
        try:
            ledger.record_plan(plan)
            ledger.create_batch(plan, batch_id, actor)
            for action in plan.get("actions", []):
                if action.get("status") != "ready":
                    continue
                seq = int(action["seq"])
                current_seq = seq
                ledger.start_action(batch_id, seq)
                if action["action"] == "move":
                    source = safe_join(lake_root, action["source"])
                    target = safe_join(lake_root, action["target"])
                    created_dirs.extend(ensure_parent_dirs(target.parent, lake_root))
                    atomic_move(source, target, lake_root, f"{batch_id}-{seq}")
                    moved.append({"seq": seq, "source": action["source"],
                                  "target": action["target"]})
                    moved_count += 1
                    ledger.finish_action(batch_id, seq, "moved")
                elif action["action"] == "mark_purge":
                    marked_count += 1
                    ledger.finish_action(batch_id, seq, "marked")
            ledger.finish_batch(batch_id, "completed", moved_count, marked_count)
            batch = ledger.get_batch(batch_id)
            return _result(
                "apply", plan, status="completed", batch_id=batch_id,
                moved_count=moved_count, marked_count=marked_count,
                batch=batch, **preflight)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            reverted = 0
            revert_errors: list[str] = []
            moved_sequences = {record["seq"] for record in moved}
            if current_seq is not None and current_seq not in moved_sequences:
                ledger.finish_action(batch_id, current_seq, "failed", error)
            for record in reversed(moved):
                try:
                    source = safe_join(lake_root, record["source"])
                    target = safe_join(lake_root, record["target"])
                    ensure_parent_dirs(source.parent, lake_root)
                    atomic_move(target, source, lake_root,
                                f"{batch_id}-revert-{record['seq']}")
                    ledger.finish_action(batch_id, record["seq"], "reverted", error)
                    reverted += 1
                except Exception as revert_exc:
                    revert_errors.append(
                        f"seq {record['seq']}: {type(revert_exc).__name__}: {revert_exc}")
                    ledger.finish_action(
                        batch_id, record["seq"], "revert_failed",
                        f"{type(revert_exc).__name__}: {revert_exc}")
            prune_created_dirs(created_dirs)
            ledger.cancel_pending(batch_id, "batch failed")
            ledger.finish_batch(batch_id, "failed", 0, marked_count, error)
            return _result(
                "apply", plan, status="failed", batch_id=batch_id,
                error=error, reverted_count=reverted,
                revert_errors=revert_errors, marked_count=marked_count,
                moved_count=0, **preflight)
    finally:
        ledger.close()
