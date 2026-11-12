"""Load local JSONL/CSV access logs with line-level validation."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from tier_triage.util import DataError, iso_z, parse_datetime

READ_OPS = frozenset({"read", "scan", "select"})


def _parse_record(raw: dict[str, Any], line: int,
                  warnings: list[dict[str, Any]]) -> dict[str, Any] | None:
    missing = [key for key in ("ts", "table", "partition") if not raw.get(key)]
    if missing:
        warnings.append({"line": line, "reason": f"missing required: {', '.join(missing)}"})
        return None
    try:
        timestamp = parse_datetime(str(raw["ts"]))
    except DataError as exc:
        warnings.append({"line": line, "reason": str(exc)})
        return None
    table = str(raw["table"]).strip()
    partition = str(raw["partition"]).strip().strip("/")
    if not table or "." not in table:
        warnings.append({"line": line,
                         "reason": "table must use db.table form"})
        return None
    if not partition or any(part in {"", ".", ".."} for part in partition.split("/")):
        warnings.append({"line": line, "reason": "invalid partition spec"})
        return None
    try:
        bytes_read = int(raw.get("bytes_read", 0) or 0)
    except (TypeError, ValueError):
        warnings.append({"line": line, "reason": "bytes_read must be an integer"})
        return None
    if bytes_read < 0:
        warnings.append({"line": line, "reason": "bytes_read cannot be negative"})
        return None
    op = str(raw.get("op", "read") or "read").strip().lower()
    return {
        "ts": iso_z(timestamp),
        "table": table,
        "partition": partition,
        "bytes_read": bytes_read,
        "op": op,
        "read": op in READ_OPS,
        "line": line,
    }


def _load_jsonl(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.append({"line": line_number, "reason": f"invalid JSON: {exc.msg}"})
            continue
        if not isinstance(raw, dict):
            warnings.append({"line": line_number, "reason": "record must be an object"})
            continue
        parsed = _parse_record(raw, line_number, warnings)
        if parsed:
            records.append(parsed)
    return records, warnings


def _load_csv(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(text))
    required = {"ts", "table", "partition"}
    fields = set(reader.fieldnames or [])
    missing = required - fields
    if missing:
        raise DataError(f"access CSV missing columns: {', '.join(sorted(missing))}")
    for line_number, raw in enumerate(reader, start=2):
        parsed = _parse_record(raw, line_number, warnings)
        if parsed:
            records.append(parsed)
    return records, warnings


def load_access_log(path: str | Path) -> tuple[list[dict[str, Any]],
                                                list[dict[str, Any]]]:
    """Return ``(records, warnings)``; malformed rows never abort the load."""
    log_path = Path(path)
    if not log_path.is_file():
        raise DataError(f"access log not found: {path}")
    text = log_path.read_text(encoding="utf-8")
    suffix = log_path.suffix.lower()
    if suffix in {".jsonl", ".ndjson"}:
        return _load_jsonl(text)
    if suffix == ".csv":
        return _load_csv(text)
    raise DataError(f"unsupported access log type: {suffix or '<none>'}")
