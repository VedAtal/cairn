"""MCP over streamable HTTP with the SDK client: the five tools, their result text and their error text."""

from __future__ import annotations

import json

import httpx

from .conftest import SERVER, World, email, mcp_session, mcp_text


async def test_tools_and_instructions(world: World) -> None:
    async with mcp_session(world.bob) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
    assert set(tools) == {"search", "get", "map", "write", "admin"}
    assert tools["search"].meta == {"anthropic/alwaysLoad": True}
    assert tools["search"].annotations.read_only_hint and not tools["write"].annotations.read_only_hint  # type: ignore[union-attr]
    assert "ALWAYS confirm with the user" in (tools["admin"].description or "")


async def test_mcp_requires_key() -> None:
    resp = httpx.post(f"{SERVER}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 401


async def test_tool_flow(world: World) -> None:
    a = world.alias
    async with mcp_session(world.bob) as s:
        async def call(tool: str, **args: object) -> str:
            return str((await s.call_tool(tool, args)).content[0].text)  # type: ignore[union-attr]

        created = await call("write", op="create_node", project=world.billing,
                             node_fields={"slug": "tax", "title": "Sales tax", "summary": "VAT and sales tax rules."})
        assert created.startswith(f"created: {a}:n") and created.endswith("(rev 1)")
        node = created.split()[1]
        added = await call("write", op="add", node=node, entry={
            "type": "reference", "title": "EU VAT for digital services follows the buyer's country",
            "body": "Since 2015 the MOSS rules tax e-services where the customer lives; collect two location proofs."})
        assert added.startswith(f"added: {a}:e") and added.endswith("(rev 1)")
        ref = added.split()[1]

        found = await call("search", concepts=["vat|sales tax", "digital|e-services"])
        assert f"· Sales tax ({node}) — VAT and sales tax rules." in found
        assert f"  {ref}  reference" in found and "matched: vat (title/keywords)" in found
        assert found.endswith(f"Next: get(refs=[{ref}]) for the entries you need.")

        got = await call("get", refs=[ref, node])
        assert got.startswith(f"{ref} · reference · rev 1 · verified ")
        assert "collect two location proofs" in got and f"{node} · Sales tax (tax) · rev 1" in got

        mapped = await call("map")
        assert mapped.startswith(f"store {a} (you: {email('bob')})")
        assert f"project {world.billing}  platform/Billing (role: write)" in mapped
        assert f"{node}  Sales tax [1] — VAT and sales tax rules." in mapped

        assert json.loads(await call("admin", op="whoami")) == {
            "email": email("bob"), "name": "Bob", "instance_admin": False, "teams": {"platform": "member"}}


async def test_error_text(world: World) -> None:
    a = world.alias
    assert await mcp_text(world.bob, "search", {"concepts": ["zzqx|qqzv"]}) == (
        "No matches in Cairn. An empty result is a normal answer: reformulate once at most, then proceed.")
    assert await mcp_text(world.bob, "get", {"refs": [f"{a}:e999999"]}) == (
        f"{a}:e999999 — not found (or not visible to you)")
    assert (await mcp_text(world.bob, "write", {"op": "add", "node": "nonsense", "entry": {"type": "gotcha"}})
            ).startswith("error (invalid): ")
    assert await mcp_text(world.dave, "write", {
        "op": "create_node", "project": world.billing, "node_fields": {"slug": "dave", "title": "Dave"}}) == (
        "error (read_only): you have read-only access to this project")
    assert await mcp_text(world.bob, "admin", {"op": "create_user", "args": {"email": "m@acme.dev"}}) == (
        "error (admin_required): instance admin required")
    assert (await mcp_text(world.bob, "admin", {"op": "nope"})).startswith("error (invalid): unknown admin op 'nope'")

    async with mcp_session(world.bob) as s:
        res = await s.call_tool("write", {"op": "create_node", "project": world.billing, "node_fields": {
            "slug": "chargebacks", "title": "Chargebacks", "summary": "Disputes raised by card holders."}})
        node = res.content[0].text.split()[1]  # type: ignore[union-attr]
        entry = {"type": "gotcha", "title": "Chargeback evidence is due seven days before the network deadline",
                 "body": "The processor needs a week to forward evidence; submit early or the dispute is lost."}
        first = (await s.call_tool("write", {"op": "add", "node": node, "entry": entry})).content[0].text  # type: ignore[union-attr]
        dup = (await s.call_tool("write", {"op": "add", "node": node, "entry": entry})).content[0].text  # type: ignore[union-attr]
    assert dup.startswith("error (duplicate): possible duplicates found; update one of them, or resend with "
                          "distinct_from\n  " + first.split()[1] + "  (similarity 1.0)  Chargeback evidence")


async def test_validation_error_text(world: World) -> None:
    assert (await mcp_text(world.bob, "search", {"concepts": []})).startswith("error (invalid): ")
