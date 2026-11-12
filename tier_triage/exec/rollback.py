"""Rollback a completed migration batch, with explicit unrecoverable results."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from tier_triage.audit.ledger import Ledger
from tier_triage.lake.fs_ops import atomic_move, ensure_parent_dirs, safe_join


class RollbackError(RuntimeError):
    pass


def rollback_batch(batch_id: str, lake_root: str | Path,
                   ledger_path: str | Path, actor: str = "cli") -> dict[str, Any]:
    """Reverse every moved action of a completed batch.

    Missing or conflicting paths are reported as ``unrecoverable``; the
    remaining actions are still attempted. No filesystem data is deleted.
    """
    ledger = Ledger(ledger_path)
    try:
        batch = ledger.get_batch(batch_id)
        if batch is None:
            raise RollbackError(f"batch not found: {batch_id}")
        if batch["status"] != "completed":
            raise RollbackError(
                f"batch {batch_id} is {batch['status']}; only completed batches "
                "can be rolled back")
        actions = [row for row in ledger.list_actions(batch_id)
                   if row["status"] in {"moved", "marked"}]
        restored = 0
        unmarked = 0
        unrecoverable: list[dict[str, Any]] = []
        restored_rows: list[dict[str, Any]] = []
        for action in reversed(actions):
            seq = action["seq"]
            if action["action"] == "mark_purge":
                ledger.finish_action(batch_id, seq, "unmarked")
                unmarked += 1
                continue
            source_rel = action["source_rel"]
            target_rel = action["target_rel"]
            try:
                source = safe_join(lake_root, source_rel)
                target = safe_join(lake_root, target_rel or "")
                if not target.exists():
                    reason = "moved target is missing; path was changed externally"
                elif source.exists():
                    reason = "both original and moved paths exist; refusing to overwrite"
                else:
                    ensure_parent_dirs(source.parent, lake_root)
                    atomic_move(target, source, lake_root,
                                f"{batch_id}-rollback-{seq}")
                    ledger.finish_action(batch_id, seq, "rolled_back")
                    restored += 1
                    restored_rows.append({
                        "seq": seq, "partition_id": action["partition_id"],
                        "source": source_rel, "target": target_rel})
                    continue
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
            unrecoverable.append({
                "seq": seq, "partition_id": action["partition_id"],
                "source": source_rel, "target": target_rel, "reason": reason})
            ledger.finish_action(batch_id, seq, "unrecoverable", reason)
        detail = {
            "actor": actor,
            "restored": restored_rows,
            "unrecoverable": unrecoverable,
            "unmarked_purge_actions": unmarked,
        }
        ledger.add_rollback(batch_id, restored, len(unrecoverable), detail)
        status = "rolled_back" if not unrecoverable else "rollback_partial"
        ledger.update_batch_status(batch_id, status)
        return {
            "batch_id": batch_id,
            "status": status,
            "restored_count": restored,
            "unrecoverable_count": len(unrecoverable),
            "unmarked_purge_count": unmarked,
            "unrecoverable": unrecoverable,
            "detail": detail,
        }
    finally:
        ledger.close()
