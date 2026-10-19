"""Ожидаемые значения выведены независимо (комбинаторика / ручной расчёт), не из кода."""

from __future__ import annotations

import math

import numpy as np
import pytest

from experiments.analysis import statistics as st


def test_cliffs_delta_complete_separation() -> None:
    assert st.cliffs_delta([10, 11, 12], [1, 2, 3]) == 1.0
    assert st.cliffs_delta([1, 2, 3], [10, 11, 12]) == -1.0


def test_cliffs_delta_hand_example() -> None:
    # x=[1,3,5], y=[2,4]: пары x>y: (3,2),(5,2),(5,4)=3; x<y: (1,2),(1,4),(3,4)=3 → δ=0
    assert st.cliffs_delta([1, 3, 5], [2, 4]) == 0.0
    # x=[2,4], y=[1,1,3]: x>y: 2>1,2>1,4>1,4>1,4>3 =5; x<y: 2<3 =1 → (5-1)/6
    assert st.cliffs_delta([2, 4], [1, 1, 3]) == pytest.approx(4 / 6)


def test_mann_whitney_exact_min_p_n5_and_n10() -> None:
    _, p5 = st.mann_whitney_exact(list(range(5)), list(range(10, 15)))
    assert p5 == pytest.approx(2 / math.comb(10, 5))  # 2/252 ≈ 0.0079
    _, p10 = st.mann_whitney_exact(list(range(10)), list(range(20, 30)))
    assert p10 == pytest.approx(2 / math.comb(20, 10))
    assert st.min_exact_p(5, 5) == pytest.approx(2 / 252)


def test_mann_whitney_u_value() -> None:
    # U_x = число пар (x_i > y_j) (+0.5 за равенство); x всегда меньше → U=0
    u, _ = st.mann_whitney_exact([1, 2, 3], [4, 5, 6])
    assert u == 0.0


def test_mann_whitney_all_values_equal() -> None:
    # доля ошибок 0 во всех 10+10 прогонах: каждая пара — ничья (0,5), U = 100·0,5 = 50 = E[U]
    u, p = st.mann_whitney_exact([0.0] * 10, [0.0] * 10)
    assert u == 50.0 and p == 1.0
    tests = st.pairwise_tests({10: {"a": [0.0] * 10, "b": [0.0] * 10}}, alpha=0.05)
    assert tests[0].p_holm == 1.0 and not tests[0].significant and tests[0].cliffs_delta == 0.0


def test_holm_hand_example() -> None:
    # p = [0.01, 0.04, 0.03], m=3: сорт. 0.01·3=0.03; 0.03·2=0.06; 0.04·1=0.04→max(0.06)=0.06
    assert st.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])


def test_holm_caps_at_one_and_validates() -> None:
    assert st.holm([0.5, 0.9]) == [1.0, 1.0]
    assert st.holm([]) == []
    with pytest.raises(ValueError):
        st.holm([1.5])
    with pytest.raises(ValueError):
        st.holm([math.nan])


def test_holm_with_n5_cannot_reach_significance_over_18_tests() -> None:
    p = [st.min_exact_p(5, 5)] * 18
    assert min(st.holm(p)) > 0.05  # вывод OQ-4 / F-8


def test_bootstrap_ci_deterministic_and_contains_mean() -> None:
    x = [10.0, 12.0, 11.0, 13.0, 9.0, 10.5, 11.5, 12.5, 10.0, 11.0]
    a = st.bootstrap_ci(x, 0.95, 2000, seed=1)
    b = st.bootstrap_ci(x, 0.95, 2000, seed=1)
    assert a == b
    assert a[0] < float(np.mean(x)) < a[1]


def test_bootstrap_constant_sample_degenerate() -> None:
    assert st.bootstrap_ci([5.0] * 10, 0.95, 500, seed=0) == (5.0, 5.0)


@pytest.mark.parametrize("bad", [[], [1.0, math.nan], [math.inf]])
def test_invalid_samples_rejected(bad: list[float]) -> None:
    with pytest.raises(ValueError):
        st.cliffs_delta(bad, [1.0])


def test_pairwise_tests_structure() -> None:
    samples = {500: {"a": [1, 2, 3, 4, 5], "b": [10, 11, 12, 13, 14], "c": [6, 7, 8, 9, 9.5]}}
    res = st.pairwise_tests(samples, alpha=0.05)
    assert len(res) == 3  # C(3,2)
    ab = next(r for r in res if (r.a, r.b) == ("a", "b"))
    assert ab.p == pytest.approx(2 / 252)
    assert ab.cliffs_delta == -1.0
    # Холм внутри ступени: m=3
    assert ab.p_holm == pytest.approx(min(1.0, 3 * 2 / 252))
