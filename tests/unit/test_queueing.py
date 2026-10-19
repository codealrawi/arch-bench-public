"""Операционный анализ (queueing.py). TEST DATA ONLY: синтетические интервалы, ожидаемые
значения выведены вручную."""

from __future__ import annotations

import pandas as pd
import pytest

from experiments.analysis import queueing as q


def _points(rows: list[tuple[float, float, str]]) -> pd.DataFrame:
    """rows: (начало, длительность, фаза) → столбцы как в load_points (t — ОКОНЧАНИЕ)."""
    return pd.DataFrame(
        {
            "t": [s + d for s, d, _ in rows],
            "duration_ms": [d * 1000.0 for _, d, _ in rows],
            "phase": [p for _, _, p in rows],
        }
    )


def test_littles_empirical_hand_example() -> None:
    # окно [0, 10): запросы стартуют в 0..9 по 0,5 с; ещё один [−0,2; 0,3] (начат в warmup);
    # setup-запрос внутри окна не учитывается.
    rows = [(float(k), 0.5, "measure") for k in range(10)]
    rows += [(-0.2, 0.5, "warmup"), (3.0, 4.0, "setup")]
    le = q.littles_empirical(_points(rows), t0=0.0, window_s=10.0)
    assert le is not None
    # L: перекрытия 10·0,5 + 0,3 = 5,3 с за 10 с → 0,53
    assert le.l_measured == pytest.approx(0.53)
    # λ: 11 завершений / 10 с; W: (10·0,5 + 0,5)/11 = 0,5 → λW = 0,55
    assert le.lam == pytest.approx(1.1) and le.w_s == pytest.approx(0.5)
    assert le.lam_w == pytest.approx(0.55)
    assert le.rel_dev == pytest.approx(0.02 / 0.53)


def test_littles_empty_window_returns_none() -> None:
    assert q.littles_empirical(_points([(20.0, 1.0, "measure")]), 0.0, 10.0) is None


def test_closed_system_measured_hand_example() -> None:
    # N = 2 VU; каждый: запрос 0,2 с, затем сон 0,8 с → итерация 1,0 с; 20 запросов за 10 с
    rows = [(float(k) + v * 0.5, 0.2, "measure") for k in range(10) for v in range(2)]
    pts = _points(rows)
    vus = pd.DataFrame({"t": [float(s) for s in range(11)], "n": [2.0] * 11})
    its = pd.DataFrame({"t": pts["t"] + 0.8, "duration_ms": [1000.0] * len(pts)})
    its = its[its["t"] < 10.0]
    cm = q.closed_system_measured(pts, vus, its, 0.0, 10.0)
    assert cm is not None
    assert cm.n_mean == 2.0 and cm.x == pytest.approx(2.0)
    assert cm.r_s == pytest.approx(0.2) and cm.z_meas_s == pytest.approx(0.8)
    assert cm.n_pred == pytest.approx(2.0) and cm.rel_dev == pytest.approx(0.0, abs=1e-12)


def test_closed_system_without_aux_series_is_not_imputed() -> None:
    pts = _points([(1.0, 0.2, "measure")])
    empty_v = pd.DataFrame(columns=["t", "n"], dtype=float)
    empty_i = pd.DataFrame(columns=["t", "duration_ms"], dtype=float)
    assert q.closed_system_measured(pts, empty_v, empty_i, 0.0, 10.0) is None


def test_asymptotic_bounds_hand_example() -> None:
    # D = 0,1 с, Z = 0,9 с, X_max = 50 → N* = 1,0·50 = 50
    assert q.n_star(0.1, 0.9, 50.0) == pytest.approx(50.0)
    light = q.asymptotic_bounds(10, 0.1, 0.9, 50.0)
    assert light.x_upper == pytest.approx(10.0)  # N/(D+Z)
    assert light.r_lower_s == pytest.approx(0.1) and light.regime == "light"
    heavy = q.asymptotic_bounds(100, 0.1, 0.9, 50.0)
    assert heavy.x_upper == pytest.approx(50.0)  # X_max
    assert heavy.r_lower_s == pytest.approx(100 / 50 - 0.9) and heavy.regime == "heavy"
    with pytest.raises(ValueError):
        q.asymptotic_bounds(10, 0.0, 0.9, 50.0)
