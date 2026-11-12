from pathlib import Path

import pytest

from tier_triage.lake.inventory import scan_lake
from tier_triage.util import DataError


def test_scan_daily_and_multilevel_partitions(lake):
    lake.make("hot", "sales", "orders", ["dt=2026-10-01"], files=2, padding=10)
    lake.make("warm", "ops", "audit_log", ["region=cn", "dt=2026-09-01"], files=1)
    inventory = scan_lake(lake.root)
    rows = {row["partition_id"]: row for row in inventory["partitions"]}
    daily = rows["sales.orders.dt=2026-10-01"]
    assert daily["layer"] == "hot"
    assert daily["keys"] == {"dt": "2026-10-01"}
    assert daily["date"] == "2026-10-01"
    assert daily["file_count"] == 2
    assert daily["last_modified"].endswith("Z")
    assert daily["largest_file"]["bytes"] > 0
    nested = rows["ops.audit_log.region=cn/dt=2026-09-01"]
    assert nested["partition_parts"] == ["region=cn", "dt=2026-09-01"]
    assert nested["keys"] == {"region": "cn", "dt": "2026-09-01"}
    assert nested["path"] == "warm/ops/audit_log/region=cn/dt=2026-09-01"
    assert inventory["inventory_hash"] == scan_lake(lake.root)["inventory_hash"]
    assert inventory["partitions"] == sorted(
        inventory["partitions"], key=lambda row: (
            row["table_name"], row["partition_spec"], row["layer"]))


def test_invalid_paths_are_unparsed_without_aborting(lake):
    lake.make("hot", "db", "table", ["dt=2026-01-01"], files=1)
    bad = lake.root / "hot" / "db" / "table" / "not-a-partition"
    bad.mkdir()
    (bad / "data.txt").write_text("x", encoding="utf-8")
    malformed = lake.root / "hot" / "db" / "table" / "dt="
    malformed.mkdir()
    (lake.root / "hot" / "db" / "table" / "loose.txt").write_text("x", encoding="utf-8")
    inventory = scan_lake(lake.root)
    assert len(inventory["partitions"]) == 1
    reasons = {item["path"]: item["reason"] for item in inventory["unparsed"]}
    assert reasons["hot/db/table/not-a-partition"] == (
        "invalid partition directory (expected key=value)")
    assert reasons["hot/db/table/dt="] == "malformed partition directory"
    assert reasons["hot/db/table/loose.txt"] == "files directly under table directory"


def test_empty_partition_and_empty_lake(lake, tmp_path):
    lake.make("hot", "db", "table", ["dt=2025-12-01"], files=0)
    inventory = scan_lake(lake.root)
    assert len(inventory["partitions"]) == 1
    assert inventory["partitions"][0]["file_count"] == 0
    assert inventory["partitions"][0]["last_modified"] is None
    empty = tmp_path / "empty-lake"
    empty.mkdir()
    empty_inventory = scan_lake(empty)
    assert empty_inventory["partitions"] == []
    assert empty_inventory["inventory_hash"]


def test_duplicate_across_layers_is_reported(lake):
    lake.make("hot", "db", "t", ["dt=2026-01-01"], files=1)
    lake.make("cold", "db", "t", ["dt=2026-01-01"], files=1)
    inventory = scan_lake(lake.root)
    assert len(inventory["partitions"]) == 1
    assert any(item["reason"] == "duplicate partition across layers"
               for item in inventory["unparsed"])


def test_missing_lake_root_raises(tmp_path):
    with pytest.raises(DataError):
        scan_lake(tmp_path / "missing")
