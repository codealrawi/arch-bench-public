"""Схема seed_snapshot — эталон начального состояния для сброса БД перед ступенью.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-05

Эталон хранит seed без абсолютных дат (age_seconds = «сколько секунд назад»). Сброс
(database/seed/reset.py) переносит эталон в рабочие таблицы с created_at = now() − age,
поэтому окно /stats (7 дней) после каждого сброса содержит одни и те же записи.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UPGRADE = """
CREATE SCHEMA seed_snapshot;

CREATE TABLE seed_snapshot.meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
CREATE TABLE seed_snapshot.categories (
    id SMALLINT PRIMARY KEY, slug VARCHAR(64) NOT NULL, name VARCHAR(128) NOT NULL
);
CREATE TABLE seed_snapshot.users (
    id BIGINT PRIMARY KEY, email VARCHAR(254) NOT NULL, password_hash VARCHAR(200) NOT NULL,
    role VARCHAR(16) NOT NULL, age_seconds BIGINT NOT NULL CHECK (age_seconds >= 0)
);
CREATE TABLE seed_snapshot.profiles (
    user_id BIGINT PRIMARY KEY, display_name VARCHAR(100) NOT NULL, bio VARCHAR(2000) NOT NULL,
    city VARCHAR(100) NOT NULL, birth_year SMALLINT, specialty_id SMALLINT
);
CREATE TABLE seed_snapshot.posts (
    id BIGINT PRIMARY KEY, author_id BIGINT NOT NULL, category_id SMALLINT NOT NULL,
    title VARCHAR(300) NOT NULL, body TEXT NOT NULL, status VARCHAR(16) NOT NULL,
    comments_count INTEGER NOT NULL, reactions_count INTEGER NOT NULL,
    age_seconds BIGINT NOT NULL CHECK (age_seconds >= 0)
);
CREATE TABLE seed_snapshot.comments (
    id BIGINT PRIMARY KEY, post_id BIGINT NOT NULL, author_id BIGINT NOT NULL,
    body VARCHAR(5000) NOT NULL, status VARCHAR(16) NOT NULL,
    age_seconds BIGINT NOT NULL CHECK (age_seconds >= 0)
);
CREATE TABLE seed_snapshot.reactions (
    post_id BIGINT NOT NULL, user_id BIGINT NOT NULL, kind VARCHAR(16) NOT NULL,
    age_seconds BIGINT NOT NULL CHECK (age_seconds >= 0), PRIMARY KEY (post_id, user_id)
);
CREATE TABLE seed_snapshot.moderation_records (
    id BIGINT PRIMARY KEY, target_type VARCHAR(16) NOT NULL, target_id BIGINT NOT NULL,
    label VARCHAR(16) NOT NULL, level SMALLINT NOT NULL, confidence REAL NOT NULL,
    reasons JSONB NOT NULL, age_seconds BIGINT NOT NULL CHECK (age_seconds >= 0)
);
"""

DOWNGRADE = "DROP SCHEMA IF EXISTS seed_snapshot CASCADE;"


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
