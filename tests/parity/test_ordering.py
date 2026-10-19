"""SQLite vs Postgres parity for result ordering (map, node outlines, injection)."""

from __future__ import annotations

from pathlib import Path

from cairn.models import NodeFields
from tests.conftest import World, open_world, sqlite_url
from tests.parity.test_concurrency import (
    make_org,
    pgw,  # noqa: F401 - fixture
)


async def scenario(w: World) -> tuple[list[str], list[str], list[str], list[str]]:
    org = await make_org(w)
    nodes = {}
    for slug in ("ab", "a_c", "billing_old"):
        nodes[slug] = await w.node(org.admin, org.project, slug, title=slug)
    nodes["billing/x"] = await w.node(org.admin, org.node, "x", title="billing/x")
    tree = (await w.map(org.admin, depth=2)).stores[0].projects[0].nodes
    map_order = [n.title for n in tree]
    [outline] = await w.get(org.admin, org.project)
    outline_order = [c.title for c in outline.children]
    # Injection: two flagged nodes, the first one edited afterwards.
    await w.admin(org.admin, "flag", ref=nodes["ab"])
    await w.admin(org.admin, "flag", ref=nodes["a_c"])
    await w.write(org.admin, "update_node", ref=nodes["ab"], base_revision=1,
                  node_fields=NodeFields(summary="Edited after flagging"))
    groups = await w.inject(org.admin)
    inject_order = [i.title for g in groups for i in g.items]
    users = [u["email"] for u in (await w.admin(w.root, "users"))["users"]]
    return map_order, outline_order, inject_order, users


async def test_ordering_parity(pgw: World, tmp_path: Path) -> None:  # noqa: F811
    async with open_world(sqlite_url(tmp_path)) as sw:
        lite = await scenario(sw)
    pg = await scenario(pgw)
    for name, a, b in zip(("map", "project outline", "inject", "users"), lite, pg, strict=True):
        print(f"{name:16} sqlite={a}\n{'':16} postgres={b}")
    assert lite == pg
