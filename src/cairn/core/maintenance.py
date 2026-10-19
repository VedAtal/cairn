from __future__ import annotations

import asyncio
from datetime import timedelta

import structlog
from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..db.schema import audit_log, change_log, credentials, meta, write_ops
from .write import now

EVERY, TICK = timedelta(hours=1), 300.0
LOG_RETENTION, OP_RETENTION = timedelta(days=400), timedelta(days=1)
log = structlog.get_logger("cairn.maintenance")


async def claim(conn: AsyncConnection) -> bool:
    insert = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
    await conn.execute(insert(meta).values(key="prune_due", value="").on_conflict_do_nothing())
    t = now()
    # Compare-and-set, so one replica prunes per hour.
    claimed = await conn.execute(update(meta).where(meta.c.key == "prune_due",
                                                    meta.c.value <= t.isoformat(timespec="seconds"))
                                 .values(value=(t + EVERY).isoformat(timespec="seconds")))
    return claimed.rowcount == 1


async def prune(conn: AsyncConnection) -> None:
    t = now()
    await conn.execute(delete(credentials).where(credentials.c.kind != "api_key", credentials.c.expires_at < t))
    await conn.execute(delete(write_ops).where(write_ops.c.at < t - OP_RETENTION))
    await conn.execute(delete(change_log).where(change_log.c.at < t - LOG_RETENTION))
    await conn.execute(delete(audit_log).where(audit_log.c.at < t - LOG_RETENTION))


async def run(engine: AsyncEngine) -> None:
    while True:
        try:
            async with engine.begin() as conn:
                if await claim(conn):
                    await prune(conn)
        except Exception as exc:  # noqa: BLE001 - retried next tick
            log.warning("maintenance failed", error=str(exc)[:300])
        await asyncio.sleep(TICK)
