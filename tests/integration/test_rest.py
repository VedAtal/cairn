from __future__ import annotations

import os

import httpx

from cairn.core.inject import HEADER

from .conftest import SERVER, Api, World


def test_health_and_hello(world: World) -> None:
    assert httpx.get(f"{SERVER}/healthz").json()["status"] == "ok"
    assert httpx.get(f"{SERVER}/readyz").json() == {"status": "ready"}
    hello = httpx.get(f"{SERVER}/api/v1/hello").json()
    assert (hello["store"], hello["mode"]) == (world.alias, "hosted")
    assert Api(world.bob).http.get("/status").json()["store"] == world.alias


def test_key_required() -> None:
    resp = httpx.post(f"{SERVER}/api/v1/search", json={"concepts": ["x"]})
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    Api("cairn_pat_not-a-real-key").post("/search", {"concepts": ["x"]}, status=401)


def test_bootstrap_is_one_time(world: World) -> None:
    body = Api().post("/bootstrap", {"token": os.environ["CAIRN_TEST_SETUP_TOKEN"], "email": "x@acme.dev",
                                     "name": "X"}, status=403)
    assert body["code"] == "already_setup"


def test_write_search_get_map(world: World) -> None:
    bob = Api(world.bob)
    node = bob.node(world.billing, "ledger", "Ledger service", "Double-entry ledger: postings and balances.")
    ref = bob.add(node, "Ledger postings are immutable; corrections are reversing postings",
                  "Never UPDATE a posting row. Write a reversing posting and a new one, so audits replay exactly.")
    assert ref.startswith(f"{world.alias}:e")

    hits = {h["ref"]: h for h in bob.post("/search", {"concepts": ["ledger|journal", "immutable|append-only"]})["hits"]}
    assert hits[ref]["node_ref"] == node
    assert hits[ref]["matched"] == ["ledger (title/keywords)", "immutable (title/keywords)"]

    item = bob.post("/get", {"refs": [ref]})["items"][0]
    assert (item["status"], item["rev"], item["type"]) == ("ok", 1, "gotcha")
    assert "reversing posting" in item["body"]

    updated = bob.post("/write", {"op": "update", "ref": ref, "base_revision": 1,
                                  "entry": {"body": item["body"] + " Applies to fee postings too."}})
    assert updated["rev"] == 2
    stale = bob.post("/write", {"op": "update", "ref": ref, "base_revision": 1, "entry": {"body": "x"}}, status=409)
    assert stale["code"] == "conflict"
    history = bob.post("/get", {"refs": [ref], "history": True})["items"][0]["history"]
    assert [h["rev"] for h in history] == [2, 1]

    projects = bob.post("/map", {})["stores"][0]["projects"]
    assert [(p["ref"], p["role"]) for p in projects] == [(world.billing, "write")]
    assert any(n["ref"] == node and n["entries"] == 1 for n in projects[0]["nodes"])


def test_write_gates(world: World) -> None:
    bob = Api(world.bob)
    node = bob.node(world.billing, "invoices", "Invoices", "Invoice generation and numbering.")
    title = "Invoice numbers must be gapless per legal entity"
    first = bob.add(node, title, "Tax law in several countries requires gapless sequences; allocate at finalization.")
    dup = bob.add(node, title, "Allocate numbers at finalization time, never at draft creation.", status=409)
    assert dup["code"] == "duplicate" and dup["duplicates"][0]["ref"] == first
    resent = bob.post("/write", {"op": "add", "node": node, "distinct_from": [first], "entry": {
        "type": "decision", "title": title, "body": "Allocate numbers at finalization time, never at draft creation."}})
    assert resent["ok"]
    secret = bob.add(node, "Staging invoice API credentials", "password: correct-horse-battery-staple", status=422)
    assert secret["code"] == "secret"


def test_archive_hides_everywhere(world: World) -> None:
    bob = Api(world.bob)
    node = bob.node(world.billing, "refunds", "Refunds", "Refund flows and partial refunds.")
    ref = bob.add(node, "Partial refunds round half-even on the minor unit",
                  "Banker's rounding matches the processor; half-up drifts by a cent over many partial refunds.")
    bob.post("/write", {"op": "archive", "ref": ref, "reason": "processor changed"})
    item = bob.post("/get", {"refs": [ref]})["items"][0]
    assert item["status"] == "archived" and item["tombstone"].startswith(f"{ref} — archived ")
    assert "processor" not in item["tombstone"] and item["body"] is None
    query = {"concepts": ["refund", "rounding|half-even"]}
    assert ref not in [h["ref"] for h in bob.post("/search", query)["hits"]]
    bob.post("/write", {"op": "unarchive", "ref": ref})
    assert ref in [h["ref"] for h in bob.post("/search", query)["hits"]]


def test_inject(world: World) -> None:
    bob, alice = Api(world.bob), Api(world.alice)
    node = bob.node(world.billing, "currency", "Currency handling", "Money types and currency conversion.")
    ref = bob.add(node, "Amounts are integer minor units, never floats",
                  "Store cents as BIGINT with an ISO 4217 code beside them; JPY has no minor unit.", type_="practice")
    assert ref not in bob.post("/inject", {})["text"]
    alice.admin("flag", ref=ref)
    text = bob.post("/inject", {})["text"]
    assert text.startswith(HEADER)
    assert f"practice {ref} (" in text and f"## {world.alias} · platform" in text
    assert len(bob.post("/inject", {"budget": 500})["text"].encode()) <= 500
    alice.admin("unflag", ref=ref)
    assert ref not in bob.post("/inject", {})["text"]


def test_duplicate_team_is_a_conflict(world: World) -> None:
    assert Api(world.admin).post("/admin", {"op": "create_team", "args": {"slug": "platform"}}, 409)["code"] == \
        "conflict"


def test_inject_stays_within_budget(world: World) -> None:
    eve, alice = Api(world.eve), Api(world.alice)
    node = eve.node(world.search, "ranking", "Ranking", "Relevance tuning.")
    for title, body in (
        ("Stemming uses the pinned Snowball English stemmer",
         "Upgrading the stemmer changes tokens for every document, so it ships only with a re-analyzing migration."),
        ("Synonym lists are per tenant and reload hourly",
         "Tenants edit synonyms in the admin console; edits appear after the hourly reload with no reindex at all."),
    ):
        alice.admin("flag", ref=eve.add(node, title, body))
    assert len(eve.post("/inject", {"budget": 600})["text"].encode()) <= 600
