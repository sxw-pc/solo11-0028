"""Strict YAML policy loading with line-aware schema errors."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from tier_triage.policy.rules import (
    BOOL_FIELDS,
    CONFIG_FIELDS,
    INT_FIELDS,
    OPTIONAL_INT_FIELDS,
    TABLE_FIELDS,
    TOP_FIELDS,
    config_is_ordered,
    resolve_policy,
)
from tier_triage.util import DataError, sha256_obj


class PolicyError(DataError):
    def __init__(self, message: str, line: int | None = None):
        self.line = line
        prefix = f"policy schema error at line {line}: " if line else "policy schema error: "
        super().__init__(prefix + message)


@dataclass(frozen=True)
class Policy:
    data: dict[str, Any]
    fingerprint: str
    source_path: str

    @property
    def windows(self) -> list[int]:
        return list(self.data["windows"])

    def resolve(self, table: str) -> dict[str, Any]:
        return resolve_policy(self.data, table)


def _line(node: Node | None) -> int | None:
    return node.start_mark.line + 1 if node is not None else None


def _map_entries(node: Node, where: str) -> dict[str, tuple[Node, Node]]:
    if not isinstance(node, MappingNode):
        raise PolicyError(f"{where} must be a mapping", _line(node))
    entries: dict[str, tuple[Node, Node]] = {}
    for key_node, value_node in node.value:
        if not isinstance(key_node, ScalarNode):
            raise PolicyError(f"{where} keys must be scalars", _line(key_node))
        key = str(key_node.value)
        if key in entries:
            raise PolicyError(f"duplicate field {key!r} in {where}", _line(key_node))
        entries[key] = (key_node, value_node)
    return entries


def _check_unknown(entries: dict[str, tuple[Node, Node]], allowed: tuple[str, ...],
                   where: str) -> None:
    for key, (key_node, _) in entries.items():
        if key not in allowed:
            raise PolicyError(f"unknown field {key!r} in {where}", _line(key_node))


def _check_int(value: Any, value_node: Node, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise PolicyError(
            f"{name} must be an integer >= {minimum}", _line(value_node))
    return value


def _check_bool(value: Any, value_node: Node, name: str) -> bool:
    if type(value) is not bool:
        raise PolicyError(f"{name} must be a boolean", _line(value_node))
    return value


def _validate_defaults(node: Node) -> dict[str, Any]:
    entries = _map_entries(node, "defaults")
    _check_unknown(entries, CONFIG_FIELDS + OPTIONAL_INT_FIELDS, "defaults")
    missing = [field for field in CONFIG_FIELDS if field not in entries]
    if missing:
        raise PolicyError(
            f"defaults is missing required fields: {', '.join(missing)}", _line(node))
    values: dict[str, Any] = {}
    for field in INT_FIELDS:
        values[field] = _check_int(yaml.safe_load(
            yaml.serialize(entries[field][1])), entries[field][1], field)
    for field in BOOL_FIELDS:
        values[field] = _check_bool(yaml.safe_load(
            yaml.serialize(entries[field][1])), entries[field][1], field)
    # Optional capacity budgets are validated and kept in the parsed data for
    # consumers, but excluded from the fingerprint payload (see load_policy),
    # so a policy document without them hashes exactly as before.
    for field in OPTIONAL_INT_FIELDS:
        if field in entries:
            values[field] = _check_int(
                yaml.safe_load(yaml.serialize(entries[field][1])),
                entries[field][1], field)
    problems = config_is_ordered(values)
    if problems:
        raise PolicyError("; ".join(problems), _line(node))
    return values


def _validate_rule(node: Node, index: int, defaults: dict[str, Any]) -> dict[str, Any]:
    where = f"tables[{index}]"
    entries = _map_entries(node, where)
    _check_unknown(entries, TABLE_FIELDS, where)
    if "match" not in entries:
        raise PolicyError(f"{where} is missing required field 'match'", _line(node))
    match_value = yaml.safe_load(yaml.serialize(entries["match"][1]))
    if not isinstance(match_value, str) or not match_value.strip():
        raise PolicyError(f"{where}.match must be a non-empty string",
                          _line(entries["match"][1]))
    rule: dict[str, Any] = {"match": match_value.strip(), "priority": 0}
    if "priority" in entries:
        rule["priority"] = _check_int(
            yaml.safe_load(yaml.serialize(entries["priority"][1])),
            entries["priority"][1], f"{where}.priority")
    if "description" in entries:
        description = yaml.safe_load(yaml.serialize(entries["description"][1]))
        if not isinstance(description, str):
            raise PolicyError(f"{where}.description must be a string",
                              _line(entries["description"][1]))
        rule["description"] = description
    for field in INT_FIELDS:
        if field in entries:
            rule[field] = _check_int(
                yaml.safe_load(yaml.serialize(entries[field][1])),
                entries[field][1], f"{where}.{field}")
    for field in BOOL_FIELDS:
        if field in entries:
            rule[field] = _check_bool(
                yaml.safe_load(yaml.serialize(entries[field][1])),
                entries[field][1], f"{where}.{field}")
    for field in OPTIONAL_INT_FIELDS:
        if field in entries:
            rule[field] = _check_int(
                yaml.safe_load(yaml.serialize(entries[field][1])),
                entries[field][1], f"{where}.{field}")
    effective = dict(defaults)
    effective.update({field: rule[field] for field in CONFIG_FIELDS if field in rule})
    problems = config_is_ordered(effective)
    if problems:
        raise PolicyError(
            f"{where} ({rule['match']}) violates ordering: {'; '.join(problems)}",
            _line(node))
    return rule


def _validate_document(root_node: Node) -> dict[str, Any]:
    entries = _map_entries(root_node, "top level")
    _check_unknown(entries, TOP_FIELDS, "top level")
    for field in ("version", "windows", "defaults", "tables"):
        if field not in entries:
            raise PolicyError(f"missing required top-level field {field!r}",
                              _line(root_node))
    version = _check_int(yaml.safe_load(yaml.serialize(entries["version"][1])),
                         entries["version"][1], "version", 1)
    if version != 1:
        raise PolicyError("version must be 1", _line(entries["version"][1]))
    windows_node = entries["windows"][1]
    if not isinstance(windows_node, SequenceNode):
        raise PolicyError("windows must be a list of positive integers",
                          _line(windows_node))
    windows: list[int] = []
    for item in windows_node.value:
        windows.append(_check_int(
            yaml.safe_load(yaml.serialize(item)), item, "window", 1))
    if not windows or windows != sorted(set(windows)):
        raise PolicyError("windows must be strictly increasing and non-empty",
                          _line(windows_node))
    if 30 not in windows:
        raise PolicyError("windows must include 30 for scoring", _line(windows_node))
    defaults = _validate_defaults(entries["defaults"][1])
    tables_node = entries["tables"][1]
    if not isinstance(tables_node, SequenceNode):
        raise PolicyError("tables must be a list", _line(tables_node))
    tables = [
        _validate_rule(rule_node, index, defaults)
        for index, rule_node in enumerate(tables_node.value)
    ]
    return {"version": 1, "windows": windows, "defaults": defaults,
            "tables": tables}


def _fingerprint_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Strip optional capacity fields so legacy policy fingerprints are stable."""
    defaults = {key: value for key, value in data["defaults"].items()
                if key not in OPTIONAL_INT_FIELDS}
    tables = [
        {key: value for key, value in rule.items()
         if key not in OPTIONAL_INT_FIELDS}
        for rule in data["tables"]
    ]
    return {"version": data["version"], "windows": data["windows"],
            "defaults": defaults, "tables": tables}


def load_policy(path: str | Path) -> Policy:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PolicyError(f"policy file not found: {path}") from exc
    try:
        root_node = yaml.compose(text, Loader=yaml.SafeLoader)
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark else None
        raise PolicyError(str(exc), line) from exc
    if root_node is None or not isinstance(loaded, dict):
        raise PolicyError("policy must be a YAML mapping", _line(root_node))
    data = _validate_document(root_node)
    return Policy(data=data,
                  fingerprint=sha256_obj(_fingerprint_payload(data)),
                  source_path=str(source))
