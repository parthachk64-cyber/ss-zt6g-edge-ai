"""The decision walkthrough: one burst, one policy denial, one duplicate after a restart.

Fictional two-site Edge-AI inference service. Every number below is produced by running the code on seeded
synthetic arrivals; nothing is quoted from the paper.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np

from .config import World
from .errors import CrashError
from .experiments import run_arm
from .ledger import AuditLog
from .orchestrator import OrchConfig, Orchestrator, ProvisioningIntent
from .prediction import forecast_from_snapshot
from .simulator import Cluster
from .trust import issue_token, make_claim
from .util import stable_id
from .workload import make_scenario

BURST_WINDOW_MS = (40_000.0, 100_000.0)


def _queue_stats(records, lo=BURST_WINDOW_MS[0], hi=BURST_WINDOW_MS[1]) -> dict:
    q = [r.queue_ms for r in records if r.status == "completed" and lo <= r.t_ms < hi]
    lat = [r.latency_ms for r in records if r.status == "completed" and lo <= r.t_ms < hi]
    if not q:
        return {"jobs": 0}
    return {"jobs": len(q), "queue_ms_mean": float(np.mean(q)), "queue_ms_p95": float(np.percentile(q, 95)),
            "latency_ms_mean": float(np.mean(lat)), "latency_ms_p95": float(np.percentile(lat, 95))}


def _first_reserve(run) -> Optional[dict]:
    for e in run.log:
        if e.get("type") == "plan" and e["kind"] in ("reserve", "move") and e["state"] in ("verified", "applied"):
            return e
    return None


def case_burst(world: World, predictor, seed: int, work_dir: Path) -> dict:
    """Reactive vs predictive vs fixed on exactly the same arrivals; trace the first predictive decision."""
    spec = next(s for s in world.settings["test_scenarios"] if s["kind"] == "ramp_burst")
    sc = make_scenario("ramp_burst", seed, world.topo, peak=spec["peak"], ramp_s=spec["ramp_s"], scenario_id=f"demo-ramp_burst-s{seed}")
    runs = {a: run_arm(world, sc, a, predictor, work_dir=work_dir, kind="ramp_burst") for a in ("fixed", "reactive", "predictive")}
    pred = runs["predictive"]
    first = _first_reserve(pred)
    out: dict = {"scenario": sc.describe(), "arms": {a: _queue_stats(r.sim.records) | {"first_action": _first_reserve(r)} for a, r in runs.items()}}
    if first is None:
        out["trace"] = None
        return out
    t = first["t"]
    snap = pred.sim.telemetry.snapshot(t, 12)
    fc = forecast_from_snapshot(predictor, snap, "vision-inference", "edge-east", world.topo.interval_ms)
    row = next(x for x in pred.actions if x["intent_id"] == first["intent_id"])
    out["trace"] = {
        "observation_cutoff_ms": snap.cutoff_ms, "snapshot_id": snap.snapshot_id,
        "snapshot_records_used": len(snap.records), "latest_record_ts_ms": snap.latest_ts_ms,
        "future_records_used": sum(1 for r in snap.records if r.ts_ms > snap.cutoff_ms),
        "ingress_arrivals_east_last6": snap.arrivals("vision-inference", "edge-east")[-6:],
        "forecast": fc.to_dict(), "forecast_matches_logged_id": fc.forecast_id in first["evidence"].get("forecast_ids", []),
        "plan": {k: first[k] for k in ("kind", "site_to", "servers")} | {"evidence": first["evidence"]},
        "decision": json.loads(row["decision"]), "receipts": json.loads(row["receipts"]),
        "state": row["state"], "submitted_ms": row["submitted_ms"], "activate_ms": row["activate_ms"], "verified_ms": row["verified_ms"],
        "intent_to_verified_ms": None if row["verified_ms"] is None else row["verified_ms"] - row["submitted_ms"],
        "ledger_verified": pred.ledger_ok,
    }
    out["same_arrivals"] = len({r.job_id for r in runs["fixed"].sim.records} ^ {r.job_id for r in runs["predictive"].sim.records}) == 0
    return out


def _fresh(world: World, db_path: str = ":memory:", ledger=None, cluster: Optional[Cluster] = None) -> Orchestrator:
    return Orchestrator(world.topo, cluster, world.policy, world.keyring, ledger if ledger is not None else AuditLog(), db_path,
                        OrchConfig(world.settings["orchestrator"]["ttl_ms"], world.settings["orchestrator"]["max_snapshot_age_ms"], world.settings["orchestrator"]["sla_ms"]))


def _intent(world: World, tag: str, tenant: str, service: str, kind: str, servers: int, site_to: Optional[str], site_from: Optional[str] = None,
            at: float = 10_000.0, subject: str = "ops") -> ProvisioningIntent:
    tok = issue_token(world.issuer, make_claim(f"{tenant}-{subject}", tenant, 0.0, 3_600_000.0, policy_revision=world.policy.revision))
    return ProvisioningIntent(stable_id("int", "demo", tag), tenant, service, kind, servers, site_to, site_from, 20_000.0, at, tok, world.policy.revision, "tenant")


def _occupancy(cluster: Cluster) -> dict:
    return {s: {"background": cluster.background.get(s, 0), "allocated": sum(cluster.allocated(sv.service_id, s) for sv in cluster.topo.services),
                "pending_adds": cluster.pending_adds(site=s), "free": cluster.free(s)} for s in cluster.topo.site_ids()}


def case_policy_denial(world: World) -> dict:
    """Capacity is available at the destination, but this tenant is not allowed to MOVE. Capacity must not change."""
    orch = _fresh(world)
    before = _occupancy(orch.cluster)
    it = _intent(world, "deny", "city-traffic", "traffic-analytics", "move", 1, "edge-east", "edge-west")
    res = orch.submit(it, 10_000.0)
    after = _occupancy(orch.cluster)
    return {"intent": {k: it.to_dict()[k] for k in ("intent_id", "tenant", "service", "kind", "servers", "site_from", "site_to")},
            "capacity_available_at_destination": before["edge-east"]["free"] >= 1, "state": res.state, "reasons": res.decision["reasons"],
            "checks": res.decision["checks"], "occupancy_before": before, "occupancy_after": after, "capacity_unchanged": before == after,
            "receipts": res.receipts, "ledger_entries": len(orch.ledger.entries())}


def case_duplicate_after_restart(world: World) -> dict:
    """Crash after the commit receipt but before the apply; restart; recover; then a duplicate delivery."""
    with tempfile.TemporaryDirectory() as td:
        db, ledger_path = str(Path(td) / "state.db"), Path(td) / "audit.jsonl"
        it = _intent(world, "dup", "acme-video", "vision-inference", "reserve", 2, "edge-east", at=10_000.0)
        o1 = _fresh(world, db, AuditLog(ledger_path))
        before = _occupancy(o1.cluster)
        o1.crash_at = "after_commit"
        crashed = False
        try:
            o1.submit(it, 10_000.0)
        except CrashError:
            crashed = True
        row_state_at_crash = o1.store.get(stable_id("act", it.intent_id))["state"]
        o1.close()  # the process dies here; nothing in memory survives
        o2 = _fresh(world, db, AuditLog(ledger_path))  # restart: state and capacity are reloaded from disk
        recovered = o2.recover(11_000.0)
        after_recover = _occupancy(o2.cluster)
        dup = o2.submit(it, 12_000.0)  # the same message delivered again
        after_dup = _occupancy(o2.cluster)
        o2.cluster.activate_due(20_000.0)
        o2.verify_pending(20_000.0)
        final = o2.result(dup.action_id)
        states = [e["entry"]["state"] for e in o2.ledger.entries() if e["entry"]["action_id"] == dup.action_id]
        ledger_ok = o2.ledger.verify().ok
        o2.close()  # release the SQLite handle before the temp directory is removed (required on Windows)
        return {"crashed_after_commit": crashed, "db_state_at_crash": row_state_at_crash, "occupancy_before": before,
                "recovered_actions": [r.state for r in recovered], "occupancy_after_recovery": after_recover,
                "duplicate_delivery": {"duplicate": dup.duplicate, "state": dup.state},
                "occupancy_after_duplicate": after_dup, "capacity_changed_by_duplicate": after_recover != after_dup,
                "allocation_delta_total": after_dup["edge-east"]["pending_adds"], "final_state": final.state,
                "ledger_states_for_action": states, "each_state_recorded_once": len(states) == len(set(states)),
                "ledger_ok": ledger_ok}


def render_trace(world: World, burst: dict, denial: dict, dup: dict) -> str:
    L: list = ["# Decision trace: one burst, one denial, one duplicate", "",
               "Generated by `sszt6g demo`. Fictional two-site Edge-AI inference service on a seeded synthetic workload. All times are SIMULATED "
               "milliseconds; inference is emulated. Nothing here is quoted from the paper.", ""]
    t = burst.get("trace")
    L += ["## Case 1. Demand rises at edge-east: predict, plan, authorize, apply, verify", ""]
    if t is None:
        L += ["No capacity change was proposed in this run.", ""]
    else:
        f = t["forecast"]
        L += [f"* **Snapshot available at the decision.** Observation cutoff {t['observation_cutoff_ms']:.0f} ms; {t['snapshot_records_used']} records, newest stamped "
              f"{t['latest_record_ts_ms']:.0f} ms; records after the cutoff used: {t['future_records_used']}. Snapshot id `{t['snapshot_id']}`.",
              f"* **Observed demand (jobs per 5 s interval at edge-east, oldest to newest):** {t['ingress_arrivals_east_last6']}.",
              f"* **Prediction.** Next interval [{f['target_window_ms'][0]:.0f}, {f['target_window_ms'][1]:.0f}) ms: model `{f['model_id']}` predicts "
              f"{f['predicted_arrivals']:.1f} jobs; persistence would say {f['persistence_arrivals']:.0f}. Forecast id `{f['forecast_id']}` (matches the logged id: {t['forecast_matches_logged_id']}).",
              f"* **Plan.** `{t['plan']['kind']}` {t['plan']['servers']} server(s) at `{t['plan']['site_to']}`; estimated utilisation "
              f"{t['plan']['evidence'].get('est_utilisation')}, need {t['plan']['evidence'].get('need')} servers, have {t['plan']['evidence'].get('have')}.",
              f"* **Policy check (zero trust, default deny).** allow = {t['decision']['allow']}; checks = {t['decision']['checks']}; policy revision {t['decision']['policy_revision']}.",
              "* **Ledger receipts** (tamper-evident audit log, one per state):"]
        for st in ("proposed", "authorized", "committed", "applied", "verified"):
            r = t["receipts"].get(st)
            if r:
                L.append(f"  * `{st}`: seq {r['seq']}, entry hash `{r['entry_hash'][:16]}…`")
        L += [f"* **What changed in the simulated service.** Submitted {t['submitted_ms']:.0f} ms, capacity active {t['activate_ms']:.0f} ms, verified against the next observed "
              f"state at {t['verified_ms']:.0f} ms (intent to verified: {t['intent_to_verified_ms']:.0f} ms simulated). Ledger verifies: {t['ledger_verified']}.", ""]
    L += ["**Same arrivals, three planners, burst window 40 s to 100 s** (queue wait and application latency of jobs that completed; simulated ms):", "",
          "| Planner | jobs | queue mean | queue p95 | latency mean | latency p95 | first capacity action |", "|---|---:|---:|---:|---:|---:|---|"]
    for a, s in burst["arms"].items():
        fa = s.get("first_action")
        L.append(f"| {a} | {s.get('jobs', 0)} | {s.get('queue_ms_mean', float('nan')):.1f} | {s.get('queue_ms_p95', float('nan')):.1f} | "
                 f"{s.get('latency_ms_mean', float('nan')):.1f} | {s.get('latency_ms_p95', float('nan')):.1f} | {('t=%.0f ms' % fa['t']) if fa else 'none'} |")
    L += ["", f"Arrivals identical across arms: {burst.get('same_arrivals')}. One seed: this is an illustration, not a result. See `docs/results.md` for the multi-seed comparison.", "",
          "## Case 2. Capacity is free, tenant policy says no", "",
          f"`{denial['intent']['tenant']}` asks to **move** {denial['intent']['servers']} server of `{denial['intent']['service']}` from {denial['intent']['site_from']} to {denial['intent']['site_to']}. "
          f"Capacity available at the destination: {denial['capacity_available_at_destination']}.", "",
          f"* Outcome: **{denial['state']}**, reasons {denial['reasons']}.",
          f"* Capacity unchanged: **{denial['capacity_unchanged']}** (occupancy before and after are identical).",
          f"* Ledger: {denial['ledger_entries']} entries (`proposed`, `denied`); no `committed`, no `applied`.", "",
          "## Case 3. Duplicate delivery after a restart", "",
          f"* The process crashed after the `committed` receipt and before the apply (crash raised: {dup['crashed_after_commit']}; database state at the crash: `{dup['db_state_at_crash']}`).",
          f"* After restart, recovery returned {dup['recovered_actions']} and applied the change **once**.",
          f"* The same intent id was then delivered again: duplicate = {dup['duplicate_delivery']['duplicate']}, outcome `{dup['duplicate_delivery']['state']}`; capacity changed by the duplicate: **{dup['capacity_changed_by_duplicate']}**.",
          f"* Pending/allocated additions at edge-east after both: {dup['allocation_delta_total']} (requested 2, not 4). Final state `{dup['final_state']}`.",
          f"* Ledger states recorded for the action: {dup['ledger_states_for_action']} (each once: {dup['each_state_recorded_once']}); ledger verifies: {dup['ledger_ok']}.", ""]
    return "\n".join(L)


def run_walkthrough(world: World, predictor, out_dir: Path, seed: Optional[int] = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    seed = seed if seed is not None else world.settings["seeds"]["demo"]
    with tempfile.TemporaryDirectory() as td:
        burst = case_burst(world, predictor, seed, Path(td))
    denial, dup = case_policy_denial(world), case_duplicate_after_restart(world)
    result = {"seed": seed, "burst": burst, "denial": denial, "duplicate_after_restart": dup}
    (out_dir / "decision_trace.json").write_text(json.dumps(result, indent=2, sort_keys=True, default=str), encoding="utf-8")
    md = render_trace(world, burst, denial, dup)
    (out_dir / "decision_trace.md").write_text(md, encoding="utf-8")
    return result
