"""Tests for volume-profile-driven evaluation and capacity-pressure planning."""
from __future__ import annotations

import copy
import os
from datetime import datetime, timezone

import pytest

from tier_triage.decide.planner import build_plan
from tier_triage.decide.scorer import evaluate_inventory, size_band
from tier_triage.lake.inventory import scan_lake
from tier_triage.lake.profile import build_profile
from tier_triage.policy.loader import load_policy
from tier_triage.usage.stats import compute_stats
from tier_triage.util import DataError

AS_OF = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _write_policy(tmp_path, data, name="policy.yaml"):
    import yaml
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    return path


def _policy_data(*, max_warm_bytes=0):
    data = {
        "version": 1,
        "windows": [7, 30, 90],
        "defaults": {
            "hot_retain_days": 7,
            "hot_min_reads_30d": 5,
            "warm_after_days": 30,
            "cold_after_days": 90,
            "cold_max_reads_30d": 1,
            "purge_after_days": 180,
            "purge_idle_days": 30,
            "purge_max_reads_30d": 0,
            "purge_allowed": False,
            "max_hot_bytes": 1024,
        },
        "tables": [],
    }
    if max_warm_bytes:
        data["defaults"]["max_warm_bytes"] = max_warm_bytes
    return data


def _partition(root, layer, spec, size):
    directory = root / layer / "demo" / "t" / spec
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "part-0000.bin").write_text("x" * size, encoding="utf-8")
    return directory


def _pipeline(root, tmp_path, *, max_warm_bytes=0, records=None):
    policy_path = _write_policy(tmp_path, _policy_data(max_warm_bytes=max_warm_bytes))
    policy = load_policy(policy_path)
    inventory = scan_lake(root)
    profile = build_profile(root, "2026-11-01T00:00:00Z")
    stats = compute_stats(inventory, records or [], AS_OF, policy.windows)
    legacy = evaluate_inventory(inventory, stats, policy, AS_OF)
    profiled = evaluate_inventory(inventory, stats, policy, AS_OF, profile)
    plan_legacy = build_plan(legacy, inventory, root)
    plan_profile = build_plan(profiled, inventory, root)
    return SimpleNamespaceShim(
        policy=policy, inventory=inventory, profile=profile, stats=stats,
        legacy=legacy, profiled=profiled,
        plan_legacy=plan_legacy, plan_profile=plan_profile)


class SimpleNamespaceShim:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_size_band_classification():
    assert size_band(0, 1000) == "empty"
    assert size_band(400, 1000) == "small"
    assert size_band(500, 1000) == "near_limit"
    assert size_band(1001, 1000) == "oversize"
    assert size_band(10, 0) == "oversize"


def test_oversize_young_partition_is_forced_out_of_hot(lake, tmp_path):
    # dt=2026-10-30 is inside the 7-day retention window, so age/reads alone
    # keep it hot; its 2 KiB size exceeds the 1 KiB hot byte limit.
    _partition(lake.root, "hot", "dt=2026-10-30", 2048)
    run = _pipeline(lake.root, tmp_path)
    legacy = {d["partition_id"]: d for d in run.legacy["decisions"]}
    profiled = {d["partition_id"]: d for d in run.profiled["decisions"]}
    pid = "demo.t.dt=2026-10-30"
    assert legacy[pid]["tier"] == "hot"
    assert "capacity" not in legacy[pid]
    assert profiled[pid]["tier"] == "warm"
    assert profiled[pid]["capacity"]["over_hot_limit"] is True
    assert profiled[pid]["capacity"]["hot_forbidden"] is True
    assert profiled[pid]["size_source"] == "volume_profile"
    assert profiled[pid]["size_band"] == "oversize"
    checks = {c["check"]: c for c in profiled[pid]["checks"]}
    assert checks["retention_floor"]["passed"] is True
    assert checks["capacity_hot_limit"]["passed"] is False
    reason_names = [c["check"] for c in profiled[pid]["reasons"]]
    assert reason_names == ["retention_floor", "capacity_hot_limit"]
    action = next(a for a in run.plan_profile["actions"] if a["partition_id"] == pid)
    assert action["action"] == "move" and action["target_layer"] == "warm"
    assert "size_forced=warm" in action["reason"]
    assert action["capacity"]["over_hot_limit"] is True


def test_legacy_evaluation_without_profile_is_unchanged(lake, tmp_path):
    _partition(lake.root, "hot", "dt=2026-10-30", 2048)
    _partition(lake.root, "hot", "dt=2026-09-01", 100)
    run = _pipeline(lake.root, tmp_path)
    # No profile-specific keys may leak into legacy artifacts.
    for decision in run.legacy["decisions"]:
        assert "capacity" not in decision
        assert "size_band" not in decision
        assert "size_source" not in decision
    assert "capacity_profile" not in run.plan_legacy
    assert "capacity_profile" not in run.legacy
    for action in run.plan_legacy["actions"]:
        assert "capacity" not in action
    # Legacy inline size check still governs without a profile.
    legacy_pid = {d["partition_id"]: d for d in run.legacy["decisions"]}
    assert legacy_pid["demo.t.dt=2026-10-30"]["tier"] == "hot"
    # Same inputs hashed twice give identical plans.
    assert run.plan_legacy["plan_hash"] == build_plan(
        run.legacy, run.inventory, lake.root)["plan_hash"]


def test_profiled_plan_is_deterministic_and_hash_stable(lake, tmp_path):
    _partition(lake.root, "hot", "dt=2026-10-30", 2048)
    _partition(lake.root, "hot", "dt=2026-09-01", 100)
    run = _pipeline(lake.root, tmp_path)
    again = build_plan(run.profiled, run.inventory, lake.root)
    assert run.plan_profile["plan_hash"] == again["plan_hash"]
    assert run.plan_profile["capacity_profile"]["profile_hash"] == \
        run.profile["profile_hash"]
    assert run.plan_profile["capacity_profile"]["oversize_partitions"] == [
        "demo.t.dt=2026-10-30"]


def test_profile_inventory_hash_mismatch_is_rejected(lake, tmp_path):
    _partition(lake.root, "hot", "dt=2026-10-30", 10)
    run = _pipeline(lake.root, tmp_path)
    stale = copy.deepcopy(run.profile)
    stale["inventory_hash"] = "0" * 64
    stale["profile_hash"] = "0" * 64
    with pytest.raises(DataError, match="inventory_hash does not match"):
        evaluate_inventory(run.inventory, run.stats, run.policy, AS_OF, stale)


def test_warm_budget_blocks_large_partition_not_small_old_one(lake, tmp_path):
    # Resident warm data consumes 600 B of the 1200 B warm budget.
    _partition(lake.root, "warm", "dt=2026-08-01", 600)
    # Inbound: a small-old 100 B partition and an oversize 2000 B one.
    _partition(lake.root, "hot", "dt=2026-09-01", 100)
    _partition(lake.root, "hot", "dt=2026-10-30", 2000)
    run = _pipeline(lake.root, tmp_path, max_warm_bytes=1200)
    actions = {a["partition_id"]: a for a in run.plan_profile["actions"]}
    small = actions["demo.t.dt=2026-09-01"]
    large = actions["demo.t.dt=2026-10-30"]
    assert small["status"] == "ready" and small["target_layer"] == "warm"
    assert large["status"] == "blocked"
    assert "warm capacity budget exceeded" in large["reason"]
    pressure = run.plan_profile["capacity_profile"]["pressure"]
    assert pressure["warm_budget_configured"] is True
    assert pressure["capacity_blocked"][0]["partition_id"] == \
        "demo.t.dt=2026-10-30"
    budget = pressure["table_budgets"][0]
    assert budget["within_budget"] is True
    # The old resident warm partition is decided cold, so its 600 B is
    # credited as a departure before admitting the 100 B inbound move.
    assert budget["resident_warm_bytes"] == 600
    assert budget["departing_warm_bytes"] == 600
    assert budget["projected_warm_bytes"] == 100
    # Relief order puts the largest partition ahead of the small-old one.
    relief_ids = [row["partition_id"] for row in pressure["relief_order"]]
    assert relief_ids.index("demo.t.dt=2026-10-30") < \
        relief_ids.index("demo.t.dt=2026-09-01")


def test_no_warm_budget_means_no_capacity_blocking(lake, tmp_path):
    _partition(lake.root, "warm", "dt=2026-08-01", 600)
    _partition(lake.root, "hot", "dt=2026-10-30", 2000)
    run = _pipeline(lake.root, tmp_path)
    pressure = run.plan_profile["capacity_profile"]["pressure"]
    assert pressure["warm_budget_configured"] is False
    assert pressure["capacity_blocked"] == []
    large = next(a for a in run.plan_profile["actions"]
                 if a["partition_id"] == "demo.t.dt=2026-10-30")
    assert large["status"] == "ready"


def test_unreadable_profile_row_keeps_pipeline_running(lake, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")
    good = _partition(lake.root, "hot", "dt=2026-10-30", 10)
    bad = _partition(lake.root, "hot", "dt=2026-10-29", 10)
    os.chmod(bad, 0o000)
    try:
        run = _pipeline(lake.root, tmp_path)
    finally:
        os.chmod(bad, 0o755)
    rows = {r["partition_id"]: r for r in run.profile["partitions"]}
    assert rows["demo.t.dt=2026-10-29"]["status"] == "unreadable"
    assert any(a["kind"] == "permission_denied" and
               a["partition_id"] == "demo.t.dt=2026-10-29"
               for a in run.profile["anomalies"])
    # The readable partition is still fully evaluated and planned; the
    # unreadable one is tracked in the profile without aborting the flow.
    decided = {d["partition_id"] for d in run.profiled["decisions"]}
    assert decided == {"demo.t.dt=2026-10-30"}
    planned = {a["partition_id"] for a in run.plan_profile["actions"]}
    assert planned == {"demo.t.dt=2026-10-30"}
    assert good.exists()


def test_optional_budget_absent_from_legacy_artifact_when_present_in_policy(
        lake, tmp_path):
    _partition(lake.root, "hot", "dt=2026-10-30", 10)
    plain_doc = _policy_data()
    plain_doc["tables"] = [
        {"match": "demo.*", "priority": 10, "hot_min_reads_30d": 9}]
    extended_doc = copy.deepcopy(plain_doc)
    extended_doc["defaults"]["max_warm_bytes"] = 9999
    extended_doc["tables"][0]["max_warm_bytes"] = 777
    plain_path = _write_policy(tmp_path, plain_doc, "plain.yaml")
    extended_path = _write_policy(tmp_path, extended_doc, "extended.yaml")
    plain_policy = load_policy(plain_path)
    extended_policy = load_policy(extended_path)
    assert plain_policy.fingerprint == extended_policy.fingerprint
    inventory = scan_lake(lake.root)
    plain_stats = compute_stats(inventory, [], AS_OF, plain_policy.windows)
    ext_stats = compute_stats(inventory, [], AS_OF, extended_policy.windows)
    plain_eval = evaluate_inventory(inventory, plain_stats, plain_policy, AS_OF)
    ext_eval = evaluate_inventory(inventory, ext_stats, extended_policy, AS_OF)
    # Same document shape plus an optional capacity field must not change the
    # profile-free artifact at all.
    assert plain_eval == ext_eval
    decision = ext_eval["decisions"][0]
    assert "max_warm_bytes" not in decision["policy"]
    assert "max_warm_bytes" not in decision["policy_sources"]
    assert all("max_warm_bytes" not in r.get("overrides", [])
               for r in decision["matched_rules"])
    assert "capacity" not in decision


def test_untrusted_profile_row_does_not_pass_hot_exclusion_on_zero(lake, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")
    big = _partition(lake.root, "hot", "dt=2026-10-30", 2048)
    os.chmod(big, 0o000)
    try:
        profile = build_profile(lake.root)
    finally:
        os.chmod(big, 0o755)
    row = next(r for r in profile["partitions"]
               if r["partition_id"] == "demo.t.dt=2026-10-30")
    assert row["status"] == "unreadable"
    # The untrusted zero must never be classified as a safe small partition.
    from tier_triage.decide.scorer import _profile_trusted
    assert _profile_trusted(row) is False


def test_warm_departure_credits_freed_space_before_admission(lake, tmp_path):
    # 800 B warm resident is old and decided cold (departs); a 400 B inbound
    # move then fits a 1000 B budget that the naive accounting would reject.
    _partition(lake.root, "warm", "dt=2026-01-01", 800)
    _partition(lake.root, "hot", "dt=2026-09-01", 400)
    run = _pipeline(lake.root, tmp_path, max_warm_bytes=1000)
    actions = {a["partition_id"]: a for a in run.plan_profile["actions"]}
    inbound = actions["demo.t.dt=2026-09-01"]
    assert inbound["status"] == "ready"
    budget = run.plan_profile["capacity_profile"]["pressure"][
        "table_budgets"][0]
    assert budget["resident_warm_bytes"] == 800
    assert budget["departing_warm_bytes"] == 800
    assert budget["projected_warm_bytes"] == 400


def test_relief_order_prefers_purge_then_largest(lake, tmp_path):
    _partition(lake.root, "hot", "dt=2026-09-01", 100)
    _partition(lake.root, "hot", "dt=2026-09-02", 500)
    data = _policy_data(max_warm_bytes=10)
    data["defaults"].update({
        "hot_retain_days": 7, "warm_after_days": 30, "cold_after_days": 60,
        "purge_after_days": 90, "purge_idle_days": 1,
        "purge_allowed": True})
    policy_path = _write_policy(tmp_path, data)
    policy = load_policy(policy_path)
    inventory = scan_lake(lake.root)
    profile = build_profile(lake.root)
    stats = compute_stats(inventory, [], AS_OF, policy.windows)
    evaluation = evaluate_inventory(inventory, stats, policy, AS_OF, profile)
    plan = build_plan(evaluation, inventory, lake.root)
    relief = plan["capacity_profile"]["pressure"]["relief_order"]
    # The 500 B partition is considered before the 100 B one.
    ids = [row["partition_id"] for row in relief]
    assert ids.index("demo.t.dt=2026-09-02") < ids.index("demo.t.dt=2026-09-01")


def test_optional_warm_budget_does_not_change_policy_fingerprint(
        lake, tmp_path):
    _partition(lake.root, "hot", "dt=2026-10-30", 10)
    plain = _write_policy(tmp_path, _policy_data(), "plain.yaml")
    extended = _write_policy(
        tmp_path, _policy_data(max_warm_bytes=9999), "extended.yaml")
    assert load_policy(plain).fingerprint == load_policy(extended).fingerprint
    resolved = load_policy(extended).resolve("demo.t")
    assert resolved["config"]["max_warm_bytes"] == 9999
    assert resolved["provenance"]["max_warm_bytes"] == "defaults"
    assert load_policy(plain).resolve("demo.t")["config"].get(
        "max_warm_bytes", 0) == 0
    assert "max_warm_bytes" not in load_policy(plain).resolve("demo.t")[
        "provenance"]


def test_audit_report_carries_capacity_section(lake, tmp_path):
    from tier_triage.audit.report import build_report, render_markdown
    _partition(lake.root, "warm", "dt=2026-08-01", 600)
    _partition(lake.root, "hot", "dt=2026-09-01", 100)
    _partition(lake.root, "hot", "dt=2026-10-30", 2000)
    run = _pipeline(lake.root, tmp_path, max_warm_bytes=1200)
    report = build_report(run.profiled, run.plan_profile, None)
    capacity = report["capacity_profile"]
    assert capacity is not None
    assert capacity["oversize_partitions"] == ["demo.t.dt=2026-10-30"]
    assert [row["partition_id"] for row in capacity["capacity_blocked"]] == [
        "demo.t.dt=2026-10-30"]
    assert capacity["table_budgets"][0]["table"] == "demo.t"
    markdown = render_markdown(report)
    assert "体积画像与容量压力" in markdown
    assert "demo.t.dt=2026-10-30" in markdown
    # A legacy plan (no profile) must not gain a capacity section.
    legacy_report = build_report(run.legacy, run.plan_legacy, None)
    assert legacy_report["capacity_profile"] is None
    assert "体积画像与容量压力" not in render_markdown(legacy_report)
