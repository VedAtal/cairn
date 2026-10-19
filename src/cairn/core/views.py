from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from ..db.schema import change_log, entries, links, nodes, users
from ..models import Archived, Change, Graph, GraphEdge, GraphNode, LinkOut
from .access import Scope


async def _visible(conn: AsyncConnection, scope: Scope, pairs: set[tuple[str, int]]) -> dict[tuple[str, int], Any]:
    """Titles (and node ids) of the given items that the reader can see and that aren't archived."""
    out: dict[tuple[str, int], Any] = {}
    for kind, table in (("e", entries), ("n", nodes)):
        ids = [i for k, i in pairs if k == kind]
        if not ids:
            continue
        node_col = table.c.node_id if kind == "e" else table.c.id
        rows = await conn.execute(select(table.c.id, table.c.title, node_col.label("node")).where(
            table.c.id.in_(ids), table.c.is_searchable, table.c.project_id.in_(scope.readable)))
        out |= {(kind, r.id): r for r in rows}
    return out


async def links_of(conn: AsyncConnection, scope: Scope, alias: str, kind: str, item_id: int
                   ) -> tuple[list[LinkOut], list[LinkOut]]:
    outgoing = (await conn.execute(select(links.c.to_kind, links.c.to_id, links.c.reason).where(
        links.c.from_kind == kind, links.c.from_id == item_id).order_by(links.c.id))).all()
    incoming = (await conn.execute(select(links.c.from_kind, links.c.from_id, links.c.reason).where(
        links.c.to_kind == kind, links.c.to_id == item_id).order_by(links.c.id))).all()
    seen = await _visible(conn, scope, {(r[0], r[1]) for r in [*outgoing, *incoming]})
    return ([LinkOut(ref=f"{alias}:{k}{i}", title=seen[(k, i)].title, reason=why)
             for k, i, why in outgoing if (k, i) in seen],
            [LinkOut(ref=f"{alias}:{k}{i}", title=seen[(k, i)].title, reason=why)
             for k, i, why in incoming if (k, i) in seen])


async def changes(conn: AsyncConnection, scope: Scope, alias: str, before: int | None, limit: int) -> list[Change]:
    q = (select(change_log, users.c.email).join(users, users.c.id == change_log.c.by)
         .where(change_log.c.project_id.in_(scope.readable)).order_by(change_log.c.seq.desc()).limit(limit))
    if before:
        q = q.where(change_log.c.seq < before)
    return [Change(seq=r.seq, ref=f"{alias}:{r.ref}", op=r.op, by=r.email.split("@")[0], at=str(r.at)[:19],
                   summary=r.summary) for r in await conn.execute(q)]


async def archived(conn: AsyncConnection, scope: Scope, alias: str, limit: int = 200) -> list[Archived]:
    out = []
    for kind, table in (("e", entries), ("n", nodes)):
        rows = await conn.execute(
            select(table.c.id, table.c.title, table.c.archive_reason, table.c.archived_at, users.c.email)
            .join(users, users.c.id == table.c.archived_by)
            .where(table.c.archived_at.is_not(None), table.c.project_id.in_(scope.readable))
            .order_by(table.c.archived_at.desc()).limit(limit))
        out += [Archived(ref=f"{alias}:{kind}{r.id}", title=r.title, reason=r.archive_reason,
                         by=r.email.split("@")[0], at=str(r.archived_at)[:19]) for r in rows]
    return sorted(out, key=lambda a: a.at, reverse=True)[:limit]


async def graph(conn: AsyncConnection, scope: Scope, alias: str, project_id: int) -> Graph:
    """One project's live nodes, plus cross-links between them (entry links folded onto their nodes)."""
    scope.require_read(project_id)
    rows = (await conn.execute(select(nodes.c.id, nodes.c.parent_id, nodes.c.title, nodes.c.inject).where(
        nodes.c.project_id == project_id, nodes.c.is_searchable).order_by(nodes.c.path))).all()
    counts: dict[int, int] = dict((await conn.execute(select(entries.c.node_id, func.count()).where(
        entries.c.project_id == project_id, entries.c.is_searchable).group_by(entries.c.node_id))).all())
    live = {r.id for r in rows}
    graph_nodes = [GraphNode(ref=f"{alias}:n{r.id}", title=r.title, inject=r.inject, entries=counts.get(r.id, 0),
                             parent=f"{alias}:n{r.parent_id}" if r.parent_id in live else None) for r in rows]
    raw = (await conn.execute(select(links.c.from_kind, links.c.from_id, links.c.to_kind, links.c.to_id,
                                     links.c.reason).where(links.c.project_id == project_id))).all()
    seen = await _visible(conn, scope, {(r[0], r[1]) for r in raw} | {(r[2], r[3]) for r in raw})
    edges: dict[tuple[int, int], list[str]] = {}
    for fk, fi, tk, ti, why in raw:
        a, b = seen.get((fk, fi)), seen.get((tk, ti))
        if a and b and a.node in live and b.node in live and a.node != b.node:
            edges.setdefault((a.node, b.node), []).append(why)
    return Graph(nodes=graph_nodes, edges=[
        GraphEdge(source=f"{alias}:n{s}", target=f"{alias}:n{t}", count=len(w), reasons=w[:3])
        for (s, t), w in edges.items()])
