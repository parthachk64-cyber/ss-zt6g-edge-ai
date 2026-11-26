"""Command line: demo, data-check, train, replay, benchmark, verify-ledger, compare, report."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from . import experiments as ex
from . import plots
from .config import build_world, repo_root
from .errors import DataError, SszError
from .fixtures import read_arrivals_csv, read_intents, read_security
from .ledger import AuditLog, QuorumLedger, verify_entries
from .metrics import latency_stats
from .pipeline import run_pipeline
from .prediction import Forecaster
from .report import write_report
from .simulator import Cluster
from .workload import Job, Scenario, ProfileSpec


def _world(args):
    return build_world(Path(args.config_dir) if getattr(args, "config_dir", None) else None)


def cmd_demo(args) -> int:
    w = _world(args)
    root = repo_root()
    run_dir = Path(args.out or root / "runs" / args.run_id)
    docs = Path(args.docs or root / "docs")
    res = run_pipeline(w, run_dir, smoke=args.smoke, run_id=args.run_id)
    figs = plots.make_all(run_dir, docs / "figures")
    write_report(run_dir, docs)
    print(f"run id {args.run_id}: artifacts in {run_dir}")
    print(f"figures: {', '.join(figs)}")
    print(f"results ledger: {docs / 'results.md'}; trace: {run_dir / 'decision_trace.md'}")
    return 0


def cmd_data_check(args) -> int:
    p = Path(args.path)
    try:
        if args.kind == "arrivals":
            jobs = read_arrivals_csv(p)
            print(f"OK arrivals: {len(jobs)} rows, t_ms {jobs[0].t_ms if jobs else None}..{jobs[-1].t_ms if jobs else None}")
        elif args.kind == "security":
            ev, lab, sp = read_security(p)
            missing = [e["event_id"] for e in ev if e["event_id"] not in lab or e["event_id"] not in sp]
            if missing:
                raise DataError(f"{len(missing)} events lack a label or split row")
            leaked = [k for k in ev[0] if any(t in k for t in ("label", "attack", "scenario", "outcome", "episode", "split"))]
            if leaked:
                raise DataError(f"detector-input file carries forbidden fields: {leaked}")
            print(f"OK security: {len(ev)} events, labels and splits joined, no label-like field in the input file")
        else:
            it, el = read_intents(p)
            print(f"OK intents: {len(it)} deliveries, {len(el)} unique, {sum(1 for e in el.values() if e['eligible'])} eligible")
    except (DataError, OSError, KeyError, json.JSONDecodeError) as e:
        print(f"DATA ERROR: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_build_fixtures(args) -> int:
    from .fixtures import build_fixtures
    from .workload import make_scenario
    w = _world(args)
    spec = next(x for x in w.settings["test_scenarios"] if x["kind"] == "ramp_burst")
    sc = make_scenario("ramp_burst", w.settings["seeds"]["demo"], w.topo, peak=spec["peak"], ramp_s=spec["ramp_s"])
    out = Path(args.out or repo_root() / "examples" / "fixtures")
    reg = build_fixtures(w, out, sc)
    reg_path = Path(args.registry or repo_root() / "datasets" / "registry.json")
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(json.dumps(reg, indent=2, sort_keys=True), encoding="utf-8")
    print(f"fixtures in {out}; registry {reg_path}")
    return 0


def cmd_train(args) -> int:
    w = _world(args)
    model, info = ex.train_forecaster(w)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"model": str(out), "sha256": model.save(out), **info, "manifest": model.train_manifest}, indent=2))
    return 0


def cmd_replay(args) -> int:
    w = _world(args)
    jobs = read_arrivals_csv(Path(args.arrivals))
    horizon = int(max(j.t_ms for j in jobs)) + 1000
    sc = Scenario("replay", w.topo, [], horizon, 0)
    predictor = Forecaster.load(args.model) if args.model else None
    if args.planner == "predictive" and predictor is None:
        print("predictive planner needs --model (run `sszt6g train --out m.joblib`)", file=sys.stderr)
        return 2
    import sszt6g.simulator as sim_mod
    from sszt6g.experiments import make_ledger
    from sszt6g.orchestrator import ClosedLoopController, OrchConfig, Orchestrator
    from sszt6g.planners import PlannerConfig, make_planner
    cl = Cluster(w.topo)
    orch = Orchestrator(w.topo, cl, w.policy, w.keyring, make_ledger("audit", Path(args.out), "replay") if args.out else AuditLog(), ":memory:", OrchConfig())
    if args.out:
        Path(args.out).mkdir(parents=True, exist_ok=True)
    ctl = ClosedLoopController(orch, make_planner(args.planner, PlannerConfig(**w.settings["planner"])), predictor, w.tokens, w.policy.revision)
    res = sim_mod.Simulator(w.topo, cl, horizon, ctl).run(jobs)
    stats = latency_stats(res.records)
    print(json.dumps({"planner": args.planner, "jobs": stats["jobs_total"], "completed": stats["jobs_completed"], "dropped": stats["jobs_dropped"],
                      "denied": stats["jobs_denied"], "latency_ms": stats["latency_ms"], "actions": len(orch.store.rows())}, indent=2))
    if args.out:
        with open(Path(args.out) / "replay_jobs.csv", "w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=list(res.records[0].to_row()))
            wr.writeheader()
            wr.writerows(r.to_row() for r in res.records)
    return 0


def cmd_benchmark(args) -> int:
    w = _world(args)
    run_dir = Path(args.out or repo_root() / "runs" / args.run_id)
    res = run_pipeline(w, run_dir, smoke=args.smoke, run_id=args.run_id)
    for kind, e in res["latency"].items():
        for name, c in e["comparisons"].items():
            print(f"{kind:12s} {name:24s} comparable={c['comparable_jobs']:6d} reduction_of_mean={c['reduction_pct_of_mean']}")
    return 0


def cmd_verify_ledger(args) -> int:
    if args.quorum_dir:
        rep = QuorumLedger(4, directory=args.quorum_dir).verify()
    else:
        entries = [json.loads(line) for line in Path(args.path).read_text(encoding="utf-8").splitlines() if line.strip()]
        cp = json.loads(Path(args.checkpoint).read_text(encoding="utf-8")) if args.checkpoint else None
        pub = build_world().checkpoint_signer.public_hex if cp else None
        rep = verify_entries(entries, cp, pub)
    print(json.dumps(rep.to_dict(), indent=2))
    return 0 if rep.ok else 1


def cmd_compare(args) -> int:
    w = _world(args)
    model, _ = ex.train_forecaster(w)
    seeds = w.settings["seeds"]["test"][: args.seeds]
    lat = ex.run_latency_experiment(w, model, seeds)
    for kind, e in lat.items():
        print(f"\n[{kind}] seeds={seeds}")
        for a, s in e["arms"].items():
            print(f"  {a:10s} completed={s['jobs_completed']:5d} dropped={s['jobs_dropped']:4d} mean={s['latency_ms']['mean']:.1f} p95={s['latency_ms']['p95']:.1f} ms")
        for n, c in e["comparisons"].items():
            print(f"  {n:24s} reduction_of_mean={c['reduction_pct_of_mean']}")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        oh = ex.run_overhead_experiment(w, Path(td))
    for k in ("append_none", "append_audit_log_memory", "append_quorum_4_validators_memory", "evaluate_zero_trust", "evaluate_permissive"):
        print(f"{k:38s} median {oh[k]['median']:.1f} us  p95 {oh[k]['p95']:.1f} us")
    return 0


def cmd_report(args) -> int:
    run_dir = Path(args.run)
    docs = Path(args.docs or repo_root() / "docs")
    plots.make_all(run_dir, docs / "figures")
    print(write_report(run_dir, docs))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sszt6g", description="Offline SS-ZT6G companion: simulator, planners, zero-trust checks, audit ledger.")
    p.add_argument("--config-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="train, walk through one decision, benchmark, plot, write the results ledger")
    d.add_argument("--run-id", default="demo")
    d.add_argument("--out")
    d.add_argument("--docs")
    d.add_argument("--smoke", action="store_true", help="2 seeds instead of 8")
    d.set_defaults(fn=cmd_demo)
    c = sub.add_parser("data-check", help="validate a file against the import contract")
    c.add_argument("--kind", choices=["arrivals", "security", "intents"], required=True)
    c.add_argument("--path", required=True)
    c.set_defaults(fn=cmd_data_check)
    bf = sub.add_parser("build-fixtures", help="regenerate the bundled synthetic fixtures and the dataset registry")
    bf.add_argument("--out")
    bf.add_argument("--registry")
    bf.set_defaults(fn=cmd_build_fixtures)
    t = sub.add_parser("train", help="fit the demand forecaster on earlier scenarios")
    t.add_argument("--out", required=True)
    t.set_defaults(fn=cmd_train)
    r = sub.add_parser("replay", help="replay an arrivals CSV through one planner")
    r.add_argument("--arrivals", required=True)
    r.add_argument("--planner", choices=["fixed", "reactive", "predictive"], default="reactive")
    r.add_argument("--model")
    r.add_argument("--out")
    r.set_defaults(fn=cmd_replay)
    b = sub.add_parser("benchmark", help="paired-seed benchmark and artifacts")
    b.add_argument("--run-id", default="bench")
    b.add_argument("--out")
    b.add_argument("--smoke", action="store_true")
    b.set_defaults(fn=cmd_benchmark)
    v = sub.add_parser("verify-ledger", help="verify an audit log (with checkpoint) or a quorum ledger directory")
    v.add_argument("--path")
    v.add_argument("--checkpoint")
    v.add_argument("--quorum-dir")
    v.set_defaults(fn=cmd_verify_ledger)
    k = sub.add_parser("compare", help="planner comparison and overhead ablations, printed")
    k.add_argument("--seeds", type=int, default=3)
    k.set_defaults(fn=cmd_compare)
    rp = sub.add_parser("report", help="regenerate plots and docs/results.md from a run directory")
    rp.add_argument("--run", required=True)
    rp.add_argument("--docs")
    rp.set_defaults(fn=cmd_report)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except SszError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
