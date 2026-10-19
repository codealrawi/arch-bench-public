"""Регрессия конвейера анализа на детерминированной фикстуре (TEST DATA ONLY).

Regression baseline ≠ научный результат: снимок фиксирует поведение кода на синтетических
данных. Если код анализа изменён намеренно и результат изменился, тест СООБЩАЕТ об изменении
и показывает разницу; новое значение не объявляется ошибочным автоматически. Обновить снимок:

    ARCH_BENCH_UPDATE_BASELINE=1 pytest tests/regression
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pandas as pd
import pytest

from experiments.analysis import run_analysis
from tests.fixtures import results_fixture as fx

BASELINE = Path(__file__).with_name("baseline_fixture.json")
KEYS = [
    "rt_mean__mean",
    "rt_mean__sd",
    "rt_p95__mean",
    "rps__mean",
    "error_share__mean",
    "n_ok",
    "n_failed",
]


def _snapshot(tmp: Path) -> dict[str, dict[str, float]]:
    cfg_path = fx.test_config(tmp)
    fx.build(tmp / "results", fail={("monolith", 2, 40)}, seed=123)
    run_analysis.run(tmp / "results", tmp / "out", cfg_path, tmp / "none.yaml")
    agg = pd.read_csv(tmp / "out" / "aggregates.csv")
    return {
        f"{r['architecture']}@{int(r['vus'])}": {k: float(r[k]) for k in KEYS}
        for _, r in agg.iterrows()
    }


def test_analysis_regression(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLA_MS", raising=False)
    snap = _snapshot(tmp_path)
    if os.environ.get("ARCH_BENCH_UPDATE_BASELINE") == "1" or not BASELINE.is_file():
        BASELINE.write_text(json.dumps(snap, indent=1, sort_keys=True), encoding="utf-8")
        pytest.skip(f"снимок записан: {BASELINE.name} (проверьте и закоммитьте)")
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    changes = []
    for key in sorted(set(base) | set(snap)):
        if key not in base or key not in snap:
            changes.append(f"{key}: есть только в {'снимке' if key in base else 'новом'}")
            continue
        for k in KEYS:
            a, b = base[key][k], snap[key][k]
            if not (
                math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12) or (math.isnan(a) and math.isnan(b))
            ):
                changes.append(f"{key}.{k}: было {a!r}, стало {b!r}")
    assert not changes, (
        "РЕЗУЛЬТАТ АНАЛИЗА НА ФИКСТУРЕ ИЗМЕНИЛСЯ (это сообщение, а не вердикт об ошибке):\n  "
        + "\n  ".join(changes[:30])
        + "\nЕсли изменение намеренное — обновите снимок (см. docstring)."
    )
