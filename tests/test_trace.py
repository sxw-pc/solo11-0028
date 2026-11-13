import copy
from datetime import datetime, timezone

from tier_triage.decide.planner import build_plan
from tier_triage.decide.scorer import (
    evaluate_inventory,
    render_explanation,
    score_partition,
)
from tier_triage.policy.loader import load_policy


def _policy(base_policy, rules):
    data = copy.deepcopy(base_policy)
    data["tables"] = rules
    return data


def test_override_order_matches_priority_then_match(write_policy, base_policy):
    data = _policy(base_policy, [
        {"match": "demo.*", "priority": 10, "purge_allowed": True,
         "max_hot_bytes": 4096},
        {"match": "demo.t", "priority": 30, "purge_allowed": False},
        {"match": "demo.t2", "priority": 10, "hot_retain_days": 5},
    ])
    resolved = load_policy(write_policy(data)).resolve("demo.t")
    trace = resolved["trace"]
    assert [rule["match"] for rule in trace["candidates"]] == [
        "demo.*", "demo.t"]
    chain = trace["field_chains"]["purge_allowed"]
    assert [entry["source"] for entry in chain] == [
        "defaults", "demo.*", "demo.t"]
    assert chain[-1]["value"] is False and chain[-1]["effective"] is True
    assert chain[0]["effective"] is False
    assert resolved["config"]["purge_allowed"] is False
    assert resolved["provenance"]["purge_allowed"] == "demo.t"


def test_defaults_path_is_visible_in_chain(write_policy, base_policy):
    resolved = load_policy(write_policy(base_policy)).resolve("unknown.table")
    trace = resolved["trace"]
    assert trace["candidates"] == []
    for field, chain in trace["field_chains"].items():
        assert len(chain) == 1
        assert chain[0]["source"] == "defaults"
        assert chain[0]["effective"] is True
        assert chain[0]["value"] == resolved["config"][field]


def test_explain_lists_max_hot_bytes_cause(lake, pipeline, base_policy):
    lake.make("hot", "demo", "t", ["dt=2026-10-01"], files=40, padding=64)
    data = _policy(base_policy, [
        {"match": "demo.*", "priority": 10, "max_hot_bytes": 2000,
         "hot_retain_days": 7, "hot_min_reads_30d": 1,
         "warm_after_days": 1000, "cold_after_days": 1000,
         "purge_after_days": 1000, "purge_idle_days": 10,
         "purge_max_reads_30d": 0, "cold_max_reads_30d": 0,
         "purge_allowed": False},
    ])
    run = pipeline(data)
    decision = run.evaluation["decisions"][0]
    # age < hot_retain_days would retain hot; size pressure must still demote
    assert decision["tier"] == "warm"
    assert decision["total_bytes"] > 2000
    explanation = render_explanation(decision)
    assert explanation["degraded"] is False
    assert "max_hot_bytes" in explanation["text"]
    assert "cannot remain hot" in explanation["text"]
    node = next(item for item in explanation["trace"]["nodes"]
                if item["rule"] == "warm_threshold")
    assert node["outcome"] == "adopted"
    assert node["size_blocked_hot"] is True
    size_component = next(item for item in node["components"]
                          if item["field"] == "max_hot_bytes")
    assert size_component["passed"] is True
    assert size_component["source"] == "demo.*"
    assert explanation["deciding_stage"] == 4


def test_purge_allowed_false_shows_veto_node(lake, pipeline, base_policy):
    lake.make("hot", "demo", "t", ["dt=2024-01-01"], files=1)
    data = _policy(base_policy, [
        {"match": "demo.*", "priority": 10, "purge_allowed": False,
         "purge_after_days": 30, "purge_idle_days": 1,
         "warm_after_days": 10, "cold_after_days": 20,
         "cold_max_reads_30d": 5},
    ])
    run = pipeline(data)
    decision = run.evaluation["decisions"][0]
    trace = decision["trace"]
    purge_node = next(item for item in trace["nodes"]
                      if item["rule"] == "purge_window")
    assert purge_node["outcome"] == "rejected"
    assert [veto["field"] for veto in purge_node["vetoes"]] == ["purge_allowed"]
    assert purge_node["vetoes"][0]["source"] == "demo.*"
    assert "veto: purge_allowed=False" in render_explanation(decision)["text"]


def test_evaluation_top_level_shape_and_plan_hash_unchanged(
        lake, pipeline, base_policy):
    lake.make("hot", "demo", "t", ["dt=2026-10-30"], files=1)
    lake.make("hot", "demo", "t", ["dt=2026-09-01"], files=2)
    run = pipeline(_policy(base_policy, [{"match": "demo.*", "priority": 10}]))
    assert set(run.evaluation) == {
        "schema_version", "as_of", "inventory_hash", "policy_fingerprint",
        "decisions", "summary"}
    assert set(run.evaluation["summary"]) == {
        "partitions", "tier_distribution", "decision_bytes"}
    fixed_plan = build_plan(run.evaluation, run.inventory, lake.root)
    assert fixed_plan["plan_hash"] == run.plan["plan_hash"]
    for decision in run.evaluation["decisions"]:
        assert set(decision) >= {
            "partition_id", "tier", "score", "checks", "reasons",
            "matched_rules", "policy", "policy_sources"}
        assert decision["tier"] in {"hot", "warm", "cold", "purge_candidate"}


def test_malformed_partition_does_not_abort_scan(write_policy, base_policy):
    policy = load_policy(write_policy(base_policy))
    good = {"partition_id": "ok.t.dt=2026-10-30", "table_name": "demo.t",
            "partition_spec": "dt=2026-10-30", "partition_parts": [],
            "layer": "hot", "path": "hot/demo/t/dt=2026-10-30",
            "date": "2026-10-30", "total_bytes": 10, "file_count": 1}
    bad = dict(good, partition_id="bad.t.dt=2026-10-30",
               date="not-a-date", total_bytes=None, file_count=1)
    inventory = {"partitions": [bad, good], "inventory_hash": "h"}
    evaluation = evaluate_inventory(inventory, {"partitions": {}},
                                    policy, "2026-11-01T00:00:00Z")
    assert [row["partition_id"] for row in evaluation["decisions"]] == [
        "ok.t.dt=2026-10-30"]
    assert evaluation["summary"]["partitions"] == 1
    assert evaluation["summary"]["skipped"][0]["partition_id"] == (
        "bad.t.dt=2026-10-30")


def test_trace_failure_degrades_instead_of_failing(monkeypatch):
    import tier_triage.decide.scorer as scorer

    monkeypatch.setattr(scorer, "_build_decision_trace",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            RuntimeError("boom")))
    policy = load_policy("config/policy.yaml")
    part = {"partition_id": "demo.t.dt=2026-10-30", "table_name": "demo.t",
            "partition_spec": "dt=2026-10-30", "partition_parts": [],
            "layer": "hot", "path": "hot/demo/t/dt=2026-10-30",
            "date": "2026-10-30", "total_bytes": 10, "file_count": 1}
    decision = score_partition(part, {"reads_30d": 0, "never_accessed": True},
                               policy.resolve("demo.t"),
                               datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert decision["tier"] == "hot"
    assert decision["trace"]["degraded"] is True
    assert decision["trace"]["error"].startswith("RuntimeError")
    explanation = render_explanation(decision)
    assert explanation["degraded"] is True


def test_candidate_rule_hits_are_recorded_per_partition(
        lake, pipeline, base_policy):
    lake.make("hot", "demo", "t", ["dt=2026-10-30"], files=1)
    data = _policy(base_policy, [
        {"match": "demo.*", "priority": 20, "cold_after_days": 60},
        {"match": "demo.t", "priority": 10, "warm_after_days": 20},
    ])
    run = pipeline(data)
    trace = run.evaluation["decisions"][0]["trace"]
    assert [rule["match"] for rule in trace["policy"]["candidates"]] == [
        "demo.t", "demo.*"]
    assert [rule["priority"] for rule in trace["policy"]["candidates"]] == [10, 20]
    assert trace["policy"]["candidates"][1]["fields"] == ["cold_after_days"]
    assert trace["config_sources"]["cold_after_days"]["source"] == "demo.*"
    assert trace["config_sources"]["warm_after_days"]["source"] == "demo.t"
