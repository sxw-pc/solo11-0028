import copy
import shutil

import pytest

from tier_triage.audit.ledger import Ledger
from tier_triage.decide.planner import PlanIntegrityError
from tier_triage.exec import executor
from tier_triage.exec.executor import ExecutionError, execute_plan
from tier_triage.exec.rollback import rollback_batch
from tier_triage.lake import fs_ops


def _policy_data(base_policy):
    data = copy.deepcopy(base_policy)
    data["tables"] = [{
        "match": "demo.*", "priority": 10, "purge_allowed": True,
        "purge_after_days": 365,
    }]
    return data


def _setup(lake, pipeline, base_policy):
    lake.make("hot", "demo", "t", ["dt=2026-09-01"], files=2)
    lake.make("hot", "demo", "t", ["dt=2026-01-01"], files=1)
    lake.make("cold", "demo", "t", ["dt=2024-01-01"], files=1)
    lake.make("hot", "demo", "t", ["dt=2026-10-30"], files=1)
    return pipeline(_policy_data(base_policy))


def test_apply_and_rollback_restore_files(
        lake, pipeline, base_policy, tmp_path, tree_files):
    run = _setup(lake, pipeline, base_policy)
    before = tree_files(lake.root)
    ledger = tmp_path / "ledger.sqlite"
    result = execute_plan(run.plan, lake.root, ledger, apply=True, confirm=True)
    assert result["status"] == "completed"
    assert result["moved_count"] == 2
    assert result["marked_count"] == 1
    assert tree_files(lake.root) != before
    assert (lake.root / "warm/demo/t/dt=2026-09-01/part-0000.txt").exists()
    assert not (lake.root / "hot/demo/t/dt=2026-09-01").exists()
    with Ledger(ledger) as store:
        actions = store.list_actions(result["batch_id"])
        assert {row["status"] for row in actions} >= {"moved", "marked", "skipped"}
        assert store.get_batch(result["batch_id"])["status"] == "completed"
    rolled = rollback_batch(result["batch_id"], lake.root, ledger)
    assert rolled["status"] == "rolled_back"
    assert rolled["restored_count"] == 2
    assert tree_files(lake.root) == before
    with Ledger(ledger) as store:
        assert store.get_batch(result["batch_id"])["status"] == "rolled_back"
        assert len(store.get_rollbacks(result["batch_id"])) == 1
        statuses = {row["status"] for row in store.list_actions(result["batch_id"])}
        assert "rolled_back" in statuses and "unmarked" in statuses


def test_dry_run_and_confirmation_do_not_touch_lake(
        lake, pipeline, base_policy, tmp_path, tree_files):
    run = _setup(lake, pipeline, base_policy)
    before = tree_files(lake.root)
    ledger = tmp_path / "ledger.sqlite"
    dry = execute_plan(run.plan, lake.root, ledger)
    assert dry["mode"] == "dry-run" and dry["status"] == "ready"
    assert not ledger.exists()
    with pytest.raises(ExecutionError, match="confirmation"):
        execute_plan(run.plan, lake.root, ledger, apply=True, confirm=False)
    assert tree_files(lake.root) == before
    assert not ledger.exists()


def test_tampered_plan_is_rejected_before_writes(
        lake, pipeline, base_policy, tmp_path, tree_files):
    run = _setup(lake, pipeline, base_policy)
    plan = copy.deepcopy(run.plan)
    plan["actions"][0]["target"] = "hot/elsewhere"
    before = tree_files(lake.root)
    with pytest.raises(PlanIntegrityError):
        execute_plan(plan, lake.root, tmp_path / "ledger.sqlite",
                     apply=True, confirm=True)
    assert tree_files(lake.root) == before


def test_mid_batch_failure_reverts_lake(
        lake, pipeline, base_policy, tmp_path, tree_files, monkeypatch):
    run = _setup(lake, pipeline, base_policy)
    before = tree_files(lake.root)
    paths = lambda root: {p.relative_to(root).as_posix() for p in root.rglob("*")}
    before_tree = paths(lake.root)
    calls = {"count": 0}
    real_move = fs_ops.atomic_move

    def flaky(source, destination, root, token=None):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("injected move failure")
        return real_move(source, destination, root, token)

    monkeypatch.setattr(executor, "atomic_move", flaky)
    ledger = tmp_path / "ledger.sqlite"
    result = execute_plan(run.plan, lake.root, ledger, apply=True, confirm=True)
    assert result["status"] == "failed"
    assert "injected move failure" in result["error"]
    assert result["moved_count"] == 0
    assert result["reverted_count"] == 1
    assert tree_files(lake.root) == before
    assert paths(lake.root) == before_tree
    with Ledger(ledger) as store:
        assert store.get_batch(result["batch_id"])["status"] == "failed"
        statuses = {row["status"] for row in store.list_actions(result["batch_id"])}
        assert "reverted" in statuses and "failed" in statuses


def test_rollback_reports_unrecoverable_external_move(
        lake, pipeline, base_policy, tmp_path):
    run = _setup(lake, pipeline, base_policy)
    ledger = tmp_path / "ledger.sqlite"
    result = execute_plan(run.plan, lake.root, ledger, apply=True, confirm=True)
    moved = next(action for action in run.plan["actions"]
                 if action["action"] == "move")
    target = lake.root / moved["target"]
    external = tmp_path / "external"
    shutil.move(str(target), str(external))
    rolled = rollback_batch(result["batch_id"], lake.root, ledger)
    assert rolled["status"] == "rollback_partial"
    assert rolled["unrecoverable_count"] == 1
    assert rolled["restored_count"] == 1
    assert rolled["unrecoverable"][0]["partition_id"] == moved["partition_id"]
    with Ledger(ledger) as store:
        rows = store.list_actions(result["batch_id"])
        assert any(row["status"] == "unrecoverable" for row in rows)
