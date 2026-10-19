"""Фабрики функций OpenFaaS. Запуск: ``uvicorn serverless.functions.handlers:create_<name>``.

Каждая функция — обычное ASGI-приложение с общими роутерами домена и пробами OpenFaaS.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from apps.shared.domain.moderation import LocalModeration
from apps.shared.http.app_factory import build_app
from apps.shared.http.routers import (
    auth_router,
    content_router,
    moderation_router,
    stats_router,
    users_router,
)
from apps.shared.settings import AppSettings


def _probes() -> APIRouter:
    """Пробы faas-netes (httpProbe): /_/health — жив, /_/ready — готов (БД доступна)."""
    r = APIRouter()

    @r.get("/_/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @r.get("/_/ready", include_in_schema=False)
    async def ready(request: Request) -> JSONResponse:
        pool = request.app.state.pool
        try:
            if pool is not None:
                await pool.fetchval("SELECT 1", timeout=2)
        except Exception:  # noqa: BLE001
            return JSONResponse({"status": "not ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    return r


def _function(
    cfg: AppSettings, routers: list[APIRouter], name: str, with_moderation: bool = False
) -> FastAPI:
    async def startup(app: FastAPI) -> None:
        if with_moderation:
            app.state.moderation = LocalModeration()

    app = build_app(cfg, routers, title=f"function {name}", startup=startup, openapi=False)
    app.include_router(_probes())
    return app


def create_auth(settings: AppSettings | None = None) -> FastAPI:
    return _function(settings or AppSettings.from_env(), [auth_router, users_router], "auth")


def create_content(settings: AppSettings | None = None) -> FastAPI:
    return _function(
        settings or AppSettings.from_env(),
        [content_router, moderation_router],
        "content",
        with_moderation=True,
    )


def create_stats(settings: AppSettings | None = None) -> FastAPI:
    return _function(settings or AppSettings.from_env(), [stats_router], "stats")
