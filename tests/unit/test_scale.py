from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from cairn.core import search
from cairn.core.store import Federation, SqlStore
from cairn.db.schema import links
from cairn.models import EntryFields, GetRequest, LinkIn, SearchRequest
from tests.conftest import Org, open_world, outcome, sqlite_url
from tests.unit.test_write import assert_index_exact


@pytest.fixture
def tiny_g(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search, "G_CAP", 1)  # any term in two entries counts as common


async def test_common_terms_stop_driving_the_scan(org: Org, tiny_g: None, monkeypatch: pytest.MonkeyPatch) -> None:
    w = org.w
    signed = await w.add(org.writer, org.node, "Webhook signatures expire after five minutes")
    for title in ("Webhook retries back off exponentially", "Webhook payloads are capped at 256 KB"):
        await w.add(org.writer, org.node, title)
    monkeypatch.setattr(search, "POSTINGS_BUDGET", 1)
    concepts = search.parse_concepts(["webhook", "signature"])
    async with w.store.engine.connect() as conn:
        assert not await search.plan(conn, concepts, [int(org.project.split(":p")[1])])
    assert concepts[0].scored_only == {"webhook"} and concepts[1].scored_only == set()
    assert [h.ref for h in await w.search(org.reader, "webhook", "signature")] == [signed]


async def test_only_common_terms_must_all_match(org: Org, tiny_g: None) -> None:
    w = org.w
    misc = await w.node(org.writer, org.project, "misc", "Misc")  # billing's summary mentions webhooks
    both = await w.add(org.writer, misc, "Webhook retries back off exponentially")
    await w.add(org.writer, misc, "Webhook payloads are capped at 256 KB")
    await w.add(org.writer, misc, "Refund retries stop after three days")
    res = await w.store.search(org.reader, SearchRequest(concepts=["webhook", "retry"]))
    assert res.common_only and [h.ref for h in res.hits] == [both]
    assert not (await w.store.search(org.reader, SearchRequest(concepts=["webhook", "payload"]))).common_only
    assert not (await w.store.search(org.reader, SearchRequest(concepts=["webhook", "xyzzy"]))).common_only


async def test_nodes_match_on_their_own(org: Org) -> None:
    w = org.w
    disputes = await w.node(org.writer, org.node, "disputes", "Disputes", "Chargeback evidence and deadlines")
    res = await w.store.search(org.reader, SearchRequest(concepts=["chargeback"]))
    assert res.hits == [] and [n.ref for n in res.nodes] == [disputes]
    entry = await w.add(org.writer, disputes, "Chargeback evidence is due in seven days")
    res = await w.store.search(org.reader, SearchRequest(concepts=["chargeback"]))
    assert [h.ref for h in res.hits] == [entry] and res.nodes == []  # its node is already shown
    assert (await w.store.search(org.reader, SearchRequest(concepts=["deadline"], types=["gotcha"]))).nodes == []
    assert (await w.store.search(org.member, SearchRequest(concepts=["chargeback"]))).nodes == []


async def test_federated_node_matches_cover_the_merged_top_hit(org: Org, tmp_path: Path) -> None:
    await org.w.add(org.writer, org.node, "Stripe webhooks retry for three days")
    (tmp_path / "me").mkdir()
    async with open_world(sqlite_url(tmp_path / "me")) as local:
        await local.admin(local.root, "create_team", slug="me")
        await local.node(local.root, await local.project("me", "personal"), "stripe", "Stripe")
        fed = Federation([SqlStore(local.store.engine, "me"), org.w.store])
        alone = await fed.search(org.w.root, SearchRequest(concepts=["stripe", "retry"], stores=["me"]))
        merged = await fed.search(org.w.root, SearchRequest(concepts=["stripe", "retry"]))
    assert [n.ref for n in alone.nodes] == ["me:n1"]
    assert merged.hits[0].features[0] == 2 and merged.nodes == []  # covers one concept, the top hit two


async def test_path_refs_resolve_to_canonical_refs(org: Org) -> None:
    w = org.w
    child = await w.node(org.writer, org.node, "webhooks", "Webhooks")
    [node, project, missing] = await w.get(org.reader, "acme:eng/platform/billing/webhooks", "acme:eng/platform",
                                           "acme:eng/platform/nope")
    assert (node.ref, project.ref, missing.status) == (child, org.project, "not_found")
    both = await Federation([w.store]).get(org.reader, GetRequest(refs=["acme:eng/platform/billing/webhooks", child]))
    assert [(i.ref, i.status) for i in both.items] == [(child, "ok"), (child, "ok")]
    assert (await w.get(org.member, "acme:eng/platform"))[0].status == "not_found"
    added = await w.write(org.writer, "add", node="acme:eng/platform/billing", entry=EntryFields(
        type="gotcha", title="Webhook signatures expire after five minutes",
        links=[LinkIn(to="acme:eng/platform/billing/webhooks", reason="where the retries live")]))
    assert added.ref and (await w.get(org.reader, added.ref))[0].node_ref == org.node
    assert [link.ref for link in (await w.get(org.reader, added.ref))[0].links] == [child]
    assert [h.ref for h in await w.search(org.reader, "signature", within="acme:eng/platform/billing")] == [added.ref]
    assert await outcome(w.write(org.writer, "add", node="acme:eng/platform/nope", entry=EntryFields(
        type="general", title="Nowhere to go"))) == "bad_ref"


async def test_moves_across_projects_are_admin_only_and_keep_the_index_exact(org: Org) -> None:
    w = org.w
    payments = await w.project("eng", "payments", {"rita@acme.dev": "read"})
    landing = await w.node(org.admin, payments, "landing", "Landing")
    child = await w.node(org.writer, org.node, "webhooks", "Webhooks")
    kept = await w.add(org.writer, child, "Webhook signatures expire after five minutes")
    gone = await w.add(org.writer, child, "Webhook replay tool is retired")
    await w.write(org.writer, "archive", ref=gone)
    linker = (await w.write(org.writer, "add", node=org.node, entry=EntryFields(
        type="general", title="Invoices are numbered per account", links=[LinkIn(to=kept, reason="same signer")],
    ))).ref
    other = await w.project("eng", "other", {"wes@acme.dev": "write"})
    assert await outcome(w.write(org.writer, "move", ref=org.node, project=other)) == "invalid"
    assert await outcome(w.write(org.writer, "move", ref=org.node, project=payments)) == "not_found"
    assert await outcome(w.admin(org.writer, "move", ref=org.node, project=other)) == "admin_required"
    await w.admin(org.admin, "move", ref=org.node, project=payments)
    [node] = await w.get(org.reader, org.node)
    assert node.path == "billing" and [h.ref for h in await w.search(org.reader, "signature")] == [kept]
    assert (await w.store.search(org.reader, SearchRequest(concepts=["signature"], within=org.project))).hits == []
    assert (await w.get(org.writer, kept))[0].status == "not_found"  # wes can't read payments
    async with w.store.engine.connect() as conn:
        assert {r.project_id for r in await conn.execute(select(links))} == {int(payments.split(":p")[1])}
    await w.admin(org.admin, "move", ref=kept, node=landing)
    assert (await w.get(org.reader, kept))[0].node_ref == landing
    await assert_index_exact(w)
    assert linker
