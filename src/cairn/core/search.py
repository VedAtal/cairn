from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import BindParameter, bindparam, func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..db.schema import entries, nodes, projects, teams, term_df, vocab
from .analyzer import Variant, expansion, variant

CAND_LIMIT = 200
NODE_LIMIT = 3
G_CAP = 5000
POSTINGS_BUDGET = 30_000
MIN_NEEDLE = 4
MAX_EXPANSIONS = 16

ENTRY_FIELDS = (("title/keywords", 40), ("body", 10))
NODE_FIELDS = (("node", 30), ("node summary", 20))

# Must match the index expressions in migration 0001, or the GIN indexes go unused.
_TSV = "array_to_tsvector(array_remove(string_to_array({}, ' '), ''))"
_PG_LEAVES = {
    "entry": ("entries", ("an_head", "an_head || ' ' || an_body")),
    "node": ("nodes", ("an_head", "an_desc")),
}
_FTS_LEAVES = {"entry": ("entries", ("an_head", None)), "node": ("nodes", ("an_head", "an_desc"))}
_WEIGHTS = {"entry": (40, 10), "node": (30, 20)}


@dataclass
class Concept:
    variants: list[Variant]
    generic: set[str] = field(default_factory=set)
    scored_only: set[str] = field(default_factory=set)

    def scanned(self) -> list[Variant]:
        return [v for v in self.variants if v.raw not in self.scored_only]


@dataclass
class Hit:
    id: int
    type: str
    title: str
    verified_at: str
    node_id: int
    node_title: str
    node_summary: str
    node_path: str
    project: str
    team: str
    matched: list[str]
    features: tuple[int, int, int, int, int]


@dataclass
class NodeHit:
    id: int
    title: str
    summary: str
    path: str
    project: str
    team: str
    matched: list[str]
    features: tuple[int, int, int, int, int]


@dataclass
class Result:
    hits: list[Hit]
    nodes: list[NodeHit]
    common_only: bool


def parse_concepts(raw: list[str]) -> list[Concept]:
    concepts = []
    for text_ in raw:
        variants = [v for v in (variant(s.strip()) for s in text_.split("|") if s.strip()) if v.chunks]
        if variants:
            concepts.append(Concept(variants))
    return concepts


def token_concepts(tokens: list[str]) -> list[Concept]:
    return [Concept([Variant(raw=t, chunks=((frozenset({t}),),))]) for t in tokens]


async def doc_frequencies(conn: AsyncConnection, terms: set[str], pids: list[int]) -> dict[str, int]:
    if not terms or not pids:
        return {}
    rows = await conn.execute(
        select(term_df.c.term, func.sum(term_df.c.df))
        .where(term_df.c.term.in_(terms), term_df.c.project_id.in_(pids))
        .group_by(term_df.c.term)
    )
    return {term: int(df) for term, df in rows}


async def _expand(conn: AsyncConnection, needle: str, pids: list[int]) -> list[str]:
    # `vocab` has no project column, so the readable filter comes from the join on term_df.
    rows = await conn.execute(
        select(term_df.c.term)
        .join(vocab, vocab.c.term == term_df.c.term)
        .where(vocab.c.term.like(f"%{needle}%"), vocab.c.term != needle, term_df.c.project_id.in_(pids))
        .group_by(term_df.c.term)
        .having(func.sum(term_df.c.df) > 0)
        .order_by(func.sum(term_df.c.df).desc(), term_df.c.term)
        .limit(MAX_EXPANSIONS)
    )
    return list(rows.scalars())


async def plan(conn: AsyncConnection, concepts: list[Concept], pids: list[int]) -> bool:
    terms = {t for c in concepts for v in c.variants for t in v.tokens}
    df = await doc_frequencies(conn, terms, pids)
    total = (await conn.execute(
        select(func.count()).select_from(entries).where(entries.c.is_searchable, entries.c.project_id.in_(pids))
    )).scalar_one()
    g = min(G_CAP, max(50, math.ceil(0.25 * total)))
    for concept in concepts:
        for v in list(concept.variants):
            hits = min((df.get(t, 0) for t in v.tokens), default=0)
            needle = v.needle
            if needle and len(needle) >= MIN_NEEDLE and v.credit == 10 and (v.code_shaped or hits == 0):
                known = {t for x in concept.variants for t in x.tokens}
                concept.variants += [expansion(t) for t in await _expand(conn, needle, pids) if t not in known]
        new_terms = {t for v in concept.variants for t in v.tokens} - df.keys()
        df |= await doc_frequencies(conn, new_terms, pids)
        concept.generic = {v.raw for v in concept.variants if _matches(v, df) > g}
    driving = any(_matches(v, df) > 0 and v.raw not in c.generic for c in concepts for v in c.variants)
    # Postings, not matches: the index reads every token's list, even for alternatives that rarely match.
    common = sorted(((sum(df.get(t, 0) for t in v.tokens), i, v.raw) for i, c in enumerate(concepts)
                     for v in c.variants if v.raw in c.generic), reverse=True)
    if not driving:  # a concept that matches nothing can't be covered, so only an all-common query is AND mode
        return all(c.generic for c in concepts)
    total = sum(n for n, _, _ in common)
    for n, i, raw in common:  # common variants still score in Python; they just stop driving the scan
        if total <= POSTINGS_BUDGET:
            break
        concepts[i].scored_only.add(raw)
        total -= n
    return False


def _matches(v: Variant, df: dict[str, int]) -> int:
    return min((sum(min((df.get(t, 0) for t in alt), default=0) for alt in chunk) for chunk in v.chunks), default=0)


def _pg_query(variants: list[Variant]) -> str:
    def chunk(c: tuple[frozenset[str], ...]) -> str:
        return " | ".join("(" + " & ".join(sorted(alt)) + ")" for alt in c)

    return " | ".join("(" + " & ".join(f"({chunk(c)})" for c in v.chunks) + ")" for v in variants)


def _fts_query(variants: list[Variant], column: str | None) -> str:
    col = f"{column} : " if column else ""

    def chunk(c: tuple[frozenset[str], ...]) -> str:
        return " OR ".join("(" + " AND ".join(f'{col}"{t}"' for t in sorted(alt)) + ")" for alt in c)

    return " OR ".join("(" + " AND ".join(f"({chunk(c)})" for c in v.chunks) + ")" for v in variants)


def _branches(kind: str, dialect: str, concepts: list[Concept], params: dict[str, Any], filters: str) -> list[str]:
    alias = "e" if kind == "entry" else "n"
    cols = f"{alias}.id AS entry_id, {alias}.node_id" if kind == "entry" else f"{alias}.id AS node_id"
    out = []
    for i, concept in enumerate(concepts):
        variants = concept.scanned()
        if not variants:
            continue
        for j, weight in enumerate(_WEIGHTS[kind]):
            name = f"q_{kind}_{i}_{j}"
            if dialect == "postgresql":
                table, exprs = _PG_LEAVES[kind]
                params[name] = _pg_query(variants)
                match_sql = f"FROM {table} {alias} WHERE {_TSV.format(exprs[j])} @@ CAST(:{name} AS tsquery)"
            else:
                table, columns = _FTS_LEAVES[kind]
                params[name] = _fts_query(variants, columns[j])
                match_sql = (f"FROM {table}_fts f JOIN {table} {alias} ON {alias}.id = f.rowid "
                             f"WHERE {table}_fts MATCH :{name}")
            where = f" AND {alias}.is_searchable AND {alias}.project_id IN :pids" + filters
            out.append(f"SELECT {cols}, {i} AS c, {weight} AS w {match_sql}{where}")
    return out


def _subtree(within: tuple[str, int, str | None], path_col: str, params: dict[str, Any]) -> str:
    kind, params["wp"], wpath = within
    if kind != "n":
        return ""
    escaped = (wpath or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    params["wpath"], params["wlike"] = wpath, f"{escaped}/%"
    return f" AND ({path_col} = :wpath OR {path_col} LIKE :wlike ESCAPE '\\')"


async def candidates(
    conn: AsyncConnection,
    concepts: list[Concept],
    pids: list[int],
    *,
    types: list[str] | None = None,
    within: tuple[str, int, str | None] | None = None,
    exclude: list[int] | None = None,
    limit: int = CAND_LIMIT,
    match_all: bool = False,
) -> list[int]:
    if not concepts or not pids:
        return []
    params: dict[str, Any] = {"pids": pids}
    binds: list[BindParameter[Any]] = [bindparam("pids", expanding=True)]
    filters = ""
    if types:
        filters += " AND e.type IN :types"
        params["types"] = types
        binds.append(bindparam("types", expanding=True))
    if within:
        filters += " AND e.project_id = :wp"
        if path := _subtree(within, "path", params):
            filters += f" AND e.node_id IN (SELECT id FROM nodes WHERE project_id = :wp{path})"
    if exclude:
        filters += " AND e.id NOT IN :exclude"
        params["exclude"] = exclude
        binds.append(bindparam("exclude", expanding=True))
    dialect = conn.dialect.name
    em = " UNION ALL ".join(_branches("entry", dialect, concepts, params, filters))
    nm = " UNION ALL ".join(_branches("node", dialect, concepts, params, ""))
    having = f"HAVING COUNT(*) = {len(concepts)}" if match_all else ""
    sql = f"""
        WITH em AS ({em}), nm AS ({nm}),
        cand AS (SELECT DISTINCT entry_id, node_id FROM em),
        hits AS (SELECT entry_id, c, w, 1 AS own FROM em
                 UNION ALL SELECT cand.entry_id, nm.c, nm.w, 0 FROM cand JOIN nm ON nm.node_id = cand.node_id),
        pc AS (SELECT entry_id, c, MAX(w) AS w, MAX(own) AS own FROM hits GROUP BY entry_id, c)
        SELECT entry_id FROM pc GROUP BY entry_id {having}
        ORDER BY COUNT(*) DESC, SUM(own) DESC, SUM(w) DESC, entry_id DESC LIMIT {int(limit)}"""
    rows = await conn.execute(text(sql).bindparams(*binds), params)
    return [int(r) for r in rows.scalars()]


async def node_candidates(conn: AsyncConnection, concepts: list[Concept], pids: list[int],
                          within: tuple[str, int, str | None] | None) -> list[int]:
    if not concepts or not pids:
        return []
    params: dict[str, Any] = {"pids": pids}
    filters = f" AND n.project_id = :wp{_subtree(within, 'n.path', params)}" if within else ""
    nm = " UNION ALL ".join(_branches("node", conn.dialect.name, concepts, params, filters))
    sql = f"""WITH nm AS ({nm}) SELECT node_id FROM nm GROUP BY node_id
              ORDER BY COUNT(DISTINCT c) DESC, MAX(w) DESC, node_id DESC LIMIT 20"""
    rows = await conn.execute(text(sql).bindparams(bindparam("pids", expanding=True)), params)
    return [int(r) for r in rows.scalars()]


def rank_one(concepts: list[Concept], head: set[str], body: set[str], node_head: set[str], node_desc: set[str]
             ) -> tuple[int, int, int, int, list[str]] | None:
    return _coverage(concepts, ((ENTRY_FIELDS[0], head, True), (ENTRY_FIELDS[1], body, True),
                                (NODE_FIELDS[0], node_head, False), (NODE_FIELDS[1], node_desc, False)))


def _coverage(concepts: list[Concept], fields: tuple[tuple[tuple[str, int], set[str], bool], ...]
              ) -> tuple[int, int, int, int, list[str]] | None:
    c = e = s = 0
    variants_hit: set[tuple[int, str]] = set()
    matched: list[str] = []
    own_all_generic = True
    for i, concept in enumerate(concepts):
        best: tuple[int, str, str, bool] | None = None
        own = False
        for v in concept.variants:
            generic = v.raw in concept.generic
            for (name, weight), tokens, is_own in fields:
                if v.matches(tokens):
                    variants_hit.add((i, v.raw))
                    score = weight * v.credit * (1 if generic else 2)
                    if is_own:
                        own = True
                        own_all_generic &= generic
                    if best is None or score > best[0]:
                        best = (score, v.raw, name, is_own)
        if best:
            c += 1
            e += own
            s += best[0]
            matched.append(f"{best[1]} ({best[2]})")
    if e == 0 or (own_all_generic and c < len(concepts)):
        return None
    return c, e, s, len(variants_hit), matched


async def search(
    conn: AsyncConnection,
    concepts: list[Concept],
    pids: list[int],
    *,
    types: list[str] | None = None,
    within: tuple[str, int, str | None] | None = None,
    limit: int = 12,
) -> Result:
    common_only = await plan(conn, concepts, pids)
    ids = await candidates(conn, concepts, pids, types=types, within=within, match_all=common_only)
    hits = await _entry_hits(conn, concepts, pids, ids) if ids else []
    hits.sort(key=sort_key)
    hits = hits[:limit]
    found = [] if types else await _node_hits(conn, concepts, pids, within, {h.node_id for h in hits},
                                               hits[0].features[0] if hits else 1)
    return Result(hits, found, common_only)


async def _entry_hits(conn: AsyncConnection, concepts: list[Concept], pids: list[int], ids: list[int]) -> list[Hit]:
    rows = await conn.execute(
        select(
            entries.c.id, entries.c.type, entries.c.title, entries.c.verified_at, entries.c.an_head, entries.c.an_body,
            nodes.c.id.label("node_id"), nodes.c.title.label("node_title"), nodes.c.summary, nodes.c.path,
            nodes.c.depth, nodes.c.an_head.label("node_head"), nodes.c.an_desc,
            projects.c.slug.label("project"), teams.c.slug.label("team"),
        )
        .join(nodes, nodes.c.id == entries.c.node_id)
        .join(projects, projects.c.id == entries.c.project_id)
        .join(teams, teams.c.id == projects.c.team_id)
        .where(entries.c.id.in_(ids), entries.c.project_id.in_(pids))
    )
    hits = []
    for r in rows.mappings():
        ranked = rank_one(concepts, set(r["an_head"].split()), set(r["an_body"].split()),
                          set(r["node_head"].split()), set(r["an_desc"].split()))
        if ranked:
            c, e, s, v, matched = ranked
            hits.append(Hit(
                id=r["id"], type=r["type"], title=r["title"], verified_at=_date(r["verified_at"]),
                node_id=r["node_id"], node_title=r["node_title"], node_summary=r["summary"], node_path=r["path"],
                project=r["project"], team=r["team"], matched=matched, features=(c, e, s, v, r["depth"]),
            ))
    return hits


async def _node_hits(conn: AsyncConnection, concepts: list[Concept], pids: list[int],
                     within: tuple[str, int, str | None] | None, shown: set[int], best: int) -> list[NodeHit]:
    ids = [i for i in await node_candidates(conn, concepts, pids, within) if i not in shown]
    if not ids:
        return []
    rows = await conn.execute(
        select(nodes.c.id, nodes.c.title, nodes.c.summary, nodes.c.path, nodes.c.depth, nodes.c.an_head,
               nodes.c.an_desc, projects.c.slug.label("project"), teams.c.slug.label("team"))
        .join(projects, projects.c.id == nodes.c.project_id).join(teams, teams.c.id == projects.c.team_id)
        .where(nodes.c.id.in_(ids), nodes.c.project_id.in_(pids)))
    found = []
    for r in rows.mappings():
        ranked = _coverage(concepts, ((NODE_FIELDS[0], set(r["an_head"].split()), True),
                                      (NODE_FIELDS[1], set(r["an_desc"].split()), True)))
        if ranked and ranked[0] >= best:  # a node that covers less than the entries shown adds nothing
            c, e, s, v, matched = ranked
            found.append(NodeHit(id=r["id"], title=r["title"], summary=r["summary"], path=r["path"],
                                 project=r["project"], team=r["team"], matched=matched,
                                 features=(c, e, s, v, r["depth"])))
    found.sort(key=sort_key)
    return found[:NODE_LIMIT]


def sort_key(hit: Hit | NodeHit, store_order: int = 0) -> tuple[int, ...]:
    c, e, s, v, depth = hit.features
    return (-c, -e, -s, -v, -depth, store_order, -hit.id)


def _date(value: Any) -> str:
    return str(value)[:10]
