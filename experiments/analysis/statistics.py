"""Статистика сравнения конфигураций (протокол P-50..P-54).

Единица наблюдения — прогон. Для каждой ступени из ``compare_steps`` сравниваются все пары
конфигураций точным двусторонним U-критерием Манна–Уитни; поправка Холма внутри ступени
(семейство = пары этой ступени); величина эффекта — δ Клиффа.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from itertools import combinations
from typing import Any

import numpy as np
from scipy import stats as sps


def _clean(x: np.ndarray | list[float], name: str) -> np.ndarray:
    a = np.asarray(x, dtype=float)
    if a.ndim != 1 or a.size == 0:
        raise ValueError(f"{name}: нужна непустая одномерная выборка")
    if not np.all(np.isfinite(a)):
        raise ValueError(f"{name}: NaN/Inf в выборке")
    return a


def cliffs_delta(x: np.ndarray | list[float], y: np.ndarray | list[float]) -> float:
    """δ = (#{x_i > y_j} − #{x_i < y_j}) / (n_x·n_y), ∈ [−1, 1]."""
    a, b = _clean(x, "x"), _clean(y, "y")
    diff = a[:, None] - b[None, :]
    return float((np.sum(diff > 0) - np.sum(diff < 0)) / (a.size * b.size))


def mann_whitney_exact(
    x: np.ndarray | list[float], y: np.ndarray | list[float]
) -> tuple[float, float]:
    """(U для x, двустороннее p). Точный метод; при совпадающих значениях scipy переходит
    к асимптотике — такие случаи помечаются отдельно в pairwise_tests."""
    a, b = _clean(x, "x"), _clean(y, "y")
    if np.unique(np.concatenate([a, b])).size == 1:
        # все значения равны (например, доля ошибок 0 во всех прогонах): дисперсия статистики
        # равна 0, scipy даёт NaN; U = n1·n2/2 совпадает с ожиданием → различий нет, p = 1
        return float(a.size * b.size / 2), 1.0
    method = "exact" if np.unique(np.concatenate([a, b])).size == a.size + b.size else "asymptotic"
    res = sps.mannwhitneyu(a, b, alternative="two-sided", method=method)
    return float(res.statistic), float(res.pvalue)


def holm(pvalues: list[float]) -> list[float]:
    """Скорректированные p по Холму (монотонные, ≤ 1)."""
    m = len(pvalues)
    if m == 0:
        return []
    if any(not (0 <= p <= 1) or math.isnan(p) for p in pvalues):
        raise ValueError("p-значения должны быть в [0, 1]")
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, (m - rank) * pvalues[i])
        adjusted[i] = min(1.0, running)
    return adjusted


def bootstrap_ci(
    x: np.ndarray | list[float], level: float, b: int, seed: int
) -> tuple[float, float]:
    """Percentile-бутстреп ДИ для среднего."""
    a = _clean(x, "x")
    if not 0 < level < 1 or b < 1:
        raise ValueError("level ∈ (0,1), b ≥ 1")
    rng = np.random.default_rng(seed)
    means = rng.choice(a, size=(b, a.size), replace=True).mean(axis=1)
    lo, hi = np.percentile(means, [(1 - level) / 2 * 100, (1 + level) / 2 * 100])
    return float(lo), float(hi)


def min_exact_p(n1: int, n2: int) -> float:
    """Наименьшее достижимое точное двустороннее p при полном разделении выборок."""
    return 2.0 / math.comb(n1 + n2, n1)


@dataclass(frozen=True)
class PairTest:
    step: int
    a: str
    b: str
    n_a: int
    n_b: int
    u: float
    p: float
    p_holm: float
    significant: bool
    cliffs_delta: float
    method: str
    min_attainable_p: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def pairwise_tests(samples: dict[int, dict[str, list[float]]], alpha: float) -> list[PairTest]:
    """samples[step][architecture] = значения метрики по прогонам (failed-прогоны исключаются
    вызывающим кодом с явной пометкой)."""
    out: list[PairTest] = []
    for step in sorted(samples):
        archs = sorted(a for a, v in samples[step].items() if len(v) > 0)
        raw: list[tuple[str, str, float, float, str, float]] = []
        for a, b in combinations(archs, 2):
            xa, xb = samples[step][a], samples[step][b]
            u, p = mann_whitney_exact(xa, xb)
            ties = np.unique(np.concatenate([xa, xb])).size < len(xa) + len(xb)
            raw.append((a, b, u, p, "asymptotic(ties)" if ties else "exact", cliffs_delta(xa, xb)))
        adj = holm([r[3] for r in raw])
        for (a, b, u, p, method, d), ph in zip(raw, adj, strict=True):
            na, nb = len(samples[step][a]), len(samples[step][b])
            out.append(
                PairTest(step, a, b, na, nb, u, p, ph, ph < alpha, d, method, min_exact_p(na, nb))
            )
    return out
