import numpy as np
import pytest

from sszt6g.errors import DataError, EmptyHistoryError
from sszt6g.prediction import Forecaster, PersistenceForecaster, feature_vector, forecast_from_snapshot, training_rows
from sszt6g.simulator import Cluster, Simulator
from sszt6g.telemetry import TelemetryRecord, TelemetryStore
from sszt6g.workload import Job, generate_jobs, make_scenario


def rec(ts, arrivals=1):
    return TelemetryRecord(ts, "svc", "site", arrivals, arrivals, 0, 1, 0)


def test_snapshot_cutoff_includes_equal_and_excludes_future():
    st = TelemetryStore(5000)
    st.extend([rec(5000), rec(10000), rec(15000)])
    snap = st.snapshot(10000)
    assert [r.ts_ms for r in snap.records] == [5000, 10000] and snap.latest_ts_ms == 10000
    assert snap.age_ms(12000) == 2000


def test_snapshot_is_empty_before_first_record():
    st = TelemetryStore(5000)
    st.append(rec(5000))
    assert st.snapshot(4999).is_empty() and st.snapshot(4999).age_ms(5000) is None


def test_out_of_order_telemetry_rejected():
    st = TelemetryStore(5000)
    st.append(rec(10000))
    with pytest.raises(DataError):
        st.append(rec(5000))


def test_snapshot_id_is_deterministic_and_content_bound():
    a, b = TelemetryStore(5000), TelemetryStore(5000)
    for s in (a, b):
        s.extend([rec(5000, 3), rec(10000, 4)])
    assert a.snapshot(10000).snapshot_id == b.snapshot(10000).snapshot_id
    b.append(rec(15000, 9))
    assert b.snapshot(15000).snapshot_id != a.snapshot(10000).snapshot_id


def test_job_at_tick_time_counts_in_next_window_not_this_one(world):
    topo = world.topo
    sc = make_scenario("steady_low", 1, topo)
    jobs = [Job("j1", 4999.0, "edge-east", "acme-video", "vision-inference", 100.0, 400.0),
            Job("j2", 5000.0, "edge-east", "acme-video", "vision-inference", 100.0, 400.0)]
    sim = Simulator(topo, Cluster(topo), sc.horizon_ms)
    sim.run(jobs)
    east = [r for r in sim.telemetry.all() if r.service == "vision-inference" and r.site == "edge-east"]
    assert east[0].ts_ms == 5000 and east[0].ingress_arrivals == 1 and east[1].ingress_arrivals == 1


def test_empty_history_raises_not_zero():
    with pytest.raises(EmptyHistoryError):
        feature_vector([], 0.0)
    with pytest.raises(EmptyHistoryError):
        PersistenceForecaster().predict_count([])


def test_short_history_is_left_padded():
    v = feature_vector([7.0, 9.0], 2.0, lags=4)
    assert list(v) == [7.0, 7.0, 7.0, 9.0, 9.0, 2.0][:0] + [7.0, 7.0, 7.0, 9.0, 2.0] or list(v) == [7.0, 7.0, 7.0, 9.0, 2.0]


def test_forecaster_scaler_is_fitted_on_training_rows_only():
    X = np.array([[1, 2, 3, 4, 5, 6, 0.0], [2, 3, 4, 5, 6, 7, 1.0], [3, 4, 5, 6, 7, 8, 2.0], [4, 5, 6, 7, 8, 9, 3.0]], dtype=float)
    f = Forecaster(1.0).fit(X, np.array([6.0, 7.0, 8.0, 9.0]))
    assert np.allclose(f.scaler.mean_, X.mean(axis=0))
    far = f.predict_count([1000.0] * 6, 0.0)  # an unseen future value must not move the fitted scaler
    assert np.allclose(f.scaler.mean_, X.mean(axis=0)) and far >= 0


def test_unfitted_forecaster_refuses():
    with pytest.raises(RuntimeError):
        Forecaster().predict_count([1, 2, 3])


def test_forecast_uses_only_history_up_to_cutoff(world, predictor):
    sc = make_scenario("ramp_burst", 11, world.topo)
    sim = Simulator(world.topo, Cluster(world.topo), sc.horizon_ms)
    sim.run(generate_jobs(sc))
    t = 50_000.0
    snap = sim.telemetry.snapshot(t)
    fc = forecast_from_snapshot(predictor, snap, "vision-inference", "edge-east", 5000)
    assert fc.made_at_ms == t and fc.target_window_ms == (t, t + 5000) and all(r.ts_ms <= t for r in snap.records)
    future = sim.telemetry.snapshot(t + 30_000)
    fc2 = forecast_from_snapshot(predictor, future, "vision-inference", "edge-east", 5000)
    assert fc.forecast_id != fc2.forecast_id
    assert fc.persistence_arrivals == snap.arrivals("vision-inference", "edge-east")[-1]


def test_forecast_on_empty_snapshot_raises(predictor):
    snap = TelemetryStore(5000).snapshot(1000)
    with pytest.raises(EmptyHistoryError):
        forecast_from_snapshot(predictor, snap, "vision-inference", "edge-east", 5000)


def test_training_rows_target_is_the_next_interval(world):
    recs = [rec(5000 * (i + 1), i) for i in range(10)]
    X, y = training_rows(recs, "svc", "site", lags=3)
    assert len(y) == 8 and list(y[:3]) == [2.0, 3.0, 4.0] and X[0, -2] == 1.0


def test_training_scenarios_never_overlap_test_seeds(world):
    from sszt6g.experiments import training_scenarios
    train = {sc.seed for sc in training_scenarios(world)}
    assert not train & set(world.settings["seeds"]["test"]) and not train & set(world.settings["seeds"]["dev"])


def test_forecaster_save_load_round_trip(predictor, tmp_path):
    h = predictor.save(tmp_path / "m.joblib")
    loaded = Forecaster.load(tmp_path / "m.joblib")
    assert len(h) == 64 and loaded.predict_count([10, 12, 14, 16, 18, 20], 0.0) == pytest.approx(predictor.predict_count([10, 12, 14, 16, 18, 20], 0.0))
