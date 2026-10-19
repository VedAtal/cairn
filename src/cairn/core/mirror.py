from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from ..db.schema import entries, entry_revisions, links, meta, nodes, projects, team_members, teams, users
from .analyzer import analyzed_text
from .write import _SLUG, _apply_df, _delta, _entry_tokens, _fts

FORMAT = 1
# Local stores have one user, so the *_by columns are implied; git history stands in for revisions and the log.
TEAM_KEYS = ("id", "slug", "name", "archived_at")
PROJECT_KEYS = ("id", "slug", "name", "archived_at")
NODE_KEYS = ("id", "slug", "title", "keywords", "rev", "inject", "created_at", "updated_at", "archived_at",
             "archive_reason", "links")
ENTRY_KEYS = ("id", "type", "title", "keywords", "rev", "inject", "created_at", "updated_at", "verified_at",
              "archived_at", "archive_reason", "links")
_TIMES = {"created_at", "updated_at", "verified_at", "archived_at"}
_FILES = {"_team.md": "team", "_project.md": "project", "_node.md": "node"}
# Set once a store has written (or been restored from) the mirror; a store without it must not replace one.
CLAIM = "git_mirror"


def _doc(keys: tuple[str, ...], row: dict[str, Any], body: str = "") -> str:
    def value(k: str) -> Any:
        v = row.get(k)
        return v.isoformat() if k in _TIMES and v is not None else v

    head = "".join(f"{k}: {json.dumps(value(k), ensure_ascii=False)}\n" for k in keys)
    return f"---\n{head}---\n{body}\n"


def parse(doc: str) -> tuple[dict[str, Any], str]:
    head, body = doc.removeprefix("---\n").split("\n---\n", 1)
    # Not splitlines(): JSON leaves U+2028 and U+0085 raw, and splitlines() breaks on them.
    fields = {k: json.loads(v) for k, v in (line.split(": ", 1) for line in head.split("\n"))}
    for k in _TIMES & fields.keys():
        fields[k] = datetime.fromisoformat(fields[k]) if fields[k] else None
    return fields, body.removesuffix("\n")


async def render(conn: AsyncConnection) -> dict[str, str]:
    """The whole store as {path: text}; unchanged data renders byte-for-byte the same."""
    out = {"cairn.json": json.dumps({"format": FORMAT}) + "\n"}
    outgoing: dict[tuple[str, int], list[dict[str, str]]] = {}
    for r in await conn.execute(select(links).order_by(links.c.id)):
        outgoing.setdefault((r.from_kind, r.from_id), []).append({"to": f"{r.to_kind}{r.to_id}", "reason": r.reason})
    team_dir = {}
    for t in await conn.execute(select(teams)):
        team_dir[t.id] = t.slug
        out[f"{t.slug}/_team.md"] = _doc(TEAM_KEYS, dict(t._mapping))
    project_dir = {}
    for p in await conn.execute(select(projects)):
        project_dir[p.id] = f"{team_dir[p.team_id]}/{p.slug}"
        out[f"{project_dir[p.id]}/_project.md"] = _doc(PROJECT_KEYS, dict(p._mapping))
    node_dir = {}
    for n in await conn.execute(select(nodes)):
        node_dir[n.id] = f"{project_dir[n.project_id]}/{n.path}"
        out[f"{node_dir[n.id]}/_node.md"] = _doc(NODE_KEYS, {**n._mapping, "links": outgoing.get(("n", n.id), [])},
                                                 n.summary)
    for e in await conn.execute(select(entries)):
        out[f"{node_dir[e.node_id]}/{e.type}-e{e.id}.md"] = _doc(
            ENTRY_KEYS, {**e._mapping, "links": outgoing.get(("e", e.id), [])}, e.body)
    return out


async def restore(conn: AsyncConnection, files: dict[str, str], user_id: int) -> None:
    """Rebuild an empty local store from a rendered tree, keeping every id so refs stay valid."""
    if json.loads(files.get("cairn.json", "{}")).get("format") != FORMAT:
        raise ValueError("not a Cairn mirror (cairn.json missing, or written by a newer Cairn)")
    docs: dict[str, list[tuple[list[str], dict[str, Any], str]]] = {}
    for path, doc in files.items():
        parts = path.split("/")
        if not all(_SLUG.match(part) for part in parts[:-1]):
            raise ValueError(f"unexpected path in mirror: {path}")
        kind = _FILES.get(parts[-1]) or ("entry" if parts[-1].endswith(".md") and "-e" in parts[-1] else None)
        if kind:
            docs.setdefault(kind, []).append((parts, *parse(doc)))
    team_id: dict[str, int] = {}
    project_id: dict[tuple[str, ...], int] = {}
    node_id: dict[tuple[int, str], int] = {}
    owner_project: dict[tuple[str, int], int] = {}
    for parts, f, _ in docs.get("team", []):
        await conn.execute(teams.insert().values(**f))
        await conn.execute(team_members.insert().values(team_id=f["id"], user_id=user_id, role="admin"))
        team_id[parts[0]] = f["id"]
    for parts, f, _ in docs.get("project", []):
        await conn.execute(projects.insert().values(**f, team_id=team_id[parts[0]]))
        project_id[tuple(parts[:2])] = f["id"]
    for parts, f, summary in sorted(docs.get("node", []), key=lambda d: len(d[0])):  # parents first
        pid, path = project_id[tuple(parts[:2])], "/".join(parts[2:-1])
        an = {"an_head": analyzed_text(f["title"], *f["keywords"]),
              "an_desc": analyzed_text(summary, path.replace("/", " "))}
        await conn.execute(nodes.insert().values(
            **{k: f[k] for k in NODE_KEYS[:-1]}, **an, summary=summary, path=path, depth=len(parts) - 3,
            project_id=pid, parent_id=node_id.get((pid, "/".join(parts[2:-2]))), created_by=user_id,
            updated_by=user_id, archived_by=user_id if f["archived_at"] else None, is_searchable=True))
        node_id[(pid, path)], owner_project[("n", f["id"])] = f["id"], pid
        await _fts(conn, "nodes", f["id"], an)
    for parts, f, body in docs.get("entry", []):
        pid = project_id[tuple(parts[:2])]
        an = {"an_head": analyzed_text(f["title"], *f["keywords"]), "an_body": analyzed_text(body)}
        await conn.execute(entries.insert().values(
            **{k: f[k] for k in ENTRY_KEYS[:-1]}, **an, body=body, project_id=pid,
            node_id=node_id[(pid, "/".join(parts[2:-1]))], created_by=user_id, updated_by=user_id,
            verified_by=user_id, archived_by=user_id if f["archived_at"] else None, is_searchable=True))
        await conn.execute(entry_revisions.insert().values(
            entry_id=f["id"], rev=f["rev"], type=f["type"], title=f["title"], body=body, keywords=f["keywords"],
            by=user_id, at=f["updated_at"]))
        owner_project[("e", f["id"])] = pid
        await _fts(conn, "entries", f["id"], an)
    for kind in ("node", "entry"):
        for _, f, _ in docs.get(kind, []):
            for link in f["links"]:
                await conn.execute(links.insert().values(
                    project_id=owner_project[(kind[0], f["id"])], from_kind=kind[0], from_id=f["id"],
                    to_kind=link["to"][0], to_id=int(link["to"][1:]), reason=link["reason"], created_by=user_id,
                    created_at=f["updated_at"]))
    await _searchable(conn)
    await claim(conn)


async def claimed(conn: AsyncConnection) -> bool:
    return (await conn.execute(select(meta.c.key).where(meta.c.key == CLAIM))).first() is not None


async def claim(conn: AsyncConnection) -> None:
    await conn.execute(meta.insert().values(key=CLAIM, value="1"))


async def _searchable(conn: AsyncConnection) -> None:
    dead = {(r.project_id, r.path) for r in await conn.execute(
        select(nodes.c.project_id, nodes.c.path).where(nodes.c.archived_at.is_not(None)))}
    live = set()
    for n in await conn.execute(select(nodes.c.id, nodes.c.project_id, nodes.c.path)):
        parts = n.path.split("/")
        if any((n.project_id, "/".join(parts[: i + 1])) in dead for i in range(len(parts))):
            await conn.execute(nodes.update().where(nodes.c.id == n.id).values(is_searchable=False))
        else:
            live.add(n.id)
    deltas: dict[int, Counter[str]] = {}
    for e in (await conn.execute(select(entries))).mappings().all():
        if e["node_id"] in live and e["archived_at"] is None:
            deltas.setdefault(e["project_id"], Counter()).update(_delta(_entry_tokens(e), +1))
        else:
            await conn.execute(entries.update().where(entries.c.id == e["id"]).values(is_searchable=False))
    for pid, counter in deltas.items():
        await _apply_df(conn, pid, counter)


async def owner(conn: AsyncConnection) -> int:
    return int((await conn.execute(select(users.c.id).where(users.c.is_instance_admin))).scalar_one())
