"""Единое ASGI-middleware: request-id, таймаут, security headers, метрики, журнал доступа.

Чистый ASGI (без BaseHTTPMiddleware) — минимальные и одинаковые накладные расходы во всех
архитектурах. Журнал каждого запроса — только при LOG_REQUESTS=true (в эксперименте выключен:
запись лога на каждый запрос влияла бы на время отклика); ошибки 5xx логируются всегда.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from apps.shared.http.metrics import LATENCY, REQUESTS

log = logging.getLogger("arch-bench.access")

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
]
CSP_API = b"default-src 'none'; frame-ancestors 'none'"
# frontend: только собственные скрипты и стили, без inline (защита от XSS)
CSP_FRONTEND = (
    b"default-src 'self'; img-src 'self' data:; connect-src 'self'; "
    b"frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
DOCS_PATHS = ("/api/v1/docs",)  # Swagger UI грузит ресурсы с CDN — CSP не ставится


def csp_for(path: str) -> bytes | None:
    if path.startswith(DOCS_PATHS):
        return None
    if path.startswith("/api/") or path in UNMETERED:
        return CSP_API
    return CSP_FRONTEND


HSTS = (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
UNMETERED = {"/metrics", "/health", "/readiness"}


class AppMiddleware:
    def __init__(
        self, app: ASGIApp, *, service: str, timeout_s: float, log_requests: bool, hsts: bool
    ):
        self.app, self.service, self.timeout_s = app, service, timeout_s
        self.log_requests, self.hsts = log_requests, hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = _incoming_request_id(scope) or uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid
        started = time.perf_counter()
        status_holder: dict[str, int] = {"status": 500}
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_holder["status"] = int(message["status"])
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", rid.encode()))
                headers.extend(SECURITY_HEADERS)
                csp = csp_for(scope.get("path", ""))
                if csp is not None:
                    headers.append((b"content-security-policy", csp))
                if self.hsts:
                    headers.append(HSTS)
                message["headers"] = headers
            await send(message)

        try:
            await asyncio.wait_for(self.app(scope, receive, send_wrapper), self.timeout_s)
        except TimeoutError:
            if response_started:
                raise
            status_holder["status"] = 504
            await _send_json(
                send_wrapper,
                504,
                {
                    "error": {
                        "code": "timeout",
                        "message": "превышено время обработки запроса",
                        "request_id": rid,
                    }
                },
            )
        finally:
            self._observe(scope, status_holder["status"], time.perf_counter() - started, rid)

    def _observe(self, scope: Scope, status: int, seconds: float, rid: str) -> None:
        route_obj = scope.get("route")
        route = getattr(route_obj, "path", None) or "unmatched"
        method = scope.get("method", "")
        if route not in UNMETERED:
            REQUESTS.labels(self.service, method, route, str(status)).inc()
            LATENCY.labels(self.service, method, route).observe(seconds)
        if self.log_requests or status >= 500:
            log.log(
                logging.ERROR if status >= 500 else logging.INFO,
                "request",
                extra={
                    "request_id": rid,
                    "method": method,
                    "path": scope.get("path"),
                    "status": status,
                    "duration_ms": round(seconds * 1000, 2),
                },
            )


def _incoming_request_id(scope: Scope) -> str | None:
    for k, v in scope.get("headers", []):
        if k == b"x-request-id":
            value: str = v.decode("latin-1")
            if 0 < len(value) <= 64 and value.replace("-", "").isalnum():
                return value
    return None


async def _send_json(send: Any, status: int, body: dict[str, Any]) -> None:
    data = json.dumps(body, ensure_ascii=False).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(data)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": data})
