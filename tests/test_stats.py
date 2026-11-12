from datetime import datetime, timezone

import pytest

from tier_triage.usage.access_log import load_access_log
from tier_triage.usage.stats import access_summary, compute_stats
from tier_triage.util import DataError


def _inventory(*partition_ids):
    parts = []
    for partition_id in partition_ids:
        db, table, spec = partition_id.split(".", 2)
        parts.append({"partition_id": partition_id,
                      "table_name": f"{db}.{table}",
                      "partition_spec": spec})
    return {"partitions": parts, "inventory_hash": "abc"}


def _record(ts, table, partition, byte_count=0, op="read"):
    return {"ts": ts, "table": table, "partition": partition,
            "bytes_read": byte_count, "op": op,
            "read": op in {"read", "scan", "select"}, "line": 1}


def test_window_aggregation_and_missing_access():
    as_of = datetime(2026, 11, 1, tzinfo=timezone.utc)
    inventory = _inventory("sales.orders.dt=2026-10-31", "sales.orders.dt=2026-01-01")
    records = [
        _record("2026-10-31T00:00:00Z", "sales.orders", "dt=2026-10-31", 100),
        _record("2026-10-22T00:00:00Z", "sales.orders", "dt=2026-10-31", 50, "scan"),
        _record("2026-09-22T00:00:00Z", "sales.orders", "dt=2026-10-31", 200),
        _record("2026-11-02T00:00:00Z", "sales.orders", "dt=2026-10-31", 999),
        _record("2026-10-30T00:00:00Z", "sales.orders", "dt=2026-10-31", 500, "write"),
    ]
    stats = compute_stats(inventory, records, as_of)
    row = stats["partitions"]["sales.orders.dt=2026-10-31"]
    assert row["windows"]["7"] == {"reads": 1, "bytes": 100}
    assert row["windows"]["30"] == {"reads": 2, "bytes": 150}
    assert row["windows"]["90"] == {"reads": 3, "bytes": 350}
    assert row["last_access"] == "2026-10-31T00:00:00Z"
    assert row["idle_days"] == 1
    missing = stats["partitions"]["sales.orders.dt=2026-01-01"]
    assert missing["never_accessed"] is True
    assert missing["reads_30d"] == 0
    assert stats["summary"]["never_accessed"] == 1
    assert access_summary(stats)["partitions"] == 2


def test_full_partition_id_and_orphans():
    as_of = datetime(2026, 11, 1, tzinfo=timezone.utc)
    inventory = _inventory("sales.orders.dt=2026-10-31")
    records = [
        _record("2026-10-31T00:00:00Z", "sales.orders",
                "sales.orders.dt=2026-10-31", 7),
        _record("2026-10-31T00:00:00Z", "other.table", "dt=2026-10-31", 9),
    ]
    stats = compute_stats(inventory, records, as_of)
    assert stats["partitions"]["sales.orders.dt=2026-10-31"]["reads_30d"] == 1
    assert stats["summary"]["orphan_usage_keys"] == 1


def test_access_log_jsonl_and_csv_validation(tmp_path):
    jsonl = tmp_path / "access.jsonl"
    jsonl.write_text(
        '{"ts":"2026-10-31T00:00:00Z","table":"db.t","partition":"dt=2026-10-31",'
        '"bytes_read":3,"op":"read"}\n'
        'not json\n'
        '{"ts":"bad","table":"db.t","partition":"dt=x"}\n',
        encoding="utf-8")
    records, warnings = load_access_log(jsonl)
    assert len(records) == 1 and records[0]["bytes_read"] == 3
    assert len(warnings) == 2
    csv_path = tmp_path / "access.csv"
    csv_path.write_text(
        "ts,table,partition,bytes_read,op\n"
        "2026-10-31T00:00:00Z,db.t,dt=2026-10-31,10,scan\n"
        "2026-10-31T00:00:00Z,db.t,dt=2026-10-31,oops,read\n"
        "2026-10-31T00:00:00Z,db.t,,1,read\n", encoding="utf-8")
    records, warnings = load_access_log(csv_path)
    assert len(records) == 1
    assert len(warnings) == 2


def test_bad_usage_extension_and_windows(tmp_path):
    bad = tmp_path / "access.txt"
    bad.write_text("x", encoding="utf-8")
    with pytest.raises(DataError):
        load_access_log(bad)
    with pytest.raises(ValueError, match="include 30"):
        compute_stats(_inventory(), [], "2026-11-01T00:00:00Z", [7, 14])
