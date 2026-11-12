import copy

import pytest

from tier_triage.policy.loader import PolicyError, load_policy
from tier_triage.policy.rules import resolve_policy


def test_policy_loads_and_fingerprint_is_stable(write_policy, base_policy):
    path = write_policy(base_policy)
    first = load_policy(path)
    second = load_policy(path)
    assert first.fingerprint == second.fingerprint
    assert first.windows == [7, 30, 90]
    changed = copy.deepcopy(base_policy)
    changed["defaults"]["warm_after_days"] = 31
    third = load_policy(write_policy(changed, "changed.yaml"))
    assert third.fingerprint != first.fingerprint


def test_unknown_field_reports_line(write_policy, base_policy):
    data = copy.deepcopy(base_policy)
    data["defaults"]["mystery"] = 1
    path = write_policy(data)
    with pytest.raises(PolicyError) as caught:
        load_policy(path)
    assert "unknown field 'mystery'" in str(caught.value)
    line = next(index for index, text in
                enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                if "mystery" in text)
    assert f"line {line}" in str(caught.value)


def test_schema_rejects_missing_and_wrong_types(write_policy, base_policy):
    missing = copy.deepcopy(base_policy)
    del missing["defaults"]["max_hot_bytes"]
    with pytest.raises(PolicyError, match="missing required fields"):
        load_policy(write_policy(missing, "missing.yaml"))
    wrong = copy.deepcopy(base_policy)
    wrong["defaults"]["purge_allowed"] = "yes"
    with pytest.raises(PolicyError, match="must be a boolean"):
        load_policy(write_policy(wrong, "wrong.yaml"))
    no_window = copy.deepcopy(base_policy)
    no_window["windows"] = [7, 90]
    with pytest.raises(PolicyError, match="include 30"):
        load_policy(write_policy(no_window, "window.yaml"))


def test_table_priority_overrides_and_provenance(write_policy, base_policy):
    data = copy.deepcopy(base_policy)
    data["tables"] = [
        {"match": "demo.*", "priority": 10, "hot_retain_days": 5,
         "warm_after_days": 20},
        {"match": "demo.orders", "priority": 20, "hot_retain_days": 2},
    ]
    policy = load_policy(write_policy(data))
    resolved = policy.resolve("demo.orders")
    assert resolved["config"]["hot_retain_days"] == 2
    assert resolved["config"]["warm_after_days"] == 20
    assert resolved["provenance"]["hot_retain_days"] == "demo.orders"
    assert resolved["provenance"]["warm_after_days"] == "demo.*"
    assert [rule["match"] for rule in resolved["matched"]] == [
        "demo.orders", "demo.*"]


def test_defaults_apply_without_matching_rule(base_policy):
    resolved = resolve_policy(base_policy, "unknown.table")
    assert resolved["matched"] == []
    assert resolved["provenance"]["cold_after_days"] == "defaults"
    assert resolved["config"]["cold_after_days"] == 90


def test_invalid_ordering_and_rule_field(write_policy, base_policy):
    bad = copy.deepcopy(base_policy)
    bad["defaults"]["hot_retain_days"] = 50
    with pytest.raises(PolicyError, match="hot_retain_days"):
        load_policy(write_policy(bad, "ordering.yaml"))
    bad_rule = copy.deepcopy(base_policy)
    bad_rule["tables"] = [{"match": "x.*", "priority": 1, "nope": 1}]
    with pytest.raises(PolicyError, match="unknown field 'nope'"):
        load_policy(write_policy(bad_rule, "rule.yaml"))
