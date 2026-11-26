"""Render docs/results.md (the results ledger) and docs/standards-map.md from run artifacts and the registry."""
from __future__ import annotations

import json
from pathlib import Path

from .standards import MAPPINGS

# Values printed in the paper (supplied PDF, IEEE Commun. Stand. Mag., DOI 10.1109/MCOMSTD.2026.3660159).
PAPER = {"latency_reduction_pct": 44.9, "automation_increase_pct": 9.8, "detection_accuracy_pct": 97.5, "automation_efficiency": 94.7,
         "latency_ss": 3.8, "latency_ao6g": 6.9, "latency_bea": 11.4, "acc_ao6g": 89.1, "acc_bea": 73.3}


def f(x, d=1):
    return "n/a" if x is None else f"{x:.{d}f}"


def pct(x, d=1):
    return "n/a" if x is None else f"{100 * x:.{d}f}%"


def _latency_section(res: dict) -> list:
    L = ["## B. Latency (companion, simulated milliseconds)", "",
         "Application latency = job completion minus arrival (network + queue + compute, simulated). Reduction = 100 x (baseline - proposed) / baseline "
         "on jobs **completed in both arms** (identical arrivals, paired seeds). Drops and deadline misses are shown because a system can look faster by dropping slow jobs.", ""]
    for kind, e in res["latency"].items():
        L += [f"### {kind} (peak {e['scenario']['peak']} jobs/s at the ingress site; {len(e['seeds'])} paired seeds)", "",
              "| Arm | completed | dropped | completion rate | late | miss rate incl. dropped | mean | median | p95 | server-seconds/run | intent→verified (mean) |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for a, s in e["arms"].items():
            lt = s["latency_ms"]
            L.append(f"| {a} | {s['jobs_completed']} | {s['jobs_dropped']} | {pct(s['completion_rate'])} | {s['late_completed']} | {pct(s['deadline_miss_rate_incl_dropped'])} | "
                     f"{f(lt['mean'])} | {f(lt['median'])} | {f(lt['p95'])} | {f(s['server_seconds_per_run_mean'], 0)} | {f(s['intent_to_verified_ms']['mean'], 0) if s['intent_to_verified_ms']['n'] else 'n/a'} |")
        L += ["", "| Comparison (baseline named) | comparable jobs | baseline mean | proposed mean | reduction of mean | reduction of p95 | per-seed mean reduction [bootstrap 95% CI] |", "|---|---:|---:|---:|---:|---:|---|"]
        for n, c in e["comparisons"].items():
            ci = c["per_seed_bootstrap_ci95"]
            L.append(f"| {c['proposed']} vs **{c['baseline']}** | {c['comparable_jobs']} | {f(c['baseline_mean_ms'])} | {f(c['proposed_mean_ms'])} | {f(c['reduction_pct_of_mean'])}% | {f(c['reduction_pct_of_p95'])}% | "
                     f"{f(c['per_seed_mean'])}% {('[' + f(ci[0]) + ', ' + f(ci[1]) + ']') if ci else ''} |")
        L.append("")
    return L


def _automation_section(res: dict) -> list:
    a = res["automation"]
    L = ["## C. Automation (companion)", "",
         f"Definition: unique policy-eligible intents completed **and verified automatically within SLA ({a['sla_ms'] / 1000:.0f} s simulated)** / all unique policy-eligible intents. "
         f"Eligibility is fixed from static policy before any controller runs. Duplicates count once. Faults: {a['lost_activation_frac']:.0%} of activations are silently lost (same actions lost in every arm). Seeds: {len(a['seeds'])}.", "",
         "| Arm | eligible (denominator) | verified in SLA (numerator) | rate | verified late | policy-denied | capacity failures | rolled back + escalated | other failures | unfinished | ineligible denied / not denied |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for n, s in a["arms"].items():
        o = s["eligible_outcomes"]
        L.append(f"| {n} | {s['denominator']} | {s['numerator']} | {pct(s['automation_rate'])} | {o['verified_late']} | {o['denied_by_policy']} | {o['failed_capacity']} | {o['rolled_back_escalated']} | {o['failed_other']} | {o['unfinished']} | {s['ineligible_denied']} / {s['ineligible_not_denied']} |")
    L += ["", "| Comparison | baseline rate | proposed rate | relative increase | percentage-point change |", "|---|---:|---:|---:|---:|"]
    for n, c in a["comparisons"].items():
        L.append(f"| {c['proposed']} vs **{c['baseline']}** | {pct(a['arms'][c['baseline']]['automation_rate'])} | {pct(a['arms'][c['proposed']]['automation_rate'])} | {f(c['relative_increase_pct'])}% | {f(c['percentage_point_change'])} pp |")
    L.append("")
    return L


def _detection_section(res: dict) -> list:
    d = res["detection"]
    L = ["## D. Security detection (companion, held-out synthetic episodes)", "",
         f"Test n = {d['detectors']['rules']['test']['n']}, attack prevalence {pct(d['prevalence']['test'])}. Threshold for `logreg` chosen on development data "
         f"(F1-maximising, dev n = {d['split_sizes']['dev']}): {d['threshold_selection']['threshold']:.4f}. Accuracy = (TP+TN)/N with a Wilson 95% interval. "
         "`policy_denial` scores the zero-trust authorization decision as if it were a detector, to show a denial is not automatically a detection.", "",
         "| Detector | accuracy [95% CI] | TP | FP | TN | FN | precision | recall | F1 |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    ab = d["always_benign"]
    L.append(f"| always benign | {pct(ab['accuracy'], 2)} | {ab['tp']} | {ab['fp']} | {ab['tn']} | {ab['fn']} | n/a | {pct(ab['recall'])} | n/a |")
    for n, s in d["detectors"].items():
        t = s["test"]
        ci = t["accuracy_ci95_wilson"]
        L.append(f"| {n} | {pct(t['accuracy'], 2)} [{pct(ci[0], 1)}, {pct(ci[1], 1)}] | {t['tp']} | {t['fp']} | {t['tn']} | {t['fn']} | {pct(t['precision'])} | {pct(t['recall'])} | {f(t['f1'], 3)} |")
    L += ["", "Recall by attack kind (test):", "", "| Kind | " + " | ".join(d["detectors"]) + " |", "|---|" + "---:|" * len(d["detectors"])]
    kinds = sorted({k for v in d["per_attack_kind_recall"].values() for k in v})
    for k in kinds:
        L.append(f"| {k} | " + " | ".join(pct(d["per_attack_kind_recall"][n].get(k, {}).get("recall")) + f" ({d['per_attack_kind_recall'][n].get(k, {}).get('detected', 0)}/{d['per_attack_kind_recall'][n].get(k, {}).get('n', 0)})" for n in d["detectors"]) + " |")
    L.append("")
    return L


def _overhead_section(res: dict) -> list:
    o = res["overhead"]
    L = ["## E. Overhead ablations (Python wall clock on this machine, microseconds)", "",
         f"Hardware: {o['hardware']['platform']}, {o['hardware']['cpu_count']} logical CPUs, Python {o['hardware']['python']}, no GPU. These are **measured**, not simulated milliseconds. "
         "The permissive evaluator and the null ledger are benchmark-only adapters, used here and nowhere else. "
         "`submit_*` is dominated by durable SQLite state commits (several per action), so the zero-trust and ledger deltas are small relative to it. "
         "`append_audit_log_file` reopens the file on every append (cost of the I/O), so it is not comparable with the in-memory rows.", "", "| Operation | n | median | p95 |", "|---|---:|---:|---:|"]
    for k in [k for k in o if k.startswith(("append_", "evaluate_", "submit_"))]:
        L.append(f"| {k} | {o[k]['n']} | {f(o[k]['median'], 1)} | {f(o[k]['p95'], 1)} |")
    L.append("")
    return L


def _forecast_section(res: dict) -> list:
    fe = res["forecast_eval"]
    L = ["## F. Forecaster vs persistence (held-out scenarios, one-step-ahead arrivals per 5 s interval)", "",
         f"Trained on {res['train']['train_scenarios']} earlier scenarios ({res['train']['rows']} rows); evaluated on different seeds and a peak/ramp not used in training. No tuning on the test data (alpha fixed in `configs/settings.yaml`).", "",
         "| Scenario | model MAE | persistence MAE | model RMSE | persistence RMSE | n |", "|---|---:|---:|---:|---:|---:|"]
    for k, v in fe.items():
        L.append(f"| {k} | {f(v['model']['mae'], 2)} | {f(v['persistence']['mae'], 2)} | {f(v['model']['rmse'], 2)} | {f(v['persistence']['rmse'], 2)} | {v['model']['n']} |")
    L.append("")
    return L


def _headline(res: dict) -> list:
    lat = res["latency"]["ramp_burst"]["comparisons"]
    pr, pf = lat["predictive_vs_reactive"], lat["predictive_vs_fixed"]
    a, d = res["automation"], res["detection"]
    ar, ap = a["arms"]["reactive"]["automation_rate"], a["arms"]["predictive"]["automation_rate"]
    lg, ab = d["detectors"]["logreg"]["test"], d["always_benign"]
    return [
        "## A. Headline claims: where each number comes from", "",
        "Three labelled origins are never mixed: **PAPER** (printed in the supplied article, not reproduced), **COMPANION-RUN** (computed by this repository's code on seeded synthetic workloads, simulated time), **HARDWARE** (Python wall clock; section E only). "
        "No value measured on real telecom hardware exists in this repository.", "",
        "| # | Headline (paper) | PAPER value, comparator, source | What the paper does not say | COMPANION-RUN definition, denominator, comparator | COMPANION-RUN value | Data source | Execution status |",
        "|---|---|---|---|---|---|---|---|",
        f"| 1 | 44.9% lower latency | 44.9% (abstract, Conclusion). The text prints a median of 3.8 ms for SS-ZT6G against a *mean* of 6.9 ms for AO6G and 11.4 ms for BEA; (6.9-3.8)/6.9 = 44.9%, so the figure is consistent with that pair, but the paper does not state this derivation. Table 1 prints different values (3.9 ms vs 8.4 ms baseline = 53.6%). | latency endpoints, workload, why the text and Table 1 disagree, a mean-versus-median mix | completion minus arrival; comparable completed jobs (n = {pr['comparable_jobs']} for predictive vs reactive, {pf['comparable_jobs']} vs fixed); baseline named | predictive vs reactive: **{f(pr['reduction_pct_of_mean'])}%** of mean, {f(pr['reduction_pct_of_p95'])}% of p95. predictive vs fixed placement: **{f(pf['reduction_pct_of_mean'])}%** (drop rate fixed {pct(res['latency']['ramp_burst']['arms']['fixed']['drop_rate'])}). Scenario `ramp_burst`, {len(res['latency']['ramp_burst']['seeds'])} paired seeds | seeded synthetic arrivals (`sszt6g.workload`) | PAPER: reported, not reproducible (no code or data). COMPANION: executed here, simulated |",
        f"| 2 | 9.8% increase in automation efficiency | 9.8% (abstract, Conclusion); automation efficiency 94.7 (text). | whether 9.8 is relative or percentage points, the baseline value, the denominator. If relative the baseline would be 94.7/1.098 = 86.2; if points, 84.9. Both are this repository's arithmetic; neither is stated by the paper | unique policy-eligible intents verified in SLA / unique eligible intents ({a['arms']['predictive']['numerator']} / {a['arms']['predictive']['denominator']} predictive) | reactive {pct(ar)} → predictive {pct(ap)}: relative {f(a['comparisons']['predictive_vs_reactive']['relative_increase_pct'])}%, {f(a['comparisons']['predictive_vs_reactive']['percentage_point_change'])} pp. fixed {pct(a['arms']['fixed']['automation_rate'])} | seeded intents + eligibility table (`examples/fixtures`) | PAPER: reported, definition unknown. COMPANION: executed, simulated |",
        f"| 3 | 97.5% security detection accuracy | 97.5% vs AO6G 89.1 vs BEA 73.3 (text), under DDoS and data tampering. The metric list defines \"Security Breach Rate\" as the share of intrusion attempts detected or mitigated, which is recall-like, not accuracy. | sample count, class balance, ground truth, split, threshold | (TP+TN)/N on {lg['n']} held-out events, prevalence {pct(d['prevalence']['test'])}; comparators: always-benign {pct(ab['accuracy'], 2)}, rules {pct(d['detectors']['rules']['test']['accuracy'], 2)}, policy denial {pct(d['detectors']['policy_denial']['test']['accuracy'], 2)} | logreg **{pct(lg['accuracy'], 2)}** (FP {lg['fp']}, FN {lg['fn']}), recall {pct(lg['recall'])}, precision {pct(lg['precision'])} | labelled synthetic management-plane events (`examples/fixtures`) | PAPER: reported, not reproducible. COMPANION: executed; the value was **not** tuned toward 97.5% |", ""]


def render_results(res: dict, manifest: dict) -> str:
    L = ["# Results ledger", "",
         f"Run `{manifest['run_id']}`, generated {manifest['created_utc']}, code commit `{manifest.get('git_commit')}` (working tree of code {'dirty' if manifest.get('code_dirty') else 'clean'} at run time). Regenerate with `sszt6g demo` (about a minute, CPU only). "
         "All tables below are produced from `runs/<id>/results.json`; per-job latencies, per-intent dispositions, labelled predictions and the manifest sit beside it.", "",
         "> **Read this first.** The paper is a simulation study whose code, data, labels and splits are not public and which does not define its comparators or endpoints. "
         "This repository therefore does **not reproduce** the paper's numbers. It runs an independent, seeded companion and reports what that companion measures, "
         "including results where the predictive planner is no better than a reactive one.", ""]
    L += _headline(res) + _latency_section(res) + _automation_section(res) + _detection_section(res) + _overhead_section(res) + _forecast_section(res)
    L += ["## G. Paper items that cannot be reproduced, and the missing input for each", "",
          "| Paper item | Missing input |", "|---|---|",
          "| Table 1 (7 metrics x 3 frameworks), Fig. 2 | simulator configuration (OMNeT++/Simulink/Kubernetes cluster), seeds, definitions of throughput, energy per bit, packet loss, QoS rate |",
          "| 44.9% latency, 3.8 / 6.9 / 11.4 ms | latency endpoints, traffic model, AO6G and BEA implementations, why a median is compared with means |",
          "| 9.8% automation efficiency, 94.7 | denominator, baseline value, relative-or-points |",
          "| 97.5% / 89.1 / 73.3 detection | attack generator, labels, class balance, sample size, splits, threshold |",
          "| Standardization Compliance Score 0.91 | scoring rubric (the paper calls it qualitative) |",
          "| Availability 99.2, 250 ms migration, 35% fewer manual configurations | measurement method and the baseline for each |", "",
          "## H. Limits of this ledger", "",
          "* Synthetic, seeded, simulated: it certifies software behaviour and the metric arithmetic, not field performance. Scenario magnitudes were chosen by the implementer.",
          "* The forecaster is a companion choice. Section F shows how it compares with persistence; where the planners tie, that is the result.",
          "* Reactive and predictive share trigger thresholds, step size and cooldown, so they differ only in the demand estimate. A tie means the forecast did not change the decision in these scenarios.",
          "* Monotone scale-up only (no release): `server-seconds` is reported so cost is visible.",
          "* Intent-to-verified time is quantised by the 5 s control tick: capacity activates 4 s after the decision and is verified at the next tick, so every verified action reads 5000 ms.",
          "* Detection: the rule detector has no false positives here because every rule fires on an attack-only condition in this generator, so its accuracy exceeds the logistic model's. The model finds more attacks (higher recall) at the cost of false alarms. Neither approaches 97.5%, and neither was tuned toward it.",
          "* Per-seed confidence intervals use few seeds; treat them as indicative.",
          "* Wall-clock overhead is for this Python implementation on this machine; it says nothing about a production ledger or consensus."]
    return "\n".join(L) + "\n"


def render_standards_map() -> str:
    L = ["# Standards map", "",
         "Every claimed interface is listed with its standards body, document, release, clause, the field or behaviour that corresponds, an executable local test and the outstanding gap. "
         "Clause numbers were read in the specification text itself (ETSI PDFs downloaded 2026-11-26), not recalled.", "",
         "**Three different statements, never merged:**", "",
         "1. *Conceptual mapping*: this code can be read as an instance of the clause's idea. Nothing is asserted about the standard's interface.",
         "2. *Local contract test succeeds*: an executable test in this repository checks a field or behaviour of this repository's own JSON contract.",
         "3. *Interoperability demonstrated against an independent implementation*: **none. This has not been done for any row.**", "",
         "No custom JSON interface here is called O1-compliant, ZSM-compliant or 3GPP-compliant. The paper's abstract names O-RAN; that does not oblige this repository to implement O-RAN interfaces, and it does not.", "",
         "| ID | Body | Document and release | Clause | Concept | Corresponds to in this repo | Test | Level | Outstanding gap |", "|---|---|---|---|---|---|---|---|---|"]
    for m in MAPPINGS:
        L.append(f"| {m['id']} | {m['body']} | {m['document']} | {m['clause']} | {m['concept']} | {m['implementation']} | {('`' + m['test'] + '`') if m['test'] else 'none'} | {m['level']} | {m['gap']} |")
    L += ["", "The paper itself cites ETSI GS ZSM 001 (requirements), 3GPP TS 28.533 (as \"MANO architecture\"; it is the 3GPP service-based management architecture, and NFV-MANO is a separate ETSI NFV matter) and ITU-T Y.3061. "
          "Only the first two are discussed here; ITU-T Y.3061 was not retrieved.", ""]
    return "\n".join(L)


def write_report(run_dir: Path, docs_dir: Path) -> str:
    res = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "results.md").write_text(render_results(res, manifest), encoding="utf-8")
    (docs_dir / "standards-map.md").write_text(render_standards_map(), encoding="utf-8")
    trace = run_dir / "decision_trace.md"
    if trace.exists():
        (docs_dir / "trace-example.md").write_text(trace.read_text(encoding="utf-8"), encoding="utf-8")
    return str(docs_dir / "results.md")
