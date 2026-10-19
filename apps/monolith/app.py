"""Configuration A (Monolith) и backend Configuration B (3-tier): все домены в одном процессе.

Модерация выполняется в процессе. В конфигурации A монолит также раздаёт frontend
(apps/frontend/static) — в измеряемый путь API статика не входит ни в одной архитектуре.

Запуск: ``uvicorn apps.monolith.app:create_app --factory --workers N``.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from apps.shared.domain.moderation import LocalModeration
from apps.shared.http.app_factory import build_app
from apps.shared.http.routers import PUBLIC_ROUTERS
from apps.shared.settings import AppSettings

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend" / "static"


async def _startup(app: FastAPI) -> None:
    app.state.moderation = LocalModeration()


def create_app(settings: AppSettings | None = None) -> FastAPI:
    cfg = settings or AppSettings.from_env()
    app = build_app(cfg, PUBLIC_ROUTERS, title="МедСоцПлатформа API (monolith)", startup=_startup)
    serve_frontend = os.environ.get("SERVE_FRONTEND", "true").lower() == "true"
    if serve_frontend and FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app
