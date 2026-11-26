"""Seeded discrete-event simulator of a two-site edge inference deployment.

Inference is EMULATED: a job occupies one simulated server for its sampled work_ms; no model is executed.
Simulated time (network + queue + compute) is kept apart from the measured Python wall time, which is
recorded separately by the benchmarks.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from .errors import CapacityError
from .telemetry import TelemetryRecord, TelemetryStore
from .workload import Job, Topology

TAIL_MS = 60_000  # jobs still unfinished this long after the last arrival window are "unfinished"
TICK_TAIL_MS = 30_000  # control ticks continue this long after the last arrival so late actions can be verified


@dataclass
class Pending:
    action_id: str
    activate_ms: float
    adds: list  # [(service, site, n)]
    removes: list


class Cluster:
    """Simulated capacity and placement. The only way capacity changes is apply() -> activate_due().

    Invariant: for every site, background + allocated + pending adds <= capacity.
    apply() is idempotent per action id, so a retry or a duplicate cannot allocate twice.
    """

    def __init__(self, topo: Topology, lost_fn: Optional[Callable[[str], bool]] = None):
        self.topo = topo
        self.capacity = {s.site_id: s.max_servers for s in topo.sites}
        self.background = dict(topo.background)
        self.servers: dict = {}
        self.pending: dict = {}
        self.history: dict = {}
        self.lost_fn = lost_fn or (lambda _a: False)
        for key, n in topo.initial.items():
            svc, site = key.split("@")
            self.servers[(svc, site)] = [[0.0, False] for _ in range(int(n))]

    # ---- views ----
    def allocated(self, service: str, site: str) -> int:
        return sum(1 for s in self.servers.get((service, site), []) if not s[1])

    def allocated_total(self, service: str) -> int:
        return sum(self.allocated(svc, site) for (svc, site) in list(self.servers) if svc == service)

    def pending_adds(self, site: Optional[str] = None, service: Optional[str] = None) -> int:
        return sum(n for p in self.pending.values() for (svc, s, n) in p.adds
                   if (site is None or s == site) and (service is None or svc == service))

    def occupied(self, site: str) -> int:
        held = sum(self.allocated(svc, s) for (svc, s) in list(self.servers) if s == site)
        return self.background.get(site, 0) + held + self.pending_adds(site=site)

    def free(self, site: str) -> int:
        return self.capacity[site] - self.occupied(site)

    def tenant_total(self, tenant: str) -> int:
        total = 0
        for spec in self.topo.services:
            if spec.tenant_id == tenant:
                total += self.allocated_total(spec.service_id) + self.pending_adds(service=spec.service_id)
        return total

    def status(self, action_id: str) -> Optional[str]:
        h = self.history.get(action_id)
        return None if h is None else h["status"]

    def next_activation(self) -> float:
        return min((p.activate_ms for p in self.pending.values()), default=math.inf)

    # ---- control plane ----
    def check(self, adds: list, removes: list) -> tuple:
        need: dict = {}
        for (_svc, site, n) in adds:
            need[site] = need.get(site, 0) + n
        for site, n in need.items():
            if n > self.free(site):
                return False, "capacity_unavailable"
        for (svc, site, n) in removes:
            if self.allocated(svc, site) < n:
                return False, "nothing_to_remove"
        return True, ""

    def apply(self, action_id: str, adds: list, removes: list, now_ms: float, delay_ms: float) -> dict:
        if action_id in self.history:  # idempotent: a duplicate or retry is a no-op
            return dict(self.history[action_id], duplicate=True)
        ok, why = self.check(adds, removes)
        if not ok:
            raise CapacityError(why)
        self.pending[action_id] = Pending(action_id, now_ms + delay_ms, list(adds), list(removes))
        self.history[action_id] = {"status": "pending", "activate_ms": now_ms + delay_ms, "adds": list(adds), "removes": list(removes)}
        return dict(self.history[action_id], duplicate=False)

    def _purge(self, now_ms: float) -> None:
        for key in list(self.servers):
            self.servers[key] = [s for s in self.servers[key] if not (s[1] and s[0] <= now_ms)]

    def _retire(self, service: str, site: str, n: int) -> None:
        live = sorted((s for s in self.servers.get((service, site), []) if not s[1]), key=lambda s: s[0])
        for s in live[:n]:
            s[1] = True

    def activate_due(self, now_ms: float) -> list:
        """Activate every pending change due at or before now. Returns the activated action ids."""
        done = []
        for aid in sorted((a for a, p in self.pending.items() if p.activate_ms <= now_ms), key=lambda a: (self.pending[a].activate_ms, a)):
            p = self.pending.pop(aid)
            if self.lost_fn(aid):  # injected fault: the change silently does not happen
                self.history[aid]["status"] = "lost"
                done.append(aid)
                continue
            for (svc, site, n) in p.adds:
                self.servers.setdefault((svc, site), []).extend([[p.activate_ms, False] for _ in range(n)])
            for (svc, site, n) in p.removes:
                self._retire(svc, site, n)
            self.history[aid]["status"] = "active"
            done.append(aid)
        self._purge(now_ms)
        return done

    def rollback(self, action_id: str) -> str:
        """Undo a change: cancel it if pending, reverse it if active, no-op if lost. Idempotent."""
        h = self.history.get(action_id)
        if h is None or h["status"] == "rolled_back":
            return "noop"
        if action_id in self.pending:
            del self.pending[action_id]
        elif h["status"] == "active":
            for (svc, site, n) in h["adds"]:
                self._retire(svc, site, n)
            for (svc, site, n) in h["removes"]:
                self.servers.setdefault((svc, site), []).extend([[0.0, False] for _ in range(n)])
        h["status"] = "rolled_back"
        return "rolled_back"

    # ---- persistence ----
    def to_state(self) -> dict:
        return {
            "servers": {f"{svc}@{site}": lst for (svc, site), lst in self.servers.items()},
            "pending": {a: {"activate_ms": p.activate_ms, "adds": p.adds, "removes": p.removes} for a, p in self.pending.items()},
            "history": self.history,
        }

    @staticmethod
    def from_state(topo: Topology, state: dict, lost_fn=None) -> "Cluster":
        c = Cluster(topo, lost_fn)
        c.servers = {tuple(k.split("@")): [list(s) for s in v] for k, v in state["servers"].items()}
        c.pending = {a: Pending(a, p["activate_ms"], [tuple(x) for x in p["adds"]], [tuple(x) for x in p["removes"]]) for a, p in state["pending"].items()}
        c.history = {a: dict(h, adds=[tuple(x) for x in h["adds"]], removes=[tuple(x) for x in h["removes"]]) for a, h in state["history"].items()}
        return c


@dataclass
class JobRecord:
    job_id: str
    t_ms: float
    service: str
    ingress: str
    site: str
    status: str  # completed | dropped | denied | unfinished
    reason: str
    net_ms: float
    queue_ms: float
    compute_ms: float
    latency_ms: float
    deadline_ms: float
    deadline_missed: bool

    def to_row(self) -> dict:
        return dict(self.__dict__)


class Controller(Protocol):
    def on_tick(self, sim: "Simulator", now_ms: float) -> None: ...


@dataclass
class SimResult:
    records: list
    cost_server_ms: dict
    horizon_ms: int
    end_ms: float
    telemetry: TelemetryStore = field(repr=False, default=None)

    def by_status(self) -> dict:
        out: dict = {}
        for r in self.records:
            out[r.status] = out.get(r.status, 0) + 1
        return out


class Simulator:
    def __init__(self, topo: Topology, cluster: Cluster, horizon_ms: int, controller: Optional[Controller] = None):
        self.topo = topo
        self.cluster = cluster
        self.horizon_ms = horizon_ms
        self.controller = controller
        self.telemetry = TelemetryStore(topo.interval_ms)
        self.now_ms = 0.0
        self._waiting: dict = {}
        self._win_ingress: dict = {}
        self._win_routed: dict = {}
        self._cost: dict = {s.service_id: 0.0 for s in topo.services}
        self._last_acc = 0.0

    # ---- accounting ----
    def _accrue(self, t: float) -> None:
        t = min(t, float(self.horizon_ms))
        if t <= self._last_acc:
            return
        for svc in self._cost:
            self._cost[svc] += self.cluster.allocated_total(svc) * (t - self._last_acc)
        self._last_acc = t

    def _prune(self, now_ms: float) -> None:
        """Forget jobs that have started by the event time. Events are processed in time order."""
        for key, lst in self._waiting.items():
            lst[:] = [(a, s) for (a, s) in lst if s > now_ms]

    def _queue_depth(self, service: str, site: str, t: float) -> int:
        return sum(1 for (a, s) in self._waiting.get((service, site), []) if a <= t < s)

    # ---- telemetry ----
    def _emit_telemetry(self, t: float) -> None:
        for spec in self.topo.services:
            for site in self.topo.site_ids():
                servers = [s for s in self.cluster.servers.get((spec.service_id, site), []) if not s[1]]
                self.telemetry.append(TelemetryRecord(
                    ts_ms=t, service=spec.service_id, site=site,
                    ingress_arrivals=self._win_ingress.get((spec.service_id, site), 0),
                    routed_arrivals=self._win_routed.get((spec.service_id, site), 0),
                    queue_depth=self._queue_depth(spec.service_id, site, t),
                    allocated=len(servers), busy=sum(1 for s in servers if s[0] > t)))
        self._win_ingress.clear()
        self._win_routed.clear()

    # ---- data plane ----
    def _route(self, job: Job, rng_unused=None) -> JobRecord:
        spec = self.topo.service(job.service)
        t = job.t_ms
        self._win_ingress[(job.service, job.ingress)] = self._win_ingress.get((job.service, job.ingress), 0) + 1
        if job.tenant not in spec.allowed_tenants:
            return JobRecord(job.job_id, t, job.service, job.ingress, "", "denied", "tenant_not_authorized", 0, 0, 0, 0, job.deadline_ms, False)
        best = None
        for site in self.topo.site_ids():
            live = [s for s in self.cluster.servers.get((job.service, site), []) if not s[1]]
            if not live:
                continue
            net = self.topo.net(job.ingress, site)
            arr = t + net
            est = max(arr, min(s[0] for s in live)) + spec.work_ms + net
            if best is None or (est, site) < (best[0], best[1]):
                best = (est, site, net, arr, live)
        if best is None:
            return JobRecord(job.job_id, t, job.service, job.ingress, "", "dropped", "no_capacity", 0, 0, 0, 0, job.deadline_ms, False)
        _, site, net, arr, live = best
        if self._queue_depth(job.service, site, arr) >= spec.max_queue:
            return JobRecord(job.job_id, t, job.service, job.ingress, site, "dropped", "queue_full", net, 0, 0, 0, job.deadline_ms, False)
        server = min(live, key=lambda s: s[0])
        start = max(arr, server[0])
        finish = start + job.work_ms
        server[0] = finish
        self._waiting.setdefault((job.service, site), []).append((arr, start))
        self._win_routed[(job.service, site)] = self._win_routed.get((job.service, site), 0) + 1
        completion = finish + net
        latency = completion - t
        end_ms = self.horizon_ms + TAIL_MS
        if completion > end_ms:
            return JobRecord(job.job_id, t, job.service, job.ingress, site, "unfinished", "past_horizon", net, start - arr, job.work_ms, latency, job.deadline_ms, True)
        return JobRecord(job.job_id, t, job.service, job.ingress, site, "completed", "", 2 * net, start - arr, job.work_ms, latency, job.deadline_ms, latency > job.deadline_ms)

    # ---- main loop ----
    def run(self, jobs: list) -> SimResult:
        interval = self.topo.interval_ms
        next_tick = float(interval)
        i, n = 0, len(jobs)
        records: list = []
        while True:
            t_job = jobs[i].t_ms if i < n else math.inf
            t_act = self.cluster.next_activation()
            t = min(t_job, next_tick, t_act)
            if t == math.inf or (i >= n and next_tick > self.horizon_ms + TICK_TAIL_MS and t_act == math.inf):
                break
            self.now_ms = t
            self._prune(t)
            if t_act == t:
                self._accrue(t)
                self.cluster.activate_due(t)
            elif next_tick == t:
                self._accrue(t)
                self.cluster.activate_due(t)
                self._emit_telemetry(t)
                if self.controller is not None:
                    self.controller.on_tick(self, t)
                next_tick += interval
            else:
                records.append(self._route(jobs[i]))
                i += 1
        self._accrue(float(self.horizon_ms))
        return SimResult(records, dict(self._cost), self.horizon_ms, self.horizon_ms + TAIL_MS, self.telemetry)
