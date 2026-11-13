"""Tests for the independent partition volume profile."""
from __future__ import annotations

import json
import os

import pytest

from tier_triage.lake.inventory import scan_lake
from tier_triage.lake.profile import (
    build_profile,
    load_profile,
    profile_index,
    save_profile,
)
from tier_triage.util import DataError, sha256_obj


def _make(root, layer, db, table, parts, files=1, padding=16):
    directory = root.joinpath(layer, db, table, *parts)
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        (directory / f"part-{index:04d}.txt").write_text(
            f"{db}.{table}|{index}|" + "x" * padding + "\n", encoding="utf-8")
    return directory


def test_profile_collects_bytes_files_and_average_size(lake):
    _make(lake.root, "hot", "sales", "orders", ["dt=2026-10-01"], files=3,
          padding=10)
    _make(lake.root, "warm", "ops", "audit_log",
          ["region=cn", "dt=2026-09-01"], files=1, padding=20)
    profile = build_profile(lake.root, "2026-11-01T00:00:00Z")
    rows = profile_index(profile)
    daily = rows["sales.orders.dt=2026-10-01"]
    assert daily["file_count"] == 3
    assert daily["total_bytes"] > 0
    assert daily["avg_file_size"] == daily["total_bytes"] // 3
    assert daily["status"] == "ok"
    assert profile["as_of"] == "2026-11-01T00:00:00Z"
    assert profile["summary"]["profiled_partitions"] == 2
    assert len(profile["profile_hash"]) == 64


def test_profile_is_byte_deterministic_across_runs(lake, tmp_path):
    _make(lake.root, "hot", "db", "table", ["dt=2026-10-01"], files=2)
    first = build_profile(lake.root, "2026-11-01T00:00:00Z")
    second = build_profile(lake.root, "2026-11-01T00:00:00Z")
    assert first == second
    out = tmp_path / "profile-a.json"
    save_profile(out, first)
    reloaded = load_profile(out)
    assert reloaded == first
    assert json.loads(out.read_text(encoding="utf-8"))["partitions"] == sorted(
        json.loads(out.read_text(encoding="utf-8"))["partitions"],
        key=lambda row: row["partition_id"])


def test_profile_without_as_of_is_a_physical_snapshot(lake):
    _make(lake.root, "hot", "db", "table", ["dt=2026-10-01"])
    profile = build_profile(lake.root)
    assert profile["as_of"] is None
    assert profile["profile_hash"] == build_profile(lake.root)["profile_hash"]


def test_symlinks_are_excluded_from_bytes_and_reported(lake):
    directory = _make(lake.root, "hot", "db", "table", ["dt=2026-10-01"])
    target = directory / "part-0000.txt"
    os.symlink(target, directory / "link.txt")
    profile = build_profile(lake.root)
    row = profile_index(profile)["db.table.dt=2026-10-01"]
    assert row["file_count"] == 1
    assert row["total_bytes"] == target.stat().st_size
    kinds = {(a["path"], a["kind"]) for a in profile["anomalies"]}
    assert any(kind == "symlink_skipped" and path.endswith("link.txt")
               for path, kind in kinds)


def test_unreadable_partition_is_anomaly_not_scan_abort(lake):
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")
    good = _make(lake.root, "hot", "db", "table", ["dt=2026-10-02"])
    bad = _make(lake.root, "hot", "db", "table", ["dt=2026-10-03"])
    os.chmod(bad, 0o000)
    try:
        profile = build_profile(lake.root)
        rows = profile_index(profile)
        assert rows["db.table.dt=2026-10-02"]["status"] == "ok"
        assert rows["db.table.dt=2026-10-03"]["status"] == "unreadable"
        assert rows["db.table.dt=2026-10-03"]["total_bytes"] == 0
        trace = [a for a in profile["anomalies"]
                 if a["partition_id"] == "db.table.dt=2026-10-03"]
        assert trace and trace[0]["kind"] == "permission_denied"
        assert profile["summary"]["unreadable_partitions"] == 1
        # The scan completed and still hashes deterministically.
        assert profile == build_profile(lake.root)
    finally:
        os.chmod(bad, 0o755)
    assert good.exists()


def test_nested_unreadable_subdir_is_incomplete_not_fatal(lake):
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")
    directory = _make(lake.root, "hot", "ops", "audit",
                      ["region=cn", "dt=2026-10-01"], files=1, padding=10)
    (directory / "sub").mkdir()
    ((directory / "sub") / "nested.txt").write_text("y" * 20, encoding="utf-8")
    locked = directory / "locked"
    locked.mkdir()
    (locked / "secret.txt").write_text("z", encoding="utf-8")
    os.chmod(locked, 0o000)
    try:
        profile = build_profile(lake.root)
    finally:
        os.chmod(locked, 0o755)
    row = profile_index(profile)["ops.audit.region=cn/dt=2026-10-01"]
    # Bytes from readable siblings survive; only the locked subtree is missing.
    assert row["status"] == "incomplete"
    assert row["file_count"] == 2
    assert any(a["kind"] == "permission_denied" and
               a["partition_id"] == row["partition_id"]
               for a in profile["anomalies"])


def test_mixed_granularity_node_with_data_and_children(lake):
    # region=us holds its own file and also contains a nested dt partition;
    # both nodes must appear in the profile, with no double counting.
    parent = lake.root.joinpath("hot", "db", "t", "region=us")
    parent.mkdir(parents=True)
    (parent / "loose.bin").write_text("x" * 100, encoding="utf-8")
    child = parent / "dt=2026-10-01"
    child.mkdir()
    (child / "c.bin").write_text("y" * 50, encoding="utf-8")
    inventory = scan_lake(lake.root)
    profile = build_profile(lake.root)
    inv_ids = {row["partition_id"] for row in inventory["partitions"]}
    prof_ids = set(profile_index(profile))
    assert inv_ids <= prof_ids
    rows = profile_index(profile)
    assert rows["db.t.region=us"]["total_bytes"] == 100
    assert rows["db.t.region=us/dt=2026-10-01"]["total_bytes"] == 50


def test_symlinked_layer_directory_is_not_followed(lake, tmp_path):
    _make(lake.root, "hot", "db", "t", ["dt=2026-10-01"])
    outside = tmp_path / "outside"
    evil = outside / "evil" / "t" / "dt=2026-01-01"
    evil.mkdir(parents=True)
    (evil / "f.bin").write_text("z" * 123, encoding="utf-8")
    os.symlink(evil.parents[1], lake.root / "warm")
    profile = build_profile(lake.root)
    assert any(a["path"] == "warm" and a["kind"] == "symlink_skipped"
               for a in profile["anomalies"])
    # External bytes never enter the profile totals; only the hot row exists.
    assert set(profile_index(profile)) == {"db.t.dt=2026-10-01"}
    assert all(r["total_bytes"] != 123 for r in profile["partitions"])


def test_unreadable_table_directory_is_anomaly_not_silent_drop(lake):
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")
    _make(lake.root, "hot", "db", "good", ["dt=2026-10-01"])
    bad_table = lake.root / "hot" / "db" / "secret"
    partition = bad_table / "dt=2026-09-01"
    partition.mkdir(parents=True)
    (partition / "f.bin").write_text("y", encoding="utf-8")
    os.chmod(bad_table, 0o000)
    try:
        profile = build_profile(lake.root)
    finally:
        os.chmod(bad_table, 0o755)
    assert any("hot/db/secret" in a["path"] and
               a["kind"] == "permission_denied" for a in profile["anomalies"])
    assert "db.good.dt=2026-10-01" in profile_index(profile)


def test_malformed_and_invalid_directories_are_anomalies(lake):
    _make(lake.root, "hot", "db", "table", ["dt=2026-10-01"])
    malformed = lake.root / "hot/db/table/dt="
    malformed.mkdir(parents=True)
    invalid = lake.root / "hot/db/table/not-a-partition"
    invalid.mkdir(parents=True)
    profile = build_profile(lake.root)
    kinds = {a["kind"] for a in profile["anomalies"]}
    assert "malformed_partition_directory" in kinds
    assert "invalid_partition_directory" in kinds
    assert list(profile_index(profile)) == ["db.table.dt=2026-10-01"]


def test_profile_hash_tampering_is_rejected(lake, tmp_path):
    _make(lake.root, "hot", "db", "table", ["dt=2026-10-01"])
    path = tmp_path / "profile.json"
    save_profile(path, build_profile(lake.root))
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["partitions"][0]["total_bytes"] += 999
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(DataError, match="hash mismatch"):
        load_profile(path)


def test_profile_matches_inventory_hash_and_empty_lake(lake, tmp_path):
    _make(lake.root, "hot", "db", "table", ["dt=2026-10-01"])
    profile = build_profile(lake.root)
    assert profile["inventory_hash"] == scan_lake(lake.root)["inventory_hash"]
    empty = tmp_path / "empty"
    empty.mkdir()
    blank = build_profile(empty)
    assert blank["partitions"] == []
    assert blank["anomalies"] == []
    assert blank["summary"]["total_bytes"] == 0
    assert blank["profile_hash"] == sha256_obj(
        {k: v for k, v in blank.items() if k != "profile_hash"})
