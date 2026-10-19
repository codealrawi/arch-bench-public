"""Генератор синтетических файлов k6 JSON для тестов.  TEST DATA ONLY.

Никогда не используется как результат эксперимента: validate_results.py отклоняет результаты
с environment=test-fixture, а фикстуры пишутся только во временные каталоги тестов.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

TEST_DATA_MARKER = "TEST DATA ONLY"


def point(
    t: float, duration_ms: float, status: int, op: str, phase: str, error_code: int | None = None
) -> dict[str, object]:
    tags: dict[str, str] = {"status": str(status), "op": op, "phase": phase, "method": "GET"}
    if error_code is not None:
        tags["error_code"] = str(error_code)
    return {
        "type": "Point",
        "metric": "http_req_duration",
        "data": {
            "time": datetime.fromtimestamp(t, UTC).isoformat(),
            "value": duration_ms,
            "tags": tags,
        },
    }


def start_point(t: float) -> dict[str, object]:
    return {
        "type": "Point",
        "metric": "arch_bench_scenario_start_ms",
        "data": {
            "time": datetime.fromtimestamp(t, UTC).isoformat(),
            "value": t * 1000,
            "tags": {"phase": "meta", "op": "meta"},
        },
    }


def write_points(path: Path, points: Iterable[dict[str, object]], gz: bool = True) -> Path:
    lines = [json.dumps({"type": "Metric", "metric": "http_req_duration", "data": {}})]
    lines += [json.dumps(p) for p in points]
    payload = ("\n".join(lines) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(payload) if gz else payload)
    return path
