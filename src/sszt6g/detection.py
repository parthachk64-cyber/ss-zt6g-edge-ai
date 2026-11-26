"""Security detection on recorded management-plane telemetry.

Detector INPUTS are request-derived features only. Labels, attack kinds, episode ids, scenario names and
outcomes never enter the feature matrix (enforced by FORBIDDEN_TOKENS and a test). Authorization denials
(trust.py) and anomaly alerts are different things and are scored separately against the labels.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from .keys import verify_signature
from .trust import Keyring, PolicyStore, Request, evaluate
from .util import canonical, file_sha256, wilson_interval

FEATURES = ["claim_age_frac", "tenant_match", "sig_valid", "posture_score", "rate_60s", "rate_z", "op_rarity", "nonce_reuse", "stale_policy"]
FORBIDDEN_TOKENS = ("label", "scenario", "attack", "outcome", "kind", "episode", "split", "benign")
POSTURE_SCORE = {"compliant": 1.0, "degraded": 0.5, "unknown": 0.0}
WINDOW_MS = 60_000.0


@dataclass
class FitStats:
    """Fitted on the training partition only, and without labels."""

    rate_mean: float = 0.0
    rate_std: float = 1.0
    op_counts: dict = field(default_factory=dict)  # "subject|op" -> n
    subject_totals: dict = field(default_factory=dict)
    n_ops: int = 5

    def to_dict(self) -> dict:
        return {"rate_mean": self.rate_mean, "rate_std": self.rate_std, "op_counts": self.op_counts, "subject_totals": self.subject_totals}


def event_to_request(ev: dict) -> Request:
    return Request(ev["event_id"], ev["token"], ev["operation"], ev["service"], ev["site"], ev["resource_tenant"],
                   int(ev["servers_total_after"]), float(ev["ts_ms"]), int(ev["policy_revision"]))


def _causal_rates(events: Sequence[dict]) -> list:
    """Per event: how many earlier requests by the same subject fell in the preceding 60 s."""
    recent: dict = defaultdict(deque)
    out = []
    for ev in events:
        subj, ts = ev["token"]["claim"]["subject"], float(ev["ts_ms"])
        dq = recent[subj]
        while dq and dq[0] < ts - WINDOW_MS:
            dq.popleft()
        out.append(float(len(dq)))
        dq.append(ts)
    return out


def fit_stats(events: Sequence[dict]) -> FitStats:
    rates = _causal_rates(events)
    st = FitStats(rate_mean=float(np.mean(rates)) if rates else 0.0, rate_std=float(np.std(rates)) or 1.0)
    for ev in events:
        subj = ev["token"]["claim"]["subject"]
        k = f"{subj}|{ev['operation']}"
        st.op_counts[k] = st.op_counts.get(k, 0) + 1
        st.subject_totals[subj] = st.subject_totals.get(subj, 0) + 1
    return st


def featurize(events: Sequence[dict], keyring: Keyring, stats: FitStats, policy_revision_now: int) -> np.ndarray:
    """Events must be in timestamp order. Features at event i use only events 0..i (causal)."""
    rates = _causal_rates(events)
    seen: set = set()
    rows = []
    for ev, rate in zip(events, rates):
        claim, kid = ev["token"]["claim"], ev["token"]["kid"]
        issued, expires, ts = float(claim["issued_ms"]), float(claim["expires_ms"]), float(ev["ts_ms"])
        pub = keyring.get(kid)
        sig_ok = bool(pub and verify_signature(pub, canonical(claim), ev["token"]["sig"]))
        subj = claim["subject"]
        cnt = stats.op_counts.get(f"{subj}|{ev['operation']}", 0)
        tot = stats.subject_totals.get(subj, 0)
        rarity = -math.log((cnt + 1.0) / (tot + stats.n_ops))
        nonce = ev["request_nonce"]
        rows.append([
            (ts - issued) / max(expires - issued, 1.0),
            1.0 if claim["tenant"] == ev["resource_tenant"] else 0.0,
            1.0 if sig_ok else 0.0,
            POSTURE_SCORE.get(claim["posture"], 0.0),
            rate,
            (rate - stats.rate_mean) / stats.rate_std,
            rarity,
            1.0 if nonce in seen else 0.0,
            1.0 if int(ev["policy_revision"]) < policy_revision_now else 0.0,
        ])
        seen.add(nonce)
    return np.asarray(rows, dtype=float)


class RuleDetector:
    """Deterministic policy-style rules over the same features. Score 1 = anomalous."""

    name = "rules"
    threshold = 0.5
    orientation = "higher_is_more_anomalous"

    def score(self, X: np.ndarray) -> np.ndarray:
        age, tenant, sig, nonce = X[:, FEATURES.index("claim_age_frac")], X[:, 1], X[:, 2], X[:, FEATURES.index("nonce_reuse")]
        return ((age > 1.0) | (tenant == 0) | (sig == 0) | (nonce == 1)).astype(float)


class ModelDetector:
    """Logistic regression on the feature vector; threshold chosen on development data only."""

    name = "logreg"
    orientation = "higher_is_more_anomalous"

    def __init__(self):
        self.scaler = StandardScaler()
        self.model = LogisticRegression(max_iter=2000, C=1.0)
        self.threshold = 0.5
        self.dev_selection: dict = {}

    def fit(self, X: np.ndarray, y: np.ndarray) -> "ModelDetector":
        self.model.fit(self.scaler.fit_transform(X), y)
        return self

    def score(self, X: np.ndarray) -> np.ndarray:
        return self.model.predict_proba(self.scaler.transform(X))[:, 1]

    def select_threshold(self, X_dev: np.ndarray, y_dev: np.ndarray) -> float:
        """Pick the F1-maximising threshold on development data; never on test data."""
        s = self.score(X_dev)
        best_t, best_f1 = 0.5, -1.0
        for t in np.unique(np.round(np.concatenate([s, [0.5]]), 4)):
            f1 = confusion_metrics(y_dev, (s >= t).astype(int))["f1"] or 0.0
            if f1 > best_f1 + 1e-12:
                best_t, best_f1 = float(t), f1
        self.threshold = best_t
        self.dev_selection = {"metric": "f1", "dev_f1": best_f1, "n_dev": int(len(y_dev)), "dev_prevalence": float(np.mean(y_dev))}
        return best_t

    def save(self, path: Path | str) -> str:
        joblib.dump({"scaler": self.scaler, "model": self.model, "threshold": self.threshold, "features": FEATURES,
                     "orientation": self.orientation, "dev_selection": self.dev_selection}, path)
        return file_sha256(path)


def confusion_metrics(y_true: Sequence[int], y_pred: Sequence[int]) -> dict:
    y, p = np.asarray(y_true, int), np.asarray(y_pred, int)
    tp, fp = int(np.sum((y == 1) & (p == 1))), int(np.sum((y == 0) & (p == 1)))
    tn, fn = int(np.sum((y == 0) & (p == 0))), int(np.sum((y == 1) & (p == 0)))
    n = tp + fp + tn + fn
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * prec * rec / (prec + rec)) if (prec and rec) else (0.0 if (prec is not None and rec is not None) else None)
    ci = wilson_interval(tp + tn, n)
    return {"n": n, "tp": tp, "fp": fp, "tn": tn, "fn": fn, "accuracy": (tp + tn) / n if n else None,
            "accuracy_ci95_wilson": list(ci) if ci else None, "precision": prec, "recall": rec, "f1": f1,
            "prevalence": (tp + fn) / n if n else None}


def always_benign_baseline(y_true: Sequence[int]) -> dict:
    return confusion_metrics(y_true, np.zeros(len(y_true), dtype=int))


def policy_denial_scores(events: Sequence[dict], policy: PolicyStore, keyring: Keyring) -> np.ndarray:
    """1.0 where the zero-trust policy would DENY the request. Scored as if it were a detector, to show that
    a denial is not automatically a correct detection."""
    return np.asarray([0.0 if evaluate(event_to_request(ev), policy, float(ev["ts_ms"]), keyring).allow else 1.0 for ev in events])


def alert_records(event_ids: Sequence[str], detector, scores: np.ndarray, X: np.ndarray, limit: Optional[int] = None) -> list:
    """Persisted detector output: features used, score orientation, threshold, evidence ids and decision."""
    out = []
    for i, (eid, s) in enumerate(zip(event_ids, scores)):
        if limit is not None and i >= limit:
            break
        out.append({"event_id": eid, "detector": detector.name, "score": float(s), "orientation": detector.orientation,
                    "threshold": float(detector.threshold), "features": {n: float(v) for n, v in zip(FEATURES, X[i])},
                    "decision": "alert" if s >= detector.threshold else "no_alert", "evidence_ids": [f"event:{eid}"]})
    return out
