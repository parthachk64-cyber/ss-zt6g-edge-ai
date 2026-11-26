"""observe -> predict -> plan -> authorize -> apply -> verify, with an audit receipt per state.

State per action (persisted in SQLite so a restart cannot allocate capacity twice):
  received -> proposed -> authorized -> committed -> applied -> verified
  with terminal side exits: denied, failed(reason), expired, rolled_back -> escalated.

Ordering guarantees:
  * the `committed` receipt is written to the ledger BEFORE capacity changes (write-ahead): if the ledger
    cannot record it, nothing is applied;
  * apply is idempotent per action id (see Cluster.apply), so recovery after a crash between commit and
    apply, and a duplicate delivery, each change capacity at most once.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .errors import BenchmarkAdapterError, CapacityError, CrashError, EmptyHistoryError, LedgerError
from .prediction import forecast_from_snapshot
from .planners import PlanContext, Planner
from .simulator import Cluster
from .trust import Decision, Keyring, PolicyStore, Request, evaluate
from .util import stable_id
from .workload import Topology

TERMINAL = {"verified", "denied", "failed", "expired", "escalated"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions(
  action_id TEXT PRIMARY KEY, intent_id TEXT UNIQUE NOT NULL, state TEXT NOT NULL, reason TEXT, origin TEXT,
  tenant TEXT, service TEXT, kind TEXT, servers INTEGER, site_to TEXT, site_from TEXT, sla_ms REAL,
  submitted_ms REAL, expires_ms REAL, activate_ms REAL, decided_ms REAL, applied_ms REAL, verified_ms REAL,
  decision TEXT, receipts TEXT, intent TEXT, audit_pending TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


@dataclass
class ProvisioningIntent:
    intent_id: str
    tenant: str
    service: str
    kind: str
    servers: int
    site_to: Optional[str]
    site_from: Optional[str]
    sla_ms: float
    submitted_ms: float
    token: dict
    policy_revision: int = 1
    origin: str = "tenant"  # tenant | closed_loop

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @staticmethod
    def from_dict(d: dict) -> "ProvisioningIntent":
        return ProvisioningIntent(**{k: d[k] for k in ProvisioningIntent.__dataclass_fields__})


@dataclass
class ActionResult:
    action_id: str
    intent_id: str
    state: str
    reason: Optional[str]
    duplicate: bool
    submitted_ms: float
    verified_ms: Optional[float]
    decision: Optional[dict] = None
    receipts: dict = field(default_factory=dict)
    activate_ms: Optional[float] = None
    origin: str = "tenant"

    def to_wire_body(self) -> dict:
        return {"action_id": self.action_id, "intent_id": self.intent_id, "state": self.state, "reason": self.reason,
                "duplicate": self.duplicate, "submitted_ms": self.submitted_ms, "verified_ms": self.verified_ms}


@dataclass
class OrchConfig:
    ttl_ms: float = 30_000.0  # an authorised action not applied within this window expires
    max_snapshot_age_ms: float = 15_000.0  # telemetry older than this is stale
    sla_ms: float = 20_000.0


class StateStore:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def get(self, action_id: str):
        return self.conn.execute("SELECT * FROM actions WHERE action_id=?", (action_id,)).fetchone()

    def get_by_intent(self, intent_id: str):
        return self.conn.execute("SELECT * FROM actions WHERE intent_id=?", (intent_id,)).fetchone()

    def insert(self, action_id: str, it: ProvisioningIntent, now_ms: float, expires_ms: float) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO actions(action_id,intent_id,state,origin,tenant,service,kind,servers,site_to,site_from,sla_ms,submitted_ms,expires_ms,receipts,intent)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (action_id, it.intent_id, "received", it.origin, it.tenant, it.service, it.kind, it.servers, it.site_to, it.site_from,
                 it.sla_ms, it.submitted_ms, expires_ms, "{}", json.dumps(it.to_dict(), sort_keys=True)))

    def update(self, action_id: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn:
            self.conn.execute(f"UPDATE actions SET {cols} WHERE action_id=?", (*fields.values(), action_id))

    def rows(self, states=None) -> list:
        if states is None:
            return self.conn.execute("SELECT * FROM actions ORDER BY submitted_ms, action_id").fetchall()
        q = ",".join("?" * len(states))
        return self.conn.execute(f"SELECT * FROM actions WHERE state IN ({q}) ORDER BY submitted_ms, action_id", tuple(states)).fetchall()

    def kv_get(self, k: str):
        r = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return None if r is None else json.loads(r["v"])

    def kv_set(self, k: str, v) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v, sort_keys=True)))


def deltas(it: ProvisioningIntent) -> tuple:
    if it.kind == "reserve":
        return [(it.service, it.site_to, it.servers)], []
    if it.kind == "move":
        return [(it.service, it.site_to, it.servers)], [(it.service, it.site_from, it.servers)]
    return [], []


class Orchestrator:
    def __init__(self, topo: Topology, cluster: Optional[Cluster], policy: PolicyStore, keyring: Keyring, ledger, db_path: str = ":memory:",
                 cfg: Optional[OrchConfig] = None, evaluator: Optional[Callable] = None, allow_benchmark_adapters: bool = False):
        for adapter in (ledger, evaluator):
            if adapter is not None and getattr(adapter, "benchmark_only", False) and not allow_benchmark_adapters:
                raise BenchmarkAdapterError(f"{type(adapter).__name__} is benchmark-only and needs allow_benchmark_adapters=True")
        self.topo, self.policy, self.keyring, self.ledger = topo, policy, keyring, ledger
        self.cfg = cfg or OrchConfig()
        self.evaluator = evaluator or evaluate
        self.store = StateStore(db_path)
        if cluster is None:  # reopen after a restart: capacity state comes from the same database as the actions
            state = self.store.kv_get("cluster")
            cluster = Cluster.from_state(topo, state) if state else Cluster(topo)
        self.cluster = cluster
        self.timings: list = []
        self.crash_at: Optional[str] = None

    # ------------------------------------------------------------------ helpers
    def _time(self, stage: str, t0: int) -> None:
        self.timings.append({"stage": stage, "ns": time.perf_counter_ns() - t0})

    def _crash(self, point: str) -> None:
        if self.crash_at == point:
            self.crash_at = None
            raise CrashError(point)

    def persist_cluster(self) -> None:
        self.store.kv_set("cluster", self.cluster.to_state())

    def _result(self, row, duplicate: bool = False) -> ActionResult:
        return ActionResult(row["action_id"], row["intent_id"], row["state"], row["reason"], duplicate, row["submitted_ms"],
                            row["verified_ms"], json.loads(row["decision"]) if row["decision"] else None,
                            json.loads(row["receipts"] or "{}"), row["activate_ms"], row["origin"])

    def result(self, action_id: str) -> ActionResult:
        return self._result(self.store.get(action_id))

    def _record(self, action_id: str, state: str, payload: dict) -> dict:
        t0 = time.perf_counter_ns()
        receipt = self.ledger.append({"action_id": action_id, "state": state, **payload})
        self._time("ledger_append", t0)
        row = self.store.get(action_id)
        receipts = json.loads(row["receipts"] or "{}")
        receipts[state] = receipt.to_dict()
        self.store.update(action_id, receipts=json.dumps(receipts, sort_keys=True))
        return receipt.to_dict()

    def _try_record(self, action_id: str, state: str, payload: dict) -> None:
        try:
            self._record(action_id, state, payload)
        except LedgerError:
            self.store.update(action_id, audit_pending=state)

    def _payload(self, it: ProvisioningIntent, now_ms: float, **extra) -> dict:
        return {"intent_id": it.intent_id, "origin": it.origin, "tenant": it.tenant, "service": it.service, "kind": it.kind,
                "servers": it.servers, "site_to": it.site_to, "site_from": it.site_from, "sim_ms": now_ms, **extra}

    def _fail(self, action_id: str, reason: str, now_ms: float, record: bool = True) -> ActionResult:
        self.store.update(action_id, state="failed", reason=reason, decided_ms=now_ms)
        if record:
            self._try_record(action_id, "failed", {"reason": reason, "sim_ms": now_ms})
        return self._result(self.store.get(action_id))

    def last_action_ms(self, service: str, origin: str) -> Optional[float]:
        r = self.store.conn.execute("SELECT MAX(applied_ms) m FROM actions WHERE service=? AND origin=? AND applied_ms IS NOT NULL", (service, origin)).fetchone()
        return None if r is None or r["m"] is None else r["m"]

    # ------------------------------------------------------------------ pipeline
    def submit(self, it: ProvisioningIntent, now_ms: float, snapshot=None, extra_evidence: Optional[dict] = None) -> ActionResult:
        t_all = time.perf_counter_ns()
        existing = self.store.get_by_intent(it.intent_id)
        if existing is not None:  # replay-safe: a duplicate returns the first outcome and changes nothing
            return self._result(existing, duplicate=True)
        action_id = stable_id("act", it.intent_id)
        self.store.insert(action_id, it, now_ms, now_ms + self.cfg.ttl_ms)
        evidence = extra_evidence or {}
        if snapshot is not None:
            age = snapshot.age_ms(now_ms)
            if age is None or age > self.cfg.max_snapshot_age_ms:
                return self._fail(action_id, "stale_telemetry", now_ms)
            evidence = {**evidence, "snapshot_id": snapshot.snapshot_id}
        try:
            self._record(action_id, "proposed", self._payload(it, now_ms, evidence=evidence))
        except LedgerError:
            return self._fail(action_id, "audit_failed", now_ms, record=False)  # nothing was applied
        self.store.update(action_id, state="proposed")

        adds, removes = deltas(it)
        spec = self.topo.service(it.service)
        total_after = self.cluster.tenant_total(spec.tenant_id) + sum(n for *_a, n in adds) - sum(n for *_a, n in removes)
        req = Request(it.intent_id, it.token, it.kind, it.service, it.site_to or it.site_from or "", spec.tenant_id, total_after, now_ms, it.policy_revision)
        t0 = time.perf_counter_ns()
        decision: Decision = self.evaluator(req, self.policy, now_ms, self.keyring)
        self._time("authorize", t0)
        self.store.update(action_id, decision=json.dumps(decision.to_dict(), sort_keys=True), decided_ms=now_ms)
        if not decision.allow:
            self.store.update(action_id, state="denied", reason=decision.reasons[0])
            self._try_record(action_id, "denied", {"reasons": decision.reasons, "decision_id": decision.decision_id, "sim_ms": now_ms})
            self._time("submit_total", t_all)
            return self._result(self.store.get(action_id))
        self.store.update(action_id, state="authorized")
        try:
            self._record(action_id, "authorized", {"decision_id": decision.decision_id, "evidence_ids": decision.evidence_ids, "sim_ms": now_ms})
        except LedgerError:
            return self._fail(action_id, "audit_failed", now_ms, record=False)

        ok, why = self.cluster.check(adds, removes)
        if not ok:
            self._time("submit_total", t_all)
            return self._fail(action_id, why, now_ms)
        out = self._commit_then_apply(action_id, it, now_ms)
        self._time("submit_total", t_all)
        return out

    def _commit_then_apply(self, action_id: str, it: ProvisioningIntent, now_ms: float) -> ActionResult:
        adds, removes = deltas(it)
        try:
            if self.ledger.find_state(action_id, "committed") is None:
                self._record(action_id, "committed", {"adds": adds, "removes": removes, "sim_ms": now_ms})
        except LedgerError:
            return self._fail(action_id, "audit_failed", now_ms, record=False)  # audit failure BEFORE action
        self.store.update(action_id, state="committed")
        self._crash("after_commit")
        return self._apply(action_id, it, now_ms)

    def _apply(self, action_id: str, it: ProvisioningIntent, now_ms: float) -> ActionResult:
        row = self.store.get(action_id)
        if now_ms > row["expires_ms"]:
            self.store.update(action_id, state="expired", reason="expired_before_apply")
            self._try_record(action_id, "expired", {"sim_ms": now_ms})
            return self._result(self.store.get(action_id))
        adds, removes = deltas(it)
        delay = self.topo.provision_ms + (self.topo.migration_ms if it.kind == "move" else 0)
        try:
            info = self.cluster.apply(action_id, adds, removes, now_ms, delay)
        except CapacityError as e:
            return self._fail(action_id, str(e), now_ms)
        self.persist_cluster()
        self.store.update(action_id, state="applied", applied_ms=now_ms, activate_ms=info["activate_ms"])
        self._try_record(action_id, "applied", {"activate_ms": info["activate_ms"], "sim_ms": now_ms})
        return self._result(self.store.get(action_id))

    def verify_pending(self, now_ms: float) -> list:
        """Verify applied actions against the next observed service state; bounded rollback then escalate."""
        done = []
        self.cluster.activate_due(now_ms)
        for row in self.store.rows(["applied"]):
            if row["activate_ms"] is None or row["activate_ms"] > now_ms:
                continue
            aid = row["action_id"]
            t0 = time.perf_counter_ns()
            if self.cluster.status(aid) == "active":
                self.store.update(aid, state="verified", verified_ms=now_ms)
                self._try_record(aid, "verified", {"observed": "active", "sim_ms": now_ms})
            else:
                self.cluster.rollback(aid)
                self.store.update(aid, state="rolled_back", reason="verification_failed")
                self._try_record(aid, "rolled_back", {"observed": self.cluster.status(aid), "sim_ms": now_ms})
                self.store.update(aid, state="escalated")
                self._try_record(aid, "escalated", {"to": "human_operator_simulated", "sim_ms": now_ms})
            self._time("verify", t0)
            done.append(aid)
        self.persist_cluster()
        return done

    def recover(self, now_ms: float) -> list:
        """Resume interrupted actions after a restart. Safe to call repeatedly."""
        out = []
        for row in self.store.rows(["authorized", "committed"]):
            it = ProvisioningIntent.from_dict(json.loads(row["intent"]))
            out.append(self._commit_then_apply(row["action_id"], it, now_ms) if row["state"] == "authorized" else self._apply(row["action_id"], it, now_ms))
        for row in self.store.rows(["received", "proposed"]):  # died before authorisation: nothing was applied
            out.append(self._fail(row["action_id"], "interrupted_before_authorization", now_ms))
        for row in self.store.conn.execute("SELECT * FROM actions WHERE audit_pending IS NOT NULL").fetchall():
            self._try_record(row["action_id"], row["audit_pending"], {"recovered": True, "sim_ms": now_ms})
            self.store.update(row["action_id"], audit_pending=None)
        return out

    def close(self) -> None:
        self.store.close()


class ClosedLoopController:
    """Glue for the simulator: each tick runs observe -> predict -> plan and submits through the orchestrator."""

    def __init__(self, orch: Orchestrator, planner: Planner, predictor, tokens: dict, policy_revision: int = 1,
                 tenant_intents: Optional[list] = None, cooldown_ms: float = 10_000.0, lookback_intervals: int = 12):
        self.orch, self.planner, self.predictor = orch, planner, predictor
        self.tokens, self.policy_revision = tokens, policy_revision
        self.pending_intents = sorted(tenant_intents or [], key=lambda i: (i.submitted_ms, i.intent_id))
        self.cooldown_ms, self.lookback = cooldown_ms, lookback_intervals
        self.log: list = []  # every decision incl. retain/suppressed, for traces

    def on_tick(self, sim, now_ms: float) -> None:
        orch, topo = self.orch, sim.topo
        orch.verify_pending(now_ms)
        snap = sim.telemetry.snapshot(now_ms, self.lookback)
        forecasts: dict = {}
        if self.planner.uses_forecast and self.predictor is not None:
            for spec in topo.services:
                for site in topo.site_ids():
                    try:
                        forecasts[(spec.service_id, site)] = forecast_from_snapshot(self.predictor, snap, spec.service_id, site, topo.interval_ms)
                    except EmptyHistoryError:
                        pass
        ctx = PlanContext(now_ms, snap, topo, sim.cluster, forecasts)
        while self.pending_intents and self.pending_intents[0].submitted_ms <= now_ms:
            it = self.pending_intents.pop(0)
            if it.kind == "reserve":
                ch = self.planner.place_intent(ctx, it.service, it.servers, it.site_to)
                if ch is not None:
                    it = ProvisioningIntent(**{**it.to_dict(), "site_to": ch.site_to})
            res = orch.submit(it, now_ms, snapshot=snap)
            self.log.append({"t": now_ms, "type": "tenant_intent", "intent_id": it.intent_id, "state": res.state, "reason": res.reason, "duplicate": res.duplicate})
        for ch in self.planner.plan(ctx):
            if ch.kind == "retain":
                self.log.append({"t": now_ms, "type": "retain", "service": ch.service, "reason": ch.reason, "evidence": ch.evidence})
                continue
            last = orch.last_action_ms(ch.service, "closed_loop")
            if last is not None and now_ms - last < self.cooldown_ms:
                self.log.append({"t": now_ms, "type": "suppressed_cooldown", "service": ch.service, "kind": ch.kind})
                continue
            spec = topo.service(ch.service)
            iid = stable_id("int", "cl", ch.service, now_ms, ch.kind, ch.site_to, ch.site_from, ch.servers)
            it = ProvisioningIntent(iid, spec.tenant_id, ch.service, ch.kind, ch.servers, ch.site_to, ch.site_from, orch.cfg.sla_ms,
                                    now_ms, self.tokens[spec.tenant_id], self.policy_revision, "closed_loop")
            res = orch.submit(it, now_ms, snapshot=snap, extra_evidence={"reason": ch.reason, "plan": ch.evidence})
            self.log.append({"t": now_ms, "type": "plan", "intent_id": iid, "kind": ch.kind, "site_to": ch.site_to, "servers": ch.servers,
                             "state": res.state, "reason": res.reason, "evidence": ch.evidence})
        orch.persist_cluster()
