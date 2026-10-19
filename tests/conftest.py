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
    await asyncio.to_thread(migrate.upgrade, url)
    engine = make_engine(url)
    try:
        async with engine.begin() as conn:
            await admin.register_setup_token(conn, SETUP_TOKEN)
            key, _ = await admin.bootstrap(conn, SETUP_TOKEN, "root@acme.dev", "Root")
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
    w: World
    admin: Principal
    writer: Principal
    reader: Principal
    member: Principal
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


@dataclass
class UI:
    client: Any
    app: Any
    api_token: str

    async def login(self) -> None:
        """Log the client in through the UI ticket flow, as `cairn ui` does."""
        ticket = (await self.client.post("/api/v1/ui/ticket", headers={"Authorization": f"Bearer {self.api_token}"},
                                         json={})).json()["ticket"]
        assert (await self.client.get(f"/ui/login?ticket={ticket}")).status_code == 303

    def csrf(self) -> dict[str, str]:
        from cairn.ui.base import _csrf

        return {"X-CSRF": _csrf(self.client.cookies[cookie_name_for(self.app)])}


def cookie_name_for(app: Any) -> str:
    port = app.state.local_port
    return f"cairn_session_{port}" if port else "cairn_session"


@asynccontextmanager
async def ui_app(settings: Any, port: int | None, base_url: str) -> AsyncIterator[UI]:
    import httpx

    from cairn.app.build import build_app

    app = build_app(settings, port=port)
    started, stop = asyncio.Event(), asyncio.Event()

    async def serve() -> None:  # the lifespan's task group must be entered and exited in one task
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(serve())
    waiter = asyncio.create_task(started.wait())
    await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        task.result()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url) as client:
            yield UI(client, app, app.state.local_token if settings.mode == "local" else "")
    finally:
        stop.set()
        await task


@pytest.fixture
async def local_ui(tmp_path: Path) -> AsyncIterator[UI]:
    """A local-mode server (SQLite, local token) served in-process; call `await ui.login()` for a session."""
    from cairn.app.settings import Settings

    async with ui_app(Settings(mode="local", home=tmp_path / ".cairn"), 6804, "http://127.0.0.1:6804") as ui:
        yield ui


@pytest.fixture
async def hosted_ui(tmp_path: Path) -> AsyncIterator[UI]:
    """A hosted-mode server on SQLite, bootstrapped: `api_token` is the instance admin's API key."""
    from cairn.app.settings import DBSettings, Settings

    settings = Settings(mode="hosted", db=DBSettings(url=sqlite_url(tmp_path)), store_alias=ALIAS,
                        setup_token=SETUP_TOKEN)
    async with ui_app(settings, None, "http://testserver") as ui:
        body = (await ui.client.post("/api/v1/bootstrap", json={"token": SETUP_TOKEN, "email": "root@acme.dev",
                                                                 "name": "Root"})).json()
        ui.api_token = body["api_key"]
        ui.app.state.password_link = body["password_link"]
        yield ui
