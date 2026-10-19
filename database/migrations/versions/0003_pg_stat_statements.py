"""pg_stat_statements: задержка запросов БД для мониторинга (postgres_exporter).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-05

Создаётся, только если расширение доступно на сервере (в образе postgres:16 — да). Для сбора
статистики нужен shared_preload_libraries = 'pg_stat_statements' (postgresql.conf из bench.yaml).
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

UPGRADE = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_stat_statements') THEN
        CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS pg_stat_statements;")
