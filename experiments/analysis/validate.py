"""Автоматическая проверка результатов (задание §18).

Проверки:
  1. результаты и выход анализа существуют;
  2. значения в допустимых диапазонах, нет NaN/Inf там, где они запрещены;
  3. число прогонов и ступеней совпадает с протоколом;
  4. aggregates.csv пересчитывается из run_level.csv (и, с --deep, run_level — из сырых данных);
  5. таблица 1 соответствует агрегатам;
  6. рисунки построены на актуальных агрегатах (SHA-256 в метаданных PNG);
  7. MANIFEST.sha256 сырых файлов не нарушен;
  8. --for-article: только environment=server, нет фикстур, git commit чистый, версия протокола.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image

from experiments.analysis import aggregate, collect, tables
from experiments.analysis.run_analysis import sha256_file
from experiments.config import ARCHITECTURES, BenchConfig


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: int = 0

    def check(self, ok: bool, msg: str, warn: bool = False) -> None:
        self.checks += 1
        if not ok:
            (self.warnings if warn else self.errors).append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors


def _verify_manifest(results: Path, rep: Report) -> None:
    man = results / "MANIFEST.sha256"
    if not man.is_file():
        rep.check(False, "MANIFEST.sha256 отсутствует (сырые файлы не защищены хешами)", warn=True)
        return
    for line in man.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split(maxsplit=1)
        p = results / rel.strip()
        if not p.is_file():
            rep.check(False, f"MANIFEST: файл отсутствует: {rel}")
            continue
        rep.check(
            hashlib.sha256(p.read_bytes()).hexdigest() == digest,
            f"MANIFEST: хеш не совпадает (файл изменён после записи): {rel}",
        )


def _compare_frames(a: pd.DataFrame, b: pd.DataFrame, what: str, rep: Report) -> None:
    if list(a.columns) != list(b.columns) or len(a) != len(b):
        rep.check(False, f"{what}: структура не совпадает")
        return
    for c in a.columns:
        x, y = a[c], b[c]
        if pd.api.types.is_numeric_dtype(x) and pd.api.types.is_numeric_dtype(y):
            same = np.allclose(
                x.to_numpy(float), y.to_numpy(float), rtol=1e-9, atol=1e-9, equal_nan=True
            )
        else:
            same = x.astype(str).tolist() == y.astype(str).tolist()
        rep.check(bool(same), f"{what}: столбец {c} не пересчитывается")


def validate(
    results: Path, out: Path, cfg: BenchConfig, for_article: bool = False, deep: bool = False
) -> Report:
    rep = Report()
    for p in (results, out / "run_level.csv", out / "aggregates.csv", out / "analysis_meta.json"):
        rep.check(p.exists(), f"нет {p}")
    if not rep.ok:
        return rep
    meta = json.loads((out / "analysis_meta.json").read_text(encoding="utf-8"))
    run_level = pd.read_csv(out / "run_level.csv")
    agg_saved = pd.read_csv(out / "aggregates.csv")

    # 2. диапазоны
    for col in ("error_share", "share_4xx"):
        v = run_level[col].dropna()
        rep.check(bool(((v >= 0) & (v <= 1)).all()), f"{col} вне [0, 1]")
    for col in ("rps", "ok_rps", "n_requests"):
        v = run_level[col]
        rep.check(bool(np.isfinite(v).all() and (v >= 0).all()), f"{col}: NaN/Inf/отрицательные")
    ok = run_level[run_level["status"] == "ok"]
    for col in ("rt_mean", "rt_p95", "rt_p99", "rt_min", "rt_max"):
        v = ok[col]
        rep.check(
            bool(np.isfinite(v).all() and (v >= 0).all()),
            f"{col}: NaN/Inf/отрицательные в ok-ступенях",
        )
    rep.check(
        bool(
            (ok["rt_min"] <= ok["rt_mean"]).all()
            and (ok["rt_mean"] <= ok["rt_max"]).all()
            and (ok["rt_p95"] <= ok["rt_p99"] + 1e-9).all()
        ),
        "нарушен порядок min ≤ mean ≤ max или p95 ≤ p99",
    )
    rep.check(set(run_level["status"]) <= {"ok", "failed"}, "неизвестный статус ступени")

    # 3. число прогонов/ступеней
    envs = set(run_level["environment"])
    steps = cfg.load.steps_server if envs == {"server"} else None
    for arch, g in run_level.groupby("architecture"):
        per_run = g.groupby("run")["vus"].apply(lambda s: sorted(s.astype(int)))
        expected = steps or per_run.iloc[0]
        for run, got in per_run.items():
            rep.check(got == list(expected), f"{arch}/{run}: ступени {got} ≠ {list(expected)}")
        if for_article:
            rep.check(
                g["run"].nunique() == cfg.runs.count,
                f"{arch}: {g['run'].nunique()} прогонов ≠ {cfg.runs.count}",
            )
    if for_article:
        for arch in ARCHITECTURES:
            rep.check(arch in set(run_level["architecture"]), f"нет результатов {arch}")

    # 4. пересчёт агрегатов (и сырых данных при --deep)
    st = cfg.statistics
    agg_re = aggregate.aggregate(run_level, st.ci_level, st.bootstrap_b, st.bootstrap_seed)
    _compare_frames(agg_saved, agg_re, "aggregates.csv", rep)
    if deep:
        rl_re, _ = collect.collect(results, cfg)
        _compare_frames(
            run_level.reset_index(drop=True),
            rl_re[run_level.columns].reset_index(drop=True),
            "run_level.csv",
            rep,
        )

    # 5. таблица 1
    t1_path = out / "tables" / "table1_rt_avg.csv"
    if t1_path.is_file():
        t1_saved = pd.read_csv(t1_path, dtype=str, keep_default_na=False)
        t1_re = tables.table1(agg_saved).astype(str)
        rep.check(
            t1_saved.values.tolist() == t1_re.values.tolist(),
            "table1 не соответствует aggregates.csv",
        )
    else:
        rep.check(False, "нет tables/table1_rt_avg.csv")

    # 6. рисунки
    sha = sha256_file(out / "aggregates.csv")
    rep.check(sha == meta.get("aggregates_sha256"), "analysis_meta: хеш агрегатов не совпадает")
    for name in meta.get("figures", []):
        p = out / "figures" / name
        if not p.is_file():
            rep.check(False, f"нет рисунка {name}")
            continue
        desc = Image.open(p).info.get("Description", "")
        rep.check(f"sha256={sha}" in desc, f"{name} построен не на актуальных агрегатах")

    # 7. манифест
    _verify_manifest(results, rep)

    # 8. пригодность для статьи
    if for_article:
        rep.check(
            envs == {"server"},
            f"окружения {sorted(envs)}: для статьи допустим только "
            "server (local validation ≠ final scientific)",
        )
        rep.check(bool(meta.get("scientific")), "analysis_meta.scientific = false")
        for arch_dir in (results / a for a in ARCHITECTURES):
            for run_dir in sorted(arch_dir.glob("run_*")) if arch_dir.is_dir() else []:
                m = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
                rep.check(
                    m.get("environment") != "test-fixture",
                    f"{run_dir}: TEST DATA ONLY — фикстура в результатах",
                )
                g = (m.get("host") or {}).get("git") or {}
                rep.check(
                    bool(g.get("commit")) and g.get("dirty") is False,
                    f"{run_dir}: нет git commit или незакоммиченные изменения",
                )
                rep.check(
                    m.get("protocol_version") == cfg.protocol_version,
                    f"{run_dir}: версия протокола {m.get('protocol_version')}",
                )
    nan_cells = int(
        agg_saved.select_dtypes("number")
        .map(lambda v: isinstance(v, float) and math.isinf(v))
        .to_numpy()
        .sum()
    )
    rep.check(nan_cells == 0, "aggregates.csv содержит Inf")
    return rep
