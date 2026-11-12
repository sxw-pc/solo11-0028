import copy
from datetime import datetime, timezone

from tier_triage.decide.scorer import explain_decision, score_partition
from tier_triage.policy.loader import load_policy

AS_OF = datetime(2026, 11, 1, tzinfo=timezone.utc)
DATE = "2026-10-30"


def _part(day=DATE, total_bytes=100):
    return {
        "partition_id": f"demo.t.dt={day}",
        "table_name": "demo.t",
        "partition_spec": f"dt={day}",
        "partition_parts": [f"dt={day}"],
        "layer": "hot",
        "path": f"hot/demo/t/dt={day}",
        "date": day,
        "total_bytes": total_bytes,
        "file_count": 1,
    }


def _stat(reads=0, idle=100, never=False):
    return {"reads_30d": reads, "bytes_30d": reads * 10,
            "never_accessed": never, "idle_days": None if never else idle}


def _policy(write_policy, base_policy, purge=True):
    data = copy.deepcopy(base_policy)
    data["tables"] = [{"match": "demo.*", "priority": 10,
                       "purge_allowed": purge}]
    return load_policy(write_policy(data))


def test_retention_floor_and_warm_and_cold(write_policy, base_policy):
    policy = _policy(write_policy, base_policy)
    resolved = policy.resolve("demo.t")
    recent = score_partition(_part("2026-10-30"), _stat(0, 999, True),
                             resolved, AS_OF)
    assert recent["tier"] == "hot"
    assert recent["reasons"][0]["check"] == "retention_floor"
    warm = score_partition(_part("2026-09-01"), _stat(3, 61),
                           resolved, AS_OF)
    assert warm["tier"] == "warm"
    cold = score_partition(_part("2026-07-01"), _stat(0, 150),
                           resolved, AS_OF)
    assert cold["tier"] == "cold"


def test_purge_requires_allow_and_idle_never_access(write_policy, base_policy):
    enabled = _policy(write_policy, base_policy, purge=True)
    purge = score_partition(_part("2026-01-01"), _stat(0, 999, True),
                            enabled.resolve("demo.t"), AS_OF)
    assert purge["tier"] == "purge_candidate"
    disabled = _policy(write_policy, base_policy, purge=False)
    cold = score_partition(_part("2026-01-01"), _stat(0, 999, True),
                           disabled.resolve("demo.t"), AS_OF)
    assert cold["tier"] == "cold"
    recent_read = score_partition(_part("2026-01-01"), _stat(1, 5),
                                  enabled.resolve("demo.t"), AS_OF)
    assert recent_read["tier"] == "cold"


def test_size_pressure_demotes_and_score_is_stable(write_policy, base_policy):
    policy = _policy(write_policy, base_policy)
    resolved = policy.resolve("demo.t")
    large = score_partition(_part("2026-10-22", total_bytes=2_000_000),
                            _stat(9, 3), resolved, AS_OF)
    assert large["tier"] == "warm"
    small = score_partition(_part("2026-10-22"), _stat(9, 3), resolved, AS_OF)
    assert small["tier"] == "hot"
    assert small["score"] == score_partition(
        _part("2026-10-22"), _stat(9, 3), resolved, AS_OF)["score"]
    assert isinstance(small["score"], int)


def test_explain_lookup_and_age_fallback(write_policy, base_policy):
    policy = _policy(write_policy, base_policy)
    part = _part("2026-10-30")
    part["date"] = None
    part["last_modified"] = "2026-09-01T00:00:00Z"
    decision = score_partition(part, _stat(1, 60),
                               policy.resolve("demo.t"), AS_OF)
    assert decision["age_source"] == "last_modified"
    assert decision["age_days"] == 61
    evaluation = {"decisions": [decision]}
    assert explain_decision(evaluation, decision["partition_id"]) is decision
    assert explain_decision(evaluation, "dt=2026-10-30") is decision
