"""Настройки процесса из переменных окружения (единые для всех компонентов всех архитектур).

Две группы:
- параметры, влияющие на эксперимент (пулы, лимиты выборок, TTL, таймауты, rate limit),
  ОБЯЗАТЕЛЬНЫ — их генерирует infrastructure/render.py из bench.yaml; отсутствие → ошибка;
- эксплуатационные (CORS, логирование, HSTS) имеют безопасные значения по умолчанию.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

AUTH_MODES = ("jwt", "trusted-headers")


class SettingsError(RuntimeError):
    """Обязательная переменная окружения отсутствует или некорректна."""


def _get(env: Mapping[str, str], name: str) -> str:
    v = env.get(name)
    if v is None or v.strip() == "":
        raise SettingsError(f"переменная окружения {name} обязательна")
    return v.strip()


def _int(env: Mapping[str, str], name: str, minimum: int) -> int:
    raw = _get(env, name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} должна быть целым числом, получено {raw!r}") from exc
    if value < minimum:
        raise SettingsError(f"{name} должна быть ≥ {minimum}, получено {value}")
    return value


def _float(env: Mapping[str, str], name: str) -> float:
    raw = _get(env, name)
    try:
        value = float(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} должна быть числом, получено {raw!r}") from exc
    if not value > 0:
        raise SettingsError(f"{name} должна быть > 0")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    if raw.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if raw.strip().lower() in ("0", "false", "no", "off"):
        return False
    raise SettingsError(f"{name}: ожидается true/false, получено {raw!r}")


@dataclass(frozen=True)
class AppSettings:
    service_name: str
    auth_mode: str
    jwt_secret: str
    access_token_ttl_s: int
    refresh_token_ttl_s: int
    pbkdf2_iterations: int
    request_timeout_s: float
    rate_limit_auth_per_minute: int
    feed_limit: int
    comments_page_limit: int
    search_limit: int
    stats_window_days: int
    database_url: str | None = None
    db_pool_size: int = 0
    db_command_timeout_s: float = 0.0
    moderation_url: str | None = None
    upstream_timeout_s: float = 0.0
    cors_origins: tuple[str, ...] = field(default_factory=tuple)
    trust_proxy: bool = False
    log_level: str = "INFO"
    log_requests: bool = False
    hsts: bool = False

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        needs_db: bool = True,
        needs_jwt_secret: bool = True,
    ) -> AppSettings:
        e = os.environ if env is None else env
        auth_mode = e.get("AUTH_MODE", "jwt").strip() or "jwt"
        if auth_mode not in AUTH_MODES:
            raise SettingsError(f"AUTH_MODE должен быть одним из {AUTH_MODES}")
        secret = _get(e, "JWT_SECRET") if needs_jwt_secret else e.get("JWT_SECRET", "")
        if needs_jwt_secret and len(secret.encode()) < 32:
            raise SettingsError("JWT_SECRET должен быть не короче 32 байт")
        db_url = _get(e, "DATABASE_URL") if needs_db else e.get("DATABASE_URL") or None
        origins = tuple(o.strip() for o in e.get("CORS_ORIGINS", "").split(",") if o.strip())
        if "*" in origins:
            raise SettingsError("CORS_ORIGINS='*' запрещён: перечислите доверенные источники")
        return cls(
            service_name=_get(e, "SERVICE_NAME"),
            auth_mode=auth_mode,
            jwt_secret=secret,
            access_token_ttl_s=_int(e, "ACCESS_TOKEN_TTL_S", 1),
            refresh_token_ttl_s=_int(e, "REFRESH_TOKEN_TTL_S", 1),
            pbkdf2_iterations=_int(e, "PBKDF2_ITERATIONS", 1),
            request_timeout_s=_float(e, "REQUEST_TIMEOUT_S"),
            rate_limit_auth_per_minute=_int(e, "RATE_LIMIT_AUTH_PER_MINUTE", 1),
            feed_limit=_int(e, "FEED_LIMIT", 1),
            comments_page_limit=_int(e, "COMMENTS_PAGE_LIMIT", 1),
            search_limit=_int(e, "SEARCH_LIMIT", 1),
            stats_window_days=_int(e, "STATS_WINDOW_DAYS", 1),
            database_url=db_url,
            db_pool_size=_int(e, "DB_POOL_SIZE", 1) if needs_db else 0,
            db_command_timeout_s=_float(e, "DB_COMMAND_TIMEOUT_S") if needs_db else 0.0,
            moderation_url=(e.get("MODERATION_URL") or "").strip().rstrip("/") or None,
            upstream_timeout_s=(
                _float(e, "UPSTREAM_TIMEOUT_S") if e.get("UPSTREAM_TIMEOUT_S") else 0.0
            ),
            cors_origins=origins,
            trust_proxy=_bool(e, "TRUST_PROXY", False),
            log_level=(e.get("LOG_LEVEL") or "INFO").upper(),
            log_requests=_bool(e, "LOG_REQUESTS", False),
            hsts=_bool(e, "HSTS", False),
        )
