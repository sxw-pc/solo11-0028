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


def resolve_policy(policy_data: dict[str, Any], table: str) -> dict[str, Any]:
    """Merge defaults and matching overrides into one effective config."""
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
    return {
        "table": table,
        "config": config,
        "provenance": provenance,
        "matched": matched,
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
