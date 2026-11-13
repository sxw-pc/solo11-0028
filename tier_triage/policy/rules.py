"""Pure, deterministic policy-rule matching and override merging."""
from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

CONFIG_FIELDS = (
    "hot_retain_days",
    "hot_min_reads_30d",
    "warm_after_days",
    "cold_after_days",
    "cold_max_reads_30d",
    "purge_after_days",
    "purge_idle_days",
    "purge_max_reads_30d",
    "purge_allowed",
    "max_hot_bytes",
)
INT_FIELDS = tuple(field for field in CONFIG_FIELDS if field != "purge_allowed")
BOOL_FIELDS = ("purge_allowed",)
TABLE_META_FIELDS = ("match", "priority", "description")
TABLE_FIELDS = TABLE_META_FIELDS + CONFIG_FIELDS
TOP_FIELDS = ("version", "windows", "defaults", "tables")


def matching_rules(policy_data: dict[str, Any], table: str) -> list[dict[str, Any]]:
    """Return matching rules in override order (lowest to highest precedence)."""
    matches = [
        rule for rule in policy_data.get("tables", [])
        if fnmatchcase(table, rule["match"])
    ]
    return sorted(matches, key=lambda rule: (rule["priority"], rule["match"]))


def build_rule_trace(policy_data: dict[str, Any], table: str,
                     rules: list[dict[str, Any]],
                     matched: list[dict[str, Any]]) -> dict[str, Any]:
    """Record candidate hits and the per-field override order for one table.

    The application order is byte-for-byte the same ``rules`` list that
    :func:`resolve_policy` merges: ascending ``(priority, match)``.  A higher
    priority or, at equal priority, a lexicographically greater ``match``
    therefore appears later in each field chain and wins.
    """
    defaults = policy_data["defaults"]
    field_chains: dict[str, list[dict[str, Any]]] = {
        field: [{"order": 0, "source": "defaults", "value": defaults[field]}]
        for field in CONFIG_FIELDS
    }
    candidates: list[dict[str, Any]] = []
    for order, rule in enumerate(rules, start=1):
        fields = [field for field in CONFIG_FIELDS if field in rule]
        candidates.append({
            "order": order,
            "match": rule["match"],
            "priority": rule["priority"],
            "description": rule.get("description"),
            "fields": fields,
            "values": {field: rule[field] for field in fields},
        })
        for field in fields:
            field_chains[field].append({
                "order": order,
                "source": rule["match"],
                "priority": rule["priority"],
                "value": rule[field],
            })
    for field, chain in field_chains.items():
        for entry in chain[:-1]:
            entry["effective"] = False
        chain[-1]["effective"] = True
    return {
        "schema_version": 1,
        "degraded": False,
        "table": table,
        "ordering": "ascending (priority, match); later overrides earlier",
        "defaults": {field: defaults[field] for field in CONFIG_FIELDS},
        "candidates": candidates,
        "matched_high_to_low": matched,
        "field_chains": field_chains,
    }


def resolve_policy(policy_data: dict[str, Any], table: str) -> dict[str, Any]:
    """Merge defaults and matching overrides into one effective config.

    The merge order is unchanged; ``trace`` only observes it.  If the trace
    cannot be built, the effective config, provenance and ``matched`` list stay
    authoritative and the trace is marked degraded instead of raising.
    """
    config = {field: policy_data["defaults"][field] for field in CONFIG_FIELDS}
    provenance = {field: "defaults" for field in CONFIG_FIELDS}
    rules = matching_rules(policy_data, table)
    for rule in rules:
        for field in CONFIG_FIELDS:
            if field in rule:
                config[field] = rule[field]
                provenance[field] = rule["match"]
    matched = [
        {
            "match": rule["match"],
            "priority": rule["priority"],
            "overrides": [field for field in CONFIG_FIELDS if field in rule],
        }
        for rule in sorted(
            rules, key=lambda rule: (-rule["priority"], rule["match"]))
    ]
    try:
        trace = build_rule_trace(policy_data, table, rules, matched)
    except Exception as exc:  # trace must never block evaluation
        trace = {
            "schema_version": 1,
            "degraded": True,
            "table": table,
            "stage": "policy_merge",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "table": table,
        "config": config,
        "provenance": provenance,
        "matched": matched,
        "trace": trace,
    }


def config_is_ordered(config: dict[str, Any]) -> list[str]:
    """Return human-readable ordering violations, if any."""
    problems: list[str] = []
    if config["hot_retain_days"] > config["warm_after_days"]:
        problems.append("hot_retain_days must be <= warm_after_days")
    if config["warm_after_days"] > config["cold_after_days"]:
        problems.append("warm_after_days must be <= cold_after_days")
    if config["cold_after_days"] > config["purge_after_days"]:
        problems.append("cold_after_days must be <= purge_after_days")
    if config["cold_max_reads_30d"] > config["hot_min_reads_30d"]:
        problems.append("cold_max_reads_30d must be <= hot_min_reads_30d")
    if config["purge_max_reads_30d"] > config["cold_max_reads_30d"]:
        problems.append("purge_max_reads_30d must be <= cold_max_reads_30d")
    return problems
