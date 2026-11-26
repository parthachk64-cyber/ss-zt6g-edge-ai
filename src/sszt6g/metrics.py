"""Metric definitions. Pure functions over recorded outputs, so each headline number can be recomputed.

Latency      : completion time minus arrival time, in SIMULATED milliseconds (network + queue + compute).
Reduction    : 100 * (baseline - proposed) / baseline, baseline named and positive, on COMPARABLE jobs
               (completed in both arms), with drops, deadline misses and completion rate published beside it.
Automation   : unique policy-eligible intents verified automatically within SLA / all unique eligible intents.
Detection    : accuracy (TP+TN)/N with the full confusion matrix (see detection.confusion_metrics).
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from .util import describe, pct_reduction, pp_change, relative_increase_pct, safe_div


def latency_stats(records: Sequence) -> dict:
    """Per-arm job accounting. `records` are simulator JobRecords."""
    total = len(records)
    by = {"completed": 0, "dropped": 0, "denied": 0, "unfinished": 0}
    for r in records:
        by[r.status] += 1
    authorized = total - by["denied"]
    done = [r for r in records if r.status == "completed"]
    late = sum(1 for r in done if r.deadline_missed)
    never = by["dropped"] + by["unfinished"]
    return {
        "jobs_total": total, **{f"jobs_{k}": v for k, v in by.items()},
        "jobs_authorized": authorized,
        "completion_rate": safe_div(by["completed"], authorized),
        "drop_rate": safe_div(by["dropped"], authorized),
        "late_completed": late,
        "deadline_miss_rate_incl_dropped": safe_div(late + never, authorized),
        "latency_ms": describe(r.latency_ms for r in done),
        "queue_ms": describe(r.queue_ms for r in done),
        "network_ms": describe(r.net_ms for r in done),
        "compute_ms": describe(r.compute_ms for r in done),
    }


def paired_latency(baseline_records: Sequence, proposed_records: Sequence) -> dict:
    """Compare two arms on the SAME arrivals, restricted to jobs completed in both."""
    b = {r.job_id: r for r in baseline_records if r.status == "completed"}
    p = {r.job_id: r for r in proposed_records if r.status == "completed"}
    common = sorted(set(b) & set(p))
    bl, pl = [b[j].latency_ms for j in common], [p[j].latency_ms for j in common]
    out = {"comparable_jobs": len(common), "baseline_mean_ms": None, "proposed_mean_ms": None, "reduction_pct_of_mean": None,
           "baseline_median_ms": None, "proposed_median_ms": None, "reduction_pct_of_median": None,
           "baseline_p95_ms": None, "proposed_p95_ms": None, "reduction_pct_of_p95": None}
    if common:
        out.update(baseline_mean_ms=float(np.mean(bl)), proposed_mean_ms=float(np.mean(pl)),
                   baseline_median_ms=float(np.median(bl)), proposed_median_ms=float(np.median(pl)),
                   baseline_p95_ms=float(np.percentile(bl, 95)), proposed_p95_ms=float(np.percentile(pl, 95)))
        out["reduction_pct_of_mean"] = pct_reduction(out["baseline_mean_ms"], out["proposed_mean_ms"])
        out["reduction_pct_of_median"] = pct_reduction(out["baseline_median_ms"], out["proposed_median_ms"])
        out["reduction_pct_of_p95"] = pct_reduction(out["baseline_p95_ms"], out["proposed_p95_ms"])
    return out


def bootstrap_mean_ci(values: Sequence[float], n_boot: int = 2000, seed: int = 7) -> Optional[tuple]:
    """Percentile bootstrap CI of the mean across independent paired seeds."""
    v = np.asarray([x for x in values if x is not None], dtype=float)
    if v.size < 2:
        return None
    rng = np.random.default_rng(seed)
    means = rng.choice(v, size=(n_boot, v.size), replace=True).mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def automation_metrics(rows: Sequence[dict], eligibility: dict, deliveries: int, sla_ms: float) -> dict:
    """rows: one dict per UNIQUE tenant intent that reached the orchestrator
    (intent_id, state, reason, submitted_ms, verified_ms). eligibility: intent_id -> {eligible, ...}.
    deliveries: number of intent messages delivered, duplicates included."""
    by_id = {r["intent_id"]: r for r in rows}
    eligible = [i for i, e in eligibility.items() if e["eligible"]]
    inelig = [i for i, e in eligibility.items() if not e["eligible"]]
    out = {"unique_intents": len(eligibility), "deliveries": deliveries, "duplicate_deliveries": deliveries - len(eligibility),
           "eligible": len(eligible), "ineligible": len(inelig)}
    cat = {"verified_in_sla": 0, "verified_late": 0, "denied_by_policy": 0, "failed_capacity": 0, "failed_other": 0,
           "rolled_back_escalated": 0, "unfinished": 0}
    for i in eligible:
        r = by_id.get(i)
        if r is None or r["state"] in ("received", "proposed", "authorized", "committed", "applied"):
            cat["unfinished"] += 1
        elif r["state"] == "verified":
            cat["verified_in_sla" if r["verified_ms"] - r["submitted_ms"] <= sla_ms else "verified_late"] += 1
        elif r["state"] == "denied":
            cat["denied_by_policy"] += 1
        elif r["state"] == "failed" and r["reason"] == "capacity_unavailable":
            cat["failed_capacity"] += 1
        elif r["state"] in ("escalated", "rolled_back"):
            cat["rolled_back_escalated"] += 1
        else:
            cat["failed_other"] += 1
    out["eligible_outcomes"] = cat
    out["numerator"] = cat["verified_in_sla"]
    out["denominator"] = len(eligible)
    out["automation_rate"] = safe_div(cat["verified_in_sla"], len(eligible))
    out["ineligible_denied"] = sum(1 for i in inelig if by_id.get(i, {}).get("state") == "denied")
    out["ineligible_not_denied"] = sum(1 for i in inelig if by_id.get(i, {}).get("state") not in (None, "denied"))
    return out


def compare_rates(baseline_rate: Optional[float], proposed_rate: Optional[float]) -> dict:
    """Both readings of "an increase of X%": relative to the baseline rate and in percentage points."""
    if baseline_rate is None or proposed_rate is None:
        return {"relative_increase_pct": None, "percentage_point_change": None}
    return {"relative_increase_pct": relative_increase_pct(baseline_rate, proposed_rate), "percentage_point_change": pp_change(baseline_rate, proposed_rate)}
