"""Окружение приложений для тестов.  TEST DATA ONLY (секреты — тестовые)."""

from __future__ import annotations

from typing import Any

from apps.shared.settings import AppSettings
from tests.conftest import TEST_JWT_SECRET


def env(dsn: str | None, service: str = "test", **overrides: str) -> dict[str, str]:
    base = {
        "SERVICE_NAME": service,
        "JWT_SECRET": TEST_JWT_SECRET,
        "ACCESS_TOKEN_TTL_S": "600",
        "REFRESH_TOKEN_TTL_S": "3600",
        "PBKDF2_ITERATIONS": "5",
        "REQUEST_TIMEOUT_S": "30",
        "RATE_LIMIT_AUTH_PER_MINUTE": "1000",
        "FEED_LIMIT": "20",
        "COMMENTS_PAGE_LIMIT": "50",
        "SEARCH_LIMIT": "20",
        "STATS_WINDOW_DAYS": "7",
        "UPSTREAM_TIMEOUT_S": "30",
        "LOG_LEVEL": "WARNING",
    }
    if dsn:
        base |= {"DATABASE_URL": dsn, "DB_POOL_SIZE": "4", "DB_COMMAND_TIMEOUT_S": "30"}
    base |= overrides
    return base


def settings(dsn: str | None, **overrides: str) -> AppSettings:
    kw: dict[str, Any] = {"needs_db": dsn is not None}
    return AppSettings.from_env(env(dsn, **overrides), **kw)
