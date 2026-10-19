"""Фабрика FastAPI-приложения: одинаковая обвязка для каждого компонента каждой архитектуры.

Состав: JSON-логи, AppMiddleware (request-id, таймаут, security headers, метрики), CORS (только
перечисленные источники), централизованные ошибки, /health (liveness), /readiness (БД и
состояние завершения), /metrics, OpenAPI под /api/v1. Корректное завершение: при SIGTERM
uvicorn перестаёт принимать соединения, /readiness отвечает 503, пул БД закрывается после
обработки текущих запросов.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from apps.shared import __version__
from apps.shared.db import create_pool
from apps.shared.http import API_PREFIX
from apps.shared.http.errors import error_response, install_error_handlers
from apps.shared.http.logging_setup import configure_logging
from apps.shared.http.metrics import render_latest
from apps.shared.http.middleware import AppMiddleware
from apps.shared.http.ratelimit import SlidingWindowLimiter
from apps.shared.settings import AppSettings

log = logging.getLogger("arch-bench.app")
Hook = Callable[[FastAPI], Awaitable[None]]


def build_app(
    settings: AppSettings,
    routers: list[APIRouter],
    *,
    title: str,
    uses_db: bool = True,
    startup: Hook | None = None,
    shutdown: Hook | None = None,
    openapi: bool = True,
) -> FastAPI:
    configure_logging(settings.service_name, settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.shutting_down = False
        app.state.pool = None
        if uses_db:
            assert settings.database_url
            app.state.pool = await create_pool(
                settings.database_url, settings.db_pool_size, settings.db_command_timeout_s
            )
        timeout = settings.upstream_timeout_s or settings.request_timeout_s
        app.state.http = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=256),
        )
        if startup:
            await startup(app)
        log.info("started", extra={"service": settings.service_name})
        try:
            yield
        finally:
            app.state.shutting_down = True
            if shutdown:
                await shutdown(app)
            await app.state.http.aclose()
            if app.state.pool is not None:
                await app.state.pool.close()
            log.info("stopped", extra={"service": settings.service_name})

    app = FastAPI(
        title=title,
        version=__version__,
        lifespan=lifespan,
        openapi_url=f"{API_PREFIX}/openapi.json" if openapi else None,
        docs_url=f"{API_PREFIX}/docs" if openapi else None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.auth_limiter = SlidingWindowLimiter(settings.rate_limit_auth_per_minute)
    install_error_handlers(app)
    for r in routers:
        app.include_router(r, prefix=API_PREFIX)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": settings.service_name, "version": __version__}

    @app.get("/readiness", include_in_schema=False)
    async def readiness(request: Request) -> Any:
        if request.app.state.shutting_down:
            return error_response(request, 503, "shutting_down", "сервис завершает работу")
        pool = request.app.state.pool
        if pool is not None:
            try:
                await pool.fetchval("SELECT 1", timeout=2)
            except Exception:  # noqa: BLE001 — любая ошибка БД означает «не готов»
                return error_response(request, 503, "db_unavailable", "БД недоступна")
        return {"status": "ready", "service": settings.service_name}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        body, ctype = render_latest()
        return Response(content=body, media_type=ctype)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-Id"],
            allow_credentials=False,
            max_age=600,
        )
    app.add_middleware(
        AppMiddleware,
        service=settings.service_name,
        timeout_s=settings.request_timeout_s,
        log_requests=settings.log_requests,
        hsts=settings.hsts,
    )
    return app
