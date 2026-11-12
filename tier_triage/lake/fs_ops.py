"""Guarded filesystem operations. Nothing here ever deletes data."""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path


class FsSafetyError(OSError):
    """A path or operation failed a safety invariant."""


class PreflightError(FsSafetyError):
    """The plan cannot be applied safely in the current filesystem state."""


def safe_join(root: str | Path, relative: str) -> Path:
    """Resolve a plan-relative path, rejecting traversal and absolutes."""
    root_path = Path(root).expanduser().resolve()
    candidate = Path(relative)
    if candidate.is_absolute():
        raise FsSafetyError(f"absolute path is not allowed: {relative}")
    joined = (root_path / candidate).resolve(strict=False)
    try:
        joined.relative_to(root_path)
    except ValueError as exc:
        raise FsSafetyError(f"path escapes lake root: {relative}") from exc
    if joined == root_path:
        raise FsSafetyError("lake root itself is not a valid action path")
    return joined


def ensure_under(root: str | Path, path: str | Path) -> Path:
    root_path = Path(root).expanduser().resolve()
    target = Path(path).expanduser().resolve(strict=False)
    try:
        target.relative_to(root_path)
    except ValueError as exc:
        raise FsSafetyError(f"path escapes lake root: {path}") from exc
    return target


def disk_free_bytes(path: str | Path) -> int:
    """Find the nearest existing ancestor and report its free bytes."""
    candidate = Path(path).expanduser().resolve(strict=False)
    while not candidate.exists():
        if candidate.parent == candidate:
            raise FsSafetyError(f"no existing ancestor for {path}")
        candidate = candidate.parent
    return shutil.disk_usage(candidate).free


def check_free_space(path: str | Path, required_bytes: int,
                     reserve_bytes: int = 0) -> None:
    if required_bytes < 0 or reserve_bytes < 0:
        raise ValueError("space requirements cannot be negative")
    free = disk_free_bytes(path)
    if free < required_bytes + reserve_bytes:
        raise PreflightError(
            f"insufficient disk space for {path}: free={free}, "
            f"required={required_bytes}, reserve={reserve_bytes}")


def ensure_parent_dirs(parent: str | Path, lake_root: str | Path) -> list[Path]:
    """Create missing parent directories and return them shallowest-first."""
    root = Path(lake_root).expanduser().resolve()
    target = ensure_under(root, parent)
    missing: list[Path] = []
    cursor = target
    while not cursor.exists():
        if cursor == root:
            break
        missing.append(cursor)
        cursor = cursor.parent
    if not cursor.exists() or not cursor.is_dir():
        raise FsSafetyError(f"cannot create directories below {parent}")
    created: list[Path] = []
    for directory in reversed(missing):
        try:
            directory.mkdir()
            created.append(directory)
        except FileExistsError:
            continue
    return created


def _same_device(source: Path, destination: Path) -> bool:
    ancestor = destination.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    return os.stat(source).st_dev == os.stat(ancestor).st_dev


def atomic_move(source: str | Path, destination: str | Path,
                lake_root: str | Path, token: str | None = None) -> None:
    """Move a directory using a staging name then an atomic rename.

    The source is restored if the second rename fails. Existing destinations
    are always rejected so a migration can never overwrite data.
    """
    root = Path(lake_root).expanduser().resolve()
    src = ensure_under(root, source)
    dst = ensure_under(root, destination)
    if not src.exists():
        raise PreflightError(f"source does not exist: {src}")
    if dst.exists():
        raise PreflightError(f"destination already exists: {dst}")
    if not dst.parent.is_dir():
        raise PreflightError(f"destination parent missing: {dst.parent}")
    if src == dst:
        raise PreflightError("source and destination are identical")
    if not _same_device(src, dst):
        raise FsSafetyError("cross-device moves are not supported")
    marker = token or uuid.uuid4().hex[:12]
    staging = dst.parent / f".tier-triage-tmp-{marker}"
    suffix = 1
    while staging.exists():
        staging = dst.parent / f".tier-triage-tmp-{marker}-{suffix}"
        suffix += 1
    os.replace(src, staging)
    try:
        os.replace(staging, dst)
    except Exception:
        if staging.exists() and not src.exists():
            os.replace(staging, src)
        raise


def soft_delete(path: str | Path, lake_root: str | Path,
                batch_token: str) -> str:
    """Move a path into ``.trash`` and return its relative trash location.

    Purge decisions deliberately do not call this function; they only create a
    review list. It exists for an explicitly approved future disposal workflow.
    """
    root = Path(lake_root).expanduser().resolve()
    source = ensure_under(root, path)
    relative = source.relative_to(root)
    trash_destination = root / ".trash" / batch_token / relative
    ensure_parent_dirs(trash_destination.parent, root)
    atomic_move(source, trash_destination, root, f"trash-{batch_token}")
    return trash_destination.relative_to(root).as_posix()


def prune_created_dirs(created: list[Path]) -> None:
    """Best-effort removal of empty directories made by a failed batch."""
    for directory in reversed(created):
        try:
            directory.rmdir()
        except OSError:
            continue
