"""Admin ops: accounts, keys, membership, projects and flags."""

from __future__ import annotations

from cairn.core import admin
from tests.conftest import Org, World, outcome


async def key_works(w: World, key: str) -> bool:
    async with w.store.engine.connect() as conn:
        return await admin.principal_for_key(conn, key) is not None


async def test_bootstrap_is_one_time(world: World) -> None:
    async with world.store.engine.begin() as conn:
        assert await admin.has_instance_admin(conn)
        assert await outcome(admin.bootstrap(conn, "test-setup-token", "x@acme.dev", "X")) == "already_setup"


async def test_users(org: Org) -> None:
    w = org.w
    users = (await w.admin(w.root, "users"))["users"]
    assert [u["email"] for u in users] == ["root@acme.dev", "ada@acme.dev", "wes@acme.dev", "rita@acme.dev",
                                           "otto@acme.dev"]
    assert await outcome(w.admin(org.admin, "users")) == "admin_required"
    assert await outcome(w.admin(w.root, "create_user", email="WES@acme.dev")) == "conflict"
    assert await outcome(w.admin(w.root, "create_user", email="not-an-email")) == "invalid"
    key = (await w.admin(w.root, "create_user", email="eve@acme.dev", name="Eve"))["api_key"]
    assert key.startswith("cairn_pat_") and await key_works(w, key)
    await w.admin(w.root, "set_user_active", email="eve@acme.dev", active=False)
    assert not await key_works(w, key)
    await w.admin(w.root, "set_user_active", email="eve@acme.dev", active=True)
    assert await key_works(w, key)


async def test_keys(org: Org) -> None:
    w = org.w
    key = (await w.admin(org.writer, "create_key", name="laptop"))["api_key"]
    assert await key_works(w, key)
    assert (await w.admin(org.reader, "revoke_key", prefix=key[:16]))["revoked"] == 0  # not rita's key
    assert (await w.admin(org.writer, "revoke_key", prefix=key[:16]))["revoked"] == 1
    assert not await key_works(w, key)
    other = (await w.admin(org.reader, "create_key"))["api_key"]
    assert (await w.admin(w.root, "revoke_key", prefix=other[:16]))["revoked"] == 1  # the instance admin may


async def test_whoami(org: Org) -> None:
    assert await org.w.admin(org.admin, "whoami") == {"email": "ada@acme.dev", "name": "ada@acme.dev",
                                                      "instance_admin": False, "teams": {"eng": "admin"}}
    assert (await org.w.admin(org.w.root, "whoami"))["instance_admin"] is True


async def test_membership(org: Org) -> None:
    w = org.w
    assert (await w.admin(org.reader, "members", project=org.project))["members"] == {
        "wes@acme.dev": "write", "rita@acme.dev": "read"}
    assert (await w.admin(org.reader, "members", team="eng"))["members"] == {
        "ada@acme.dev": "admin", "wes@acme.dev": "member", "rita@acme.dev": "member", "otto@acme.dev": "member"}
    for args in ({"project": org.project}, {"team": "eng"}):  # in zero projects: sees nothing
        assert await outcome(w.admin(org.member, "members", **args)) == "not_found"
    for args, result in [
        ({"email": "otto@acme.dev", "role": "owner"}, "invalid"),
        ({"email": "nobody@acme.dev", "role": "read"}, "not_found"),
        ({"email": "otto@acme.dev", "role": "read"}, "ok"),
    ]:
        assert await outcome(w.admin(org.admin, "set_project_member", project=org.project, **args)) == result
    assert await outcome(w.admin(org.writer, "set_project_member", project=org.project, email="otto@acme.dev",
                                 role="write")) == "admin_required"
    entry = await w.add(org.writer, org.node, "Invoices are numbered per account")
    assert [h.ref for h in await w.search(org.member, "invoice")] == [entry]
    await w.admin(org.admin, "set_project_member", project=org.project, email="otto@acme.dev", role=None)
    assert await w.search(org.member, "invoice") == []
    # Removing a team admin's team role takes away their implicit access.
    await w.admin(w.root, "set_team_member", team="eng", email="ada@acme.dev", role=None)
    assert await w.search(org.admin, "invoice") == []


async def test_team_admins_manage_only_their_team(org: Org) -> None:
    w = org.w
    await w.admin(w.root, "create_team", slug="ops")
    ops_project = (await w.admin(w.root, "create_project", team="ops", slug="runbooks"))["project"]
    for op, args, result in [
        ("create_project", {"team": "ops", "slug": "mine"}, "not_found"),  # another team is invisible
        ("set_team_member", {"team": "ops", "email": "otto@acme.dev", "role": "member"}, "not_found"),
        ("set_project_member", {"project": ops_project, "email": "otto@acme.dev", "role": "read"}, "not_found"),
        ("archive_project", {"project": ops_project}, "not_found"),
        ("create_project", {"team": "eng", "slug": "mine"}, "ok"),
        ("set_team_member", {"team": "eng", "email": "otto@acme.dev", "role": "admin"}, "admin_required"),
        ("set_team_member", {"team": "eng", "email": "otto@acme.dev", "role": "member"}, "ok"),
        ("bogus", {}, "invalid"),
    ]:
        assert await outcome(w.admin(org.admin, op, **args)) == result, op


async def test_archived_project_can_be_unarchived_by_its_team_admin(org: Org) -> None:
    w = org.w
    await w.admin(org.admin, "archive_project", project=org.project)
    assert (await w.map(org.admin)).stores[0].projects == []
    await w.admin(org.admin, "unarchive_project", project=org.project)
    assert [p.ref for p in (await w.map(org.admin)).stores[0].projects] == [org.project]


async def test_flags(org: Org) -> None:
    w = org.w
    entry = await w.add(org.writer, org.node, "Invoices are numbered per account")
    assert await outcome(w.admin(org.writer, "flag", ref=entry)) == "admin_required"
    assert await outcome(w.admin(org.member, "flag", ref=entry)) == "not_found"
    for op, ref in [("flag", entry), ("flag", org.node)]:
        assert (await w.admin(org.admin, op, ref=ref))["inject"] is True
    assert [i.inject for i in await w.get(org.reader, entry, org.node)] == [True, True]
    await w.admin(org.admin, "unflag", ref=entry)
    assert [i.inject for i in await w.get(org.reader, entry, org.node)] == [False, True]
