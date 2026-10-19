"""Развёртывания архитектур процессами uvicorn (без Docker/k8s) для контрактных тестов.

Каждое развёртывание отдаёт Endpoint: функцию url(path) → полный URL. Для serverless
маршрутизация по доменам повторяет то, что делает шлюз OpenFaaS (/function/<name>/...):
шлюза в тесте нет, запрос идёт прямо в процесс функции. TEST DATA ONLY.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

from apps.gateway.app import ROUTES, resolve_upstream
from apps.shared.http import API_PREFIX
from tests.fixtures.app_env import env
from tests.fixtures.processes import uvicorn_process


@dataclass(frozen=True)
class Endpoint:
    name: str
    url: Callable[[str], str]


@contextmanager
def monolith(dsn: str) -> Iterator[Endpoint]:
    with uvicorn_process("apps.monolith.app:create_app", env(dsn, "monolith")) as base:
        yield Endpoint("monolith", lambda p: base + p)


@contextmanager
def microservices(dsn: str) -> Iterator[Endpoint]:
    internal = {"AUTH_MODE": "trusted-headers", "TRUST_PROXY": "true"}
    with ExitStack() as st:
        mod = st.enter_context(
            uvicorn_process("apps.services.app:create_moderation", env(None, "moderation"))
        )
        auth = st.enter_context(
            uvicorn_process("apps.services.app:create_auth", env(dsn, "auth", **internal))
        )
        content = st.enter_context(
            uvicorn_process(
                "apps.services.app:create_content",
                env(dsn, "content", MODERATION_URL=mod, **internal),
            )
        )
        analytics = st.enter_context(
            uvicorn_process("apps.services.app:create_analytics", env(dsn, "analytics", **internal))
        )
        gw = st.enter_context(
            uvicorn_process(
                "apps.gateway.app:create_app",
                env(None, "gateway", AUTH_URL=auth, CONTENT_URL=content, ANALYTICS_URL=analytics),
                health="/readiness",
            )
        )
        yield Endpoint("microservices", lambda p: gw + p)


@contextmanager
def serverless(dsn: str) -> Iterator[Endpoint]:
    with ExitStack() as st:
        fns = {
            name: st.enter_context(
                uvicorn_process(
                    f"serverless.functions.handlers:create_{name}",
                    env(dsn, f"fn-{name}"),
                    health="/_/ready",
                )
            )
            for name in ("auth", "content", "stats")
        }
        # тот же маршрутизатор доменов, что у gateway, но upstream — функция
        upstreams = {
            "AUTH_URL": fns["auth"],
            "CONTENT_URL": fns["content"],
            "ANALYTICS_URL": fns["stats"],
        }

        def url(path: str) -> str:
            base = resolve_upstream(path.split("?")[0], upstreams)
            assert base is not None, path
            return base + path

        yield Endpoint("serverless", url)


FACTORIES = {"monolith": monolith, "microservices": microservices, "serverless": serverless}
__all__ = ["API_PREFIX", "ROUTES", "FACTORIES", "Endpoint"]
