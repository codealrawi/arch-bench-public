"""Окружение Alembic. Миграции написаны на SQL (op.execute) — без ORM-моделей.

DATABASE_URL — тот же DSN, что у приложения (postgresql://...); для Alembic он приводится к
драйверу psycopg 3 (postgresql+psycopg://...).
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

from database.migrations.env_url import to_sqlalchemy_url


def sqlalchemy_url() -> str:
    url = context.config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL обязателен для миграций")
    return to_sqlalchemy_url(url)


def run_migrations_offline() -> None:
    context.configure(url=sqlalchemy_url(), literal_binds=True, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(sqlalchemy_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transactional_ddl=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
