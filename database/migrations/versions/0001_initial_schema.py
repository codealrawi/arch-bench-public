"""Начальная схема медико-социальной платформы.

Revision ID: 0001
Revises:
Create Date: 2026-10-05

Таблицы: medical_categories, users, profiles, posts, comments, reactions,
moderation_records, refresh_tokens, audit_logs. Одна схема для всех архитектур.
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE medical_categories (
    id          SMALLSERIAL  PRIMARY KEY,
    slug        VARCHAR(64)  NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]+$'),
    name        VARCHAR(128) NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id            BIGSERIAL    PRIMARY KEY,
    email         VARCHAR(254) NOT NULL CHECK (position('@' IN email) > 1),
    password_hash VARCHAR(200) NOT NULL,
    role          VARCHAR(16)  NOT NULL DEFAULT 'patient'
                  CHECK (role IN ('patient', 'doctor', 'moderator', 'admin')),
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_users_email_lower ON users (lower(email));

CREATE TABLE profiles (
    user_id        BIGINT       PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    display_name   VARCHAR(100) NOT NULL CHECK (length(btrim(display_name)) > 0),
    bio            VARCHAR(2000) NOT NULL DEFAULT '',
    city           VARCHAR(100) NOT NULL DEFAULT '',
    birth_year     SMALLINT     CHECK (birth_year BETWEEN 1900 AND 2100),
    specialty_id   SMALLINT     REFERENCES medical_categories(id) ON DELETE SET NULL,
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE posts (
    id               BIGSERIAL    PRIMARY KEY,
    author_id        BIGINT       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category_id      SMALLINT     NOT NULL REFERENCES medical_categories(id),
    title            VARCHAR(300) NOT NULL CHECK (length(btrim(title)) > 0),
    body             TEXT         NOT NULL
                     CHECK (length(btrim(body)) > 0 AND length(body) <= 20000),
    status           VARCHAR(16)  NOT NULL CHECK (status IN ('approved', 'suspicious', 'blocked')),
    comments_count   INTEGER      NOT NULL DEFAULT 0 CHECK (comments_count >= 0),
    reactions_count  INTEGER      NOT NULL DEFAULT 0 CHECK (reactions_count >= 0),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    search           TSVECTOR     GENERATED ALWAYS AS (
                         to_tsvector('russian', title || ' ' || body)) STORED
);
CREATE INDEX ix_posts_feed ON posts (created_at DESC) WHERE status = 'approved';
CREATE INDEX ix_posts_author ON posts (author_id, created_at DESC);
CREATE INDEX ix_posts_category ON posts (category_id, created_at DESC) WHERE status = 'approved';
CREATE INDEX ix_posts_created ON posts (created_at);
CREATE INDEX ix_posts_search ON posts USING GIN (search);

CREATE TABLE comments (
    id          BIGSERIAL    PRIMARY KEY,
    post_id     BIGINT       NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    author_id   BIGINT       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body        VARCHAR(5000) NOT NULL CHECK (length(btrim(body)) > 0),
    status      VARCHAR(16)  NOT NULL CHECK (status IN ('approved', 'suspicious', 'blocked')),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX ix_comments_post ON comments (post_id, created_at);
CREATE INDEX ix_comments_created ON comments (created_at);

CREATE TABLE reactions (
    post_id     BIGINT       NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    user_id     BIGINT       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind        VARCHAR(16)  NOT NULL CHECK (kind IN ('like', 'support', 'useful')),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, user_id)
);
CREATE INDEX ix_reactions_user ON reactions (user_id);
CREATE INDEX ix_reactions_created ON reactions (created_at);

CREATE TABLE moderation_records (
    id           BIGSERIAL    PRIMARY KEY,
    target_type  VARCHAR(16)  NOT NULL CHECK (target_type IN ('post', 'comment')),
    target_id    BIGINT       NOT NULL,
    label        VARCHAR(16)  NOT NULL CHECK (label IN ('approved', 'suspicious', 'blocked')),
    level        SMALLINT     NOT NULL CHECK (level IN (1, 2)),
    confidence   REAL         NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    reasons      JSONB        NOT NULL DEFAULT '[]'::jsonb,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX ix_moderation_target ON moderation_records (target_type, target_id);
CREATE INDEX ix_moderation_created ON moderation_records (created_at);

CREATE TABLE refresh_tokens (
    id          BIGSERIAL   PRIMARY KEY,
    user_id     BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  CHAR(64)    NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (expires_at > created_at)
);
CREATE INDEX ix_refresh_user ON refresh_tokens (user_id);

CREATE TABLE audit_logs (
    id          BIGSERIAL   PRIMARY KEY,
    user_id     BIGINT      REFERENCES users(id) ON DELETE SET NULL,
    action      VARCHAR(64) NOT NULL,
    entity      VARCHAR(32) NOT NULL,
    entity_id   BIGINT,
    details     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_created ON audit_logs (created_at);
CREATE INDEX ix_audit_user ON audit_logs (user_id, created_at);
"""

DOWNGRADE = """
DROP TABLE IF EXISTS audit_logs;
DROP TABLE IF EXISTS refresh_tokens;
DROP TABLE IF EXISTS moderation_records;
DROP TABLE IF EXISTS reactions;
DROP TABLE IF EXISTS comments;
DROP TABLE IF EXISTS posts;
DROP TABLE IF EXISTS profiles;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS medical_categories;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
