"""Figures. Every data figure is drawn from run artifacts (results.json, CSVs); the architecture diagram is an
original drawing of THIS companion, not a reproduction of the paper's Figure 1."""
from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

COL = {"fixed": "#7f7f7f", "reactive": "#d55e00", "predictive": "#0072b2", "accent": "#009e73", "warn": "#cc79a7"}
plt.rcParams.update({"font.size": 9, "axes.spines.top": False, "axes.spines.right": False, "figure.dpi": 130, "savefig.bbox": "tight"})


def _read_jobs(run_dir: Path) -> list:
    with gzip.open(run_dir / "per_job_latency.csv.gz", "rt", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def architecture(path: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 5.6))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 56)
    ax.axis("off")

    def box(x, y, w, h, text, fc="#f2f2f2", ec="#333333", bold=False):
        ax.add_patch(plt.Rectangle((x, y), w, h, fc=fc, ec=ec, lw=1.1))
        ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=8.5, fontweight="bold" if bold else "normal", wrap=True)

    def arrow(x1, y1, x2, y2, label="", color="#333333"):
        ax.annotate("", xy=(x2, y2), xytext=(x1, y1), arrowprops=dict(arrowstyle="->", color=color, lw=1.2))
        if label:
            ax.text((x1 + x2) / 2, (y1 + y2) / 2 + 1.1, label, fontsize=7.5, ha="center", color=color)

    ax.text(2, 53.5, "Management plane (closed loop, advisory to the local simulator only)", fontsize=10, fontweight="bold")
    box(2, 40, 15, 9, "Telemetry\nsnapshot\n(cutoff t)", "#e6f2fa")
    box(21, 40, 15, 9, "Forecaster\nridge on lags\nvs persistence", "#e6f2fa")
    box(40, 40, 15, 9, "Planner\nretain / reserve /\nmove", "#e6f2fa")
    box(59, 40, 15, 9, "Zero-trust policy\ndefault deny\n(per operation)", "#fdeee0")
    box(78, 40, 20, 9, "Audit ledger\nhash-chained log or\nquorum replicas\n(commit BEFORE apply)", "#e8f5ee")
    for x1, x2 in ((17, 21), (36, 40), (55, 59), (74, 78)):
        arrow(x1, 44.5, x2, 44.5)
    box(78, 24, 20, 9, "Actuator\nidempotent per action id\n(simulated capacity)", "#e8f5ee")
    arrow(88, 40, 88, 33, "commit receipt")
    box(59, 24, 15, 9, "Verify against next\nobserved state\nrollback / escalate", "#e6f2fa")
    arrow(78, 28.5, 74, 28.5)
    ax.text(76, 30.2, "applied", fontsize=7.5, ha="center", color="#333333")
    ax.plot([59, 9.5], [28.5, 28.5], color="#555555", lw=1.0, ls=":")
    ax.annotate("", xy=(9.5, 40), xytext=(9.5, 28.5), arrowprops=dict(arrowstyle="->", color="#555555", lw=1.0))
    ax.text(10.5, 33.5, "next observation closes the loop", fontsize=7.5, color="#555555")
    ax.text(3, 21.6, "Every state (proposed, authorized/denied, committed, applied, verified) gets its own receipt", fontsize=8, color="#444444")
    ax.plot([0, 100], [17.5, 17.5], color="#999999", lw=0.8, ls="--")
    ax.text(2, 14.5, "Data plane (inference requests; emulated, never routed through the management plane)", fontsize=10, fontweight="bold")
    box(2, 3, 18, 8, "Jobs arrive at\ningress site\n(seeded Poisson)", "#f6f0f8")
    box(28, 3, 22, 8, "Route to site with\nlowest expected\ncompletion", "#f6f0f8")
    box(58, 3, 18, 8, "Per-site FIFO\nservers (queue,\ncompute)", "#f6f0f8")
    box(82, 3, 16, 8, "Completed / dropped\n/ denied /\nunfinished", "#f6f0f8")
    for x1, x2 in ((20, 28), (50, 58), (76, 82)):
        arrow(x1, 7, x2, 7)
    ax.text(50, 0.3, "Simulated network, queue and compute time are kept apart from measured Python wall time.", fontsize=8, ha="center", color="#444444")
    fig.savefig(path)
    plt.close(fig)


def latency_distribution(run_dir: Path, path: Path) -> None:
    rows = _read_jobs(run_dir)
    kinds = sorted({r["scenario_kind"] for r in rows})
    fig, axes = plt.subplots(1, len(kinds), figsize=(4.2 * len(kinds), 3.6), sharey=True)
    axes = np.atleast_1d(axes)
    for ax, kind in zip(axes, kinds):
        for arm in ("fixed", "reactive", "predictive"):
            lat = np.sort([float(r["latency_ms"]) for r in rows if r["scenario_kind"] == kind and r["arm"] == arm and r["status"] == "completed"])
            if lat.size:
                ax.plot(lat, np.arange(1, lat.size + 1) / lat.size, color=COL[arm], label=f"{arm} (n={lat.size})", lw=1.5)
        ax.set_xscale("log")
        ax.set_xlabel("application latency (simulated ms, log)")
        ax.set_title(kind)
        ax.axvline(400, color="#999999", ls=":", lw=1)
        ax.grid(alpha=0.25)
    axes[0].set_ylabel("cumulative share of completed jobs")
    axes[-1].legend(frameon=False, fontsize=8, loc="lower right")
    fig.suptitle("Latency distribution, completed jobs, pooled paired seeds (dotted line: 400 ms deadline)", fontsize=9)
    fig.savefig(path)
    plt.close(fig)


def automation_outcomes(results: dict, path: Path) -> None:
    arms = list(results["automation"]["arms"])
    cats = ["verified_in_sla", "verified_late", "denied_by_policy", "failed_capacity", "rolled_back_escalated", "failed_other", "unfinished"]
    colors = ["#009e73", "#56b4e9", "#cc79a7", "#d55e00", "#e69f00", "#999999", "#444444"]
    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    bottom = np.zeros(len(arms))
    for c, col in zip(cats, colors):
        vals = np.array([results["automation"]["arms"][a]["eligible_outcomes"][c] for a in arms], dtype=float)
        ax.bar(arms, vals, bottom=bottom, color=col, label=c.replace("_", " "))
        bottom += vals
    ax.set_ylabel("policy-eligible unique intents (all seeds)")
    ax.set_title("Disposition of eligible intents by planner (injected activation faults)")
    ax.legend(frameon=False, fontsize=7.5, loc="center left", bbox_to_anchor=(1.0, 0.5))
    fig.savefig(path)
    plt.close(fig)


def detection_errors(results: dict, path: Path) -> None:
    det = results["detection"]
    names = ["policy_denial", "rules", "logreg"]
    fig, axes = plt.subplots(1, len(names) + 1, figsize=(3.0 * (len(names) + 1) + 1.6, 3.4), gridspec_kw={"width_ratios": [1, 1, 1, 1.5], "wspace": 0.55})
    for ax, n in zip(axes, names):
        t = det["detectors"][n]["test"]
        m = np.array([[t["tn"], t["fp"]], [t["fn"], t["tp"]]])
        ax.imshow(m, cmap="Blues")
        for (i, j), v in np.ndenumerate(m):
            ax.text(j, i, str(v), ha="center", va="center", color="white" if v > m.max() / 2 else "black", fontsize=11)
        ax.set_xticks([0, 1], ["pred benign", "pred attack"], fontsize=7)
        ax.set_yticks([0, 1], ["benign", "attack"], fontsize=7)
        ax.set_title(f"{n}\nacc {t['accuracy']:.3f}", fontsize=8.5)
    ax = axes[-1]
    vals = {"always benign": det["always_benign"]["accuracy"], **{n: det["detectors"][n]["test"]["accuracy"] for n in names}}
    ax.barh(list(vals), list(vals.values()), color=["#999999", COL["warn"], COL["reactive"], COL["predictive"]])
    ax.set_xlim(0.5, 1.0)
    ax.set_xlabel("test accuracy")
    for i, v in enumerate(vals.values()):
        ax.text(v + 0.004, i, f"{v:.3f}", va="center", fontsize=8)
    fig.suptitle(f"Detection on held-out synthetic episodes (test n={det['detectors']['rules']['test']['n']}, prevalence {det['prevalence']['test']:.3f})", fontsize=9)
    fig.savefig(path)
    plt.close(fig)


def audit_overhead(results: dict, path: Path) -> None:
    o = results["overhead"]
    keys = [("append_none", "no ledger"), ("append_audit_log_memory", "audit log (memory)"), ("append_audit_log_file", "audit log (file)"),
            ("append_quorum_4_validators_memory", "quorum, 4 validators")]
    fig, axes = plt.subplots(1, 2, figsize=(9.4, 3.4))
    med = [o[k]["median"] for k, _ in keys]
    p95 = [o[k]["p95"] for k, _ in keys]
    x = np.arange(len(keys))
    axes[0].bar(x - 0.18, med, 0.36, label="median", color=COL["predictive"])
    axes[0].bar(x + 0.18, p95, 0.36, label="p95", color=COL["reactive"])
    axes[0].set_xticks(x, [n for _, n in keys], rotation=18, ha="right", fontsize=8)
    axes[0].set_ylabel("microseconds per append (Python wall clock)")
    axes[0].set_yscale("log")
    axes[0].legend(frameon=False)
    axes[0].set_title("Ledger append cost")
    sub = [("submit_permissive_no_ledger", "permissive,\nno ledger"), ("submit_zero_trust_no_ledger", "zero trust,\nno ledger"), ("submit_zero_trust_audit_log", "zero trust,\naudit log"),
           ("submit_zero_trust_quorum", "zero trust,\nquorum")]
    axes[1].bar(range(len(sub)), [o[k]["median"] for k, _ in sub], color="#56b4e9")
    axes[1].set_xticks(range(len(sub)), [n for _, n in sub], fontsize=8)
    axes[1].set_ylabel("median microseconds per submit()")
    axes[1].set_title("Whole management operation")
    fig.suptitle("Measured on this machine, not simulated milliseconds", fontsize=9)
    fig.savefig(path)
    plt.close(fig)


def make_all(run_dir: Path, out_dir: Path) -> list:
    out_dir.mkdir(parents=True, exist_ok=True)
    results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    architecture(out_dir / "architecture.png")
    latency_distribution(run_dir, out_dir / "latency_distribution.png")
    automation_outcomes(results, out_dir / "automation_outcomes.png")
    detection_errors(results, out_dir / "detection_errors.png")
    audit_overhead(results, out_dir / "audit_overhead.png")
    return sorted(p.name for p in out_dir.glob("*.png"))
