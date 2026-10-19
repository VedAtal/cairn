from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from .conftest import SERVER, Api, Local, World, free_port, mcp_text, start_local, write_config


@pytest.fixture(scope="module")
def local(world: World, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Local]:
    home = tmp_path_factory.mktemp("fed") / ".cairn"
    write_config(home, SERVER, world.bob, world.alias)
    yield from start_local(home)


@pytest.fixture(scope="module")
def orphan(world: World, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Local]:
    """A local server whose remote is down."""
    home = tmp_path_factory.mktemp("orphan") / ".cairn"
    write_config(home, f"http://127.0.0.1:{free_port()}", world.bob, world.alias)
    yield from start_local(home)


@pytest.fixture(scope="module")
def seeded(world: World, local: Local) -> dict[str, str]:
    me, a = Api(local.token, local.url), world.alias
    me_node = me.node("me:p1", "webhooks", "Webhooks", "Personal notes on webhook delivery.")
    remote_node = me.node(world.billing, "webhook_delivery", "Webhook delivery", "Outbound merchant webhooks.")
    assert me_node.startswith("me:n") and remote_node.startswith(f"{a}:n")
    out = {
        "me_node": me_node,
        "remote_node": remote_node,
        "me": me.add(me_node, "Webhook signatures cover the raw body, not parsed JSON",
                     "Verify the HMAC before any JSON parsing; re-serializing reorders keys and breaks it."),
        "remote": me.add(remote_node, "Outbound webhooks give up after 16 attempts over three days",
                         "Exponential backoff capped at 6 h; merchants see the attempts in the dashboard."),
    }
    me.admin("flag", ref=out["me"])
    Api(world.alice).admin("flag", ref=out["remote"])
    return out


def test_write_routes_by_prefix(world: World, local: Local, seeded: dict[str, str]) -> None:
    assert seeded["me"].startswith("me:e") and seeded["remote"].startswith(f"{world.alias}:e")
    on_remote = Api(world.bob).post("/get", {"refs": [seeded["remote"]]})["items"][0]
    assert on_remote["status"] == "ok" and on_remote["title"].startswith("Outbound webhooks")
    items = Api(local.token, local.url).post("/get", {"refs": [seeded["me"], seeded["remote"]]})["items"]
    assert [i["status"] for i in items] == ["ok", "ok"]


def test_search_map_inject_merge(world: World, local: Local, seeded: dict[str, str]) -> None:
    me = Api(local.token, local.url)
    res = me.post("/search", {"concepts": ["webhook|webhooks"]})
    assert {seeded["me"], seeded["remote"]} <= {h["ref"] for h in res["hits"]} and res["unreachable"] == []
    only_me = me.post("/search", {"concepts": ["webhook|webhooks"], "stores": ["me"]})["hits"]
    assert {h["store"] for h in only_me} == {"me"}

    stores = me.post("/map", {})["stores"]
    assert [s["store"] for s in stores] == ["me", world.alias]
    assert [p["ref"] for p in stores[0]["projects"]] == ["me:p1"]
    assert [p["ref"] for p in stores[1]["projects"]] == [world.billing]

    text = me.post("/inject", {})["text"]
    assert "\n## me\n" in text and f"\n## {world.alias} · platform\n" in text
    assert seeded["me"] in text and seeded["remote"] in text


async def test_mcp_through_local(world: World, local: Local, seeded: dict[str, str]) -> None:
    text = await mcp_text(local.token, "search", {"concepts": ["webhook|webhooks", "signature|attempts"]}, local.url)
    assert seeded["me"] in text and seeded["remote"] in text and "unreachable" not in text


def test_local_rejects_foreign_host(local: Local) -> None:
    api, body = Api(local.token, local.url), {"concepts": ["x"]}
    assert api.http.post("/search", json=body, headers={"Host": "evil.test"}).status_code == 421
    assert api.http.post("/search", json=body, headers={"Origin": "https://evil.test"}).status_code == 403
    assert Api("wrong", local.url).http.post("/search", json=body).status_code == 401


async def test_remote_down_is_reported(world: World, orphan: Local) -> None:
    a, me = world.alias, Api(orphan.token, orphan.url)
    assert me.post("/search", {"concepts": ["anything"]})["unreachable"] == [a]
    mapped = me.post("/map", {})
    assert [s["store"] for s in mapped["stores"]] == ["me"] and mapped["unreachable"] == [a]
    assert me.post("/get", {"refs": [f"{a}:e1"]})["unreachable"] == [a]
    assert me.post("/inject", {})["text"].endswith(f"(Cairn store unreachable: {a})")
    assert me.post("/write", {"op": "add", "node": f"{a}:n1", "entry": {"type": "gotcha", "title": "t"}},
                   status=503)["code"] == "unreachable"
    assert (await mcp_text(orphan.token, "search", {"concepts": ["anything"]}, orphan.url)).endswith(
        f"(unreachable: {a})")
    assert await mcp_text(orphan.token, "write", {"op": "add", "node": f"{a}:n1", "entry": {"type": "gotcha"}},
                          orphan.url) == f"error (unreachable): store {a} is unreachable; try again later"


def test_search_within_local_node(local: Local, seeded: dict[str, str]) -> None:
    body = {"concepts": ["webhook|webhooks"], "within": seeded["me_node"]}
    assert [h["ref"] for h in Api(local.token, local.url).post("/search", body)["hits"]] == [seeded["me"]]


def test_local_home_layout(local: Local) -> None:
    home: Path = local.home
    assert (home / "cairn.db").exists()
    assert (home / "run" / "token").stat().st_mode & 0o777 == 0o600
