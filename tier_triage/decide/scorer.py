"""Deterministic hot/warm/cold/purge evaluation with explanation chains."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from tier_triage.policy.loader import Policy
from tier_triage.util import DataError, clamp, iso_z, parse_datetime

TIER_ORDER = ("hot", "warm", "cold", "purge_candidate")


def partition_age_days(part: dict[str, Any], as_of: datetime) -> tuple[int | None, str]:
    if part.get("date"):
        try:
            partition_date = datetime.fromisoformat(part["date"]).date()
            return max(0, (as_of.date() - partition_date).days), "partition_date"
        except ValueError:
            pass
    if part.get("last_modified"):
        stamp = parse_datetime(part["last_modified"])
        return max(0, (as_of - stamp).days), "last_modified"
    return None, "unknown"


def _score(reads_30d: int, idle_days: int | None, bytes_30d: int,
           never_accessed: bool) -> int:
    read_component = min(40.0, reads_30d * 4.0)
    if never_accessed or idle_days is None:
        recency_component = 0.0
    else:
        recency_component = max(0.0, 45.0 - idle_days * 0.5)
    byte_component = min(15.0, bytes_30d / (1024 * 1024))
    return int(round(clamp(read_component + recency_component + byte_component,
                           0, 100)))


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": passed, "detail": detail}


def _component(config: dict[str, Any], provenance: dict[str, Any], field: str,
               observed: Any, passed: bool, comparison: str) -> dict[str, Any]:
    return {
        "field": field,
        "value": config.get(field),
        "source": provenance.get(field, "unknown"),
        "observed": observed,
        "passed": bool(passed),
        "comparison": comparison,
    }


def _degraded_trace(stage: str, exc: BaseException) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "degraded": True,
        "stage": stage,
        "error": f"{type(exc).__name__}: {exc}",
        "nodes": [],
        "final_tier": None,
    }


def _build_decision_trace(config: dict[str, Any], provenance: dict[str, Any],
                          policy_trace: dict[str, Any], tier: str,
                          facts: dict[str, Any],
                          check_details: list[str]) -> dict[str, Any]:
    """Describe how the four-tier result was reached, without changing it.

    Every stage in the scorer's fixed evaluation order becomes one node:
    ``adopted`` for the stage that determined the tier, ``rejected`` for a
    stage whose conditions failed (its failed components are listed as vetoes)
    and ``shadowed`` for a stage that passed but lost to an earlier stage.
    """
    age = facts["age_days"]
    age_text = f"age_days={age}" if age is not None else "age_days=unknown"
    stages: list[dict[str, Any]] = [
        {
            "stage": 1,
            "rule": "retention_floor",
            "target_tier": "hot",
            "passed": facts["retention_passed"],
            "detail": check_details[0],
            "components": [_component(
                config, provenance, "hot_retain_days", age_text,
                facts["retention_passed"], "age_days < hot_retain_days")],
        },
        {
            "stage": 2,
            "rule": "purge_window",
            "target_tier": "purge_candidate",
            "passed": facts["purge_passed"],
            "detail": check_details[1],
            "components": [
                _component(config, provenance, "purge_allowed",
                           config.get("purge_allowed"),
                           bool(config.get("purge_allowed")),
                           "purge_allowed must be true"),
                _component(config, provenance, "purge_after_days", age_text,
                           facts["purge_age"], "age_days >= purge_after_days"),
                _component(config, provenance, "purge_max_reads_30d",
                           f"reads_30d={facts['reads_30d']}",
                           facts["purge_reads"],
                           "reads_30d <= purge_max_reads_30d"),
                _component(config, provenance, "purge_idle_days",
                           f"idle_days={facts['idle_days']}; "
                           f"never_accessed={facts['never_accessed']}",
                           facts["purge_idle"],
                           "never_accessed or idle_days >= purge_idle_days"),
            ],
        },
        {
            "stage": 3,
            "rule": "cold_threshold",
            "target_tier": "cold",
            "passed": facts["cold_passed"],
            "detail": check_details[2],
            "components": [
                _component(config, provenance, "cold_after_days", age_text,
                           facts["cold_age"], "age_days >= cold_after_days"),
                _component(config, provenance, "cold_max_reads_30d",
                           f"reads_30d={facts['reads_30d']}",
                           facts["cold_reads"],
                           "reads_30d <= cold_max_reads_30d"),
            ],
        },
        {
            "stage": 4,
            "rule": "warm_threshold",
            "target_tier": "warm",
            "passed": facts["warm_passed"],
            "detail": check_details[3],
            "components": [
                _component(config, provenance, "warm_after_days", age_text,
                           facts["warm_age"], "age_days >= warm_after_days"),
                _component(config, provenance, "hot_min_reads_30d",
                           f"reads_30d={facts['reads_30d']}",
                           facts["warm_reads"],
                           "reads_30d < hot_min_reads_30d"),
                _component(config, provenance, "max_hot_bytes",
                           f"total_bytes={facts['total_bytes']}",
                           facts["warm_size"], "total_bytes > max_hot_bytes"),
            ],
        },
        {
            "stage": 5,
            "rule": "hot_default",
            "target_tier": "hot",
            "passed": True,
            "detail": check_details[4],
            "components": [],
            "fallback": True,
        },
    ]
    deciding_stage = {
        "purge_candidate": 2, "cold": 3, "warm": 4,
    }.get(tier)
    if deciding_stage is None:
        deciding_stage = 1 if facts["retention_passed"] else 5

    nodes: list[dict[str, Any]] = []
    for stage in stages:
        node = dict(stage)
        if stage["stage"] == deciding_stage:
            node["outcome"] = "adopted"
            node["decisive"] = True
        elif stage["passed"]:
            node["outcome"] = "shadowed"
            node["decisive"] = False
            node["shadowed_by"] = deciding_stage
        else:
            node["outcome"] = "rejected"
            node["decisive"] = False
            node["decided_by"] = deciding_stage
            node["vetoes"] = [
                {"field": item["field"], "value": item["value"],
                 "source": item["source"], "observed": item["observed"],
                 "comparison": item["comparison"]}
                for item in stage["components"] if not item["passed"]
            ]
        if stage["rule"] == "warm_threshold":
            node["demotion_causes"] = [
                item["field"] for item in stage["components"] if item["passed"]]
            node["size_blocked_hot"] = bool(facts["warm_size"])
        nodes.append(node)

    policy_ok = bool(policy_trace) and not policy_trace.get("degraded")
    return {
        "schema_version": 1,
        "degraded": False,
        "table": policy_trace.get("table"),
        "final_tier": tier,
        "deciding_stage": deciding_stage,
        "nodes": nodes,
        "config_sources": {
            field: {"value": config.get(field),
                    "source": provenance.get(field, "unknown")}
            for field in config
        },
        "policy": policy_trace,
        "policy_trace_degraded": not policy_ok,
    }


def _safe_decision_trace(config: dict[str, Any], provenance: dict[str, Any],
                         policy_trace: dict[str, Any], tier: str,
                         facts: dict[str, Any],
                         check_details: list[str]) -> dict[str, Any]:
    try:
        return _build_decision_trace(config, provenance, policy_trace, tier,
                                     facts, check_details)
    except Exception as exc:  # trace must never break evaluate
        return _degraded_trace("decision_trace", exc)


def score_partition(part: dict[str, Any], stat: dict[str, Any],
                    resolved: dict[str, Any], as_of: datetime) -> dict[str, Any]:
    """Evaluate one partition. All comparisons use the injected clock."""
    config = resolved["config"]
    age_days, age_source = partition_age_days(part, as_of)
    reads_30d = int(stat.get("reads_30d", 0))
    bytes_30d = int(stat.get("bytes_30d", 0))
    never_accessed = bool(stat.get("never_accessed", True))
    idle_days = stat.get("idle_days")
    total_bytes = int(part.get("total_bytes", 0))
    age_value = age_days if age_days is not None else 0
    checks: list[dict[str, Any]] = []
    decisions: list[str] = []

    retention_passed = age_days is not None and age_days < config["hot_retain_days"]
    checks.append(_check(
        "retention_floor", retention_passed,
        f"age={age_days}d from {age_source}; hot_retain_days={config['hot_retain_days']}"))
    if retention_passed:
        decisions.append("hot")

    purge_age = age_days is not None and age_days >= config["purge_after_days"]
    purge_reads = reads_30d <= config["purge_max_reads_30d"]
    purge_idle = never_accessed or (
        idle_days is not None and idle_days >= config["purge_idle_days"])
    purge_passed = bool(
        config["purge_allowed"] and purge_age and purge_reads and purge_idle)
    checks.append(_check(
        "purge_window", purge_passed,
        f"allowed={str(config['purge_allowed']).lower()}; age>={config['purge_after_days']}d="
        f"{str(purge_age).lower()}; reads_30d<={config['purge_max_reads_30d']}="
        f"{str(purge_reads).lower()}; idle_ok={str(purge_idle).lower()}"))
    if purge_passed:
        decisions.append("purge_candidate")

    cold_age = age_days is not None and age_days >= config["cold_after_days"]
    cold_reads = reads_30d <= config["cold_max_reads_30d"]
    cold_passed = cold_age and cold_reads
    checks.append(_check(
        "cold_threshold", cold_passed,
        f"age>={config['cold_after_days']}d={str(cold_age).lower()}; "
        f"reads_30d<={config['cold_max_reads_30d']}={str(cold_reads).lower()}"))
    if cold_passed:
        decisions.append("cold")

    warm_age = age_days is not None and age_days >= config["warm_after_days"]
    warm_reads = reads_30d < config["hot_min_reads_30d"]
    warm_size = total_bytes > config["max_hot_bytes"]
    warm_passed = warm_age or warm_reads or warm_size
    checks.append(_check(
        "warm_threshold", warm_passed,
        f"age>={config['warm_after_days']}d={str(warm_age).lower()}; "
        f"reads_30d<{config['hot_min_reads_30d']}={str(warm_reads).lower()}; "
        f"bytes>{config['max_hot_bytes']}={str(warm_size).lower()}"))
    if warm_passed:
        decisions.append("warm")
    checks.append(_check(
        "hot_default", True,
        f"age={age_value}d; cold/purge checks did not demote the partition"))

    if retention_passed:
        tier = "hot"
        reason_count = 1
    elif "purge_candidate" in decisions:
        tier = "purge_candidate"
        reason_count = 2
    elif "cold" in decisions:
        tier = "cold"
        reason_count = 3
    elif "warm" in decisions:
        tier = "warm"
        reason_count = 4
    else:
        tier = "hot"
        reason_count = 5
    decisive_checks = checks[:reason_count]
    score = _score(reads_30d, idle_days, bytes_30d, never_accessed)
    facts = {
        "age_days": age_days,
        "retention_passed": retention_passed,
        "purge_passed": purge_passed,
        "purge_age": purge_age,
        "purge_reads": purge_reads,
        "purge_idle": purge_idle,
        "cold_passed": cold_passed,
        "cold_age": cold_age,
        "cold_reads": cold_reads,
        "warm_passed": warm_passed,
        "warm_age": warm_age,
        "warm_reads": warm_reads,
        "warm_size": warm_size,
        "reads_30d": reads_30d,
        "idle_days": idle_days,
        "never_accessed": never_accessed,
        "total_bytes": total_bytes,
    }
    policy_trace = resolved.get("trace") or _degraded_trace(
        "policy_merge", RuntimeError("policy trace missing"))
    trace = _safe_decision_trace(
        resolved["config"], resolved["provenance"], policy_trace, tier, facts,
        [item["detail"] for item in checks])
    return {
        "partition_id": part["partition_id"],
        "table": part["table_name"],
        "partition_spec": part["partition_spec"],
        "partition_parts": part.get("partition_parts", []),
        "current_layer": part["layer"],
        "source_path": part["path"],
        "tier": tier,
        "score": score,
        "age_days": age_days,
        "age_source": age_source,
        "idle_days": idle_days,
        "never_accessed": never_accessed,
        "reads_30d": reads_30d,
        "bytes_30d": bytes_30d,
        "file_count": part.get("file_count", 0),
        "total_bytes": total_bytes,
        "policy": resolved["config"],
        "policy_sources": resolved["provenance"],
        "matched_rules": resolved["matched"],
        "reasons": decisive_checks,
        "checks": checks,
        "trace": trace,
    }


def evaluate_inventory(inventory: dict[str, Any], stats: dict[str, Any],
                       policy: Policy, as_of: datetime | str) -> dict[str, Any]:
    """Score every partition; one malformed partition never aborts the scan.

    A partition that cannot be scored is recorded under ``summary.skipped`` and
    omitted from decisions; the remaining partitions are still evaluated.  A
    deficient trace degrades to a record instead of raising.
    """
    clock = parse_datetime(as_of)
    decisions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for part in inventory.get("partitions", []):
        stat = stats.get("partitions", {}).get(part["partition_id"])
        if stat is None:
            stat = {"reads_30d": 0, "never_accessed": True}
        try:
            resolved = policy.resolve(part["table_name"])
            decisions.append(score_partition(part, stat, resolved, clock))
        except Exception as exc:  # malformed partition: degrade, keep scanning
            skipped.append({
                "partition_id": part.get("partition_id"),
                "reason": f"{type(exc).__name__}: {exc}"})
    decisions.sort(key=lambda row: row["partition_id"])
    distribution = {tier: 0 for tier in TIER_ORDER}
    for decision in decisions:
        distribution[decision["tier"]] += 1
    summary: dict[str, Any] = {
        "partitions": len(decisions),
        "tier_distribution": distribution,
        "decision_bytes": {
            tier: sum(row["total_bytes"] for row in decisions
                      if row["tier"] == tier)
            for tier in TIER_ORDER
        },
    }
    if skipped:
        summary["skipped"] = skipped
    return {
        "schema_version": 1,
        "as_of": iso_z(clock),
        "inventory_hash": inventory.get("inventory_hash"),
        "policy_fingerprint": policy.fingerprint,
        "decisions": decisions,
        "summary": summary,
    }


def explain_decision(evaluation: dict[str, Any], target: str) -> dict[str, Any]:
    for decision in evaluation.get("decisions", []):
        if decision["partition_id"] == target:
            return decision
    matches = [row for row in evaluation.get("decisions", [])
               if row["partition_id"].endswith(target)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise DataError(f"partition not found: {target}")
    raise DataError(f"ambiguous partition suffix: {target}")


def render_explanation(decision: dict[str, Any]) -> dict[str, Any]:
    """Human-readable provenance view of one decision.

    Pure formatting over the already computed decision; it never re-scores or
    changes the tier.  Missing or degraded trace data is reported as such.
    """
    trace = decision.get("trace") or {}
    degraded = bool(trace.get("degraded")) or not trace.get("nodes")
    lines: list[str] = [
        f"partition {decision['partition_id']} -> {decision['tier']} "
        f"(score={decision['score']}, age={decision['age_days']}d from "
        f"{decision['age_source']})",
    ]
    policy = trace.get("policy") or {}
    candidates = policy.get("candidates") or []
    if candidates:
        ordered = sorted(candidates, key=lambda row: row["order"])
        lines.append("candidate rules (applied lowest -> highest precedence):")
        for rule in ordered:
            fields = ", ".join(rule.get("fields") or [])
            lines.append(
                f"  [{rule['order']}] priority={rule['priority']} "
                f"match={rule['match']} overrides={fields or '(none)'}")
    elif policy.get("degraded") or trace.get("policy_trace_degraded") or not policy:
        lines.append("candidate rules: unavailable (policy trace degraded)")
    else:
        lines.append("candidate rules: none matched; defaults only")

    chains = (policy.get("field_chains") or {})
    deciding = trace.get("deciding_stage")
    for field, chain in chains.items():
        entries = " -> ".join(
            f"{item.get('source')}={item.get('value')}"
            + (" [wins]" if item.get("effective") else "")
            for item in chain)
        lines.append(f"  field {field}: {entries}")

    for node in trace.get("nodes") or []:
        outcome = node.get("outcome")
        marker = "ADOPTED" if outcome == "adopted" else outcome.upper()
        lines.append(
            f"  stage {node['stage']} {node['rule']} "
            f"(target={node['target_tier']}): {marker}")
        if outcome == "rejected" and node.get("vetoes"):
            for veto in node["vetoes"]:
                lines.append(
                    f"    veto: {veto['field']}={veto['value']} "
                    f"(from {veto['source']}) failed "
                    f"[{veto['comparison']}] at {veto['observed']}")
        if node.get("size_blocked_hot"):
            for item in node.get("components") or []:
                if item["field"] == "max_hot_bytes" and item["passed"]:
                    lines.append(
                        f"    cause: {item['field']}={item['value']} "
                        f"(from {item['source']}) -> "
                        f"{item['observed']} > {item['value']}, "
                        f"cannot remain hot")
        if node.get("demotion_causes") and outcome == "adopted":
            causes = [field for field in node["demotion_causes"]
                      if field != "max_hot_bytes"]
            if causes:
                lines.append(f"    demotion causes: {', '.join(causes)}")
        if node.get("shadowed_by"):
            lines.append(
                f"    shadowed by earlier stage {node['shadowed_by']}")
    if degraded:
        lines.append(
            f"  trace degraded at {trace.get('stage', 'unknown')}: "
            f"{trace.get('error', 'trace unavailable')}")
    return {
        "partition_id": decision["partition_id"],
        "tier": decision["tier"],
        "score": decision["score"],
        "degraded": degraded,
        "deciding_stage": deciding,
        "trace": trace,
        "text": "\n".join(lines),
    }
