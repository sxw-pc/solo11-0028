import json

from tier_triage.audit.ledger import Ledger
from tier_triage.audit.report import build_report, render_markdown, write_reports
from tier_triage.policy.loader import load_policy


def _plan():
    return {
        "schema_version": 1,
        "plan_id": "plan-abc",
        "plan_hash": "a" * 64,
        "as_of": "2026-11-01T00:00:00Z",
        "inventory_hash": "inv",
        "policy_fingerprint": "pol",
        "summary": {"projected_release_bytes": 300,
                    "action_counts": {"move": 1, "mark_purge": 1, "noop": 1}},
        "actions": [
            {"seq": 1, "partition_id": "db.t.dt=old", "action": "move",
             "status": "ready", "current_layer": "hot", "target_layer": "cold",
             "source": "hot/db/t/dt=old", "target": "cold/db/t/dt=old",
             "bytes": 200, "file_count": 1, "reason": "decision=cold"},
            {"seq": 2, "partition_id": "db.t.dt=purge", "action": "mark_purge",
             "status": "ready", "current_layer": "hot", "target_layer": None,
             "source": "hot/db/t/dt=purge", "target": None,
             "bytes": 100, "file_count": 1, "reason": "decision=purge_candidate"},
            {"seq": 3, "partition_id": "db.t.dt=hot", "action": "noop",
             "status": "noop", "current_layer": "hot", "target_layer": "hot",
             "source": "hot/db/t/dt=hot", "target": "hot/db/t/dt=hot",
             "bytes": 10, "file_count": 1, "reason": "decision=hot"},
            {"seq": 4, "partition_id": "db.t.dt=blocked", "action": "move",
             "status": "blocked", "current_layer": "hot", "target_layer": "warm",
             "source": "hot/db/t/dt=blocked", "target": "warm/db/t/dt=blocked",
             "bytes": 5, "file_count": 1, "reason": "target path already exists"},
        ],
    }


def _evaluation():
    return {
        "as_of": "2026-11-01T00:00:00Z",
        "inventory_hash": "inv",
        "policy_fingerprint": "pol",
        "summary": {"tier_distribution": {
            "hot": 1, "warm": 0, "cold": 2, "purge_candidate": 1}},
    }


def test_ledger_roundtrip_and_rollback_record(tmp_path, write_policy, base_policy):
    policy = load_policy(write_policy(base_policy))
    plan = _plan()
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        snapshot_id = ledger.record_snapshot(
            {"inventory_hash": "inv", "partitions": [{"total_bytes": 10}]},
            "2026-11-01T00:00:00Z", "lake")
        policy_id = ledger.record_policy(policy)
        ledger.record_plan(plan, "plan.json")
        ledger.create_batch(plan, "batch-1", "test")
        ledger.start_action("batch-1", 1)
        ledger.finish_action("batch-1", 1, "moved")
        ledger.finish_batch("batch-1", "completed", 1, 0)
        rollback_id = ledger.add_rollback(
            "batch-1", 1, 0, {"restored": [{"seq": 1}]})
        snapshot = ledger.latest_snapshot()
        recorded = ledger.get_plan("plan-abc")
        assert snapshot["snapshot_id"] == snapshot_id
        assert snapshot["total_bytes"] == 10
        assert policy_id.startswith("policy-")
        assert recorded["payload"]["actions"] == plan["actions"]
        assert ledger.list_actions("batch-1")[0]["status"] == "moved"
        assert ledger.get_batch("batch-1")["moved_count"] == 1
        assert ledger.get_rollbacks("batch-1")[0]["rollback_id"] == rollback_id
        assert ledger.get_rollbacks("batch-1")[0]["detail"]["restored"]


def test_report_fields_markdown_and_json(tmp_path, write_policy, base_policy):
    evaluation = _evaluation()
    plan = _plan()
    ledger_path = tmp_path / "ledger.sqlite"
    policy = load_policy(write_policy(base_policy))
    with Ledger(ledger_path) as ledger:
        ledger.record_policy(policy)
        ledger.record_plan(plan)
        ledger.create_batch(plan, "batch-report", "tester")
        ledger.finish_batch("batch-report", "completed", 1, 0)
        ledger.add_rollback("batch-report", 1, 0, {"restored": []})
    report = build_report(evaluation, plan, ledger_path, "batch-report")
    assert report["tier_distribution"] == {
        "hot": 1, "warm": 0, "cold": 2, "purge_candidate": 1}
    assert report["projected_release_bytes"] == 300
    assert report["purge_reclaimable_bytes"] == 100
    assert report["blocked_items"][0]["reason"] == "target path already exists"
    assert report["batch_summary"]["batch_id"] == "batch-report"
    assert report["batch_summary"]["rollbacks"][0]["restored_count"] == 1
    markdown = render_markdown(report)
    assert "# 冷热分诊台" in markdown
    assert "purge 清退清单" in markdown
    assert "`db.t.dt=purge`" in markdown
    assert "target path already exists" in markdown
    base = tmp_path / "audit" / "report"
    markdown_path, json_path = write_reports(base, report)
    assert markdown_path.exists() and json_path.exists()
    stored = json.loads(json_path.read_text(encoding="utf-8"))
    assert stored["plan_hash"] == plan["plan_hash"]
    assert set(stored) >= {
        "tier_distribution", "blocked_items", "purge_list", "batch_summary"}
