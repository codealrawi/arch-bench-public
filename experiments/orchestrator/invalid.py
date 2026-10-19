"""Критерии невалидного прогона (протокол §12.2), проверяются сразу после каждой ступени.

Невалидность — сбой стенда, а не результат архитектуры. Критерии заданы заранее в
bench.yaml (invalid_run) и не подбираются по результатам.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from experiments.analysis.load_raw import RawDataError, load_points
from experiments.analysis.metrics import classify
from experiments.config import BenchConfig


def _longest_run_above(df: pd.DataFrame, threshold: float) -> float:
    """Наибольшая длительность (с) подряд идущих замеров с cpu_busy_share ≥ threshold."""
    if df.empty:
        return 0.0
    best = cur = 0.0
    prev_t: float | None = None
    for t, share in zip(df["t"], df["cpu_busy_share"], strict=True):
        dt = 0.0 if prev_t is None else t - prev_t
        cur = cur + dt if share >= threshold else 0.0
        best = max(best, cur)
        prev_t = t
    return best


def _max_gap(path: Path, t0: float, t1: float) -> float | None:
    if not path.is_file():
        return None
    df = pd.read_csv(path)
    if df.empty or "t" not in df:
        return t1 - t0
    ts = sorted(set(float(x) for x in df["t"] if t0 <= float(x) <= t1))
    if not ts:
        return t1 - t0
    edges = [t0, *ts, t1]
    return max(b - a for a, b in zip(edges, edges[1:], strict=False))


def evaluate_step(
    step_dir: Path, cfg: BenchConfig, k6_exit: int, monitored_files: list[str]
) -> list[str]:
    inv = cfg.invalid_run
    reasons: list[str] = []
    if k6_exit != 0:
        reasons.append(f"k6 завершился с кодом {k6_exit} (аварийно)")
    summary = step_dir / "k6_summary.json"
    if summary.is_file():
        metrics = json.loads(summary.read_text(encoding="utf-8")).get("metrics", {})
        dropped = (metrics.get("dropped_iterations") or {}).get("count", 0)
        if dropped:
            reasons.append(f"dropped_iterations = {dropped}")
    else:
        reasons.append("нет k6_summary.json")
    try:
        points, start = load_points(step_dir / "points.json.gz")
    except RawDataError as exc:
        reasons.append(f"сырые данные: {exc}")
        return reasons
    if start is None:
        reasons.append("нет момента старта сценария в сырых данных")
        return reasons
    t0 = start + cfg.load.ramp_s + cfg.load.warmup_s
    t1 = t0 + cfg.load.measure_s
    c = classify(points, float(cfg.load.timeout_s))
    m = c[c["phase"] == "measure"]
    if len(m) and float(m["is_4xx"].mean()) > inv.max_4xx_share:
        reasons.append(
            f"доля 4xx {float(m['is_4xx'].mean()):.4f} > {inv.max_4xx_share}"
            " (дефект сценария/стенда)"
        )
    gcpu = step_dir / "generator_cpu.csv"
    if gcpu.is_file():
        g = pd.read_csv(gcpu)
        g = g[(g["t"] >= t0) & (g["t"] <= t1)]
        longest = _longest_run_above(g, inv.generator_cpu_max)
        if longest > inv.generator_cpu_max_duration_s:
            reasons.append(
                f"CPU генератора ≥ {inv.generator_cpu_max:.0%} в течение "
                f"{longest:.0f} с (> {inv.generator_cpu_max_duration_s} с)"
            )
    else:
        reasons.append("нет generator_cpu.csv (загрузка генератора не измерена)")
    for name in monitored_files:
        gap = _max_gap(step_dir / name, t0, t1)
        if gap is None:
            reasons.append(f"нет файла мониторинга {name}")
        elif gap > inv.max_monitoring_gap_s:
            reasons.append(
                f"пропуск мониторинга {name}: {gap:.0f} с > " f"{inv.max_monitoring_gap_s} с"
            )
    return reasons
