"""Demand forecasting: regularised regression on lagged arrival counts and queue depth.

The estimator is a COMPANION CHOICE. The paper does not say which predictor, features or horizon it uses.
Scalers and model are fitted on earlier training scenarios only; whole scenarios and future periods are
held out for evaluation.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Sequence

import joblib
import numpy as np
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from .errors import EmptyHistoryError
from .telemetry import Snapshot, TelemetryRecord
from .util import file_sha256

LAGS = 6
MODEL_ID = "ridge-lag6-queue"


def feature_vector(history: Sequence[float], queue_per_server: float, lags: int = LAGS) -> np.ndarray:
    """[lag_1 .. lag_L (oldest to newest), queue depth per server]. A short history is left-padded
    with its earliest value. An empty history is an error, never a silent zero."""
    if len(history) == 0:
        raise EmptyHistoryError("no telemetry before the observation cutoff")
    h = list(history)[-lags:]
    pad = [h[0]] * (lags - len(h))
    return np.asarray(pad + h + [float(queue_per_server)], dtype=float)


@dataclass(frozen=True)
class Forecast:
    forecast_id: str
    made_at_ms: float  # observation cutoff: only records with ts <= made_at_ms were used
    target_window_ms: tuple  # [start, end) of the interval being predicted
    service: str
    ingress: str
    predicted_arrivals: float
    persistence_arrivals: float
    model_id: str
    history_len: int
    features: tuple

    def to_dict(self) -> dict:
        d = asdict(self)
        d["target_window_ms"] = list(self.target_window_ms)
        d["features"] = list(self.features)
        return d


class PersistenceForecaster:
    """Predicts that the next interval equals the last observed one."""

    model_id = "persistence"

    def predict_count(self, history: Sequence[float], queue_per_server: float = 0.0) -> float:
        if len(history) == 0:
            raise EmptyHistoryError("no telemetry before the observation cutoff")
        return float(history[-1])


class Forecaster:
    model_id = MODEL_ID

    def __init__(self, alpha: float = 3.0, lags: int = LAGS):
        self.alpha, self.lags = alpha, lags
        self.scaler = StandardScaler()
        self.model = Ridge(alpha=alpha)
        self.fitted = False
        self.train_manifest: dict = {}

    def fit(self, X: np.ndarray, y: np.ndarray, manifest: Optional[dict] = None) -> "Forecaster":
        Xs = self.scaler.fit_transform(X)  # scaler sees training rows only
        self.model.fit(Xs, y)
        self.fitted = True
        self.train_manifest = dict(manifest or {}, n_rows=int(len(y)), alpha=self.alpha, lags=self.lags)
        return self

    def predict_count(self, history: Sequence[float], queue_per_server: float = 0.0) -> float:
        if not self.fitted:
            raise RuntimeError("forecaster is not fitted")
        x = feature_vector(history, queue_per_server, self.lags).reshape(1, -1)
        return float(max(0.0, self.model.predict(self.scaler.transform(x))[0]))

    def save(self, path: Path | str) -> str:
        joblib.dump({"alpha": self.alpha, "lags": self.lags, "scaler": self.scaler, "model": self.model, "manifest": self.train_manifest}, path)
        return file_sha256(path)

    @staticmethod
    def load(path: Path | str) -> "Forecaster":
        d = joblib.load(path)
        f = Forecaster(d["alpha"], d["lags"])
        f.scaler, f.model, f.train_manifest, f.fitted = d["scaler"], d["model"], d["manifest"], True
        return f


def forecast_from_snapshot(predictor, snap: Snapshot, service: str, ingress: str, interval_ms: int) -> Forecast:
    """Predict the next planning interval from a snapshot. Raises EmptyHistoryError on no history."""
    history = snap.arrivals(service, ingress)
    latest: Optional[TelemetryRecord] = snap.latest(service, ingress)
    qps = (latest.queue_depth / max(1, latest.allocated)) if latest else 0.0
    pred = predictor.predict_count(history, qps)
    persistence = float(history[-1])
    start = snap.cutoff_ms
    from .util import stable_id
    fid = stable_id("fc", snap.snapshot_id, service, ingress, predictor.model_id)
    return Forecast(fid, snap.cutoff_ms, (start, start + interval_ms), service, ingress, pred, persistence,
                    predictor.model_id, len(history), tuple(float(v) for v in feature_vector(history, qps, getattr(predictor, "lags", LAGS))))


def training_rows(telemetry_records: Sequence[TelemetryRecord], service: str, ingress: str, lags: int = LAGS) -> tuple:
    """(X, y) from one run's telemetry: features use only records up to tick k, target is tick k+1."""
    recs = [r for r in telemetry_records if r.service == service and r.site == ingress]
    counts = [r.ingress_arrivals for r in recs]
    X, y = [], []
    for k in range(1, len(recs) - 1):
        qps = recs[k].queue_depth / max(1, recs[k].allocated)
        X.append(feature_vector(counts[: k + 1], qps, lags))
        y.append(counts[k + 1])
    return (np.asarray(X), np.asarray(y, dtype=float)) if X else (np.zeros((0, lags + 1)), np.zeros(0))


def error_metrics(y_true: Sequence[float], y_pred: Sequence[float]) -> dict:
    a, b = np.asarray(y_true, float), np.asarray(y_pred, float)
    if a.size == 0:
        return {"n": 0, "mae": None, "rmse": None}
    return {"n": int(a.size), "mae": float(np.mean(np.abs(a - b))), "rmse": float(np.sqrt(np.mean((a - b) ** 2)))}
