"""Фабрики внутренних сервисов. Запуск: ``uvicorn apps.services.app:create_<name> --factory``."""

from __future__ import annotations

from fastapi import FastAPI

from apps.shared.domain.moderation import LocalModeration, RemoteModeration
from apps.shared.http.app_factory import build_app
from apps.shared.http.routers import (
    auth_router,
    content_router,
    internal_router,
    moderation_router,
    stats_router,
    users_router,
)
from apps.shared.moderation import ContentModerator
from apps.shared.settings import AppSettings, SettingsError


def create_auth(settings: AppSettings | None = None) -> FastAPI:
    cfg = settings or AppSettings.from_env()
    return build_app(cfg, [auth_router, users_router], title="auth service", openapi=False)


def create_content(settings: AppSettings | None = None) -> FastAPI:
    cfg = settings or AppSettings.from_env()
    if not cfg.moderation_url:
        raise SettingsError("MODERATION_URL обязателен для content")
    moderation_url = cfg.moderation_url

    async def startup(app: FastAPI) -> None:
        app.state.moderation = RemoteModeration(moderation_url, app.state.http)

    return build_app(
        cfg,
        [content_router, moderation_router],
        title="content service",
        startup=startup,
        openapi=False,
    )


def create_moderation(settings: AppSettings | None = None) -> FastAPI:
    cfg = settings or AppSettings.from_env(needs_db=False, needs_jwt_secret=False)

    async def startup(app: FastAPI) -> None:
        app.state.moderator = ContentModerator().train()
        app.state.moderation = LocalModeration(app.state.moderator)

    return build_app(
        cfg,
        [internal_router],
        title="moderation service",
        uses_db=False,
        startup=startup,
        openapi=False,
    )


def create_analytics(settings: AppSettings | None = None) -> FastAPI:
    cfg = settings or AppSettings.from_env()
    return build_app(cfg, [stats_router], title="analytics service", openapi=False)
