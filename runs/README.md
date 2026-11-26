# runs/

`runs/<id>/` holds the artifacts of one execution: `run_manifest.json` (run id, seeds, resolved settings hash, git commit, package versions, hardware, source and artifact hashes), `results.json`, `settings.resolved.yaml`, `per_job_latency.csv.gz`, `per_intent_disposition.csv`, `detection_labeled_predictions.csv`, `detector_alert_samples.json`, `decision_trace.json` / `.md`, and `audit/` (hash-chained logs with a signed checkpoint, verifiable with `sszt6g verify-ledger`).

The committed `runs/demo/` is the run that `docs/results.md` was generated from. Model files (`*.joblib`) are regenerated, not committed. Everything here is synthetic and simulated unless a section says "wall clock".
