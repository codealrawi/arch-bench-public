"""Централизованная обработка ошибок: единый формат {"error": {code, message, request_id}}.

Внутренние детали (SQL, трассировки) клиенту не возвращаются — только в лог.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import asyncpg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from apps.shared.auth import AuthError
from apps.shared.domain.errors import DomainError

log = logging.getLogger("arch-bench.http")


def error_response(
    request: Request, status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=status,
        headers=headers,
        content={"error": {"code": code, "message": message, "request_id": rid}},
    )


def _validation_message(errors: list[dict[str, Any]]) -> str:
    parts = []
    for e in errors[:5]:
        loc = ".".join(str(x) for x in e.get("loc", []) if x not in ("body", "query", "path"))
        parts.append(f"{loc or 'request'}: {e.get('msg', 'invalid')}")
    return "; ".join(parts)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError) -> JSONResponse:
        return error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(AuthError)
    async def _auth(request: Request, exc: AuthError) -> JSONResponse:
        return error_response(
            request,
            401,
            "unauthorized",
            "требуется действительный токен",
            {"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            request, 422, "validation_error", _validation_message(list(exc.errors()))
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            429: "rate_limited",
        }.get(exc.status_code, "http_error")
        return error_response(
            request,
            exc.status_code,
            code,
            str(exc.detail),
            dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(asyncpg.QueryCanceledError)
    async def _db_timeout(request: Request, exc: asyncpg.QueryCanceledError) -> JSONResponse:
        log.warning("db query cancelled (timeout) rid=%s", getattr(request.state, "request_id", ""))
        return error_response(request, 504, "db_timeout", "превышено время запроса к БД")

    @app.exception_handler(asyncio.TimeoutError)
    async def _timeout(request: Request, exc: asyncio.TimeoutError) -> JSONResponse:
        return error_response(request, 504, "timeout", "превышено время обработки запроса")

    async def _db_down(request: Request, exc: Exception) -> JSONResponse:
        log.error(
            "db unavailable: %s rid=%s",
            type(exc).__name__,
            getattr(request.state, "request_id", ""),
        )
        return error_response(request, 503, "db_unavailable", "база данных недоступна")

    for exc_cls in (
        asyncpg.TooManyConnectionsError,
        asyncpg.CannotConnectNowError,
        ConnectionError,
        OSError,
    ):
        app.add_exception_handler(exc_cls, _db_down)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error rid=%s", getattr(request.state, "request_id", ""))
        return error_response(request, 500, "internal_error", "внутренняя ошибка сервера")
