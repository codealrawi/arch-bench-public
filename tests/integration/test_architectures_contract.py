"""Одна функциональность — разные архитектуры (главное правило задания).

1. Эквивалентность: одинаковые запросы на чтение к monolith, microservices и serverless,
   подключённым к ОДНОЙ БД, дают одинаковые ответы.
2. Сценарий пользователя и проверки доступа выполняются одинаково в каждой архитектуре.
TEST DATA ONLY.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from typing import Any

import httpx
import pytest

from tests.fixtures.api import login as _login_tc
from tests.fixtures.deployments import FACTORIES, Endpoint

pytestmark = pytest.mark.pg
API = "/api/v1"
PATIENT, MODERATOR = 10, 2


@pytest.fixture
def deployments(seeded_dsn: str) -> Iterator[dict[str, Endpoint]]:
    with ExitStack() as st:
        yield {n: st.enter_context(f(seeded_dsn)) for n, f in FACTORIES.items()}


def _login(c: httpx.Client, ep: Endpoint, uid: int) -> dict[str, str]:
    class _Adapter:
        def post(self, path: str, json: Any) -> httpx.Response:
            return c.post(ep.url(path), json=json)

    return _login_tc(_Adapter(), uid)  # type: ignore[arg-type]


def _get(c: httpx.Client, ep: Endpoint, path: str, h: dict[str, str]) -> Any:
    r = c.get(ep.url(path), headers=h)
    assert r.status_code == 200, (ep.name, path, r.text)
    return r.json()


def test_read_responses_identical_across_architectures(deployments: dict[str, Endpoint]) -> None:
    paths = [
        f"{API}/categories",
        f"{API}/posts",
        f"{API}/posts?category_id=3",
        f"{API}/posts/5",
        f"{API}/posts/5/comments",
        f"{API}/users/12",
        f"{API}/search?q=кардиология",
        f"{API}/users/me",
    ]
    with httpx.Client(timeout=30) as c:
        results: dict[str, list[Any]] = {}
        for name, ep in deployments.items():
            h = _login(c, ep, PATIENT)
            results[name] = [_get(c, ep, p, h) for p in paths]
        stats = {
            n: _get(c, ep, f"{API}/stats", _login(c, ep, PATIENT)) for n, ep in deployments.items()
        }
    base = results["monolith"]
    for name in ("microservices", "serverless"):
        for p, a, b in zip(paths, base, results[name], strict=True):
            assert a == b, f"{name}: ответ на {p} отличается от монолита"
    for name in ("microservices", "serverless"):
        assert stats[name]["posts_total"] == stats["monolith"]["posts_total"]
        assert stats[name]["top_categories"] == stats["monolith"]["top_categories"]


@pytest.mark.parametrize("arch", sorted(FACTORIES))
def test_journey_and_access_control(arch: str, seeded_dsn: str) -> None:
    with FACTORIES[arch](seeded_dsn) as ep, httpx.Client(timeout=30) as c:
        u = ep.url
        reg = c.post(
            u(f"{API}/auth/register"),
            json={
                "email": f"{arch}@example.org",
                "password": "arch-pass-123",
                "display_name": arch,
                "role": "doctor",
            },
        )
        assert reg.status_code == 201, reg.text
        h = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        post = c.post(
            u(f"{API}/posts"),
            headers=h,
            json={
                "title": "Контроль глюкозы",
                "category_id": 2,
                "body": "Эндокринолог рекомендует контролировать глюкозу и питание",
            },
        )
        assert post.status_code == 201, post.text
        pid = post.json()["id"]
        assert post.json()["moderation"]["level"] in (1, 2)
        assert c.get(u(f"{API}/posts/{pid}"), headers=h).json()["author_name"] == arch
        assert (
            c.post(
                u(f"{API}/posts/{pid}/comments"), headers=h, json={"body": "Полезная информация"}
            ).status_code
            == 201
        )
        assert (
            c.put(u(f"{API}/posts/{pid}/reaction"), headers=h, json={"kind": "like"}).json()[
                "reactions_count"
            ]
            == 1
        )
        prof = c.patch(u(f"{API}/users/me"), headers=h, json={"city": "Казань"})
        assert prof.status_code == 200 and prof.json()["city"] == "Казань"
        assert c.get(u(f"{API}/search?q=глюкоза"), headers=h).status_code == 200
        assert c.get(u(f"{API}/stats"), headers=h).status_code == 200
        refreshed = c.post(
            u(f"{API}/auth/refresh"), json={"refresh_token": reg.json()["refresh_token"]}
        )
        assert refreshed.status_code == 200

        # доступ
        assert c.get(u(f"{API}/posts")).status_code == 401
        assert c.get(u(f"{API}/moderation/queue"), headers=h).status_code == 403
        bad = c.post(
            u(f"{API}/posts"), headers=h, json={"title": "", "body": "x", "category_id": 1}
        )
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation_error"
        assert c.get(u(f"{API}/posts/99999999"), headers=h).status_code == 404


def test_gateway_ignores_spoofed_identity_headers(seeded_dsn: str) -> None:
    with FACTORIES["microservices"](seeded_dsn) as ep, httpx.Client(timeout=30) as c:
        spoof = {"X-User-Id": "1", "X-User-Role": "admin"}
        assert c.get(ep.url(f"{API}/moderation/queue"), headers=spoof).status_code == 401
        h = _login(c, ep, PATIENT) | spoof  # валидный токен пациента + подделка заголовков
        assert c.get(ep.url(f"{API}/moderation/queue"), headers=h).status_code == 403
        assert c.get(ep.url(f"{API}/users/me"), headers=h).json()["id"] == PATIENT
        assert c.get(ep.url(f"{API}/internal/moderate")).status_code == 404  # не публикуется
        spec = c.get(ep.url(f"{API}/openapi.json")).json()
        assert "/api/v1/posts" in spec["paths"]
