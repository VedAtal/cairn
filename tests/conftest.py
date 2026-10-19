"""Shared fixtures: fresh stores on SQLite (and on Postgres when CAIRN_TEST_PG_URL is set), plus helpers that build
users, teams, projects and content through the real admin and write paths."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import make_url

from cairn.core import admin
from cairn.core.access import AccessError, NotFound, Principal
from cairn.core.refs import RefError
from cairn.core.store import SqlStore
from cairn.core.write import WriteError
from cairn.db import migrate
from cairn.db.engine import make_engine
from cairn.models import (
    AdminRequest,
    EntryFields,
    GetRequest,
    HitOut,
    InjectGroup,
    Item,
    MapRequest,
    MapResponse,
    NodeFields,
    SearchRequest,
    WriteRequest,
    WriteResponse,
)

ALIAS = "acme"
SETUP_TOKEN = "test-setup-token"


@dataclass
class World:
    """A store and its instance admin (`root`), with shortcuts over the store's public operations."""

    store: SqlStore
    root: Principal

    async def admin(self, p: Principal, op: str, **args: Any) -> dict[str, Any]:
        return (await self.store.admin(p, AdminRequest(op=op, args=args))).result

    async def principal(self, key: str) -> Principal:
        async with self.store.engine.connect() as conn:
            p = await admin.principal_for_key(conn, key)
        assert p is not None, "key rejected"
        return p

    async def user(self, email: str, team: str | None = None, role: str = "member") -> Principal:
        key = (await self.admin(self.root, "create_user", email=email))["api_key"]
        if team:
            await self.admin(self.root, "set_team_member", team=team, email=email, role=role)
        return await self.principal(key)

    async def project(self, team: str, slug: str, members: dict[str, str] | None = None) -> str:
        ref: str = (await self.admin(self.root, "create_project", team=team, slug=slug))["project"]
        for email, role in (members or {}).items():
            await self.admin(self.root, "set_project_member", project=ref, email=email, role=role)
        return ref

    async def write(self, p: Principal, op: str, **fields: Any) -> WriteResponse:
        return await self.store.write(p, WriteRequest(op=op, **fields))

    async def node(self, p: Principal, parent: str, slug: str, title: str = "", summary: str = "",
                   keywords: list[str] | None = None) -> str:
        """Create a node under `parent`, a project ref (top level) or a node ref."""
        where = {"project" if ":p" in parent else "node": parent}
        fields = NodeFields(slug=slug, title=title or slug, summary=summary, keywords=keywords or [])
        ref = (await self.write(p, "create_node", **where, node_fields=fields)).ref
        assert ref
        return ref

    async def add(self, p: Principal, node: str, title: str, body: str = "", type: str = "general",
                  keywords: list[str] | None = None, distinct_from: list[str] | None = None) -> str:
        entry = EntryFields(type=type, title=title, body=body, keywords=keywords or [])
        ref = (await self.write(p, "add", node=node, entry=entry, distinct_from=distinct_from or [])).ref
        assert ref
        return ref

    async def search(self, p: Principal, *concepts: str, **kw: Any) -> list[HitOut]:
        return (await self.store.search(p, SearchRequest(concepts=list(concepts), **kw))).hits

    async def get(self, p: Principal, *refs: str, history: bool = False) -> list[Item]:
        return (await self.store.get(p, GetRequest(refs=list(refs), history=history))).items

    async def map(self, p: Principal, **kw: Any) -> MapResponse:
        return await self.store.map(p, MapRequest(**kw))

    async def inject(self, p: Principal) -> list[InjectGroup]:
        return await self.store.inject(p)


async def outcome(call: Awaitable[Any]) -> str:
    """'ok', or the error code the call failed with ('not_found' for NotFound), for table-driven tests."""
    try:
        await call
    except (WriteError, AccessError) as exc:
        return exc.code
    except NotFound:
        return "not_found"
    except RefError:
        return "bad_ref"
    return "ok"


@asynccontextmanager
async def open_world(url: str) -> AsyncIterator[World]:
    """Migrate the database at `url`, then bootstrap its instance admin through the one-time setup token."""
    await asyncio.to_thread(migrate.upgrade, url)
    engine = make_engine(url)
    try:
        async with engine.begin() as conn:
            await admin.register_setup_token(conn, SETUP_TOKEN)
            key = await admin.bootstrap(conn, SETUP_TOKEN, "root@acme.dev", "Root")
            root = await admin.principal_for_key(conn, key)
        assert root is not None
        yield World(SqlStore(engine, ALIAS), root)
    finally:
        await engine.dispose()


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path / 'cairn.db'}"


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    async with open_world(sqlite_url(tmp_path)) as w:
        yield w


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """A uniquely named Postgres database for this test session, dropped at the end."""
    base = os.environ.get("CAIRN_TEST_PG_URL")
    if not base:
        pytest.skip("CAIRN_TEST_PG_URL is not set")
    name = f"cairn_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield make_url(base).set(database=name).render_as_string(hide_password=False)
    finally:
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@dataclass
class Org:
    """Team `eng` with project `platform` (one node), and one user per role."""

    w: World
    admin: Principal  # team admin
    writer: Principal  # writes `platform`
    reader: Principal  # reads `platform`
    member: Principal  # in the team, in zero projects
    project: str
    node: str


@pytest.fixture
async def org(world: World) -> Org:
    await world.admin(world.root, "create_team", slug="eng", name="Engineering")
    ada = await world.user("ada@acme.dev", team="eng", role="admin")
    wes = await world.user("wes@acme.dev", team="eng")
    rita = await world.user("rita@acme.dev", team="eng")
    otto = await world.user("otto@acme.dev", team="eng")
    project = await world.project("eng", "platform", {"wes@acme.dev": "write", "rita@acme.dev": "read"})
    node = await world.node(ada, project, "billing", "Billing service", "Stripe integration: invoices, webhooks")
    return Org(world, ada, wes, rita, otto, project, node)
