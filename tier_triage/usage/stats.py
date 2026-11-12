"""Partition-level access aggregation against an injected evaluation clock."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from tier_triage.util import iso_z, parse_datetime


def _normalize_partition(table: str, partition: str) -> str:
    """Accept either ``dt=...`` or a full ``db.table.dt=...`` partition id."""
    text = partition.strip().strip("/")
    prefix = f"{table}."
    if text.startswith(prefix):
        text = text[len(prefix):]
    elif "." in table:
        short = table.rsplit(".", 1)[1]
        short_prefix = f"{short}."
        if text.startswith(short_prefix):
            text = text[len(short_prefix):]
    return text


def _build_usage_index(records: Iterable[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        table = record["table"]
        spec = _normalize_partition(table, record["partition"])
        index.setdefault((table, spec), []).append(record)
    for rows in index.values():
        rows.sort(key=lambda row: (row["ts"], row["line"]))
    return index


def compute_stats(inventory: dict[str, Any],
                  records: Iterable[dict[str, Any]],
                  as_of: datetime | str,
                  windows: Iterable[int] = (7, 30, 90)) -> dict[str, Any]:
    """Aggregate reads, bytes and recency for every inventory partition."""
    clock = parse_datetime(as_of)
    window_list = sorted({int(value) for value in windows})
    if not window_list or any(value <= 0 for value in window_list):
        raise ValueError("windows must contain positive day counts")
    if 30 not in window_list:
        raise ValueError("windows must include 30 days for policy evaluation")
    index = _build_usage_index(records)
    matched_keys: set[tuple[str, str]] = set()
    partition_rows: dict[str, dict[str, Any]] = {}
    for part in inventory.get("partitions", []):
        table = part["table_name"]
        spec = part["partition_spec"]
        matched_keys.add((table, spec))
        rows = index.get((table, spec), [])
        read_rows = []
        for row in rows:
            try:
                stamp = parse_datetime(row["ts"])
            except (KeyError, ValueError):
                continue
            if stamp <= clock and row.get("read", True):
                read_rows.append((stamp, row))
        read_rows.sort(key=lambda item: (item[0], item[1].get("line", 0)))
        window_values: dict[str, dict[str, int]] = {}
        for days in window_list:
            cutoff = clock - timedelta(days=days)
            selected = [(stamp, row) for stamp, row in read_rows if stamp >= cutoff]
            window_values[str(days)] = {
                "reads": len(selected),
                "bytes": sum(int(row.get("bytes_read", 0)) for _, row in selected),
            }
        last_stamp = read_rows[-1][0] if read_rows else None
        last_access = iso_z(last_stamp) if last_stamp else None
        idle_days = None
        if last_stamp:
            idle_days = max(0, (clock - last_stamp).days)
        partition_rows[part["partition_id"]] = {
            "partition_id": part["partition_id"],
            "table": table,
            "partition_spec": spec,
            "windows": window_values,
            "reads_30d": window_values["30"]["reads"],
            "bytes_30d": window_values["30"]["bytes"],
            "reads_total": len(read_rows),
            "last_access": last_access,
            "idle_days": idle_days,
            "never_accessed": last_stamp is None,
            "record_count": len(rows),
        }
    orphan_keys = sorted(
        (key for key in index if key not in matched_keys),
        key=lambda key: (key[0], key[1]))
    return {
        "as_of": iso_z(clock),
        "windows": window_list,
        "partitions": partition_rows,
        "summary": {
            "inventory_partitions": len(partition_rows),
            "never_accessed": sum(
                1 for row in partition_rows.values() if row["never_accessed"]),
            "orphan_usage_keys": len(orphan_keys),
            "orphan_usage": [
                {"table": table, "partition_spec": spec}
                for table, spec in orphan_keys[:50]
            ],
        },
    }


def access_summary(stats: dict[str, Any]) -> dict[str, Any]:
    """Small stable summary used by reports and the web dashboard."""
    rows = stats.get("partitions", {}).values()
    return {
        "as_of": stats.get("as_of"),
        "partitions": len(stats.get("partitions", {})),
        "never_accessed": sum(1 for row in rows if row.get("never_accessed")),
        "reads_30d": sum(int(row.get("reads_30d", 0)) for row in rows),
    }
