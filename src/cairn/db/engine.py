"""Database engines. Postgres via psycopg 3; SQLite via aiosqlite on a single connection."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def dialect_url(url: str, *, sync: bool = False) -> str:
    """Normalize `postgresql://` / `sqlite:///` URLs to the drivers Cairn uses."""
    for prefix, async_driver, sync_driver in (
        ("postgresql://", "postgresql+psycopg://", "postgresql+psycopg://"),
        ("postgres://", "postgresql+psycopg://", "postgresql+psycopg://"),
        ("sqlite:///", "sqlite+aiosqlite:///", "sqlite:///"),
    ):
        if url.startswith(prefix):
            return (sync_driver if sync else async_driver) + url[len(prefix) :]
    return url


def _sqlite_pragmas(dbapi_conn: Any, _: Any) -> None:
    cur = dbapi_conn.cursor()
    for pragma in ("journal_mode=WAL", "busy_timeout=5000", "foreign_keys=ON", "synchronous=NORMAL"):
        cur.execute(f"PRAGMA {pragma}")
    cur.close()


def make_engine(url: str) -> AsyncEngine:
    url = dialect_url(url)
    if url.startswith("sqlite"):
        # One connection: the local server is the file's only user, and serializing access avoids
        # concurrent checkpoints (the trigger for SQLite's WAL-reset bug in older builds).
        engine = create_async_engine(url, pool_size=1, max_overflow=0)
        event.listen(engine.sync_engine, "connect", _sqlite_pragmas)
        return engine
    # Prepared statements off so transaction-mode poolers (PgBouncer, Neon, Supavisor) work.
    return create_async_engine(url, pool_pre_ping=True, connect_args={"prepare_threshold": None})


def make_sync_engine(url: str) -> Engine:
    url = dialect_url(url, sync=True)
    engine = create_engine(url)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _sqlite_pragmas)
    return engine
