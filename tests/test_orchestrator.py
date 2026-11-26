import json

import pytest

from sszt6g.errors import CrashError
from sszt6g.experiments import run_arm
from sszt6g.ledger import AuditLog, QuorumLedger
from sszt6g.orchestrator import OrchConfig, Orchestrator
from sszt6g.simulator import Cluster
from sszt6g.telemetry import TelemetryStore, TelemetryRecord
from sszt6g.util import stable_id
from sszt6g.workload import make_scenario
from tests.conftest import make_intent


def mk(world, ledger=None, db=":memory:", lost=None, cfg=None):
    return Orchestrator(world.topo, None if db != ":memory:" else Cluster(world.topo, lost), world.policy, world.keyring,
                        ledger or AuditLog(), db, cfg or OrchConfig())


def occ(cluster):
    return {s: cluster.occupied(s) for s in cluster.topo.site_ids()}


def test_every_state_has_a_ledger_receipt(world, orch):
    res = orch.submit(make_intent(world, "full"), 1000.0)
    orch.verify_pending(res.activate_ms + 1)
    r = orch.result(res.action_id)
    assert r.state == "verified" and set(r.receipts) == {"proposed", "authorized", "committed", "applied", "verified"}
    states = [e["entry"]["state"] for e in orch.ledger.entries()]
    assert states == ["proposed", "authorized", "committed", "applied", "verified"] and orch.ledger.verify().ok


def test_denial_leaves_capacity_unchanged_and_writes_no_commit(world, orch):
    before = occ(orch.cluster)
    res = orch.submit(make_intent(world, "deny", tenant="city-traffic", service="traffic-analytics", kind="move", site_to="edge-east", site_from="edge-west"), 1000.0)
    assert res.state == "denied" and res.reason == "op_not_allowed" and occ(orch.cluster) == before and not orch.cluster.pending
    assert [e["entry"]["state"] for e in orch.ledger.entries()] == ["proposed", "denied"]


def test_quota_denial_even_though_capacity_exists(world, orch):
    assert orch.cluster.free("edge-east") >= 4
    res = orch.submit(make_intent(world, "q", servers=5), 1000.0)  # 2 allocated + 5 = 7 > quota 6
    assert res.state == "denied" and res.reason == "quota_exceeded" and not orch.cluster.pending


def test_capacity_failure_is_explicit_and_changes_nothing(world, orch):
    before = occ(orch.cluster)
    res = orch.submit(make_intent(world, "cap", tenant="city-traffic", service="traffic-analytics", servers=3, site_to="edge-west"), 1000.0)
    assert res.state == "failed" and res.reason == "capacity_unavailable" and occ(orch.cluster) == before


def test_duplicate_delivery_returns_first_outcome_and_changes_nothing(world, orch):
    it = make_intent(world, "dup", servers=2)
    first = orch.submit(it, 1000.0)
    after_first = occ(orch.cluster)
    n = len(orch.ledger.entries())
    dup = orch.submit(it, 2000.0)
    assert dup.duplicate and dup.action_id == first.action_id and occ(orch.cluster) == after_first and len(orch.ledger.entries()) == n


def test_action_ids_are_stable(world):
    assert stable_id("act", make_intent(world, "same").intent_id) == stable_id("act", make_intent(world, "same").intent_id)
    o1, o2 = mk(world), mk(world)
    assert o1.submit(make_intent(world, "s"), 1000.0).action_id == o2.submit(make_intent(world, "s"), 5000.0).action_id


def test_audit_failure_before_action_blocks_apply(world):
    led = AuditLog()
    o = mk(world, led)
    led.unavailable = True
    before = occ(o.cluster)
    res = o.submit(make_intent(world, "aud"), 1000.0)
    assert res.state == "failed" and res.reason == "audit_failed" and occ(o.cluster) == before and not o.cluster.pending


def test_audit_failure_at_commit_blocks_apply(world):
    class FailOnCommit(AuditLog):
        def append(self, entry):
            if entry.get("state") == "committed":
                from sszt6g.errors import LedgerUnavailable
                raise LedgerUnavailable("no commit")
            return super().append(entry)

    o = mk(world, FailOnCommit())
    res = o.submit(make_intent(world, "c"), 1000.0)
    assert res.reason == "audit_failed" and not o.cluster.pending and o.cluster.status(res.action_id) is None


def test_quorum_failure_blocks_apply(world):
    q = QuorumLedger(4)
    o = mk(world, q)
    q.set_online("v3", False)
    q.set_online("v4", False)
    res = o.submit(make_intent(world, "qf"), 1000.0)
    assert res.reason == "audit_failed" and not o.cluster.pending


def test_crash_between_commit_and_apply_then_recover_applies_once(world, tmp_path):
    db, lp = str(tmp_path / "s.db"), tmp_path / "a.jsonl"
    it = make_intent(world, "crash", servers=2)
    o1 = mk(world, AuditLog(lp), db)
    o1.crash_at = "after_commit"
    with pytest.raises(CrashError):
        o1.submit(it, 1000.0)
    assert o1.store.get(stable_id("act", it.intent_id))["state"] == "committed" and not o1.cluster.pending
    o1.close()
    o2 = mk(world, AuditLog(lp), db)
    rec = o2.recover(2000.0)
    assert [r.state for r in rec] == ["applied"] and o2.cluster.pending_adds(site="edge-east") == 2
    assert o2.recover(2500.0) == []  # idempotent
    dup = o2.submit(it, 3000.0)
    assert dup.duplicate and o2.cluster.pending_adds(site="edge-east") == 2
    states = [e["entry"]["state"] for e in o2.ledger.entries()]
    assert states.count("committed") == 1 and states.count("applied") == 1


def test_restart_after_apply_does_not_reallocate(world, tmp_path):
    db, lp = str(tmp_path / "s.db"), tmp_path / "a.jsonl"
    it = make_intent(world, "restart", servers=2)
    o1 = mk(world, AuditLog(lp), db)
    o1.submit(it, 1000.0)
    o1.close()
    o2 = mk(world, AuditLog(lp), db)
    assert o2.cluster.pending_adds(site="edge-east") == 2
    assert o2.submit(it, 2000.0).duplicate and o2.cluster.pending_adds(site="edge-east") == 2


def test_recover_before_authorization_fails_closed(world, tmp_path):
    db = str(tmp_path / "s.db")
    o1 = mk(world, db=db)
    it = make_intent(world, "early")
    o1.store.insert(stable_id("act", it.intent_id), it, 1000.0, 99999.0)
    o1.close()
    o2 = mk(world, db=db)
    out = o2.recover(2000.0)
    assert out[0].state == "failed" and out[0].reason == "interrupted_before_authorization" and not o2.cluster.pending


def test_expired_action_is_not_applied(world, tmp_path):
    db, lp = str(tmp_path / "s.db"), tmp_path / "a.jsonl"
    it = make_intent(world, "exp")
    o1 = mk(world, AuditLog(lp), db, cfg=OrchConfig(ttl_ms=1000.0))
    o1.crash_at = "after_commit"
    with pytest.raises(CrashError):
        o1.submit(it, 1000.0)
    o1.close()
    o2 = mk(world, AuditLog(lp), db, cfg=OrchConfig(ttl_ms=1000.0))
    res = o2.recover(50_000.0)
    assert res[0].state == "expired" and not o2.cluster.pending


def test_stale_telemetry_rejected(world, orch):
    st = TelemetryStore(5000)
    st.append(TelemetryRecord(5000, "vision-inference", "edge-east", 1, 1, 0, 2, 0))
    snap = st.snapshot(5000)
    res = orch.submit(make_intent(world, "stale", at=40_000.0), 40_000.0, snapshot=snap)
    assert res.state == "failed" and res.reason == "stale_telemetry" and not orch.cluster.pending
    empty = TelemetryStore(5000).snapshot(1000)
    assert orch.submit(make_intent(world, "stale2"), 1000.0, snapshot=empty).reason == "stale_telemetry"


def test_stale_policy_denied_end_to_end(world):
    o = Orchestrator(world.topo, Cluster(world.topo), world.policy.bumped(2), world.keyring, AuditLog(), ":memory:", OrchConfig())
    res = o.submit(make_intent(world, "pol", rev=1), 1000.0)
    assert res.state == "denied" and res.reason == "stale_policy"


def test_expired_claim_denied_end_to_end(world, orch):
    res = orch.submit(make_intent(world, "claim", issued=-7_200_000.0, ttl=3_600_000.0), 1000.0)
    assert res.state == "denied" and res.reason == "claim_expired_or_not_yet_valid"


def test_verification_failure_rolls_back_then_escalates(world):
    o = mk(world, lost=lambda aid: True)
    res = o.submit(make_intent(world, "lost", servers=2), 1000.0)
    before_pending = o.cluster.pending_adds(site="edge-east")
    assert before_pending == 2
    o.verify_pending(res.activate_ms + 1)
    r = o.result(res.action_id)
    assert r.state == "escalated" and set(r.receipts) >= {"rolled_back", "escalated"} and "verified" not in r.receipts
    assert o.cluster.allocated("vision-inference", "edge-east") == world.topo.initial["vision-inference@edge-east"] and o.cluster.pending_adds() == 0
    assert o.cluster.status(res.action_id) == "rolled_back"


def test_move_changes_placement_after_migration_cost(world, orch):
    it = make_intent(world, "mv", tenant="acme-video", kind="move", servers=1, site_to="edge-west", site_from="edge-east")
    res = orch.submit(it, 1000.0)
    assert res.activate_ms == 1000.0 + world.topo.provision_ms + world.topo.migration_ms
    orch.verify_pending(res.activate_ms)
    assert orch.cluster.allocated("vision-inference", "edge-west") == 1 and orch.cluster.allocated("vision-inference", "edge-east") == 1


def test_wire_body_matches_contract(world, orch):
    from sszt6g.interfaces import to_wire
    res = orch.submit(make_intent(world, "wire"), 1000.0)
    to_wire("action_result", res.to_wire_body(), "msg-" + res.action_id)
    to_wire("policy_decision", res.decision, "msg-" + res.decision["decision_id"])
    to_wire("audit_receipt", res.receipts["proposed"], "msg-" + res.receipts["proposed"]["receipt_id"])
    to_wire("provisioning_intent", make_intent(world, "wire").to_dict(), "msg-intent-1")


def test_full_intent_prediction_to_report(world, predictor, tmp_path):
    r = run_arm(world, make_scenario("ramp_burst", 21, world.topo), "predictive", predictor, work_dir=tmp_path, kind="ramp_burst")
    cl = [x for x in r.actions if x["origin"] == "closed_loop"]
    assert cl and all(x["state"] == "verified" for x in cl) and r.ledger_ok
    assert all(x["verified_ms"] > x["applied_ms"] > x["submitted_ms"] - 1 for x in cl)
    entries = AuditLog(next(tmp_path.glob("*.audit.jsonl"))).entries()
    assert len(entries) == 5 * len(cl)


def test_plan_evidence_carries_snapshot_and_forecast_ids(world, predictor, tmp_path):
    run_arm(world, make_scenario("ramp_burst", 22, world.topo), "predictive", predictor, work_dir=tmp_path, kind="ramp_burst")
    ents = [e["entry"] for e in AuditLog(next(tmp_path.glob("*.audit.jsonl"))).entries() if e["entry"]["state"] == "proposed"]
    plan = ents[0]["evidence"]["plan"]
    assert plan["snapshot_id"].startswith("snap-") and plan["forecast_ids"] and all(f.startswith("fc-") for f in plan["forecast_ids"])
    assert ents[0]["evidence"]["snapshot_id"] == plan["snapshot_id"]


def test_cooldown_suppresses_back_to_back_actions(world, predictor, monkeypatch):
    sc = make_scenario("step_burst", 23, world.topo, peak=40.0)  # needs more than one scale-up step
    normal = run_arm(world, sc, "reactive", predictor)
    gaps = sorted(x["t"] for x in normal.log if x["type"] == "plan")
    assert len(gaps) >= 2 and all(b - a >= world.settings["orchestrator"]["cooldown_ms"] for a, b in zip(gaps, gaps[1:]))
    monkeypatch.setitem(world.settings["orchestrator"], "cooldown_ms", 10_000_000.0)
    long_cd = run_arm(world, sc, "reactive", predictor)
    assert sum(1 for x in long_cd.log if x["type"] == "plan") == 1 and any(x["type"] == "suppressed_cooldown" for x in long_cd.log)


def test_predictive_logs_retain_decisions(world, predictor):
    r = run_arm(world, make_scenario("steady_low", 24, world.topo), "predictive", predictor)
    assert r.actions == [] and any(x["type"] == "retain" for x in r.log)


def test_verify_pending_is_idempotent(world, orch):
    res = orch.submit(make_intent(world, "idem"), 1000.0)
    orch.verify_pending(res.activate_ms)
    n = len(orch.ledger.entries())
    assert orch.verify_pending(res.activate_ms + 5000) == [] and len(orch.ledger.entries()) == n
