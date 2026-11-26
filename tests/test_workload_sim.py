import pytest

from sszt6g import simulator
from sszt6g.errors import CapacityError
from sszt6g.experiments import run_arm
from sszt6g.ledger import AuditLog
from sszt6g.orchestrator import ClosedLoopController, OrchConfig, Orchestrator
from sszt6g.planners import make_planner
from sszt6g.simulator import Cluster, Simulator
from sszt6g.workload import generate_jobs, make_scenario


def test_arrivals_are_seeded_and_reproducible(world):
    a = generate_jobs(make_scenario("ramp_burst", 7, world.topo))
    b = generate_jobs(make_scenario("ramp_burst", 7, world.topo))
    c = generate_jobs(make_scenario("ramp_burst", 8, world.topo))
    assert a == b and a != c and [j.t_ms for j in a] == sorted(j.t_ms for j in a)


def test_burst_has_higher_rate_in_burst_window(world):
    jobs = [j for j in generate_jobs(make_scenario("step_burst", 3, world.topo)) if j.service == "vision-inference"]
    inside = sum(1 for j in jobs if 40_000 <= j.t_ms < 90_000) / 50.0
    outside = sum(1 for j in jobs if j.t_ms < 40_000) / 40.0
    assert inside > 2 * outside


def test_unknown_scenario_kind_rejected(world):
    with pytest.raises(ValueError):
        make_scenario("nope", 1, world.topo)


def test_job_records_separate_network_queue_compute(world):
    sc = make_scenario("steady_low", 1, world.topo)
    res = Simulator(world.topo, Cluster(world.topo), sc.horizon_ms).run(generate_jobs(sc))
    done = [r for r in res.records if r.status == "completed"]
    assert done
    for r in done:
        assert r.latency_ms == pytest.approx(r.net_ms + r.queue_ms + r.compute_ms, abs=1e-6)


def test_all_job_dispositions_are_recorded(world):
    sc = make_scenario("step_burst", 2, world.topo)
    res = Simulator(world.topo, Cluster(world.topo), sc.horizon_ms).run(generate_jobs(sc))
    by = res.by_status()
    assert sum(by.values()) == len(generate_jobs(sc))
    assert by.get("denied", 0) > 0 and by.get("dropped", 0) > 0 and by.get("completed", 0) > 0


def test_unfinished_jobs_are_recorded_when_tail_short(world, monkeypatch):
    sc = make_scenario("step_burst", 2, world.topo, horizon_ms=80_000)  # burst still running at the end
    monkeypatch.setattr(simulator, "TAIL_MS", 1_000)
    res = Simulator(world.topo, Cluster(world.topo), sc.horizon_ms).run(generate_jobs(sc))
    assert res.by_status().get("unfinished", 0) > 0


def test_cluster_rejects_over_capacity_and_conserves(world):
    cl = Cluster(world.topo)
    free = cl.free("edge-east")
    with pytest.raises(CapacityError):
        cl.apply("a1", [("vision-inference", "edge-east", free + 1)], [], 0.0, 1000.0)
    cl.apply("a2", [("vision-inference", "edge-east", free)], [], 0.0, 1000.0)
    assert cl.free("edge-east") == 0
    cl.activate_due(1000.0)
    assert cl.free("edge-east") == 0 and cl.occupied("edge-east") == cl.capacity["edge-east"]


def test_cluster_apply_is_idempotent_per_action_id(world):
    cl = Cluster(world.topo)
    cl.apply("same", [("vision-inference", "edge-east", 2)], [], 0.0, 1000.0)
    again = cl.apply("same", [("vision-inference", "edge-east", 2)], [], 5.0, 1000.0)
    assert again["duplicate"] and cl.pending_adds(site="edge-east") == 2


def test_rollback_restores_capacity(world):
    cl = Cluster(world.topo)
    before = cl.free("edge-east")
    cl.apply("r1", [("vision-inference", "edge-east", 2)], [], 0.0, 1000.0)
    cl.activate_due(1000.0)
    assert cl.free("edge-east") == before - 2
    cl.rollback("r1")
    assert cl.status("r1") == "rolled_back"
    assert cl.allocated("vision-inference", "edge-east") == world.topo.initial["vision-inference@edge-east"]
    assert cl.rollback("r1") == "noop"


def test_move_transfers_servers(world):
    cl = Cluster(world.topo)
    cl.apply("m1", [("traffic-analytics", "edge-east", 1)], [("traffic-analytics", "edge-west", 1)], 0.0, 1000.0)
    cl.activate_due(1000.0)
    assert cl.allocated("traffic-analytics", "edge-east") == 1 and cl.allocated("traffic-analytics", "edge-west") == 1


def test_cluster_state_round_trip(world):
    cl = Cluster(world.topo)
    cl.apply("x1", [("vision-inference", "edge-east", 1)], [], 0.0, 1000.0)
    restored = Cluster.from_state(world.topo, cl.to_state())
    assert restored.pending_adds(site="edge-east") == 1 and restored.status("x1") == "pending"
    assert restored.apply("x1", [("vision-inference", "edge-east", 1)], [], 5.0, 1.0)["duplicate"]


def test_capacity_invariant_holds_every_tick_for_every_planner(world, predictor):
    class Checked(ClosedLoopController):
        def on_tick(self, sim, now_ms):
            super().on_tick(sim, now_ms)
            for s in sim.topo.site_ids():
                assert sim.cluster.occupied(s) <= sim.cluster.capacity[s]
            for t, q in sim.topo.tenant_quota.items():
                assert sim.cluster.tenant_total(t) <= q

    sc = make_scenario("ramp_burst", 5, world.topo)
    for arm in ("fixed", "reactive", "predictive"):
        cl = Cluster(world.topo)
        orch = Orchestrator(world.topo, cl, world.policy, world.keyring, AuditLog(), ":memory:", OrchConfig())
        Simulator(world.topo, cl, sc.horizon_ms, Checked(orch, make_planner(arm), predictor, world.tokens, 1)).run(generate_jobs(sc))


def test_fixed_planner_never_changes_capacity_and_drops_jobs(world, predictor):
    r = run_arm(world, make_scenario("step_burst", 4, world.topo), "fixed", predictor)
    assert r.actions == [] and r.sim.by_status().get("dropped", 0) > 0


def test_arms_see_identical_arrivals(world, predictor):
    sc = make_scenario("ramp_burst", 9, world.topo)
    ids = [{x.job_id for x in run_arm(world, sc, a, predictor).sim.records} for a in ("fixed", "reactive", "predictive")]
    assert ids[0] == ids[1] == ids[2]


def test_reactive_adds_capacity_in_a_burst(world, predictor):
    r = run_arm(world, make_scenario("step_burst", 4, world.topo), "reactive", predictor)
    assert any(x["state"] == "verified" and x["origin"] == "closed_loop" for x in r.actions)
    assert r.ledger_ok
