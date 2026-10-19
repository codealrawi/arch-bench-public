"""Мини-эксперимент «холодный старт»: статистики и dry-run (команды не выполняются)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from experiments import coldstart


def test_summarize_hand_values() -> None:
    rows = [
        {"function": "content", "kind": "cold", "ok": True, "latency_ms": v}
        for v in (1000.0, 2000.0, 3000.0)
    ]
    rows += [
        {"function": "content", "kind": "warm", "ok": True, "latency_ms": 10.0},
        {"function": "content", "kind": "cold", "ok": False, "latency_ms": 30000.0},
    ]
    s = coldstart.summarize(rows)
    assert s["content.cold"]["n"] == 4 and s["content.cold"]["n_ok"] == 3
    assert s["content.cold"]["median_ms"] == 2000.0  # неуспешный запрос не входит в задержки
    assert s["content.warm"]["min_ms"] == s["content.warm"]["max_ms"] == 10.0


def test_all_failed_reported_not_hidden() -> None:
    s = coldstart.summarize(
        [{"function": "auth", "kind": "cold", "ok": False, "latency_ms": 30000.0}]
    )
    assert s["auth.cold"] == {"n": 1, "n_ok": 0}


def test_dry_run_writes_raw_and_summary(tmp_path: Path) -> None:
    code = coldstart.main(
        [
            "--environment",
            "local-validation",
            "--target-host",
            "127.0.0.1",
            "--out",
            str(tmp_path),
            "--dry-run",
        ]
    )
    assert code == 0
    run_dir = next(tmp_path.iterdir())
    with (run_dir / "coldstart.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    from experiments.config import load_bench_config

    cfg = load_bench_config()
    assert len(rows) == 3 * cfg.coldstart.repeats * 2  # 3 функции × повторы × (cold, warm)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["dry_run"] is True and summary["repeats"] == cfg.coldstart.repeats


@pytest.mark.parametrize("missing", [["--target-host", "x"], ["--environment", "server"]])
def test_required_args(missing: list[str]) -> None:
    with pytest.raises(SystemExit):
        coldstart.main(missing)
