"""Учётные записи: регистрация, вход, refresh, выход, профили."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from apps.shared.auth import (
    Principal,
    issue_token,
    new_password_hash,
    new_refresh_token,
    refresh_token_hash,
    verify_password,
)
from apps.shared.domain.errors import Conflict, NotFound, Unauthorized, ValidationFailed

SELF_REGISTER_ROLES = ("patient", "doctor")
PROFILE_FIELDS = ("display_name", "bio", "city", "birth_year", "specialty_id")

_PROFILE_SQL = """
SELECT u.id, u.role, u.created_at, p.display_name, p.bio, p.city, p.birth_year,
       p.specialty_id, c.name AS specialty_name, p.updated_at
FROM users u JOIN profiles p ON p.user_id = u.id
LEFT JOIN medical_categories c ON c.id = p.specialty_id
WHERE u.id = $1 AND u.is_active
"""


async def audit(
    conn: asyncpg.Connection | asyncpg.Pool,
    user_id: int | None,
    action: str,
    entity: str,
    entity_id: int | None,
    details: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO audit_logs (user_id, action, entity, entity_id, details) "
        "VALUES ($1, $2, $3, $4, $5::jsonb)",
        user_id,
        action,
        entity,
        entity_id,
        json.dumps(details or {}, ensure_ascii=False),
    )


async def _issue_pair(
    conn: asyncpg.Connection,
    principal: Principal,
    secret: str,
    access_ttl_s: int,
    refresh_ttl_s: int,
) -> dict[str, Any]:
    token, token_hash = new_refresh_token()
    expires = datetime.now(UTC) + timedelta(seconds=refresh_ttl_s)
    await conn.execute(
        "INSERT INTO refresh_tokens (user_id, token_hash, expires_at) VALUES ($1, $2, $3)",
        principal.user_id,
        token_hash,
        expires,
    )
    return {
        "access_token": issue_token(principal, secret, access_ttl_s),
        "refresh_token": token,
        "token_type": "bearer",
        "expires_in": access_ttl_s,
    }


async def register(
    pool: asyncpg.Pool,
    *,
    email: str,
    password: str,
    display_name: str,
    role: str,
    secret: str,
    access_ttl_s: int,
    refresh_ttl_s: int,
    pbkdf2_iterations: int,
) -> dict[str, Any]:
    if role not in SELF_REGISTER_ROLES:
        raise ValidationFailed("при регистрации доступны роли patient и doctor")
    password_hash = new_password_hash(password, pbkdf2_iterations)
    async with pool.acquire() as conn, conn.transaction():
        try:
            user_id = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) VALUES ($1, $2, $3) RETURNING id",
                email.strip(),
                password_hash,
                role,
            )
        except asyncpg.UniqueViolationError as exc:
            raise Conflict("пользователь с таким email уже зарегистрирован") from exc
        await conn.execute(
            "INSERT INTO profiles (user_id, display_name) VALUES ($1, $2)",
            user_id,
            display_name.strip(),
        )
        await audit(conn, user_id, "register", "user", user_id)
        tokens = await _issue_pair(
            conn, Principal(user_id, role), secret, access_ttl_s, refresh_ttl_s
        )
    return {"user": {"id": user_id, "email": email.strip(), "role": role}, **tokens}


async def login(
    pool: asyncpg.Pool,
    *,
    email: str,
    password: str,
    secret: str,
    access_ttl_s: int,
    refresh_ttl_s: int,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, role, password_hash, is_active FROM users WHERE lower(email) = lower($1)",
            email.strip(),
        )
        # одинаковый ответ для «нет пользователя» и «неверный пароль» (без перебора email)
        if (
            row is None
            or not row["is_active"]
            or not verify_password(password, row["password_hash"])
        ):
            raise Unauthorized("неверный email или пароль")
        async with conn.transaction():
            await audit(conn, row["id"], "login", "user", row["id"])
            tokens = await _issue_pair(
                conn,
                Principal(int(row["id"]), str(row["role"])),
                secret,
                access_ttl_s,
                refresh_ttl_s,
            )
    return {"user": {"id": row["id"], "role": row["role"]}, **tokens}


async def refresh(
    pool: asyncpg.Pool, *, refresh_token: str, secret: str, access_ttl_s: int, refresh_ttl_s: int
) -> dict[str, Any]:
    """Ротация: использованный refresh-токен отзывается, выдаётся новая пара."""
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "UPDATE refresh_tokens t SET revoked_at = now() FROM users u "
            "WHERE t.token_hash = $1 AND t.revoked_at IS NULL AND t.expires_at > now() "
            "AND u.id = t.user_id AND u.is_active RETURNING u.id, u.role",
            refresh_token_hash(refresh_token),
        )
        if row is None:
            raise Unauthorized("refresh-токен недействителен")
        await audit(conn, row["id"], "refresh", "user", row["id"])
        tokens = await _issue_pair(
            conn, Principal(int(row["id"]), str(row["role"])), secret, access_ttl_s, refresh_ttl_s
        )
    return tokens


async def logout(pool: asyncpg.Pool, *, refresh_token: str) -> None:
    await pool.execute(
        "UPDATE refresh_tokens SET revoked_at = now() "
        "WHERE token_hash = $1 AND revoked_at IS NULL",
        refresh_token_hash(refresh_token),
    )


def _profile(row: asyncpg.Record, include_private: bool) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "role": row["role"],
        "display_name": row["display_name"],
        "bio": row["bio"],
        "city": row["city"],
        "specialty_id": row["specialty_id"],
        "specialty_name": row["specialty_name"],
        "member_since": row["created_at"].isoformat(),
    }
    if include_private:
        out["birth_year"] = row["birth_year"]
        out["updated_at"] = row["updated_at"].isoformat()
    return out


async def get_profile(pool: asyncpg.Pool, user_id: int, *, include_private: bool) -> dict[str, Any]:
    row = await pool.fetchrow(_PROFILE_SQL, user_id)
    if row is None:
        raise NotFound("пользователь не найден")
    return _profile(row, include_private)


async def update_profile(
    pool: asyncpg.Pool, user_id: int, changes: dict[str, Any]
) -> dict[str, Any]:
    fields = {k: v for k, v in changes.items() if k in PROFILE_FIELDS}
    if not fields:
        raise ValidationFailed("нет изменяемых полей")
    cols = sorted(fields)
    assignments = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))  # имена из белого списка
    async with pool.acquire() as conn, conn.transaction():
        try:
            updated = await conn.execute(
                f"UPDATE profiles SET {assignments}, updated_at = now() WHERE user_id = $1",
                user_id,
                *[fields[c] for c in cols],
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise ValidationFailed("неизвестная специальность") from exc
        if updated == "UPDATE 0":
            raise NotFound("профиль не найден")
        await audit(conn, user_id, "profile_update", "profile", user_id, {"fields": cols})
    return await get_profile(pool, user_id, include_private=True)
