"""Рисунки из агрегатов: PNG 300 dpi, подписи на русском, лог. ось X, планки погрешностей.

В метаданные PNG записывается SHA-256 таблицы агрегатов, из которой построен рисунок —
validate_results.py проверяет, что рисунок построен на актуальных данных.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

from experiments.analysis.tables import ARCH_RU  # noqa: E402

DPI = 300
STYLE = {
    "monolith": ("o", "-"),
    "three_tier": ("s", "-"),
    "microservices": ("^", "-"),
    "serverless": ("D", "-"),
}


def _save(fig: Figure, path: Path, data_sha256: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        path,
        dpi=DPI,
        bbox_inches="tight",
        metadata={"Description": f"arch-bench aggregates sha256={data_sha256}"},
    )
    plt.close(fig)


def _metric_plot(
    agg: pd.DataFrame,
    archs: list[str],
    metric: str,
    ylabel: str,
    title: str,
    path: Path,
    sha: str,
    scale: float = 1.0,
    only_ok: bool = True,
) -> None:
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for a in archs:
        sub = agg[agg["architecture"] == a].sort_values("vus")
        if only_ok:
            sub = sub[sub["n_ok"] > 0]
        if sub.empty:
            continue
        marker, ls = STYLE[a]
        ax.errorbar(
            sub["vus"],
            sub[f"{metric}__mean"] * scale,
            yerr=sub[f"{metric}__sd"].fillna(0) * scale,
            marker=marker,
            linestyle=ls,
            capsize=3,
            label=ARCH_RU[a],
        )
        failed = agg[(agg["architecture"] == a) & (agg["n_ok"] < agg["n_runs"])]
        for _, r in failed.iterrows():
            ax.annotate(
                f"failed {int(r['n_failed'])}/{int(r['n_runs'])}",
                (r["vus"], ax.get_ylim()[1]),
                fontsize=7,
                rotation=90,
                va="top",
            )
    ax.set_xscale("log")
    ax.set_xlabel("Нагрузка, виртуальные пользователи (VU), логарифмическая шкала")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    _save(fig, path, sha)


def make_figures(agg: pd.DataFrame, out_dir: Path, sha: str) -> list[Path]:
    paths = {
        "fig1_rt_monolith_three_tier.png": (
            ["monolith", "three_tier"],
            "rt_mean",
            "Среднее время отклика RT_avg, мс",
            "Рис. 1. RT_avg: монолитная и трёхуровневая",
            1.0,
            True,
        ),
        "fig2_rt_microservices_serverless.png": (
            ["microservices", "serverless"],
            "rt_mean",
            "Среднее время отклика RT_avg, мс",
            "Рис. 2. RT_avg: микросервисная и бессерверная",
            1.0,
            True,
        ),
        "rt_p95_all.png": (
            list(STYLE),
            "rt_p95",
            "RT_p95, мс",
            "95-й перцентиль времени отклика",
            1.0,
            True,
        ),
        "throughput_all.png": (
            list(STYLE),
            "rps",
            "Пропускная способность, запросов/с",
            "Пропускная способность",
            1.0,
            False,
        ),
        "errors_all.png": (
            list(STYLE),
            "error_share",
            "Доля ошибок, %",
            "Доля ошибок (5xx и таймауты)",
            100.0,
            False,
        ),
    }
    written = []
    for name, (archs, metric, ylabel, title, scale, only_ok) in paths.items():
        p = out_dir / name
        _metric_plot(agg, archs, metric, ylabel, title, p, sha, scale, only_ok)
        written.append(p)
    return written
