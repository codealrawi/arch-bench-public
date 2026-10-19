"""Проверка загрузчика конфигурации и инвариантов протокола."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from experiments.config import (
    DEFAULT_CONFIG_PATH,
    SLA_REQUIRED_MESSAGE,
    BenchConfig,
    ConfigError,
    SlaMissingError,
    load_bench_config,
    resolve_sla_ms,
)


@pytest.fixture
def raw() -> dict[str, Any]:
    return yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    p = tmp_path / "bench.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def test_repository_config_is_valid() -> None:
    cfg = load_bench_config()
    assert cfg.protocol_version == "2.1"
    # M-1: чтение 60 / запись 30 / статистика 10 (READ 70 / WRITE 30)
    from experiments.config import READ_OPERATIONS

    m = cfg.load.mix
    assert sum(m[k] for k in READ_OPERATIONS) == pytest.approx(0.70)
    assert m["stats"] == pytest.approx(0.10)
    assert cfg.metrics.count_transport_errors is True  # M-4
    assert cfg.runs.count == 10


def test_every_architecture_has_equal_request_path_budget() -> None:
    cfg = load_bench_config()
    budgets = {name: arch.max_cpu() for name, arch in cfg.architectures.items()}
    # протокол P-31: одинаковый бюджет пути запроса у каждой архитектуры
    assert all(b == pytest.approx(cfg.budget.request_path_vcpu) for b in budgets.values()), budgets


def test_db_pools_do_not_exceed_total() -> None:
    cfg = load_bench_config()
    for arch in cfg.architectures.values():
        assert arch.max_db_connections() <= cfg.postgres.total_app_pool


def test_cpu_over_budget_is_rejected(tmp_path: Path, raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["architectures"]["monolith"]["components"]["app"]["cpu"] = 4.5
    with pytest.raises(ConfigError, match="бюджета пути запроса"):
        load_bench_config(_write(tmp_path, bad))


def test_db_pool_over_total_is_rejected(tmp_path: Path, raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["architectures"]["monolith"]["components"]["app"]["db_pool"] = 30  # 4 воркера × 30
    with pytest.raises(ConfigError, match="суммарный пул"):
        load_bench_config(_write(tmp_path, bad))


def test_mix_must_sum_to_one(tmp_path: Path, raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["load"]["mix"]["feed"] = 0.7
    with pytest.raises(ConfigError, match="mix"):
        load_bench_config(_write(tmp_path, bad))


def test_scale_to_zero_forbidden_in_main_series(tmp_path: Path, raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["architectures"]["serverless"]["scale_to_zero"] = True
    with pytest.raises(ConfigError, match="scale_to_zero"):
        load_bench_config(_write(tmp_path, bad))


def test_unknown_key_is_rejected(tmp_path: Path, raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["load"]["epochs"] = 37
    with pytest.raises(ConfigError):
        load_bench_config(_write(tmp_path, bad))


def test_unsorted_steps_rejected(tmp_path: Path, raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["load"]["steps_server"] = [10, 100, 50]
    with pytest.raises(ConfigError, match="steps_server"):
        load_bench_config(_write(tmp_path, bad))


def test_missing_file() -> None:
    with pytest.raises(ConfigError, match="не найден"):
        load_bench_config("does/not/exist.yaml")


def test_corrupted_yaml(tmp_path: Path) -> None:
    p = tmp_path / "bench.yaml"
    p.write_text("load: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="повреждённый YAML"):
        load_bench_config(p)


def _cfg_with_sla(raw: dict[str, Any], sla: float | None) -> BenchConfig:
    data = copy.deepcopy(raw)
    data["sla"]["sla_ms"] = sla
    return BenchConfig.model_validate(data)


def test_sla_missing_everywhere_raises_exact_message(raw: dict[str, Any]) -> None:
    cfg = _cfg_with_sla(raw, None)
    with pytest.raises(SlaMissingError) as exc:
        resolve_sla_ms(cfg, env={})
    assert str(exc.value) == SLA_REQUIRED_MESSAGE
    assert "SLA_MS is required." in str(exc.value)


def test_sla_from_env_only(raw: dict[str, Any]) -> None:
    cfg = _cfg_with_sla(raw, None)
    assert resolve_sla_ms(cfg, env={"SLA_MS": "750"}) == 750.0


def test_sla_from_config_only(raw: dict[str, Any]) -> None:
    cfg = _cfg_with_sla(raw, 500)
    assert resolve_sla_ms(cfg, env={}) == 500.0


def test_sla_conflict_rejected(raw: dict[str, Any]) -> None:
    cfg = _cfg_with_sla(raw, 500)
    with pytest.raises(ConfigError, match="≠"):
        resolve_sla_ms(cfg, env={"SLA_MS": "400"})


@pytest.mark.parametrize("value", ["-1", "0", "nan", "inf", "abc"])
def test_sla_invalid_values(raw: dict[str, Any], value: str) -> None:
    cfg = _cfg_with_sla(raw, None)
    with pytest.raises(ConfigError):
        resolve_sla_ms(cfg, env={"SLA_MS": value})
