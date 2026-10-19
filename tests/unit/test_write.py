"""The write path on SQLite: validation, secrets, dedup, revisions, archive, moves, nodes, and term_df upkeep."""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from typing import Any

import pytest
from sqlalchemy import func, select

from cairn.core.analyzer import is_opaque
from cairn.core.write import WriteError
from cairn.db.schema import change_log, entries, entry_revisions, term_df, vocab
from cairn.models import EntryFields, NodeFields
from tests.conftest import Org, World, outcome

TABLES = (entries, entry_revisions, change_log, term_df, vocab)


async def counts(w: World) -> dict[str, int]:
    async with w.store.engine.connect() as conn:
        out = {t.name: (await conn.execute(select(func.count()).select_from(t))).scalar_one() for t in TABLES}
        out["df_total"] = (await conn.execute(select(func.coalesce(func.sum(term_df.c.df), 0)))).scalar_one()
    return out


async def assert_index_exact(w: World) -> None:
    """term_df equals a recount over searchable entries; vocab holds their tokens, never opaque ones."""
    async with w.store.engine.connect() as conn:
        rows = (await conn.execute(select(entries.c.project_id, entries.c.an_head, entries.c.an_body,
                                          entries.c.is_searchable))).all()
        actual = {(t, p): df for t, p, df in (await conn.execute(select(term_df))) if df}
        words = set((await conn.execute(select(vocab.c.term))).scalars())
    expected: Counter[tuple[str, int]] = Counter()
    for pid, head, body, searchable in rows:
        tokens = set(head.split()) | set(body.split())
        if searchable:
            expected.update((t, pid) for t in tokens)
            assert {t for t in tokens if not is_opaque(t)} <= words
    assert actual == dict(expected)
    assert not any(is_opaque(t) for t in words)


async def found(w: World, org: Org, ref: str, *concepts: str) -> bool:
    return ref in [h.ref for h in await w.search(org.admin, *concepts)]


# --- validation and secrets ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("fields", "result"), [
    ({"title": "t" * 160, "body": "b" * 2000, "keywords": [f"k{i}" for i in range(8)]}, "ok"),
    ({"title": "é" * 160}, "ok"),  # limits count code points after NFC
    ({"title": "t" * 161}, "invalid"),
    ({"title": "fine", "body": "b" * 2001}, "invalid"),
    ({"title": "fine", "keywords": [f"k{i}" for i in range(9)]}, "invalid"),
    ({"title": "  ​ "}, "invalid"),  # empty once invisible characters are stripped
])
async def test_entry_limits(org: Org, fields: dict[str, object], result: str) -> None:
    assert await outcome(org.w.add(org.writer, org.node, **fields)) == result  # type: ignore[arg-type]


@pytest.mark.parametrize(("fields", "result"), [
    ({"slug": "ok_1", "summary": "s" * 400, "keywords": [f"k{i}" for i in range(12)]}, "ok"),
    ({"slug": "ok", "summary": "s" * 401}, "invalid"),
    ({"slug": "ok", "keywords": [f"k{i}" for i in range(13)]}, "invalid"),
    ({"slug": "Bad-Slug"}, "invalid"),
    ({"slug": "x" * 49}, "invalid"),
])
async def test_node_limits(org: Org, fields: dict[str, object], result: str) -> None:
    assert await outcome(org.w.node(org.writer, org.node, title="Node", **fields)) == result  # type: ignore[arg-type]


async def test_normalization_and_keyword_stripping(org: Org) -> None:
    ref = await org.w.add(org.writer, org.node, "Stripe​ webhooks retry\r\n", body="line one\r\nline two",
                          keywords=["webhook", "Idempotency", "idempotency"])
    [item] = await org.w.get(org.writer, ref)
    assert (item.title, item.body, item.keywords) == ("Stripe webhooks retry", "line one\nline two", ["Idempotency"])


SECRETS = [
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA",
    "use AKIAIOSFODNN7EXAMPLE for uploads",
    "token ghp_" + "a1B2" * 9,
    "xoxb-1234567890-abcdefghij",
    "cairn_pat_" + "Q" * 30,
    "api_key = 'abcd1234efgh5678ijkl'",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.e30.sig",
]


@pytest.mark.parametrize("secret", SECRETS)
@pytest.mark.parametrize("where", ["title", "body", "keywords"])
async def test_secrets_are_rejected(org: Org, secret: str, where: str) -> None:
    before = await counts(org.w)
    fields = {"title": "A harmless fact", where: [secret] if where == "keywords" else secret[:160]}
    assert await outcome(org.w.add(org.writer, org.node, **fields)) == "secret"  # type: ignore[arg-type]
    assert await counts(org.w) == before


async def test_secrets_in_nodes_are_rejected(org: Org) -> None:
    assert await outcome(org.w.node(org.writer, org.node, "keys", summary=SECRETS[4])) == "secret"


async def test_high_entropy_strings_are_rejected(org: Org) -> None:
    body = "the signing value is 9fQ2xLmZ8pR4tW7yB1nC6vD3hJ5kG0sAeUq"
    assert await outcome(org.w.add(org.writer, org.node, "Signing setup", body=body)) == "secret"


# --- dedup -----------------------------------------------------------------------------------------------------------

STRIPE = ("Stripe webhooks retry for up to 3 days, so handlers must be idempotent",
          "Stripe resends failed events with exponential backoff for 72 hours; store processed event ids.")


async def test_dedup_returns_candidates_and_writes_nothing(org: Org) -> None:
    w = org.w
    first = await w.add(org.writer, org.node, *STRIPE)
    before = await counts(w)
    with pytest.raises(WriteError) as exc:
        await w.add(org.writer, org.node, "Stripe webhooks retry for up to three days; handlers must be idempotent")
    assert exc.value.code == "duplicate" and [d.ref for d in exc.value.duplicates] == [first]
    assert exc.value.duplicates[0].score >= 0.6
    assert await counts(w) == before
    # Body containment alone is enough.
    assert await outcome(w.add(org.writer, org.node, "Event redelivery window", body=STRIPE[1])) == "duplicate"
    # An update is never its own duplicate, but may duplicate another entry.
    await w.write(org.writer, "update", ref=first, base_revision=1, entry=EntryFields(body=STRIPE[1] + " Really."))
    other = await w.add(org.writer, org.node, "Invoices render as PDF through WeasyPrint")
    assert await outcome(w.write(org.writer, "update", ref=other, base_revision=1,
                                 entry=EntryFields(title=STRIPE[0]))) == "duplicate"
    # distinct_from bypasses exactly the named entries.
    second = await w.add(org.writer, org.node, STRIPE[0] + "!", distinct_from=[first])
    assert await outcome(w.add(org.writer, org.node, STRIPE[0] + "?", distinct_from=[first])) == "duplicate"
    assert await outcome(w.add(org.writer, org.node, STRIPE[0] + "?", distinct_from=[first, second])) == "ok"


async def test_dedup_spans_readable_projects_only(org: Org) -> None:
    w = org.w
    secret_project = await w.project("eng", "secret")  # wes can't read it
    hidden = await w.node(org.admin, secret_project, "ops")
    await w.add(org.admin, hidden, *STRIPE)
    assert await outcome(w.add(org.writer, org.node, *STRIPE)) == "ok"
    shared = await w.project("eng", "shared", {"wes@acme.dev": "read"})
    await w.add(org.admin, await w.node(org.admin, shared, "notes"), "Kafka consumers lag behind after deploys")
    assert await outcome(w.add(org.writer, org.node, "Kafka consumers lag behind after deploys")) == "duplicate"


# --- revisions and verify --------------------------------------------------------------------------------------------


async def test_update_needs_current_base_revision(org: Org) -> None:
    w = org.w
    ref = await w.add(org.writer, org.node, "Refunds take 5 business days", body="Stripe default.")
    for base in (None, 0, 2):
        assert await outcome(w.write(org.writer, "update", ref=ref, base_revision=base,
                                     entry=EntryFields(body="x"))) == "conflict"
    up = await w.write(org.writer, "update", ref=ref, base_revision=1, entry=EntryFields(title="Refunds take 7 days"))
    assert (up.ref, up.rev) == (ref, 2)
    assert await outcome(w.write(org.writer, "update", ref=ref, base_revision=1, entry=EntryFields(body="y"))) \
        == "conflict"
    same = await w.write(org.writer, "update", ref=ref, base_revision=2, entry=EntryFields(title="Refunds take 7 days"))
    assert same.rev == 2  # no change, no new revision
    [item] = await w.get(org.reader, ref, history=True)
    assert item.rev == 2 and [(r.rev, r.title) for r in item.history] == [
        (2, "Refunds take 7 days"), (1, "Refunds take 5 business days")]


async def test_verify_bumps_verified_without_a_revision(org: Org) -> None:
    w = org.w
    ref = await w.add(org.writer, org.node, "Proration is computed daily")
    entry_id = int(ref.rsplit("e", 1)[1])
    stmt = select(entries.c.verified_at, entries.c.verified_by, entries.c.rev).where(entries.c.id == entry_id)
    async with w.store.engine.connect() as conn:
        before = (await conn.execute(stmt)).one()
    await asyncio.sleep(0.01)
    await w.write(org.admin, "verify", ref=ref)
    async with w.store.engine.connect() as conn:
        after = (await conn.execute(stmt)).one()
    assert after.verified_at > before.verified_at and after.rev == before.rev == 1
    assert after.verified_by == org.admin.user_id != before.verified_by
    assert await outcome(w.write(org.reader, "verify", ref=ref)) == "read_only"


# --- archive -------------------------------------------------------------------------------------------------------


async def test_archive_hides_entry_everywhere_and_unarchive_restores(org: Org) -> None:
    w = org.w
    title = "Kafka consumer lag alerts fire after 5 minutes"
    ref = await w.add(org.writer, org.node, title, type="gotcha")
    await w.admin(org.admin, "flag", ref=ref)

    async def visible() -> tuple[bool, str, int, bool]:
        [item] = await w.get(org.reader, ref)
        [node] = (await w.map(org.reader)).stores[0].projects[0].nodes
        injected = any(i.ref == ref for g in await w.inject(org.reader) for i in g.items)
        return await found(w, org, ref, "kafka", "lag"), item.status, node.entries, injected

    assert await visible() == (True, "ok", 1, True)
    await w.write(org.admin, "archive", ref=ref, reason="moved to runbook")
    assert await visible() == (False, "archived", 0, False)
    [item] = await w.get(org.reader, ref)
    assert item.body is None and re.fullmatch(rf"{ref} — archived \d{{4}}-\d\d-\d\d by ada", item.tombstone or "")
    assert await outcome(w.write(org.admin, "update", ref=ref, base_revision=1, entry=EntryFields(body="x"))) \
        == "archived"
    await assert_index_exact(w)
    twin = await w.add(org.writer, org.node, title)  # an archived entry is no duplicate
    await w.write(org.admin, "unarchive", ref=ref)
    assert await visible() == (True, "ok", 2, True)
    assert await found(w, org, twin, "kafka", "lag")
    await assert_index_exact(w)


async def test_node_archive_cascades_to_subtree(org: Org) -> None:
    w = org.w
    child = await w.node(org.writer, org.node, "webhooks", "Webhooks")
    grandchild = await w.node(org.writer, child, "retries", "Retries")
    top = await w.add(org.writer, org.node, "Invoices are finalized one hour after creation")
    deep = await w.add(org.writer, grandchild, "Webhook retries use exponential backoff")
    sibling = await w.add(org.writer, child, "Webhook signatures use HMAC SHA-256")

    async def state() -> list[str]:
        items = await w.get(org.writer, org.node, child, grandchild, top, deep, sibling)
        return [i.status for i in items]

    await w.write(org.writer, "archive", ref=grandchild)
    assert await state() == ["ok", "ok", "archived", "ok", "archived", "ok"]
    await w.write(org.writer, "archive", ref=org.node)
    assert await state() == ["archived"] * 6
    [_, inner, *_] = await w.get(org.writer, org.node, child)
    assert inner.tombstone == f"{child} — inside an archived node"
    assert not await w.search(org.writer, "webhook|invoice")
    assert (await w.map(org.writer)).stores[0].projects[0].nodes == []
    assert await outcome(w.add(org.writer, child, "Anything new")) == "archived"
    assert await outcome(w.node(org.writer, child, "sub")) == "archived"
    await assert_index_exact(w)
    # Unarchiving the top restores everything except the subtree that was archived on its own.
    await w.write(org.writer, "unarchive", ref=org.node)
    assert await state() == ["ok", "ok", "archived", "ok", "archived", "ok"]
    await assert_index_exact(w)


# --- move and nodes --------------------------------------------------------------------------------------------------


async def test_move_within_project(org: Org) -> None:
    w = org.w
    payments = await w.node(org.writer, org.project, "payments", "Payments")
    ref = await w.add(org.writer, org.node, "Chargebacks freeze the payout for 10 days")
    await w.write(org.writer, "move", ref=ref, node=payments)
    [item] = await w.get(org.writer, ref)
    assert item.node_ref == payments and item.path == "payments"
    await w.write(org.writer, "move", ref=org.node, node=payments)
    [node] = await w.get(org.writer, org.node)
    assert node.path == "payments/billing"
    [hit] = await w.search(org.writer, "chargeback")
    assert hit.node_ref == payments
    await w.write(org.writer, "move", ref=org.node, project=org.project)  # back to the top level
    [node] = await w.get(org.writer, org.node)
    assert node.path == "billing"

    other = await w.project("eng", "other", {"wes@acme.dev": "write"})
    elsewhere = await w.node(org.writer, other, "misc")
    child = await w.node(org.writer, org.node, "child")
    await w.node(org.writer, payments, "billing")
    for fields, result in [
        ({"ref": ref, "node": elsewhere}, "invalid"),  # across projects: not supported yet
        ({"ref": org.node, "node": elsewhere}, "invalid"),
        ({"ref": org.node, "node": child}, "invalid"),  # into its own subtree
        ({"ref": org.node, "node": payments}, "conflict"),  # payments/billing exists
        ({"ref": ref}, "invalid"),  # an entry needs a target node
    ]:
        assert await outcome(w.write(org.writer, "move", **fields)) == result
    await assert_index_exact(w)


async def test_node_depth_limit(org: Org) -> None:
    w = org.w
    chain = [org.node]
    for depth in range(2, 9):
        chain.append(await w.node(org.writer, chain[-1], f"d{depth}"))
    [deepest] = await w.get(org.writer, chain[-1])
    assert deepest.path == "billing/d2/d3/d4/d5/d6/d7/d8"
    assert await outcome(w.node(org.writer, chain[-1], "d9")) == "invalid"
    # Moving a two-level subtree under depth 7 would put its leaf at depth 9.
    branch = await w.node(org.writer, org.project, "branch")
    await w.node(org.writer, branch, "leaf")
    assert await outcome(w.write(org.writer, "move", ref=branch, node=chain[-2])) == "invalid"
    assert await outcome(w.write(org.writer, "move", ref=branch, node=chain[-3])) == "ok"


async def test_update_node(org: Org) -> None:
    w = org.w
    up = await w.write(org.writer, "update_node", ref=org.node, base_revision=1,
                       node_fields=NodeFields(title="Billing", summary="Invoices and refunds"))
    [node] = await w.get(org.reader, org.node)
    assert (up.rev, node.title, node.summary) == (2, "Billing", "Invoices and refunds")
    ref = await w.add(org.writer, org.node, "Credit notes need a reason code")
    assert ref in [h.ref for h in await w.search(org.reader, "refund", "reason code")]
    for fields, base, result in [(NodeFields(title="x"), 1, "conflict"), (NodeFields(slug="renamed"), 2, "invalid")]:
        assert await outcome(w.write(org.writer, "update_node", ref=org.node, base_revision=base,
                                     node_fields=fields)) == result


async def test_term_df_stays_exact(org: Org) -> None:
    w = org.w
    a = await w.add(org.writer, org.node, "Webhook retries back off exponentially", body="Up to 3 days. 日本語 note.")
    b = await w.add(org.writer, org.node, "Invoice numbers are gapless per account", keywords=["webhook"])
    child = await w.node(org.writer, org.node, "child")
    c = await w.add(org.writer, child, "Retries share the webhook queue", body="queue name billing_webhooks")
    steps: list[tuple[str, dict[str, Any], str]] = [
        ("update", {"ref": a, "base_revision": 1, "entry": EntryFields(title="Webhook retries end after a day")}, "ok"),
        ("archive", {"ref": b}, "ok"),
        ("update", {"ref": b, "base_revision": 1, "entry": EntryFields(body="x")}, "archived"),
        ("unarchive", {"ref": b}, "ok"),
        ("archive", {"ref": child}, "ok"),
        ("update", {"ref": c, "base_revision": 1, "entry": EntryFields(body="queue name billing_hooks")},
         "archived"),  # inside an archived subtree,
        ("unarchive", {"ref": child}, "ok"),
        ("archive", {"ref": a}, "ok"),
        ("archive", {"ref": org.node}, "ok"),
        ("unarchive", {"ref": org.node}, "ok"),
        ("unarchive", {"ref": a}, "ok"),
    ]
    await assert_index_exact(w)
    for op, fields, result in steps:
        assert await outcome(w.write(org.writer, op, **fields)) == result, (op, fields)
        await assert_index_exact(w)


async def test_underscore_in_slugs_is_not_a_wildcard(org: Org) -> None:
    """Slugs allow "_", a LIKE wildcard: subtree matches must not reach a sibling that differs only there."""
    w = org.w
    a = await w.node(org.writer, org.project, "my_node")
    b = await w.node(org.writer, org.project, "myxnode")
    a_child, b_child = await w.node(org.writer, a, "child"), await w.node(org.writer, b, "child")
    in_a = await w.add(org.writer, a_child, "Sharded counters need a reconcile job")
    in_b = await w.add(org.writer, b_child, "Webhook signatures expire after five minutes")
    assert [h.ref for h in await w.search(org.admin, "webhook", within=a)] == []
    await w.admin(org.admin, "flag", ref=a)
    assert await outcome(w.write(org.writer, "update", ref=in_b, base_revision=1,
                                 entry=EntryFields(body="still writable"))) == "ok"
    await w.write(org.admin, "archive", ref=a)
    assert await found(w, org, in_b, "webhook") and not await found(w, org, in_a, "sharded")
    [node] = await w.get(org.reader, b_child)
    assert node.status == "ok"
