# Results ledger

Run `demo`, generated 2026-11-26T02:07:18Z, code commit `92d039b4ba40ef69306f63d60123404784f59ffa` (working tree of code clean at run time). Regenerate with `sszt6g demo` (about a minute, CPU only). All tables below are produced from `runs/<id>/results.json`; per-job latencies, per-intent dispositions, labelled predictions and the manifest sit beside it.

> **Read this first.** The paper is a simulation study whose code, data, labels and splits are not public and which does not define its comparators or endpoints. This repository therefore does **not reproduce** the paper's numbers. It runs an independent, seeded companion and reports what that companion measures, including results where the predictive planner is no better than a reactive one.

## A. Headline claims: where each number comes from

Three labelled origins are never mixed: **PAPER** (printed in the supplied article, not reproduced), **COMPANION-RUN** (computed by this repository's code on seeded synthetic workloads, simulated time), **HARDWARE** (Python wall clock; section E only). No value measured on real telecom hardware exists in this repository.

| # | Headline (paper) | PAPER value, comparator, source | What the paper does not say | COMPANION-RUN definition, denominator, comparator | COMPANION-RUN value | Data source | Execution status |
|---|---|---|---|---|---|---|---|
| 1 | 44.9% lower latency | 44.9% (abstract, Conclusion). The text prints a median of 3.8 ms for SS-ZT6G against a *mean* of 6.9 ms for AO6G and 11.4 ms for BEA; (6.9-3.8)/6.9 = 44.9%, so the figure is consistent with that pair, but the paper does not state this derivation. Table 1 prints different values (3.9 ms vs 8.4 ms baseline = 53.6%). | latency endpoints, workload, why the text and Table 1 disagree, a mean-versus-median mix | completion minus arrival; comparable completed jobs (n = 17049 for predictive vs reactive, 15465 vs fixed); baseline named | predictive vs reactive: **-11.9%** of mean, -24.8% of p95. predictive vs fixed placement: **87.1%** (drop rate fixed 9.3%). Scenario `ramp_burst`, 8 paired seeds | seeded synthetic arrivals (`sszt6g.workload`) | PAPER: reported, not reproducible (no code or data). COMPANION: executed here, simulated |
| 2 | 9.8% increase in automation efficiency | 9.8% (abstract, Conclusion); automation efficiency 94.7 (text). | whether 9.8 is relative or percentage points, the baseline value, the denominator. If relative the baseline would be 94.7/1.098 = 86.2; if points, 84.9. Both are this repository's arithmetic; neither is stated by the paper | unique policy-eligible intents verified in SLA / unique eligible intents (31 / 48 predictive) | reactive 66.7% → predictive 64.6%: relative -3.1%, -2.1 pp. fixed 47.9% | seeded intents + eligibility table (`examples/fixtures`) | PAPER: reported, definition unknown. COMPANION: executed, simulated |
| 3 | 97.5% security detection accuracy | 97.5% vs AO6G 89.1 vs BEA 73.3 (text), under DDoS and data tampering. The metric list defines "Security Breach Rate" as the share of intrusion attempts detected or mitigated, which is recall-like, not accuracy. | sample count, class balance, ground truth, split, threshold | (TP+TN)/N on 453 held-out events, prevalence 17.9%; comparators: always-benign 82.12%, rules 92.72%, policy denial 82.56% | logreg **89.85%** (FP 34, FN 12), recall 85.2%, precision 67.0% | labelled synthetic management-plane events (`examples/fixtures`) | PAPER: reported, not reproducible. COMPANION: executed; the value was **not** tuned toward 97.5% |

## B. Latency (companion, simulated milliseconds)

Application latency = job completion minus arrival (network + queue + compute, simulated). Reduction = 100 x (baseline - proposed) / baseline on jobs **completed in both arms** (identical arrivals, paired seeds). Drops and deadline misses are shown because a system can look faster by dropping slow jobs.

### ramp_burst (peak 24.0 jobs/s at the ingress site; 8 paired seeds)

| Arm | completed | dropped | completion rate | late | miss rate incl. dropped | mean | median | p95 | server-seconds/run | intent→verified (mean) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fixed | 15465 | 1584 | 90.7% | 6875 | 49.6% | 1751.6 | 181.1 | 5005.9 | 480 | n/a |
| predictive | 17049 | 0 | 100.0% | 1870 | 11.0% | 226.2 | 122.0 | 888.1 | 632 | 5000 |
| reactive | 17049 | 0 | 100.0% | 1624 | 9.5% | 202.2 | 122.0 | 711.8 | 630 | 5000 |

| Comparison (baseline named) | comparable jobs | baseline mean | proposed mean | reduction of mean | reduction of p95 | per-seed mean reduction [bootstrap 95% CI] |
|---|---:|---:|---:|---:|---:|---|
| predictive vs **fixed** | 15465 | 1751.6 | 225.4 | 87.1% | 82.0% | 87.2% [84.4, 89.5] |
| predictive vs **reactive** | 17049 | 202.2 | 226.2 | -11.9% | -24.8% | -11.9% [-35.8, 0.0] |
| reactive vs **fixed** | 15465 | 1751.6 | 203.3 | 88.4% | 85.2% | 88.4% [87.0, 89.8] |

### steady_low (peak 6.0 jobs/s at the ingress site; 8 paired seeds)

| Arm | completed | dropped | completion rate | late | miss rate incl. dropped | mean | median | p95 | server-seconds/run | intent→verified (mean) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fixed | 10499 | 0 | 100.0% | 0 | 0.0% | 111.2 | 103.2 | 188.6 | 480 | n/a |
| predictive | 10499 | 0 | 100.0% | 0 | 0.0% | 111.2 | 103.2 | 188.6 | 480 | n/a |
| reactive | 10499 | 0 | 100.0% | 0 | 0.0% | 111.2 | 103.2 | 188.6 | 480 | n/a |

| Comparison (baseline named) | comparable jobs | baseline mean | proposed mean | reduction of mean | reduction of p95 | per-seed mean reduction [bootstrap 95% CI] |
|---|---:|---:|---:|---:|---:|---|
| predictive vs **fixed** | 10499 | 111.2 | 111.2 | 0.0% | 0.0% | 0.0% [0.0, 0.0] |
| predictive vs **reactive** | 10499 | 111.2 | 111.2 | 0.0% | 0.0% | 0.0% [0.0, 0.0] |
| reactive vs **fixed** | 10499 | 111.2 | 111.2 | 0.0% | 0.0% | 0.0% [0.0, 0.0] |

### step_burst (peak 24.0 jobs/s at the ingress site; 8 paired seeds)

| Arm | completed | dropped | completion rate | late | miss rate incl. dropped | mean | median | p95 | server-seconds/run | intent→verified (mean) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fixed | 15392 | 2365 | 86.7% | 7500 | 55.6% | 2073.1 | 226.2 | 5017.6 | 480 | n/a |
| predictive | 17740 | 17 | 99.9% | 2658 | 15.1% | 353.5 | 122.1 | 1820.1 | 684 | 5000 |
| reactive | 17741 | 16 | 99.9% | 2341 | 13.3% | 338.2 | 121.9 | 1820.1 | 673 | 5000 |

| Comparison (baseline named) | comparable jobs | baseline mean | proposed mean | reduction of mean | reduction of p95 | per-seed mean reduction [bootstrap 95% CI] |
|---|---:|---:|---:|---:|---:|---|
| predictive vs **fixed** | 15392 | 2073.1 | 369.3 | 82.2% | 57.1% | 82.2% [80.6, 83.8] |
| predictive vs **reactive** | 17740 | 338.2 | 353.5 | -4.5% | 0.0% | -4.5% [-11.2, 0.2] |
| reactive vs **fixed** | 15392 | 2073.1 | 357.2 | 82.8% | 57.1% | 82.8% [81.4, 84.1] |

## C. Automation (companion)

Definition: unique policy-eligible intents completed **and verified automatically within SLA (20 s simulated)** / all unique policy-eligible intents. Eligibility is fixed from static policy before any controller runs. Duplicates count once. Faults: 10% of activations are silently lost (same actions lost in every arm). Seeds: 8.

| Arm | eligible (denominator) | verified in SLA (numerator) | rate | verified late | policy-denied | capacity failures | rolled back + escalated | other failures | unfinished | ineligible denied / not denied |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| fixed | 48 | 23 | 47.9% | 0 | 0 | 21 | 4 | 0 | 0 | 32 / 0 |
| predictive | 48 | 31 | 64.6% | 0 | 0 | 13 | 4 | 0 | 0 | 32 / 0 |
| reactive | 48 | 32 | 66.7% | 0 | 0 | 12 | 4 | 0 | 0 | 32 / 0 |

| Comparison | baseline rate | proposed rate | relative increase | percentage-point change |
|---|---:|---:|---:|---:|
| predictive vs **fixed** | 47.9% | 64.6% | 34.8% | 16.7 pp |
| predictive vs **reactive** | 66.7% | 64.6% | -3.1% | -2.1 pp |
| reactive vs **fixed** | 47.9% | 66.7% | 39.1% | 18.7 pp |

## D. Security detection (companion, held-out synthetic episodes)

Test n = 453, attack prevalence 17.9%. Threshold for `logreg` chosen on development data (F1-maximising, dev n = 302): 0.2419. Accuracy = (TP+TN)/N with a Wilson 95% interval. `policy_denial` scores the zero-trust authorization decision as if it were a detector, to show a denial is not automatically a detection.

| Detector | accuracy [95% CI] | TP | FP | TN | FN | precision | recall | F1 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| always benign | 82.12% | 0 | 0 | 372 | 81 | n/a | 0.0% | n/a |
| logreg | 89.85% [86.7%, 92.3%] | 69 | 34 | 338 | 12 | 67.0% | 85.2% | 0.750 |
| policy_denial | 82.56% [78.8%, 85.8%] | 35 | 33 | 339 | 46 | 51.5% | 43.2% | 0.470 |
| rules | 92.72% [89.9%, 94.8%] | 48 | 0 | 372 | 33 | 100.0% | 59.3% | 0.744 |

Recall by attack kind (test):

| Kind | logreg | policy_denial | rules |
|---|---:|---:|---:|
| bad_signature | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) |
| credential_misuse | 63.6% (21/33) | 3.0% (1/33) | 0.0% (0/33) |
| expired_identity | 100.0% (12/12) | 100.0% (12/12) | 100.0% (12/12) |
| replay | 100.0% (15/15) | 6.7% (1/15) | 100.0% (15/15) |
| tenant_mismatch | 100.0% (15/15) | 100.0% (15/15) | 100.0% (15/15) |

## E. Overhead ablations (Python wall clock on this machine, microseconds)

Hardware: Windows-10-10.0.26300-SP0, 12 logical CPUs, Python 3.10.11, no GPU. These are **measured**, not simulated milliseconds. The permissive evaluator and the null ledger are benchmark-only adapters, used here and nowhere else. `submit_*` is dominated by durable SQLite state commits (several per action), so the zero-trust and ledger deltas are small relative to it. `append_audit_log_file` reopens the file on every append (cost of the I/O), so it is not comparable with the in-memory rows.

| Operation | n | median | p95 |
|---|---:|---:|---:|
| append_audit_log_file | 400 | 1509.4 | 10426.5 |
| append_audit_log_memory | 400 | 24.8 | 49.7 |
| append_none | 400 | 3.0 | 3.7 |
| append_quorum_4_validators_memory | 400 | 1112.4 | 11077.9 |
| evaluate_permissive | 3000 | 17.0 | 25.5 |
| evaluate_zero_trust | 3000 | 315.8 | 4364.5 |
| submit_permissive_audit_log | 100 | 9850.4 | 29450.7 |
| submit_permissive_no_ledger | 100 | 7959.0 | 15424.5 |
| submit_zero_trust_audit_log | 100 | 10790.9 | 26546.9 |
| submit_zero_trust_no_ledger | 100 | 9107.2 | 21907.9 |
| submit_zero_trust_quorum | 100 | 22175.8 | 52158.2 |

## F. Forecaster vs persistence (held-out scenarios, one-step-ahead arrivals per 5 s interval)

Trained on 24 earlier scenarios (2688 rows); evaluated on different seeds and a peak/ramp not used in training. No tuning on the test data (alpha fixed in `configs/settings.yaml`).

| Scenario | model MAE | persistence MAE | model RMSE | persistence RMSE | n |
|---|---:|---:|---:|---:|---:|
| all | 10.88 | 10.66 | 18.59 | 19.25 | 672 |
| ramp_burst | 12.33 | 12.52 | 17.07 | 18.06 | 224 |
| steady_low | 5.77 | 5.85 | 9.30 | 9.50 | 224 |
| step_burst | 14.54 | 13.62 | 25.68 | 26.37 | 224 |

## G. Paper items that cannot be reproduced, and the missing input for each

| Paper item | Missing input |
|---|---|
| Table 1 (7 metrics x 3 frameworks), Fig. 2 | simulator configuration (OMNeT++/Simulink/Kubernetes cluster), seeds, definitions of throughput, energy per bit, packet loss, QoS rate |
| 44.9% latency, 3.8 / 6.9 / 11.4 ms | latency endpoints, traffic model, AO6G and BEA implementations, why a median is compared with means |
| 9.8% automation efficiency, 94.7 | denominator, baseline value, relative-or-points |
| 97.5% / 89.1 / 73.3 detection | attack generator, labels, class balance, sample size, splits, threshold |
| Standardization Compliance Score 0.91 | scoring rubric (the paper calls it qualitative) |
| Availability 99.2, 250 ms migration, 35% fewer manual configurations | measurement method and the baseline for each |

## H. Limits of this ledger

* Synthetic, seeded, simulated: it certifies software behaviour and the metric arithmetic, not field performance. Scenario magnitudes were chosen by the implementer.
* The forecaster is a companion choice. Section F shows how it compares with persistence; where the planners tie, that is the result.
* Reactive and predictive share trigger thresholds, step size and cooldown, so they differ only in the demand estimate. A tie means the forecast did not change the decision in these scenarios.
* Monotone scale-up only (no release): `server-seconds` is reported so cost is visible.
* Intent-to-verified time is quantised by the 5 s control tick: capacity activates 4 s after the decision and is verified at the next tick, so every verified action reads 5000 ms.
* Detection: the rule detector has no false positives here because every rule fires on an attack-only condition in this generator, so its accuracy exceeds the logistic model's. The model finds more attacks (higher recall) at the cost of false alarms. Neither approaches 97.5%, and neither was tuned toward it.
* Per-seed confidence intervals use few seeds; treat them as indicative.
* Wall-clock overhead is for this Python implementation on this machine; it says nothing about a production ledger or consensus.
