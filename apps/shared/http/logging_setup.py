"""Структурное логирование: одна JSON-строка на событие в stdout."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_EXTRA_KEYS = ("request_id", "method", "path", "status", "duration_ms", "service", "client")


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service,
            "message": record.getMessage(),
        }
        for k in _EXTRA_KEYS:
            if hasattr(record, k):
                out[k] = getattr(record, k)
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def configure_logging(service: str, level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("uvicorn.access",):  # доступ логируется своим middleware (LOG_REQUESTS)
        logging.getLogger(noisy).handlers[:] = []
        logging.getLogger(noisy).propagate = False
