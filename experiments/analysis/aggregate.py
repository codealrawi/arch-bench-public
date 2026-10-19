"""Агрегирование по прогонам: конфигурация × ступень (протокол P-44).

Для RT-метрик используются только прогоны со статусом ступени ``ok``: у ``failed`` ступени
RT «не определено» (протокол §12.1). Число ok/failed прогонов выводится всегда.
Доля ошибок и RPS агрегируются по всем прогонам.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from experiments.analysis.statistics import bootstrap_ci
from experiments.config import LOAD_OPERATIONS

RT_METRICS = (
    "rt_mean",
    "rt_median",
    "rt_sd",
    "rt_min",
    "rt_max",
    "rt_p50",
    "rt_p95",
    "rt_p99",
    "rt_ok_mean",
    "rt_ok_p95",
    *(f"rt_{op}_{s}" for op in LOAD_OPERATIONS for s in ("mean", "p95")),
)
ALL_RUN_METRICS = ("rps", "ok_rps", "error_share", "share_4xx")
# ресурсы и сэмплеры: среднее по прогонам
MEAN_PREFIXES = (
    "cpu_s_",
    "mem_bytes_max_",
    "pg_conn_",
    "pg_active_",
    "replicas_",
    "db_reset_",
    "little_",
    "closed_",
)


def describe(values: list[float], level: float, b: int, seed: int) -> dict[str, float]:
    v = [x for x in values if x is not None and math.isfinite(x)]
    if not v:
        return {k: math.nan for k in ("mean", "median", "sd", "min", "max", "ci_lo", "ci_hi")}
    a = np.asarray(v, dtype=float)
    lo, hi = bootstrap_ci(a, level, b, seed) if a.size >= 2 else (math.nan, math.nan)
    return {
        "mean": float(a.mean()),
        "median": float(np.median(a)),
        "sd": float(a.std(ddof=1)) if a.size > 1 else math.nan,
        "min": float(a.min()),
        "max": float(a.max()),
        "ci_lo": lo,
        "ci_hi": hi,
    }


def aggregate(run_level: pd.DataFrame, level: float, b: int, seed: int) -> pd.DataFrame:
    if run_level.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for (arch, vus), g in run_level.groupby(["architecture", "vus"], sort=True):
        ok = g[g["status"] == "ok"]
        row: dict[str, Any] = {
            "architecture": arch,
            "vus": int(vus),
            "n_runs": int(len(g)),
            "n_ok": int(len(ok)),
            "n_failed": int((g["status"] == "failed").sum()),
        }
        for metric in RT_METRICS:
            if metric in g.columns:
                for k, v in describe(ok[metric].tolist(), level, b, seed).items():
                    row[f"{metric}__{k}"] = v
        for metric in ALL_RUN_METRICS:
            for k, v in describe(g[metric].tolist(), level, b, seed).items():
                row[f"{metric}__{k}"] = v
        for col in [c for c in g.columns if c.startswith(MEAN_PREFIXES)]:
            row[f"{col}__mean"] = float(g[col].mean())
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["architecture", "vus"]).reset_index(drop=True)
