"""Контент: категории, лента, посты, комментарии, реакции, поиск, очередь модерации.

Все запросы параметризованы ($1, $2, …); пользовательский ввод никогда не попадает в текст SQL.
Публично видны только одобренные (approved) посты и комментарии; автор и модераторы видят
свои неодобренные посты.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg

from apps.shared.auth import Principal
from apps.shared.domain.accounts import audit
from apps.shared.domain.errors import Forbidden, NotFound, ValidationFailed
from apps.shared.domain.moderation import ModerationClient, ModerationVerdict

REACTION_KINDS = ("like", "support", "useful")

_POST_COLUMNS = """
p.id, p.title, p.body, p.status, p.category_id, c.name AS category_name, p.comments_count,
p.reactions_count, p.created_at, p.author_id, pr.display_name AS author_name, u.role AS author_role
"""
_POST_FROM = """
FROM posts p
JOIN users u ON u.id = p.author_id
JOIN profiles pr ON pr.user_id = p.author_id
JOIN medical_categories c ON c.id = p.category_id
"""


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def _row(r: asyncpg.Record) -> dict[str, Any]:
    return {k: _iso(v) for k, v in r.items()}


async def list_categories(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    return [
        _row(r)
        for r in await pool.fetch("SELECT id, slug, name FROM medical_categories ORDER BY id")
    ]


async def feed(
    pool: asyncpg.Pool, *, limit: int, category_id: int | None, before_id: int | None
) -> list[dict[str, Any]]:
    """Последние одобренные посты (keyset-пагинация по id, монотонному со временем создания)."""
    rows = await pool.fetch(
        f"SELECT {_POST_COLUMNS} {_POST_FROM} "
        "WHERE p.status = 'approved' AND ($2::smallint IS NULL OR p.category_id = $2) "
        "AND ($3::bigint IS NULL OR p.id < $3) "
        "ORDER BY p.created_at DESC, p.id DESC LIMIT $1",
        limit,
        category_id,
        before_id,
    )
    return [_row(r) for r in rows]


async def get_post(
    pool: asyncpg.Pool, post_id: int, viewer: Principal, *, comments_limit: int
) -> dict[str, Any]:
    row = await pool.fetchrow(f"SELECT {_POST_COLUMNS} {_POST_FROM} WHERE p.id = $1", post_id)
    if row is None or (
        row["status"] != "approved"
        and row["author_id"] != viewer.user_id
        and not viewer.is_moderator
    ):
        raise NotFound("пост не найден")
    post = _row(row)
    post["comments"] = await list_comments(pool, post_id, limit=comments_limit, after_id=None)
    post["my_reaction"] = await pool.fetchval(
        "SELECT kind FROM reactions WHERE post_id = $1 AND user_id = $2", post_id, viewer.user_id
    )
    return post


async def _record(
    conn: asyncpg.Connection, target_type: str, target_id: int, v: ModerationVerdict
) -> None:
    await conn.execute(
        "INSERT INTO moderation_records (target_type, target_id, label, level, confidence, "
        "reasons) VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
        target_type,
        target_id,
        v.label,
        v.level,
        v.confidence,
        json.dumps(v.reasons, ensure_ascii=False),
    )


async def create_post(
    pool: asyncpg.Pool,
    moderation: ModerationClient,
    author: Principal,
    *,
    title: str,
    body: str,
    category_id: int,
) -> dict[str, Any]:
    """Синхронная модерация до записи; пост и запись модерации — в одной транзакции."""
    verdict = await moderation.moderate(f"{title}\n{body}")
    async with pool.acquire() as conn, conn.transaction():
        try:
            post_id = await conn.fetchval(
                "INSERT INTO posts (author_id, category_id, title, body, status) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING id",
                author.user_id,
                category_id,
                title,
                body,
                verdict.label,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise ValidationFailed("неизвестная категория") from exc
        await _record(conn, "post", int(post_id), verdict)
    return {"id": post_id, "status": verdict.label, "moderation": verdict.to_dict()}


async def delete_post(pool: asyncpg.Pool, post_id: int, actor: Principal) -> None:
    async with pool.acquire() as conn, conn.transaction():
        author = await conn.fetchval(
            "SELECT author_id FROM posts WHERE id = $1 FOR UPDATE", post_id
        )
        if author is None:
            raise NotFound("пост не найден")
        if author != actor.user_id and not actor.is_moderator:
            raise Forbidden("удалить пост может только автор или модератор")
        await conn.execute("DELETE FROM posts WHERE id = $1", post_id)
        await audit(conn, actor.user_id, "post_delete", "post", post_id)


async def list_comments(
    pool: asyncpg.Pool, post_id: int, *, limit: int, after_id: int | None
) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        "SELECT cm.id, cm.body, cm.created_at, cm.author_id, pr.display_name AS author_name "
        "FROM comments cm JOIN profiles pr ON pr.user_id = cm.author_id "
        "WHERE cm.post_id = $1 AND cm.status = 'approved' AND ($3::bigint IS NULL OR cm.id > $3) "
        "ORDER BY cm.id LIMIT $2",
        post_id,
        limit,
        after_id,
    )
    return [_row(r) for r in rows]


async def _visible_post(conn: asyncpg.Connection, post_id: int) -> None:
    status = await conn.fetchval("SELECT status FROM posts WHERE id = $1", post_id)
    if status != "approved":
        raise NotFound("пост не найден")


async def add_comment(
    pool: asyncpg.Pool, moderation: ModerationClient, author: Principal, post_id: int, body: str
) -> dict[str, Any]:
    verdict = await moderation.moderate(body)
    async with pool.acquire() as conn, conn.transaction():
        await _visible_post(conn, post_id)
        comment_id = await conn.fetchval(
            "INSERT INTO comments (post_id, author_id, body, status) VALUES ($1, $2, $3, $4) "
            "RETURNING id",
            post_id,
            author.user_id,
            body,
            verdict.label,
        )
        if verdict.label == "approved":
            await conn.execute(
                "UPDATE posts SET comments_count = comments_count + 1 " "WHERE id = $1", post_id
            )
        else:  # правило записи модерации: каждый неодобренный комментарий (как в seed)
            await _record(conn, "comment", int(comment_id), verdict)
    return {"id": comment_id, "status": verdict.label}


async def set_reaction(
    pool: asyncpg.Pool, user: Principal, post_id: int, kind: str
) -> dict[str, Any]:
    if kind not in REACTION_KINDS:
        raise ValidationFailed(f"kind должен быть одним из {REACTION_KINDS}")
    async with pool.acquire() as conn, conn.transaction():
        await _visible_post(conn, post_id)
        inserted = await conn.fetchval(
            "INSERT INTO reactions (post_id, user_id, kind) VALUES ($1, $2, $3) "
            "ON CONFLICT (post_id, user_id) DO UPDATE SET kind = EXCLUDED.kind "
            "RETURNING (xmax = 0)",
            post_id,
            user.user_id,
            kind,
        )
        if inserted:
            await conn.execute(
                "UPDATE posts SET reactions_count = reactions_count + 1 " "WHERE id = $1", post_id
            )
        count = await conn.fetchval("SELECT reactions_count FROM posts WHERE id = $1", post_id)
    return {"post_id": post_id, "kind": kind, "created": bool(inserted), "reactions_count": count}


async def remove_reaction(pool: asyncpg.Pool, user: Principal, post_id: int) -> None:
    async with pool.acquire() as conn, conn.transaction():
        deleted = await conn.fetchval(
            "DELETE FROM reactions WHERE post_id = $1 AND user_id = $2 RETURNING 1",
            post_id,
            user.user_id,
        )
        if not deleted:
            raise NotFound("реакция не найдена")
        await conn.execute(
            "UPDATE posts SET reactions_count = reactions_count - 1 WHERE id = $1", post_id
        )


SEARCH_CANDIDATES = 200
"""Сколько самых свежих совпадений ранжируется по релевантности (протокол v2.1, §14).

Без ограничения ts_rank считался по всем совпадениям: на seed частый термин совпадает с
40–60 % постов (до ~117 тыс. строк), и стоимость запроса росла с размером таблицы.
"""


async def search(pool: asyncpg.Pool, query: str, *, limit: int) -> list[dict[str, Any]]:
    """Полнотекстовый поиск (russian) по одобренным постам.

    Кандидаты — SEARCH_CANDIDATES самых свежих совпадений; среди них порядок по релевантности,
    затем по новизне.
    """
    q = query.strip()
    if not q:
        raise ValidationFailed("пустой поисковый запрос")
    rows = await pool.fetch(
        # tsquery — прямо в условии, не в общем CTE: материализованный CTE скрывает значение от
        # планировщика, и он выбирает bitmap-скан всех совпадений вместо обхода ix_posts_feed
        "WITH cand AS ("
        " SELECT p.id FROM posts p"
        " WHERE p.status = 'approved' AND p.search @@ plainto_tsquery('russian', $1)"
        " ORDER BY p.created_at DESC LIMIT $3"
        ") "
        f"SELECT {_POST_COLUMNS}, ts_rank(p.search, plainto_tsquery('russian', $1)) AS rank "
        f"{_POST_FROM} JOIN cand ON cand.id = p.id "
        "ORDER BY rank DESC, p.created_at DESC LIMIT $2",
        q,
        limit,
        SEARCH_CANDIDATES,
    )
    return [_row(r) for r in rows]


async def moderation_queue(pool: asyncpg.Pool, *, limit: int) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        f"SELECT {_POST_COLUMNS} {_POST_FROM} WHERE p.status = 'suspicious' "
        "ORDER BY p.created_at DESC LIMIT $1",
        limit,
    )
    return [_row(r) for r in rows]


async def decide(
    pool: asyncpg.Pool, moderator: Principal, post_id: int, decision: str
) -> dict[str, Any]:
    if not moderator.is_moderator:
        raise Forbidden("решения модерации доступны только модераторам")
    status = {"approve": "approved", "block": "blocked"}.get(decision)
    if status is None:
        raise ValidationFailed("decision должен быть approve или block")
    async with pool.acquire() as conn, conn.transaction():
        updated = await conn.fetchval(
            "UPDATE posts SET status = $2, updated_at = now() WHERE id = $1 RETURNING id",
            post_id,
            status,
        )
        if updated is None:
            raise NotFound("пост не найден")
        await _record(
            conn,
            "post",
            post_id,
            ModerationVerdict(status, 2, 1.0, [f"решение модератора {moderator.user_id}"]),
        )
        await audit(
            conn, moderator.user_id, "moderation_decision", "post", post_id, {"decision": decision}
        )
    return {"id": post_id, "status": status}
