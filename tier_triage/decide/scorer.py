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
    }


def evaluate_inventory(inventory: dict[str, Any], stats: dict[str, Any],
                       policy: Policy, as_of: datetime | str) -> dict[str, Any]:
    clock = parse_datetime(as_of)
    decisions: list[dict[str, Any]] = []
    for part in inventory.get("partitions", []):
        stat = stats.get("partitions", {}).get(part["partition_id"])
        if stat is None:
            stat = {"reads_30d": 0, "never_accessed": True}
        resolved = policy.resolve(part["table_name"])
        decisions.append(score_partition(part, stat, resolved, clock))
    decisions.sort(key=lambda row: row["partition_id"])
    distribution = {tier: 0 for tier in TIER_ORDER}
    for decision in decisions:
        distribution[decision["tier"]] += 1
    return {
        "schema_version": 1,
        "as_of": iso_z(clock),
        "inventory_hash": inventory.get("inventory_hash"),
        "policy_fingerprint": policy.fingerprint,
        "decisions": decisions,
        "summary": {
            "partitions": len(decisions),
            "tier_distribution": distribution,
            "decision_bytes": {
                tier: sum(row["total_bytes"] for row in decisions
                          if row["tier"] == tier)
                for tier in TIER_ORDER
            },
        },
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
