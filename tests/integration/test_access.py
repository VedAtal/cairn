"""Access rules end to end: project isolation, members without projects, read-only members, admin-only ops, and the
lock on injected content."""

from __future__ import annotations

import pytest

from .conftest import Api, World


@pytest.fixture(scope="module")
def entries(world: World) -> dict[str, str]:
    """One entry in each project, written by its writer."""
    bob, eve = Api(world.bob), Api(world.eve)
    billing_node = bob.node(world.billing, "dunning", "Dunning", "Retrying failed card payments.")
    search_node = eve.node(world.search, "indexing", "Indexing", "How documents reach the search index.")
    return {
        "billing_node": billing_node,
        "billing": bob.add(billing_node, "Dunning emails stop after the fourth failed charge",
                           "The processor marks the card dead after four declines; retrying hurts the merchant score."),
        "search": eve.add(search_node, "Reindexing a tenant takes the shard write lock",
                          "Run tenant reindexes off-peak; the shard rejects writes for the duration."),
    }


def refs(api: Api, concepts: list[str]) -> list[str]:
    return [h["ref"] for h in api.post("/search", {"concepts": concepts})["hits"]]


def test_projects_are_isolated(world: World, entries: dict[str, str]) -> None:
    bob, eve = Api(world.bob), Api(world.eve)
    assert entries["billing"] in refs(bob, ["dunning", "charge|card"])
    assert refs(eve, ["dunning", "charge|card"]) == []
    assert refs(bob, ["reindex|reindexing", "shard"]) == []
    assert entries["search"] in refs(eve, ["reindex|reindexing", "shard"])
    items = eve.post("/get", {"refs": [entries["billing"], entries["billing_node"], world.billing]})["items"]
    assert [i["status"] for i in items] == ["not_found"] * 3
    assert [p["ref"] for p in eve.post("/map", {})["stores"][0]["projects"]] == [world.search]
    # Unreadable looks like absent, for writes too.
    assert eve.add(entries["billing_node"], "Eve was here", "Should not land.", status=404)["code"] == "not_found"


def test_member_without_projects_sees_nothing(world: World, entries: dict[str, str]) -> None:
    carol = Api(world.carol)
    assert refs(carol, ["dunning", "charge|card"]) == []
    assert carol.post("/get", {"refs": [entries["billing"]]})["items"][0]["status"] == "not_found"
    assert carol.post("/map", {})["stores"][0]["projects"] == []
    assert carol.post("/inject", {})["text"] == ""


def test_read_only_member(world: World, entries: dict[str, str]) -> None:
    dave = Api(world.dave)
    assert entries["billing"] in refs(dave, ["dunning", "charge|card"])
    assert dave.post("/get", {"refs": [entries["billing"]]})["items"][0]["status"] == "ok"
    assert [p["role"] for p in dave.post("/map", {})["stores"][0]["projects"]] == ["read"]
    denied = dave.add(entries["billing_node"], "Dave's note on dunning cadence", "Readers cannot write.", status=403)
    assert denied["code"] == "read_only"
    verify = dave.post("/write", {"op": "verify", "ref": entries["billing"]}, status=403)
    assert verify["code"] == "read_only"


def test_admin_ops_need_admin(world: World, entries: dict[str, str]) -> None:
    bob = Api(world.bob)
    assert bob.post("/admin", {"op": "create_user", "args": {"email": "mallory@acme.dev"}}, 403)["code"] == \
        "admin_required"
    assert bob.post("/admin", {"op": "create_project", "args": {"team": "platform", "slug": "x"}}, 403)["code"] == \
        "admin_required"
    assert bob.post("/admin", {"op": "flag", "args": {"ref": entries["billing"]}}, 403)["code"] == "admin_required"


def test_injected_content_is_admin_only(world: World) -> None:
    bob, alice = Api(world.bob), Api(world.alice)
    node = bob.node(world.billing, "payouts", "Payouts", "Merchant payout schedules.")
    child = bob.node(world.billing, "payouts_fx", "Payout FX", "Foreign-exchange conversion for payouts.")
    bob.post("/write", {"op": "move", "ref": child, "node": node})
    ref = bob.add(node, "Payouts settle T+2 business days in the merchant's timezone",
                  "Weekend and bank holidays of the payout country push settlement; the schedule is per merchant.")
    alice.admin("flag", ref=node)

    update = {"op": "update", "ref": ref, "base_revision": 1,
              "entry": {"body": "Settlement moves to T+1 for domestic payouts from next quarter."}}
    assert bob.post("/write", update, status=403)["code"] == "inject_locked"
    assert bob.post("/write", {"op": "archive", "ref": ref}, status=403)["code"] == "inject_locked"
    assert bob.add(child, "Payout FX uses the mid-market rate at cut-off", "Rate snapshot at 16:00 UTC.",
                   status=403)["code"] == "inject_locked"
    assert alice.post("/write", update)["rev"] == 2

    for key in (world.bob, world.dave, world.alice):
        assert ref in Api(key).post("/inject", {})["text"]
    for key in (world.eve, world.carol):
        assert ref not in Api(key).post("/inject", {})["text"]
    alice.admin("unflag", ref=node)
    assert bob.post("/write", update | {"base_revision": 2, "entry": {"body": "Unlocked again."}})["rev"] == 3


def test_verify_is_inject_locked(world: World) -> None:
    bob = Api(world.bob)
    node = bob.node(world.billing, "fees", "Fees", "Processing fee schedules.")
    ref = bob.add(node, "Interchange is passed through at cost plus a fixed margin",
                  "Blended pricing hid scheme fee changes from merchants; pass-through makes them visible.")
    Api(world.alice).admin("flag", ref=ref)
    assert bob.post("/write", {"op": "verify", "ref": ref}, status=403)["code"] == "inject_locked"
