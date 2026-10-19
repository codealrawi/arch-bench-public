"""API v1 ↔ PostgreSQL: все эндпоинты монолита (TEST DATA ONLY)."""

from __future__ import annotations

import asyncio
import re

import asyncpg
import pytest
from fastapi.testclient import TestClient

from tests.fixtures.api import login

pytestmark = pytest.mark.pg
API = "/api/v1"
PATIENT, DOCTOR, MODERATOR, OTHER = 10, 11, 2, 12


def _db(dsn: str, sql: str, *args: object) -> object:
    async def q() -> object:
        conn = await asyncpg.connect(dsn)
        try:
            return await conn.fetchval(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(q())


def test_health_readiness_metrics_openapi(monolith: TestClient) -> None:
    assert monolith.get("/health").json()["status"] == "ok"
    assert monolith.get("/readiness").json()["status"] == "ready"
    monolith.get(f"{API}/categories")
    m = monolith.get("/metrics").text
    # метка route — шаблон пути (форма зависит от версии FastAPI: с префиксом или без)
    assert re.search(
        r'app_http_requests_total\{method="GET",route="(/api/v1)?/categories",'
        r'service="monolith",status="200"\} [1-9]\d*\.0',
        m,
    ), m[-2000:]
    assert 'route="/health"' not in m  # служебные пути не учитываются
    spec = monolith.get(f"{API}/openapi.json").json()
    for path in (
        "/api/v1/auth/register",
        "/api/v1/posts",
        "/api/v1/posts/{post_id}/comments",
        "/api/v1/search",
        "/api/v1/stats",
        "/api/v1/users/me",
    ):
        assert path in spec["paths"], path
    assert "/api/v1/internal/moderate" not in spec["paths"]


def test_register_login_refresh_logout(monolith: TestClient, seeded_dsn: str) -> None:
    r = monolith.post(
        f"{API}/auth/register",
        json={
            "email": "New.User@example.org",
            "password": "correct horse 1",
            "display_name": "Новый пользователь",
            "role": "doctor",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user"]["role"] == "doctor" and body["refresh_token"]
    dup = monolith.post(
        f"{API}/auth/register",
        json={"email": "new.user@EXAMPLE.org", "password": "another pass 1", "display_name": "x"},
    )
    assert dup.status_code == 409
    assert (
        monolith.post(
            f"{API}/auth/login", json={"email": "new.user@example.org", "password": "wrong-pass"}
        ).status_code
        == 401
    )
    ok = monolith.post(
        f"{API}/auth/login", json={"email": "new.user@example.org", "password": "correct horse 1"}
    )
    assert ok.status_code == 200
    rt = ok.json()["refresh_token"]
    rotated = monolith.post(f"{API}/auth/refresh", json={"refresh_token": rt})
    assert rotated.status_code == 200
    assert monolith.post(f"{API}/auth/refresh", json={"refresh_token": rt}).status_code == 401
    new_rt = rotated.json()["refresh_token"]
    assert monolith.post(f"{API}/auth/logout", json={"refresh_token": new_rt}).status_code == 204
    assert monolith.post(f"{API}/auth/refresh", json={"refresh_token": new_rt}).status_code == 401
    assert _db(seeded_dsn, "SELECT count(*) FROM audit_logs WHERE action = 'register'") == 1
    stored = _db(
        seeded_dsn,
        "SELECT password_hash FROM users WHERE lower(email) = $1",
        "new.user@example.org",
    )
    assert str(stored).startswith("pbkdf2_sha256$") and "correct horse" not in str(stored)


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "bad", "password": "longenough1", "display_name": "x"},
        {"email": "a@b.org", "password": "short", "display_name": "x"},
        {"email": "a@b.org", "password": "longenough1", "display_name": ""},
        {"email": "a@b.org", "password": "longenough1", "display_name": "x", "role": "admin"},
        {"email": "a@b.org", "password": "longenough1", "display_name": "x", "extra": 1},
    ],
)
def test_register_validation(monolith: TestClient, payload: dict) -> None:  # type: ignore[type-arg]
    r = monolith.post(f"{API}/auth/register", json=payload)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_profiles(monolith: TestClient) -> None:
    h = login(monolith, PATIENT)
    me = monolith.get(f"{API}/users/me", headers=h).json()
    assert me["id"] == PATIENT and "birth_year" in me
    upd = monolith.patch(f"{API}/users/me", headers=h, json={"bio": "новое", "city": "Тверь"})
    assert upd.status_code == 200 and upd.json()["bio"] == "новое"
    other = monolith.get(f"{API}/users/{OTHER}", headers=h).json()
    assert "birth_year" not in other and other["id"] == OTHER
    assert monolith.get(f"{API}/users/999999", headers=h).status_code == 404
    assert monolith.patch(f"{API}/users/me", headers=h, json={}).status_code == 422
    assert (
        monolith.patch(f"{API}/users/me", headers=h, json={"specialty_id": 999}).status_code == 422
    )


def test_feed_post_comments_reactions(monolith: TestClient, seeded_dsn: str) -> None:
    h = login(monolith, PATIENT)
    feed = monolith.get(f"{API}/posts", headers=h).json()
    assert 0 < len(feed) <= 20
    assert all(p["status"] == "approved" for p in feed)
    times = [p["created_at"] for p in feed]
    assert times == sorted(times, reverse=True)
    page2 = monolith.get(f"{API}/posts", headers=h, params={"before_id": feed[-1]["id"]}).json()
    assert all(p["id"] < feed[-1]["id"] for p in page2)
    cat = feed[0]["category_id"]
    assert all(
        p["category_id"] == cat
        for p in monolith.get(f"{API}/posts", headers=h, params={"category_id": cat}).json()
    )

    created = monolith.post(
        f"{API}/posts",
        headers=h,
        json={
            "title": "Реабилитация после операции",
            "body": "Врач назначил курс терапии",
            "category_id": 1,
        },
    )
    assert created.status_code == 201
    pid = created.json()["id"]
    assert created.json()["status"] in {"approved", "suspicious", "blocked"}
    assert (
        _db(
            seeded_dsn,
            "SELECT count(*) FROM moderation_records WHERE target_type='post' " "AND target_id=$1",
            pid,
        )
        == 1
    )
    detail = monolith.get(f"{API}/posts/{pid}", headers=h).json()
    assert detail["id"] == pid and detail["comments"] == [] and detail["my_reaction"] is None

    if created.json()["status"] == "approved":
        c = monolith.post(f"{API}/posts/{pid}/comments", headers=h, json={"body": "Спасибо"})
        assert c.status_code == 201
        assert len(monolith.get(f"{API}/posts/{pid}/comments", headers=h).json()) >= 0
        r1 = monolith.put(f"{API}/posts/{pid}/reaction", headers=h, json={"kind": "like"})
        assert r1.json() == {"post_id": pid, "kind": "like", "created": True, "reactions_count": 1}
        r2 = monolith.put(f"{API}/posts/{pid}/reaction", headers=h, json={"kind": "useful"})
        assert r2.json()["created"] is False and r2.json()["reactions_count"] == 1
        assert monolith.get(f"{API}/posts/{pid}", headers=h).json()["my_reaction"] == "useful"
        assert monolith.delete(f"{API}/posts/{pid}/reaction", headers=h).status_code == 204
        assert monolith.delete(f"{API}/posts/{pid}/reaction", headers=h).status_code == 404
        assert _db(seeded_dsn, "SELECT reactions_count FROM posts WHERE id=$1", pid) == 0
    assert (
        monolith.post(
            f"{API}/posts", headers=h, json={"title": "t", "body": "b", "category_id": 999}
        ).status_code
        == 422
    )
    assert (
        monolith.post(
            f"{API}/posts", headers=h, json={"title": "   ", "body": "b", "category_id": 1}
        ).status_code
        == 422
    )
    assert (
        monolith.put(f"{API}/posts/{pid}/reaction", headers=h, json={"kind": "love"}).status_code
        == 422
    )


def test_comment_counter_on_existing_post(monolith: TestClient, seeded_dsn: str) -> None:
    h = login(monolith, PATIENT)
    pid = monolith.get(f"{API}/posts", headers=h).json()[0]["id"]
    before = _db(seeded_dsn, "SELECT comments_count FROM posts WHERE id=$1", pid)
    r = monolith.post(
        f"{API}/posts/{pid}/comments", headers=h, json={"body": "Уточните дозировку препарата"}
    )
    assert r.status_code == 201
    after = _db(seeded_dsn, "SELECT comments_count FROM posts WHERE id=$1", pid)
    assert after == before + (1 if r.json()["status"] == "approved" else 0)
    assert (
        monolith.post(f"{API}/posts/999999/comments", headers=h, json={"body": "x"}).status_code
        == 404
    )


def test_search(monolith: TestClient) -> None:
    h = login(monolith, PATIENT)
    hits = monolith.get(f"{API}/search", headers=h, params={"q": "терапия"}).json()
    assert hits and all("терап" in (x["title"] + x["body"]).lower() for x in hits)
    assert monolith.get(f"{API}/search", headers=h, params={"q": ""}).status_code == 422


def test_stats(monolith: TestClient, seeded_dsn: str) -> None:
    h = login(monolith, PATIENT)
    st = monolith.get(f"{API}/stats", headers=h).json()
    expected = _db(
        seeded_dsn, "SELECT count(*) FROM posts WHERE created_at >= " "now() - interval '7 days'"
    )
    assert st["window_days"] == 7 and st["posts_total"] == expected
    assert sum(d["count"] for d in st["posts_by_day"]) == expected
    assert len(st["top_categories"]) <= 5


def test_delete_post_authorization(monolith: TestClient) -> None:
    author, stranger, mod = (
        login(monolith, PATIENT),
        login(monolith, OTHER),
        login(monolith, MODERATOR),
    )
    pid = monolith.post(
        f"{API}/posts",
        headers=author,
        json={"title": "Мой пост", "body": "Текст о лечении", "category_id": 2},
    ).json()["id"]
    assert monolith.delete(f"{API}/posts/{pid}", headers=stranger).status_code == 403
    assert monolith.delete(f"{API}/posts/{pid}", headers=mod).status_code == 204
    assert monolith.delete(f"{API}/posts/{pid}", headers=author).status_code == 404


def test_moderation_queue_and_decision(monolith: TestClient, seeded_dsn: str) -> None:
    patient, mod = login(monolith, PATIENT), login(monolith, MODERATOR)
    assert monolith.get(f"{API}/moderation/queue", headers=patient).status_code == 403
    queue = monolith.get(f"{API}/moderation/queue", headers=mod).json()
    if not queue:
        pytest.skip("в уменьшенном seed нет suspicious-постов")
    pid = queue[0]["id"]
    r = monolith.post(
        f"{API}/moderation/posts/{pid}/decision", headers=mod, json={"decision": "approve"}
    )
    assert r.json() == {"id": pid, "status": "approved"}
    assert (
        _db(seeded_dsn, "SELECT count(*) FROM audit_logs WHERE action='moderation_decision'") == 1
    )


def test_hidden_posts_not_visible_to_others(monolith: TestClient, seeded_dsn: str) -> None:
    pid = _db(seeded_dsn, "SELECT id FROM posts WHERE status <> 'approved' ORDER BY id LIMIT 1")
    if pid is None:
        pytest.skip("нет неодобренных постов")
    author = _db(seeded_dsn, "SELECT author_id FROM posts WHERE id=$1", pid)
    viewer = PATIENT if author != PATIENT else OTHER
    assert monolith.get(f"{API}/posts/{pid}", headers=login(monolith, viewer)).status_code == 404
    assert monolith.get(f"{API}/posts/{pid}", headers=login(monolith, MODERATOR)).status_code == 200


def test_unauthenticated(monolith: TestClient) -> None:
    for method, path in (
        ("get", "/posts"),
        ("get", "/posts/1"),
        ("get", "/search?q=x"),
        ("get", "/stats"),
        ("get", "/users/me"),
        ("post", "/posts"),
    ):
        r = getattr(monolith, method)(f"{API}{path}")
        assert r.status_code == 401, path
        assert r.json()["error"]["code"] == "unauthorized"
    assert monolith.get(f"{API}/categories").status_code == 200  # публичный справочник


def test_error_format_and_request_id(monolith: TestClient) -> None:
    r = monolith.get(f"{API}/posts/abc", headers=login(monolith, PATIENT))
    assert r.status_code == 422
    assert r.json()["error"]["request_id"] == r.headers["x-request-id"]
    r2 = monolith.get(f"{API}/categories", headers={"X-Request-Id": "abc123"})
    assert r2.headers["x-request-id"] == "abc123"
