"""Таблицы статьи и вспомогательные таблицы — только из агрегатов/прогонов, без ручных чисел."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from experiments.analysis import mcda
from experiments.analysis.models import (
    asymptotic_check,
    check_littles_law,
    fit_saturation_model,
    scaling_efficiency,
)
from experiments.analysis.statistics import pairwise_tests
from experiments.config import ARCHITECTURES, BenchConfig

ARCH_RU = {
    "monolith": "Монолит",
    "three_tier": "Трёхуровневая",
    "microservices": "Микросервисы",
    "serverless": "Бессерверная",
}


def _f(x: float | None, nd: int = 1) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "н/о"
    return f"{x:.{nd}f}"


def _cell(agg_row: pd.Series, metric: str, nd: int = 1) -> str:
    """mean ± SD [CI]; при failed-прогонах — явная пометка."""
    n_runs, n_ok = int(agg_row["n_runs"]), int(agg_row["n_ok"])
    if n_ok == 0:
        return f"failed ({n_runs}/{n_runs})"
    s = (
        f"{_f(agg_row[f'{metric}__mean'], nd)} ± {_f(agg_row[f'{metric}__sd'], nd)} "
        f"[{_f(agg_row[f'{metric}__ci_lo'], nd)}; {_f(agg_row[f'{metric}__ci_hi'], nd)}]"
    )
    if n_ok < n_runs:
        s += f" †failed {n_runs - n_ok}/{n_runs}"
    return s


def _pivot(agg: pd.DataFrame, metric: str, nd: int = 1) -> pd.DataFrame:
    steps = sorted(agg["vus"].unique())
    data: dict[str, list[str]] = {"Нагрузка, VU": [str(s) for s in steps]}
    for arch in ARCHITECTURES:
        col = []
        for s in steps:
            r = agg[(agg["architecture"] == arch) & (agg["vus"] == s)]
            col.append(_cell(r.iloc[0], metric, nd) if not r.empty else "нет данных")
        data[ARCH_RU[arch]] = col
    return pd.DataFrame(data)


def table1(agg: pd.DataFrame) -> pd.DataFrame:
    """Табл. 1: RT_avg, мс — mean ± SD [95 % бутстреп-ДИ] по прогонам."""
    return _pivot(agg, "rt_mean")


def _row(agg: pd.DataFrame, arch: str, vus: int) -> pd.Series | None:
    r = agg[(agg["architecture"] == arch) & (agg["vus"] == vus)]
    return None if r.empty else r.iloc[0]


def max_admissible_load(
    agg: pd.DataFrame, arch: str, sla_ms: float, error_threshold: float
) -> int | None:
    """Наибольшая ступень, где во ВСЕХ прогонах ступень ok, mean RT_p95 ≤ SLA и
    mean доля ошибок ≤ порога. Только ступени теста, без интерполяции (ТЗ §5.2)."""
    best: int | None = None
    for _, r in agg[agg["architecture"] == arch].sort_values("vus").iterrows():
        if (
            int(r["n_ok"]) == int(r["n_runs"])
            and r["rt_p95__mean"] <= sla_ms
            and r["error_share__mean"] <= error_threshold
        ):
            best = int(r["vus"])
    return best


def table2(agg: pd.DataFrame, cfg: BenchConfig, sla_ms: float) -> pd.DataFrame:
    ref, ft = cfg.mcda.reference_step, cfg.mcda.fault_tolerance_step
    rows: dict[str, list[str]] = {
        "Показатель": [
            f"Максимальная допустимая нагрузка (RT_p95 ≤ {sla_ms:g} мс, ошибки ≤ "
            f"{cfg.mcda.error_threshold:.0%}), VU",
            "Пиковая пропускная способность (max по ступеням от mean RPS), запросов/с",
            f"RT_avg при {ref} VU, мс",
            f"RT_p95 при {ref} VU, мс",
            f"Доля ошибок при {ft} VU, %",
        ]
    }
    for arch in ARCHITECTURES:
        sub = agg[agg["architecture"] == arch]
        if sub.empty:
            rows[ARCH_RU[arch]] = ["нет данных"] * 5
            continue
        mal = max_admissible_load(agg, arch, sla_ms, cfg.mcda.error_threshold)
        peak = sub.loc[sub["rps__mean"].idxmax()]
        r_ref, r_ft = _row(agg, arch, ref), _row(agg, arch, ft)
        rows[ARCH_RU[arch]] = [
            "ни одна ступень" if mal is None else str(mal),
            f"{_f(peak['rps__mean'])} (при {int(peak['vus'])} VU)",
            _cell(r_ref, "rt_mean") if r_ref is not None else "нет данных",
            _cell(r_ref, "rt_p95") if r_ref is not None else "нет данных",
            (
                (
                    f"{_f(100 * r_ft['error_share__mean'], 2)} ± "
                    f"{_f(100 * r_ft['error_share__sd'], 2)}"
                    + (
                        f" †failed {int(r_ft['n_failed'])}/{int(r_ft['n_runs'])}"
                        if int(r_ft["n_failed"])
                        else ""
                    )
                )
                if r_ft is not None
                else "нет данных"
            ),
        ]
    return pd.DataFrame(rows)


def littles_table(agg: pd.DataFrame, cfg: BenchConfig) -> pd.DataFrame:
    out = []
    for _, r in agg.iterrows():
        if int(r["n_ok"]) == 0:
            continue
        c = check_littles_law(
            int(r["vus"]),
            float(r["rt_mean__mean"]),
            cfg.load.think_time_s,
            float(r["rps__mean"]),
            cfg.statistics.littles_law_tolerance,
        )
        out.append({"architecture": r["architecture"], **c.to_dict()})
    return pd.DataFrame(out)


def saturation_table(agg: pd.DataFrame, run_level: pd.DataFrame) -> pd.DataFrame:
    """Подгонка (2) по средним (ступени, где все прогоны ok) и по каждому прогону."""
    out = []
    for arch in ARCHITECTURES:
        sub = agg[(agg["architecture"] == arch) & (agg["n_ok"] == agg["n_runs"])]
        if sub.empty:
            continue
        fit = fit_saturation_model(sub["vus"].to_numpy(), sub["rt_mean__mean"].to_numpy())
        out.append(
            {
                "architecture": arch,
                "level": "mean_of_runs",
                "steps": ",".join(str(int(v)) for v in sub["vus"]),
                **fit.to_dict(),
            }
        )
        for run, g in run_level[
            (run_level["architecture"] == arch) & (run_level["status"] == "ok")
        ].groupby("run"):
            fr = fit_saturation_model(g["vus"].to_numpy(), g["rt_mean"].to_numpy())
            out.append(
                {
                    "architecture": arch,
                    "level": str(run),
                    "steps": ",".join(str(int(v)) for v in sorted(g["vus"])),
                    **fr.to_dict(),
                }
            )
    return pd.DataFrame(out)


def scaling_table(agg: pd.DataFrame) -> pd.DataFrame:
    out = []
    for arch in ARCHITECTURES:
        sub = agg[agg["architecture"] == arch].sort_values("vus")
        steps, xs = sub["vus"].tolist(), sub["rps__mean"].tolist()
        for (r1, x1), (r2, x2) in zip(
            zip(steps, xs, strict=True), zip(steps[1:], xs[1:], strict=True), strict=False
        ):
            e = scaling_efficiency(r1, x1, r2, x2) if x1 > 0 else math.nan
            out.append(
                {"architecture": arch, "r1": int(r1), "r2": int(r2), "x1": x1, "x2": x2, "E": e}
            )
    return pd.DataFrame(out)


def asymptotic_table(agg: pd.DataFrame, cfg: BenchConfig) -> pd.DataFrame:
    out = []
    for arch in ARCHITECTURES:
        sub = agg[(agg["architecture"] == arch) & (agg["n_ok"] > 0)].sort_values("vus")
        if len(sub) < 2:
            continue
        x_max, r_star, pts = asymptotic_check(
            sub["vus"].astype(int).tolist(),
            sub["rt_mean__mean"].tolist(),
            sub["rps__mean"].tolist(),
            cfg.load.think_time_s,
        )
        for p in pts:
            out.append({"architecture": arch, "x_max": x_max, "r_star": r_star, **p.to_dict()})
        if not pts:
            out.append({"architecture": arch, "x_max": x_max, "r_star": r_star, "vus": None})
    return pd.DataFrame(out)


def stats_table(run_level: pd.DataFrame, cfg: BenchConfig) -> pd.DataFrame:
    samples: dict[int, dict[str, list[float]]] = {}
    for step in cfg.statistics.compare_steps:
        g = run_level[(run_level["vus"] == step) & (run_level["status"] == "ok")]
        samples[step] = {
            a: g.loc[g["architecture"] == a, "rt_mean"].tolist() for a in ARCHITECTURES
        }
    tests = pairwise_tests(samples, cfg.statistics.alpha)
    df = pd.DataFrame([t.to_dict() for t in tests])
    if not df.empty:
        notes = []
        for step in cfg.statistics.compare_steps:
            g = run_level[run_level["vus"] == step]
            excl = g[g["status"] != "ok"].groupby("architecture").size().to_dict()
            notes.append(f"{step}: исключены failed-прогоны {excl}" if excl else "")
        df.attrs["notes"] = [n for n in notes if n]
    return df


def resources_table(agg: pd.DataFrame, cfg: BenchConfig) -> pd.DataFrame:
    out = []
    for _, r in agg.iterrows():
        row: dict[str, Any] = {"architecture": r["architecture"], "vus": int(r["vus"])}
        for c in [c for c in agg.columns if c.startswith("cpu_s_")]:
            row[c.removesuffix("__mean")] = r[c]
        out.append(row)
    return pd.DataFrame(out)


def table3(
    agg: pd.DataFrame, cfg: BenchConfig, sla_ms: float, expert_path: Path
) -> tuple[pd.DataFrame, list[str]]:
    """Формула (1). Возвращает (таблица, примечания). Score — только при полных данных."""
    notes: list[str] = []
    archs = list(ARCHITECTURES)
    ref, ft = cfg.mcda.reference_step, cfg.mcda.fault_tolerance_step
    x: dict[str, dict[str, float | None]] = {c: {} for c in mcda.CRITERIA}
    for a in archs:
        mal = max_admissible_load(agg, a, sla_ms, cfg.mcda.error_threshold)
        x["scalability"][a] = float(mal) if mal is not None else 0.0
        r_ft, r_ref = _row(agg, a, ft), _row(agg, a, ref)
        x["fault_tolerance"][a] = (
            (1 - float(r_ft["error_share__mean"])) if r_ft is not None else None
        )
        x["performance"][a] = (
            1000.0 / float(r_ref["rt_mean__mean"])
            if r_ref is not None and int(r_ref["n_ok"]) == int(r_ref["n_runs"])
            else None
        )
        cpu_cols = [
            c for c in ("cpu_s_request_path__mean", "cpu_s_database__mean") if c in agg.columns
        ]
        if r_ref is not None and cpu_cols and float(r_ref["ok_rps__mean"]) > 0:
            cpu_h = sum(float(r_ref[c]) for c in cpu_cols) / 3600.0
            n_ok = float(r_ref["ok_rps__mean"]) * cfg.load.measure_s
            x["cost"][a] = cpu_h / (n_ok / 1e6)
        else:
            x["cost"][a] = None
    if all(v is None for v in x["cost"].values()):
        notes.append("Стоимость не вычислена: нет данных мониторинга CPU (resources.csv).")
    expert = mcda.load_expert_input(expert_path, archs)
    for c in mcda.EXPERT_CRITERIA:
        x[c] = expert.scores.get(c, {a: None for a in archs})
    r = {c: mcda.normalize(x[c], mcda.BENEFIT[c]) for c in mcda.CRITERIA}
    rows = {"Критерий": list(mcda.CRITERIA) + ["Score(A_k)"]}
    sc = mcda.score(r, expert.weights, archs) if expert.complete and expert.weights else None
    if not expert.complete:
        notes.append(
            "Score не вычислен: экспертные данные не получены (" + "; ".join(expert.missing) + ")."
        )
    for a in archs:
        rows[ARCH_RU[a]] = [_f(r[c][a], 3) for c in mcda.CRITERIA] + [
            _f(sc[a], 3) if sc else "не вычислен"
        ]
    rows["w_j"] = (
        [_f(expert.weights[c], 2) for c in mcda.CRITERIA]
        if expert.weights
        else ["INPUT"] * len(mcda.CRITERIA)
    ) + ["1.00" if expert.weights else "INPUT"]
    return pd.DataFrame(rows), notes


def to_markdown(df: pd.DataFrame) -> str:
    if df.empty:
        return "_нет данных_\n"
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, r in df.iterrows():
        cells = []
        for v in r.tolist():
            if isinstance(v, float):
                cells.append(_f(v, 4) if abs(v) < 10 else _f(v, 2))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def write_table(df: pd.DataFrame, out_dir: Path, name: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_dir / f"{name}.csv", index=False, encoding="utf-8")
    (out_dir / f"{name}.md").write_text(to_markdown(df), encoding="utf-8")


__all__ = ["np"]
