import copy

import pytest

from tier_triage.decide.planner import (
    PlanIntegrityError,
    build_plan,
    load_plan,
    save_plan,
    validate_plan_integrity,
)


def _policy_data(base_policy):
    data = copy.deepcopy(base_policy)
    data["tables"] = [{
        "match": "demo.*", "priority": 10, "purge_allowed": True,
        "purge_after_days": 365,
    }]
    return data


def _lake_with_partitions(lake):
    lake.make("hot", "demo", "t", ["dt=2026-10-30"], files=1)
    lake.make("hot", "demo", "t", ["dt=2026-09-01"], files=1)
    lake.make("hot", "demo", "t", ["dt=2026-01-01"], files=1)
    lake.make("warm", "demo", "t", ["dt=2025-06-01"], files=1)
    lake.make("cold", "demo", "t", ["dt=2024-01-01"], files=1)
    lake.make("cold", "other", "t", ["dt=2024-01-01"], files=1)


def test_plan_is_deterministic_and_contains_all_action_types(
        lake, pipeline, base_policy):
    _lake_with_partitions(lake)
    data = _policy_data(base_policy)
    first = pipeline(data)
    second = pipeline(data)
    assert first.plan["plan_hash"] == second.plan["plan_hash"]
    assert first.plan["plan_id"] == second.plan["plan_id"]
    assert [a["seq"] for a in first.plan["actions"]] == list(
        range(1, len(first.plan["actions"]) + 1))
    actions = {row["partition_id"]: row for row in first.plan["actions"]}
    assert actions["demo.t.dt=2026-10-30"]["action"] == "noop"
    assert actions["demo.t.dt=2026-09-01"]["action"] == "move"
    assert actions["demo.t.dt=2026-09-01"]["target_layer"] == "warm"
    assert actions["demo.t.dt=2026-01-01"]["target_layer"] == "cold"
    assert actions["demo.t.dt=2024-01-01"]["action"] == "mark_purge"
    assert first.plan["summary"]["ready_actions"] > 0
    assert first.plan["summary"]["action_counts"]["noop"] >= 2


def test_blocked_when_target_exists_or_source_missing(
        lake, pipeline, base_policy):
    _lake_with_partitions(lake)
    run = pipeline(_policy_data(base_policy))
    target = lake.root / "warm/demo/t/dt=2026-09-01"
    target.mkdir(parents=True)
    conflicted = build_plan(run.evaluation, run.inventory, lake.root)
    row = next(item for item in conflicted["actions"]
               if item["partition_id"] == "demo.t.dt=2026-09-01")
    assert row["status"] == "blocked"
    assert row["reason"] == "target path already exists"
    target.rmdir()
    source = lake.root / "hot/demo/t/dt=2026-09-01"
    for path in sorted(source.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    source.rmdir()
    missing = build_plan(run.evaluation, run.inventory, lake.root)
    row = next(item for item in missing["actions"]
               if item["partition_id"] == "demo.t.dt=2026-09-01")
    assert row["status"] == "blocked"
    assert row["reason"] == "source path is missing"


def test_plan_roundtrip_and_integrity_rejects_tampering(
        lake, pipeline, base_policy, tmp_path):
    _lake_with_partitions(lake)
    plan = pipeline(_policy_data(base_policy)).plan
    path = tmp_path / "plan.json"
    save_plan(path, plan)
    loaded = load_plan(path)
    assert loaded["plan_hash"] == plan["plan_hash"]
    tampered = copy.deepcopy(plan)
    tampered["actions"][0]["bytes"] += 1
    with pytest.raises(PlanIntegrityError, match="hash mismatch"):
        validate_plan_integrity(tampered)
    tampered["plan_hash"] = "0" * 64
    with pytest.raises(PlanIntegrityError, match="hash mismatch"):
        validate_plan_integrity(tampered)
