"""Timestamped telemetry with an explicit observation cutoff.

A record stamped ts_ms summarises the half-open window [ts_ms - interval, ts_ms). A snapshot built for
cutoff t contains only records with ts_ms <= t, so nothing from the future can reach a planner.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from .errors import DataError
from .util import stable_id


@dataclass(frozen=True)
class TelemetryRecord:
    ts_ms: float
    service: str
    site: str
    ingress_arrivals: int  # jobs that entered the system at this site in the window (demand)
    routed_arrivals: int  # jobs routed to this site in the window (depends on placement)
    queue_depth: int  # jobs waiting (not yet started) at ts_ms
    allocated: int  # servers allocated to the service at this site at ts_ms
    busy: int  # servers busy at ts_ms

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    cutoff_ms: float
    latest_ts_ms: Optional[float]
    interval_ms: int
    records: tuple

    def is_empty(self) -> bool:
        return not self.records

    def age_ms(self, now_ms: float) -> Optional[float]:
        return None if self.latest_ts_ms is None else now_ms - self.latest_ts_ms

    def arrivals(self, service: str, ingress: str) -> list:
        """Demand history at an ingress site, oldest first."""
        return [r.ingress_arrivals for r in self.records if r.service == service and r.site == ingress]

    def latest(self, service: str, site: str) -> Optional[TelemetryRecord]:
        found = [r for r in self.records if r.service == service and r.site == site]
        return found[-1] if found else None

    def to_dict(self) -> dict:
        return {"snapshot_id": self.snapshot_id, "cutoff_ms": self.cutoff_ms, "latest_ts_ms": self.latest_ts_ms,
                "records": [r.to_dict() for r in self.records]}


class TelemetryStore:
    """Append-only, time-ordered store."""

    def __init__(self, interval_ms: int):
        self.interval_ms = interval_ms
        self._records: list = []

    def append(self, rec: TelemetryRecord) -> None:
        if self._records and rec.ts_ms < self._records[-1].ts_ms:
            raise DataError(f"out-of-order telemetry: {rec.ts_ms} < {self._records[-1].ts_ms}")
        self._records.append(rec)

    def extend(self, recs) -> None:
        for r in recs:
            self.append(r)

    def __len__(self) -> int:
        return len(self._records)

    def all(self) -> list:
        return list(self._records)

    def snapshot(self, cutoff_ms: float, lookback_intervals: int = 12) -> Snapshot:
        """Everything observable at the cutoff, limited to the last lookback_intervals intervals."""
        lo = cutoff_ms - lookback_intervals * self.interval_ms
        recs = tuple(r for r in self._records if lo < r.ts_ms <= cutoff_ms)
        latest = recs[-1].ts_ms if recs else None
        sid = stable_id("snap", cutoff_ms, [r.to_dict() for r in recs])
        return Snapshot(sid, cutoff_ms, latest, self.interval_ms, recs)
