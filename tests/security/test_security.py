"""Проверки безопасности API (OWASP-ориентированные). TEST DATA ONLY."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator

import asyncpg
import jwt
import pytest
from fastapi.testclient import TestClient

from apps.shared.auth import Principal, issue_token
from tests.conftest import TEST_JWT_SECRET
from tests.fixtures.api import login
from tests.fixtures.app_env import settings

pytestmark = pytest.mark.pg
API = "/api/v1"
PATIENT, OTHER = 10, 12


def _count(dsn: str, sql: str) -> int:
    async def q() -> int:
        conn = await asyncpg.connect(dsn)
        try:
            return int(await conn.fetchval(sql))
        finally:
            await conn.close()

    return asyncio.run(q())


@pytest.mark.parametrize(
    "payload",
    [
        "'; DROP TABLE posts; --",
        '" OR 1=1 --',
        "%' UNION SELECT password_hash FROM users --",
        "терапия) & !(",
        "\\x00\\x27",
    ],
)
def test_sql_injection_in_search(monolith: TestClient, seeded_dsn: str, payload: str) -> None:
    before = _count(seeded_dsn, "SELECT count(*) FROM posts")
    r = monolith.get(f"{API}/search", params={"q": payload}, headers=login(monolith, PATIENT))
    assert r.status_code in (200, 422)
    assert "pbkdf2" not in r.text
    assert _count(seeded_dsn, "SELECT count(*) FROM posts") == before


def test_sql_injection_in_login(monolith: TestClient) -> None:
    r = monolith.post(
        f"{API}/auth/login",
        json={"email": "user10@bench.example.com' --", "password": "x' OR '1'='1"},
    )
    assert r.status_code in (401, 422)


def test_stored_xss_is_returned_as_inert_json(monolith: TestClient) -> None:
    h = login(monolith, PATIENT)
    evil = "<script>alert(document.cookie)</script><img src=x onerror=alert(1)>"
    pid = monolith.post(
        f"{API}/posts", headers=h, json={"title": "XSS проба", "body": evil, "category_id": 1}
    ).json()["id"]
    r = monolith.get(f"{API}/posts/{pid}", headers=h)
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["body"] == evil  # хранится как текст; frontend выводит через textContent
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_security_headers_present(monolith: TestClient) -> None:
    r = monolith.get(f"{API}/categories")
    for header in (
        "x-content-type-options",
        "x-frame-options",
        "referrer-policy",
        "content-security-policy",
        "permissions-policy",
        "x-request-id",
    ):
        assert header in r.headers, header
    assert "strict-transport-security" not in r.headers  # HSTS только при HSTS=true (HTTPS)


@pytest.mark.parametrize(
    "make_token",
    [
        lambda: jwt.encode(
            {"sub": "1", "role": "admin", "iat": int(time.time()), "exp": int(time.time()) + 60},
            None,
            algorithm="none",
        ),
        lambda: jwt.encode(
            {"sub": "1", "role": "admin", "iat": int(time.time()), "exp": int(time.time()) + 60},
            "wrong-secret-" + "z" * 40,
            algorithm="HS256",
        ),
        lambda: issue_token(Principal(1, "admin"), TEST_JWT_SECRET, 1, now=time.time() - 3600),
        lambda: jwt.encode(
            {
                "sub": "1",
                "role": "superuser",
                "iat": int(time.time()),
                "exp": int(time.time()) + 60,
            },
            TEST_JWT_SECRET,
            algorithm="HS256",
        ),
        lambda: jwt.encode(
            {
                "sub": "1",
                "role": "admin",
                "typ": "refresh",
                "iat": int(time.time()),
                "exp": int(time.time()) + 60,
            },
            TEST_JWT_SECRET,
            algorithm="HS256",
        ),
        lambda: "not-a-jwt",
    ],
)
def test_forged_tokens_rejected(monolith: TestClient, make_token) -> None:  # type: ignore[no-untyped-def]
    r = monolith.get(f"{API}/moderation/queue", headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


def test_broken_access_control(monolith: TestClient) -> None:
    patient = login(monolith, PATIENT)
    # обычный пользователь не может модерировать
    assert monolith.get(f"{API}/moderation/queue", headers=patient).status_code == 403
    assert (
        monolith.post(
            f"{API}/moderation/posts/1/decision", headers=patient, json={"decision": "block"}
        ).status_code
        == 403
    )
    # нельзя повысить себе роль через профиль или регистрацию
    assert (
        monolith.patch(f"{API}/users/me", headers=patient, json={"role": "admin"}).status_code
        == 422
    )
    assert (
        monolith.post(
            f"{API}/auth/register",
            json={
                "email": "x@example.org",
                "password": "longenough1",
                "display_name": "x",
                "role": "moderator",
            },
        ).status_code
        == 422
    )
    # чужой пост не удалить
    other = login(monolith, OTHER)
    pid = monolith.post(
        f"{API}/posts", headers=other, json={"title": "Чужой", "body": "Текст", "category_id": 1}
    ).json()["id"]
    assert monolith.delete(f"{API}/posts/{pid}", headers=patient).status_code == 403


def test_no_secrets_in_responses(monolith: TestClient) -> None:
    h = login(monolith, PATIENT)
    for path in ("/users/me", f"/users/{OTHER}", "/posts", "/stats"):
        text = monolith.get(f"{API}{path}", headers=h).text
        assert "pbkdf2" not in text and "password" not in text.lower(), path
    reg = monolith.post(
        f"{API}/auth/register",
        json={"email": "secret@example.org", "password": "my-secret-pass", "display_name": "S"},
    )
    assert "my-secret-pass" not in reg.text


def test_error_responses_hide_internals(monolith: TestClient) -> None:
    r = monolith.post(
        f"{API}/posts",
        headers=login(monolith, PATIENT),
        json={"title": "t", "body": "b", "category_id": 30000},
    )
    assert r.status_code == 422
    assert "asyncpg" not in r.text and "violates" not in r.text


@pytest.fixture
def limited(seeded_dsn: str) -> Iterator[TestClient]:
    from apps.monolith.app import create_app

    cfg = settings(
        seeded_dsn,
        SERVICE_NAME="limited",
        RATE_LIMIT_AUTH_PER_MINUTE="3",
        CORS_ORIGINS="https://allowed.example",
    )
    with TestClient(create_app(cfg)) as c:
        yield c


def test_rate_limit_on_auth(limited: TestClient) -> None:
    codes = [
        limited.post(
            f"{API}/auth/login", json={"email": "nobody@example.org", "password": "wrong"}
        ).status_code
        for _ in range(4)
    ]
    assert codes[:3] == [401, 401, 401] and codes[3] == 429
    last = limited.post(f"{API}/auth/login", json={"email": "a@example.org", "password": "x"})
    assert int(last.headers["retry-after"]) >= 1
    assert last.json()["error"]["code"] == "rate_limited"
    # операции нагрузки rate limit не затрагивает
    assert limited.get(f"{API}/categories").status_code == 200


def test_cors(limited: TestClient, monolith: TestClient) -> None:
    ok = limited.get(f"{API}/categories", headers={"Origin": "https://allowed.example"})
    assert ok.headers.get("access-control-allow-origin") == "https://allowed.example"
    bad = limited.get(f"{API}/categories", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad.headers
    # без CORS_ORIGINS — только same-origin
    none = monolith.get(f"{API}/categories", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in none.headers


def test_trusted_headers_mode_requires_gateway_headers(seeded_dsn: str) -> None:
    from apps.monolith.app import create_app

    cfg = settings(seeded_dsn, SERVICE_NAME="internal", AUTH_MODE="trusted-headers")
    with TestClient(create_app(cfg)) as c:
        assert c.get(f"{API}/posts").status_code == 401
        assert (
            c.get(f"{API}/posts", headers={"X-User-Id": "10", "X-User-Role": "root"}).status_code
            == 401
        )
        assert (
            c.get(f"{API}/posts", headers={"X-User-Id": "10", "X-User-Role": "patient"}).status_code
            == 200
        )


def test_cors_wildcard_forbidden() -> None:
    from apps.shared.settings import SettingsError

    with pytest.raises(SettingsError):
        settings(None, CORS_ORIGINS="*")
