"""Эталон начального состояния (seed_snapshot), сброс рабочих таблиц и проверка данных.

- ``load_snapshot`` — один раз: генерация (generator.py) → COPY в seed_snapshot.* + контрольные
  суммы в seed_snapshot.meta;
- ``reset`` — перед каждой ступенью (протокол P-19): рабочие таблицы := эталон, даты
  отсчитываются от момента сброса (``now()`` транзакции — одинаково для всех строк);
- ``verify`` — проверки P-18; при нарушении — DataValidationError (прогон не стартует);
- ``data_state`` — состояние для meta.json до и после ступени.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

import asyncpg

from apps.shared.moderation import ContentModerator
from database.seed.generator import CATEGORIES, SeedData, SeedParams, generate

WORK_TABLES = (
    "audit_logs",
    "refresh_tokens",
    "moderation_records",
    "reactions",
    "comments",
    "posts",
    "profiles",
    "users",
    "medical_categories",
)
SNAPSHOT_TABLES = (
    "categories",
    "users",
    "profiles",
    "posts",
    "comments",
    "reactions",
    "moderation_records",
)
SEQUENCES = {
    "users": "id",
    "posts": "id",
    "comments": "id",
    "moderation_records": "id",
    "medical_categories": "id",
    "refresh_tokens": "id",
    "audit_logs": "id",
}
_ORDER = {
    "categories": "id",
    "users": "id",
    "profiles": "user_id",
    "posts": "id",
    "comments": "id",
    "reactions": "post_id, user_id",
    "moderation_records": "id",
}

RESET_SQL = f"""
TRUNCATE {", ".join(WORK_TABLES)} RESTART IDENTITY CASCADE;
INSERT INTO medical_categories (id, slug, name, created_at)
    SELECT id, slug, name, now() FROM seed_snapshot.categories;
INSERT INTO users (id, email, password_hash, role, is_active, created_at, updated_at)
    SELECT id, email, password_hash, role, TRUE, now() - make_interval(secs => age_seconds),
           now() - make_interval(secs => age_seconds)
    FROM seed_snapshot.users;
INSERT INTO profiles (user_id, display_name, bio, city, birth_year, specialty_id, updated_at)
    SELECT s.user_id, s.display_name, s.bio, s.city, s.birth_year, s.specialty_id,
           now() - make_interval(secs => u.age_seconds)
    FROM seed_snapshot.profiles s JOIN seed_snapshot.users u ON u.id = s.user_id;
INSERT INTO posts (id, author_id, category_id, title, body, status, comments_count,
                   reactions_count, created_at, updated_at)
    SELECT id, author_id, category_id, title, body, status, comments_count, reactions_count,
           now() - make_interval(secs => age_seconds), now() - make_interval(secs => age_seconds)
    FROM seed_snapshot.posts;
INSERT INTO comments (id, post_id, author_id, body, status, created_at)
    SELECT id, post_id, author_id, body, status, now() - make_interval(secs => age_seconds)
    FROM seed_snapshot.comments;
INSERT INTO reactions (post_id, user_id, kind, created_at)
    SELECT post_id, user_id, kind, now() - make_interval(secs => age_seconds)
    FROM seed_snapshot.reactions;
INSERT INTO moderation_records (id, target_type, target_id, label, level, confidence, reasons,
                                created_at)
    SELECT id, target_type, target_id, label, level, confidence, reasons,
           now() - make_interval(secs => age_seconds)
    FROM seed_snapshot.moderation_records;
""" + "".join(
    f"SELECT setval(pg_get_serial_sequence('{t}', '{c}'), "
    f"(SELECT coalesce(max({c}), 0) + 1 FROM {t}), false);\n"
    for t, c in SEQUENCES.items()
)


class DataValidationError(RuntimeError):
    """Данные БД не прошли проверку — прогон запускать нельзя."""


def params_from_config(cfg: Any) -> SeedParams:
    d = cfg.data
    return SeedParams(
        seed=d.seed,
        users=d.users,
        admins=d.admins,
        moderators=d.moderators,
        posts=d.posts,
        comments=d.comments,
        reactions=d.reactions,
        doctor_share=d.doctor_share,
        user_age_days=d.user_age_days,
        window_days=d.created_at_window_days,
        suspicious_share=d.suspicious_share,
        body_words=(d.body_words_min, d.body_words_max),
        comment_words=(d.comment_words_min, d.comment_words_max),
        reaction_kinds=dict(d.reaction_kinds),
        password_template=cfg.auth.password_template,
        pbkdf2_iterations=cfg.auth.pbkdf2_iterations,
    )


async def _copy(
    conn: asyncpg.Connection, table: str, columns: list[str], rows: list[tuple[Any, ...]]
) -> None:
    for i in range(0, len(rows), 50_000):
        await conn.copy_records_to_table(
            table, schema_name="seed_snapshot", records=rows[i : i + 50_000], columns=columns
        )


async def write_snapshot(conn: asyncpg.Connection, data: SeedData, p: SeedParams) -> None:
    async with conn.transaction():
        await conn.execute(
            "TRUNCATE " + ", ".join(f"seed_snapshot.{t}" for t in (*SNAPSHOT_TABLES, "meta"))
        )
        await _copy(
            conn,
            "categories",
            ["id", "slug", "name"],
            [(i + 1, s, n) for i, (s, n) in enumerate(CATEGORIES)],
        )
        await _copy(
            conn,
            "users",
            ["id", "email", "password_hash", "role", "age_seconds"],
            [(u.id, u.email, u.password_hash, u.role, u.age_seconds) for u in data.users],
        )
        await _copy(
            conn,
            "profiles",
            ["user_id", "display_name", "bio", "city", "birth_year", "specialty_id"],
            [
                (r.user_id, r.display_name, r.bio, r.city, r.birth_year, r.specialty_id)
                for r in data.profiles
            ],
        )
        await _copy(
            conn,
            "posts",
            [
                "id",
                "author_id",
                "category_id",
                "title",
                "body",
                "status",
                "comments_count",
                "reactions_count",
                "age_seconds",
            ],
            [
                (
                    r.id,
                    r.author_id,
                    r.category_id,
                    r.title,
                    r.body,
                    r.status,
                    r.comments_count,
                    r.reactions_count,
                    r.age_seconds,
                )
                for r in data.posts
            ],
        )
        await _copy(
            conn,
            "comments",
            ["id", "post_id", "author_id", "body", "status", "age_seconds"],
            [
                (r.id, r.post_id, r.author_id, r.body, r.status, r.age_seconds)
                for r in data.comments
            ],
        )
        await _copy(
            conn,
            "reactions",
            ["post_id", "user_id", "kind", "age_seconds"],
            [(r.post_id, r.user_id, r.kind, r.age_seconds) for r in data.reactions],
        )
        await _copy(
            conn,
            "moderation_records",
            [
                "id",
                "target_type",
                "target_id",
                "label",
                "level",
                "confidence",
                "reasons",
                "age_seconds",
            ],
            [
                (
                    r.id,
                    r.target_type,
                    r.target_id,
                    r.label,
                    r.level,
                    r.confidence,
                    r.reasons,
                    r.age_seconds,
                )
                for r in data.moderation
            ],
        )
        sums = await snapshot_checksums(conn)
        meta = {
            "seed": str(p.seed),
            **{f"count.{k}": str(v) for k, v in (await snapshot_counts(conn)).items()},
            **{f"md5.{k}": v for k, v in sums.items()},
        }
        await conn.executemany(
            "INSERT INTO seed_snapshot.meta (key, value) VALUES ($1, $2)", sorted(meta.items())
        )


async def load_snapshot(
    conn: asyncpg.Connection, p: SeedParams, moderator: ContentModerator | None = None
) -> dict[str, Any]:
    t0 = time.perf_counter()
    data = generate(p, moderator or ContentModerator().train())
    t1 = time.perf_counter()
    await write_snapshot(conn, data, p)
    return {"generate_s": round(t1 - t0, 2), "write_s": round(time.perf_counter() - t1, 2)}


async def snapshot_counts(conn: asyncpg.Connection) -> dict[str, int]:
    return {
        t: int(await conn.fetchval(f"SELECT count(*) FROM seed_snapshot.{t}"))
        for t in SNAPSHOT_TABLES
    }


async def snapshot_checksums(conn: asyncpg.Connection) -> dict[str, str]:
    # row_to_json, а не s::text: вывод записи берёт значения в кавычки по isspace() локали
    # сервера (в cp1252 байт 0xA0 из «Р» = D0 A0 — «пробел»), и сумма зависела бы от локали.
    out = {}
    for t in SNAPSHOT_TABLES:
        out[t] = str(
            await conn.fetchval(
                "SELECT md5(coalesce(string_agg(md5(row_to_json(s)::text), '' "
                f"ORDER BY {_ORDER[t]}), '')) "
                f"FROM seed_snapshot.{t} s"
            )
        )
    return out


async def snapshot_meta(conn: asyncpg.Connection) -> dict[str, str]:
    return {
        r["key"]: r["value"]
        for r in await conn.fetch("SELECT key, value FROM seed_snapshot.meta ORDER BY key")
    }


async def reset(conn: asyncpg.Connection) -> float:
    """Сброс рабочих таблиц к эталону. Возвращает длительность, с."""
    if not await conn.fetchval("SELECT count(*) FROM seed_snapshot.meta"):
        raise DataValidationError("эталон seed_snapshot пуст: выполните `seed` до `reset`")
    t0 = time.perf_counter()
    async with conn.transaction():
        await conn.execute(RESET_SQL)
    # VACUUM, а не только ANALYZE (протокол v2.1): после TRUNCATE + INSERT карта видимости пуста —
    # без неё нет index-only scan, и autovacuum по вставкам запускался бы в окне измерения.
    # Вне транзакции (VACUUM в ней запрещён).
    await conn.execute("VACUUM (ANALYZE)")
    return time.perf_counter() - t0


@dataclass(frozen=True)
class DataState:
    counts: dict[str, int]
    posts_by_status: dict[str, int]
    posts_in_stats_window: int
    snapshot_md5: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def data_state(conn: asyncpg.Connection, stats_window_days: int) -> DataState:
    counts = {t: int(await conn.fetchval(f"SELECT count(*) FROM {t}")) for t in WORK_TABLES}
    by_status = {
        r["status"]: int(r["n"])
        for r in await conn.fetch(
            "SELECT status, count(*) AS n FROM posts GROUP BY status ORDER BY status"
        )
    }
    window = int(
        await conn.fetchval(
            "SELECT count(*) FROM posts WHERE created_at >= now() - make_interval(days => $1)",
            stats_window_days,
        )
    )
    meta = await snapshot_meta(conn)
    return DataState(
        counts,
        by_status,
        window,
        {k.removeprefix("md5."): v for k, v in meta.items() if k.startswith("md5.")},
    )


async def verify(conn: asyncpg.Connection, stats_window_days: int) -> DataState:
    """Проверки P-18 сразу после reset: рабочие таблицы ровно соответствуют эталону."""
    state = await data_state(conn, stats_window_days)
    snap = await snapshot_counts(conn)
    expected = {
        "medical_categories": snap["categories"],
        "users": snap["users"],
        "profiles": snap["profiles"],
        "posts": snap["posts"],
        "comments": snap["comments"],
        "reactions": snap["reactions"],
        "moderation_records": snap["moderation_records"],
        "refresh_tokens": 0,
        "audit_logs": 0,
    }
    problems = [
        f"{t}={state.counts[t]}, по эталону {n}"
        for t, n in expected.items()
        if state.counts[t] != n
    ]
    sums = await snapshot_checksums(conn)
    if sums != state.snapshot_md5:
        problems.append("контрольные суммы эталона изменились после загрузки")
    exp_window = int(
        await conn.fetchval(
            "SELECT count(*) FROM seed_snapshot.posts WHERE age_seconds < $1 * 86400",
            stats_window_days,
        )
    )
    if not 0 <= exp_window - state.posts_in_stats_window <= 100:
        problems.append(f"в окне /stats {state.posts_in_stats_window}, по эталону {exp_window}")
    # малые БД — построчная проверка; полный объём — сверка сумм (построчная заняла бы минуты)
    if state.counts["posts"] <= 1000:
        bad_counts = await conn.fetchval(
            "SELECT count(*) FROM posts p WHERE p.comments_count <> "
            "(SELECT count(*) FROM comments c WHERE c.post_id = p.id AND c.status = 'approved')"
        )
    else:
        bad_counts = await conn.fetchval(
            "SELECT (SELECT sum(comments_count) FROM posts) - "
            "(SELECT count(*) FROM comments WHERE status = 'approved')"
        )
    if bad_counts:
        problems.append("posts.comments_count не согласован с комментариями")
    if problems:
        raise DataValidationError("; ".join(problems))
    return state
