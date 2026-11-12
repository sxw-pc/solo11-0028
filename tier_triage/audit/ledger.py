"""SQLite audit ledger: snapshots, policies, plans, batches and rollbacks."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from tier_triage.util import now_iso, sha256_obj

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  as_of TEXT NOT NULL,
  lake_root TEXT NOT NULL,
  inventory_hash TEXT NOT NULL,
  partition_count INTEGER NOT NULL,
  total_bytes INTEGER NOT NULL,
  inventory_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policies (
  policy_id TEXT PRIMARY KEY,
  loaded_at TEXT NOT NULL,
  source_path TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  policy_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
  plan_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  as_of TEXT NOT NULL,
  policy_fingerprint TEXT,
  inventory_hash TEXT,
  plan_path TEXT,
  payload_json TEXT NOT NULL,
  action_count INTEGER NOT NULL,
  blocked_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
  batch_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  actor TEXT NOT NULL,
  action_count INTEGER NOT NULL,
  moved_count INTEGER NOT NULL DEFAULT 0,
  marked_count INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE TABLE IF NOT EXISTS actions (
  action_id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  partition_id TEXT NOT NULL,
  action TEXT NOT NULL,
  source_rel TEXT NOT NULL,
  target_rel TEXT,
  bytes INTEGER NOT NULL,
  file_count INTEGER NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  error TEXT,
  UNIQUE(batch_id, seq)
);
CREATE TABLE IF NOT EXISTS rollbacks (
  rollback_id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  restored_count INTEGER NOT NULL,
  unrecoverable_count INTEGER NOT NULL,
  detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batches_started ON batches(started_at);
CREATE INDEX IF NOT EXISTS idx_actions_batch ON actions(batch_id, seq);
CREATE INDEX IF NOT EXISTS idx_rollbacks_batch ON rollbacks(batch_id);
"""


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Ledger:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def record_snapshot(self, inventory: dict[str, Any], as_of: str,
                        lake_root: str) -> str:
        fingerprint = inventory.get("inventory_hash") or sha256_obj(inventory)
        snapshot_id = f"snap-{fingerprint[:16]}"
        partitions = inventory.get("partitions", [])
        self.conn.execute(
            "INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?,?,?)",
            (snapshot_id, now_iso(), as_of, lake_root, fingerprint,
             len(partitions), sum(int(p.get("total_bytes", 0)) for p in partitions),
             _dump(inventory)))
        self.conn.commit()
        return snapshot_id

    def record_policy(self, policy: Any) -> str:
        policy_id = f"policy-{policy.fingerprint[:16]}"
        self.conn.execute(
            "INSERT OR IGNORE INTO policies VALUES (?,?,?,?,?)",
            (policy_id, now_iso(), policy.source_path, policy.fingerprint,
             _dump(policy.data)))
        self.conn.commit()
        return policy_id

    def record_plan(self, plan: dict[str, Any], plan_path: str | None = None) -> str:
        plan_id = plan.get("plan_id") or f"plan-{plan['plan_hash'][:16]}"
        actions = plan.get("actions", [])
        self.conn.execute(
            "INSERT OR REPLACE INTO plans VALUES (?,?,?,?,?,?,?,?,?)",
            (plan_id, now_iso(), plan.get("as_of"), plan.get("policy_fingerprint"),
             plan.get("inventory_hash"), plan_path, _dump(plan), len(actions),
             sum(1 for action in actions if action.get("status") == "blocked")))
        self.conn.commit()
        return plan_id

    def create_batch(self, plan: dict[str, Any], batch_id: str,
                     actor: str = "cli") -> None:
        actions = plan.get("actions", [])
        ready = sum(1 for action in actions if action.get("status") == "ready")
        self.conn.execute(
            "INSERT INTO batches (batch_id, plan_id, started_at, status, actor, "
            "action_count) VALUES (?,?,?,?,?,?)",
            (batch_id, plan["plan_id"], now_iso(), "running", actor, ready))
        for action in actions:
            initial = {"ready": "pending", "blocked": "blocked",
                       "noop": "skipped"}[action["status"]]
            self.conn.execute(
                "INSERT INTO actions (batch_id, seq, partition_id, action, "
                "source_rel, target_rel, bytes, file_count, status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (batch_id, action.get("seq", 0), action["partition_id"],
                 action["action"], action["source"], action.get("target"),
                 int(action.get("bytes", 0)), int(action.get("file_count", 0)),
                 initial))
        self.conn.commit()

    def start_action(self, batch_id: str, seq: int) -> None:
        self.conn.execute(
            "UPDATE actions SET status='running', started_at=? "
            "WHERE batch_id=? AND seq=?", (now_iso(), batch_id, seq))
        self.conn.commit()

    def finish_action(self, batch_id: str, seq: int, status: str,
                      error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE actions SET status=?, finished_at=?, error=? "
            "WHERE batch_id=? AND seq=?",
            (status, now_iso(), error, batch_id, seq))
        self.conn.commit()

    def cancel_pending(self, batch_id: str, detail: str) -> None:
        self.conn.execute(
            "UPDATE actions SET status='cancelled', finished_at=?, error=? "
            "WHERE batch_id=? AND status='pending'",
            (now_iso(), detail, batch_id))
        self.conn.commit()

    def finish_batch(self, batch_id: str, status: str, moved: int,
                     marked: int, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE batches SET status=?, finished_at=?, moved_count=?, "
            "marked_count=?, error=? WHERE batch_id=?",
            (status, now_iso(), moved, marked, error, batch_id))
        self.conn.commit()

    def update_batch_status(self, batch_id: str, status: str,
                            error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE batches SET status=?, finished_at=?, error=? WHERE batch_id=?",
            (status, now_iso(), error, batch_id))
        self.conn.commit()

    def add_rollback(self, batch_id: str, restored: int,
                     unrecoverable: int, detail: dict[str, Any]) -> int:
        cursor = self.conn.execute(
            "INSERT INTO rollbacks (batch_id, created_at, restored_count, "
            "unrecoverable_count, detail_json) VALUES (?,?,?,?,?)",
            (batch_id, now_iso(), restored, unrecoverable, _dump(detail)))
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_batches(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM batches ORDER BY started_at DESC, batch_id DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(row) for row in rows]

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        return _row(self.conn.execute(
            "SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone())

    def list_actions(self, batch_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM actions WHERE batch_id=? ORDER BY seq",
            (batch_id,)).fetchall()]

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def latest_plan(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM plans ORDER BY created_at DESC, plan_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["inventory"] = json.loads(result.pop("inventory_json"))
        return result

    def latest_snapshot(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM snapshots ORDER BY created_at DESC, snapshot_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["inventory"] = json.loads(result.pop("inventory_json"))
        return result

    def get_rollbacks(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM rollbacks WHERE batch_id=? ORDER BY rollback_id",
            (batch_id,)).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            result["detail"] = json.loads(result.pop("detail_json"))
            results.append(result)
        return results
