"""Hive-style partition path parsing and date normalization."""
from __future__ import annotations

import re
from datetime import date
from typing import Iterable

KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.+)$")
DATE_KEYS = ("dt", "date", "day", "event_date")
TIERS = ("hot", "warm", "cold")
LAYOUT = "tiered-v1"


def parse_partition_component(name: str) -> tuple[str, str] | None:
    """Return ``(key, value)`` for ``key=value``, otherwise ``None``.

    Empty values, reserved path segments and a leading dot are rejected.
    """
    if not isinstance(name, str) or name in {"", ".", ".."}:
        return None
    match = KEY_RE.match(name)
    if not match:
        return None
    key, value = match.groups()
    if value.strip() == "" or value.startswith(".") or "/" in value:
        return None
    return key, value


def is_partition_component(name: str) -> bool:
    return parse_partition_component(name) is not None


def parse_partition_spec(parts: Iterable[str]) -> dict[str, str] | None:
    """Parse one or more path segments into an ordered partition dictionary.

    Ordered dictionaries are used so multi-level partition paths remain
    reproducible when rendered back to disk.
    """
    keys: dict[str, str] = {}
    for part in parts:
        parsed = parse_partition_component(part)
        if parsed is None:
            return None
        key, value = parsed
        if key in keys:
            return None
        keys[key] = value
    return keys if keys else None


def partition_spec(parts: Iterable[str]) -> str:
    return "/".join(parts)


def make_partition_id(db: str, table: str, spec: str) -> str:
    return f"{db}.{table}.{spec}"


def date_from_partition(keys: dict[str, str]) -> date | None:
    """Extract a date partition value, accepting ``YYYY-MM-DD`` only."""
    for key in DATE_KEYS:
        raw = keys.get(key)
        if raw:
            try:
                return date.fromisoformat(raw)
            except ValueError:
                continue
    return None


def normalize_table_name(db: str, table: str) -> str:
    return f"{db}.{table}"


def render_partition_path(parts: list[str]) -> str:
    """Render validated partition parts using forward slashes."""
    if not parts:
        raise ValueError("partition path cannot be empty")
    for part in parts:
        if not is_partition_component(part):
            raise ValueError(f"invalid partition component: {part!r}")
    return "/".join(parts)


def split_table_name(value: str) -> tuple[str, str] | None:
    pieces = value.split(".")
    if len(pieces) != 2 or not all(pieces):
        return None
    return pieces[0], pieces[1]
