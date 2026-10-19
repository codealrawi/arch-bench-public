"""Модели статьи: закон Литтла, формула (2), формула (3), формула (4)."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from scipy.optimize import curve_fit


# ─── Закон Литтла (замкнутая модель): X = N / (T + Z) ────────────────────────────────────
@dataclass(frozen=True)
class LittleCheck:
    vus: int
    rt_avg_s: float
    think_s: float
    x_pred: float
    x_measured: float
    rel_deviation: float
    warning: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def littles_law_throughput(vus: int, rt_avg_s: float, think_s: float) -> float:
    if vus < 0 or rt_avg_s < 0 or think_s < 0:
        raise ValueError("N, T, Z должны быть неотрицательны")
    if rt_avg_s + think_s == 0:
        raise ValueError("T + Z = 0")
    return vus / (rt_avg_s + think_s)


def check_littles_law(
    vus: int, rt_avg_ms: float, think_s: float, x_measured: float, tolerance: float
) -> LittleCheck:
    rt_s = rt_avg_ms / 1000.0
    x_pred = littles_law_throughput(vus, rt_s, think_s)
    dev = (x_measured - x_pred) / x_pred if x_pred > 0 else math.inf
    return LittleCheck(vus, rt_s, think_s, x_pred, x_measured, dev, abs(dev) > tolerance)


# ─── Формула (2): T(R) = T0 · (1 + (R/Rsat)^α) ────────────────────────────────────────────
def saturation_model(r: np.ndarray, t0: float, r_sat: float, alpha: float) -> np.ndarray:
    """Формула (2) статьи. r — нагрузка (VU), t0 — мс, r_sat — VU, alpha — безразмерный."""
    r = np.asarray(r, dtype=float)
    return t0 * (1.0 + np.power(r / r_sat, alpha))


@dataclass(frozen=True)
class SaturationFit:
    applicable: bool
    reason: str | None
    n_points: int
    t0: float | None
    r_sat: float | None
    alpha: float | None
    r2: float | None
    t0_se: float | None
    r_sat_se: float | None
    alpha_se: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_non_decreasing(y: np.ndarray) -> bool:
    return bool(np.all(np.diff(np.asarray(y, dtype=float)) >= 0))


def fit_saturation_model(r: list[float] | np.ndarray, t: list[float] | np.ndarray) -> SaturationFit:
    """Нелинейный МНК (протокол P-56). Модель монотонно возрастает при α>0, поэтому при
    немонотонных данных фиксируется «модель неприменима» без подгонки."""
    rr, tt = np.asarray(r, dtype=float), np.asarray(t, dtype=float)
    if rr.size != tt.size:
        raise ValueError("r и t разной длины")
    if not (np.all(np.isfinite(rr)) and np.all(np.isfinite(tt))):
        raise ValueError("NaN/Inf во входных данных")
    order = np.argsort(rr)
    rr, tt = rr[order], tt[order]
    na = SaturationFit(False, None, int(rr.size), None, None, None, None, None, None, None)
    if rr.size < 4:
        return SaturationFit(**{**na.to_dict(), "reason": "меньше 4 точек для 3 параметров"})
    if not is_non_decreasing(tt):
        return SaturationFit(
            **{**na.to_dict(), "reason": "зависимость немонотонна — модель (2) неприменима"}
        )
    p0 = [float(tt[0]), float(np.median(rr)), 1.0]
    try:
        popt, pcov = curve_fit(
            saturation_model,
            rr,
            tt,
            p0=p0,
            bounds=([0, 1e-9, 1e-6], [np.inf, np.inf, 20.0]),
            maxfev=20000,
        )
    except (RuntimeError, ValueError) as exc:
        return SaturationFit(**{**na.to_dict(), "reason": f"МНК не сошёлся: {exc}"})
    pred = saturation_model(rr, *popt)
    ss_res = float(np.sum((tt - pred) ** 2))
    ss_tot = float(np.sum((tt - tt.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else math.nan
    se = np.sqrt(np.diag(pcov)) if np.all(np.isfinite(pcov)) else [math.nan] * 3
    return SaturationFit(
        True,
        None,
        int(rr.size),
        float(popt[0]),
        float(popt[1]),
        float(popt[2]),
        r2,
        float(se[0]),
        float(se[1]),
        float(se[2]),
    )


# ─── Формула (3): E(R1,R2) = [X(R2)/X(R1)] / (R2/R1) ─────────────────────────────────────
def scaling_efficiency(r1: float, x1: float, r2: float, x2: float) -> float:
    if not r2 > r1 > 0:
        raise ValueError("нужно R2 > R1 > 0")
    if x1 <= 0:
        raise ValueError("X(R1) должна быть > 0")
    if x2 < 0:
        raise ValueError("X(R2) не может быть отрицательной")
    return (x2 / x1) / (r2 / r1)


# ─── Формула (4): T(R) ≈ R/X_max − Z при R > R* ──────────────────────────────────────────
@dataclass(frozen=True)
class AsymptoticPoint:
    vus: int
    rt_measured_ms: float
    rt_asymptotic_ms: float
    rel_deviation: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def asymptotic_rt_ms(vus: int, x_max: float, think_s: float) -> float:
    if x_max <= 0:
        raise ValueError("X_max должна быть > 0")
    return (vus / x_max - think_s) * 1000.0


def asymptotic_check(
    vus: list[int], rt_ms: list[float], x: list[float], think_s: float
) -> tuple[float, int, list[AsymptoticPoint]]:
    """X_max = max измеренной X; R* = наименьшая ступень, на которой X ≥ 0,95·X_max.
    Для ступеней R > R* сравнивает измеренное RT с R/X_max − Z (диагностика, P-58)."""
    if not (len(vus) == len(rt_ms) == len(x)) or not vus:
        raise ValueError("непустые списки одинаковой длины")
    x_max = max(x)
    r_star = min(v for v, xi in zip(vus, x, strict=True) if xi >= 0.95 * x_max)
    pts = []
    for v, rt, _ in zip(vus, rt_ms, x, strict=True):
        if v > r_star:
            a = asymptotic_rt_ms(v, x_max, think_s)
            pts.append(AsymptoticPoint(v, rt, a, (rt - a) / a if a > 0 else math.nan))
    return x_max, r_star, pts
