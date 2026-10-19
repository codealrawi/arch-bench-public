"""Оркестратор в dry-run: полный цикл без инфраструктуры (команды не выполняются)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiments.orchestrator import run as orch


def test_interleaved_order() -> None:
    a = ["m", "t", "ms", "s"]
    assert orch.interleaved_order(a, 1) == ["m", "t", "ms", "s"]
    assert orch.interleaved_order(a, 2) == ["t", "ms", "s", "m"]
    assert orch.interleaved_order(a, 5) == a


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-only-password")  # TEST DATA ONLY
    monkeypatch.setenv("JWT_SECRET", "t" * 40)
    monkeypatch.setenv("OPENFAAS_PASSWORD", "test-only")
    monkeypatch.delenv("GENERATOR_SSH", raising=False)
    monkeypatch.delenv("SLA_MS", raising=False)


def test_dry_run_full_cycle(tmp_path: Path, env: None) -> None:
    results = tmp_path / "results"
    code = orch.main(
        [
            "--environment",
            "local-validation",
            "--target-host",
            "127.0.0.1",
            "--runs",
            "2",
            "--results",
            str(results),
            "--dry-run",
        ]
    )
    assert code == 0
    for arch in ("monolith", "three_tier", "microservices", "serverless"):
        for r in (1, 2):
            rd = results / arch / f"run_{r:02d}"
            meta = json.loads((rd / "meta.json").read_text(encoding="utf-8"))
            assert meta["environment"] == "local-validation"
            assert meta["sla_ms"] == 500
            assert meta["steps"] == [10, 100]
            assert meta["invalid_reasons"] == []
            assert meta["resources"]["max_request_path_cpu"] == pytest.approx(4.0)
            for vus in (10, 100):
                sm = json.loads((rd / f"step_{vus:04d}" / "step_meta.json").read_text())
                assert sm["vus"] == vus
    # одинаковый seed k6 у всех архитектур для (прогон, ступень)
    seeds = {
        json.loads((results / a / "run_02" / "step_0100" / "step_meta.json").read_text())["k6_seed"]
        for a in ("monolith", "serverless")
    }
    assert len(seeds) == 1


def test_refuses_to_overwrite(tmp_path: Path, env: None) -> None:
    results = tmp_path / "results"
    (results / "monolith" / "run_01").mkdir(parents=True)
    code = orch.main(
        [
            "--environment",
            "local-validation",
            "--target-host",
            "127.0.0.1",
            "--architectures",
            "monolith",
            "--runs",
            "1",
            "--results",
            str(results),
            "--dry-run",
        ]
    )
    assert code == 1


def test_server_requires_generator_and_prometheus(
    tmp_path: Path, env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from experiments.config import load_bench_config
    from experiments.orchestrator.shell import Shell

    args = orch.argparse.Namespace(
        environment="server",
        target_host="10.0.0.1",
        architectures=["monolith"],
        runs=1,
        start_run=1,
        max_attempts=1,
        prometheus_url=None,
        results=str(tmp_path / "r"),
        config=str(orch.DEFAULT_CONFIG_PATH),
        dry_run=False,
    )
    exp = orch.Experiment(load_bench_config(), args, Shell(dry_run=False))
    with pytest.raises(orch.RunAborted):
        exp.preflight()


def test_sla_missing_aborts(tmp_path: Path, env: None) -> None:
    import yaml

    raw = yaml.safe_load(orch.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["sla"]["sla_ms"] = None
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    code = orch.main(
        [
            "--environment",
            "local-validation",
            "--target-host",
            "127.0.0.1",
            "--config",
            str(cfgp),
            "--results",
            str(tmp_path / "r"),
            "--dry-run",
        ]
    )
    assert code == 2
