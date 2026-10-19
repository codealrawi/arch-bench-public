"""Операционный анализ (план анализа серии 2, §9): только формулы над измеренными величинами.

Измерено (k6, окно measure длительностью T):
- интервалы запросов [t − d, t] (t — окончание, d — длительность);
- фактическое число VU во времени (gauge ``vus``);
- длительности итераций (запрос + think time + накладные расходы JS).

Формулы:
- закон Литтла L = λ·W: L — среднее по времени число запросов «в системе» (по интервалам,
  обрезанным окном), λ — завершения в окне / T, W — средняя длительность завершённых в окне;
- закон времени ответа закрытой системы N = X·(R + Z): N — среднее измеренное число VU,
  X = λ, R = W, Z — измеренное время размышления = средняя длительность итерации − средняя
  длительность запроса (один запрос на итерацию);
- асимптотические границы закрытой системы (теоретическая модель, НЕ измерение):
  X(N) ≤ min(N/(D+Z), X_max), R(N) ≥ max(D, N/X_max − Z), N* = (D+Z)·X_max.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class LittleEmpirical:
    l_measured: float  # среднее число запросов в системе (по времени)
    lam: float  # завершений в секунду
    w_s: float  # средняя длительность завершённых, с
    lam_w: float  # λ·W
    rel_dev: float  # |L − λW| / L
    n_completed: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def littles_empirical(points: pd.DataFrame, t0: float, window_s: float) -> LittleEmpirical | None:
    """L измеряется независимо от λ и W: по перекрытию интервалов запросов с окном.

    Учитываются запросы всех фаз, кроме setup (запрос, начатый в warmup, занимает систему и
    в окне measure). Граничный эффект: запросы, начатые до окна и завершённые в нём, входят
    в W целиком, а в L — частично; при W ≪ T он мал, при W ~ таймаута — порядка W/T.
    """
    if window_s <= 0:
        raise ValueError("window_s должна быть > 0")
    p = points[points["phase"] != "setup"]
    if p.empty:
        return None
    end = p["t"].to_numpy(dtype=float)
    dur = p["duration_ms"].to_numpy(dtype=float) / 1000.0
    start = end - dur
    t1 = t0 + window_s
    overlap = np.clip(np.minimum(end, t1) - np.maximum(start, t0), 0.0, None)
    l_measured = float(overlap.sum() / window_s)
    done = (end >= t0) & (end < t1)
    n = int(done.sum())
    if n == 0:
        return None
    lam = n / window_s
    w = float(dur[done].mean())
    lw = lam * w
    rel = abs(l_measured - lw) / l_measured if l_measured > 0 else math.nan
    return LittleEmpirical(l_measured, lam, w, lw, rel, n)


@dataclass(frozen=True)
class ClosedMeasured:
    n_mean: float  # измеренное среднее число VU в окне
    x: float  # завершений в секунду
    r_s: float  # среднее время ответа, с
    z_meas_s: float  # измеренное время размышления, с
    n_pred: float  # X·(R + Z_meas)
    rel_dev: float  # |N − X(R+Z)| / N

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def closed_system_measured(
    points: pd.DataFrame,
    vus: pd.DataFrame,
    iterations: pd.DataFrame,
    t0: float,
    window_s: float,
) -> ClosedMeasured | None:
    """Закон времени ответа N = X·(R + Z) только по измеренным величинам.

    None — если в сырых данных нет рядов vus или iteration_duration (тогда Z не измерено,
    и номинальное значение из конфигурации НЕ подставляется).
    """
    t1 = t0 + window_s
    v = vus[(vus["t"] >= t0) & (vus["t"] <= t1)]
    it = iterations[(iterations["t"] >= t0) & (iterations["t"] < t1)]
    m = points[(points["phase"] != "setup") & (points["t"] >= t0) & (points["t"] < t1)]
    if v.empty or it.empty or m.empty:
        return None
    n_mean = float(v["n"].mean())
    x = len(m) / window_s
    r = float(m["duration_ms"].mean()) / 1000.0
    z = float(it["duration_ms"].mean()) / 1000.0 - r
    n_pred = x * (r + z)
    rel = abs(n_mean - n_pred) / n_mean if n_mean > 0 else math.nan
    return ClosedMeasured(n_mean, x, r, z, n_pred, rel)


@dataclass(frozen=True)
class Bounds:
    n: float
    x_upper: float  # min(N/(D+Z), X_max)
    r_lower_s: float  # max(D, N/X_max − Z)
    regime: str  # "light" (N < N*) | "heavy"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def asymptotic_bounds(n: float, d_s: float, z_s: float, x_max: float) -> Bounds:
    """Асимптотические границы закрытой системы (теоретическая модель)."""
    if d_s <= 0 or x_max <= 0 or z_s < 0:
        raise ValueError("D > 0, X_max > 0, Z ≥ 0")
    x_up = min(n / (d_s + z_s), x_max)
    r_lo = max(d_s, n / x_max - z_s)
    n_star = (d_s + z_s) * x_max
    return Bounds(n, x_up, r_lo, "light" if n < n_star else "heavy")


def n_star(d_s: float, z_s: float, x_max: float) -> float:
    """Точка перегиба N* = (D + Z)·X_max."""
    return (d_s + z_s) * x_max
