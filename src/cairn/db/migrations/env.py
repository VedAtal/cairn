"""Alembic environment. Migrations run on a connection handed in by cairn.db.migrate (or, for autogenerate during
development, on CAIRN_DB__URL)."""

from __future__ import annotations

import os
from typing import Any

from alembic import context

from cairn.db.engine import make_sync_engine
from cairn.db.schema import metadata


def _include(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    # FTS5 shadow tables and expression indexes are created by hand in migrations; autogenerate must ignore them.
    return not (name or "").startswith(("entries_fts", "nodes_fts", "ix_search_"))


def _run(connection: Any) -> None:
    context.configure(connection=connection, target_metadata=metadata, include_object=_include, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    with make_sync_engine(os.environ["CAIRN_DB__URL"]).connect() as conn:
        _run(conn)
        conn.commit()
