from __future__ import annotations

import copy
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tier_triage.decide.planner import build_plan
from tier_triage.decide.scorer import evaluate_inventory
from tier_triage.lake.inventory import scan_lake
from tier_triage.policy.loader import load_policy
from tier_triage.usage.stats import compute_stats

AS_OF_TEXT = "2026-11-01T00:00:00Z"


def _base_policy() -> dict:
    return {
        "version": 1,
        "windows": [7, 30, 90],
        "defaults": {
            "hot_retain_days": 7,
            "hot_min_reads_30d": 5,
            "warm_after_days": 30,
            "cold_after_days": 90,
            "cold_max_reads_30d": 1,
            "purge_after_days": 180,
            "purge_idle_days": 30,
            "purge_max_reads_30d": 0,
            "purge_allowed": False,
            "max_hot_bytes": 1048576,
        },
        "tables": [],
    }


@pytest.fixture
def as_of() -> datetime:
    return datetime(2026, 11, 1, tzinfo=timezone.utc)


@pytest.fixture
def base_policy() -> dict:
    return copy.deepcopy(_base_policy())


@pytest.fixture
def write_policy(tmp_path):
    def _write(data: dict | str, name: str = "policy.yaml") -> Path:
        path = tmp_path / name
        text = data if isinstance(data, str) else yaml.safe_dump(
            data, sort_keys=False, allow_unicode=True)
        path.write_text(text, encoding="utf-8")
        return path
    return _write


@pytest.fixture
def lake(tmp_path):
    root = tmp_path / "lake"
    root.mkdir()

    def make(layer: str, db: str, table: str, parts: list[str],
             files: int = 1, padding: int = 16) -> Path:
        directory = root.joinpath(layer, db, table, *parts)
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(files):
            content = f"{db}.{table}|{index}|" + "x" * padding + "\n"
            file_path = directory / f"part-{index:04d}.txt"
            file_path.write_text(content, encoding="utf-8")
            os.utime(file_path, (1760000000 + index, 1760000000 + index))
        return directory
    return SimpleNamespace(root=root, make=make)


@pytest.fixture
def pipeline(lake, tmp_path, as_of):
    def build(policy_data: dict, records: list[dict] | None = None):
        policy_path = tmp_path / f"policy-{len(list(tmp_path.glob('policy-*.yaml')))}.yaml"
        policy_path.write_text(yaml.safe_dump(
            policy_data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        policy = load_policy(policy_path)
        inventory = scan_lake(lake.root)
        stats = compute_stats(inventory, records or [], as_of, policy.windows)
        evaluation = evaluate_inventory(inventory, stats, policy, as_of)
        plan = build_plan(evaluation, inventory, lake.root)
        return SimpleNamespace(policy=policy, policy_path=policy_path,
                               inventory=inventory, stats=stats,
                               evaluation=evaluation, plan=plan)
    return build


@pytest.fixture
def tree_files():
    def walk(root: Path) -> dict[str, int]:
        return {
            path.relative_to(root).as_posix(): path.stat().st_size
            for path in sorted(root.rglob("*")) if path.is_file()
        }
    return walk
