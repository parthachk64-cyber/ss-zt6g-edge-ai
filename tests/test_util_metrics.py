import pytest

from sszt6g.metrics import automation_metrics, compare_rates, latency_stats, paired_latency
from sszt6g.simulator import JobRecord
from sszt6g.util import pct_reduction, pp_change, relative_increase_pct, safe_div, stable_id, unit_hash, wilson_interval


def rec(jid, lat, status="completed", missed=False):
    return JobRecord(jid, 0.0, "s", "i", "x", status, "", 2.0, lat - 2.0, 0.0, lat, 400.0, missed)


def test_pct_reduction_arithmetic_and_named_baseline():
    assert pct_reduction(6.9, 3.8) == pytest.approx(44.93, abs=0.01)
    assert pct_reduction(10, 12) == pytest.approx(-20.0)  # worse than baseline is negative, not hidden


def test_zero_or_missing_denominators_return_none():
    assert pct_reduction(0, 5) is None and pct_reduction(None, 5) is None
    assert safe_div(1, 0) is None and relative_increase_pct(0, 0.5) is None
    assert wilson_interval(0, 0) is None


def test_relative_increase_differs_from_percentage_points():
    assert relative_increase_pct(0.80, 0.88) == pytest.approx(10.0)
    assert pp_change(0.80, 0.88) == pytest.approx(8.0)
    c = compare_rates(0.5, 0.6)
    assert c["relative_increase_pct"] == pytest.approx(20.0) and c["percentage_point_change"] == pytest.approx(10.0)
    assert compare_rates(None, 0.6) == {"relative_increase_pct": None, "percentage_point_change": None}


def test_stable_id_and_unit_hash_deterministic():
    assert stable_id("a", 1, [2]) == stable_id("a", 1, [2]) != stable_id("a", 1, [3])
    assert 0 <= unit_hash("x", 1) < 1 and unit_hash("x", 1) == unit_hash("x", 1)


def test_paired_latency_uses_only_jobs_completed_in_both_arms():
    base = [rec("a", 100), rec("b", 200), rec("c", 900)]
    prop = [rec("a", 50), rec("b", 100), rec("c", 0, status="dropped")]
    p = paired_latency(base, prop)
    assert p["comparable_jobs"] == 2 and p["reduction_pct_of_mean"] == pytest.approx(50.0)


def test_latency_stats_counts_drops_and_misses():
    s = latency_stats([rec("a", 100), rec("b", 500, missed=True), rec("c", 0, "dropped"), rec("d", 0, "denied")])
    assert s["jobs_authorized"] == 3 and s["completion_rate"] == pytest.approx(2 / 3)
    assert s["deadline_miss_rate_incl_dropped"] == pytest.approx(2 / 3)


def test_latency_stats_empty():
    s = latency_stats([])
    assert s["completion_rate"] is None and s["latency_ms"]["n"] == 0


def test_automation_duplicates_count_once_and_denominator_is_eligibility():
    elig = {"i1": {"eligible": True}, "i2": {"eligible": True}, "i3": {"eligible": False}}
    rows = [{"intent_id": "i1", "state": "verified", "reason": None, "submitted_ms": 0, "verified_ms": 10_000},
            {"intent_id": "i2", "state": "verified", "reason": None, "submitted_ms": 0, "verified_ms": 30_000},
            {"intent_id": "i3", "state": "denied", "reason": "x", "submitted_ms": 0, "verified_ms": None}]
    m = automation_metrics(rows, elig, deliveries=5, sla_ms=20_000)
    assert m["denominator"] == 2 and m["numerator"] == 1 and m["automation_rate"] == 0.5
    assert m["duplicate_deliveries"] == 2 and m["ineligible_denied"] == 1 and m["eligible_outcomes"]["verified_late"] == 1


def test_automation_zero_eligible_gives_none_rate():
    assert automation_metrics([], {}, 0, 1000)["automation_rate"] is None


def test_automation_counts_failures_rollbacks_and_unfinished():
    elig = {k: {"eligible": True} for k in "abcde"}
    rows = [{"intent_id": "a", "state": "failed", "reason": "capacity_unavailable", "submitted_ms": 0, "verified_ms": None},
            {"intent_id": "b", "state": "escalated", "reason": "verification_failed", "submitted_ms": 0, "verified_ms": None},
            {"intent_id": "c", "state": "applied", "reason": None, "submitted_ms": 0, "verified_ms": None},
            {"intent_id": "d", "state": "denied", "reason": "quota_exceeded", "submitted_ms": 0, "verified_ms": None}]
    o = automation_metrics(rows, elig, 5, 1000)["eligible_outcomes"]
    assert o["failed_capacity"] == 1 and o["rolled_back_escalated"] == 1 and o["unfinished"] == 2 and o["denied_by_policy"] == 1
