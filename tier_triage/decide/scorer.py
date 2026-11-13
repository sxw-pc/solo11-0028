"""Deterministic hot/warm/cold/purge evaluation with explanation chains."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from tier_triage.policy.loader import Policy
from tier_triage.policy.rules import OPTIONAL_INT_FIELDS
from tier_triage.util import DataError, clamp, iso_z, parse_datetime

TIER_ORDER = ("hot", "warm", "cold", "purge_candidate")
SIZE_BAND_ORDER = ("empty", "small", "near_limit", "oversize", "unknown")


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


def size_band(total_bytes: int, max_hot_bytes: int) -> str:
    """Classify a partition against the hot-layer byte threshold.

    Bands are intentionally relative to the policy threshold so the label
    stays meaningful per table family:

    * ``empty``       — no profiled files (0 bytes);
    * ``small``       — up to 50% of ``max_hot_bytes``;
    * ``near_limit``  — 50%–100% of ``max_hot_bytes``;
    * ``oversize``    — strictly above ``max_hot_bytes`` (hot is forbidden);
    * ``unknown``     — volume could not be measured (unreadable/incomplete);
      such a partition is never assumed safely small.
    """
    if total_bytes <= 0:
        return "empty"
    if max_hot_bytes <= 0:
        return "oversize"
    if total_bytes > max_hot_bytes:
        return "oversize"
    if total_bytes * 2 >= max_hot_bytes:
        return "near_limit"
    return "small"


def _profile_trusted(profile_row: dict[str, Any] | None) -> bool:
    """Whether the measured bytes can drive a capacity conclusion."""
    return profile_row is not None and profile_row.get("status") == "ok"


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
                    resolved: dict[str, Any], as_of: datetime,
                    profile_row: dict[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate one partition. All comparisons use the injected clock.

    When ``profile_row`` is supplied, its measured bytes/file counts override
    the inventory values and the policy's capacity threshold becomes a hard
    exclusion: a partition above ``max_hot_bytes`` can never land in ``hot``,
    even while still inside the hot retention window. Without a profile the
    original age/reads-only behavior is preserved exactly.
    """
    config = resolved["config"]
    age_days, age_source = partition_age_days(part, as_of)
    reads_30d = int(stat.get("reads_30d", 0))
    bytes_30d = int(stat.get("bytes_30d", 0))
    never_accessed = bool(stat.get("never_accessed", True))
    idle_days = stat.get("idle_days")
    if profile_row is not None:
        total_bytes = int(profile_row.get("total_bytes", 0))
        file_count = int(profile_row.get("file_count", 0))
        size_source = "volume_profile"
    else:
        total_bytes = int(part.get("total_bytes", 0))
        file_count = int(part.get("file_count", 0))
        size_source = "inventory"
    age_value = age_days if age_days is not None else 0
    checks: list[dict[str, Any]] = []
    decisions: list[str] = []

    retention_passed = age_days is not None and age_days < config["hot_retain_days"]
    checks.append(_check(
        "retention_floor", retention_passed,
        f"age={age_days}d from {age_source}; hot_retain_days={config['hot_retain_days']}"))
    if retention_passed:
        decisions.append("hot")

    # Capacity exclusion only exists when an independent volume profile is
    # attached with trustworthy bytes. It is evaluated after the retention
    # floor so an oversize young partition is visibly *removed* from hot rather
    # than silently scored warm. Unreadable/incomplete rows carry no trusted
    # size, so they never pass a hard exclusion on a false zero; the capacity
    # check is recorded as not passed with the real status and the partition
    # falls through to the normal age/reads evaluation.
    profile_status = profile_row.get("status") if profile_row is not None else None
    trusted = _profile_trusted(profile_row)
    over_hot_limit = trusted and total_bytes > config["max_hot_bytes"]
    capacity_passed = profile_row is None or (trusted and not over_hot_limit)
    capacity_check: dict[str, Any] | None = None
    if profile_row is not None:
        capacity_check = _check(
            "capacity_hot_limit", capacity_passed,
            f"source=volume_profile; total_bytes={total_bytes}; "
            f"max_hot_bytes={config['max_hot_bytes']}; "
            f"profile_status={profile_status}; "
            f"hot_forbidden={str(over_hot_limit).lower()}")
        if over_hot_limit:
            decisions.append("warm")

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
    # The legacy inline byte check is retained for profile-free runs. With a
    # profile the decision is owned by the explicit capacity_hot_limit check
    # above, so it is not double-counted here.
    warm_size = False if profile_row is not None else (
        total_bytes > config["max_hot_bytes"])
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
    if capacity_check is not None:
        # Appended after the legacy-ordered checks so the decisive slicing
        # below (retention/purge/cold/warm/default) keeps its original indices.
        checks.append(capacity_check)

    # Decisive reason chain. The capacity exclusion only rewrites the chain
    # when it actually changes the landing tier: retention would keep the
    # partition hot, but its measured volume forbids hot. For purge/cold
    # outcomes the normal age/reads check that decided the tier stays decisive.
    if retention_passed and profile_row is not None and not capacity_passed:
        tier = "warm"
        decisive_checks = [checks[0], capacity_check]
    elif retention_passed and capacity_passed:
        tier = "hot"
        decisive_checks = checks[:1]
    elif "purge_candidate" in decisions:
        tier = "purge_candidate"
        decisive_checks = checks[:2]
    elif "cold" in decisions:
        tier = "cold"
        decisive_checks = checks[:3]
    elif "warm" in decisions:
        tier = "warm"
        decisive_checks = checks[:4]
    else:
        tier = "hot"
        decisive_checks = checks[:5]
    score = _score(reads_30d, idle_days, bytes_30d, never_accessed)
    result = {
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
        "file_count": file_count,
        "total_bytes": total_bytes,
        "policy": resolved["config"],
        "policy_sources": resolved["provenance"],
        "matched_rules": resolved["matched"],
        "reasons": decisive_checks,
        "checks": checks,
    }
    if profile_row is not None:
        # Extra, profile-only payload. Every key here is new so evaluations
        # produced without a profile remain byte-identical to the old output.
        band = ("unknown" if not trusted
                else size_band(total_bytes, config["max_hot_bytes"]))
        result["size_source"] = size_source
        result["size_band"] = band
        result["capacity"] = {
            "max_hot_bytes": int(config["max_hot_bytes"]),
            "total_bytes": total_bytes,
            "over_hot_limit": over_hot_limit,
            "hot_forbidden": over_hot_limit,
            "size_trusted": trusted,
            "profile_status": profile_row.get("status"),
            "avg_file_size": profile_row.get("avg_file_size"),
            "file_count": file_count,
        }
    return result


def _strip_optional_capacity(decision: dict[str, Any]) -> None:
    """Remove optional capacity-budget fields from a legacy decision.

    A policy may declare ``max_warm_bytes`` while a run does not attach a
    volume profile. The optional field is still resolved onto the policy
    config, so strip it from the embedded config, provenance and matched-rule
    overrides to keep profile-free artifacts byte-identical to the original
    behavior.
    """
    for field in OPTIONAL_INT_FIELDS:
        decision.get("policy", {}).pop(field, None)
        decision.get("policy_sources", {}).pop(field, None)
    kept_rules = []
    for rule in decision.get("matched_rules", []):
        overrides = rule.get("overrides")
        if overrides:
            rule["overrides"] = [f for f in overrides
                                 if f not in OPTIONAL_INT_FIELDS]
        # A rule that only overrode optional capacity fields leaves no trace
        # in the legacy artifact.
        if rule.get("overrides"):
            kept_rules.append(rule)
    if "matched_rules" in decision:
        decision["matched_rules"] = kept_rules


def evaluate_inventory(inventory: dict[str, Any], stats: dict[str, Any],
                       policy: Policy, as_of: datetime | str,
                       profile: dict[str, Any] | None = None) -> dict[str, Any]:
    clock = parse_datetime(as_of)
    profile_rows: dict[str, dict[str, Any]] = {}
    if profile is not None:
        if profile.get("inventory_hash") != inventory.get("inventory_hash"):
            raise DataError(
                "volume profile inventory_hash does not match the scanned lake; "
                "rebuild the profile against this lake directory")
        profile_rows = {row["partition_id"]: row
                        for row in profile.get("partitions", [])}
        missing = sorted(
            part["partition_id"] for part in inventory.get("partitions", [])
            if part["partition_id"] not in profile_rows)
        if missing:
            raise DataError(
                "volume profile is missing partitions: " + ", ".join(missing[:10]))
    decisions: list[dict[str, Any]] = []
    for part in inventory.get("partitions", []):
        stat = stats.get("partitions", {}).get(part["partition_id"])
        if stat is None:
            stat = {"reads_30d": 0, "never_accessed": True}
        resolved = policy.resolve(part["table_name"])
        decisions.append(score_partition(
            part, stat, resolved, clock,
            profile_rows.get(part["partition_id"]) if profile is not None else None))
    if profile is None:
        for decision in decisions:
            _strip_optional_capacity(decision)
    decisions.sort(key=lambda row: row["partition_id"])
    distribution = {tier: 0 for tier in TIER_ORDER}
    for decision in decisions:
        distribution[decision["tier"]] += 1
    result = {
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
    if profile is not None:
        oversize = [row["partition_id"] for row in decisions
                    if row.get("capacity", {}).get("over_hot_limit")]
        result["capacity_profile"] = {
            "profile_hash": profile.get("profile_hash"),
            "as_of": profile.get("as_of"),
            "oversize_partitions": sorted(oversize),
            "anomaly_count": len(profile.get("anomalies", [])),
            "profile_summary": profile.get("summary", {}),
        }
    return result


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
