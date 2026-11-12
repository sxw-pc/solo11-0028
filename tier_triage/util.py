"""Small shared helpers: canonical JSON, hashing, UTC time, safe files."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


class DataError(ValueError):
    """Raised for malformed user data or plans."""


def canonical_json(value: Any) -> str:
    """Stable JSON used by every fingerprint in the project."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_obj(value: Any) -> str:
    return sha256_text(canonical_json(value))


def parse_datetime(value: str | datetime) -> datetime:
    """Parse an ISO timestamp and return an aware UTC datetime."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise DataError(f"invalid ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_z(value: datetime) -> str:
    """Serialize UTC at second precision for stable audits."""
    return parse_datetime(value).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def now_iso() -> str:
    return iso_z(datetime.now(timezone.utc))


def parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def load_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DataError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DataError(f"invalid JSON in {path}: {exc}") from exc


def atomic_write_text(path: str | Path, text: str) -> None:
    """Write text and replace the destination atomically."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=str(destination.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp_name, destination)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def write_json(path: str | Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, sort_keys=True, indent=2,
                                       ensure_ascii=False) + "\n")


def relpath_posix(path: str | Path, root: str | Path) -> str:
    return Path(os.path.relpath(Path(path), Path(root))).as_posix()
