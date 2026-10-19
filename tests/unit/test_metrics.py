"""Метрики ступени на синтетических точках (TEST DATA ONLY) с ответами, посчитанными вручную."""

from __future__ import annotations

import gzip
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from experiments.analysis import load_raw
from experiments.analysis.metrics import classify, detect_failed, rt_stats, step_metrics
from tests.fixtures.k6_fixture import point, start_point, write_points

T0 = 1_800_000_000.0
KW = dict(timeout_s=30.0, failed_window_s=60, failed_share=0.5, percentile_method="linear")


def _df(rows: list[tuple[float, float, int, int, str, str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=load_raw.COLUMNS)


def test_rt_stats_hand_values() -> None:
    s = rt_stats(np.array([10.0, 20.0, 30.0, 40.0]), "linear")
    assert s is not None
    assert (s.n, s.mean, s.median, s.min, s.max) == (4, 25.0, 25.0, 10.0, 40.0)
    assert s.sd == pytest.approx(math.sqrt(((15**2) * 2 + (5**2) * 2) / 3))
    # linear: позиция 0.95·3 = 2.85 → 30 + 0.85·10 = 38.5
    assert s.p95 == pytest.approx(38.5)
    assert rt_stats(np.array([]), "linear") is None
    with pytest.raises(ValueError):
        rt_stats(np.array([1.0, -1.0]), "linear")


def test_classification() -> None:
    df = _df(
        [
            (T0, 5, 200, 0, "feed", "measure"),
            (T0, 5, 503, 0, "feed", "measure"),
            (T0, 30000, 0, 1050, "feed", "measure"),
            (T0, 5, 404, 0, "feed", "measure"),
            (T0, 2, 0, 1211, "feed", "measure"),
        ]
    )
    c = classify(df, 30.0)  # M-4 (по умолчанию): сброс соединения — тоже ошибка
    assert c["is_error"].tolist() == [False, True, True, False, True]
    assert c["is_4xx"].tolist() == [False, False, False, True, False]
    assert c["is_transport"].tolist() == [False, False, False, False, True]
    strict = classify(df, 30.0, count_transport_errors=False)  # буквально P-46 v1.0
    assert strict["is_error"].tolist() == [False, True, True, False, False]


def _uniform(n_seconds: int, per_second: int, error_from: int | None = None) -> pd.DataFrame:
    rows = []
    for s in range(n_seconds):
        for k in range(per_second):
            err = error_from is not None and s >= error_from
            rows.append(
                (
                    T0 + s + k / per_second,
                    10.0 + k,
                    500 if err else 200,
                    0,
                    ["feed", "create_post", "stats"][k % 3],
                    "measure",
                )
            )
    return _df(rows)


def test_step_metrics_ok() -> None:
    df = _uniform(300, 10)
    m = step_metrics(df, t_measure_start=T0, measure_s=300, **KW)
    assert m.status == "ok" and m.n_requests == 3000
    assert m.rps == pytest.approx(10.0)
    assert m.error_share == 0.0
    assert m.rt_all is not None and m.rt_all.mean == pytest.approx(14.5)  # среднее 10..19


def test_failed_when_errors_dominate_60s_window() -> None:
    # последние 70 с — все ответы 5xx → есть окно 60 с с долей 1.0
    df = _uniform(300, 10, error_from=230)
    m = step_metrics(df, t_measure_start=T0, measure_s=300, **KW)
    assert m.status == "failed" and "доля ошибок" in (m.failed_reason or "")
    assert m.error_share == pytest.approx(70 / 300)


def test_not_failed_when_errors_spread() -> None:
    # 40 % ошибок равномерно — ни в одном окне > 50 %
    rows = [
        (T0 + i * 0.1, 10.0, 500 if i % 5 < 2 else 200, 0, "feed", "measure") for i in range(3000)
    ]
    m = step_metrics(_df(rows), t_measure_start=T0, measure_s=300, **KW)
    assert m.status == "ok" and m.error_share == pytest.approx(0.4)


def test_failed_when_service_stalls() -> None:
    df = _uniform(300, 10)
    df = df[(df["t"] < T0 + 100) | (df["t"] >= T0 + 170)]  # 70 с без ответов
    reason = detect_failed(classify(df, 30.0), T0, 300, 60, 0.5)
    assert reason is not None and "без завершённых" in reason


def test_warmup_points_excluded() -> None:
    df = pd.concat([_uniform(300, 10), _df([(T0 - 5, 99999.0, 200, 0, "feed", "warmup")] * 50)])
    m = step_metrics(df, t_measure_start=T0, measure_s=300, **KW)
    assert m.n_requests == 3000 and m.rt_all is not None and m.rt_all.max < 100


def test_no_measure_points_is_failed() -> None:
    m = step_metrics(
        _df([(T0, 1.0, 200, 0, "feed", "warmup")]), t_measure_start=T0, measure_s=300, **KW
    )
    assert m.status == "failed" and m.n_requests == 0


def test_load_points_roundtrip(tmp_path: Path) -> None:
    pts = [
        start_point(T0),
        point(T0 + 1, 12.5, 200, "feed", "measure"),
        point(T0 + 2, 30000, 0, "create_post", "measure", error_code=1050),
    ]
    df, start = load_raw.load_points(write_points(tmp_path / "p.json.gz", pts))
    assert start == pytest.approx(T0)
    assert df["duration_ms"].tolist() == [12.5, 30000]
    assert df["error_code"].tolist() == [0, 1050]


def test_load_points_corrupted(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text('{"type":"Point","metric":"http_req_duration"\n', encoding="utf-8")
    with pytest.raises(load_raw.RawDataError, match="повреждённая"):
        load_raw.load_points(p)


def test_load_points_truncated_gzip(tmp_path: Path) -> None:
    good = write_points(tmp_path / "p.json.gz", [point(T0, 1, 200, "feed", "measure")] * 200)
    trunc = tmp_path / "t.json.gz"
    trunc.write_bytes(good.read_bytes()[:-20])
    with pytest.raises(load_raw.RawDataError):
        load_raw.load_points(trunc)


def test_load_points_negative_and_unknown_tag(tmp_path: Path) -> None:
    with pytest.raises(load_raw.RawDataError, match="длительность"):
        load_raw.load_points(
            write_points(tmp_path / "a.json.gz", [point(T0, -1, 200, "feed", "measure")])
        )
    with pytest.raises(load_raw.RawDataError, match="теги"):
        load_raw.load_points(
            write_points(tmp_path / "b.json.gz", [point(T0, 1, 200, "hack", "measure")])
        )


def test_load_points_missing_file() -> None:
    with pytest.raises(load_raw.RawDataError, match="не найден"):
        load_raw.load_points("nope.json.gz")


def test_gzip_helper_is_really_gzip(tmp_path: Path) -> None:
    p = write_points(tmp_path / "p.json.gz", [point(T0, 1, 200, "feed", "measure")])
    assert gzip.decompress(p.read_bytes()).startswith(b"{")
