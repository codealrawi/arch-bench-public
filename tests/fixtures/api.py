"""Помощники API-тестов.  TEST DATA ONLY."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from database.seed.generator import user_email, user_password
from tests.fixtures.seed_params import PASSWORD_TEMPLATE


def login(
    client: TestClient | httpx.Client,
    user_id: int,
    prefix: str = "/api/v1",
    path: str = "/auth/login",
) -> dict[str, str]:
    r = client.post(
        f"{prefix}{path}",
        json={"email": user_email(user_id), "password": user_password(PASSWORD_TEMPLATE, user_id)},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
