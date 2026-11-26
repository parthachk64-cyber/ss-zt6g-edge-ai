"""Small pure helpers: canonical JSON, hashing, stable ids and metric arithmetic."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np


def canonical(obj: Any) -> bytes:
    """Deterministic JSON bytes: sorted keys, no whitespace, no NaN."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    """A short deterministic identifier: the same parts always give the same id."""
    return f"{prefix}-{sha256_hex(canonical(list(parts)))[:16]}"


def unit_hash(*parts: Any) -> float:
    """Deterministic pseudo-random number in [0, 1) derived from the parts (for paired fault injection)."""
    return int(sha256_hex(canonical(list(parts)))[:12], 16) / float(16 ** 12)


def seeded_rng(*parts: Any) -> np.random.Generator:
    return np.random.default_rng(int(sha256_hex(canonical(list(parts)))[:16], 16))


def safe_div(num: float, den: float) -> Optional[float]:
    """Division that reports a zero denominator as None instead of raising or returning inf."""
    if den == 0:
        return None
    return num / den


def pct_reduction(baseline: float, proposed: float) -> Optional[float]:
    """100 * (baseline - proposed) / baseline. Baseline must be positive, else None."""
    if baseline is None or proposed is None or not baseline > 0:
        return None
    return 100.0 * (baseline - proposed) / baseline


def relative_increase_pct(baseline: float, proposed: float) -> Optional[float]:
    """Relative change of a rate, in percent of the baseline rate. None for a zero baseline."""
    if baseline is None or proposed is None or not baseline > 0:
        return None
    return 100.0 * (proposed - baseline) / baseline


def pp_change(baseline_rate: float, proposed_rate: float) -> Optional[float]:
    """Percentage-point difference between two rates given as fractions in [0, 1]."""
    if baseline_rate is None or proposed_rate is None:
        return None
    return 100.0 * (proposed_rate - baseline_rate)


def wilson_interval(k: int, n: int, z: float = 1.96) -> Optional[tuple[float, float]]:
    """Wilson score interval for a binomial proportion; None when n == 0."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    if len(values) == 0:
        return None
    return float(np.percentile(np.asarray(values, dtype=float), q))


def describe(values: Iterable[float]) -> dict:
    arr = np.asarray(list(values), dtype=float)
    if arr.size == 0:
        return {"n": 0, "mean": None, "median": None, "p95": None}
    return {"n": int(arr.size), "mean": float(arr.mean()), "median": float(np.median(arr)), "p95": float(np.percentile(arr, 95))}
