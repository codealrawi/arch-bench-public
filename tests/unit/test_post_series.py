"""Анализ по плану серии 2 (post_series.py). TEST DATA ONLY: синтетические агрегаты,
ожидаемые значения — из определений плана (docs/post-series-analysis-plan.md)."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from experiments.analysis import post_series as ps
from experiments.config import ARCHITECTURES, BenchConfig, load_bench_config


@pytest.fixture(scope="module")
def cfg() -> BenchConfig:
    return load_bench_config()


def test_thresholds_are_the_preregistered_ones() -> None:
    # план §3, §5 — изменение порогов = отклонение от плана, тест должен упасть
    assert ps.U_DB_SATURATION == 0.85 and ps.X_GROWTH_MAX == 0.10 and ps.VU_RATIO_MIN == 2.0
    assert ps.CLIFF_THRESHOLDS == (0.147, 0.33, 0.474)


def test_budgets_sum_to_request_path(cfg: BenchConfig) -> None:
    for arch in ARCHITECTURES:
        b = ps.budgets(cfg, arch)
        assert b["application"] + b["gateway"] == pytest.approx(cfg.budget.request_path_vcpu)
        assert b["database"] == cfg.budget.postgres_vcpu and b["node"] == cfg.budget.total_vcpu
    assert ps.budgets(cfg, "monolith")["gateway"] == 0.0


def test_derive_hand_example(cfg: BenchConfig) -> None:
    t = cfg.load.measure_s
    rl = pd.DataFrame(
        [
            {
                "architecture": "monolith",
                "run": "run_01",
                "vus": 100,
                "status": "ok",
                "ok_rps": 10.0,
                "cpu_s_database": 0.5 * t * cfg.budget.postgres_vcpu,  # U_db = 0,5
                "cpu_s_application": 1.0 * t,
                "cpu_s_infrastructure": 0.5 * t,
            }
        ]
    )
    d = ps.derive(rl, cfg).iloc[0]
    assert d["u_db"] == pytest.approx(0.5)
    assert d["vcpu_database"] == pytest.approx(0.5 * cfg.budget.postgres_vcpu)
    # D_db = CPU_db / (X·T) = (0,5·T·2) / (10·T) = 0,1 с = 100 мс
    assert d["d_db_ms"] == pytest.approx(1000.0 * 0.5 * cfg.budget.postgres_vcpu / 10.0)
    assert d["vcpu_node"] == pytest.approx(0.5 * cfg.budget.postgres_vcpu + 1.0 + 0.5)
    assert math.isnan(d["u_gw"])  # у монолита нет шлюза


def _agg(arch: str, steps: list[int], x: list[float], u_db: list[float]) -> pd.DataFrame:
    rows = []
    for s, xi, ui in zip(steps, x, u_db, strict=True):
        rows.append(
            {
                "architecture": arch,
                "vus": s,
                "ok_rps__mean": xi,
                "ok_rps__ci_lo": xi,
                "ok_rps__ci_hi": xi,
                "u_db__mean": ui,
                "u_app__mean": 0.2,
                "u_gw__mean": math.nan,
                "u_node__mean": 0.3,
            }
        )
    return pd.DataFrame(rows)


def test_saturation_point_rules() -> None:
    dagg = pd.concat(
        [
            # 100→200: рост X 5,6 % < 10 %, VU ×2, U_db 0,9 ≥ 0,85 → R* = 200
            _agg("monolith", [10, 100, 200, 500], [10, 90, 95, 96], [0.1, 0.8, 0.9, 0.95]),
            # плато при U_db 0,4 → упор не в БД
            _agg("three_tier", [10, 100, 200], [10, 90, 92], [0.1, 0.3, 0.4]),
            # рост ровно 10 % — НЕ плато (строго < 10 %); U_db ровно 0,85 не спасает
            _agg("microservices", [100, 200], [100, 110], [0.85, 0.85]),
            # U_db ≥ 0,85, но VU выросло меньше чем вдвое → условие 2 не выполнено
            _agg("serverless", [100, 150], [100, 101], [0.9, 0.95]),
        ]
    )
    sat = ps.saturation_points(dagg).set_index("architecture")
    assert sat.loc["monolith", "r_star_db"] == 200
    assert sat.loc["monolith", "status"] == "db_saturated"
    assert sat.loc["monolith", "first_step_u_db_ge_085"] == 200
    assert sat.loc["three_tier", "status"] == "plateau_other_resource"
    assert sat.loc["three_tier", "first_plateau_step"] == 200
    assert sat.loc["three_tier", "max_u_resource"] == "u_db"  # 0,4 > u_app 0,2
    assert sat.loc["microservices", "status"] == "not_saturated_in_range"
    assert sat.loc["serverless", "status"] == "not_saturated_in_range"
    common = ps.common_bottleneck(sat.reset_index())
    assert not common["postgres_common_bottleneck"] and common["db_saturated"] == ["monolith"]


def test_pre_saturation_zone() -> None:
    df = pd.DataFrame({"vus": [10, 100, 200, 500]})
    sat = pd.DataFrame({"architecture": ["monolith", "three_tier"], "r_star_db": [200, None]})
    steps, note = ps.pre_saturation_steps(df, sat)
    assert steps == [10, 100] and "200" in note
    steps, _ = ps.pre_saturation_steps(df, pd.DataFrame({"r_star_db": [None]}))
    assert steps == [10, 100, 200, 500]


def test_effect_labels() -> None:
    assert ps.effect_label(0.1) == "пренебрежимый" and ps.effect_label(-0.2) == "малый"
    assert ps.effect_label(0.4) == "средний" and ps.effect_label(-0.9) == "большой"
