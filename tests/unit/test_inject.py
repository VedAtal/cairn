"""Startup injection: what gets collected, and rendering into a byte budget with fair shares."""

from __future__ import annotations

import re

import pytest

from cairn.core.inject import HEADER, render
from cairn.models import InjectGroup, InjectItem
from tests.conftest import Org


def group(label: str, n: int, body: str = "", title: str = "Fact") -> InjectGroup:
    items = [InjectItem(ref=f"acme:e{i}", type="gotcha", title=f"{title} {i}", body=body or f"Body {i} " * 10,
                        verified_at="2026-10-01") for i in range(1, n + 1)]
    return InjectGroup(label=label, items=items)


def shown(text: str) -> set[str]:
    return set(re.findall(r"^- \w+ (\S+) ", text, re.M))


def not_shown(text: str) -> list[str]:
    match = re.search(r"^Not shown — read with get: (.*?)(?: … and \d+ more)?$", text, re.M)
    return [r for r in re.split(r",\s*", match.group(1).strip(" ,")) if r] if match else []


def more(text: str) -> int:
    match = re.search(r"… and (\d+) more$", text, re.M)
    return int(match.group(1)) if match else 0


def test_everything_fits() -> None:
    text = render([group("acme · eng", 2, body="Use the ledger.")], 8000)
    assert text == (HEADER + "\n## acme · eng\n"
                    "- gotcha acme:e1 (2026-10-01): Fact 1 — Use the ledger.\n"
                    "- gotcha acme:e2 (2026-10-01): Fact 2 — Use the ledger.\n")
    assert render([], 8000) == render([InjectGroup(label="x", items=[])], 8000) == ""


@pytest.mark.parametrize("budget", range(500, 9001, 500))
@pytest.mark.parametrize("groups", [1, 2, 3])
def test_budget_is_respected(budget: int, groups: int) -> None:
    gs = [group(f"g{i}", 200, body="Ünïcödé body text " * 6) for i in range(groups)]
    assert len(render(gs, budget).encode()) <= budget


def test_budget_is_respected_when_bodies_almost_fit() -> None:
    g = group("g", 40, body="Ünïcödé body text " * 6)  # titles fit easily; with bodies it is ~7,300 bytes
    assert len(render([g], 5000).encode()) <= 5000


def test_bodies_are_dropped_before_items() -> None:
    g = group("acme", 10, body="x" * 200)
    text = render([g], 1200)
    assert shown(text) == {i.ref for i in g.items} and " — " not in text and not not_shown(text)


def test_overflow_moves_the_tail_to_not_shown() -> None:
    text = render([group("acme", 60)], 1500)
    n = len(shown(text))
    assert 0 < n < 60 and shown(text) == {f"acme:e{i}" for i in range(1, n + 1)}
    assert not_shown(text)[0] == f"acme:e{n + 1}"


@pytest.mark.parametrize("n", [60, 200])
def test_overflow_lists_every_item_not_shown(n: int) -> None:
    g = group("acme", n)
    text = render([g], 1500)
    listed = shown(text) | set(not_shown(text))
    assert listed <= {i.ref for i in g.items} and len(listed) + more(text) == n  # every item accounted for


def test_fair_shares_across_groups() -> None:
    small, big = group("small", 2, body="short"), group("big", 200)
    text = render([small, big], 4000)
    assert "Fact 1 — short" in text  # the small group keeps its bodies
    big_part = text.split("\n## big\n", 1)[1]
    assert len(big_part.encode()) > (4000 - len(HEADER.encode())) // 2  # and the big one gets the spare bytes
    one, two = group("one", 200, title="One"), group("two", 200, title="Two")
    text = render([one, two], 4000)
    first, second = text.split("\n## two\n")
    assert abs(len(first.encode()) - len(HEADER.encode()) - len(second.encode())) < 200


async def test_collect(org: Org) -> None:
    w = org.w
    child = await w.node(org.writer, org.node, "webhooks", "Webhooks", "Signed with HMAC")
    other = await w.node(org.writer, org.project, "deploys", "Deploys")
    in_node = await w.add(org.writer, org.node, "Invoices are finalized after one hour", type="open_question")
    in_child = await w.add(org.writer, child, "Webhook secrets rotate every 90 days", type="practice")
    alone = await w.add(org.writer, other, "Deploy freezes start on Fridays at noon", type="decision")
    unflagged = await w.add(org.writer, other, "Canary runs for 20 minutes before promotion")
    archived = await w.add(org.writer, child, "Old webhook endpoints were removed in March")
    for ref in (org.node, alone, in_child):  # in_child is covered twice: it must appear once
        await w.admin(org.admin, "flag", ref=ref)
    await w.write(org.admin, "archive", ref=archived)
    [g] = await w.inject(org.reader)
    assert g.label == "acme · eng"
    assert [(i.ref, i.type) for i in g.items] == [
        (org.node, "node"), (in_child, "practice"), (alone, "decision"), (in_node, "open_question")]
    assert g.items[0].body == "Stripe integration: invoices, webhooks"
    assert unflagged not in {i.ref for i in g.items}
    await w.write(org.admin, "archive", ref=org.node)
    assert [i.ref for g in await w.inject(org.reader) for i in g.items] == [alone]
    assert await w.inject(org.member) == []
