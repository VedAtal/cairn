from __future__ import annotations

import pytest

from cairn.core import accounts
from cairn.core.access import Principal
from cairn.models import EntryFields, LinkIn
from tests.conftest import Org, World, outcome


def token_of(link: str) -> str:
    return link.split("token=", 1)[1]


async def test_password_hashing() -> None:
    stored = accounts.hash_password("correct horse battery")
    assert stored.startswith("scrypt$") and accounts.verify_password("correct horse battery", stored)
    assert not accounts.verify_password("wrong", stored) and not accounts.verify_password("x", None)
    assert not accounts.verify_password("x", "garbage")


async def test_set_password_login_logout(world: World) -> None:
    created = await world.admin(world.root, "create_user", email="pam@acme.dev", name="Pam")
    async with world.store.engine.begin() as conn:
        assert await accounts.login(conn, "pam@acme.dev", "whatever-123") is None
        assert await outcome(accounts.set_password(conn, token_of(created["password_link"]), "short")) == "invalid"
        await accounts.set_password(conn, token_of(created["password_link"]), "a long enough secret")
        assert await outcome(accounts.set_password(conn, token_of(created["password_link"]), "another secret!")) \
            == "bad_token"
        session = await accounts.login(conn, "PAM@acme.dev", "a long enough secret")
        assert session and await accounts.login(conn, "pam@acme.dev", "nope nope nope") is None
        principal = await accounts.session_principal(conn, session)
        assert principal and not principal.instance_admin
        await accounts.logout(conn, session)
        assert await accounts.session_principal(conn, session) is None


async def test_password_links_are_instance_admin_only(org: Org) -> None:
    w = org.w
    assert await outcome(w.admin(org.admin, "password_link", email="wes@acme.dev")) == "admin_required"
    assert (await w.admin(w.root, "password_link", email="wes@acme.dev"))["password_link"].startswith("/ui/")


async def test_ui_tickets_are_single_use_and_need_a_full_key(world: World) -> None:
    async with world.store.engine.begin() as conn:
        ticket = await accounts.ui_ticket(conn, world.root)
        session = await accounts.redeem_ticket(conn, ticket)
        assert session and await accounts.session_principal(conn, session) is not None
        assert await accounts.redeem_ticket(conn, ticket) is None
        narrowed = Principal(world.root.user_id, True, read_only=True)
        assert await outcome(accounts.ui_ticket(conn, narrowed)) == "admin_required"


async def test_links_show_both_ways_and_respect_access(org: Org) -> None:
    w = org.w
    other = await w.project("eng", "secret", {})
    hidden = await w.add(org.admin, await w.node(org.admin, other, "vault"), "Vault tokens rotate daily")
    target = await w.add(org.writer, org.node, "Webhook retries back off exponentially")
    for links, result in [
        ([LinkIn(to=target, reason="")], "invalid"),
        ([LinkIn(to=hidden, reason="shares the rotation job")], "not_found"),
    ]:
        assert await outcome(w.write(org.writer, "add", node=org.node, entry=EntryFields(
            type="gotcha", title="Refunds reuse the webhook queue", links=links))) == result
    source = (await w.write(org.writer, "add", node=org.node, entry=EntryFields(
        type="gotcha", title="Refunds reuse the webhook queue",
        links=[LinkIn(to=target, reason="its backoff delays refunds too"), LinkIn(to=org.node, reason="lives here")],
    ))).ref
    [item] = await w.get(org.reader, source)
    assert [(link.ref, link.reason) for link in item.links] == [
        (target, "its backoff delays refunds too"), (org.node, "lives here")]
    [back] = await w.get(org.reader, target)
    assert [link.ref for link in back.linked_from] == [source]
    await w.write(org.writer, "archive", ref=target)
    [item] = await w.get(org.reader, source)
    assert [link.ref for link in item.links] == [org.node]
    await w.write(org.writer, "unarchive", ref=target)
    third = await w.add(org.writer, org.node, "Payouts settle two days after capture")
    await w.write(org.writer, "update", ref=source, base_revision=1, entry=EntryFields(
        links=[LinkIn(to=third, reason="refunds wait for settlement")], unlink=[org.node]))
    [item] = await w.get(org.reader, source)
    assert [link.ref for link in item.links] == [target, third] and item.rev == 1


async def test_changes_archived_and_graph(org: Org) -> None:
    w = org.w
    child = await w.node(org.writer, org.node, "webhooks", "Webhooks")
    a = await w.add(org.writer, org.node, "Invoices are numbered per account")
    await w.write(org.writer, "add", node=child, entry=EntryFields(
        type="gotcha", title="Webhook signatures expire", links=[LinkIn(to=a, reason="numbers appear in payloads")]))
    await w.write(org.writer, "archive", ref=a, reason="moved")
    changes = await w.store.changes(org.reader, None, 10)
    assert [c.op for c in changes][:3] == ["archive", "add", "add"] and changes[0].by == "wes"
    assert [c.ref for c in await w.store.changes(org.member, None, 10)] == []
    [gone] = await w.store.archived(org.reader)
    assert (gone.ref, gone.reason, gone.by) == (a, "moved", "wes")
    graph = await w.store.graph(org.reader, org.project)
    assert {(n.ref, n.parent) for n in graph.nodes} == {(org.node, None), (child, org.node)}
    assert graph.edges == []  # the link's target is archived
    await w.write(org.writer, "unarchive", ref=a)
    [edge] = (await w.store.graph(org.reader, org.project)).edges
    assert (edge.source, edge.target, edge.count) == (child, org.node, 1)
    assert await outcome(w.store.graph(org.member, org.project)) == "not_found"


@pytest.mark.parametrize("writer_is_admin", [False, True])
async def test_links_on_injected_content_are_admin_only(org: Org, writer_is_admin: bool) -> None:
    w = org.w
    ref = await w.add(org.writer, org.node, "Payments settle at midnight UTC")
    other = await w.add(org.writer, org.node, "Ledger closes daily")
    await w.admin(org.admin, "flag", ref=ref)
    who = org.admin if writer_is_admin else org.writer
    result = await outcome(w.write(who, "update", ref=ref, base_revision=1,
                                   entry=EntryFields(links=[LinkIn(to=other, reason="same cutoff")])))
    assert result == ("ok" if writer_is_admin else "inject_locked")
