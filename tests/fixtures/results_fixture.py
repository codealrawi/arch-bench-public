"""Синтетическое дерево results/ для тестов конвейера анализа.  TEST DATA ONLY.

environment = "test-fixture": validate_results --for-article такие результаты отклоняет.
Параметры распределений произвольные и не связаны со статьёй; тест проверяет, что конвейер
правильно вычисляет числа из данных, а не какие-то конкретные значения.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from experiments.config import DEFAULT_CONFIG_PATH
from tests.fixtures.k6_fixture import TEST_DATA_MARKER, point, start_point, write_points

ARCHS = ("monolith", "three_tier", "microservices", "serverless")
T_BASE = 1_900_000_000.0


def test_config(tmp: Path) -> Path:
    raw: dict[str, Any] = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw = copy.deepcopy(raw)
    raw["load"].update(
        ramp_s=1, warmup_s=2, measure_s=60, steps_server=[10, 20, 40], steps_local=[10, 20, 40]
    )
    raw["metrics"]["failed_window_s"] = 30
    raw["statistics"].update(bootstrap_b=200, compare_steps=[20, 40])
    raw["mcda"].update(reference_step=20, fault_tolerance_step=40)
    raw["runs"]["count"] = 3
    p = tmp / "bench_test.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return p


def build(
    results: Path,
    *,
    runs: int = 3,
    steps: tuple[int, ...] = (10, 20, 40),
    fail: set[tuple[str, int, int]] | None = None,
    seed: int = 0,
) -> dict[tuple[str, int, int], float]:
    """Пишет дерево; возвращает независимо посчитанный RT_avg фазы measure по (arch,run,vus)."""
    fail = fail or set()
    rng = np.random.default_rng(seed)
    expected: dict[tuple[str, int, int], float] = {}
    manifest: list[str] = []
    for ai, arch in enumerate(ARCHS):
        for run in range(1, runs + 1):
            run_dir = results / arch / f"run_{run:02d}"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "environment": "test-fixture",
                        "marker": TEST_DATA_MARKER,
                        "protocol_version": "1.0",
                        "host": {"git": {"commit": None, "dirty": None}},
                    }
                ),
                encoding="utf-8",
            )
            for si, vus in enumerate(steps):
                start = T_BASE + 1000 * (ai * 100 + run * 10 + si)
                t_measure = start + 1 + 2
                pts = [start_point(start)]
                pts += [point(start + 0.5, 5.0, 200, "feed", "ramp") for _ in range(5)]
                durations = []
                n = vus * 60
                lat = rng.lognormal(mean=2.0 + 0.2 * ai + 0.002 * vus, sigma=0.3, size=n)
                for k in range(n):
                    t = t_measure + 60 * k / n
                    status = 503 if (arch, run, vus) in fail and k > n * 0.4 else 200
                    op = ("feed", "create_post", "stats")[k % 3]
                    pts.append(point(t, float(lat[k]), status, op, "measure"))
                    durations.append(float(lat[k]))
                expected[(arch, run, vus)] = float(np.mean(durations))
                sd = run_dir / f"step_{vus:04d}"
                f = write_points(sd / "points.json.gz", pts)
                (sd / "step_meta.json").write_text(
                    json.dumps(
                        {
                            "vus": vus,
                            "k6_seed": 1000 * run + si,
                            "db_state_before": {"reset_s": 1.0, "counts": {"posts": 900}},
                            "db_state_after": {"counts": {"posts": 950}},
                        }
                    ),
                    encoding="utf-8",
                )
                (sd / "resources.csv").write_text(
                    "group,component,cpu_seconds\nrequest_path,app,"
                    f"{1.0 + ai}\ndatabase,postgres,0.5\ninfrastructure,k3s,0.2\n",
                    encoding="utf-8",
                )
                rel = f.relative_to(results).as_posix()
                manifest.append(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {rel}")
    (results / "MANIFEST.sha256").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    return expected
