"""Command line interface for scan, evaluate, plan, apply, rollback, report."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from tier_triage.audit.ledger import Ledger
from tier_triage.audit.report import build_report, write_reports
from tier_triage.decide.planner import build_plan, load_plan, save_plan
from tier_triage.decide.scorer import evaluate_inventory, explain_decision
from tier_triage.exec.executor import ExecutionError, execute_plan
from tier_triage.exec.rollback import RollbackError, rollback_batch
from tier_triage.lake.inventory import scan_lake
from tier_triage.lake.profile import build_profile, load_profile, save_profile
from tier_triage.policy.loader import PolicyError, load_policy
from tier_triage.usage.access_log import load_access_log
from tier_triage.usage.stats import compute_stats
from tier_triage.util import DataError, load_json, parse_datetime, write_json

DEFAULT_LAKE = "data/lake"
DEFAULT_USAGE = "data/usage/access.jsonl"
DEFAULT_POLICY = "config/policy.yaml"
DEFAULT_DB = "data/ledger/tier_triage.sqlite"
DEFAULT_ARTIFACTS = "data/artifacts"


def _add_common(parser: argparse.ArgumentParser, as_of: bool = True) -> None:
    parser.add_argument("--lake", default=DEFAULT_LAKE)
    parser.add_argument("--usage", default=DEFAULT_USAGE)
    parser.add_argument("--policy", default=DEFAULT_POLICY)
    parser.add_argument("--db", default=DEFAULT_DB)
    if as_of:
        parser.add_argument("--as-of", required=True,
                            help="evaluation clock, e.g. 2026-11-01T00:00:00Z")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tier_triage",
                                     description="冷热分诊台：分区分层与迁移编排")
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="scan lake partitions")
    _add_common(scan, as_of=False)
    scan.add_argument("--out", default=f"{DEFAULT_ARTIFACTS}/inventory.json")
    scan.add_argument("--profile-out",
                      help="also write an independent partition volume profile")
    scan.add_argument("--as-of",
                      help="optional clock stamped onto --profile-out; omit "
                           "for a pure physical snapshot")
    profile = sub.add_parser(
        "profile",
        help="build an independent partition volume profile (bytes, files, anomalies)")
    profile.add_argument("--lake", default=DEFAULT_LAKE)
    profile.add_argument("--out", default=f"{DEFAULT_ARTIFACTS}/volume_profile.json")
    profile.add_argument(
        "--as-of",
        help="optional evaluation clock stamped onto the profile; omit for a "
             "pure physical snapshot")
    evaluate = sub.add_parser("evaluate", help="score every partition")
    _add_common(evaluate)
    evaluate.add_argument("--out", default=f"{DEFAULT_ARTIFACTS}/evaluation.json")
    evaluate.add_argument("--profile",
                          help="volume profile artifact; without it the legacy "
                               "age/reads-only behavior is used")
    evaluate.add_argument("--explain", help="partition id or unique suffix")
    plan = sub.add_parser("plan", help="build a deterministic migration plan")
    _add_common(plan)
    plan.add_argument("--out", default=f"{DEFAULT_ARTIFACTS}/plan.json")
    plan.add_argument("--profile",
                      help="volume profile artifact; without it the legacy "
                           "age/reads-only behavior is used")
    apply = sub.add_parser("apply", help="preflight and optionally execute a plan")
    apply.add_argument("--plan", default=f"{DEFAULT_ARTIFACTS}/plan.json")
    apply.add_argument("--lake", default=DEFAULT_LAKE)
    apply.add_argument("--db", default=DEFAULT_DB)
    apply.add_argument("--apply", action="store_true", help="perform moves")
    apply.add_argument("--yes", action="store_true", help="confirm destructive flow")
    apply.add_argument("--result")
    apply.add_argument("--actor", default="cli")
    rollback = sub.add_parser("rollback", help="roll back one completed batch")
    rollback.add_argument("--batch", required=True)
    rollback.add_argument("--lake", default=DEFAULT_LAKE)
    rollback.add_argument("--db", default=DEFAULT_DB)
    rollback.add_argument("--result")
    report = sub.add_parser("report", help="write Markdown and JSON audit reports")
    report.add_argument("--evaluation", default=f"{DEFAULT_ARTIFACTS}/evaluation.json")
    report.add_argument("--plan", default=f"{DEFAULT_ARTIFACTS}/plan.json")
    report.add_argument("--db", default=DEFAULT_DB)
    report.add_argument("--batch")
    report.add_argument("--out", default=f"{DEFAULT_ARTIFACTS}/report")
    serve = sub.add_parser("serve", help="run the local-only read-only UI")
    serve.add_argument("--lake", default=DEFAULT_LAKE)
    serve.add_argument("--db", default=DEFAULT_DB)
    serve.add_argument("--port", type=int, default=5000)
    return parser


def _ledger_record(inventory: dict[str, Any], policy: Any, as_of: str,
                   db_path: str, lake_root: str) -> None:
    with Ledger(db_path) as ledger:
        ledger.record_snapshot(inventory, as_of, str(Path(lake_root).resolve()))
        ledger.record_policy(policy)


def _inputs(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], Any,
                                                dict[str, Any] | None]:
    inventory = scan_lake(args.lake)
    policy = load_policy(args.policy)
    records, warnings = load_access_log(args.usage)
    if warnings:
        print(f"warning: skipped {len(warnings)} malformed usage rows",
              file=sys.stderr)
    stats = compute_stats(inventory, records, parse_datetime(args.as_of),
                          policy.windows)
    profile = None
    profile_path = getattr(args, "profile", None)
    if profile_path:
        profile = load_profile(profile_path)
    return inventory, stats, policy, profile


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False))


def _write_optional(path: str | None, value: Any) -> None:
    if path:
        write_json(path, value)


def _cmd_scan(args: argparse.Namespace) -> int:
    inventory = scan_lake(args.lake)
    write_json(args.out, inventory)
    profile_out = getattr(args, "profile_out", None)
    if profile_out:
        profile = build_profile(args.lake, getattr(args, "as_of", None))
        save_profile(profile_out, profile)
    with Ledger(args.db) as ledger:
        ledger.record_snapshot(inventory, "",
                               str(Path(args.lake).resolve()))
    result = {"inventory": args.out,
              "partitions": len(inventory["partitions"]),
              "unparsed": len(inventory["unparsed"]),
              "inventory_hash": inventory["inventory_hash"]}
    if profile_out:
        result["volume_profile"] = profile_out
        result["profile_anomalies"] = len(profile["anomalies"])
        result["profile_hash"] = profile["profile_hash"]
    _print(result)
    return 0


def _cmd_profile(args: argparse.Namespace) -> int:
    profile = build_profile(args.lake, getattr(args, "as_of", None))
    save_profile(args.out, profile)
    _print({"volume_profile": args.out,
            "profile_hash": profile["profile_hash"],
            "inventory_hash": profile["inventory_hash"],
            "as_of": profile["as_of"],
            "partitions": profile["summary"]["profiled_partitions"],
            "unreadable": profile["summary"]["unreadable_partitions"],
            "incomplete": profile["summary"]["incomplete_partitions"],
            "anomalies": profile["summary"]["anomaly_count"],
            "total_bytes": profile["summary"]["total_bytes"]})
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    inventory, stats, policy, profile = _inputs(args)
    evaluation = evaluate_inventory(inventory, stats, policy, args.as_of, profile)
    write_json(args.out, evaluation)
    _ledger_record(inventory, policy, args.as_of, args.db, args.lake)
    result = {"evaluation": args.out,
              "partitions": evaluation["summary"]["partitions"],
              "tier_distribution": evaluation["summary"]["tier_distribution"],
              "policy_fingerprint": policy.fingerprint}
    if profile is not None:
        result["profile_hash"] = profile["profile_hash"]
        result["oversize_partitions"] = evaluation["capacity_profile"][
            "oversize_partitions"]
    _print(result)
    if args.explain:
        _print(explain_decision(evaluation, args.explain))
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    inventory, stats, policy, profile = _inputs(args)
    evaluation = evaluate_inventory(inventory, stats, policy, args.as_of, profile)
    plan = build_plan(evaluation, inventory, args.lake)
    save_plan(args.out, plan)
    with Ledger(args.db) as ledger:
        ledger.record_snapshot(inventory, args.as_of, str(Path(args.lake).resolve()))
        ledger.record_policy(policy)
        ledger.record_plan(plan, str(Path(args.out).resolve()))
    result = {"plan": args.out, "plan_id": plan["plan_id"],
              "plan_hash": plan["plan_hash"], "summary": plan["summary"]}
    if profile is not None:
        pressure = plan["capacity_profile"]["pressure"]
        result["profile_hash"] = profile["profile_hash"]
        result["capacity_blocked"] = len(pressure["capacity_blocked"])
    _print(result)
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    result = execute_plan(plan, args.lake, args.db, apply=args.apply,
                          confirm=args.yes, actor=args.actor)
    _write_optional(args.result, result)
    _print(result)
    return 0 if result["status"] in {"ready", "completed"} else 1


def _cmd_rollback(args: argparse.Namespace) -> int:
    result = rollback_batch(args.batch, args.lake, args.db)
    _write_optional(args.result, result)
    _print(result)
    return 0 if result["status"] == "rolled_back" else 1


def _cmd_report(args: argparse.Namespace) -> int:
    evaluation = load_json(args.evaluation)
    plan = load_plan(args.plan)
    if evaluation.get("policy_fingerprint") != plan.get("policy_fingerprint"):
        raise DataError("evaluation and plan use different policy fingerprints")
    if evaluation.get("inventory_hash") != plan.get("inventory_hash"):
        raise DataError("evaluation and plan use different inventory hashes")
    report = build_report(evaluation, plan, args.db, args.batch)
    markdown, json_path = write_reports(args.out, report)
    _print({"markdown": str(markdown), "json": str(json_path),
            "plan_hash": report["plan_hash"],
            "batch": (report["batch_summary"] or {}).get("batch_id")})
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from tier_triage.web.app import create_app
    app = create_app(args.lake, args.db)
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
    return 0


COMMANDS = {
    "scan": _cmd_scan, "profile": _cmd_profile,
    "evaluate": _cmd_evaluate, "plan": _cmd_plan,
    "apply": _cmd_apply, "rollback": _cmd_rollback, "report": _cmd_report,
    "serve": _cmd_serve,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except (DataError, PolicyError, ExecutionError, RollbackError,
            OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
