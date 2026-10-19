from __future__ import annotations

import asyncio
import os
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
from sqlalchemy import make_url, select

from cairn.core import admin, write
from cairn.core.store import SqlStore
from cairn.db import migrate
from cairn.db.engine import make_engine
from cairn.db.schema import entries, term_df, users
from cairn.models import EntryFields, NodeFields, WriteRequest
from tests.conftest import SETUP_TOKEN, Org, World, open_world, outcome


@pytest.fixture
async def pgw() -> AsyncIterator[World]:
    base = os.environ.get("CAIRN_TEST_PG_URL") or pytest.skip("needs CAIRN_TEST_PG_URL")
    name = f"review_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    url = make_url(base).set(database=name).render_as_string(hide_password=False)
    try:
        async with open_world(url) as w:
            yield w
    finally:
        with psycopg.connect(base, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


async def make_org(w: World) -> Org:
    await w.admin(w.root, "create_team", slug="eng")
    ada = await w.user("ada@acme.dev", team="eng", role="admin")
    wes = await w.user("wes@acme.dev", team="eng")
    project = await w.project("eng", "platform", {"wes@acme.dev": "write"})
    node = await w.node(ada, project, "billing", "Billing service", "Stripe integration")
    return Org(w, ada, wes, wes, wes, project, node)


def slow(monkeypatch: pytest.MonkeyPatch, module: Any, name: str, seconds: float, before: bool = False) -> None:
    original = getattr(module, name)

    async def wrapper(*a: Any, **k: Any) -> Any:
        if before:
            await asyncio.sleep(seconds)
        out = await original(*a, **k)
        if not before:
            await asyncio.sleep(seconds)
        return out

    monkeypatch.setattr(module, name, wrapper)


async def staggered(*calls: Any, gap: float = 0.15) -> list[str]:
    async def run(i: int, call: Any) -> str:
        await asyncio.sleep(i * gap)
        try:
            return await outcome(call())
        except Exception as exc:  # noqa: BLE001
            return f"UNHANDLED {type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
    return list(await asyncio.gather(*(run(i, c) for i, c in enumerate(calls))))


async def df_drift(w: World) -> dict[tuple[str, int], tuple[int, int]]:
    async with w.store.engine.connect() as conn:
        rows = (await conn.execute(select(entries.c.project_id, entries.c.an_head, entries.c.an_body)
                                   .where(entries.c.is_searchable))).all()
        actual = {(t, p): df for t, p, df in await conn.execute(select(term_df)) if df}
    expected: Counter[tuple[str, int]] = Counter()
    for pid, head, body in rows:
        expected.update((t, pid) for t in set(head.split()) | set(body.split()))
    keys = set(actual) | set(expected)
    return {k: (actual.get(k, 0), expected.get(k, 0)) for k in keys if actual.get(k, 0) != expected.get(k, 0)}


async def test_update_node_lost_update(pgw: World, monkeypatch: pytest.MonkeyPatch) -> None:
    org = await make_org(pgw)
    slow(monkeypatch, write, "_log", 0.5, before=True)
    res = await staggered(
        lambda: pgw.write(org.writer, "update_node", ref=org.node, base_revision=1,
                          node_fields=NodeFields(title="Billing (Alice's edit)")),
        lambda: pgw.write(org.admin, "update_node", ref=org.node, base_revision=1,
                          node_fields=NodeFields(title="Billing (Bob's edit)")),
    )
    [node] = await pgw.get(org.admin, org.node)
    print("update_node x2 with base_revision=1 ->", res, "| final:", node.title, "rev", node.rev)
    assert res.count("ok") == 1, "both writers passed the base_revision check"


async def test_update_entry_race_is_a_conflict(pgw: World, monkeypatch: pytest.MonkeyPatch) -> None:
    org = await make_org(pgw)
    ref = await pgw.add(org.writer, org.node, "Refunds settle in five business days")
    slow(monkeypatch, write, "_log", 0.5, before=True)
    res = await staggered(
        lambda: pgw.write(org.writer, "update", ref=ref, base_revision=1, entry=EntryFields(body="Seven days now.")),
        lambda: pgw.write(org.admin, "update", ref=ref, base_revision=1, entry=EntryFields(body="Ten days now.")),
    )
    print("update x2 with base_revision=1 ->", res)
    assert sorted(res) == ["conflict", "ok"]


async def test_concurrent_archive_keeps_term_df_exact(pgw: World, monkeypatch: pytest.MonkeyPatch) -> None:
    org = await make_org(pgw)
    ref = await pgw.add(org.writer, org.node, "Chargebacks freeze the payout for ten days")
    assert await df_drift(pgw) == {}
    slow(monkeypatch, write, "_log", 0.5, before=True)
    res = await staggered(
        lambda: pgw.write(org.writer, "archive", ref=ref),
        lambda: pgw.write(org.admin, "archive", ref=org.node),
    )
    drift = await df_drift(pgw)
    print("archive entry || archive node ->", res, "| term_df (actual, expected) drift:", drift)
    assert drift == {}


async def test_concurrent_update_and_archive_keep_term_df_exact(pgw: World, monkeypatch: pytest.MonkeyPatch) -> None:
    org = await make_org(pgw)
    ref = await pgw.add(org.writer, org.node, "Chargebacks freeze the payout for ten days")
    slow(monkeypatch, write, "_log", 0.5, before=True)
    res = await staggered(
        lambda: pgw.write(org.writer, "update", ref=ref, base_revision=1,
                          entry=EntryFields(title="Disputes hold settlements for two weeks")),
        lambda: pgw.write(org.admin, "archive", ref=ref),
    )
    drift = await df_drift(pgw)
    print("update || archive ->", res, "| term_df drift:", drift)
    assert drift == {}


# The swapped tokens deadlock unless term_df upserts are sorted per transaction.
async def test_crossing_updates_do_not_deadlock(pgw: World, monkeypatch: pytest.MonkeyPatch) -> None:
    org = await make_org(pgw)
    a = await pgw.add(org.writer, org.node, "Alpha zebra quokka")
    b = await pgw.add(org.writer, org.node, "Bravo yak walrus")
    slow(monkeypatch, write, "_apply_df", 0.4)
    res = await staggered(
        lambda: pgw.write(org.writer, "update", ref=a, base_revision=1, entry=EntryFields(title="Alpha zebra walrus")),
        lambda: pgw.write(org.admin, "update", ref=b, base_revision=1, entry=EntryFields(title="Bravo yak quokka")),
    )
    print("update a (quokka->walrus) || update b (walrus->quokka) ->", res)
    assert res == ["ok", "ok"]


async def test_a_retry_overlapping_its_first_attempt_replays_it(pgw: World, monkeypatch: pytest.MonkeyPatch) -> None:
    org = await make_org(pgw)
    store = SqlStore(pgw.store.engine, pgw.store.alias, write.WRITE_QUOTA)
    title = "Refunds take ten days to settle"
    req = WriteRequest(op="add", node=org.node, op_id="op-1", entry=EntryFields(type="gotcha", title=title))
    slow(monkeypatch, write, "_log", 0.5, before=True)
    res = await staggered(lambda: store.write(org.writer, req), lambda: store.write(org.writer, req))
    async with pgw.store.engine.connect() as conn:
        added = (await conn.execute(select(entries.c.id).where(entries.c.title == title))).all()
    print("write || its retry ->", res, "| entries:", len(added))
    assert res == ["ok", "ok"] and len(added) == 1


async def test_bootstrap_creates_one_instance_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    base = os.environ.get("CAIRN_TEST_PG_URL") or pytest.skip("needs CAIRN_TEST_PG_URL")
    name = f"review_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    url = make_url(base).set(database=name).render_as_string(hide_password=False)
    await asyncio.to_thread(migrate.upgrade, url)
    engine = make_engine(url)
    try:
        async with engine.begin() as conn:
            await admin.register_setup_token(conn, SETUP_TOKEN)
        slow(monkeypatch, admin, "_create_user", 0.4)

        async def boot(email: str) -> str:
            async with engine.begin() as conn:
                return await admin.bootstrap(conn, SETUP_TOKEN, email, email)

        res = await staggered(lambda: boot("a@acme.dev"), lambda: boot("b@acme.dev"))
        async with engine.connect() as conn:
            admins = (await conn.execute(select(users.c.email).where(users.c.is_instance_admin))).scalars().all()
        print("two bootstraps ->", res, "| instance admins:", admins)
        assert len(admins) == 1
    finally:
        await engine.dispose()
        with psycopg.connect(base, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
