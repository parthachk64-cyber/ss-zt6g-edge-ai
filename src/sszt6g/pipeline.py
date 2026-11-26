"""End-to-end run: train, replay, benchmark, write run artifacts, plots and the results ledger."""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import tempfile
from pathlib import Path
from typing import Optional

import yaml

from . import experiments as ex
from .config import World, build_world, repo_root
from .demo import run_walkthrough
from .fixtures import read_security
from .ledger import AuditLog
from .prediction import Forecaster
from .util import file_sha256, sha256_hex, canonical

JOB_COLS = ["scenario_kind", "seed", "arm", "job_id", "t_ms", "service", "ingress", "site", "status", "reason", "network_ms", "queue_ms",
            "compute_ms", "latency_ms", "deadline_ms", "deadline_missed"]


def _write_csv(path: Path, rows: list, cols: list, gz: bool = False) -> None:
    opener = (lambda p: gzip.open(p, "wt", newline="", encoding="utf-8")) if gz else (lambda p: open(p, "w", newline="", encoding="utf-8"))
    with opener(path) as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def run_pipeline(world: World, run_dir: Path, *, seeds: Optional[list] = None, smoke: bool = False, fixtures_dir: Optional[Path] = None,
                 run_id: str = "demo", log=print) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    seeds = seeds or world.settings["seeds"]["test"]
    if smoke:
        seeds = seeds[:2]
    fixtures_dir = fixtures_dir or repo_root() / "examples" / "fixtures"
    log(f"[1/7] training forecaster on {len(world.settings['seeds']['train'])} earlier scenarios")
    model, train_info = ex.train_forecaster(world)
    model_path = run_dir / "forecaster.joblib"
    model_hash = model.save(model_path)
    log("[2/7] forecaster vs persistence on held-out scenarios")
    forecast_eval = ex.evaluate_forecaster(world, model)

    log(f"[3/7] latency benchmark: 3 scenarios x {len(seeds)} paired seeds x 3 planners")
    job_rows: list = []

    def sink(r: ex.ArmRun) -> None:
        for rec in r.sim.records:
            job_rows.append({"scenario_kind": r.kind, "seed": r.seed, "arm": r.arm, "job_id": rec.job_id, "t_ms": rec.t_ms, "service": rec.service,
                             "ingress": rec.ingress, "site": rec.site, "status": rec.status, "reason": rec.reason, "network_ms": round(rec.net_ms, 3),
                             "queue_ms": round(rec.queue_ms, 3), "compute_ms": round(rec.compute_ms, 3), "latency_ms": round(rec.latency_ms, 3),
                             "deadline_ms": rec.deadline_ms, "deadline_missed": int(rec.deadline_missed)})

    audit_dir = run_dir / "audit"
    audit_dir.mkdir(exist_ok=True)
    latency = ex.run_latency_experiment(world, model, seeds, work_dir=audit_dir, job_sink=sink)
    _write_csv(run_dir / "per_job_latency.csv.gz", job_rows, JOB_COLS, gz=True)

    log("[4/7] automation efficiency benchmark (tenant intents, injected activation faults)")
    automation = ex.run_automation_experiment(world, model, seeds)
    _write_csv(run_dir / "per_intent_disposition.csv", automation.pop("_rows"), ["arm", "intent_id", "eligible", "state", "reason", "submitted_ms", "verified_ms"])

    log("[5/7] security detection on labelled fixtures")
    events, labels, splits = read_security(fixtures_dir)
    detection = ex.run_detection_experiment(world, events, labels, splits)
    preds = detection.pop("_predictions")
    _write_csv(run_dir / "detection_labeled_predictions.csv", preds, list(preds[0]))
    model_det = detection.pop("_model")
    det_hash = model_det.save(run_dir / "detector.joblib")
    (run_dir / "detector_alert_samples.json").write_text(json.dumps(detection.pop("_sample_alerts"), indent=2), encoding="utf-8")

    log("[6/7] overhead (wall clock) and decision walkthrough")
    with tempfile.TemporaryDirectory() as td:
        overhead = ex.run_overhead_experiment(world, Path(td))
    walk = run_walkthrough(world, model, run_dir, world.settings["seeds"]["demo"])

    # a small, verifiable audit log + signed checkpoint kept with the run
    log = log
    cands = sorted(audit_dir.glob("*ramp_burst*predictive.audit.jsonl"))
    sample = cands[0] if cands else None
    ledger_check = None
    if sample is not None:
        al = AuditLog(sample)
        cp = al.checkpoint(world.checkpoint_signer)
        sample.with_name(sample.name.replace(".audit.jsonl", ".checkpoint.json")).write_text(json.dumps(cp, indent=2), encoding="utf-8")
        ledger_check = {"file": str(sample.relative_to(run_dir)), **al.verify(cp, world.checkpoint_signer.public_hex).to_dict()}

    log("[7/7] writing manifest and results")
    results = {"run_id": run_id, "kind": "synthetic_seeded_simulation", "inference": "emulated (no model executed in the simulator)",
               "time_units": "simulated milliseconds unless stated; overhead section is Python wall-clock microseconds",
               "train": train_info, "forecast_eval": forecast_eval, "latency": latency, "automation": automation, "detection": detection,
               "overhead": overhead, "ledger_check": ledger_check,
               "walkthrough_summary": {"duplicate_changed_capacity": walk["duplicate_after_restart"]["capacity_changed_by_duplicate"],
                                       "denial_capacity_unchanged": walk["denial"]["capacity_unchanged"]}}
    (run_dir / "results.json").write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
    (run_dir / "settings.resolved.yaml").write_text(yaml.safe_dump({"settings": world.settings, "topology": world.topo.to_dict(),
                                                                   "policy_revision": world.policy.revision}, sort_keys=False), encoding="utf-8")
    fixture_hashes = {p.name: file_sha256(p) for p in sorted(fixtures_dir.glob("*")) if p.is_file() and p.name != "README.md"}
    manifest = {"run_id": run_id, "created_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": "synthetic_seeded_simulation",
                "smoke": smoke, "seeds_test": seeds, "seeds_train": world.settings["seeds"]["train"], "git_commit": ex.git_commit(), "code_dirty": ex.code_dirty(),
                "package_versions": ex.package_versions(), "hardware": ex.hardware_info(), "source_hashes": {"fixtures": fixture_hashes},
                "artifact_hashes": {"forecaster.joblib": model_hash, "detector.joblib": det_hash, "per_job_latency.csv.gz": file_sha256(run_dir / "per_job_latency.csv.gz")},
                "settings_hash": sha256_hex(canonical(world.settings)), "notes": ["No GPU, carrier, radio unit, cloud control plane or physical device was used.",
                                                                                 "Hardware measurements exist only in results.json 'overhead' and are Python wall-clock on this machine."]}
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return results
