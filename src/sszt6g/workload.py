"""Edge sites, tenants, services and the seeded arrival process.

All quantities are simulated. Times are milliseconds on the simulation clock, never wall time.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .util import seeded_rng

UNAUTHORIZED_TENANT = "unregistered-tenant"


@dataclass(frozen=True)
class Site:
    site_id: str
    max_servers: int


@dataclass(frozen=True)
class ServiceSpec:
    """A tenant's inference service. work_ms is mean compute time of one job on one server."""

    service_id: str
    tenant_id: str
    work_ms: float
    deadline_ms: float
    max_queue: int
    allowed_tenants: tuple = ()


@dataclass
class Topology:
    sites: list
    services: list
    net_ms: dict  # ingress -> site -> one-way network delay (ms)
    interval_ms: int = 5000
    provision_ms: int = 4000
    migration_ms: int = 3000
    initial: dict = field(default_factory=dict)  # "service@site" -> servers
    tenant_quota: dict = field(default_factory=dict)  # tenant -> max servers in total
    background: dict = field(default_factory=dict)  # site -> servers held by other tenants

    def site(self, site_id: str) -> Site:
        for s in self.sites:
            if s.site_id == site_id:
                return s
        raise KeyError(site_id)

    def service(self, service_id: str) -> ServiceSpec:
        for s in self.services:
            if s.service_id == service_id:
                return s
        raise KeyError(service_id)

    def site_ids(self) -> list:
        return [s.site_id for s in self.sites]

    def net(self, ingress: str, site: str) -> float:
        return float(self.net_ms[ingress][site])

    def to_dict(self) -> dict:
        return {
            "sites": [{"site_id": s.site_id, "max_servers": s.max_servers} for s in self.sites],
            "services": [
                {
                    "service_id": s.service_id, "tenant_id": s.tenant_id, "work_ms": s.work_ms,
                    "deadline_ms": s.deadline_ms, "max_queue": s.max_queue, "allowed_tenants": list(s.allowed_tenants),
                }
                for s in self.services
            ],
            "net_ms": self.net_ms, "interval_ms": self.interval_ms, "provision_ms": self.provision_ms,
            "migration_ms": self.migration_ms, "initial": self.initial, "tenant_quota": self.tenant_quota,
            "background": self.background,
        }

    @staticmethod
    def from_dict(d: dict) -> "Topology":
        return Topology(
            sites=[Site(**s) for s in d["sites"]],
            services=[ServiceSpec(**{**s, "allowed_tenants": tuple(s.get("allowed_tenants", ()))}) for s in d["services"]],
            net_ms=d["net_ms"], interval_ms=int(d.get("interval_ms", 5000)), provision_ms=int(d.get("provision_ms", 4000)),
            migration_ms=int(d.get("migration_ms", 3000)), initial=dict(d.get("initial", {})),
            tenant_quota=dict(d.get("tenant_quota", {})), background=dict(d.get("background", {})),
        )


@dataclass(frozen=True)
class Job:
    """A data-plane inference request. It carries demand only: no label, no scenario name, no outcome."""

    job_id: str
    t_ms: float
    ingress: str
    tenant: str
    service: str
    work_ms: float
    deadline_ms: float


@dataclass(frozen=True)
class RateProfile:
    """Piecewise-linear arrival rate in jobs per second."""

    kind: str  # steady | ramp_burst | step_burst
    base: float
    peak: float = 0.0
    start_s: float = 0.0
    ramp_s: float = 0.0
    hold_s: float = 0.0
    decay_s: float = 0.0

    def rate(self, t_s: float) -> float:
        if self.kind == "steady":
            return self.base
        if self.kind == "step_burst":
            return self.peak if self.start_s <= t_s < self.start_s + self.hold_s else self.base
        if self.kind == "ramp_burst":
            t0, t1 = self.start_s, self.start_s + self.ramp_s
            t2 = t1 + self.hold_s
            t3 = t2 + self.decay_s
            if t_s < t0 or t_s >= t3:
                return self.base
            if t_s < t1:
                return self.base + (self.peak - self.base) * (t_s - t0) / max(self.ramp_s, 1e-9)
            if t_s < t2:
                return self.peak
            return self.peak - (self.peak - self.base) * (t_s - t2) / max(self.decay_s, 1e-9)
        raise ValueError(f"unknown profile kind {self.kind}")

    def max_rate(self) -> float:
        return max(self.base, self.peak)


@dataclass
class ProfileSpec:
    service: str
    ingress: str
    profile: RateProfile


@dataclass
class Scenario:
    scenario_id: str
    topology: Topology
    profiles: list
    horizon_ms: int
    seed: int
    unauthorized_frac: float = 0.01

    def describe(self) -> dict:
        return {
            "scenario_id": self.scenario_id, "seed": self.seed, "horizon_ms": self.horizon_ms,
            "profiles": [{"service": p.service, "ingress": p.ingress, **p.profile.__dict__} for p in self.profiles],
        }


def _sample_work(rng: np.random.Generator, mean_ms: float, sigma: float = 0.25) -> float:
    mu = math.log(mean_ms) - sigma * sigma / 2.0
    return float(rng.lognormal(mu, sigma))


def generate_jobs(sc: Scenario) -> list:
    """Seeded non-homogeneous Poisson arrivals by thinning. Same scenario and seed give identical jobs."""
    raw: list = []
    for spec in sc.profiles:
        rng = seeded_rng("arrivals", sc.seed, spec.service, spec.ingress)
        svc = sc.topology.service(spec.service)
        lam_max = spec.profile.max_rate()
        if lam_max <= 0:
            continue
        t_s = 0.0
        end_s = sc.horizon_ms / 1000.0
        while True:
            t_s += float(rng.exponential(1.0 / lam_max))
            if t_s >= end_s:
                break
            if rng.random() <= spec.profile.rate(t_s) / lam_max:
                tenant = UNAUTHORIZED_TENANT if rng.random() < sc.unauthorized_frac else svc.tenant_id
                raw.append((round(t_s * 1000.0, 3), spec.ingress, tenant, svc.service_id, _sample_work(rng, svc.work_ms), svc.deadline_ms))
    raw.sort(key=lambda r: (r[0], r[1], r[3]))
    return [Job(f"{sc.scenario_id}-{i:05d}", *r) for i, r in enumerate(raw)]


def default_topology() -> Topology:
    """Fictional two-site Edge-AI inference deployment used by the demo."""
    return Topology(
        sites=[Site("edge-east", 8), Site("edge-west", 8)],
        services=[
            ServiceSpec("vision-inference", "acme-video", work_ms=120.0, deadline_ms=400.0, max_queue=80, allowed_tenants=("acme-video",)),
            ServiceSpec("traffic-analytics", "city-traffic", work_ms=80.0, deadline_ms=400.0, max_queue=80, allowed_tenants=("city-traffic",)),
        ],
        net_ms={"edge-east": {"edge-east": 2.0, "edge-west": 9.0}, "edge-west": {"edge-east": 9.0, "edge-west": 2.0}},
        initial={"vision-inference@edge-east": 2, "traffic-analytics@edge-west": 2},
        tenant_quota={"acme-video": 6, "city-traffic": 8},
        background={"edge-east": 2, "edge-west": 4},
    )


def make_scenario(kind: str, seed: int, topology: Optional[Topology] = None, *, peak: float = 24.0, ramp_s: float = 20.0,
                  horizon_ms: int = 120_000, scenario_id: Optional[str] = None) -> Scenario:
    """Build a named scenario. Demand rates are per ingress site, in jobs per second."""
    topo = topology or default_topology()
    base = 6.0
    if kind == "steady_low":
        prof = RateProfile("steady", base=base)
    elif kind == "ramp_burst":
        prof = RateProfile("ramp_burst", base=base, peak=peak, start_s=40.0, ramp_s=ramp_s, hold_s=30.0, decay_s=10.0)
    elif kind == "step_burst":
        prof = RateProfile("step_burst", base=base, peak=peak, start_s=40.0, hold_s=50.0)
    else:
        raise ValueError(f"unknown scenario kind {kind}")
    profiles = [
        ProfileSpec("vision-inference", "edge-east", prof),
        ProfileSpec("traffic-analytics", "edge-west", RateProfile("steady", base=5.0)),
    ]
    return Scenario(scenario_id or f"{kind}-s{seed}", topo, profiles, horizon_ms, seed)
