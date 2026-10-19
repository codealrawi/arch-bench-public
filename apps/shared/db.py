"""Пул соединений PostgreSQL (asyncpg). Схема создаётся только миграциями (database/migrations)."""

from __future__ import annotations

import asyncpg


async def create_pool(database_url: str, size: int, command_timeout_s: float) -> asyncpg.Pool:
    """Пул фиксированного размера (min = max): размер — параметр эксперимента (P-35).

    ``command_timeout_s`` ограничивает время одного запроса к БД; при превышении asyncpg
    отменяет запрос (клиент получает 503/504 — см. apps/shared/http/errors.py).
    """
    if size < 1:
        raise ValueError("размер пула должен быть ≥ 1 для компонентов, работающих с БД")
    if command_timeout_s <= 0:
        raise ValueError("command_timeout_s должен быть > 0")
    pool = await asyncpg.create_pool(
        database_url, min_size=size, max_size=size, command_timeout=command_timeout_s
    )
    assert pool is not None
    return pool
