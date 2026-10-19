"""Concept-coverage search: the same plan and ranking on Postgres and SQLite.

The agent sends concepts, each one idea written as `a|b|c` variants. The database only answers "which rows' tokens
satisfy this formula" (GIN on Postgres, FTS5 on SQLite) and aggregates coverage to pick ~200 candidates; Python
ranks them on features that depend only on the document, so results also merge cleanly across stores.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import BindParameter, bindparam, func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..db.schema import entries, nodes, projects, teams, term_df, vocab
from .analyzer import Variant, expansion, variant

CAND_LIMIT = 200
G_CAP = 5000
MIN_NEEDLE = 4
MAX_EXPANSIONS = 16

# (field, weight). Entry fields count as the entry's "own" coverage; node fields only add to it.
ENTRY_FIELDS = (("title/keywords", 40), ("body", 10))
NODE_FIELDS = (("node", 30), ("node summary", 20))

# Postgres index expressions; they must match the ones in migration 0001 for the GIN indexes to be used.
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
    generic: set[str] = field(default_factory=set)  # raw spellings of variants whose every token is common


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
    features: tuple[int, int, int, int, int]  # C, E, S, V, depth


def parse_concepts(raw: list[str]) -> list[Concept]:
    concepts = []
    for text_ in raw:
        variants = [v for v in (variant(s.strip()) for s in text_.split("|") if s.strip()) if v.chunks]
        if variants:
            concepts.append(Concept(variants))
    return concepts


def token_concepts(tokens: list[str]) -> list[Concept]:
    """Concepts built from already-analyzed tokens (the dedup gate passes tokens, never re-analyzes them)."""
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


async def plan(conn: AsyncConnection, concepts: list[Concept], pids: list[int]) -> None:
    """Add substring expansions and mark generic variants, in place."""
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
        concept.generic = {v.raw for v in concept.variants if v.tokens and all(df.get(t, 0) > g for t in v.tokens)}


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
        for j, weight in enumerate(_WEIGHTS[kind]):
            name = f"q_{kind}_{i}_{j}"
            if dialect == "postgresql":
                table, exprs = _PG_LEAVES[kind]
                params[name] = _pg_query(concept.variants)
                match_sql = f"FROM {table} {alias} WHERE {_TSV.format(exprs[j])} @@ CAST(:{name} AS tsquery)"
            else:
                table, columns = _FTS_LEAVES[kind]
                params[name] = _fts_query(concept.variants, columns[j])
                match_sql = (f"FROM {table}_fts f JOIN {table} {alias} ON {alias}.id = f.rowid "
                             f"WHERE {table}_fts MATCH :{name}")
            where = f" AND {alias}.is_searchable AND {alias}.project_id IN :pids" + (filters if kind == "entry" else "")
            out.append(f"SELECT {cols}, {i} AS c, {weight} AS w {match_sql}{where}")
    return out


async def candidates(
    conn: AsyncConnection,
    concepts: list[Concept],
    pids: list[int],
    *,
    types: list[str] | None = None,
    within: tuple[str, int, str | None] | None = None,  # ("p", project_id, None) | ("n", project_id, path)
    exclude: list[int] | None = None,
    limit: int = CAND_LIMIT,
) -> list[int]:
    """Entry ids ordered by concept coverage (concepts covered, then own coverage, then weight)."""
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
        kind, wp, wpath = within
        params["wp"] = wp
        filters += " AND e.project_id = :wp"
        if kind == "n":
            escaped = (wpath or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params["wpath"], params["wlike"] = wpath, f"{escaped}/%"
            filters += (" AND e.node_id IN (SELECT id FROM nodes WHERE project_id = :wp"
                        " AND (path = :wpath OR path LIKE :wlike ESCAPE '\\'))")
    if exclude:
        filters += " AND e.id NOT IN :exclude"
        params["exclude"] = exclude
        binds.append(bindparam("exclude", expanding=True))
    dialect = conn.dialect.name
    em = " UNION ALL ".join(_branches("entry", dialect, concepts, params, filters))
    nm = " UNION ALL ".join(_branches("node", dialect, concepts, params, ""))
    sql = f"""
        WITH em AS ({em}), nm AS ({nm}),
        cand AS (SELECT DISTINCT entry_id, node_id FROM em),
        hits AS (SELECT entry_id, c, w, 1 AS own FROM em
                 UNION ALL SELECT cand.entry_id, nm.c, nm.w, 0 FROM cand JOIN nm ON nm.node_id = cand.node_id),
        pc AS (SELECT entry_id, c, MAX(w) AS w, MAX(own) AS own FROM hits GROUP BY entry_id, c)
        SELECT entry_id FROM pc GROUP BY entry_id
        ORDER BY COUNT(*) DESC, SUM(own) DESC, SUM(w) DESC, entry_id DESC LIMIT {int(limit)}"""
    rows = await conn.execute(text(sql).bindparams(*binds), params)
    return [int(r) for r in rows.scalars()]


def rank_one(concepts: list[Concept], head: set[str], body: set[str], node_head: set[str], node_desc: set[str]
             ) -> tuple[int, int, int, int, list[str]] | None:
    """Features (C, E, S, V) and matched labels for one candidate, or None if it should be dropped."""
    fields = ((ENTRY_FIELDS[0], head, True), (ENTRY_FIELDS[1], body, True),
              (NODE_FIELDS[0], node_head, False), (NODE_FIELDS[1], node_desc, False))
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
) -> list[Hit]:
    await plan(conn, concepts, pids)
    ids = await candidates(conn, concepts, pids, types=types, within=within)
    if not ids:
        return []
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
    hits.sort(key=sort_key)
    return hits[:limit]


def sort_key(hit: Hit, store_order: int = 0) -> tuple[int, ...]:
    c, e, s, v, depth = hit.features
    return (-c, -e, -s, -v, -depth, store_order, -hit.id)


def _date(value: Any) -> str:
    return str(value)[:10]
