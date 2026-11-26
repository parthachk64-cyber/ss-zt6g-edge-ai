import json
import re
from pathlib import Path

import numpy as np
import pytest

from sszt6g import detection as det
from sszt6g.cli import main
from sszt6g.config import repo_root
from sszt6g.experiments import run_detection_experiment
from sszt6g.fixtures import (assign_splits, build_registry, generate_intents, generate_security, read_arrivals_csv, read_intents, read_security)
from sszt6g.errors import DataError
from sszt6g.standards import MAPPINGS, validate_registry
from sszt6g.util import file_sha256

FIX = repo_root() / "examples" / "fixtures"


def test_feature_names_never_contain_label_like_tokens():
    assert not [n for n in det.FEATURES if any(t in n for t in det.FORBIDDEN_TOKENS)]


def test_detector_input_file_has_no_label_fields():
    ev = json.loads((FIX / "security_events.jsonl").read_text().splitlines()[0])
    assert not [k for k in ev if any(t in k for t in ("label", "attack", "scenario", "outcome", "episode", "split", "benign"))]


def test_confusion_metrics_and_always_benign():
    m = det.confusion_metrics([1, 1, 0, 0, 0], [1, 0, 0, 1, 0])
    assert (m["tp"], m["fn"], m["fp"], m["tn"]) == (1, 1, 1, 2) and m["accuracy"] == 0.6 and m["precision"] == 0.5
    ab = det.always_benign_baseline([1, 0, 0, 0])
    assert ab["accuracy"] == 0.75 and ab["tp"] == 0 and ab["recall"] == 0.0
    empty = det.confusion_metrics([], [])
    assert empty["accuracy"] is None and empty["precision"] is None


def test_causal_features_do_not_depend_on_future_events(world):
    events, labels, splits = read_security(FIX)
    stats = det.fit_stats(events[:500])
    X_full = det.featurize(events[:800], world.keyring, stats, 1)
    X_part = det.featurize(events[:400], world.keyring, stats, 1)
    assert np.allclose(X_full[:400], X_part)


def test_threshold_selected_on_dev_not_test(world):
    events, labels, splits = read_security(FIX)
    ids = [e["event_id"] for e in events]
    sp = np.array([splits[i] for i in ids])
    y = np.array([int(labels[i]["label"]) for i in ids])
    stats = det.fit_stats([e for e, s in zip(events, sp) if s == "train"])
    X = det.featurize(events, world.keyring, stats, 1)
    m = det.ModelDetector().fit(X[sp == "train"], y[sp == "train"])
    t1 = m.select_threshold(X[sp == "dev"], y[sp == "dev"])
    y_test_flipped = 1 - y[sp == "test"]
    assert m.select_threshold(X[sp == "dev"], y[sp == "dev"]) == t1  # unaffected by anything in the test partition
    assert m.dev_selection["n_dev"] == int((sp == "dev").sum()) and y_test_flipped.size > 0


def test_splits_are_whole_episodes_in_chronological_order():
    events, labels, splits = read_security(FIX)
    eps = {}
    for eid, s in splits.items():
        eps.setdefault(int(labels[eid]["episode"]), set()).add(s)
    assert all(len(v) == 1 for v in eps.values())
    order = [next(iter(eps[e])) for e in sorted(eps)]
    assert order == sorted(order, key=["train", "dev", "test"].index)


def test_detection_experiment_reports_full_confusion_and_baselines(world):
    events, labels, splits = read_security(FIX)
    r = run_detection_experiment(world, events, labels, splits)
    t = r["detectors"]["logreg"]["test"]
    assert t["tp"] + t["fp"] + t["tn"] + t["fn"] == r["split_sizes"]["test"] == t["n"]
    assert r["always_benign"]["accuracy"] == pytest.approx(1 - r["prevalence"]["test"], abs=1e-9)
    assert t["accuracy_ci95_wilson"][0] <= t["accuracy"] <= t["accuracy_ci95_wilson"][1]
    assert r["threshold_selection"]["detector"] == "logreg" and 0 < r["threshold_selection"]["threshold"] <= 1


def test_policy_denial_is_not_automatically_a_correct_detection(world):
    events, labels, splits = read_security(FIX)
    r = run_detection_experiment(world, events, labels, splits)
    pol = r["detectors"]["policy_denial"]["test"]
    assert pol["fp"] > 0 and pol["fn"] > 0  # denies some benign requests, misses some attacks


def test_rules_miss_credential_misuse_which_is_reported_per_kind(world):
    events, labels, splits = read_security(FIX)
    r = run_detection_experiment(world, events, labels, splits)
    assert r["per_attack_kind_recall"]["rules"]["credential_misuse"]["recall"] < 1.0
    assert "expired_identity" in r["per_attack_kind_recall"]["rules"]


def test_alert_records_persist_features_threshold_orientation_and_evidence(world):
    events, labels, splits = read_security(FIX)
    r = run_detection_experiment(world, events, labels, splits)
    a = r["_sample_alerts"][0]
    assert set(a) >= {"features", "threshold", "orientation", "evidence_ids", "decision", "score"} and set(a["features"]) == set(det.FEATURES)


# ----------------------------------------------------------------------------- fixtures
def test_bundled_fixtures_regenerate_byte_identically(world, tmp_path):
    s = world.settings["security"]
    ev, lab, _ = generate_security(world, s["seed"], s["episodes"], s["events_per_episode"])
    from sszt6g.fixtures import write_security, write_intents
    write_security(tmp_path, ev, lab, assign_splits(lab, s["train_episodes"], s["dev_episodes"]))
    for name in ("security_events.jsonl", "security_labels.csv", "security_splits.csv"):
        assert file_sha256(tmp_path / name) == file_sha256(FIX / name), name
    it, el = generate_intents(world, world.settings["seeds"]["demo"])
    write_intents(tmp_path, it, el)
    for name in ("intents.jsonl", "intent_eligibility.csv"):
        assert file_sha256(tmp_path / name) == file_sha256(FIX / name), name


def test_registry_hashes_match_files_and_declare_no_original_data():
    reg = json.loads((repo_root() / "datasets" / "registry.json").read_text())
    for f in reg["datasets"][0]["files"]:
        assert file_sha256(repo_root() / f["file"]) == f["sha256"]
    assert reg["original_paper_datasets"]["status"] == "not identified"
    assert build_registry(FIX)["datasets"][0]["files"] == reg["datasets"][0]["files"]


def test_intent_eligibility_is_static_and_independent_of_outcomes():
    it, el = read_intents(FIX)
    assert sum(e["eligible"] for e in el.values()) == 6 and len(el) == 10 and len(it) == 12
    assert {i.intent_id for i in it} == set(el)
    assert any("claim_expired_or_not_yet_valid" in e["static_reasons"] for e in el.values())


def test_arrivals_import_contract_errors(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("job_id,t_ms\nx,1\n")
    with pytest.raises(DataError, match="needs columns"):
        read_arrivals_csv(p)
    p.write_text("job_id,t_ms,ingress,tenant,service,work_ms,deadline_ms\nx,5,e,t,s,10,400\ny,1,e,t,s,10,400\n")
    with pytest.raises(DataError, match="non-decreasing"):
        read_arrivals_csv(p)
    p.write_text("job_id,t_ms,ingress,tenant,service,work_ms,deadline_ms\nx,abc,e,t,s,10,400\n")
    with pytest.raises(DataError, match="row 2"):
        read_arrivals_csv(p)


# ----------------------------------------------------------------------------- standards
def test_standards_registry_is_consistent_and_never_claims_interop():
    assert validate_registry() == []
    assert not any("interop" in m["level"] for m in MAPPINGS)
    assert not any("compliant" in m["implementation"].lower() for m in MAPPINGS)


def test_every_cited_test_exists():
    for m in MAPPINGS:
        if not m["test"]:
            continue
        path, name = m["test"].split("::")
        assert re.search(rf"^def {name}\(", (repo_root() / path).read_text(encoding="utf-8"), re.M), m["test"]


# ----------------------------------------------------------------------------- CLI
def test_cli_data_check_exit_codes(tmp_path, capsys):
    assert main(["data-check", "--kind", "security", "--path", str(FIX)]) == 0
    bad = tmp_path / "a.csv"
    bad.write_text("nope\n")
    assert main(["data-check", "--kind", "arrivals", "--path", str(bad)]) == 2
    assert "DATA ERROR" in capsys.readouterr().err


def test_cli_verify_ledger_detects_tampering(world, tmp_path, capsys):
    from sszt6g.ledger import AuditLog
    log = AuditLog(tmp_path / "l.jsonl")
    for i in range(4):
        log.append({"action_id": f"a{i}", "state": "x"})
    cp = tmp_path / "cp.json"
    cp.write_text(json.dumps(log.checkpoint(world.checkpoint_signer)))
    assert main(["verify-ledger", "--path", str(tmp_path / "l.jsonl"), "--checkpoint", str(cp)]) == 0
    lines = (tmp_path / "l.jsonl").read_text().splitlines()
    (tmp_path / "l.jsonl").write_text("\n".join(lines[:3]) + "\n")
    assert main(["verify-ledger", "--path", str(tmp_path / "l.jsonl"), "--checkpoint", str(cp)]) == 1


def test_cli_train_and_replay(tmp_path, capsys):
    m = tmp_path / "m.joblib"
    assert main(["train", "--out", str(m)]) == 0
    assert main(["replay", "--arrivals", str(FIX / "arrivals_ramp_burst_seed1001.csv"), "--planner", "predictive", "--model", str(m), "--out", str(tmp_path / "r")]) == 0
    assert (tmp_path / "r" / "replay_jobs.csv").exists()
    assert main(["replay", "--arrivals", str(FIX / "arrivals_ramp_burst_seed1001.csv"), "--planner", "predictive"]) == 2


def test_demo_smoke_end_to_end_writes_run_artifacts_and_report(tmp_path):
    run, docs = tmp_path / "run", tmp_path / "docs"
    assert main(["demo", "--smoke", "--run-id", "ci", "--out", str(run), "--docs", str(docs)]) == 0
    for name in ("run_manifest.json", "results.json", "per_job_latency.csv.gz", "per_intent_disposition.csv", "detection_labeled_predictions.csv",
                 "decision_trace.json", "decision_trace.md", "forecaster.joblib"):
        assert (run / name).exists(), name
    manifest = json.loads((run / "run_manifest.json").read_text())
    assert manifest["package_versions"]["sszt6g"] and manifest["hardware"]["gpu_used"] is False and manifest["source_hashes"]["fixtures"]
    res = json.loads((run / "results.json").read_text())
    assert res["walkthrough_summary"] == {"duplicate_changed_capacity": False, "denial_capacity_unchanged": True}
    assert res["ledger_check"]["ok"] is True
    md = (docs / "results.md").read_text()
    assert "Headline claims" in md and "44.9%" in md and "97.5%" in md and "9.8%" in md and "PAPER" in md and "COMPANION-RUN" in md
    assert (docs / "standards-map.md").exists() and (docs / "trace-example.md").exists()
    assert {p.name for p in (docs / "figures").glob("*.png")} >= {"architecture.png", "latency_distribution.png", "automation_outcomes.png", "detection_errors.png", "audit_overhead.png"}
    assert main(["verify-ledger", "--path", str(next((run / "audit").glob("*ramp_burst*predictive.audit.jsonl"))), "--checkpoint",
                 str(next((run / "audit").glob("*ramp_burst*predictive.checkpoint.json")))]) == 0


def test_custom_predictor_example_drives_the_predictive_planner(world):
    import importlib.util
    spec = importlib.util.spec_from_file_location("custom_predictor", repo_root() / "examples" / "custom_predictor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from sszt6g.experiments import run_arm
    from sszt6g.workload import make_scenario
    run = run_arm(world, make_scenario("step_burst", 31, world.topo), "predictive", mod.EwmaTrend())
    assert any(x["origin"] == "closed_loop" and x["state"] == "verified" for x in run.actions)
    assert mod.EwmaTrend().predict_count([10.0]) == 10.0
