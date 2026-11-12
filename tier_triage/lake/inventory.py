"""Deterministic inventory scanning for a tiered local data lake.

Layout (``tiered-v1``)::

    <lake>/<hot|warm|cold>/<db>/<table>/[key=value/...]/data-files
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tier_triage.lake.partitioning import (
    LAYOUT,
    TIERS,
    date_from_partition,
    make_partition_id,
    parse_partition_component,
    parse_partition_spec,
    partition_spec,
)
from tier_triage.util import DataError, sha256_obj

MAX_DEPTH = 12


def _iso_mtime(timestamp: float) -> str:
    return (datetime.fromtimestamp(timestamp, timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"))


def _unparsed(entries: list[dict[str, Any]], path: Path, root: Path,
              reason: str) -> None:
    entries.append({"path": path.relative_to(root).as_posix(),
                    "reason": reason})


def _data_stats(directory: Path, actual_root: Path, partition_root: Path,
                unparsed: list[dict[str, Any]], depth: int = 0) -> dict[str, Any]:
    """Sum files in data subdirectories, skipping nested partition dirs."""
    file_count = 0
    total_bytes = 0
    last_modified: float | None = None
    largest_name = ""
    largest_bytes = -1
    if depth > MAX_DEPTH:
        _unparsed(unparsed, directory, actual_root, "directory nesting too deep")
        return {"file_count": 0, "total_bytes": 0, "last_modified": None,
                "largest_file": None}
    try:
        entries = sorted(os.scandir(directory), key=lambda item: item.name)
    except OSError as exc:
        _unparsed(unparsed, directory, actual_root, f"unreadable directory: {exc}")
        return {"file_count": 0, "total_bytes": 0, "last_modified": None,
                "largest_file": None}
    for entry in entries:
        if entry.is_symlink():
            _unparsed(unparsed, Path(entry.path), actual_root,
                      "symlink skipped for safety")
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                name = entry.name
                if parse_partition_component(name):
                    continue  # belongs to the child partition node
                if "=" in name:
                    _unparsed(unparsed, Path(entry.path), actual_root,
                              "malformed partition directory")
                    continue
                child = _data_stats(Path(entry.path), actual_root,
                                    partition_root, unparsed, depth + 1)
                file_count += child["file_count"]
                total_bytes += child["total_bytes"]
                if child["last_modified"]:
                    stamp = datetime.fromisoformat(
                        child["last_modified"].replace("Z", "+00:00")).timestamp()
                    last_modified = max(last_modified or stamp, stamp)
                    if child["largest_file"]:
                        rel = f"{name}/{child['largest_file']['name']}"
                        if child["largest_file"]["bytes"] > largest_bytes:
                            largest_name, largest_bytes = rel, child["largest_file"]["bytes"]
            elif entry.is_file(follow_symlinks=False):
                stat = entry.stat(follow_symlinks=False)
                file_count += 1
                total_bytes += stat.st_size
                last_modified = max(last_modified or stat.st_mtime, stat.st_mtime)
                rel = Path(entry.path).relative_to(partition_root).as_posix()
                if stat.st_size > largest_bytes:
                    largest_name, largest_bytes = rel, stat.st_size
        except OSError as exc:
            _unparsed(unparsed, Path(entry.path), actual_root,
                      f"unreadable entry: {exc}")
    return {
        "file_count": file_count,
        "total_bytes": total_bytes,
        "last_modified": _iso_mtime(last_modified) if last_modified else None,
        "largest_file": ({"name": largest_name, "bytes": largest_bytes}
                         if largest_bytes >= 0 else None),
    }


def _scan_partition_node(node: Path, db: str, table: str, layer: str,
                         parts: list[str], actual_root: Path,
                         unparsed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    child_dirs: list[Path] = []
    try:
        entries = sorted(os.scandir(node), key=lambda item: item.name)
    except OSError as exc:
        _unparsed(unparsed, node, actual_root, f"unreadable partition: {exc}")
        return []
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_dir(follow_symlinks=False):
            parsed = parse_partition_component(entry.name)
            if parsed:
                child_dirs.append(Path(entry.path))
            elif "=" in entry.name:
                _unparsed(unparsed, Path(entry.path), actual_root,
                          "malformed partition directory")
        elif entry.name.startswith(".tier-triage-"):
            _unparsed(unparsed, Path(entry.path), actual_root,
                      "stale staging file")
    stats = _data_stats(node, actual_root, node, unparsed)
    records: list[dict[str, Any]] = []
    for child in child_dirs:
        records.extend(_scan_partition_node(child, db, table, layer,
                                            parts + [child.name], actual_root,
                                            unparsed))
    if stats["file_count"] == 0 and child_dirs:
        return records
    keys = parse_partition_spec(parts) or {}
    spec = partition_spec(parts)
    date_value = date_from_partition(keys)
    records.append({
        "partition_id": make_partition_id(db, table, spec),
        "db": db,
        "table": table,
        "table_name": f"{db}.{table}",
        "layer": layer,
        "partition_spec": spec,
        "partition_parts": list(parts),
        "keys": keys,
        "date": date_value.isoformat() if date_value else None,
        "path": "/".join([layer, db, table, *parts]),
        "file_count": stats["file_count"],
        "total_bytes": stats["total_bytes"],
        "last_modified": stats["last_modified"],
        "largest_file": stats["largest_file"],
    })
    return records


def _scan_table(table_dir: Path, db: str, table: str, layer: str,
                actual_root: Path, unparsed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        entries = sorted(os.scandir(table_dir), key=lambda item: item.name)
    except OSError as exc:
        _unparsed(unparsed, table_dir, actual_root, f"unreadable table: {exc}")
        return records
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_file(follow_symlinks=False):
            _unparsed(unparsed, Path(entry.path), actual_root,
                      "files directly under table directory")
        elif entry.is_dir(follow_symlinks=False):
            if parse_partition_component(entry.name):
                records.extend(_scan_partition_node(
                    Path(entry.path), db, table, layer, [entry.name],
                    actual_root, unparsed))
            elif "=" in entry.name:
                _unparsed(unparsed, Path(entry.path), actual_root,
                          "malformed partition directory")
            else:
                _unparsed(unparsed, Path(entry.path), actual_root,
                          "invalid partition directory (expected key=value)")
    return records


def scan_lake(lake_root: str | Path) -> dict[str, Any]:
    """Scan a tiered lake and return a stable, largely portable inventory."""
    root = Path(lake_root).expanduser().resolve()
    if not root.exists():
        raise DataError(f"lake root does not exist: {lake_root}")
    if not root.is_dir():
        raise DataError(f"lake root is not a directory: {lake_root}")
    partitions: list[dict[str, Any]] = []
    unparsed: list[dict[str, Any]] = []
    for layer in TIERS:
        layer_dir = root / layer
        if not layer_dir.is_dir():
            continue
        for db_entry in sorted(os.scandir(layer_dir), key=lambda item: item.name):
            if db_entry.is_symlink():
                continue
            if not db_entry.is_dir(follow_symlinks=False):
                _unparsed(unparsed, Path(db_entry.path), root,
                          "files directly under layer directory")
                continue
            db_dir = Path(db_entry.path)
            for table_entry in sorted(os.scandir(db_dir), key=lambda item: item.name):
                if table_entry.is_symlink():
                    continue
                if not table_entry.is_dir(follow_symlinks=False):
                    _unparsed(unparsed, Path(table_entry.path), root,
                              "files directly under database directory")
                    continue
                partitions.extend(_scan_table(
                    Path(table_entry.path), db_entry.name, table_entry.name,
                    layer, root, unparsed))
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for record in sorted(partitions, key=lambda item: (
            item["table_name"], item["partition_spec"], item["layer"])):
        if record["partition_id"] in seen:
            _unparsed(unparsed, root / record["path"], root,
                      "duplicate partition across layers")
            continue
        seen.add(record["partition_id"])
        unique.append(record)
    unparsed.sort(key=lambda item: (item["path"], item["reason"]))
    tables: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in unique:
        key = (record["layer"], record["db"], record["table"])
        summary = tables.setdefault(key, {
            "layer": record["layer"], "db": record["db"],
            "table": record["table"], "partition_count": 0,
            "total_bytes": 0})
        summary["partition_count"] += 1
        summary["total_bytes"] += record["total_bytes"]
    table_rows = [tables[key] for key in sorted(tables)]
    payload = {
        "schema_version": 1,
        "layout": LAYOUT,
        "partitions": unique,
        "tables": table_rows,
        "unparsed": unparsed,
    }
    payload["inventory_hash"] = sha256_obj({
        "schema_version": 1, "layout": LAYOUT,
        "partitions": unique, "tables": table_rows, "unparsed": unparsed})
    return payload
