"""Покрывающий индекс posts для статистики за окно (протокол v2.1, §14).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-05

Статистика группирует посты окна по дню/статусу и по категории. С индексом только по
created_at это seq scan по posts (в окне ~23 % строк). INCLUDE (status, category_id) даёт
index-only scan, если карта видимости заполнена (VACUUM после сброса, snapshot.reset).
Заменяет ix_posts_created.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_posts_created_cover ON posts (created_at) INCLUDE (status, category_id);"
    )
    op.execute("DROP INDEX ix_posts_created;")


def downgrade() -> None:
    op.execute("CREATE INDEX ix_posts_created ON posts (created_at);")
    op.execute("DROP INDEX ix_posts_created_cover;")
