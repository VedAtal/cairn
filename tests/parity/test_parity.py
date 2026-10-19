"""Cross-backend parity: one corpus written through SqlStore.write on SQLite and on Postgres must dedup and rank
identically. Golden queries also pin down that the obvious best entry ranks first on each backend."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cairn.core.access import NotFound, Principal
from cairn.core.write import WriteError
from tests.conftest import World, open_world, sqlite_url
from tests.parity.corpus import ENTRIES, GOLDEN_FIRST, NODES, PROBES, PROJECTS, QUERIES, Query


@dataclass
class Corpus:
    w: World
    refs: dict[str, str]  # project slugs, node paths and golden keys -> refs
    readers: dict[str, Principal]
    decisions: list[tuple[Any, ...]]  # every write's outcome, in order


async def load(w: World) -> Corpus:
    await w.admin(w.root, "create_team", slug="eng")
    rita = await w.user("rita@acme.dev", team="eng")  # reads two of the three projects
    refs = {slug: await w.project("eng", slug, {"rita@acme.dev": "read"} if slug != "data" else None)
            for slug in PROJECTS}
    for n in NODES:
        parent, _, slug = n.path.rpartition("/")
        refs[n.path] = await w.node(w.root, refs[parent], slug, n.title, n.summary, list(n.keywords))
    decisions: list[tuple[Any, ...]] = []
    for e in ENTRIES:  # on a duplicate, resend with distinct_from as an agent would
        distinct: list[str] = []
        while True:
            try:
                ref = await w.add(w.root, refs[e.node], e.title, e.body, e.type, list(e.keywords), distinct)
                break
            except WriteError as exc:
                assert exc.code == "duplicate", exc
                decisions.append(("duplicate", e.title, [(d.ref, d.score) for d in exc.duplicates]))
                distinct += [d.ref for d in exc.duplicates]
        decisions.append(("ok", e.title, ref))
        if e.key:
            refs[e.key] = ref
    for e in PROBES:
        try:
            decisions.append(("ok", e.title, await w.add(w.root, refs[e.node], e.title, e.body, e.type)))
        except WriteError as exc:
            decisions.append((exc.code, e.title, [(d.ref, d.score) for d in exc.duplicates]))
    return Corpus(w, refs, {"root": w.root, "rita": rita}, decisions)


@pytest.fixture(scope="module")
async def sqlite_corpus(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Corpus]:
    async with open_world(sqlite_url(Path(tmp_path_factory.mktemp("parity")))) as w:
        yield await load(w)


@pytest.fixture(scope="module")
async def pg_corpus(pg_url: str) -> AsyncIterator[Corpus]:
    async with open_world(pg_url) as w:
        yield await load(w)


@pytest.fixture(params=["sqlite", "pg"])
def corpus(request: pytest.FixtureRequest) -> Corpus:
    c: Corpus = request.getfixturevalue(f"{request.param}_corpus")
    return c


async def ranked(c: Corpus, q: Query, who: str = "root") -> list[tuple[Any, ...]]:
    try:
        hits = await c.w.search(c.readers[who], *q.concepts, types=list(q.types) or None,
                                within=c.refs[q.within] if q.within else None, limit=50)
    except NotFound:  # `within` names something this reader can't see
        return [("not_found",)]
    return [(h.ref, h.features, h.matched, h.type, h.node_path, h.project) for h in hits]


def unordered(decisions: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return [(op, title, sorted(out) if isinstance(out, list) else out) for op, title, out in decisions]


def test_dedup_decisions_match(sqlite_corpus: Corpus, pg_corpus: Corpus) -> None:
    assert sqlite_corpus.refs == pg_corpus.refs
    assert unordered(sqlite_corpus.decisions) == unordered(pg_corpus.decisions)
    stored = sum(d[0] == "ok" for d in sqlite_corpus.decisions)
    rejected = sum(d[0] == "duplicate" for d in sqlite_corpus.decisions[-len(PROBES):])
    assert stored >= 300 and rejected >= len(PROBES) // 2  # both paths are actually exercised


def test_dedup_duplicate_order_matches(sqlite_corpus: Corpus, pg_corpus: Corpus) -> None:
    assert sqlite_corpus.decisions == pg_corpus.decisions


@pytest.mark.parametrize("who", ["root", "rita"])
@pytest.mark.parametrize("q", QUERIES, ids=str)
async def test_search_parity(sqlite_corpus: Corpus, pg_corpus: Corpus, q: Query, who: str) -> None:
    assert await ranked(sqlite_corpus, q, who) == await ranked(pg_corpus, q, who)


@pytest.mark.parametrize(("q", "key"), GOLDEN_FIRST, ids=[str(q) for q, _ in GOLDEN_FIRST])
async def test_golden_first(corpus: Corpus, q: Query, key: str) -> None:
    hits = await ranked(corpus, q)
    assert hits and hits[0][0] == corpus.refs[key]


async def test_filters(corpus: Corpus) -> None:
    by_text = {str(q): q for q in QUERIES}
    assert await ranked(corpus, by_text["xylophone|quokka"]) == []
    assert await ranked(corpus, by_text["zzz_unknown_ident"]) == []
    for q in QUERIES:
        hits = await ranked(corpus, q)
        assert all(h[3] in q.types for h in hits) if q.types else True, q
        if q.within:
            project, _, path = q.within.partition("/")
            assert hits and all(h[5] == project and (not path or f"{h[4]}/".startswith(f"{path}/")) for h in hits), q
        expected = [("not_found",)] if q.within.startswith("data") else []
        assert [h for h in await ranked(corpus, q, "rita") if h[-1] == "data" or h == ("not_found",)] == expected, q
