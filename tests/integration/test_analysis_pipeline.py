"""Сквозной конвейер анализа на фикстуре (TEST DATA ONLY): raw → метрики → агрегаты → таблицы
→ рисунки → отчёт → validate."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from experiments.analysis import run_analysis
from experiments.analysis.validate import validate
from experiments.config import load_bench_config
from tests.fixtures import results_fixture as fx


@pytest.fixture
def pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    monkeypatch.delenv("SLA_MS", raising=False)
    cfg_path = fx.test_config(tmp_path)
    results, out = tmp_path / "results", tmp_path / "out"
    expected = fx.build(results, fail={("monolith", 2, 40)})
    meta = run_analysis.run(results, out, cfg_path, tmp_path / "no_experts.yaml")
    return {
        "cfg_path": cfg_path,
        "results": results,
        "out": out,
        "expected": expected,
        "meta": meta,
    }


def test_run_level_matches_independent_computation(pipeline: dict) -> None:  # type: ignore[type-arg]
    rl = pd.read_csv(pipeline["out"] / "run_level.csv")
    for (arch, run, vus), exp in pipeline["expected"].items():
        row = rl[
            (rl["architecture"] == arch) & (rl["run"] == f"run_{run:02d}") & (rl["vus"] == vus)
        ]
        assert len(row) == 1
        assert row["rt_mean"].iloc[0] == pytest.approx(exp, rel=1e-12)
        assert row["n_requests"].iloc[0] == vus * 60
        assert row["rps"].iloc[0] == pytest.approx(vus)  # vus·60 запросов / 60 с


def test_aggregates_mean_over_ok_runs(pipeline: dict) -> None:  # type: ignore[type-arg]
    agg = pd.read_csv(pipeline["out"] / "aggregates.csv")
    exp = pipeline["expected"]
    r = agg[(agg["architecture"] == "serverless") & (agg["vus"] == 20)].iloc[0]
    assert r["rt_mean__mean"] == pytest.approx(
        np.mean([exp[("serverless", k, 20)] for k in (1, 2, 3)])
    )
    # monolith/40 VU: прогон 2 — failed (5xx > 50 % в окне) → RT по 2 ok-прогонам
    m = agg[(agg["architecture"] == "monolith") & (agg["vus"] == 40)].iloc[0]
    assert (m["n_runs"], m["n_ok"], m["n_failed"]) == (3, 2, 1)
    assert m["rt_mean__mean"] == pytest.approx(np.mean([exp[("monolith", k, 40)] for k in (1, 3)]))


def test_failed_step_is_reported_not_hidden(pipeline: dict) -> None:  # type: ignore[type-arg]
    t1 = (pipeline["out"] / "tables" / "table1_rt_avg.md").read_text(encoding="utf-8")
    assert "†failed 1/3" in t1
    report = (pipeline["out"] / "report.md").read_text(encoding="utf-8")
    assert "monolith/run_02/40 VU" in report


def test_outputs_and_non_scientific_header(pipeline: dict) -> None:  # type: ignore[type-arg]
    out: Path = pipeline["out"]
    for name in (
        "table1_rt_avg",
        "table2_summary",
        "littles_law",
        "saturation_fit",
        "scaling_efficiency",
        "mann_whitney_holm",
        "table3_mcda",
    ):
        assert (out / "tables" / f"{name}.csv").is_file()
    assert len(list((out / "figures").glob("*.png"))) == 5
    assert pipeline["meta"]["scientific"] is False
    assert (
        (out / "report.md")
        .read_text(encoding="utf-8")
        .startswith("# PIPELINE CHECK — NOT A SCIENTIFIC RESULT")
    )


def test_score_not_computed_without_experts(pipeline: dict) -> None:  # type: ignore[type-arg]
    t3 = pd.read_csv(pipeline["out"] / "tables" / "table3_mcda.csv")
    assert (t3[t3["Критерий"] == "Score(A_k)"].iloc[0, 1:5] == "не вычислен").all()


def test_mann_whitney_uses_run_means(pipeline: dict) -> None:  # type: ignore[type-arg]
    mw = pd.read_csv(pipeline["out"] / "tables" / "mann_whitney_holm.csv")
    assert set(mw["step"]) == {20, 40}
    assert len(mw[mw["step"] == 20]) == 6  # все пары
    r = mw[(mw["step"] == 40) & (mw["a"] == "microservices") & (mw["b"] == "monolith")].iloc[0]
    assert (r["n_a"], r["n_b"]) == (3, 2)  # failed-прогон монолита исключён из теста


def test_validate_passes_and_rejects_for_article(pipeline: dict) -> None:  # type: ignore[type-arg]
    cfg = load_bench_config(pipeline["cfg_path"])
    rep = validate(pipeline["results"], pipeline["out"], cfg, deep=True)
    assert rep.ok, rep.errors
    art = validate(pipeline["results"], pipeline["out"], cfg, for_article=True)
    assert not art.ok
    assert any("server" in e for e in art.errors)
    assert any("TEST DATA ONLY" in e for e in art.errors)


def test_validate_detects_tampered_aggregates(pipeline: dict) -> None:  # type: ignore[type-arg]
    p = pipeline["out"] / "aggregates.csv"
    df = pd.read_csv(p)
    df.loc[0, "rt_mean__mean"] = df.loc[0, "rt_mean__mean"] * 0.9
    df.to_csv(p, index=False)
    rep = validate(pipeline["results"], pipeline["out"], load_bench_config(pipeline["cfg_path"]))
    assert any("aggregates.csv" in e for e in rep.errors)
    assert any("не на актуальных" in e for e in rep.errors)


def test_validate_detects_modified_raw_file(pipeline: dict) -> None:  # type: ignore[type-arg]
    raw = next(pipeline["results"].rglob("points.json.gz"))
    raw.write_bytes(raw.read_bytes() + b"\x00")
    rep = validate(pipeline["results"], pipeline["out"], load_bench_config(pipeline["cfg_path"]))
    assert any("MANIFEST" in e for e in rep.errors)


def test_sla_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import yaml

    cfg_path = fx.test_config(tmp_path)
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    raw["sla"]["sla_ms"] = None
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.delenv("SLA_MS", raising=False)
    fx.build(tmp_path / "results", runs=1, steps=(10,))
    code = run_analysis.main(
        [
            "--results",
            str(tmp_path / "results"),
            "--out",
            str(tmp_path / "out"),
            "--config",
            str(cfg_path),
        ]
    )
    assert code == 2
    assert not (tmp_path / "out" / "aggregates.csv").exists()


def test_exclusion_requires_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLA_MS", raising=False)
    cfg_path = fx.test_config(tmp_path)
    results = tmp_path / "results"
    fx.build(results, runs=2, steps=(10,))
    (results / "exclusions.yaml").write_text(
        "exclusions:\n  - {architecture: monolith, run: run_01, reason: ''}\n", encoding="utf-8"
    )
    from experiments.analysis.collect import ResultsError

    with pytest.raises(ResultsError, match="основания"):
        run_analysis.run(results, tmp_path / "out", cfg_path, tmp_path / "x.yaml")
