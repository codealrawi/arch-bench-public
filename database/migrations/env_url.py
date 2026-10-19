"""Приведение DSN приложения (postgresql://) к URL SQLAlchemy с драйвером psycopg 3."""

from __future__ import annotations


def to_sqlalchemy_url(url: str) -> str:
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url
