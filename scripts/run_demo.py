#!/usr/bin/env python3
"""One-command offline demo: fixtures, scan, evaluate, plan, apply, report, rollback."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_fixtures import AS_OF, generate  # noqa: E402
from tier_triage.cli import main as cli_main  # noqa: E402


def _section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _run(argv: list[str]) -> None:
    code = cli_main(argv)
    if code != 0:
        raise SystemExit(f"command failed ({code}): {' '.join(argv)}")


def main() -> int:
    data = ROOT / "data"
    lake = data / "lake"
    usage = data / "usage" / "access.jsonl"
    policy = ROOT / "config" / "policy.yaml"
    db = data / "ledger" / "tier_triage.sqlite"
    artifacts = data / "artifacts"
    fixture = generate(data)
    for generated in (data / "ledger", artifacts):
        if generated.exists():
            shutil.rmtree(generated)
    artifacts.mkdir(parents=True, exist_ok=True)
    common = ["--lake", str(lake), "--usage", str(usage),
              "--policy", str(policy), "--db", str(db), "--as-of", AS_OF]

    _section("1/7 生成离线 fixture")
    print(json.dumps(fixture, sort_keys=True, indent=2, ensure_ascii=False))

    _section("2/7 扫描分层湖目录")
    _run(["scan", "--lake", str(lake), "--db", str(db),
          "--out", str(artifacts / "inventory.json")])

    _section("3/7 按固定评估时钟计算访问与分层")
    _run(["evaluate", *common, "--out", str(artifacts / "evaluation.json")])

    _section("4/7 生成确定性迁移计划（dry-run）")
    _run(["plan", *common, "--out", str(artifacts / "plan.json")])

    _section("5/7 apply 前置校验（不写入）")
    _run(["apply", "--plan", str(artifacts / "plan.json"),
          "--lake", str(lake), "--db", str(db)])

    _section("6/7 确认后执行迁移并生成报告")
    _run(["apply", "--plan", str(artifacts / "plan.json"),
          "--lake", str(lake), "--db", str(db), "--apply", "--yes",
          "--result", str(artifacts / "apply_result.json")])
    apply_result = json.loads((artifacts / "apply_result.json").read_text("utf-8"))
    batch_id = apply_result["batch_id"]
    _run(["report", "--evaluation", str(artifacts / "evaluation.json"),
          "--plan", str(artifacts / "plan.json"), "--db", str(db),
          "--batch", batch_id, "--out", str(artifacts / "report_after_apply")])

    _section("7/7 回滚批次并生成回滚后报告")
    _run(["rollback", "--batch", batch_id, "--lake", str(lake), "--db", str(db),
          "--result", str(artifacts / "rollback_result.json")])
    _run(["report", "--evaluation", str(artifacts / "evaluation.json"),
          "--plan", str(artifacts / "plan.json"), "--db", str(db),
          "--batch", batch_id, "--out", str(artifacts / "report_after_rollback")])

    rollback_result = json.loads(
        (artifacts / "rollback_result.json").read_text("utf-8"))
    plan = json.loads((artifacts / "plan.json").read_text("utf-8"))
    print("\nDEMO COMPLETE")
    print(f"  plan_hash:  {plan['plan_hash']}")
    print(f"  batch:      {batch_id}")
    print(f"  apply:      {apply_result['status']} "
          f"({apply_result['moved_count']} moved, "
          f"{apply_result['marked_count']} purge marks)")
    print(f"  rollback:   {rollback_result['status']} "
          f"({rollback_result['restored_count']} restored)")
    print(f"  reports:    {artifacts / 'report_after_apply.md'}")
    print(f"              {artifacts / 'report_after_rollback.md'}")
    if apply_result["status"] != "completed":
        return 1
    if rollback_result["status"] != "rolled_back":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
