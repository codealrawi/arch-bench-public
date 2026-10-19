"""Метрики уровня «прогон × ступень» (протокол P-43, P-45..P-48).

Определения:
- ошибка (протокол v2.0, P-46 + решение автора M-4): HTTP 5xx ИЛИ таймаут (k6 error_code 1050
  или длительность ≥ таймаута) ИЛИ нет ответа (status 0: сброс/отказ соединения) — при
  metrics.count_transport_errors=true; все три категории выводятся раздельно;
- 4xx в долю ошибок не входят (выводятся отдельно; > 1 % — признак невалидного прогона);
- RT-статистики считаются по всем завершённым запросам фазы measure (как http_req_duration
  в k6) и отдельно — только по успешным;
- RPS = число завершённых запросов фазы measure / длительность окна измерения;
- ступень ``failed`` (P-47): доля ошибок > порога в любом скользящем окне ``window_s`` секунд
  (шаг 1 с). Окно без единого завершённого запроса также считается ``failed`` — сервис
  ничего не обслужил, а «0 ошибок из 0» не означает работоспособность.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from experiments.config import LOAD_OPERATIONS

K6_REQUEST_TIMEOUT_CODE = 1050


@dataclass(frozen=True)
class RtStats:
    n: int
    mean: float
    median: float
    sd: float
    min: float
    max: float
    p50: float
    p95: float
    p99: float


@dataclass(frozen=True)
class StepMetrics:
    n_requests: int
    measure_s: float
    rps: float
    ok_rps: float
    error_share: float
    n_5xx: int
    n_timeout: int
    n_4xx: int
    n_transport_error: int
    share_4xx: float
    status: str
    failed_reason: str | None
    rt_all: RtStats | None
    rt_ok: RtStats | None
    rt_by_op: dict[str, RtStats | None]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify(
    df: pd.DataFrame, timeout_s: float, count_transport_errors: bool = True
) -> pd.DataFrame:
    out = df.copy()
    timeout_ms = timeout_s * 1000.0
    out["is_timeout"] = (out["error_code"] == K6_REQUEST_TIMEOUT_CODE) | (
        (out["status"] == 0) & (out["duration_ms"] >= timeout_ms)
    )
    out["is_5xx"] = (out["status"] >= 500) & (out["status"] <= 599)
    out["is_4xx"] = (out["status"] >= 400) & (out["status"] <= 499)
    out["is_transport"] = (out["status"] == 0) & ~out["is_timeout"]
    out["is_error"] = out["is_5xx"] | out["is_timeout"]
    if count_transport_errors:
        out["is_error"] = out["is_error"] | out["is_transport"]
    out["is_ok"] = (out["status"] >= 200) & (out["status"] <= 399)
    return out


def rt_stats(values: np.ndarray, method: str) -> RtStats | None:
    v = np.asarray(values, dtype=float)
    if v.size == 0:
        return None
    if not np.all(np.isfinite(v)) or np.any(v < 0):
        raise ValueError("RT содержит NaN/Inf/отрицательные значения")
    sd = float(np.std(v, ddof=1)) if v.size > 1 else math.nan
    m: Any = method  # значение проверяется numpy; заглушки типов требуют Literal
    return RtStats(
        n=int(v.size),
        mean=float(np.mean(v)),
        median=float(np.median(v)),
        sd=sd,
        min=float(np.min(v)),
        max=float(np.max(v)),
        p50=float(np.percentile(v, 50, method=m)),
        p95=float(np.percentile(v, 95, method=m)),
        p99=float(np.percentile(v, 99, method=m)),
    )


def detect_failed(
    measure: pd.DataFrame, t_start: float, measure_s: float, window_s: int, threshold: float
) -> str | None:
    """Возвращает причину ``failed`` или None. Скользящее окно с шагом 1 с."""
    n_bins = int(math.ceil(measure_s))
    if n_bins < window_s:
        raise ValueError("окно измерения короче окна правила failed")
    idx = np.floor(measure["t"].to_numpy() - t_start).astype(int)
    idx = np.clip(idx, 0, n_bins - 1)
    total = np.bincount(idx, minlength=n_bins).astype(float)
    errors = np.bincount(idx, weights=measure["is_error"].to_numpy(dtype=float), minlength=n_bins)
    kernel = np.ones(window_s)
    tot_w = np.convolve(total, kernel, mode="valid")
    err_w = np.convolve(errors, kernel, mode="valid")
    if np.any(tot_w == 0):
        first = int(np.argmax(tot_w == 0))
        return f"окно {first}–{first + window_s} с без завершённых запросов"
    share = err_w / tot_w
    if np.any(share > threshold):
        k = int(np.argmax(share > threshold))
        return f"доля ошибок {share[k]:.3f} > {threshold} в окне {k}–{k + window_s} с"
    return None


def measurement_window(df: pd.DataFrame) -> tuple[float, float]:
    """Начало окна measure = время окончания первого запроса фазы measure, если не задано явно.

    Оркестратор записывает точное время старта сценария; эта функция — запасной вариант
    для валидации и тестов.
    """
    m = df[df["phase"] == "measure"]
    if m.empty:
        raise ValueError("нет запросов фазы measure")
    return float(m["t"].min()), float(m["t"].max())


def step_metrics(
    df: pd.DataFrame,
    *,
    t_measure_start: float,
    measure_s: float,
    timeout_s: float,
    failed_window_s: int,
    failed_share: float,
    percentile_method: str,
    count_transport_errors: bool = True,
) -> StepMetrics:
    if measure_s <= 0:
        raise ValueError("measure_s должна быть > 0")
    c = classify(df, timeout_s, count_transport_errors)
    m = c[c["phase"] == "measure"]
    n = int(len(m))
    if n == 0:
        return StepMetrics(
            0,
            measure_s,
            0.0,
            0.0,
            math.nan,
            0,
            0,
            0,
            0,
            math.nan,
            "failed",
            "нет ни одного завершённого запроса в фазе measure",
            None,
            None,
            {op: None for op in LOAD_OPERATIONS},
        )
    reason = detect_failed(m, t_measure_start, measure_s, failed_window_s, failed_share)
    by_op = {
        op: rt_stats(m.loc[m["op"] == op, "duration_ms"].to_numpy(), percentile_method)
        for op in LOAD_OPERATIONS
    }
    return StepMetrics(
        n_requests=n,
        measure_s=measure_s,
        rps=n / measure_s,
        ok_rps=float(m["is_ok"].sum()) / measure_s,
        error_share=float(m["is_error"].mean()),
        n_5xx=int(m["is_5xx"].sum()),
        n_timeout=int(m["is_timeout"].sum()),
        n_4xx=int(m["is_4xx"].sum()),
        n_transport_error=int(m["is_transport"].sum()),
        share_4xx=float(m["is_4xx"].mean()),
        status="failed" if reason else "ok",
        failed_reason=reason,
        rt_all=rt_stats(m["duration_ms"].to_numpy(), percentile_method),
        rt_ok=rt_stats(m.loc[m["is_ok"], "duration_ms"].to_numpy(), percentile_method),
        rt_by_op=by_op,
    )
