"""Access: roles per project, admin-only injected content, and API key narrowing. Unauthorized looks like absent."""

from __future__ import annotations

from typing import Any

import pytest

from cairn.core.access import AccessError, Principal
from cairn.models import EntryFields, NodeFields
from tests.conftest import Org, World, outcome


def writes(org: Org, entry: str) -> dict[str, dict[str, Any]]:
    """One request per write op, all aimed at `org.node` / `entry`."""
    return {
        "add": {"op": "add", "node": org.node, "entry": EntryFields(type="general", title="Ledger exports run at 2am")},
        "update": {"op": "update", "ref": entry, "base_revision": 1, "entry": EntryFields(body="Changed body")},
        "verify": {"op": "verify", "ref": entry},
        "archive": {"op": "archive", "ref": entry},
        "create_node": {"op": "create_node", "node": org.node, "node_fields": NodeFields(slug="sub", title="Sub")},
        "update_node": {"op": "update_node", "ref": org.node, "base_revision": 1,
                        "node_fields": NodeFields(title="Billing and invoicing")},
    }


async def try_write(w: World, p: Principal, req: dict[str, Any]) -> str:
    fields = dict(req)
    return await outcome(w.write(p, fields.pop("op"), **fields))


async def test_member_in_zero_projects_sees_nothing(org: Org) -> None:
    w = org.w
    entry = await w.add(org.writer, org.node, "Invoices are numbered per account", type="decision")
    await w.admin(org.admin, "flag", ref=entry)
    assert await w.search(org.member, "invoice") == []
    assert [i.status for i in await w.get(org.member, entry, org.node, org.project)] == ["not_found"] * 3
    assert (await w.map(org.member)).stores[0].projects == []
    assert await outcome(w.map(org.member, root=org.node)) == "not_found"
    assert await w.inject(org.member) == []
    for op, req in writes(org, entry).items():
        assert await try_write(w, org.member, req) == "not_found", op


@pytest.mark.parametrize("op", ["add", "update", "verify", "archive", "create_node", "update_node"])
async def test_reader_gets_read_only(org: Org, op: str) -> None:
    w = org.w
    entry = await w.add(org.writer, org.node, "Invoices are numbered per account")
    assert [h.ref for h in await w.search(org.reader, "invoice")] == [entry]
    assert await try_write(w, org.reader, writes(org, entry)[op]) == "read_only"


LOCKED = {"entry": {"update", "archive"}, "node": {"add", "update", "archive", "create_node", "update_node"}}
LOCKED["parent node"] = LOCKED["node"]


async def flag(org: Org, flagged: str) -> str:
    """Add an entry under org.node, flag the entry, org.node, or a new parent of org.node; return the entry."""
    w = org.w
    entry = await w.add(org.writer, org.node, "Invoices are numbered per account")
    target = {"entry": entry, "node": org.node}.get(flagged)
    if target is None:
        target = await w.node(org.admin, org.project, "finance")
        await w.write(org.admin, "move", ref=org.node, node=target)
    await w.admin(org.admin, "flag", ref=target)
    return entry


@pytest.mark.parametrize("op", ["add", "update", "archive", "create_node", "update_node"])
@pytest.mark.parametrize("flagged", ["entry", "node", "parent node"])
async def test_injected_content_is_admin_only(org: Org, flagged: str, op: str) -> None:
    req = writes(org, await flag(org, flagged))[op]
    locked = op in LOCKED[flagged]
    assert await try_write(org.w, org.writer, req) == ("inject_locked" if locked else "ok")
    if locked:
        assert await try_write(org.w, org.admin, req) == "ok"


@pytest.mark.parametrize("op", ["archive", "move"])
async def test_ancestor_of_injected_content_is_locked(org: Org, op: str) -> None:
    await flag(org, "entry")
    target = await org.w.node(org.writer, org.project, "elsewhere")
    req = {"op": op, "ref": org.node} | ({"node": target} if op == "move" else {})
    assert await try_write(org.w, org.writer, req) == "inject_locked"
    assert await try_write(org.w, org.w.root, req) == "ok"


async def test_writer_cannot_verify_injected_entry(org: Org) -> None:
    entry = await flag(org, "entry")
    assert await try_write(org.w, org.writer, {"op": "verify", "ref": entry}) == "inject_locked"


@pytest.mark.parametrize(("who", "scopes", "write", "admin_op"), [
    ("writer", {"read_only": True}, "read_only", "admin_required"),
    ("admin", {"read_only": True}, "read_only", None),  # admin ops: see the xfail below
    ("admin", {"no_admin": True}, "ok", "admin_required"),
    ("admin", {}, "ok", "ok"),
])
async def test_key_scopes(org: Org, who: str, scopes: dict[str, bool], write: str, admin_op: str | None) -> None:
    w = org.w
    key = await w.principal((await w.admin(getattr(org, who), "create_key", name="narrow", **scopes))["api_key"])
    assert (key.read_only, key.no_admin) == (scopes.get("read_only", False), scopes.get("no_admin", False))
    entry = await w.add(org.writer, org.node, "Invoices are numbered per account")
    assert [h.ref for h in await w.search(key, "invoice")] == [entry]  # reading is never narrowed away
    assert await try_write(w, key, writes(org, entry)["update"]) == write
    if admin_op:
        assert await outcome(w.admin(key, "set_project_member", project=org.project, email="otto@acme.dev",
                                     role="read")) == admin_op


async def test_read_only_key_cannot_change_access(org: Org) -> None:
    w = org.w
    key = await w.principal((await w.admin(org.admin, "create_key", read_only=True))["api_key"])
    assert await outcome(w.admin(key, "set_project_member", project=org.project, email="otto@acme.dev",
                                 role="write")) != "ok"


@pytest.mark.parametrize("scope", ["read_only", "no_admin"])
async def test_narrowed_key_cannot_mint_a_broader_key(org: Org, scope: str) -> None:
    w = org.w
    narrow = await w.principal((await w.admin(org.admin, "create_key", **{scope: True}))["api_key"])
    try:
        key = (await w.admin(narrow, "create_key", name="escalate"))["api_key"]
    except AccessError:
        return
    assert getattr(await w.principal(key), scope)


async def test_instance_admin_key_with_no_admin_cannot_manage_users(world: World) -> None:
    key = await world.principal((await world.admin(world.root, "create_key", no_admin=True))["api_key"])
    assert await outcome(world.admin(key, "create_user", email="new@acme.dev")) == "admin_required"
    assert await outcome(world.admin(key, "create_team", slug="ops")) == "admin_required"


async def test_instance_admin_creates_users_and_teams(world: World) -> None:
    ada = await world.user("ada@acme.dev")
    assert await outcome(world.admin(ada, "create_user", email="eve@acme.dev")) == "admin_required"
    assert await outcome(world.admin(ada, "create_team", slug="ops")) == "admin_required"
    await world.admin(world.root, "create_team", slug="ops", name="Operations", admin="ada@acme.dev")
    assert (await world.admin(ada, "whoami"))["teams"] == {"ops": "admin"}
    project = (await world.admin(ada, "create_project", team="ops", slug="runbooks"))["project"]
    node = await world.node(ada, project, "oncall")
    assert [p.ref for p in (await world.map(ada)).stores[0].projects] == [project]
    assert await world.add(ada, node, "Pages escalate after 15 minutes")


async def test_project_and_team_archive_remove_access(org: Org) -> None:
    w = org.w
    entry = await w.add(org.writer, org.node, "Invoices are numbered per account")
    for op, args in [("archive_project", {"project": org.project}), ("archive_team", {"team": "eng"})]:
        await w.admin(w.root, op, **args)
        assert await w.search(org.reader, "invoice") == []
        assert [i.status for i in await w.get(org.reader, entry)] == ["not_found"]
        assert await try_write(w, org.writer, writes(org, entry)["update"]) == "not_found"
        await w.admin(w.root, "un" + op, **args)
        assert [h.ref for h in await w.search(org.reader, "invoice")] == [entry]
