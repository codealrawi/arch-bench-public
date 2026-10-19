"""Чтение сырых точек k6 (``k6 run --out json=points.json.gz``) в таблицу запросов.

Берутся только точки метрики ``http_req_duration`` — одна точка на каждый HTTP-запрос
(включая ошибки и таймауты). Поля: время окончания (UTC, с), длительность (мс), HTTP-статус
(0 — нет ответа), код ошибки k6, операция (tag ``op``), фаза (tag ``phase``).
"""

from __future__ import annotations

import gzip
import json
import math
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import IO, Any

import numpy as np
import pandas as pd

from experiments.config import LOAD_OPERATIONS

OPS = (*LOAD_OPERATIONS, "login", "post_pool")
PHASES = ("setup", "ramp", "warmup", "measure")
COLUMNS = ["t", "duration_ms", "status", "error_code", "op", "phase"]


class RawDataError(ValueError):
    """Сырые данные повреждены или не соответствуют ожидаемому формату."""


def _open(path: Path) -> IO[str]:
    """gzip определяется по сигнатуре 1f 8b, а не по расширению файла."""
    with path.open("rb") as f:
        magic = f.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


SCENARIO_START_METRIC = "arch_bench_scenario_start_ms"


def _iter_points(
    path: Path, starts: list[float]
) -> Iterator[tuple[float, float, int, int, str, str]]:
    with _open(path) as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                obj: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RawDataError(f"{path}:{lineno}: повреждённая строка JSON") from exc
            if obj.get("type") != "Point":
                continue
            if obj.get("metric") == SCENARIO_START_METRIC:
                starts.append(float(obj["data"]["value"]) / 1000.0)
                continue
            if obj.get("metric") != "http_req_duration":
                continue
            data = obj.get("data") or {}
            tags = data.get("tags") or {}
            try:
                t = datetime.fromisoformat(str(data["time"])).timestamp()
                value = float(data["value"])
            except (KeyError, ValueError, TypeError) as exc:
                raise RawDataError(f"{path}:{lineno}: нет time/value") from exc
            if not math.isfinite(value) or value < 0:
                raise RawDataError(f"{path}:{lineno}: недопустимая длительность {value}")
            op, phase = tags.get("op"), tags.get("phase")
            if op not in OPS or phase not in PHASES:
                raise RawDataError(f"{path}:{lineno}: неизвестные теги op={op} phase={phase}")
            status = int(tags.get("status") or 0)
            error_code = int(tags.get("error_code") or 0)
            yield t, value, status, error_code, str(op), str(phase)


def load_aux(path: Path | str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Вспомогательные ряды k6 для операционного анализа: (vus, iterations).

    vus — gauge фактического числа активных VU: столбцы t (с epoch), n.
    iterations — по точке на итерацию: t (окончание), duration_ms (запрос + think time +
    накладные расходы JS). Если метрик нет (старые данные), таблицы пустые.
    Время точки k6 — момент ОКОНЧАНИЯ (проверено на сырых данных 2026-10-06: запросы фазы
    warmup заканчиваются после границы окна, все запросы measure начинаются после неё).
    """
    p = Path(path)
    vus: list[tuple[float, float]] = []
    its: list[tuple[float, float]] = []
    try:
        with _open(p) as f:
            for line in f:
                if '"vus"' not in line and '"iteration_duration"' not in line:
                    continue
                obj = json.loads(line)
                if obj.get("type") != "Point":
                    continue
                d = obj.get("data") or {}
                t = datetime.fromisoformat(str(d["time"])).timestamp()
                if obj.get("metric") == "vus":
                    vus.append((t, float(d["value"])))
                elif obj.get("metric") == "iteration_duration":
                    its.append((t, float(d["value"])))
    except (EOFError, OSError) as exc:
        raise RawDataError(f"{p}: файл повреждён ({exc})") from exc
    return (
        pd.DataFrame(vus, columns=["t", "n"], dtype=float),
        pd.DataFrame(its, columns=["t", "duration_ms"], dtype=float),
    )


def load_points(path: Path | str) -> tuple[pd.DataFrame, float | None]:
    """Возвращает (таблица запросов, момент старта сценария в с epoch или None)."""
    p = Path(path)
    if not p.is_file():
        raise RawDataError(f"файл сырых данных не найден: {p}")
    starts: list[float] = []
    try:
        rows = list(_iter_points(p, starts))
    except (EOFError, OSError) as exc:  # обрезанный gzip
        raise RawDataError(f"{p}: файл повреждён ({exc})") from exc
    if len(set(starts)) > 1:
        raise RawDataError(f"{p}: несколько разных моментов старта сценария")
    start = starts[0] if starts else None
    if not rows:
        empty = pd.DataFrame(
            {
                c: pd.Series(dtype=d)
                for c, d in zip(
                    COLUMNS,
                    ["float64", "float64", "int64", "int64", "object", "object"],
                    strict=True,
                )
            }
        )
        return empty, start
    df = pd.DataFrame(rows, columns=COLUMNS)
    df["status"] = df["status"].astype(np.int64)
    df["error_code"] = df["error_code"].astype(np.int64)
    return df, start
