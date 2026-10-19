"""Окружение процессов приложения, вычисляемое из bench.yaml (одно правило для всех архитектур).

Секреты (JWT_SECRET, POSTGRES_PASSWORD → DATABASE_URL) сюда не входят: compose читает их
из .env, Kubernetes — из Secret, OpenFaaS — из окружения faas-cli при развёртывании.
"""

from __future__ import annotations

from experiments.config import BenchConfig, Component

MULTIPROC_DIR = "/tmp/prometheus-multiproc"  # noqa: S108 — общий каталог воркеров в контейнере


def common_env(cfg: BenchConfig) -> dict[str, str]:
    a, d = cfg.app, cfg.data
    return {
        "ACCESS_TOKEN_TTL_S": str(a.access_token_ttl_s),
        "REFRESH_TOKEN_TTL_S": str(a.refresh_token_ttl_s),
        "PBKDF2_ITERATIONS": str(cfg.auth.pbkdf2_iterations),
        "REQUEST_TIMEOUT_S": str(a.request_timeout_s),
        "RATE_LIMIT_AUTH_PER_MINUTE": str(a.rate_limit_auth_per_minute),
        "FEED_LIMIT": str(d.feed_limit),
        "COMMENTS_PAGE_LIMIT": str(d.comments_page_limit),
        "SEARCH_LIMIT": str(d.search_limit),
        "STATS_WINDOW_DAYS": str(d.stats_window_days),
        "UPSTREAM_TIMEOUT_S": str(cfg.network.upstream_timeout_s),
        "LOG_LEVEL": a.log_level,
        "LOG_REQUESTS": "true" if a.log_requests else "false",
        "PYTHONUNBUFFERED": "1",
    }


def component_env(
    cfg: BenchConfig,
    comp: Component,
    *,
    service: str,
    factory: str,
    port: int,
    uses_db: bool,
    auth_mode: str = "jwt",
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env = common_env(cfg) | {
        "SERVICE_NAME": service,
        "AUTH_MODE": auth_mode,
        "APP_FACTORY": factory,
        "APP_PORT": str(port),
        "APP_WORKERS": str(comp.workers),
        "GRACEFUL_SHUTDOWN_S": str(cfg.app.graceful_shutdown_s),
    }
    if comp.workers > 1:
        env["PROMETHEUS_MULTIPROC_DIR"] = MULTIPROC_DIR
    if uses_db:
        env |= {
            "DB_POOL_SIZE": str(comp.db_pool),
            "DB_COMMAND_TIMEOUT_S": str(cfg.app.db_command_timeout_s),
        }
    return env | (extra or {})
