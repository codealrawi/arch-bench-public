"""Метрики приложения в формате Prometheus (одинаковая обвязка во всех процессах).

При нескольких воркерах uvicorn задаётся PROMETHEUS_MULTIPROC_DIR (общий каталог процессов);
/metrics тогда собирает значения всех воркеров (prometheus_client multiprocess mode).
"""

from __future__ import annotations

import os

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)

# границы гистограммы: от 1 мс до 30 с (таймаут эксперимента)
BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

REQUESTS = Counter(
    "app_http_requests_total", "HTTP-запросы", ["service", "method", "route", "status"]
)
LATENCY = Histogram(
    "app_http_request_duration_seconds",
    "Время обработки HTTP-запроса",
    ["service", "method", "route"],
    buckets=BUCKETS,
)
# Задержка запросов к БД собирается на стороне PostgreSQL (pg_stat_statements →
# postgres_exporter): одинаково для всех архитектур и без обвязки в коде приложения.


def render_latest() -> tuple[bytes, str]:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST
