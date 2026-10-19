"""Обход results/ и расчёт метрик уровня «конфигурация × прогон × ступень».

Структура (reproducibility.md §3):
    results/<arch>/run_NN/meta.json
    results/<arch>/run_NN/step_VVVV/{points.json.gz, step_meta.json, k6_summary.json,
                                     resources.csv, pg_connections.csv, pods.csv,
                                     generator_cpu.csv}
    results/<arch>/_invalid/...   — не анализируются, но перечисляются
    results/exclusions.yaml       — исключения валидных прогонов с основанием
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from experiments.analysis.load_raw import load_aux, load_points
from experiments.analysis.metrics import step_metrics
from experiments.analysis.queueing import closed_system_measured, littles_empirical
from experiments.config import ARCHITECTURES, BenchConfig

RUN_RE = re.compile(r"^run_(\d{2,})$")
STEP_RE = re.compile(r"^step_(\d{4,})$")


class ResultsError(RuntimeError):
    pass


@dataclass
class Inventory:
    runs: list[dict[str, Any]] = field(default_factory=list)
    invalid: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    environments: set[str] = field(default_factory=set)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ResultsError(f"нет файла {path}") from exc
    except json.JSONDecodeError as exc:
        raise ResultsError(f"повреждённый JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ResultsError(f"{path}: ожидался объект JSON")
    return data


def load_exclusions(results: Path) -> dict[tuple[str, str], str]:
    p = results / "exclusions.yaml"
    if not p.is_file():
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out: dict[tuple[str, str], str] = {}
    for item in raw.get("exclusions", []) or []:
        reason = str(item.get("reason", "")).strip()
        if not reason:
            raise ResultsError("exclusions.yaml: у исключения нет основания (reason)")
        out[(str(item["architecture"]), str(item["run"]))] = reason
    return out


def _resource_columns(step_dir: Path) -> dict[str, float]:
    """CPU-секунды и максимум памяти по группам (request_path / auxiliary / database /
    infrastructure) за окно measure."""
    p = step_dir / "resources.csv"
    if not p.is_file():
        return {}
    df = pd.read_csv(p)
    if df.empty or not {"group", "cpu_seconds"} <= set(df.columns):
        return {}
    out = {f"cpu_s_{k}": float(v) for k, v in df.groupby("group")["cpu_seconds"].sum().items()}
    # путь запроса (бюджет 4 vCPU) = приложение + шлюз
    out["cpu_s_request_path"] = out.get("cpu_s_application", 0.0) + out.get("cpu_s_gateway", 0.0)
    if "mem_bytes_max" in df.columns:
        out |= {
            f"mem_bytes_max_{k}": float(v)
            for k, v in df.groupby("group")["mem_bytes_max"].sum().items()
        }
    return out


def _sampler_columns(step_dir: Path, t0: float, t1: float) -> dict[str, float]:
    """Соединения PostgreSQL и реплики в окне measure (сэмплеры оркестратора)."""
    out: dict[str, float] = {}
    pg = step_dir / "pg_connections.csv"
    if pg.is_file():
        df = pd.read_csv(pg)
        df = df[(df["t"] >= t0) & (df["t"] <= t1)]
        if not df.empty:
            per_t = df.groupby("t")["n"].sum()
            active = df[df["state"] == "active"].groupby("t")["n"].sum()
            out |= {
                "pg_conn_mean": float(per_t.mean()),
                "pg_conn_max": float(per_t.max()),
                "pg_active_max": float(active.max()) if not active.empty else 0.0,
            }
    pods = step_dir / "pods.csv"
    if pods.is_file():
        df = pd.read_csv(pods)
        df = df[(df["t"] >= t0) & (df["t"] <= t1)]
        if not df.empty:
            per_t = df.groupby("t")["ready_replicas"].sum()
            out |= {
                "replicas_min": float(per_t.min()),
                "replicas_max": float(per_t.max()),
                "replicas_mean": float(per_t.mean()),
            }
    return out


def collect(
    results: Path, cfg: BenchConfig, apply_exclusions: bool = True
) -> tuple[pd.DataFrame, Inventory]:
    if not results.is_dir():
        raise ResultsError(f"каталог результатов не найден: {results}")
    inv = Inventory()
    exclusions = load_exclusions(results)
    rows: list[dict[str, Any]] = []
    for arch in ARCHITECTURES:
        adir = results / arch
        if not adir.is_dir():
            continue
        inval = adir / "_invalid"
        if inval.is_dir():
            for d in sorted(inval.iterdir()):
                reason_f = d / "INVALID_REASON.md"
                inv.invalid.append(
                    {
                        "architecture": arch,
                        "run_dir": d.name,
                        "reason": (
                            reason_f.read_text(encoding="utf-8").strip()
                            if reason_f.is_file()
                            else "INVALID_REASON.md отсутствует"
                        ),
                    }
                )
        for run_dir in sorted(p for p in adir.iterdir() if RUN_RE.match(p.name)):
            meta = read_json(run_dir / "meta.json")
            env = str(meta.get("environment", "unknown"))
            inv.environments.add(env)
            key = (arch, run_dir.name)
            if apply_exclusions and key in exclusions:
                inv.excluded.append(
                    {"architecture": arch, "run": run_dir.name, "reason": exclusions[key]}
                )
                continue
            inv.runs.append({"architecture": arch, "run": run_dir.name, "environment": env})
            for step_dir in sorted(p for p in run_dir.iterdir() if STEP_RE.match(p.name)):
                rows.append(_step_row(arch, run_dir.name, env, step_dir, cfg))
    df = pd.DataFrame(rows)
    return df, inv


def _step_row(arch: str, run: str, env: str, step_dir: Path, cfg: BenchConfig) -> dict[str, Any]:
    smeta = read_json(step_dir / "step_meta.json")
    vus = int(smeta["vus"])
    points, start = load_points(step_dir / "points.json.gz")
    if start is None:
        raise ResultsError(f"{step_dir}: в сырых данных нет момента старта сценария")
    ld = cfg.load
    t_measure = start + ld.ramp_s + ld.warmup_s
    m = step_metrics(
        points,
        t_measure_start=t_measure,
        measure_s=float(ld.measure_s),
        timeout_s=float(ld.timeout_s),
        failed_window_s=cfg.metrics.failed_window_s,
        failed_share=cfg.metrics.failed_error_share,
        percentile_method=cfg.metrics.percentile_method,
        count_transport_errors=cfg.metrics.count_transport_errors,
    )
    before = (smeta.get("db_state_before") or {}).get("counts") or {}
    after = (smeta.get("db_state_after") or {}).get("counts") or {}
    row: dict[str, Any] = {
        "architecture": arch,
        "run": run,
        "environment": env,
        "vus": vus,
        "status": m.status,
        "failed_reason": m.failed_reason,
        "n_requests": m.n_requests,
        "rps": m.rps,
        "ok_rps": m.ok_rps,
        "error_share": m.error_share,
        "n_5xx": m.n_5xx,
        "n_timeout": m.n_timeout,
        "n_4xx": m.n_4xx,
        "share_4xx": m.share_4xx,
        "n_transport_error": m.n_transport_error,
        "k6_seed": smeta.get("k6_seed"),
        "db_posts_before": before.get("posts"),
        "db_posts_after": after.get("posts"),
        "db_reset_s": (smeta.get("db_state_before") or {}).get("reset_s"),
    }
    row.update(_sampler_columns(step_dir, t_measure, t_measure + ld.measure_s))
    for prefix, s in (("rt", m.rt_all), ("rt_ok", m.rt_ok)):
        for f in ("mean", "median", "sd", "min", "max", "p50", "p95", "p99"):
            row[f"{prefix}_{f}"] = getattr(s, f) if s is not None else math.nan
    for op, s in m.rt_by_op.items():
        row[f"rt_{op}_mean"] = s.mean if s is not None else math.nan
        row[f"rt_{op}_p95"] = s.p95 if s is not None else math.nan
    row.update(_resource_columns(step_dir))
    row.update(_queueing_columns(step_dir, points, t_measure, float(ld.measure_s)))
    return row


def _queueing_columns(
    step_dir: Path, points: pd.DataFrame, t0: float, window_s: float
) -> dict[str, float]:
    """Эмпирический закон Литтла и закон закрытой системы (queueing.py) по сырым точкам.

    Отсутствующие величины остаются NaN — номинальные значения не подставляются.
    """
    out: dict[str, float] = {}
    le = littles_empirical(points, t0, window_s)
    if le is not None:
        out |= {
            "little_l": le.l_measured,
            "little_lambda": le.lam,
            "little_w_s": le.w_s,
            "little_lambda_w": le.lam_w,
            "little_rel_dev": le.rel_dev,
        }
    vus, its = load_aux(step_dir / "points.json.gz")
    cm = closed_system_measured(points, vus, its, t0, window_s)
    if cm is not None:
        out |= {
            "closed_n_mean": cm.n_mean,
            "closed_x": cm.x,
            "closed_r_s": cm.r_s,
            "closed_z_meas_s": cm.z_meas_s,
            "closed_n_pred": cm.n_pred,
            "closed_rel_dev": cm.rel_dev,
        }
    return out
