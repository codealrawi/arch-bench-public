"""API-gateway (Configuration C): проверка JWT и маршрутизация во внутренние сервисы.

- JWT проверяется здесь, локально (P-36): без обращения к auth на каждый запрос;
- клиентские X-User-Id / X-User-Role удаляются, после проверки токена gateway выставляет свои;
- X-Forwarded-For = адрес клиента (сервисы с TRUST_PROXY=true используют его для rate limit);
- OpenAPI — тот же контракт, что у монолита (строится из общих роутеров без их запуска).

Окружение: общее (apps/shared/settings.py) + AUTH_URL, CONTENT_URL, ANALYTICS_URL.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from apps.shared import __version__
from apps.shared.auth import AuthError, Principal, bearer_token, verify_token
from apps.shared.http import API_PREFIX
from apps.shared.http.app_factory import build_app
from apps.shared.http.errors import error_response
from apps.shared.http.routers import PUBLIC_ROUTERS
from apps.shared.http.security import USER_ID_HEADER, USER_ROLE_HEADER
from apps.shared.settings import AppSettings, SettingsError

# (префикс пути без API_PREFIX, upstream-переменная)
ROUTES: list[tuple[str, str]] = [
    ("/auth/", "AUTH_URL"),
    ("/users/", "AUTH_URL"),
    ("/users", "AUTH_URL"),
    ("/categories", "CONTENT_URL"),
    ("/posts", "CONTENT_URL"),
    ("/search", "CONTENT_URL"),
    ("/moderation/", "CONTENT_URL"),
    ("/stats", "ANALYTICS_URL"),
]
PUBLIC = {
    ("POST", "/auth/register"),
    ("POST", "/auth/login"),
    ("POST", "/auth/refresh"),
    ("POST", "/auth/logout"),
    ("GET", "/categories"),
}
FORWARD_REQUEST_HEADERS = ("content-type", "accept", "x-request-id")
FORWARD_RESPONSE_HEADERS = ("content-type", "retry-after", "www-authenticate")


def resolve_upstream(path: str, upstreams: dict[str, str]) -> str | None:
    """Путь вида /api/v1/... → базовый URL сервиса (или None, если маршрута нет)."""
    if not path.startswith(API_PREFIX + "/"):
        return None
    rest = path[len(API_PREFIX) :]
    for prefix, key in ROUTES:
        if rest == prefix.rstrip("/") or rest.startswith(prefix):
            return upstreams[key]
    return None


def contract_openapi() -> dict[str, Any]:
    """Спецификация общего контракта /api/v1 (идентична монолиту)."""
    spec_app = FastAPI()
    for r in PUBLIC_ROUTERS:
        spec_app.include_router(r, prefix=API_PREFIX)
    return get_openapi(
        title="МедСоцПлатформа API (gateway)", version=__version__, routes=spec_app.routes
    )


def create_app(settings: AppSettings | None = None) -> FastAPI:
    cfg = settings or AppSettings.from_env(needs_db=False)
    upstreams = {}
    for key in ("AUTH_URL", "CONTENT_URL", "ANALYTICS_URL"):
        value = (os.environ.get(key) or "").rstrip("/")
        if not value:
            raise SettingsError(f"переменная окружения {key} обязательна")
        upstreams[key] = value
    spec = contract_openapi()
    proxy = APIRouter()

    @proxy.get(f"{API_PREFIX}/openapi.json", include_in_schema=False)
    async def openapi() -> JSONResponse:
        return JSONResponse(spec)

    @proxy.api_route(
        f"{API_PREFIX}/{{path:path}}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def forward(request: Request, path: str) -> Response:
        upstream = resolve_upstream(request.url.path, upstreams)
        if upstream is None:
            return error_response(request, 404, "not_found", "маршрут не найден")
        rest = request.url.path[len(API_PREFIX) :]
        headers = {k: v for k, v in request.headers.items() if k in FORWARD_REQUEST_HEADERS}
        headers["x-request-id"] = request.state.request_id
        headers["x-forwarded-for"] = request.client.host if request.client else ""
        if (request.method, rest) not in PUBLIC:
            try:
                user: Principal = verify_token(
                    bearer_token(request.headers.get("authorization")), cfg.jwt_secret
                )
            except AuthError:
                return error_response(
                    request,
                    401,
                    "unauthorized",
                    "требуется действительный токен",
                    {"WWW-Authenticate": "Bearer"},
                )
            headers[USER_ID_HEADER] = str(user.user_id)
            headers[USER_ROLE_HEADER] = user.role
        client: httpx.AsyncClient = request.app.state.http
        try:
            r = await client.request(
                request.method,
                upstream + request.url.path,
                params=request.query_params,
                content=await request.body(),
                headers=headers,
            )
        except httpx.TimeoutException:
            return error_response(request, 504, "upstream_timeout", "сервис не ответил вовремя")
        except httpx.HTTPError:
            return error_response(request, 503, "upstream_unavailable", "сервис недоступен")
        return Response(
            content=r.content,
            status_code=r.status_code,
            headers={k: v for k, v in r.headers.items() if k.lower() in FORWARD_RESPONSE_HEADERS},
        )

    # gateway не обращается к БД; OpenAPI отдаёт маршрут proxy (общий контракт)
    app = build_app(cfg, [], title="МедСоцПлатформа API (gateway)", uses_db=False, openapi=False)
    app.include_router(proxy)
    return app
