from __future__ import annotations

import pytest

from apps.shared.settings import AppSettings, SettingsError

GOOD = {
    "SERVICE_NAME": "monolith",
    "DATABASE_URL": "postgresql://u@h/db",
    "DB_POOL_SIZE": "20",
    "DB_COMMAND_TIMEOUT_S": "30",
    "JWT_SECRET": "s" * 32,
    "ACCESS_TOKEN_TTL_S": "3600",
    "REFRESH_TOKEN_TTL_S": "86400",
    "PBKDF2_ITERATIONS": "1000",
    "REQUEST_TIMEOUT_S": "30",
    "RATE_LIMIT_AUTH_PER_MINUTE": "60",
    "FEED_LIMIT": "20",
    "COMMENTS_PAGE_LIMIT": "50",
    "SEARCH_LIMIT": "20",
    "STATS_WINDOW_DAYS": "7",
}


def test_ok_and_defaults() -> None:
    s = AppSettings.from_env(GOOD)
    assert (s.db_pool_size, s.feed_limit, s.stats_window_days) == (20, 20, 7)
    assert s.auth_mode == "jwt" and s.cors_origins == () and s.log_requests is False


@pytest.mark.parametrize("missing", sorted(GOOD))
def test_every_experiment_variable_is_required(missing: str) -> None:
    env = {k: v for k, v in GOOD.items() if k != missing}
    with pytest.raises(SettingsError, match=missing):
        AppSettings.from_env(env)


def test_db_optional_for_db_less_components() -> None:
    env = {k: v for k, v in GOOD.items() if not k.startswith("DB") and k != "DATABASE_URL"}
    s = AppSettings.from_env(env, needs_db=False)
    assert s.database_url is None and s.db_pool_size == 0


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DB_POOL_SIZE", "0"),
        ("FEED_LIMIT", "0"),
        ("ACCESS_TOKEN_TTL_S", "abc"),
        ("JWT_SECRET", "short"),
        ("AUTH_MODE", "none"),
        ("CORS_ORIGINS", "*"),
        ("REQUEST_TIMEOUT_S", "-1"),
        ("TRUST_PROXY", "maybe"),
    ],
)
def test_invalid_values(key: str, value: str) -> None:
    with pytest.raises(SettingsError):
        AppSettings.from_env({**GOOD, key: value})


def test_cors_list_parsing() -> None:
    s = AppSettings.from_env({**GOOD, "CORS_ORIGINS": "https://a.example, http://localhost:8080"})
    assert s.cors_origins == ("https://a.example", "http://localhost:8080")
