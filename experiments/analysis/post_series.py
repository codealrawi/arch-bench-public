"""Анализ серии по плану docs/post-series-analysis-plan.md (зафиксирован 2026-10-06 ДО результатов).

Вход: run_level (collect.py) и конфигурация. Выход: таблицы, рисунки и отчёт post_series.md.
Пороги — константы плана; после просмотра данных не меняются (план §0, §11).
Формулы — docs/post-series-analysis-methods.md.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from experiments.analysis.aggregate import describe  # noqa: E402
from experiments.analysis.queueing import asymptotic_bounds, n_star  # noqa: E402
from experiments.analysis.statistics import pairwise_tests  # noqa: E402
from experiments.analysis.tables import ARCH_RU, to_markdown, write_table  # noqa: E402
from experiments.config import ARCHITECTURES, BenchConfig  # noqa: E402

# ── константы плана (§3, §5) ─────────────────────────────────────────────────────────────
U_DB_SATURATION = 0.85  # §3 п. 1: средняя загрузка лимита PostgreSQL
X_GROWTH_MAX = 0.10  # §3 п. 2: прирост X < 10 % …
VU_RATIO_MIN = 2.0  # … при росте VU как минимум вдвое
CLIFF_THRESHOLDS = (0.147, 0.33, 0.474)  # §5: Romano et al. (пренебрежимый/малый/средний/большой)
GATEWAY_COMPONENTS = frozenset({"frontend", "gateway", "of_gateway"})
GROUPS = ("database", "application", "gateway", "infrastructure")
PLAN = "docs/post-series-analysis-plan.md"

# метрики сравнения (§4): (столбец run_level, только ok-ступени)
TEST_METRICS: tuple[tuple[str, bool], ...] = (
    ("ok_rps", False),
    ("rt_p95", True),
    ("rt_p99", True),
    ("error_share", False),
    ("d_db_ms", False),
    ("u_db", False),
)
PRE_SATURATION_METRICS: tuple[tuple[str, bool], ...] = (("rt_p95", True), ("ok_rps", False))

ALL_RUN = (
    "ok_rps",
    "rps",
    "error_share",
    "vcpu_database",
    "vcpu_application",
    "vcpu_gateway",
    "vcpu_infrastructure",
    "vcpu_node",
    "u_db",
    "u_app",
    "u_gw",
    "u_node",
    "d_db_ms",
    "d_app_ms",
    "d_gw_ms",
    "pg_conn_mean",
    "pg_conn_max",
    "pg_active_max",
    "little_l",
    "little_lambda_w",
    "little_rel_dev",
    "closed_n_mean",
    "closed_n_pred",
    "closed_z_meas_s",
    "closed_rel_dev",
)
OK_ONLY = ("rt_mean", "rt_p95", "rt_p99")

STATIC_GAPS = (
    "pg_stat_statements по ступеням не сохраняется: МЕХАНИЗМ различий D_db (какие запросы, "
    "сколько их на HTTP-запрос) по сырым данным не раскрывается — только величина D_db.",
    "Троттлинг CPU cgroup (container_cpu_cfs_throttled_*) не экспортируется: U_db ≈ 1 "
    "показывает упор в лимит по потреблению, но время ожидания из-за троттлинга не измерено.",
    "D_i оценивается по CPU-секундам: ожидания ввода-вывода, блокировок и сети в D_i не входят, "
    "поэтому X_max = c/D_i — оптимистичная (верхняя) оценка, а не измерение.",
    "D (суммарный спрос) оценивается средним временем ответа на минимальной ступени: включает "
    "сетевой RTT (~0,6 мс) и возможное ожидание в очередях — это оценка, а не измерение.",
)


# ── производные величины (§2) ────────────────────────────────────────────────────────────
def budgets(cfg: BenchConfig, arch: str) -> dict[str, float]:
    """vCPU-лимиты групп: Σ cpu·max компонентов (для HPA — верхняя граница)."""
    app = gw = 0.0
    for name, c in cfg.architectures[arch].components.items():
        if not c.on_request_path:
            continue
        if name in GATEWAY_COMPONENTS:
            gw += c.cpu * c.max
        else:
            app += c.cpu * c.max
    return {
        "database": cfg.budget.postgres_vcpu,
        "application": app,
        "gateway": gw,
        "node": cfg.budget.total_vcpu,
    }


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    return df[name] if name in df.columns else pd.Series(math.nan, index=df.index, dtype=float)


def derive(run_level: pd.DataFrame, cfg: BenchConfig) -> pd.DataFrame:
    df = run_level.copy()
    t = float(cfg.load.measure_s)
    for g in GROUPS:
        df[f"vcpu_{g}"] = _col(df, f"cpu_s_{g}") / t
    df["cpu_s_node"] = sum(_col(df, f"cpu_s_{g}").fillna(0.0) for g in GROUPS)
    no_res = _col(df, "cpu_s_database").isna() & _col(df, "cpu_s_infrastructure").isna()
    df.loc[no_res, "cpu_s_node"] = math.nan
    df["vcpu_node"] = df["cpu_s_node"] / t
    n_ok = _col(df, "ok_rps") * t
    n_ok = n_ok.where(n_ok > 0)
    for short, g in (("db", "database"), ("app", "application"), ("gw", "gateway")):
        df[f"d_{short}_ms"] = 1000.0 * _col(df, f"cpu_s_{g}") / n_ok
    u_db, u_app, u_gw, u_node = [], [], [], []
    for _, r in df.iterrows():
        b = budgets(cfg, str(r["architecture"]))
        u_db.append(r.get("cpu_s_database", math.nan) / (t * b["database"]))
        u_app.append(
            r.get("cpu_s_application", math.nan) / (t * b["application"])
            if b["application"] > 0
            else math.nan
        )
        u_gw.append(
            r.get("cpu_s_gateway", math.nan) / (t * b["gateway"]) if b["gateway"] > 0 else math.nan
        )
        u_node.append(r["cpu_s_node"] / (t * b["node"]))
    df["u_db"], df["u_app"], df["u_gw"], df["u_node"] = u_db, u_app, u_gw, u_node
    return df


def aggregate_derived(df: pd.DataFrame, cfg: BenchConfig) -> pd.DataFrame:
    st = cfg.statistics
    rows: list[dict[str, Any]] = []
    for (arch, vus), g in df.groupby(["architecture", "vus"], sort=True):
        ok = g[g["status"] == "ok"]
        row: dict[str, Any] = {
            "architecture": arch,
            "vus": int(vus),
            "n_runs": int(len(g)),
            "n_ok": int(len(ok)),
        }
        for m in ALL_RUN:
            vals = _col(g, m).tolist()
            for k, v in describe(vals, st.ci_level, st.bootstrap_b, st.bootstrap_seed).items():
                row[f"{m}__{k}"] = v
        for m in OK_ONLY:
            vals = _col(ok, m).tolist()
            for k, v in describe(vals, st.ci_level, st.bootstrap_b, st.bootstrap_seed).items():
                row[f"{m}__{k}"] = v
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["architecture", "vus"]).reset_index(drop=True)


# ── точка насыщения PostgreSQL (§3) ──────────────────────────────────────────────────────
def saturation_points(dagg: pd.DataFrame) -> pd.DataFrame:
    out: list[dict[str, Any]] = []
    for arch in ARCHITECTURES:
        sub = dagg[dagg["architecture"] == arch].sort_values("vus").reset_index(drop=True)
        if sub.empty:
            continue
        r_star: int | None = None
        plateau: int | None = None
        for i in range(1, len(sub)):
            prev, cur = sub.iloc[i - 1], sub.iloc[i]
            x0, x1 = prev["ok_rps__mean"], cur["ok_rps__mean"]
            ratio = cur["vus"] / prev["vus"]
            flat = (
                ratio >= VU_RATIO_MIN
                and x0 > 0
                and math.isfinite(x1)
                and (x1 - x0) / x0 < X_GROWTH_MAX
            )
            if flat and plateau is None:
                plateau = int(cur["vus"])
            if flat and cur["u_db__mean"] >= U_DB_SATURATION:
                r_star = int(cur["vus"])
                break
        first_u = sub[sub["u_db__mean"] >= U_DB_SATURATION]
        row: dict[str, Any] = {
            "architecture": arch,
            "r_star_db": r_star,
            "first_step_u_db_ge_085": int(first_u["vus"].iloc[0]) if not first_u.empty else None,
            "first_plateau_step": plateau,
        }
        at = sub[sub["vus"] == (r_star if r_star is not None else plateau)]
        if r_star is not None:
            row["status"] = "db_saturated"
        elif plateau is not None:
            row["status"] = "plateau_other_resource"
        else:
            row["status"] = "not_saturated_in_range"
        if not at.empty:
            a = at.iloc[0]
            row |= {
                "x_ok_rps_mean": a["ok_rps__mean"],
                "x_ci_lo": a["ok_rps__ci_lo"],
                "x_ci_hi": a["ok_rps__ci_hi"],
                "u_db": a["u_db__mean"],
                "u_app": a["u_app__mean"],
                "u_gw": a["u_gw__mean"],
                "u_node": a["u_node__mean"],
            }
            us = {k: a[f"{k}__mean"] for k in ("u_db", "u_app", "u_gw")}
            us = {k: v for k, v in us.items() if isinstance(v, float) and math.isfinite(v)}
            row["max_u_resource"] = max(us, key=lambda k: us[k]) if us else None
        out.append(row)
    return pd.DataFrame(out)


def common_bottleneck(sat: pd.DataFrame) -> dict[str, Any]:
    present = set(sat["architecture"]) if not sat.empty else set()
    db = sorted(sat.loc[sat["status"] == "db_saturated", "architecture"]) if present else []
    return {
        "architectures_analysed": sorted(present),
        "db_saturated": db,
        "postgres_common_bottleneck": bool(present) and set(db) == set(ARCHITECTURES),
        "note": (
            "все 4 архитектуры должны присутствовать в данных"
            if present != set(ARCHITECTURES)
            else ""
        ),
    }


# ── статистика (§4, §5) ──────────────────────────────────────────────────────────────────
def effect_label(d: float) -> str:
    a = abs(d)
    if not math.isfinite(a):
        return "н/о"
    s, m, large = CLIFF_THRESHOLDS
    return "пренебрежимый" if a < s else "малый" if a < m else "средний" if a < large else "большой"


def pairwise_by_metric(
    df: pd.DataFrame,
    cfg: BenchConfig,
    metrics: tuple[tuple[str, bool], ...],
    steps: list[int] | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    all_steps = sorted(int(s) for s in df["vus"].unique())
    use = [s for s in all_steps if steps is None or s in steps]
    for metric, only_ok in metrics:
        if metric not in df.columns:
            continue
        samples: dict[int, dict[str, list[float]]] = {}
        for s in use:
            g = df[df["vus"] == s]
            if only_ok:
                g = g[g["status"] == "ok"]
            samples[s] = {
                a: [float(v) for v in g.loc[g["architecture"] == a, metric] if math.isfinite(v)]
                for a in ARCHITECTURES
            }
        for t in pairwise_tests(samples, cfg.statistics.alpha):
            d = t.to_dict()
            d["metric"] = metric
            d["effect"] = effect_label(t.cliffs_delta)
            d["can_reach_alpha"] = t.min_attainable_p < cfg.statistics.alpha
            rows.append(d)
    cols = ["metric", "step", "a", "b", "n_a", "n_b", "u", "p", "p_holm", "significant"]
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out[cols + [c for c in out.columns if c not in cols]]


def pre_saturation_steps(df: pd.DataFrame, sat: pd.DataFrame) -> tuple[list[int], str]:
    steps = sorted(int(s) for s in df["vus"].unique())
    defined = [int(v) for v in sat.get("r_star_db", pd.Series(dtype=float)).dropna()]
    if not defined:
        return steps, "R*_db не определена ни для одной архитектуры — все ступени (план §5)"
    lim = min(defined)
    return [s for s in steps if s < lim], f"ступени < min R*_db = {lim} (план §5)"


# ── закрытая модель (§9): параметры — оценки, границы — теория ───────────────────────────
def closed_model(dagg: pd.DataFrame, cfg: BenchConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    z_model = cfg.load.think_time_s
    params: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for arch in ARCHITECTURES:
        sub = dagg[dagg["architecture"] == arch].sort_values("vus")
        if sub.empty:
            continue
        low = sub.iloc[0]
        b = budgets(cfg, arch)
        d_total = low["rt_mean__mean"] / 1000.0
        demands = {
            "database": low["d_db_ms__mean"] / 1000.0,
            "application": low["d_app_ms__mean"] / 1000.0,
            "gateway": low["d_gw_ms__mean"] / 1000.0,
        }
        x_max_by = {
            k: b[k] / v for k, v in demands.items() if math.isfinite(v) and v > 0 and b[k] > 0
        }
        p: dict[str, Any] = {
            "architecture": arch,
            "estimated_at_vus": int(low["vus"]),
            "D_total_s": d_total,
            "D_db_s": demands["database"],
            "D_app_s": demands["application"],
            "D_gw_s": demands["gateway"],
            "Z_model_s": z_model,
            "Z_measured_s": low["closed_z_meas_s__mean"],
            **{f"X_max_{k}": v for k, v in x_max_by.items()},
        }
        if not x_max_by or not math.isfinite(d_total) or d_total <= 0:
            p["note"] = "недостаточно данных: нет D_i (resources.csv) или RT на мин. ступени"
            params.append(p)
            continue
        bottleneck = min(x_max_by, key=lambda k: x_max_by[k])
        x_max = x_max_by[bottleneck]
        p |= {
            "X_max": x_max,
            "model_bottleneck": bottleneck,
            "N_star": n_star(d_total, z_model, x_max),
        }
        params.append(p)
        for _, r in sub.iterrows():
            bd = asymptotic_bounds(float(r["vus"]), d_total, z_model, x_max)
            x_meas = r["rps__mean"]
            r_meas = r["rt_mean__mean"] / 1000.0 if math.isfinite(r["rt_mean__mean"]) else math.nan
            rows.append(
                {
                    "architecture": arch,
                    "vus": int(r["vus"]),
                    "regime_model": bd.regime,
                    "X_upper_model": bd.x_upper,
                    "X_measured_all": x_meas,
                    "X_measured_ok": r["ok_rps__mean"],
                    "X_over_bound": x_meas / bd.x_upper if bd.x_upper > 0 else math.nan,
                    "R_lower_model_s": bd.r_lower_s,
                    "R_measured_s": r_meas,
                    "N_measured": r["closed_n_mean__mean"],
                    "Z_measured_s": r["closed_z_meas_s__mean"],
                }
            )
    return pd.DataFrame(params), pd.DataFrame(rows)


# ── таблицы-выборки ──────────────────────────────────────────────────────────────────────
def _pick(dagg: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    keep = ["architecture", "vus", "n_runs", "n_ok"]
    sel = [c for c in cols if c in dagg.columns]
    return dagg[keep + sel].copy()


def tables(df: pd.DataFrame, dagg: pd.DataFrame, cfg: BenchConfig) -> dict[str, pd.DataFrame]:
    sat = saturation_points(dagg)
    pre_steps, _ = pre_saturation_steps(df, sat)
    params, bounds = closed_model(dagg, cfg)
    chain_cols = [
        f"{m}__{k}"
        for m in ("ok_rps", "u_db", "rt_p95", "error_share")
        for k in ("mean", "ci_lo", "ci_hi")
    ]
    cpu_cols = [
        f"{m}__mean"
        for m in (
            "vcpu_database",
            "vcpu_application",
            "vcpu_gateway",
            "vcpu_infrastructure",
            "vcpu_node",
            "u_db",
            "u_app",
            "u_gw",
            "u_node",
        )
    ]
    db_cols = [f"{m}__{k}" for m in ("d_db_ms",) for k in ("mean", "ci_lo", "ci_hi")] + [
        "pg_conn_mean__mean",
        "pg_conn_max__mean",
        "pg_active_max__mean",
    ]
    lit_cols = [
        "little_l__mean",
        "little_lambda_w__mean",
        "little_rel_dev__mean",
        "little_rel_dev__max",
        "closed_n_mean__mean",
        "closed_n_pred__mean",
        "closed_z_meas_s__mean",
        "closed_rel_dev__mean",
        "closed_rel_dev__max",
    ]
    lit = _pick(dagg, lit_cols)
    tol = cfg.statistics.littles_law_tolerance
    if "little_rel_dev__max" in lit.columns:
        lit["little_within_tolerance"] = lit["little_rel_dev__max"] <= tol
    if "closed_rel_dev__max" in lit.columns:
        lit["closed_within_tolerance"] = lit["closed_rel_dev__max"] <= tol
    return {
        "ps_saturation_points": sat,
        "ps_chain": _pick(dagg, chain_cols),
        "ps_cpu_utilization": _pick(dagg, cpu_cols),
        "ps_db_load": _pick(dagg, db_cols),
        "ps_littles_law_empirical": lit,
        "ps_pairwise_all_steps": pairwise_by_metric(df, cfg, TEST_METRICS),
        "ps_pairwise_pre_saturation": pairwise_by_metric(
            df, cfg, PRE_SATURATION_METRICS, pre_steps
        ),
        "ps_closed_model_params": params,
        "ps_closed_model_bounds": bounds,
    }


# ── рисунки (§10) ────────────────────────────────────────────────────────────────────────
def _save(fig: Any, path: Path, sha: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=200, bbox_inches="tight", metadata={"Description": f"sha256={sha}"})
    plt.close(fig)
    return path


def _err(sub: pd.DataFrame, m: str, scale: float) -> tuple[Any, Any, Any]:
    mean = sub[f"{m}__mean"].to_numpy(dtype=float) * scale
    lo = (sub[f"{m}__mean"] - sub[f"{m}__ci_lo"]).to_numpy(dtype=float) * scale
    hi = (sub[f"{m}__ci_hi"] - sub[f"{m}__mean"]).to_numpy(dtype=float) * scale
    return mean, np.nan_to_num(np.clip(lo, 0, None)), np.nan_to_num(np.clip(hi, 0, None))


def chain_figures(dagg: pd.DataFrame, sat: pd.DataFrame, out: Path, sha: str) -> list[Path]:
    panels = (
        ("ok_rps", "X, успешных запросов/с", 1.0),
        ("u_db", "U_db = CPU PostgreSQL / лимит", 1.0),
        ("rt_p95", "RT_p95, с", 0.001),
        ("error_share", "Доля ошибок, %", 100.0),
    )
    paths = []
    for arch in ARCHITECTURES:
        sub = dagg[dagg["architecture"] == arch].sort_values("vus")
        if sub.empty:
            continue
        fig, axes = plt.subplots(4, 1, figsize=(6.5, 9), sharex=True)
        r_star = sat.loc[sat["architecture"] == arch, "r_star_db"]
        rs = r_star.iloc[0] if not r_star.empty else None
        for ax, (m, label, scale) in zip(axes, panels, strict=True):
            mean, lo, hi = _err(sub, m, scale)
            ax.errorbar(sub["vus"], mean, yerr=[lo, hi], marker="o", capsize=3)
            if m == "u_db":
                ax.axhline(U_DB_SATURATION, ls=":", c="gray", lw=1)
            if rs is not None and isinstance(rs, (int, float)) and math.isfinite(rs):
                ax.axvline(float(rs), ls="--", c="red", lw=1)
            ax.set_ylabel(label, fontsize=8)
            ax.grid(True, which="both", alpha=0.3)
        axes[-1].set_xscale("log")
        axes[-1].set_xlabel("VU (лог. шкала); точки — средние по прогонам, отрезки — 95 % ДИ")
        axes[0].set_title(
            f"{ARCH_RU[arch]}: VU → X → U_db → RT_p95 → ошибки"
            + (f" (R*_db = {int(rs)})" if rs is not None and math.isfinite(float(rs)) else "")
        )
        paths.append(_save(fig, out / f"ps_chain_{arch}.png", sha))
    return paths


def bounds_figure(bounds: pd.DataFrame, params: pd.DataFrame, out: Path, sha: str) -> Path | None:
    if bounds.empty:
        return None
    fig, axes = plt.subplots(2, 2, figsize=(10, 7))
    for ax, arch in zip(axes.flat, ARCHITECTURES, strict=True):
        b = bounds[bounds["architecture"] == arch]
        p = params[params["architecture"] == arch]
        ax.set_title(ARCH_RU[arch], fontsize=9)
        if b.empty or p.empty or "X_max" not in p.columns or not math.isfinite(p["X_max"].iloc[0]):
            ax.text(0.5, 0.5, "нет данных", ha="center", transform=ax.transAxes)
            continue
        pr = p.iloc[0]
        n = np.logspace(math.log10(b["vus"].min()), math.log10(b["vus"].max()), 100)
        xb = np.minimum(n / (pr["D_total_s"] + pr["Z_model_s"]), pr["X_max"])
        ax.plot(n, xb, "--", c="gray", label="модель: min(N/(D+Z), X_max)")
        # измерения — только точки: линия между ступенями не измерена
        ax.plot(b["vus"], b["X_measured_all"], "o", label="измерено: X (все ответы)")
        ax.plot(b["vus"], b["X_measured_ok"], "s", mfc="none", label="измерено: X (успешные)")
        ax.axvline(pr["N_star"], ls=":", c="red", lw=1, label="модель: N*")
        ax.set_xscale("log")
        ax.grid(True, which="both", alpha=0.3)
        ax.set_xlabel("N = VU")
        ax.set_ylabel("X, запросов/с")
        ax.legend(fontsize=6)
    fig.suptitle("Закрытая модель (теория, оценки D_i) и измерения", fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    return _save(fig, out / "ps_closed_model_bounds.png", sha)


def cpu_figure(dagg: pd.DataFrame, out: Path, sha: str) -> Path:
    fig, axes = plt.subplots(2, 2, figsize=(10, 7), sharey=True)
    for ax, arch in zip(axes.flat, ARCHITECTURES, strict=True):
        sub = dagg[dagg["architecture"] == arch].sort_values("vus")
        ax.set_title(ARCH_RU[arch], fontsize=9)
        if sub.empty:
            ax.text(0.5, 0.5, "нет данных", ha="center", transform=ax.transAxes)
            continue
        x = np.arange(len(sub))
        bottom = np.zeros(len(sub))
        for g in GROUPS:
            v = np.nan_to_num(sub[f"vcpu_{g}__mean"].to_numpy(dtype=float))
            ax.bar(x, v, bottom=bottom, label=g)
            bottom += v
        ax.set_xticks(x, [str(int(v)) for v in sub["vus"]], fontsize=7)
        ax.set_xlabel("VU")
        ax.set_ylabel("vCPU (среднее за окно)")
        ax.grid(True, axis="y", alpha=0.3)
    axes.flat[0].legend(fontsize=7)
    fig.suptitle("CPU по группам: PostgreSQL / application / gateway / infrastructure", fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    return _save(fig, out / "ps_cpu_by_group.png", sha)


# ── пробелы в данных (план §0: не восстанавливать предположениями) ───────────────────────
def data_gaps(df: pd.DataFrame, dagg: pd.DataFrame, cfg: BenchConfig) -> list[str]:
    gaps = list(STATIC_GAPS)
    checks = (
        ("cpu_s_database", "нет resources.csv (CPU по группам)"),
        ("pg_conn_mean", "нет pg_connections.csv в окне"),
        ("little_l", "закон Литтла не вычислен (нет точек)"),
        ("closed_z_meas_s", "нет рядов vus/iteration_duration (Z не измерено)"),
    )
    for col, text in checks:
        miss = df[_col(df, col).isna()]
        if not miss.empty:
            by = miss.groupby("architecture").size().to_dict()
            gaps.append(f"{text}: ступеней {len(miss)} {by}")
    few = dagg[dagg["n_runs"] < 2]
    if not few.empty:
        gaps.append(
            f"ступеней с < 2 прогонами: {len(few)} — ДИ не вычисляются, тесты не информативны"
        )
    n = int(df.groupby(["architecture", "vus"]).size().min()) if not df.empty else 0
    if n and 2.0 / math.comb(2 * n, n) >= cfg.statistics.alpha:
        gaps.append(
            f"при n = {n} прогонов на группу точный U-тест не может дать p < α = "
            f"{cfg.statistics.alpha} (минимально достижимое p = {2.0 / math.comb(2 * n, n):.3f})"
        )
    return gaps


# ── точка входа ──────────────────────────────────────────────────────────────────────────
def run(
    run_level: pd.DataFrame, cfg: BenchConfig, out: Path, sha: str, scientific: bool
) -> dict[str, Any]:
    df = derive(run_level, cfg)
    dagg = aggregate_derived(df, cfg)
    t = tables(df, dagg, cfg)
    tdir, fdir = out / "tables", out / "figures"
    for name, tab in t.items():
        write_table(tab, tdir, name)
    df.to_csv(out / "ps_run_level_derived.csv", index=False, encoding="utf-8")
    dagg.to_csv(out / "ps_aggregates_derived.csv", index=False, encoding="utf-8")
    sat = t["ps_saturation_points"]
    figs = chain_figures(dagg, sat, fdir, sha)
    bf = bounds_figure(t["ps_closed_model_bounds"], t["ps_closed_model_params"], fdir, sha)
    figs += [p for p in (bf, cpu_figure(dagg, fdir, sha)) if p is not None]
    pre_steps, pre_note = pre_saturation_steps(df, sat)
    common = common_bottleneck(sat)
    gaps = data_gaps(df, dagg, cfg)
    _write_report(out, t, common, pre_note, gaps, figs, scientific)
    return {
        "plan": PLAN,
        "thresholds": {
            "u_db_saturation": U_DB_SATURATION,
            "x_growth_max": X_GROWTH_MAX,
            "vu_ratio_min": VU_RATIO_MIN,
            "cliff": CLIFF_THRESHOLDS,
            "alpha": cfg.statistics.alpha,
            "littles_tolerance": cfg.statistics.littles_law_tolerance,
        },
        "common_bottleneck": common,
        "pre_saturation_steps": pre_steps,
        "data_gaps": gaps,
        "figures": [p.name for p in figs],
        "tables": sorted(t),
    }


def _write_report(
    out: Path,
    t: dict[str, pd.DataFrame],
    common: dict[str, Any],
    pre_note: str,
    gaps: list[str],
    figs: list[Path],
    scientific: bool,
) -> None:
    lines = ["# Анализ по плану серии 2 (post-series)", ""]
    if not scientific:
        lines += [
            "> **PIPELINE CHECK — NOT A SCIENTIFIC RESULT.** Данные не серверной серии "
            "(environment ≠ server); выводы не делаются.",
            "",
        ]
    lines += [
        f"План и пороги: `{PLAN}` (зафиксированы до результатов). "
        f"U_db ≥ {U_DB_SATURATION}; прирост X < {X_GROWTH_MAX:.0%} при росте VU ≥ "
        f"{VU_RATIO_MIN:g}×.",
        "",
        "## Точки насыщения PostgreSQL (§3)",
        "",
        to_markdown(t["ps_saturation_points"]),
        f"Общий bottleneck PostgreSQL для всех 4 архитектур: "
        f"**{common['postgres_common_bottleneck']}** "
        f"(насыщены: {common['db_saturated'] or 'нет'}). {common['note']}",
        "",
        f"Зона «до насыщения» (§5): {pre_note}.",
        "",
    ]
    for name, title in (
        ("ps_chain", "Цепочка VU → X → U_db → RT_p95 → ошибки (§10)"),
        ("ps_cpu_utilization", "CPU по группам и загрузка лимитов (§7)"),
        ("ps_db_load", "Нагрузка на PostgreSQL на успешный запрос и соединения (§8)"),
        ("ps_littles_law_empirical", "Закон Литтла и закон закрытой системы — эмпирически (§9)"),
        ("ps_closed_model_params", "Закрытая модель: параметры (оценки, §9)"),
        ("ps_closed_model_bounds", "Закрытая модель: границы (теория) vs измерения (§9)"),
        ("ps_pairwise_pre_saturation", "Манн–Уитни + Холм, δ Клиффа: до насыщения (§5)"),
        ("ps_pairwise_all_steps", "Манн–Уитни + Холм, δ Клиффа: все ступени (§4)"),
    ):
        lines += [f"## {title}", "", to_markdown(t[name]), ""]
    lines += ["## Рисунки", ""] + [f"- `figures/{p.name}`" for p in figs] + [""]
    lines += ["## Ограничения данных (не восстанавливаются предположениями)", ""]
    lines += [f"- {g}" for g in gaps] + [""]
    (out / "post_series.md").write_text("\n".join(lines), encoding="utf-8")
