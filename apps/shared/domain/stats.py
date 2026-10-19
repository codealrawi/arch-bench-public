"""Агрегированная статистика платформы за окно N дней (COUNT/GROUP BY, ТЗ)."""

from __future__ import annotations

from typing import Any

import asyncpg

_POSTS_BY_DAY = """
SELECT date_trunc('day', created_at) AS day, status, count(*) AS n
FROM posts WHERE created_at >= now() - make_interval(days => $1)
GROUP BY 1, 2 ORDER BY 1, 2
"""
_TOP_CATEGORIES = """
SELECT c.id, c.name, count(*) AS n
FROM posts p JOIN medical_categories c ON c.id = p.category_id
WHERE p.created_at >= now() - make_interval(days => $1) AND p.status = 'approved'
GROUP BY c.id, c.name ORDER BY n DESC, c.id LIMIT 5
"""
_TOTALS = """
SELECT
  (SELECT count(*) FROM comments WHERE created_at >= now() - make_interval(days => $1)) AS comments,
  (SELECT count(*) FROM reactions WHERE created_at >= now() - make_interval(days => $1))
      AS reactions,
  (SELECT count(*) FROM users WHERE created_at >= now() - make_interval(days => $1)) AS new_users
"""


async def platform_stats(pool: asyncpg.Pool, window_days: int) -> dict[str, Any]:
    if window_days < 1:
        raise ValueError("window_days должен быть ≥ 1")
    by_day = [
        {"day": r["day"].date().isoformat(), "status": r["status"], "count": int(r["n"])}
        for r in await pool.fetch(_POSTS_BY_DAY, window_days)
    ]
    top = [
        {"category_id": r["id"], "name": r["name"], "posts": int(r["n"])}
        for r in await pool.fetch(_TOP_CATEGORIES, window_days)
    ]
    totals = await pool.fetchrow(_TOTALS, window_days)
    assert totals is not None
    return {
        "window_days": window_days,
        "posts_total": sum(x["count"] for x in by_day),
        "posts_by_day": by_day,
        "comments_total": int(totals["comments"]),
        "reactions_total": int(totals["reactions"]),
        "new_users": int(totals["new_users"]),
        "top_categories": top,
    }
