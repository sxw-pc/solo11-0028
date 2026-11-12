import copy
import json
from pathlib import Path

from tier_triage.cli import main
from tier_triage.web.app import create_app


def _cli_policy_data(base_policy):
    data = copy.deepcopy(base_policy)
    data["tables"] = [{
        "match": "demo.*", "priority": 10, "purge_allowed": True,
        "purge_after_days": 365,
    }]
    return data


def _setup(lake, tmp_path, write_policy, base_policy):
    lake.make("hot", "demo", "t", ["dt=2026-10-30"], files=1)
    lake.make("hot", "demo", "t", ["dt=2026-09-01"], files=2)
    lake.make("hot", "demo", "t", ["dt=2026-01-01"], files=1)
    lake.make("cold", "demo", "t", ["dt=2024-01-01"], files=1)
    policy = write_policy(_cli_policy_data(base_policy))
    access = tmp_path / "access.jsonl"
    access.write_text("", encoding="utf-8")
    return policy, access


def _common(lake, tmp_path, policy, access):
    return ["--lake", str(lake.root), "--usage", str(access),
            "--policy", str(policy), "--db", str(tmp_path / "ledger.sqlite"),
            "--as-of", "2026-11-01T00:00:00Z"]


def test_cli_end_to_end_and_deterministic_plan(
        lake, tmp_path, write_policy, base_policy, tree_files):
    policy, access = _setup(lake, tmp_path, write_policy, base_policy)
    common = _common(lake, tmp_path, policy, access)
    artifacts = tmp_path / "artifacts"
    inventory_path = artifacts / "inventory.json"
    evaluation_path = artifacts / "evaluation.json"
    plan_path = artifacts / "plan.json"
    assert main(["scan", "--lake", str(lake.root), "--db",
                 str(tmp_path / "ledger.sqlite"), "--out",
                 str(inventory_path)]) == 0
    assert main(["evaluate", *common, "--out", str(evaluation_path),
                 "--explain", "demo.t.dt=2026-09-01"]) == 0
    assert main(["plan", *common, "--out", str(plan_path)]) == 0
    first_hash = json.loads(plan_path.read_text(encoding="utf-8"))["plan_hash"]
    assert main(["plan", *common, "--out", str(plan_path)]) == 0
    second_hash = json.loads(plan_path.read_text(encoding="utf-8"))["plan_hash"]
    assert first_hash == second_hash
    before = tree_files(lake.root)
    assert main(["apply", "--plan", str(plan_path), "--lake", str(lake.root),
                 "--db", str(tmp_path / "ledger.sqlite")]) == 0
    assert tree_files(lake.root) == before
    result_path = artifacts / "apply.json"
    assert main(["apply", "--plan", str(plan_path), "--lake", str(lake.root),
                 "--db", str(tmp_path / "ledger.sqlite"), "--apply", "--yes",
                 "--result", str(result_path)]) == 0
    applied = json.loads(result_path.read_text(encoding="utf-8"))
    assert applied["status"] == "completed"
    assert applied["moved_count"] >= 2
    rollback_path = artifacts / "rollback.json"
    assert main(["rollback", "--batch", applied["batch_id"], "--lake",
                 str(lake.root), "--db", str(tmp_path / "ledger.sqlite"),
                 "--result", str(rollback_path)]) == 0
    rolled = json.loads(rollback_path.read_text(encoding="utf-8"))
    assert rolled["status"] == "rolled_back"
    assert tree_files(lake.root) == before


def test_cli_rejects_hash_mismatch_and_missing_confirmation(
        lake, tmp_path, write_policy, base_policy, tree_files, capsys):
    policy, access = _setup(lake, tmp_path, write_policy, base_policy)
    common = _common(lake, tmp_path, policy, access)
    plan_path = tmp_path / "plan.json"
    assert main(["plan", *common, "--out", str(plan_path)]) == 0
    before = tree_files(lake.root)
    assert main(["apply", "--plan", str(plan_path), "--lake", str(lake.root),
                 "--db", str(tmp_path / "ledger.sqlite"), "--apply"]) == 2
    assert tree_files(lake.root) == before
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    for action in plan["actions"]:
        if action["action"] == "move":
            action["bytes"] += 1
            break
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(plan), encoding="utf-8")
    assert main(["apply", "--plan", str(tampered), "--lake", str(lake.root),
                 "--db", str(tmp_path / "ledger.sqlite"), "--apply", "--yes"]) == 2
    assert "hash mismatch" in capsys.readouterr().err
    assert tree_files(lake.root) == before


def test_web_is_local_only_and_renders(lake, tmp_path, write_policy,
                                       base_policy):
    policy, access = _setup(lake, tmp_path, write_policy, base_policy)
    common = _common(lake, tmp_path, policy, access)
    plan_path = tmp_path / "plan.json"
    result_path = tmp_path / "apply.json"
    assert main(["plan", *common, "--out", str(plan_path)]) == 0
    assert main(["apply", "--plan", str(plan_path), "--lake", str(lake.root),
                 "--db", str(tmp_path / "ledger.sqlite"), "--apply", "--yes",
                 "--result", str(result_path)]) == 0
    app = create_app(str(lake.root), str(tmp_path / "ledger.sqlite"))
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/batches").status_code == 200
    assert client.get("/healthz").status_code == 200
    remote = client.get("/", environ_base={"REMOTE_ADDR": "203.0.113.7"})
    assert remote.status_code == 403
    batch_id = json.loads(result_path.read_text(encoding="utf-8"))["batch_id"]
    detail = client.get(f"/batches/{batch_id}")
    assert detail.status_code == 200
    assert b"roll" in detail.data.lower() or "回滚".encode() in detail.data
