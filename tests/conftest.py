"""Общие фикстуры тестов.

PostgreSQL для integration-тестов: ARCH_BENCH_TEST_DSN (например, контейнер postgres:16),
иначе встроенный сервер pgserver (PostgreSQL 16). Это тестовая инфраструктура, а не среда
эксперимента.
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

TEST_JWT_SECRET = "test-secret-" + "x" * 40  # TEST DATA ONLY


@pytest.fixture(scope="session")
def pg_admin_dsn() -> Iterator[str]:
    dsn = os.environ.get("ARCH_BENCH_TEST_DSN")
    if dsn:
        yield dsn
        return
    try:
        import pgserver
    except ImportError:
        pytest.skip("нет PostgreSQL: задайте ARCH_BENCH_TEST_DSN или установите pgserver")
    tmp = tempfile.mkdtemp(prefix="archbench-pg-")
    server = pgserver.get_server(tmp, cleanup_mode="stop")
    yield server.get_uri()
    server.cleanup()


@pytest.fixture(scope="session")
def pg_bin() -> str | None:
    """Каталог pg_dump/pg_restore: PG_BIN или встроенный pgserver."""
    if os.environ.get("PG_BIN"):
        return os.environ["PG_BIN"]
    try:
        import pgserver
    except ImportError:
        return None
    d = Path(pgserver.__file__).parent / "pginstall" / "bin"
    return str(d) if d.is_dir() else None


@pytest.fixture
def migrated_dsn(pg_dsn: str) -> str:
    """Чистая БД с применёнными миграциями (alembic upgrade head)."""
    from database.manage import migrate

    migrate(pg_dsn, "up")
    return pg_dsn


@pytest.fixture
async def seeded_dsn(migrated_dsn: str) -> str:
    """Миграции + уменьшенный seed (TEST DATA ONLY) + сброс рабочих таблиц к эталону."""
    import asyncpg

    from database.seed import snapshot
    from tests.fixtures.seed_params import small

    conn = await asyncpg.connect(migrated_dsn)
    try:
        await snapshot.load_snapshot(conn, small())
        await snapshot.reset(conn)
    finally:
        await conn.close()
    return migrated_dsn


@pytest.fixture
def monolith(seeded_dsn: str) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from apps.monolith.app import create_app
    from tests.fixtures.app_env import settings

    with TestClient(create_app(settings(seeded_dsn, SERVICE_NAME="monolith"))) as c:
        yield c


@pytest.fixture
async def pg_dsn(pg_admin_dsn: str) -> AsyncIterator[str]:
    """Отдельная чистая БД на каждый тест."""
    import asyncpg

    name = "t_" + uuid.uuid4().hex[:12]
    admin = await asyncpg.connect(pg_admin_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    base, _, _ = pg_admin_dsn.rpartition("/")
    yield f"{base}/{name}"
    admin = await asyncpg.connect(pg_admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await admin.close()
