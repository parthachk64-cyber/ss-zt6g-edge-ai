"""Experiment harness: identical arrivals, paired seeds, constant trust and ledger across planner arms."""
from __future__ import annotations

import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import detection as det
from .config import World, repo_root
from .fixtures import generate_intents, generate_security, assign_splits
from .ledger import AuditLog, NullLedger, QuorumLedger
from .metrics import automation_metrics, bootstrap_mean_ci, compare_rates, latency_stats, paired_latency
from .orchestrator import ClosedLoopController, OrchConfig, Orchestrator, ProvisioningIntent
from .planners import PlannerConfig, make_planner
from .prediction import Forecaster, PersistenceForecaster, error_metrics, feature_vector, training_rows
from .simulator import Cluster, SimResult, Simulator
from .trust import PermissiveEvaluator, Request, evaluate, issue_token, make_claim
from .util import describe, stable_id, unit_hash
from .workload import generate_jobs, make_scenario

ARMS = ("fixed", "reactive", "predictive")


@dataclass
class ArmRun:
    arm: str
    scenario_id: str
    seed: int
    kind: str
    sim: SimResult
    actions: list
    log: list
    timings: list
    ledger_ok: bool
    ledger_entries: int
    intents_delivered: int = 0


def make_ledger(kind: str, work_dir: Optional[Path], tag: str):
    if kind == "none":
        return NullLedger()
    if kind == "audit":
        return AuditLog(Path(work_dir) / f"{tag}.audit.jsonl" if work_dir else None)
    if kind == "quorum":
        return QuorumLedger(4, directory=Path(work_dir) / f"{tag}.quorum" if work_dir else None)
    raise ValueError(f"unknown ledger kind {kind}")


def run_arm(world: World, scenario, arm: str, predictor, *, intents: Optional[list] = None, lost_frac: float = 0.0, ledger: str = "audit",
            work_dir: Optional[Path] = None, kind: str = "") -> ArmRun:
    s = world.settings
    lost = (lambda aid: unit_hash(s["faults"]["salt"], aid) < lost_frac) if lost_frac > 0 else None
    cluster = Cluster(world.topo, lost)
    led = make_ledger(ledger, work_dir, f"{scenario.scenario_id}-{arm}")
    bench = ledger == "none"
    orch = Orchestrator(world.topo, cluster, world.policy, world.keyring, led, ":memory:",
                        OrchConfig(s["orchestrator"]["ttl_ms"], s["orchestrator"]["max_snapshot_age_ms"], s["orchestrator"]["sla_ms"]),
                        None, allow_benchmark_adapters=bench)
    ctl = ClosedLoopController(orch, make_planner(arm, PlannerConfig(**s["planner"])), predictor, world.tokens, world.policy.revision,
                               intents, s["orchestrator"]["cooldown_ms"])
    res = Simulator(world.topo, cluster, scenario.horizon_ms, ctl).run(generate_jobs(scenario))
    rep = led.verify()
    rows = [dict(r) for r in orch.store.rows()]
    return ArmRun(arm, scenario.scenario_id, scenario.seed, kind, res, rows, ctl.log, orch.timings, rep.ok, rep.entries, len(intents or []))


# --------------------------------------------------------------------------- forecaster
def training_scenarios(world: World) -> list:
    out = []
    for i, seed in enumerate(world.settings["seeds"]["train"]):
        kind = ("ramp_burst", "step_burst", "steady_low")[i % 3]
        out.append(make_scenario(kind, seed, world.topo, peak=(14.0, 18.0, 22.0, 28.0, 32.0)[i % 5], ramp_s=(10.0, 15.0, 30.0, 40.0)[i % 4],
                                 scenario_id=f"train-{kind}-s{seed}"))
    return out


def _telemetry_only(world: World, scenario) -> list:
    sim = Simulator(world.topo, Cluster(world.topo), scenario.horizon_ms, None)
    sim.run(generate_jobs(scenario))
    return sim.telemetry.all()


def train_forecaster(world: World) -> tuple:
    f = world.settings["forecaster"]
    Xs, ys, ids = [], [], []
    for sc in training_scenarios(world):
        recs = _telemetry_only(world, sc)
        for spec in world.topo.services:
            for site in world.topo.site_ids():
                X, y = training_rows(recs, spec.service_id, site, f["lags"])
                if len(y):
                    Xs.append(X)
                    ys.append(y)
        ids.append(sc.scenario_id)
    X, y = np.vstack(Xs), np.concatenate(ys)
    model = Forecaster(f["alpha"], f["lags"]).fit(X, y, {"train_scenarios": ids, "train_seeds": world.settings["seeds"]["train"],
                                                         "estimator": "StandardScaler + Ridge", "note": "companion choice; not from the paper"})
    return model, {"train_scenarios": len(ids), "rows": int(len(y))}


def evaluate_forecaster(world: World, model: Forecaster) -> dict:
    """One-step-ahead error on HELD-OUT scenarios (different seeds and a peak/ramp never used in training)."""
    persist = PersistenceForecaster()
    out: dict = {}
    allt, allm, allp = [], [], []
    for spec in world.settings["test_scenarios"]:
        yt, ym, yp = [], [], []
        for seed in world.settings["seeds"]["test"]:
            sc = make_scenario(spec["kind"], seed, world.topo, peak=spec["peak"], ramp_s=spec["ramp_s"])
            recs = [r for r in _telemetry_only(world, sc) if r.service == "vision-inference" and r.site == "edge-east"]
            counts = [r.ingress_arrivals for r in recs]
            for k in range(1, len(recs) - 1):
                qps = recs[k].queue_depth / max(1, recs[k].allocated)
                yt.append(counts[k + 1])
                ym.append(model.predict_count(counts[: k + 1], qps))
                yp.append(persist.predict_count(counts[: k + 1]))
        out[spec["kind"]] = {"model": error_metrics(yt, ym), "persistence": error_metrics(yt, yp)}
        allt += yt; allm += ym; allp += yp
    out["all"] = {"model": error_metrics(allt, allm), "persistence": error_metrics(allt, allp)}
    return out


# --------------------------------------------------------------------------- latency experiment
def run_latency_experiment(world: World, predictor, seeds: list, arms=ARMS, ledger: str = "audit", work_dir: Optional[Path] = None,
                           job_sink: Optional[Callable] = None) -> dict:
    out: dict = {}
    for spec in world.settings["test_scenarios"]:
        runs = {a: [] for a in arms}
        for seed in seeds:
            sc = make_scenario(spec["kind"], seed, world.topo, peak=spec["peak"], ramp_s=spec["ramp_s"])
            for a in arms:
                r = run_arm(world, sc, a, predictor, ledger=ledger, work_dir=work_dir, kind=spec["kind"])
                runs[a].append(r)
                if job_sink:
                    job_sink(r)
        entry = {"scenario": spec, "seeds": list(seeds), "arms": {}, "comparisons": {}}
        for a in arms:
            pooled = [rec for r in runs[a] for rec in r.sim.records]
            verified = [x["verified_ms"] - x["submitted_ms"] for r in runs[a] for x in r.actions if x["origin"] == "closed_loop" and x["state"] == "verified"]
            states: dict = {}
            for r in runs[a]:
                for x in r.actions:
                    states[x["state"]] = states.get(x["state"], 0) + 1
            cost = [sum(r.sim.cost_server_ms.values()) / 1000.0 for r in runs[a]]
            entry["arms"][a] = {**latency_stats(pooled), "intent_to_verified_ms": describe(verified), "action_states": states,
                                "server_seconds_per_run_mean": float(np.mean(cost)), "ledger_all_ok": all(r.ledger_ok for r in runs[a])}
        for base, prop in (("fixed", "reactive"), ("fixed", "predictive"), ("reactive", "predictive")):
            if base in arms and prop in arms:
                pooled = paired_latency([x for r in runs[base] for x in r.sim.records], [x for r in runs[prop] for x in r.sim.records])
                per_seed = [paired_latency(b.sim.records, p.sim.records)["reduction_pct_of_mean"] for b, p in zip(runs[base], runs[prop])]
                ci = bootstrap_mean_ci(per_seed)
                entry["comparisons"][f"{prop}_vs_{base}"] = {"baseline": base, "proposed": prop, **pooled, "per_seed_reduction_pct_of_mean": per_seed,
                                                              "per_seed_mean": float(np.mean([p for p in per_seed if p is not None])) if any(p is not None for p in per_seed) else None,
                                                              "per_seed_bootstrap_ci95": list(ci) if ci else None}
        out[spec["kind"]] = entry
    return out


# --------------------------------------------------------------------------- automation experiment
def run_automation_experiment(world: World, predictor, seeds: list, arms=ARMS, ledger: str = "audit") -> dict:
    lost = world.settings["faults"]["lost_activation_frac"]
    sla = world.settings["orchestrator"]["sla_ms"]
    spec = next(s for s in world.settings["test_scenarios"] if s["kind"] == "ramp_burst")
    acc = {a: {"rows": [], "elig": {}, "deliveries": 0, "cl_states": {}, "per_seed": []} for a in arms}
    for seed in seeds:
        sc = make_scenario("ramp_burst", seed, world.topo, peak=spec["peak"], ramp_s=spec["ramp_s"])
        intents, elig = generate_intents(world, seed, sc.horizon_ms)
        for a in arms:
            r = run_arm(world, sc, a, predictor, intents=list(intents), lost_frac=lost, ledger=ledger, kind="ramp_burst+intents")
            tenant_rows = [x for x in r.actions if x["origin"] == "tenant"]
            m = automation_metrics(tenant_rows, elig, len(intents), sla)
            acc[a]["per_seed"].append({"seed": seed, "numerator": m["numerator"], "denominator": m["denominator"]})
            acc[a]["rows"] += [dict(x, intent_id=f"{seed}:{x['intent_id']}") for x in tenant_rows]
            acc[a]["elig"].update({f"{seed}:{i}": e for i, e in elig.items()})
            acc[a]["deliveries"] += len(intents)
            for x in r.actions:
                if x["origin"] == "closed_loop":
                    acc[a]["cl_states"][x["state"]] = acc[a]["cl_states"].get(x["state"], 0) + 1
    out: dict = {"sla_ms": sla, "lost_activation_frac": lost, "seeds": list(seeds), "arms": {}, "comparisons": {}}
    out["_rows"] = []
    for a in arms:
        m = automation_metrics(acc[a]["rows"], acc[a]["elig"], acc[a]["deliveries"], sla)
        out["arms"][a] = {**m, "closed_loop_action_states": acc[a]["cl_states"], "per_seed": acc[a]["per_seed"]}
        for x in acc[a]["rows"]:
            out["_rows"].append({"arm": a, "intent_id": x["intent_id"], "eligible": acc[a]["elig"][x["intent_id"]]["eligible"], "state": x["state"],
                                 "reason": x["reason"], "submitted_ms": x["submitted_ms"], "verified_ms": x["verified_ms"]})
    for base, prop in (("fixed", "reactive"), ("fixed", "predictive"), ("reactive", "predictive")):
        if base in arms and prop in arms:
            out["comparisons"][f"{prop}_vs_{base}"] = {"baseline": base, "proposed": prop,
                                                       **compare_rates(out["arms"][base]["automation_rate"], out["arms"][prop]["automation_rate"])}
    return out


# --------------------------------------------------------------------------- security detection
def run_detection_experiment(world: World, events: list, labels: dict, splits: dict) -> dict:
    ids = [e["event_id"] for e in events]
    split = np.asarray([splits[i] for i in ids])
    y = np.asarray([int(labels[i]["label"]) for i in ids])
    stats = det.fit_stats([e for e, s in zip(events, split) if s == "train"])  # train partition, no labels
    X = det.featurize(events, world.keyring, stats, world.policy.revision)
    tr, dv, te = split == "train", split == "dev", split == "test"
    model = det.ModelDetector().fit(X[tr], y[tr])
    thr = model.select_threshold(X[dv], y[dv])  # development data only
    rules = det.RuleDetector()
    scores = {"rules": rules.score(X), "logreg": model.score(X), "policy_denial": det.policy_denial_scores(events, world.policy, world.keyring)}
    thresholds = {"rules": rules.threshold, "logreg": thr, "policy_denial": 0.5}
    result = {"split_sizes": {"train": int(tr.sum()), "dev": int(dv.sum()), "test": int(te.sum())},
              "prevalence": {"train": float(y[tr].mean()), "dev": float(y[dv].mean()), "test": float(y[te].mean())},
              "features": det.FEATURES, "threshold_selection": {"detector": "logreg", "threshold": thr, **model.dev_selection},
              "always_benign": det.always_benign_baseline(y[te]), "detectors": {}, "per_attack_kind_recall": {}}
    for name, sc in scores.items():
        pred = (sc >= thresholds[name]).astype(int)
        result["detectors"][name] = {"threshold": thresholds[name], "test": det.confusion_metrics(y[te], pred[te]), "dev": det.confusion_metrics(y[dv], pred[dv])}
        kinds: dict = {}
        for i, e in enumerate(events):
            if te[i] and y[i] == 1:
                k = labels[e["event_id"]]["attack_kind"]
                kinds.setdefault(k, [0, 0])
                kinds[k][1] += 1
                kinds[k][0] += int(pred[i])
        result["per_attack_kind_recall"][name] = {k: {"detected": a, "n": n, "recall": a / n} for k, (a, n) in sorted(kinds.items())}
    result["_predictions"] = [{"event_id": ids[i], "split": str(split[i]), "label": int(y[i]), "attack_kind": labels[ids[i]]["attack_kind"],
                               **{f"score_{n}": float(s[i]) for n, s in scores.items()}, **{f"alert_{n}": int(s[i] >= thresholds[n]) for n, s in scores.items()}}
                              for i in range(len(ids)) if te[i] or dv[i]]
    result["_model"] = model
    result["_sample_alerts"] = det.alert_records([e for e, t in zip(ids, te) if t], model, model.score(X[te]), X[te], limit=5)
    return result


# --------------------------------------------------------------------------- overhead (wall clock, measured)
def _sample_intent(world: World, i: int) -> ProvisioningIntent:
    tok = issue_token(world.issuer, make_claim("city-ops", "city-traffic", 0.0, 3_600_000.0, policy_revision=world.policy.revision))
    return ProvisioningIntent(stable_id("int", "bench", i), "city-traffic", "traffic-analytics", "retain", 0, None, None, 20000.0, 0.0, tok, world.policy.revision)


def run_overhead_experiment(world: World, work_dir: Path) -> dict:
    """Wall-clock Python cost per management operation. These are NOT simulated milliseconds."""
    cfg = world.settings["overhead"]
    n = int(cfg["appends"])
    out: dict = {"unit": "microseconds of Python wall time on this machine", "n_per_variant": n, "hardware": hardware_info()}
    entry = {"action_id": "act-bench", "state": "committed", "payload": "x" * 200}
    variants = {"none": NullLedger(), "audit_log_memory": AuditLog(), "audit_log_file": AuditLog(Path(work_dir) / "overhead.audit.jsonl"),
                "quorum_4_validators_memory": QuorumLedger(4)}
    for name, led in variants.items():
        ts = []
        for i in range(n):
            t0 = time.perf_counter_ns()
            led.append({**entry, "i": i})
            ts.append((time.perf_counter_ns() - t0) / 1000.0)
        out[f"append_{name}"] = describe(ts)
    # macro: whole submit() with the ledger and zero-trust varied (retain intents change no capacity)
    for name, ledger, evaluator in (("permissive_no_ledger", NullLedger(), PermissiveEvaluator()), ("zero_trust_no_ledger", NullLedger(), None),
                                    ("permissive_audit_log", AuditLog(), PermissiveEvaluator()), ("zero_trust_audit_log", AuditLog(), None),
                                    ("zero_trust_quorum", QuorumLedger(4), None)):
        orch = Orchestrator(world.topo, Cluster(world.topo), world.policy, world.keyring, ledger, ":memory:", OrchConfig(), evaluator,
                            allow_benchmark_adapters=True)
        ts = []
        for i in range(max(50, n // 4)):
            t0 = time.perf_counter_ns()
            orch.submit(_sample_intent(world, i), 1000.0)
            ts.append((time.perf_counter_ns() - t0) / 1000.0)
        out[f"submit_{name}"] = describe(ts)
        orch.close()
    reqs = []
    for i in range(int(cfg["evaluations"])):
        it = _sample_intent(world, i)
        reqs.append(Request(it.intent_id, it.token, "reserve", "traffic-analytics", "edge-west", "city-traffic", 3, 1000.0, world.policy.revision))
    for name, fn in (("evaluate_zero_trust", evaluate), ("evaluate_permissive", PermissiveEvaluator())):
        ts = []
        for r in reqs:
            t0 = time.perf_counter_ns()
            fn(r, world.policy, 1000.0, world.keyring)
            ts.append((time.perf_counter_ns() - t0) / 1000.0)
        out[name] = describe(ts)
    return out


# --------------------------------------------------------------------------- provenance
def hardware_info() -> dict:
    import os
    return {"platform": platform.platform(), "processor": platform.processor(), "cpu_count": os.cpu_count(), "python": sys.version.split()[0],
            "gpu_used": False}


def package_versions() -> dict:
    import cryptography, joblib, matplotlib, sklearn, yaml
    from . import __version__
    return {"sszt6g": __version__, "numpy": np.__version__, "scikit-learn": sklearn.__version__, "matplotlib": matplotlib.__version__,
            "cryptography": cryptography.__version__, "joblib": joblib.__version__, "pyyaml": yaml.__version__}


def code_dirty() -> Optional[bool]:
    """True if tracked source, tests, configs or fixtures differ from HEAD (generated docs and runs are ignored)."""
    try:
        r = subprocess.run(["git", "status", "--porcelain", "--", "src", "tests", "configs", "examples", "pyproject.toml"], cwd=repo_root(),
                           capture_output=True, text=True, timeout=10)
        return bool(r.stdout.strip()) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def git_commit() -> Optional[str]:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root(), capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None
