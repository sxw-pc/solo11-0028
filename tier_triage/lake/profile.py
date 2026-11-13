"""Independent partition volume profiling.

The volume profile is a *separate artifact* from the lake inventory. The
inventory answers "which partitions exist and how are they parsed"; this
module answers "how much physical data does every partition actually hold".

Design invariants:

* Deterministic: every directory listing is sorted before iteration, every
  output list is sorted on stable keys, and no wall-clock value participates
  in the profile hash. Repeated runs against the same lake directory produce
  byte-identical artifacts.
* Safe walking: symbolic links are never followed and never contribute bytes;
  they are recorded in ``anomalies`` instead. Unreadable directories, missing
  files and unparsable entries likewise land in ``anomalies`` — a single bad
  partition never aborts the whole scan.
* Self-contained: :func:`build_profile` derives its partition universe from a
  fresh inventory scan plus an independent partition-candidate walk, so it can
  run standalone and still account for partitions the inventory had to drop
  (for example a permission-denied partition directory).
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

from tier_triage.lake.inventory import scan_lake
from tier_triage.lake.partitioning import (
    TIERS,
    make_partition_id,
    parse_partition_component,
    partition_spec,
)
from tier_triage.util import (
    DataError,
    iso_z,
    parse_datetime,
    sha256_obj,
    write_json,
    load_json,
)

PROFILE_SCHEMA_VERSION = 1
ANOMALY_KINDS = (
    "permission_denied",
    "missing",
    "symlink_skipped",
    "unreadable_entry",
    "malformed_partition_directory",
    "invalid_partition_directory",
    "inventory_unparsed",
)
# Inventory unparsed reasons that the profile walker already records in its
# own vocabulary. Mirroring them would only create duplicate anomalies.
_WALK_OWNED_REASONS = (
    "symlink",
    "unreadable",
    "malformed partition directory",
    "invalid partition directory",
)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _add(anomalies: list[dict[str, Any]], path: str, kind: str,
         detail: str, partition_id: str | None = None) -> None:
    anomalies.append({
        "path": path,
        "partition_id": partition_id,
        "kind": kind,
        "detail": detail,
    })


def _list_dir(path: Path) -> tuple[list[os.DirEntry] | None, OSError | None]:
    """Return ``(sorted entries, None)`` or ``(None, error)`` on failure."""
    try:
        return sorted(os.scandir(path), key=lambda item: item.name), None
    except OSError as exc:
        return None, exc


def _failure_kind(exc: OSError) -> str:
    if isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT:
        return "missing"
    if isinstance(exc, PermissionError) or exc.errno == errno.EACCES:
        return "permission_denied"
    return "unreadable_entry"


def _walk_partition(root: Path, partition_path: Path, partition_id: str,
                    anomalies: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Walk one partition directory without following symlinks.

    Returns ``None`` when the partition root itself cannot be read; otherwise
    returns accumulated counters. A failure inside a nested subdirectory is
    non-fatal: bytes already counted are retained, an anomaly is recorded and
    the partition status becomes ``incomplete``.
    """
    file_count = 0
    total_bytes = 0
    incomplete = False
    root_entries, root_error = _list_dir(partition_path)
    if root_entries is None:
        kind = _failure_kind(root_error)
        _add(anomalies, _rel(partition_path, root), kind,
             f"directory not readable: {root_error}", partition_id)
        return None
    stack: list[list[os.DirEntry]] = [root_entries]
    while stack:
        entries = stack.pop()
        for entry in entries:
            entry_path = Path(entry.path)
            rel = _rel(entry_path, root)
            try:
                if entry.is_symlink():
                    _add(anomalies, rel, "symlink_skipped",
                         "symbolic link excluded from volume accounting",
                         partition_id)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if parse_partition_component(entry.name):
                        # Nested partition node: owned by its own profile row.
                        continue
                    if "=" in entry.name:
                        _add(anomalies, rel, "malformed_partition_directory",
                             "malformed partition directory excluded",
                             partition_id)
                        continue
                    child_entries, child_error = _list_dir(entry_path)
                    if child_entries is None:
                        # Nested unreadable directory: keep what we have and
                        # mark the partition incomplete rather than discarding
                        # the bytes counted elsewhere.
                        incomplete = True
                        _add(anomalies, rel, _failure_kind(child_error),
                             f"directory not readable: {child_error}",
                             partition_id)
                        continue
                    stack.append(child_entries)
                elif entry.is_file(follow_symlinks=False):
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        incomplete = True
                        _add(anomalies, rel, "missing",
                             "file vanished during scan", partition_id)
                        continue
                    except PermissionError as exc:
                        incomplete = True
                        _add(anomalies, rel, "permission_denied",
                             f"file not readable: {exc}", partition_id)
                        continue
                    file_count += 1
                    total_bytes += int(stat.st_size)
            except PermissionError as exc:
                incomplete = True
                _add(anomalies, rel, "permission_denied",
                     f"entry not accessible: {exc}", partition_id)
            except OSError as exc:
                incomplete = True
                _add(anomalies, rel, "unreadable_entry",
                     f"entry not accessible: {exc}", partition_id)
    return {
        "file_count": file_count,
        "total_bytes": total_bytes,
        "incomplete": incomplete,
    }


def _enumerate_partition_candidates(
        root: Path, anomalies: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Independent, sorted enumeration of every valid partition directory.

    This walk is deliberately separate from the inventory parser so that a
    permission-denied partition still appears in the profile (as an
    ``unreadable`` row plus an anomaly) instead of silently disappearing.
    """
    candidates: list[dict[str, str]] = []

    def make_candidate(node: Path, db: str, table: str,
                       parts: list[str]) -> dict[str, str]:
        spec = partition_spec(parts)
        return {
            "partition_id": make_partition_id(db, table, spec),
            "table": f"{db}.{table}",
            "partition_spec": spec,
            "path": _rel(node, root),
        }

    def descend(node: Path, db: str, table: str, parts: list[str]) -> None:
        entries, error = _list_dir(node)
        if entries is None:
            # Retain the node as a candidate so an unreadable partition still
            # surfaces as an ``unreadable`` row; the partition walk below
            # emits the authoritative, partition-scoped anomaly.
            candidates.append(make_candidate(node, db, table, parts))
            _add(anomalies, _rel(node, root), _failure_kind(error),
                 f"partition directory not readable: {error}")
            return
        child_nodes: list[Path] = []
        for entry in entries:
            if entry.is_symlink():
                _add(anomalies, _rel(Path(entry.path), root),
                     "symlink_skipped",
                     "symbolic link excluded from volume accounting")
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
            if parse_partition_component(entry.name):
                child_nodes.append(Path(entry.path))
            elif "=" in entry.name:
                _add(anomalies, _rel(Path(entry.path), root),
                     "malformed_partition_directory",
                     "malformed partition directory excluded from candidates")
        # Every partition node is a candidate, including a node that holds its
        # own data files *and* nested partition dirs (mixed granularity). The
        # inventory emits rows for both; the walk skips the nested partition
        # subtree when summing this node's own bytes.
        if parts:
            candidates.append(make_candidate(node, db, table, parts))
        for child in child_nodes:
            descend(child, db, table, parts + [child.name])

    for layer in TIERS:
        layer_dir = root / layer
        if layer_dir.is_symlink():
            _add(anomalies, layer, "symlink_skipped",
                 "symbolic link layer directory excluded from volume accounting")
            continue
        if not layer_dir.is_dir():
            continue
        db_entries, layer_error = _list_dir(layer_dir)
        if db_entries is None:
            _add(anomalies, _rel(layer_dir, root),
                 _failure_kind(layer_error),
                 f"layer directory not readable: {layer_error}")
            continue
        for db_entry in db_entries:
            if db_entry.is_symlink():
                _add(anomalies, _rel(Path(db_entry.path), root),
                     "symlink_skipped",
                     "symbolic link database directory excluded")
                continue
            if not db_entry.is_dir(follow_symlinks=False):
                continue
            db_dir = Path(db_entry.path)
            table_entries, db_error = _list_dir(db_dir)
            if table_entries is None:
                _add(anomalies, _rel(db_dir, root),
                     _failure_kind(db_error),
                     f"database directory not readable: {db_error}")
                continue
            for table_entry in table_entries:
                if table_entry.is_symlink():
                    _add(anomalies, _rel(Path(table_entry.path), root),
                         "symlink_skipped",
                         "symbolic link table directory excluded")
                    continue
                if not table_entry.is_dir(follow_symlinks=False):
                    continue
                table_dir = Path(table_entry.path)
                partition_entries, table_error = _list_dir(table_dir)
                if partition_entries is None:
                    _add(anomalies, _rel(table_dir, root),
                         _failure_kind(table_error),
                         f"table directory not readable: {table_error}")
                    continue
                for partition_entry in partition_entries:
                    if partition_entry.is_symlink():
                        _add(anomalies,
                             _rel(Path(partition_entry.path), root),
                             "symlink_skipped",
                             "symbolic link partition directory excluded")
                        continue
                    if not partition_entry.is_dir(follow_symlinks=False):
                        continue
                    if not parse_partition_component(partition_entry.name):
                        if "=" in partition_entry.name:
                            _add(anomalies,
                                 _rel(Path(partition_entry.path), root),
                                 "malformed_partition_directory",
                                 "malformed partition directory excluded "
                                 "from candidates")
                        else:
                            _add(anomalies,
                                 _rel(Path(partition_entry.path), root),
                                 "invalid_partition_directory",
                                 "directory under table is not a key=value "
                                 "partition")
                        continue
                    descend(Path(partition_entry.path), db_entry.name,
                            table_entry.name, [partition_entry.name])
    # Path-sort tie-break for cross-layer duplicates matches the inventory
    # dedup outcome (the deeper tier path survives); the inventory also emits
    # a "duplicate partition across layers" unparsed entry for the loser.
    candidates.sort(key=lambda row: (row["partition_id"], row["path"]))
    return candidates


def build_profile(lake_root: str | Path,
                  as_of: str | Any | None = None) -> dict[str, Any]:
    """Build a deterministic volume profile for every partition in the lake.

    ``as_of`` is optional: a profile is a physical snapshot and may carry no
    evaluation clock. When supplied it is normalized to second-precision UTC.
    """
    inventory = scan_lake(lake_root)
    root = Path(lake_root).expanduser().resolve()
    clock = None
    if as_of is not None:
        clock = iso_z(parse_datetime(as_of))
    anomalies: list[dict[str, Any]] = []
    for item in inventory.get("unparsed", []):
        if any(token in item["reason"] for token in _WALK_OWNED_REASONS):
            continue
        _add(anomalies, item["path"], "inventory_unparsed", item["reason"])

    candidates = _enumerate_partition_candidates(root, anomalies)
    inventory_rows = {part["partition_id"]: part
                      for part in inventory.get("partitions", [])}
    seen_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        partition_id = candidate["partition_id"]
        if partition_id in seen_ids:
            continue
        seen_ids.add(partition_id)
        known = inventory_rows.get(partition_id)
        layer = known["layer"] if known else candidate["path"].split("/", 1)[0]
        walked = _walk_partition(root, root / candidate["path"],
                                 partition_id, anomalies)
        if walked is None:
            status = "unreadable"
            file_count = 0
            total_bytes = 0
        else:
            status = "incomplete" if walked["incomplete"] else "ok"
            file_count = walked["file_count"]
            total_bytes = walked["total_bytes"]
        # Reconcile with the inventory. A node the inventory skips (an empty
        # intermediate partition directory) is omitted here too — unless it
        # is unreadable, in which case keeping the row is the only way to show
        # the permission failure. Everything the inventory does expose is
        # always represented, so profile consumers never see a missing row.
        if known is None and status == "ok" and file_count == 0:
            continue
        rows.append({
            "partition_id": partition_id,
            "table": candidate["table"],
            "partition_spec": candidate["partition_spec"],
            "layer": layer,
            "path": candidate["path"],
            "status": status,
            "total_bytes": total_bytes,
            "file_count": file_count,
            "avg_file_size": (total_bytes // file_count
                              if file_count > 0 else None),
        })
    rows.sort(key=lambda row: row["partition_id"])
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for anomaly in anomalies:
        key = (anomaly["path"], anomaly["kind"], anomaly["detail"])
        deduped.setdefault(key, anomaly)
    anomaly_rows = sorted(
        deduped.values(),
        key=lambda row: (row["path"], row["kind"], row["detail"],
                         row.get("partition_id") or ""))
    totals = {
        "profiled_partitions": len(rows),
        "ok_partitions": sum(1 for row in rows if row["status"] == "ok"),
        "incomplete_partitions": sum(
            1 for row in rows if row["status"] == "incomplete"),
        "unreadable_partitions": sum(
            1 for row in rows if row["status"] == "unreadable"),
        "total_bytes": sum(row["total_bytes"] for row in rows),
        "total_files": sum(row["file_count"] for row in rows),
        "anomaly_count": len(anomaly_rows),
    }
    payload = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "layout": inventory.get("layout"),
        "as_of": clock,
        "inventory_hash": inventory.get("inventory_hash"),
        "partitions": rows,
        "anomalies": anomaly_rows,
        "summary": totals,
    }
    payload["profile_hash"] = sha256_obj(
        {key: value for key, value in payload.items() if key != "profile_hash"})
    return payload


def validate_profile(profile: dict[str, Any]) -> None:
    if not isinstance(profile, dict):
        raise DataError("volume profile must be a JSON object")
    if profile.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise DataError("unsupported volume profile schema_version")
    expected = profile.get("profile_hash")
    if not isinstance(expected, str) or len(expected) != 64:
        raise DataError("volume profile is missing a valid profile_hash")
    actual = sha256_obj(
        {key: value for key, value in profile.items() if key != "profile_hash"})
    if actual != expected:
        raise DataError(
            f"volume profile hash mismatch: expected {expected}, "
            f"recomputed {actual}")
    if not isinstance(profile.get("partitions"), list):
        raise DataError("volume profile partitions must be a list")
    if not isinstance(profile.get("anomalies"), list):
        raise DataError("volume profile anomalies must be a list")
    ids = [row.get("partition_id") for row in profile["partitions"]]
    if ids != sorted(ids):
        raise DataError("volume profile partitions must be sorted by partition_id")


def load_profile(path: str | Path) -> dict[str, Any]:
    profile = load_json(path)
    validate_profile(profile)
    return profile


def save_profile(path: str | Path, profile: dict[str, Any]) -> None:
    validate_profile(profile)
    write_json(path, profile)


def profile_index(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map partition id to its profile row for O(1) consumer lookups."""
    return {row["partition_id"]: row for row in profile.get("partitions", [])}
