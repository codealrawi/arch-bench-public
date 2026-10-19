"""Миграции, seed, сброс, проверки, health, backup/restore на реальном PostgreSQL 16.

Объём уменьшен (TEST DATA ONLY); логика та же, что на полном объёме.
"""

from __future__ import annotations

import asyncpg
import pytest

from database import manage
from database.seed import snapshot
from tests.fixtures.seed_params import small

pytestmark = pytest.mark.pg
WINDOW = 7

TABLES = {
    "medical_categories",
    "users",
    "profiles",
    "posts",
    "comments",
    "reactions",
    "moderation_records",
    "refresh_tokens",
    "audit_logs",
}


async def _tables(dsn: str, schema: str = "public") -> set[str]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = $1", schema)
        return {r["tablename"] for r in rows}
    finally:
        await conn.close()


async def test_migrations_up_down_up(pg_dsn: str) -> None:
    assert manage.migrate(pg_dsn, "up") == {"current": "0004", "head": "0004"}
    assert TABLES | {"alembic_version"} <= await _tables(pg_dsn)
    assert "posts" in await _tables(pg_dsn, "seed_snapshot")
    assert manage.migrate(pg_dsn, "down")["current"] is None
    assert await _tables(pg_dsn) == {"alembic_version"}
    assert await _tables(pg_dsn, "seed_snapshot") == set()
    assert manage.migrate(pg_dsn, "up")["current"] == "0004"


async def _seeded(dsn: str) -> asyncpg.Connection:
    conn = await asyncpg.connect(dsn)
    await snapshot.load_snapshot(conn, small())
    await snapshot.reset(conn)
    return conn


async def test_seed_reset_verify(migrated_dsn: str) -> None:
    conn = await _seeded(migrated_dsn)
    try:
        st = await snapshot.verify(conn, WINDOW)
        p = small()
        assert st.counts["users"] == p.users and st.counts["posts"] == p.posts
        assert st.counts["comments"] == p.comments and st.counts["reactions"] == p.reactions
        assert st.counts["medical_categories"] == 20
        # FK и ограничения соблюдены (иначе INSERT упал бы); полнотекстовый вектор построен
        n = await conn.fetchval("SELECT count(*) FROM posts WHERE search IS NULL")
        assert n == 0
    finally:
        await conn.close()


async def test_reset_restores_after_writes(migrated_dsn: str) -> None:
    conn = await _seeded(migrated_dsn)
    try:
        before = await snapshot.data_state(conn, WINDOW)
        await conn.execute(
            "INSERT INTO posts (author_id, category_id, title, body, status) "
            "SELECT 5, 1, 't', 'b', 'approved' FROM generate_series(1, 50)"
        )
        await conn.execute("DELETE FROM reactions WHERE post_id < 10")
        await conn.execute("UPDATE profiles SET bio = 'changed' WHERE user_id = 7")
        await conn.execute("INSERT INTO audit_logs (action, entity) VALUES ('x', 'y')")
        await snapshot.reset(conn)
        after = await snapshot.verify(conn, WINDOW)
        assert after.counts == before.counts
        assert after.snapshot_md5 == before.snapshot_md5
        assert await conn.fetchval("SELECT bio FROM profiles WHERE user_id = 7") != "changed"
        new_id = await conn.fetchval(
            "INSERT INTO posts (author_id, category_id, title, body, "
            "status) VALUES (5, 1, 't', 'b', 'approved') RETURNING id"
        )
        assert new_id == small().posts + 1  # последовательности выставлены после эталона
    finally:
        await conn.close()


async def test_same_seed_same_snapshot(migrated_dsn: str) -> None:
    conn = await _seeded(migrated_dsn)
    try:
        first = await snapshot.snapshot_checksums(conn)
        await snapshot.load_snapshot(conn, small())
        assert await snapshot.snapshot_checksums(conn) == first
        await snapshot.load_snapshot(conn, small(seed=99))
        assert await snapshot.snapshot_checksums(conn) != first
    finally:
        await conn.close()


async def test_verify_detects_drift(migrated_dsn: str) -> None:
    conn = await _seeded(migrated_dsn)
    try:
        await conn.execute("DELETE FROM comments WHERE id <= 3")
        with pytest.raises(snapshot.DataValidationError, match="comments"):
            await snapshot.verify(conn, WINDOW)
    finally:
        await conn.close()


async def test_snapshot_checksum_is_locale_independent(migrated_dsn: str) -> None:
    """Сумма эталона = сумма, посчитанная вне PostgreSQL из исходных данных генератора.

    Регрессия: md5(s::text) на Windows (cp1252) брал «Ревматология» в кавычки, в контейнере —
    нет; одинаковые данные давали разные суммы на разных узлах.
    """
    import hashlib
    import json

    from database.seed.generator import CATEGORIES

    conn = await _seeded(migrated_dsn)
    try:
        rows = [
            json.dumps(
                {"id": i, "slug": slug, "name": name}, ensure_ascii=False, separators=(",", ":")
            )
            for i, (slug, name) in enumerate(CATEGORIES, start=1)
        ]
        expected = hashlib.md5(
            "".join(hashlib.md5(r.encode()).hexdigest() for r in rows).encode()
        ).hexdigest()
        assert (await snapshot.snapshot_checksums(conn))["categories"] == expected
    finally:
        await conn.close()


async def test_reset_leaves_visibility_map_for_index_only_scans(migrated_dsn: str) -> None:
    """v2.1: после сброса VACUUM заполняет карту видимости — count(*) за окно без heap fetch."""
    conn = await _seeded(migrated_dsn)
    try:
        await snapshot.reset(conn)
        rows = await conn.fetch(
            "SELECT relname, relpages, relallvisible FROM pg_class "
            "WHERE relnamespace = 'public'::regnamespace "
            "AND relname IN ('posts', 'comments', 'reactions')"
        )
        assert len(rows) == 3
        assert all(r["relallvisible"] == r["relpages"] > 0 for r in rows), [dict(r) for r in rows]
    finally:
        await conn.close()


async def test_search_ranks_most_recent_candidates(migrated_dsn: str) -> None:
    """v2.1: релевантность — среди SEARCH_CANDIDATES самых свежих совпадений."""
    from apps.shared.domain import content

    conn = await _seeded(migrated_dsn)
    pool = await asyncpg.create_pool(migrated_dsn, min_size=1, max_size=1)
    try:
        term = "кардиология"
        newest = await conn.fetch(
            "SELECT id FROM posts WHERE status = 'approved' "
            "AND search @@ plainto_tsquery('russian', $1) "
            "ORDER BY created_at DESC LIMIT $2",
            term,
            content.SEARCH_CANDIDATES,
        )
        hits = await content.search(pool, term, limit=20)
        assert hits and {h["id"] for h in hits} <= {r["id"] for r in newest}
        ranks = [h["rank"] for h in hits]
        assert ranks == sorted(ranks, reverse=True)
    finally:
        await pool.close()
        await conn.close()


async def test_reset_requires_snapshot(migrated_dsn: str) -> None:
    conn = await asyncpg.connect(migrated_dsn)
    try:
        with pytest.raises(snapshot.DataValidationError, match="пуст"):
            await snapshot.reset(conn)
    finally:
        await conn.close()


async def test_constraints_enforced(migrated_dsn: str) -> None:
    conn = await _seeded(migrated_dsn)
    try:
        with pytest.raises(asyncpg.UniqueViolationError):  # email без учёта регистра
            await conn.execute(
                "INSERT INTO users (email, password_hash) VALUES "
                "('USER5@bench.example.com', 'x')"
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("INSERT INTO reactions (post_id, user_id, kind) VALUES (1, 1, 'x')")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO comments (post_id, author_id, body, status) "
                "VALUES (999999, 1, 'b', 'approved')"
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO posts (author_id, category_id, title, body, status) "
                "VALUES (1, 1, '   ', 'b', 'approved')"
            )
    finally:
        await conn.close()


async def test_full_text_search_index(migrated_dsn: str) -> None:
    conn = await _seeded(migrated_dsn)
    try:
        n = await conn.fetchval(
            "SELECT count(*) FROM posts WHERE search @@ "
            "plainto_tsquery('russian', 'кардиология')"
        )
        assert n > 0
    finally:
        await conn.close()


async def test_health(migrated_dsn: str) -> None:
    conn = await asyncpg.connect(migrated_dsn)
    try:
        h = await manage.health(conn)
        assert h["ok"] and h["migration"] == "0004" and h["server_version"].startswith("16")
    finally:
        await conn.close()


async def test_health_reports_outdated_schema(pg_dsn: str) -> None:
    conn = await asyncpg.connect(pg_dsn)
    try:
        assert (await manage.health(conn))["ok"] is False  # миграции не применены
    finally:
        await conn.close()


async def test_backup_restore_roundtrip(
    migrated_dsn: str,
    pg_bin: str | None,
    tmp_path,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if pg_bin is None:
        pytest.skip("нет pg_dump/pg_restore")
    monkeypatch.setenv("PG_BIN", pg_bin)
    conn = await _seeded(migrated_dsn)
    try:
        before = await snapshot.data_state(conn, WINDOW)
        sums = await snapshot.snapshot_checksums(conn)
        post_md5 = await conn.fetchval(
            "SELECT md5(string_agg(md5(p::text), '' ORDER BY id)) "
            "FROM (SELECT id, author_id, title, body, status, "
            "created_at FROM posts) p"
        )
    finally:
        await conn.close()
    dump = tmp_path / "db.dump"
    info = manage.backup(migrated_dsn, dump)
    assert info["bytes"] > 0
    conn = await asyncpg.connect(migrated_dsn)
    try:  # повреждаем данные после копии
        await conn.execute("DELETE FROM reactions")
        await conn.execute("DELETE FROM seed_snapshot.comments")
    finally:
        await conn.close()
    manage.restore(migrated_dsn, dump)
    conn = await asyncpg.connect(migrated_dsn)
    try:
        after = await snapshot.data_state(conn, WINDOW)
        assert after.counts == before.counts
        assert await snapshot.snapshot_checksums(conn) == sums
        assert (
            await conn.fetchval(
                "SELECT md5(string_agg(md5(p::text), '' ORDER BY id)) "
                "FROM (SELECT id, author_id, title, body, status, created_at "
                "FROM posts) p"
            )
            == post_md5
        )
        assert (await manage.health(conn))["ok"]
    finally:
        await conn.close()


def test_restore_missing_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(manage.ManageError, match="не найден"):
        manage.restore("postgresql://u@localhost/db", tmp_path / "nope.dump")


def test_pg_connections_sampler_on_real_db(migrated_dsn: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Сэмплер pg_stat_activity — живой поток против PostgreSQL: пишет строки и корректно стоп."""
    import csv
    import time

    from experiments.orchestrator.samplers import PgConnectionsSampler

    s = PgConnectionsSampler(tmp_path / "pg.csv", migrated_dsn, step_s=0.2)
    s.start()
    time.sleep(1.0)
    s.stop()
    assert not s.is_alive()
    assert s.errors == []
    with (tmp_path / "pg.csv").open(encoding="utf-8") as f:
        assert next(csv.reader(f)) == ["t", "state", "n"]
