"""Planners: fixed placement, threshold-based reactive, predictive closed loop.

Reactive and predictive share the SAME trigger thresholds and sizing rule. They differ only in the demand
estimate: the last observed interval (reactive) versus the forecast of the next interval (predictive).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .simulator import Cluster
from .telemetry import Snapshot
from .workload import Topology


@dataclass(frozen=True)
class PlannerConfig:
    target_util: float = 0.7  # size capacity so expected utilisation is at most this
    trigger_util: float = 0.8  # act only when estimated utilisation exceeds this
    queue_trigger: float = 6.0  # ...or when queued jobs per server exceed this
    max_step: int = 3  # servers added per decision


@dataclass(frozen=True)
class PlannedChange:
    kind: str  # retain | reserve | move
    service: str
    site_to: Optional[str]
    site_from: Optional[str]
    servers: int
    reason: str
    evidence: dict = field(default_factory=dict)


@dataclass
class PlanContext:
    now_ms: float
    snapshot: Snapshot
    topo: Topology
    cluster: Cluster
    forecasts: dict = field(default_factory=dict)  # (service, ingress) -> Forecast


def required_servers(rate_per_s: float, work_ms: float, target_util: float) -> int:
    return max(1, math.ceil(rate_per_s * work_ms / 1000.0 / target_util))


class Planner:
    name = "base"
    uses_forecast = False

    def __init__(self, cfg: Optional[PlannerConfig] = None):
        self.cfg = cfg or PlannerConfig()

    def demand(self, ctx: PlanContext, service: str, ingress: str) -> float:
        raise NotImplementedError

    def hot_ingress(self, ctx: PlanContext, service: str) -> str:
        rates = {s: self.demand(ctx, service, s) for s in ctx.topo.site_ids()}
        return max(sorted(rates), key=lambda s: rates[s])

    # ------------------------------------------------------------ closed loop
    def plan(self, ctx: PlanContext) -> list:
        out: list = []
        for spec in ctx.topo.services:
            out.append(self._plan_service(ctx, spec))
        return out

    def _plan_service(self, ctx: PlanContext, spec) -> PlannedChange:
        svc, cfg, cl = spec.service_id, self.cfg, ctx.cluster
        rates = {s: self.demand(ctx, svc, s) for s in ctx.topo.site_ids()}
        total = sum(rates.values())
        have = cl.allocated_total(svc) + cl.pending_adds(service=svc)
        util = math.inf if have == 0 else total * spec.work_ms / 1000.0 / have
        queue = sum((ctx.snapshot.latest(svc, s).queue_depth if ctx.snapshot.latest(svc, s) else 0) for s in ctx.topo.site_ids())
        ev = {"snapshot_id": ctx.snapshot.snapshot_id, "demand_jobs_per_s": round(total, 3), "have": have,
              "est_utilisation": None if math.isinf(util) else round(util, 3), "queue_depth": queue,
              "forecast_ids": [f.forecast_id for (s, _i), f in ctx.forecasts.items() if s == svc]}
        if util <= cfg.trigger_util and queue / max(1, have) <= cfg.queue_trigger:
            return PlannedChange("retain", svc, None, None, 0, "below_trigger", ev)
        need = required_servers(total, spec.work_ms, cfg.target_util)
        add = need - have
        ev["need"] = need
        if add <= 0:
            return PlannedChange("retain", svc, None, None, 0, "capacity_sufficient", ev)
        hot = max(sorted(rates), key=lambda s: rates[s])
        quota_left = ctx.topo.tenant_quota.get(spec.tenant_id, 0) - cl.tenant_total(spec.tenant_id)
        n = min(add, cfg.max_step, quota_left)
        if n <= 0:
            return self._try_move(ctx, spec, rates, hot, min(add, cfg.max_step), ev)
        if cl.free(hot) >= 1:
            m = min(n, cl.free(hot))
            return PlannedChange("reserve", svc, hot, None, m, "scale_up_at_ingress_site", ev)
        for other in sorted((s for s in ctx.topo.site_ids() if s != hot), key=lambda s: ctx.topo.net(hot, s)):
            if cl.free(other) >= 1:
                return PlannedChange("reserve", svc, other, None, min(n, cl.free(other)), "ingress_site_full_remote_site", ev)
        return PlannedChange("retain", svc, None, None, 0, "no_capacity_anywhere", ev)

    def _try_move(self, ctx: PlanContext, spec, rates: dict, hot: str, want: int, ev: dict) -> PlannedChange:
        """Quota is binding: relocate servers from a site with surplus to the hot ingress site."""
        svc, cl = spec.service_id, ctx.cluster
        for src in sorted((s for s in ctx.topo.site_ids() if s != hot), key=lambda s: ctx.topo.net(hot, s)):
            keep = required_servers(rates[src], spec.work_ms, self.cfg.target_util) if rates[src] > 0 else 0
            surplus = cl.allocated(svc, src) - keep
            m = min(surplus, want, cl.free(hot))
            if m >= 1:
                return PlannedChange("move", svc, hot, src, m, "quota_binding_move_surplus", ev)
        return PlannedChange("retain", svc, None, None, 0, "quota_binding_no_surplus", ev)

    # ------------------------------------------------------------ tenant intents
    def place_intent(self, ctx: PlanContext, service: str, servers: int, site_pref: Optional[str]) -> Optional[PlannedChange]:
        """Choose a site for a tenant's reserve intent. Fixed placement never leaves the preferred site."""
        cl = ctx.cluster
        pref = site_pref or ctx.topo.site_ids()[0]
        if cl.free(pref) >= servers:
            return PlannedChange("reserve", service, pref, None, servers, "preferred_site", {"snapshot_id": ctx.snapshot.snapshot_id})
        return None


class FixedPlanner(Planner):
    name = "fixed"

    def demand(self, ctx, service, ingress):
        return 0.0

    def plan(self, ctx):
        return [PlannedChange("retain", s.service_id, None, None, 0, "fixed_placement", {"snapshot_id": ctx.snapshot.snapshot_id}) for s in ctx.topo.services]


class ReactivePlanner(Planner):
    name = "reactive"

    def demand(self, ctx, service, ingress):
        rec = ctx.snapshot.latest(service, ingress)
        return 0.0 if rec is None else rec.ingress_arrivals / (ctx.topo.interval_ms / 1000.0)

    def place_intent(self, ctx, service, servers, site_pref):
        first = super().place_intent(ctx, service, servers, site_pref)
        if first:
            return first
        for s in sorted(ctx.topo.site_ids(), key=lambda s: (ctx.topo.net(site_pref or ctx.topo.site_ids()[0], s), s)):
            if ctx.cluster.free(s) >= servers:
                return PlannedChange("reserve", service, s, None, servers, "fallback_nearest_site_with_capacity", {"snapshot_id": ctx.snapshot.snapshot_id})
        return None


class PredictivePlanner(Planner):
    name = "predictive"
    uses_forecast = True

    def demand(self, ctx, service, ingress):
        f = ctx.forecasts.get((service, ingress))
        if f is not None:
            return f.predicted_arrivals / (ctx.topo.interval_ms / 1000.0)
        rec = ctx.snapshot.latest(service, ingress)  # no forecast available: fall back to the observed interval
        return 0.0 if rec is None else rec.ingress_arrivals / (ctx.topo.interval_ms / 1000.0)

    def place_intent(self, ctx, service, servers, site_pref):
        first = super().place_intent(ctx, service, servers, site_pref)
        if first:
            return first
        def predicted_load(site: str) -> float:  # least predicted ingress demand first
            return self.demand(ctx, service, site)
        for s in sorted(ctx.topo.site_ids(), key=lambda s: (predicted_load(s), s)):
            if ctx.cluster.free(s) >= servers:
                return PlannedChange("reserve", service, s, None, servers, "fallback_lowest_predicted_load", {"snapshot_id": ctx.snapshot.snapshot_id})
        return None


PLANNERS = {"fixed": FixedPlanner, "reactive": ReactivePlanner, "predictive": PredictivePlanner}


def make_planner(name: str, cfg: Optional[PlannerConfig] = None) -> Planner:
    return PLANNERS[name](cfg)
