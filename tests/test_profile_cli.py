"""CLI integration for the volume profile and profile-aware evaluate/plan."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

from tier_triage.cli import main


def _write_policy(tmp_path, *, max_warm_bytes=None):
    data = {
        "version": 1, "windows": [7, 30, 90],
        "defaults": {
            "hot_retain_days": 7, "hot_min_reads_30d": 5,
            "warm_after_days": 30, "cold_after_days": 90,
            "cold_max_reads_30d": 1, "purge_after_days": 180,
            "purge_idle_days": 30, "purge_max_reads_30d": 0,
            "purge_allowed": False, "max_hot_bytes": 1024,
        },
        "tables": [],
    }
    if max_warm_bytes is not None:
        data["defaults"]["max_warm_bytes"] = max_warm_bytes
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _lake(root: Path):
    # Young, hot by age/reads but 2 KiB > 1 KiB hot limit.
    big = root / "hot/demo/t/dt=2026-10-30"
    big.mkdir(parents=True)
    (big / "part-0000.bin").write_text("x" * 2048, encoding="utf-8")
    small = root / "hot/demo/t/dt=2026-09-01"
    small.mkdir(parents=True)
    (small / "part-0000.txt").write_text("y" * 64, encoding="utf-8")


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_profile_command_is_deterministic_and_scan_emits_profile(
        lake, tmp_path):
    _lake(lake.root)
    db = tmp_path / "ledger.sqlite"
    profile_a = tmp_path / "profile-a.json"
    profile_b = tmp_path / "profile-b.json"
    assert main(["profile", "--lake", str(lake.root),
                 "--as-of", "2026-11-01T00:00:00Z",
                 "--out", str(profile_a)]) == 0
    assert main(["profile", "--lake", str(lake.root),
                 "--as-of", "2026-11-01T00:00:00Z",
                 "--out", str(profile_b)]) == 0
    assert _file_hash(profile_a) == _file_hash(profile_b)
    payload = json.loads(profile_a.read_text(encoding="utf-8"))
    assert payload["as_of"] == "2026-11-01T00:00:00Z"
    assert [r["partition_id"] for r in payload["partitions"]] == sorted(
        r["partition_id"] for r in payload["partitions"])
    # scan --profile-out produces the same profile artifact when the same
    # clock is injected.
    scan_profile = tmp_path / "scan-profile.json"
    assert main(["scan", "--lake", str(lake.root), "--db", str(db),
                 "--as-of", "2026-11-01T00:00:00Z",
                 "--out", str(tmp_path / "inventory.json"),
                 "--profile-out", str(scan_profile)]) == 0
    scan_payload = json.loads(scan_profile.read_text(encoding="utf-8"))
    assert scan_payload["profile_hash"] == payload["profile_hash"]
    # Without a clock the scan profile is a pure physical snapshot.
    physical = tmp_path / "physical.json"
    assert main(["scan", "--lake", str(lake.root), "--db", str(db),
                 "--out", str(tmp_path / "inventory2.json"),
                 "--profile-out", str(physical)]) == 0
    assert json.loads(physical.read_text())["as_of"] is None


def test_profile_aware_plan_forces_oversize_out_of_hot(lake, tmp_path):
    _lake(lake.root)
    policy = _write_policy(tmp_path)
    access = tmp_path / "access.jsonl"
    access.write_text("", encoding="utf-8")
    db = tmp_path / "ledger.sqlite"
    artifacts = tmp_path / "artifacts"
    profile_path = artifacts / "volume_profile.json"
    plan_path = artifacts / "plan.json"
    common = ["--lake", str(lake.root), "--usage", str(access),
              "--policy", str(policy), "--db", str(db),
              "--as-of", "2026-11-01T00:00:00Z"]
    assert main(["profile", "--lake", str(lake.root),
                 "--as-of", "2026-11-01T00:00:00Z",
                 "--out", str(profile_path)]) == 0
    assert main(["plan", *common, "--profile", str(profile_path),
                 "--out", str(plan_path)]) == 0
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    action = next(a for a in plan["actions"]
                  if a["partition_id"] == "demo.t.dt=2026-10-30")
    assert action["target_layer"] == "warm"
    assert "size_forced=warm" in action["reason"]
    assert plan["capacity_profile"]["oversize_partitions"] == [
        "demo.t.dt=2026-10-30"]
    # Re-running yields an identical plan hash.
    plan_again = artifacts / "plan-again.json"
    assert main(["plan", *common, "--profile", str(profile_path),
                 "--out", str(plan_again)]) == 0
    assert json.loads(plan_again.read_text())["plan_hash"] == \
        plan["plan_hash"]


def test_legacy_plan_without_profile_matches_original_behavior(
        lake, tmp_path):
    _lake(lake.root)
    policy = _write_policy(tmp_path)
    access = tmp_path / "access.jsonl"
    access.write_text("", encoding="utf-8")
    db = tmp_path / "ledger.sqlite"
    common = ["--lake", str(lake.root), "--usage", str(access),
              "--policy", str(policy), "--db", str(db),
              "--as-of", "2026-11-01T00:00:00Z"]
    legacy_plan = tmp_path / "legacy.json"
    assert main(["plan", *common, "--out", str(legacy_plan)]) == 0
    plan = json.loads(legacy_plan.read_text())
    assert "capacity_profile" not in plan
    young = next(a for a in plan["actions"]
                 if a["partition_id"] == "demo.t.dt=2026-10-30")
    # Retention floor keeps the young oversize partition hot without profile.
    assert young["action"] == "noop"
    assert "capacity" not in young
    # Legacy evaluation artifact likewise has no capacity block.
    evaluation_path = tmp_path / "evaluation.json"
    assert main(["evaluate", *common, "--out", str(evaluation_path)]) == 0
    evaluation = json.loads(evaluation_path.read_text())
    assert "capacity_profile" not in evaluation
    assert all("capacity" not in d for d in evaluation["decisions"])


def test_unreadable_directory_leaves_anomaly_and_flow_completes(
        lake, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")
    _lake(lake.root)
    bad = lake.root / "hot/demo/t/dt=2026-10-28"
    bad.mkdir(parents=True)
    (bad / "secret.bin").write_text("z" * 32, encoding="utf-8")
    os.chmod(bad, 0o000)
    try:
        profile_path = tmp_path / "profile.json"
        code = main(["profile", "--lake", str(lake.root),
                     "--out", str(profile_path)])
    finally:
        os.chmod(bad, 0o755)
    assert code == 0
    profile = json.loads(profile_path.read_text())
    assert any(a["kind"] == "permission_denied" and
               a["partition_id"] == "demo.t.dt=2026-10-28"
               for a in profile["anomalies"])
    row = next(r for r in profile["partitions"]
               if r["partition_id"] == "demo.t.dt=2026-10-28")
    assert row["status"] == "unreadable"


def test_stale_profile_is_rejected_by_plan(lake, tmp_path):
    _lake(lake.root)
    policy = _write_policy(tmp_path)
    access = tmp_path / "access.jsonl"
    access.write_text("", encoding="utf-8")
    profile_path = tmp_path / "profile.json"
    assert main(["profile", "--lake", str(lake.root),
                 "--out", str(profile_path)]) == 0
    stale = json.loads(profile_path.read_text())
    stale["inventory_hash"] = "f" * 64
    stale["profile_hash"] = "f" * 64
    profile_path.write_text(json.dumps(stale), encoding="utf-8")
    # The CLI turns the integrity error into exit code 2 instead of crashing.
    assert main(["plan", "--lake", str(lake.root), "--usage", str(access),
                 "--policy", str(policy), "--db", str(tmp_path / "db.sqlite"),
                 "--as-of", "2026-11-01T00:00:00Z",
                 "--profile", str(profile_path),
                 "--out", str(tmp_path / "plan.json")]) == 2


def test_warm_budget_is_reported_in_cli_plan(lake, tmp_path):
    _lake(lake.root)
    resident = lake.root / "warm/demo/t/dt=2026-08-01"
    resident.mkdir(parents=True)
    (resident / "r.bin").write_text("z" * 900, encoding="utf-8")
    policy = _write_policy(tmp_path, max_warm_bytes=1000)
    access = tmp_path / "access.jsonl"
    access.write_text("", encoding="utf-8")
    profile_path = tmp_path / "profile.json"
    plan_path = tmp_path / "plan.json"
    assert main(["profile", "--lake", str(lake.root),
                 "--out", str(profile_path)]) == 0
    assert main(["plan", "--lake", str(lake.root), "--usage", str(access),
                 "--policy", str(policy), "--db", str(tmp_path / "db.sqlite"),
                 "--as-of", "2026-11-01T00:00:00Z",
                 "--profile", str(profile_path),
                 "--out", str(plan_path)]) == 0
    plan = json.loads(plan_path.read_text())
    blocked = plan["capacity_profile"]["pressure"]["capacity_blocked"]
    assert [row["partition_id"] for row in blocked] == [
        "demo.t.dt=2026-10-30"]
