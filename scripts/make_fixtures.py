#!/usr/bin/env python3
"""Generate a deterministic, wholly local demo lake and access log."""
from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-11-01T00:00:00Z"
READ_OPS = ("read", "scan", "read")


PARTITIONS = [
    # layer, db, table, partition parts, file count, (7d, 30d, 90d reads), bytes
    ("hot", "sales", "orders", ["dt=2026-10-28"], 2, (18, 24, 30), None),
    ("hot", "sales", "orders", ["dt=2026-10-10"], 2, (12, 16, 18), None),
    ("hot", "sales", "orders", ["dt=2026-09-05"], 1, (1, 4, 9), None),
    ("hot", "sales", "orders", ["dt=2026-06-15"], 2, (0, 0, 2), None),
    ("warm", "sales", "orders", ["dt=2025-05-01"], 1, (0, 0, 0), None),
    ("cold", "sales", "orders", ["dt=2024-01-01"], 1, (0, 0, 0), None),
    ("hot", "sales", "order_items", ["dt=2026-09-20"], 2, (1, 2, 5), None),
    ("hot", "sales", "order_items", ["dt=2026-03-01"], 1, (0, 0, 0), None),
    # Young (inside the retention window) but physically huge: the volume
    # profile must force it out of hot even though age/reads would keep it.
    ("hot", "sales", "order_items", ["dt=2026-10-28"], 1, (14, 20, 26),
     9 * 1024 * 1024),
    ("hot", "marketing", "events", ["dt=2026-10-31"], 2, (9, 14, 20), None),
    ("hot", "marketing", "events", ["dt=2026-10-05"], 1, (2, 5, 8), None),
    ("hot", "marketing", "events", ["dt=2026-08-20"], 2, (0, 1, 6), None),
    ("hot", "marketing", "events", ["dt=2026-05-01"], 1, (0, 0, 0), None),
    ("warm", "marketing", "events", ["dt=2025-07-01"], 1, (0, 0, 0), None),
    ("warm", "marketing", "events", ["dt=2026-09-10"], 1, (0, 0, 3), None),
    ("cold", "marketing", "events", ["dt=2026-07-15"], 1, (0, 0, 1), None),
    ("hot", "ops", "audit_log", ["region=cn", "dt=2026-10-20"], 2, (6, 9, 12), None),
    ("hot", "ops", "audit_log", ["region=cn", "dt=2026-07-01"], 1, (2, 3, 7), None),
    ("hot", "ops", "audit_log", ["region=us", "dt=2025-06-01"], 1, (0, 0, 0), None),
    ("hot", "finance", "ledger", ["dt=2026-10-01"], 2, (8, 11, 16), None),
    ("warm", "finance", "ledger", ["dt=2025-01-01"], 1, (0, 0, 0), None),
]


def _iso(stamp: datetime) -> str:
    return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _partition_date(parts: list[str]) -> datetime:
    for part in parts:
        if part.startswith("dt="):
            return datetime.fromisoformat(part[3:]).replace(tzinfo=timezone.utc)
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _write_partition(lake_root: Path, layer: str, db: str, table: str,
                     parts: list[str], file_count: int, data_root: Path,
                     byte_size: int | None = None) -> None:
    directory = lake_root.joinpath(layer, db, table, *parts)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (_partition_date(parts) + timedelta(hours=12)).timestamp()
    for index in range(file_count):
        path = directory / f"part-{index:04d}.json"
        if byte_size is not None:
            # Sparse, seek-written file: deterministic st_size without
            # allocating the full payload on disk. Content is still real
            # bytes (a header plus a trailing marker), never a symlink.
            header = json.dumps({
                "partition": "/".join([layer, db, table, *parts]),
                "file": index, "schema": "demo-v1-sparse",
            }, sort_keys=True).encode("utf-8")
            with path.open("wb") as handle:
                handle.write(header)
                handle.truncate(byte_size)
                handle.seek(max(0, byte_size - 1))
                handle.write(b"\n")
        else:
            payload = {
                "partition": "/".join([layer, db, table, *parts]),
                "file": index,
                "schema": "demo-v1",
                "rows": 3 + index,
            }
            path.write_text(json.dumps(payload, sort_keys=True) + "\n",
                            encoding="utf-8", newline="\n")
        os.utime(path, (stamp, stamp))
    os.utime(directory, (stamp, stamp))


def _usage_rows(table: str, partition: str, counts: tuple[int, int, int],
                read_ops: tuple[str, ...] = READ_OPS) -> list[dict]:
    seven, thirty, ninety = counts
    offsets: list[int] = []
    for index in range(seven):
        offsets.append(1 + (index % 6))
    for index in range(max(0, thirty - seven)):
        offsets.append(8 + (index % 21))
    for index in range(max(0, ninety - thirty)):
        offsets.append(32 + (index % 58))
    clock = datetime.fromisoformat(AS_OF.replace("Z", "+00:00"))
    rows = []
    for index, offset in enumerate(offsets):
        stamp = clock - timedelta(days=offset, hours=index % 12)
        rows.append({
            "ts": _iso(stamp),
            "table": table,
            "partition": partition,
            "bytes_read": 1024 + index * 137,
            "op": read_ops[index % len(read_ops)],
        })
    return rows


def generate(data_root: Path) -> dict:
    """Regenerate fixtures; rerunning produces byte-identical lake files/logs."""
    lake_root = data_root / "lake"
    usage_root = data_root / "usage"
    for target in (lake_root, usage_root):
        if target.exists():
            shutil.rmtree(target)
    lake_root.mkdir(parents=True)
    usage_root.mkdir(parents=True)
    rows: list[dict] = []
    for layer, db, table, parts, files, counts, byte_size in PARTITIONS:
        _write_partition(lake_root, layer, db, table, parts, files, data_root,
                         byte_size)
        spec = "/".join(parts)
        rows.extend(_usage_rows(f"{db}.{table}", spec, counts))
    # A write-only access must not count as a read.
    rows.append({
        "ts": "2026-10-20T03:00:00Z", "table": "ops.audit_log",
        "partition": "region=us/dt=2025-06-01", "bytes_read": 4096, "op": "write",
    })
    rows.sort(key=lambda row: (row["ts"], row["table"], row["partition"]))
    log_path = usage_root / "access.jsonl"
    with log_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    return {
        "lake_root": str(lake_root),
        "usage_log": str(log_path),
        "partitions": len(PARTITIONS),
        "usage_rows": len(rows),
        "as_of": AS_OF,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="generate offline tier-triage fixtures")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    args = parser.parse_args(argv)
    summary = generate(Path(args.data_dir).resolve())
    print(json.dumps(summary, sort_keys=True, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
