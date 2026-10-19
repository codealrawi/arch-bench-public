"""E2E: полный пользовательский сценарий через реальный HTTP (uvicorn-процесс + PostgreSQL 16).

register → login → create post → read post → comment → reaction → statistics (+ frontend).
Позже параметризуется по архитектурам (фазы 7–8). TEST DATA ONLY.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from tests.fixtures.app_env import env
from tests.fixtures.processes import uvicorn_process

pytestmark = pytest.mark.pg
API = "/api/v1"


@pytest.fixture
def monolith_url(seeded_dsn: str) -> Iterator[str]:
    with uvicorn_process("apps.monolith.app:create_app", env(seeded_dsn, "monolith-e2e")) as url:
        yield url


def test_full_user_journey(monolith_url: str) -> None:
    with httpx.Client(base_url=monolith_url, timeout=30) as c:
        # frontend раздаётся монолитом и защищён CSP
        page = c.get("/")
        assert page.status_code == 200 and "app.js" in page.text
        assert "default-src 'self'" in page.headers["content-security-policy"]

        reg = c.post(
            f"{API}/auth/register",
            json={
                "email": "journey@example.org",
                "password": "journey-pass-1",
                "display_name": "Путешественник",
                "role": "patient",
            },
        )
        assert reg.status_code == 201
        tok = c.post(
            f"{API}/auth/login", json={"email": "journey@example.org", "password": "journey-pass-1"}
        ).json()
        h = {"Authorization": f"Bearer {tok['access_token']}"}

        post = c.post(
            f"{API}/posts",
            headers=h,
            json={
                "title": "Восстановление после операции",
                "category_id": 19,
                "body": "Врач рекомендует лечебную физкультуру и контроль давления",
            },
        )
        assert post.status_code == 201
        pid = post.json()["id"]
        assert post.json()["status"] == "approved"

        read = c.get(f"{API}/posts/{pid}", headers=h).json()
        assert read["title"] == "Восстановление после операции"
        assert read["author_name"] == "Путешественник"

        com = c.post(f"{API}/posts/{pid}/comments", headers=h, json={"body": "Спасибо за совет"})
        assert com.status_code == 201

        react = c.put(f"{API}/posts/{pid}/reaction", headers=h, json={"kind": "support"})
        assert react.json()["reactions_count"] == 1

        after = c.get(f"{API}/posts/{pid}", headers=h).json()
        assert after["my_reaction"] == "support"
        assert after["comments_count"] == (1 if com.json()["status"] == "approved" else 0)
        assert any(x["body"] == "Спасибо за совет" for x in after["comments"]) == (
            com.json()["status"] == "approved"
        )

        feed_ids = [p["id"] for p in c.get(f"{API}/posts", headers=h).json()]
        assert pid in feed_ids  # самый свежий одобренный пост

        stats = c.get(f"{API}/stats", headers=h).json()
        assert stats["posts_total"] >= 1 and stats["new_users"] >= 1

        rt = c.post(f"{API}/auth/refresh", json={"refresh_token": tok["refresh_token"]})
        assert rt.status_code == 200
        assert (
            c.get(
                f"{API}/users/me", headers={"Authorization": f"Bearer {rt.json()['access_token']}"}
            ).json()["display_name"]
            == "Путешественник"
        )
