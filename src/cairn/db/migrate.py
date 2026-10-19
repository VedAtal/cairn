"""Run Alembic migrations at startup. On Postgres an advisory lock lets exactly one replica migrate."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from .engine import make_sync_engine

LOCK_KEY = 0x0CA1_2B0B  # arbitrary, stable advisory-lock key


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return cfg


def upgrade(url: str) -> None:
    """Upgrade the database at `url` to head. Blocking; call via a thread from async code."""
    engine = make_sync_engine(url)
    try:
        with engine.connect() as conn:
            postgres = conn.dialect.name == "postgresql"
            if postgres:
                conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_KEY})
                conn.commit()
            try:
                cfg = alembic_config()
                cfg.attributes["connection"] = conn
                command.upgrade(cfg, "head")
                conn.commit()
            finally:
                if postgres:
                    conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
                    conn.commit()
    finally:
        engine.dispose()
