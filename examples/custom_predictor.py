"""Replace the demand predictor.

Any object with a `model_id` string and `predict_count(history, queue_per_server) -> float` can drive the
predictive planner. `history` is the list of arrivals per 5 s interval observed up to the cutoff (never empty:
an empty history raises EmptyHistoryError before your code is called).

Run:  python examples/custom_predictor.py
"""
from sszt6g.config import build_world
from sszt6g.experiments import run_arm
from sszt6g.metrics import latency_stats
from sszt6g.workload import make_scenario


class EwmaTrend:
    """Exponentially weighted level plus the last observed step, as a deliberately simple alternative."""

    model_id = "ewma-trend"

    def __init__(self, alpha: float = 0.6):
        self.alpha = alpha

    def predict_count(self, history, queue_per_server: float = 0.0) -> float:
        level = history[0]
        for h in history[1:]:
            level = self.alpha * h + (1 - self.alpha) * level
        trend = history[-1] - history[-2] if len(history) > 1 else 0.0
        return max(0.0, level + 0.5 * trend)


if __name__ == "__main__":
    world = build_world()
    scenario = make_scenario("ramp_burst", 1001, world.topo, peak=24.0, ramp_s=20.0)
    for name in ("reactive", "predictive"):
        run = run_arm(world, scenario, name, EwmaTrend())
        s = latency_stats(run.sim.records)
        print(f"{name:10s} completed={s['jobs_completed']} dropped={s['jobs_dropped']} mean={s['latency_ms']['mean']:.1f} ms p95={s['latency_ms']['p95']:.1f} ms")
