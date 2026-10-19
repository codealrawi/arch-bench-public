"""Управление БД. DSN — из DATABASE_URL или --dsn. Все команды печатают JSON.

python -m database.manage migrate up|down|status
python -m database.manage seed          # генерация эталона seed_snapshot + reset
python -m database.manage reset         # рабочие таблицы := эталон (перед каждой ступенью)
python -m database.manage verify        # проверки P-18
python -m database.manage state         # состояние для meta.json
python -m database.manage health        # SELECT 1 + версия миграций = head
python -m database.manage backup  FILE  # pg_dump -Fc
python -m database.manage restore FILE  # pg_restore --clean --if-exists
python -m database.manage load-texts    # тексты операций записи для k6
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import asyncpg
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from database.seed import snapshot
from database.seed.generator import generate_load_texts
from experiments.config import DEFAULT_CONFIG_PATH, K6_DIR, load_bench_config

ALEMBIC_INI = Path(__file__).resolve().parent / "alembic.ini"
LOAD_TEXTS_COUNT = 2000


class ManageError(RuntimeError):
    pass


def resolve_dsn(dsn: str | None) -> str:
    value = dsn or os.environ.get("DATABASE_URL")
    if not value:
        raise ManageError("нужен --dsn или DATABASE_URL")
    return value


def alembic_config(dsn: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))
    return cfg


def migrate(dsn: str, action: str) -> dict[str, Any]:
    cfg = alembic_config(dsn)
    if action == "up":
        command.upgrade(cfg, "head")
    elif action == "down":
        command.downgrade(cfg, "base")
    elif action != "status":
        raise ManageError(f"неизвестное действие migrate {action}")
    return {"current": current_revision(dsn), "head": head_revision()}


def head_revision() -> str | None:
    heads = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_heads()
    return heads[0] if heads else None


def current_revision(dsn: str) -> str | None:
    from sqlalchemy import create_engine

    from database.migrations.env_url import to_sqlalchemy_url

    engine = create_engine(to_sqlalchemy_url(dsn))
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


async def _with_conn(dsn: str, fn: Any) -> Any:
    conn = await asyncpg.connect(dsn)
    try:
        return await fn(conn)
    finally:
        await conn.close()


def _pg_tool(name: str) -> str:
    pg_bin = os.environ.get("PG_BIN")
    candidate = Path(pg_bin) / name if pg_bin else None
    if candidate and (candidate.exists() or candidate.with_suffix(".exe").exists()):
        return str(candidate)
    found = shutil.which(name)
    if not found:
        raise ManageError(f"{name} не найден (PATH или PG_BIN)")
    return found


def _pg_env(dsn: str) -> tuple[list[str], dict[str, str]]:
    """Параметры подключения для pg_dump/pg_restore без пароля в командной строке."""
    u = urlsplit(dsn)
    env = {**os.environ}
    if u.password:
        env["PGPASSWORD"] = u.password
    args = [
        "-h",
        u.hostname or "localhost",
        "-p",
        str(u.port or 5432),
        "-U",
        u.username or "postgres",
        "-d",
        (u.path or "/postgres").lstrip("/"),
    ]
    return args, env


def backup(dsn: str, path: Path) -> dict[str, Any]:
    args, env = _pg_env(dsn)
    path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    subprocess.run(
        [_pg_tool("pg_dump"), "-Fc", "--no-owner", "-f", str(path), *args], env=env, check=True
    )
    return {
        "file": str(path),
        "bytes": path.stat().st_size,
        "seconds": round(time.perf_counter() - t0, 2),
    }


def restore(dsn: str, path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ManageError(f"файл резервной копии не найден: {path}")
    args, env = _pg_env(dsn)
    t0 = time.perf_counter()
    subprocess.run(
        [
            _pg_tool("pg_restore"),
            "--clean",
            "--if-exists",
            "--no-owner",
            "--single-transaction",
            *args,
            str(path),
        ],
        env=env,
        check=True,
    )
    return {"file": str(path), "seconds": round(time.perf_counter() - t0, 2)}


async def health(conn: asyncpg.Connection) -> dict[str, Any]:
    t0 = time.perf_counter()
    ok = await conn.fetchval("SELECT 1") == 1
    latency_ms = (time.perf_counter() - t0) * 1000
    has_version = await conn.fetchval("SELECT to_regclass('public.alembic_version') IS NOT NULL")
    current = (
        await conn.fetchval("SELECT version_num FROM alembic_version") if has_version else None
    )
    return {
        "ok": ok and current == head_revision(),
        "select1_ms": round(latency_ms, 2),
        "migration": current,
        "head": head_revision(),
        "server_version": await conn.fetchval("SHOW server_version"),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_bench_config(args.config)
    if args.command == "load-texts":
        d = cfg.data
        texts = generate_load_texts(
            d.seed,
            LOAD_TEXTS_COUNT,
            d.suspicious_share,
            (d.body_words_min, d.body_words_max),
            (d.comment_words_min, d.comment_words_max),
        )
        out = Path(args.target or (K6_DIR / "write_texts.json"))
        out.write_text(json.dumps(texts, ensure_ascii=False), encoding="utf-8")
        return {"out": str(out), "count": LOAD_TEXTS_COUNT}
    dsn = resolve_dsn(args.dsn)
    if args.command == "migrate":
        return migrate(dsn, args.target or "status")
    if args.command == "backup":
        return backup(dsn, Path(args.target or "backups/arch-bench.dump"))
    if args.command == "restore":
        return restore(dsn, Path(args.target or "backups/arch-bench.dump"))
    window = cfg.data.stats_window_days

    async def body(conn: asyncpg.Connection) -> dict[str, Any]:
        if args.command == "health":
            return await health(conn)
        if args.command == "seed":
            timings = await snapshot.load_snapshot(conn, snapshot.params_from_config(cfg))
            timings["reset_s"] = round(await snapshot.reset(conn), 2)
            return {**timings, "state": (await snapshot.verify(conn, window)).to_dict()}
        if args.command == "reset":
            seconds = await snapshot.reset(conn)
            return {
                "reset_s": round(seconds, 2),
                "state": (await snapshot.verify(conn, window)).to_dict(),
            }
        if args.command == "verify":
            return (await snapshot.verify(conn, window)).to_dict()
        if args.command == "state":
            return (await snapshot.data_state(conn, window)).to_dict()
        raise ManageError(f"неизвестная команда {args.command}")

    result: dict[str, Any] = asyncio.run(_with_conn(dsn, body))
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="database.manage")
    ap.add_argument(
        "command",
        choices=[
            "migrate",
            "seed",
            "reset",
            "verify",
            "state",
            "health",
            "backup",
            "restore",
            "load-texts",
        ],
    )
    ap.add_argument("target", nargs="?", help="up|down|status для migrate; файл для backup/restore")
    ap.add_argument("--dsn")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    args = ap.parse_args(argv)
    try:
        result = run(args)
    except snapshot.DataValidationError as exc:
        print(json.dumps({"command": args.command, "error": str(exc)}, ensure_ascii=False))
        return 2
    except (ManageError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"command": args.command, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"command": args.command, **result}, ensure_ascii=False, default=str))
    if args.command == "health" and not result.get("ok"):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
