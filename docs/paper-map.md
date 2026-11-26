# Paper map: from a published claim to code and evidence

**Paper.** Mahin MRH, Chakraborty P, Das N, Kaur H, Himel HU, Kaur J, Mohapatra AG. *Secured and Standardized Intelligent Zero-Touch 6G Framework for Edge-AI Applications.* IEEE Communications Standards Magazine, 2026. DOI [10.1109/MCOMSTD.2026.3660159](https://doi.org/10.1109/MCOMSTD.2026.3660159); IEEE Xplore document 11411876, pp. 1-5.

**What was read.** The full text PDF supplied with the task (5 pages: text, Fig. 1, Fig. 2, Table 1, 15 references). Nothing here is taken from the abstract alone.

## Bibliographic verification (2026-11-26)

| Check | Result |
|---|---|
| Complete byline | Seven authors, confirmed three ways: the PDF title block (Mahin, Chakraborty, Das, H. Kaur, Himel, J. Kaur, Mohapatra), the Crossref record for the DOI (seven authors in that order, each with an ORCID), and an IEEE Xplore metadata export found in a third-party repository. The abbreviated web listing that hides the last author is not used. |
| Final issue metadata | **None exists yet.** Crossref carries no volume or issue (year 2026, pages 1-5). The Xplore export lists the item under *IEEE Early Access Articles* with date 25 Feb 2026, volume "PP". The PDF footer reads "accepted for inclusion in a future issue ... with the exception of pagination". The citation therefore stays an early-access citation. |
| Corresponding author | Mohapatra (PDF). |
| Author-contribution statement | **The supplied PDF has none.** This repository therefore claims no specific authorship role for any author in the article. |
| Author-released code, data, appendices | **None found.** The PDF links none. A GitHub repository and code search for the title and for "SS-ZT6G" returned only a third-party literature-review CSV that lists the paper's metadata. No supplementary material was available. |

## What the paper is

A short framework paper. Fig. 1 is a layered block diagram, Fig. 2 a bar chart without printed values, Table 1 a seven-metric comparison. The evaluation is a **simulation** (OMNeT++, MATLAB Simulink, a Kubernetes Edge-AI cluster; 25 edge nodes in three domains; 50-500 Mbps per node; 5-20 intrusion attempts per minute; TensorFlow Lite models; Hyperledger Fabric; 100 iterations). No equation, algorithm, hyper-parameter, dataset name, label definition, split or code is given.

## Claim map

| Where | Claim | Inputs stated | Units | Comparator stated | Matching code in this repo | Reproduction command | Evidence status |
|---|---|---|---|---|---|---|---|
| Abstract, Conclusion | 44.9% decrease in latency | none | percent | "reference architectures" | `metrics.paired_latency`, `experiments.run_latency_experiment` (companion definition) | `sszt6g demo` | Paper: reported, **not reproducible**. Value is consistent with the text pair (6.9 - 3.8) / 6.9 = 44.93%, which compares a **median** (3.8 ms, SS-ZT6G) with a **mean** (6.9 ms, AO6G); the paper does not state that derivation. |
| Abstract, Conclusion | 9.8% increase in automation efficiency | none | percent | unstated | `metrics.automation_metrics`, `metrics.compare_rates` | `sszt6g demo` | Paper: reported, **definition unknown**. Relative or percentage points is not stated; with 94.7 as the SS-ZT6G value the implied baseline is 86.2 (relative) or 84.9 (points). Both are this repo's arithmetic. |
| Abstract, Conclusion | 97.5% security detection accuracy | none | percent | AO6G 89.1, BEA 73.3 (text) | `detection.py`, `experiments.run_detection_experiment` | `sszt6g demo` | Paper: reported, **not reproducible**. No ground truth, class balance, sample size or split. The metric list defines "Security Breach Rate" as the share of intrusion attempts detected or mitigated, which is recall-like rather than accuracy. |
| Experimental Setup | simulated hybrid testbed, 25 nodes, 3 domains | topology figures above | n/a | three frameworks: BEA, AO6G, SS-ZT6G | `workload.default_topology` (two sites, fictional) | n/a | Not reproduced. This repo uses its own two-site simulator, not OMNeT++/Simulink/Kubernetes. |
| Evaluation Metrics | 100 iterations; latency, availability, breach rate, automation efficiency, SCS | metric prose | ms, %, 0-1 | n/a | `metrics.py` (own definitions) | `sszt6g demo` | Definitions are one sentence each; SCS is called "qualitative". |
| Performance Analysis | median 3.8 ms; AO6G mean 6.9; BEA mean 11.4; availability 99.2 | none | ms, % | AO6G, BEA | none | n/a | Reported, not reproducible. |
| Performance Analysis | container migration in 250 ms; 35% fewer manual configuration events than AO6G | none | ms, % | AO6G | `Cluster` migration cost is an assumption (3 s), not 250 ms | n/a | Not reproduced; no measurement method. |
| Standards Alignment | SCS 0.91 | rubric absent | 0-1 | n/a | none | n/a | Not reproduced; `docs/standards-map.md` is a separate, sourced mapping. |
| Table 1 | seven metrics x three frameworks | none | ms, Gbps, %, J/bit, s | Baseline 6G, Zero-Touch 6G (No Security) | none | n/a | Arithmetic of the "Improvement" column re-checked (below). Values not reproducible. |
| Fig. 1 | four-layer architecture | n/a | n/a | n/a | `docs/figures/architecture.png` is an **original** drawing of this repo's pipeline | `sszt6g demo` | Not a reproduction of Fig. 1. |
| Fig. 2 | grouped bar chart of the same metrics | values not printed | mixed | three frameworks | none | n/a | Not reproducible without the data. |

## Arithmetic and consistency checks on the paper itself

Re-computed from the printed numbers (these are observations, not accusations: a short paper omits much):

1. Table 1 "Improvement" is computed against the **Baseline** column and matches to rounding: latency 53.57 (printed 53.6), throughput 41.76 (printed 41.7, truncated), packet loss 69.23, energy 50.00, breach probability 78.57, provisioning 60.00, QoS 17.35 (printed 17.3, truncated). The energy row is marked "up" although J/bit fell.
2. The text and Table 1 give **different values** for what look like the same quantities: SS-ZT6G latency 3.8 ms (text, median) vs 3.9 ms (Table 1); the lower comparator is BEA 11.4 vs Baseline 8.4, and AO6G 6.9 vs Zero-Touch No Security 6.1. The comparator names also differ between text and table.
3. 44.9% is reproduced by (6.9 - 3.8) / 6.9 only. Against Table 1 the same claim would be 53.6% (vs Baseline) or 36.1% (vs Zero-Touch, No Security, (6.1 - 3.9) / 6.1).
4. "Security Breach Rate" is defined as the detected-or-mitigated share (higher is better) while Table 1's "Security Breach Probability" is 0.9% (lower is better). 100 - 97.5 = 2.5, not 0.9; the two are not obviously the same quantity.
5. The prose metric list defines five metrics (latency, availability, security breach rate, automation efficiency, SCS). Table 1 shares only latency with it, plus a differently named breach metric. Its other five rows (throughput, packet loss, energy per bit, provisioning time, QoS rate) are never defined, and availability, automation efficiency and SCS appear only in prose.

## Which setting is where

| Setting | Status |
|---|---|
| Closed-loop stages, zero trust, blockchain audit, standards alignment | **paper-specified** (concept level only) |
| Edge nodes, domains, traffic range, attack rate, iterations, toolchain | **paper-specified** (testbed prose); **not used** by this companion |
| Latency endpoints, workload model, comparator implementations (BEA, AO6G) | **unspecified** by the paper |
| Consensus algorithm, ledger parameters | **unspecified** (paper names Hyperledger Fabric only) |
| Efficiency denominator, detection ground truth, class balance, splits | **unspecified** |
| Anything verified in original artifacts | **none**: no original artifact is public |
| Two sites, capacities, tenants, work and deadline values, 5 s planning interval, 4 s provisioning delay, 3 s migration cost | **chosen for this companion** (`configs/topology.yaml`) |
| Ridge forecaster on six lags and queue depth; persistence comparator | **chosen for this companion** (`configs/settings.yaml`) |
| Fixed, reactive and predictive planners; shared thresholds and cooldown | **chosen for this companion** |
| Ed25519 local identities, hash-chained log, 3-of-4 quorum ledger | **chosen for this companion**; not Hyperledger Fabric, not BFT |
| Seeds, SLA of 20 s, fault rate 10%, fixture sizes | **chosen for this companion** |

If the full methodology or any artifact appears later, update this map and prefer the verified specification.
