"""Формулы (2)–(4) и закон Литтла. Для подгонки — TEST DATA ONLY, сгенерированные из модели
с известными параметрами (проверяется восстановление параметров, а не «нужный результат»)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from experiments.analysis import models as m


def test_littles_law_formula() -> None:
    # X = N/(T+Z): 100 VU, T=0.25 c, Z=1 c → 80 запросов/с
    assert m.littles_law_throughput(100, 0.25, 1.0) == pytest.approx(80.0)


def test_littles_law_check_tolerance() -> None:
    ok = m.check_littles_law(100, 250.0, 1.0, 76.0, tolerance=0.10)  # −5 %
    bad = m.check_littles_law(100, 250.0, 1.0, 60.0, tolerance=0.10)  # −25 %
    assert not ok.warning and bad.warning
    assert bad.rel_deviation == pytest.approx(-0.25)


@pytest.mark.parametrize("args", [(-1, 0.1, 1.0), (1, -0.1, 1.0), (1, 0.0, 0.0)])
def test_littles_law_invalid(args: tuple[int, float, float]) -> None:
    with pytest.raises(ValueError):
        m.littles_law_throughput(*args)


def test_saturation_model_values() -> None:
    # при R = Rsat: T = 2·T0
    assert m.saturation_model(np.array([100.0]), 40.0, 100.0, 1.5)[0] == pytest.approx(80.0)


def test_fit_recovers_known_parameters() -> None:
    r = np.array([10, 50, 100, 200, 500, 1000, 2000], dtype=float)
    t = m.saturation_model(r, 37.0, 118.0, 1.42)  # TEST DATA ONLY
    fit = m.fit_saturation_model(r, t)
    assert fit.applicable
    assert fit.t0 == pytest.approx(37.0, rel=1e-4)
    assert fit.r_sat == pytest.approx(118.0, rel=1e-4)
    assert fit.alpha == pytest.approx(1.42, rel=1e-4)
    assert fit.r2 == pytest.approx(1.0)


def test_fit_reproduces_independent_refit_of_article_table1() -> None:
    """Регрессия расчёта из reproducibility_findings.md F-6 (подгонка опубликованных средних
    табл. 1 независимым сеточным методом): монолит 37,4 / 118,4 / 1,419."""
    r = [10, 50, 100, 200, 500, 1000, 2000]
    t = [42, 48, 67, 120, 310, 820, 2100]  # ДАННЫЕ СТАТЬИ (табл. 1), не результат стенда
    fit = m.fit_saturation_model(r, t)
    assert fit.t0 == pytest.approx(37.38, abs=0.05)
    assert fit.r_sat == pytest.approx(118.4, abs=0.2)
    assert fit.alpha == pytest.approx(1.419, abs=0.002)


def test_fit_non_monotonic_is_not_applicable() -> None:
    fit = m.fit_saturation_model([10, 50, 100, 200, 500], [95, 72, 58, 51, 48])
    assert not fit.applicable and "немонотонна" in (fit.reason or "")
    assert fit.t0 is None


def test_fit_too_few_points() -> None:
    assert not m.fit_saturation_model([10, 50, 100], [1, 2, 3]).applicable


def test_fit_rejects_nan() -> None:
    with pytest.raises(ValueError):
        m.fit_saturation_model([1, 2, 3, 4], [1, math.nan, 3, 4])


def test_scaling_efficiency() -> None:
    # X удвоилась при удвоении R → E = 1; X выросла в 1.5 раза → 0.75
    assert m.scaling_efficiency(100, 80, 200, 160) == pytest.approx(1.0)
    assert m.scaling_efficiency(100, 80, 200, 120) == pytest.approx(0.75)
    with pytest.raises(ValueError):
        m.scaling_efficiency(200, 1, 100, 1)
    with pytest.raises(ValueError):
        m.scaling_efficiency(100, 0, 200, 1)


def test_asymptotic_check() -> None:
    # X_max = 1000; при R=2000, Z=1: T = 2000/1000 − 1 = 1 c = 1000 мс
    assert m.asymptotic_rt_ms(2000, 1000.0, 1.0) == pytest.approx(1000.0)
    x_max, r_star, pts = m.asymptotic_check(
        [100, 500, 1000, 2000], [50, 200, 400, 1000], [95, 420, 980, 1000], 1.0
    )
    assert x_max == 1000 and r_star == 1000
    assert [p.vus for p in pts] == [2000]
    assert pts[0].rel_deviation == pytest.approx(0.0)
