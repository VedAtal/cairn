from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Row, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from ..db.schema import change_log, entries, entry_revisions, links, nodes, term_df, users, vocab
from ..models import ENTRY_TYPES, Duplicate, EntryFields, NodeFields
from .access import AccessError, NotFound, Scope, inject_locked
from .analyzer import analyze, analyzed_text, is_opaque
from .search import candidates, doc_frequencies, token_concepts

MAX_TITLE, MAX_BODY, MAX_KEYWORDS, MAX_SUMMARY, MAX_NODE_KEYWORDS, MAX_DEPTH = 160, 2000, 8, 400, 12, 8
MAX_LINKS, MAX_REASON = 10, 200
type Links = tuple[list[tuple[str, int, str]], list[tuple[str, int]]] | None
DUP_THRESHOLD = 0.6
WRITE_QUOTA = 500  # changes per user per hour on a hosted store
_SLUG = re.compile(r"^[a-z0-9_]{1,48}$")
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_SECRETS = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "Cairn API key": re.compile(r"\bcairn_pat_[A-Za-z0-9_-]{20,}"),
    "API key assignment": re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*['\"]?[A-Za-z0-9/+_\-]{16,}"
    ),
    "bearer token": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),
}


class WriteError(Exception):
    def __init__(self, message: str, code: str = "invalid", duplicates: list[Duplicate] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.duplicates = duplicates or []


def now() -> datetime:
    return datetime.now(UTC)


def clean(value: str) -> str:
    value = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    return _INVISIBLE.sub("", value).strip()


def _keywords(values: list[str], limit: int) -> list[str]:
    out: list[str] = []
    for kw in (clean(v) for v in values):
        if kw and kw.lower() not in (o.lower() for o in out):
            out.append(kw[:64])
    if len(out) > limit:
        raise WriteError(f"at most {limit} keywords")
    return out


# Three variables per term: one upsert must stay under SQLite's default cap of 32766.
DF_CHUNK = 5000
_RANDOMISH = re.compile(r"[A-Za-z0-9+/=_-]{32,}")


def _entropy(value: str) -> float:
    return -sum(n / len(value) * math.log2(n / len(value)) for n in Counter(value).values())


def _check_secrets(*texts: str) -> None:
    for value in texts:
        if any(_entropy(m) >= 4.0 for m in _RANDOMISH.findall(value)):
            raise WriteError("refused: contains a long random-looking token (a key or secret?). "
                             "Never store credentials.", "secret")
        for name, pattern in _SECRETS.items():
            if pattern.search(value):
                raise WriteError(f"refused: looks like it contains a secret ({name}). Never store credentials.",
                                 "secret")


def validate_entry(fields: EntryFields, current: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = dict(current or {}) | {k: v for k, v in fields.model_dump().items() if v is not None}
    title, body = clean(merged.get("title") or ""), clean(merged.get("body") or "")
    if merged.get("type") not in ENTRY_TYPES:
        raise WriteError(f"type must be one of {', '.join(ENTRY_TYPES)}")
    if not title:
        raise WriteError("title is required: one line stating the fact")
    if len(title) > MAX_TITLE or len(body) > MAX_BODY:
        raise WriteError(f"title must be ≤ {MAX_TITLE} characters and body ≤ {MAX_BODY}; write one fact per entry")
    keywords = _keywords(merged.get("keywords") or [], MAX_KEYWORDS)
    title_words = set(analyze(title))
    keywords = [k for k in keywords if not set(analyze(k)) <= title_words]
    _check_secrets(title, body, *keywords)
    return {"type": merged["type"], "title": title, "body": body, "keywords": keywords}


def validate_node(fields: NodeFields, current: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = dict(current or {}) | {k: v for k, v in fields.model_dump().items() if v is not None}
    title, summary = clean(merged.get("title") or ""), clean(merged.get("summary") or "")
    slug = merged.get("slug") or ""
    if not _SLUG.match(slug):
        raise WriteError("slug must match [a-z0-9_]{1,48}")
    if not title or len(title) > MAX_TITLE or len(summary) > MAX_SUMMARY:
        raise WriteError(f"node needs a title ≤ {MAX_TITLE} characters and a summary ≤ {MAX_SUMMARY}")
    keywords = _keywords(merged.get("keywords") or [], MAX_NODE_KEYWORDS)
    _check_secrets(title, summary, *keywords)
    return {"slug": slug, "title": title, "summary": summary, "keywords": keywords}


async def _apply_df(conn: AsyncConnection, project_id: int, deltas: Counter[str]) -> None:
    """Sorted upserts: rows locked out of order across transactions could deadlock."""
    changed = sorted(t for t, d in deltas.items() if d)
    insert = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
    for i in range(0, len(changed), DF_CHUNK):
        chunk = changed[i : i + DF_CHUNK]
        stmt = insert(term_df).values([{"term": t, "project_id": project_id, "df": deltas[t]} for t in chunk])
        await conn.execute(stmt.on_conflict_do_update(index_elements=["term", "project_id"],
                                                      set_={"df": term_df.c.df + stmt.excluded.df}))
        words = [{"term": t} for t in chunk if deltas[t] > 0 and not is_opaque(t)]
        if words:
            await conn.execute(insert(vocab).values(words).on_conflict_do_nothing())


def _delta(tokens: set[str], sign: int) -> Counter[str]:
    return Counter({t: sign for t in tokens})


def _entry_tokens(row: Any) -> set[str]:
    return set(row["an_head"].split()) | set(row["an_body"].split())


async def _fts(conn: AsyncConnection, table: str, row_id: int, cols: dict[str, str] | None) -> None:
    if conn.dialect.name != "sqlite":
        return
    await conn.execute(text(f"DELETE FROM {table}_fts WHERE rowid = :id"), {"id": row_id})
    if cols is not None:
        names = ", ".join(cols)
        await conn.execute(text(f"INSERT INTO {table}_fts (rowid, {names}) VALUES (:id, :{', :'.join(cols)})"),
                           {"id": row_id, **cols})


async def _log(conn: AsyncConnection, project_id: int, ref: str, op: str, user: int, summary: str) -> None:
    await conn.execute(change_log.insert().values(project_id=project_id, ref=ref, op=op, by=user, at=now(),
                                                  summary=summary[:200]))


async def check_quota(conn: AsyncConnection, user_id: int, limit: int) -> None:
    await conn.execute(select(users.c.id).where(users.c.id == user_id).with_for_update())  # exact under concurrency
    recent = (await conn.execute(select(func.count()).select_from(change_log).where(
        change_log.c.by == user_id, change_log.c.at > now() - timedelta(hours=1)))).scalar_one()
    if recent >= limit:
        raise WriteError(f"write quota reached ({limit} changes an hour); try again later", "quota")


async def get_node(conn: AsyncConnection, scope: Scope, node_id: int, lock: bool = False) -> Row[Any]:
    q = select(nodes).where(nodes.c.id == node_id)
    node = (await conn.execute(q.with_for_update() if lock else q)).first()
    if node is None:
        raise NotFound()
    scope.require_read(node.project_id)
    return node


async def get_entry(conn: AsyncConnection, scope: Scope, entry_id: int, lock: bool = False
                    ) -> tuple[Row[Any], Row[Any]]:
    q = select(entries).where(entries.c.id == entry_id)
    entry = (await conn.execute(q.with_for_update() if lock else q)).first()
    if entry is None:
        raise NotFound()
    return entry, await get_node(conn, scope, entry.node_id)


async def _guard(conn: AsyncConnection, scope: Scope, node: Row[Any], entry: Row[Any] | None = None,
                 subtree: bool = False) -> None:
    scope.require_write(node.project_id)
    if scope.is_team_admin(scope.project_team[node.project_id]):
        return
    locked = (entry is not None and entry.inject) or await inject_locked(conn, node.path, node.project_id)
    if not locked and subtree:
        below = nodes.c.path.startswith(f"{node.path}/", autoescape=True)  # autoescape: "_" is a LIKE wildcard
        flagged_node = select(nodes.c.id).where(nodes.c.project_id == node.project_id, nodes.c.inject, below)
        sub = select(nodes.c.id).where(nodes.c.project_id == node.project_id, (nodes.c.path == node.path) | below)
        flagged_entry = select(entries.c.id).where(entries.c.node_id.in_(sub), entries.c.inject)
        locked = (await conn.execute(flagged_node.union(flagged_entry).limit(1))).first() is not None
    if locked:
        raise AccessError("this content is injected into every session, so only admins can change it", "inject_locked")


def _live(node: Row[Any]) -> None:
    if not node.is_searchable:
        raise WriteError("that node is archived (or inside an archived node); unarchive it first", "archived")


async def _dedup(conn: AsyncConnection, scope: Scope, fields: dict[str, Any], exclude: list[int]) -> None:
    head = analyze(f"{fields['title']} {' '.join(fields['keywords'])}")
    pids = sorted(scope.readable)
    df = await doc_frequencies(conn, set(head), pids)
    rare = sorted(set(head), key=lambda t: (df.get(t, 0), t))[:12]
    ids = await candidates(conn, token_concepts(rare), pids, exclude=exclude, limit=20)
    if not ids:
        return
    new_head, new_body = set(head), set(analyze(fields["body"]))
    rows = await conn.execute(select(entries.c.id, entries.c.title, entries.c.an_head, entries.c.an_body)
                              .where(entries.c.id.in_(ids)))
    dups = []
    for row in rows.mappings():
        score = similarity(new_head, fields["title"], new_body, set(row["an_head"].split()), row["title"],
                           set(row["an_body"].split()))
        if score >= DUP_THRESHOLD:
            dups.append(Duplicate(ref=f"e{row['id']}", title=row["title"], score=round(score, 2)))
    if dups:
        dups.sort(key=lambda d: (-d.score, int(d.ref[1:])))
        raise WriteError("possible duplicates found; update one of them, or resend with distinct_from",
                         "duplicate", dups[:5])


def similarity(head: set[str], title: str, body: set[str], other_head: set[str], other_title: str,
               other_body: set[str]) -> float:
    jaccard = len(head & other_head) / len(head | other_head) if head | other_head else 0.0
    a, b = _trigrams(title), _trigrams(other_title)
    dice = 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0
    containment = len(body & other_body) / len(body) if body else 0.0
    return max(jaccard, dice, 0.8 * containment)


def _trigrams(value: str) -> set[str]:
    value = f"  {value.lower()} "
    return {value[i : i + 3] for i in range(len(value) - 2)}


async def add_entry(conn: AsyncConnection, scope: Scope, node_id: int, fields: EntryFields,
                    distinct_from: list[int], new_links: Links = None) -> int:
    node = await get_node(conn, scope, node_id, lock=True)
    await _guard(conn, scope, node)
    _live(node)
    values = validate_entry(fields)
    await _dedup(conn, scope, values, distinct_from)
    user, ts = scope.principal.user_id, now()
    an = {"an_head": analyzed_text(values["title"], *values["keywords"]), "an_body": analyzed_text(values["body"])}
    entry_id = (await conn.execute(entries.insert().values(
        node_id=node.id, project_id=node.project_id, **values, **an, rev=1, inject=False, is_searchable=True,
        created_at=ts, created_by=user, updated_at=ts, updated_by=user, verified_at=ts, verified_by=user,
    ).returning(entries.c.id))).scalar_one()
    await conn.execute(entry_revisions.insert().values(entry_id=entry_id, rev=1, by=user, at=ts, **values))
    await _fts(conn, "entries", entry_id, an)
    await _apply_df(conn, node.project_id, _delta(_entry_tokens(an), +1))
    await set_links(conn, scope, "e", int(entry_id), node.project_id, new_links)
    await _log(conn, node.project_id, f"e{entry_id}", "add", user, values["title"])
    return int(entry_id)


async def update_entry(conn: AsyncConnection, scope: Scope, entry_id: int, base_revision: int | None,
                       fields: EntryFields, distinct_from: list[int], new_links: Links = None) -> int:
    entry, node = await get_entry(conn, scope, entry_id, lock=True)
    await _guard(conn, scope, node, entry)
    if entry.archived_at is not None:
        raise WriteError("that entry is archived; unarchive it first", "archived")
    _live(node)
    if base_revision != entry.rev:
        raise WriteError(f"conflict: entry is at revision {entry.rev}; read it again and resend with that "
                         "base_revision", "conflict")
    current = {"type": entry.type, "title": entry.title, "body": entry.body, "keywords": entry.keywords}
    values = validate_entry(fields, current)
    await set_links(conn, scope, "e", entry_id, node.project_id, new_links)
    if values == current:
        return int(entry.rev)
    await _dedup(conn, scope, values, [entry_id, *distinct_from])
    user, ts, rev = scope.principal.user_id, now(), entry.rev + 1
    an = {"an_head": analyzed_text(values["title"], *values["keywords"]), "an_body": analyzed_text(values["body"])}
    await conn.execute(update(entries).where(entries.c.id == entry_id)
                       .values(**values, **an, rev=rev, updated_at=ts, updated_by=user))
    await conn.execute(entry_revisions.insert().values(entry_id=entry_id, rev=rev, by=user, at=ts, **values))
    await _fts(conn, "entries", entry_id, an)
    if entry.is_searchable:
        before, after = _entry_tokens(entry._mapping), _entry_tokens(an)
        deltas = _delta(after - before, +1)
        deltas.update(_delta(before - after, -1))  # update(), not +: Counter arithmetic drops negative counts
        await _apply_df(conn, node.project_id, deltas)
    await _log(conn, node.project_id, f"e{entry_id}", "update", user, values["title"])
    return int(rev)


async def verify_entry(conn: AsyncConnection, scope: Scope, entry_id: int) -> None:
    entry, node = await get_entry(conn, scope, entry_id, lock=True)
    await _guard(conn, scope, node, entry)
    if entry.archived_at is not None:
        raise WriteError("that entry is archived", "archived")
    _live(node)
    await conn.execute(update(entries).where(entries.c.id == entry_id)
                       .values(verified_at=now(), verified_by=scope.principal.user_id))


async def archive(conn: AsyncConnection, scope: Scope, kind: str, item_id: int, reason: str | None,
                  restore: bool = False) -> None:
    user, ts = scope.principal.user_id, now()
    reason = clean(reason or "") or None
    if reason:
        _check_secrets(reason)
    values = {"archived_at": None, "archived_by": None, "archive_reason": None} if restore else \
        {"archived_at": ts, "archived_by": user, "archive_reason": reason}
    op = "unarchive" if restore else "archive"
    if kind == "e":
        entry, node = await get_entry(conn, scope, item_id, lock=True)
        await _guard(conn, scope, node, entry)
        searchable = restore and node.is_searchable
        await conn.execute(update(entries).where(entries.c.id == item_id).values(**values, is_searchable=searchable))
        if searchable != entry.is_searchable:
            await _apply_df(conn, node.project_id, _delta(_entry_tokens(entry._mapping), +1 if searchable else -1))
        await _log(conn, node.project_id, f"e{item_id}", op, user, entry.title)
        return
    node = await get_node(conn, scope, item_id, lock=True)
    await _guard(conn, scope, node, subtree=True)
    await conn.execute(update(nodes).where(nodes.c.id == item_id).values(**values))
    await refresh_searchable(conn, node.project_id, node.path)
    await _log(conn, node.project_id, f"n{item_id}", op, user, node.title)


async def purge_entry(conn: AsyncConnection, scope: Scope, entry_id: int) -> None:
    entry, node = await get_entry(conn, scope, entry_id, lock=True)
    scope.require_team_admin(scope.project_team[node.project_id])
    tokens = _entry_tokens(entry._mapping)
    if entry.is_searchable:
        await _apply_df(conn, entry.project_id, _delta(tokens, -1))
    await conn.execute(delete(links).where(((links.c.from_kind == "e") & (links.c.from_id == entry_id))
                                           | ((links.c.to_kind == "e") & (links.c.to_id == entry_id))))
    await conn.execute(delete(entry_revisions).where(entry_revisions.c.entry_id == entry_id))
    await _fts(conn, "entries", entry_id, None)
    await conn.execute(delete(entries).where(entries.c.id == entry_id))
    # Purges are for leaked secrets and legal requests, so drop the words that only this entry used.
    await conn.execute(delete(term_df).where(term_df.c.term.in_(tokens), term_df.c.df <= 0))
    await conn.execute(delete(vocab).where(vocab.c.term.in_(tokens),
                                           ~select(term_df.c.term).where(term_df.c.term == vocab.c.term).exists()))
    ref = f"e{entry_id}"
    await conn.execute(update(change_log).where(change_log.c.project_id == entry.project_id, change_log.c.ref == ref)
                       .values(summary="(purged)"))
    await _log(conn, entry.project_id, ref, "purge", scope.principal.user_id, "(purged)")


async def refresh_searchable(conn: AsyncConnection, project_id: int, path: str) -> None:
    parts = path.split("/")
    ancestors = ["/".join(parts[:i]) for i in range(1, len(parts))]
    archived_above = (await conn.execute(select(nodes.c.id).where(
        nodes.c.project_id == project_id, nodes.c.path.in_(ancestors), nodes.c.archived_at.is_not(None)
    ))).first() is not None
    subtree = (await conn.execute(select(nodes.c.id, nodes.c.path, nodes.c.archived_at, nodes.c.is_searchable).where(
        nodes.c.project_id == project_id, (nodes.c.path == path) | nodes.c.path.startswith(f"{path}/", autoescape=True)
    ).order_by(nodes.c.depth, nodes.c.id).with_for_update())).all()
    deltas: Counter[str] = Counter()
    dead: set[str] = set()
    for n in subtree:
        if n.archived_at is not None:
            dead.add(n.path)
        searchable = not archived_above and not any(n.path == d or n.path.startswith(d + "/") for d in dead)
        if searchable != n.is_searchable:
            await conn.execute(update(nodes).where(nodes.c.id == n.id).values(is_searchable=searchable))
        rows = await conn.execute(select(entries).where(entries.c.node_id == n.id).order_by(entries.c.id)
                                  .with_for_update())
        for e in rows.mappings():
            want = searchable and e["archived_at"] is None
            if want != e["is_searchable"]:
                await conn.execute(update(entries).where(entries.c.id == e["id"]).values(is_searchable=want))
                deltas.update(_delta(_entry_tokens(e), +1 if want else -1))
    await _apply_df(conn, project_id, deltas)


async def move(conn: AsyncConnection, scope: Scope, kind: str, item_id: int, target_node: int | None,
               target_project: int | None) -> None:
    user = scope.principal.user_id
    target = await get_node(conn, scope, target_node) if target_node else None
    if target is not None:
        await _guard(conn, scope, target)
        if not target.is_searchable:
            raise WriteError("target node is archived", "archived")
    if kind == "e":
        if target is None:
            raise WriteError("an entry moves into a node: pass node")
        entry, node = await get_entry(conn, scope, item_id, lock=True)
        await _guard(conn, scope, node, entry)
        _live(node)
        if target.project_id != node.project_id:
            raise WriteError("moving across projects isn't supported yet")
        await conn.execute(update(entries).where(entries.c.id == item_id).values(node_id=target.id))
        await _log(conn, node.project_id, f"e{item_id}", "move", user, f"to n{target.id}")
        return
    node = await get_node(conn, scope, item_id, lock=True)
    await _guard(conn, scope, node, subtree=True)
    _live(node)
    project_id = target.project_id if target else (target_project or node.project_id)
    if project_id != node.project_id:
        raise WriteError("moving across projects isn't supported yet")
    if target and (target.path == node.path or target.path.startswith(node.path + "/")):
        raise WriteError("can't move a node into its own subtree")
    new_path = f"{target.path}/{node.slug}" if target else node.slug
    await _reparent(conn, node, target.id if target else None, new_path, (target.depth + 1) if target else 1)
    await refresh_searchable(conn, project_id, new_path)
    await _log(conn, project_id, f"n{item_id}", "move", user, f"to {new_path}")


async def _reparent(conn: AsyncConnection, node: Row[Any], parent_id: int | None, new_path: str, depth: int) -> None:
    subtree = (await conn.execute(select(nodes).where(
        nodes.c.project_id == node.project_id,
        (nodes.c.path == node.path) | nodes.c.path.startswith(f"{node.path}/", autoescape=True),
    ))).all()
    shift = depth - node.depth
    if max(n.depth for n in subtree) + shift > MAX_DEPTH:
        raise WriteError(f"nodes nest at most {MAX_DEPTH} levels")
    clash = await conn.execute(select(nodes.c.id).where(nodes.c.project_id == node.project_id,
                                                        nodes.c.path == new_path, nodes.c.id != node.id))
    if clash.first():
        raise WriteError(f"a node already exists at {new_path}", "conflict")
    for n in subtree:
        path = new_path + n.path[len(node.path):]
        an_desc = analyzed_text(n.summary, path.replace("/", " "))
        values: dict[str, Any] = {"path": path, "depth": n.depth + shift, "an_desc": an_desc}
        if n.id == node.id:
            values["parent_id"] = parent_id
        await conn.execute(update(nodes).where(nodes.c.id == n.id).values(**values))
        await _fts(conn, "nodes", n.id, {"an_head": n.an_head, "an_desc": an_desc})


async def create_node(conn: AsyncConnection, scope: Scope, project_id: int | None, parent_id: int | None,
                      fields: NodeFields, new_links: Links = None) -> int:
    parent = await get_node(conn, scope, parent_id, lock=True) if parent_id else None
    if parent is not None:
        await _guard(conn, scope, parent)
        _live(parent)
        project_id = parent.project_id
    if project_id is None:
        raise WriteError("create_node needs node (the parent) or project")
    scope.require_write(project_id)
    values = validate_node(fields)
    path = f"{parent.path}/{values['slug']}" if parent else values["slug"]
    depth = parent.depth + 1 if parent else 1
    if depth > MAX_DEPTH:
        raise WriteError(f"nodes nest at most {MAX_DEPTH} levels")
    if (await conn.execute(select(nodes.c.id).where(nodes.c.project_id == project_id, nodes.c.path == path))).first():
        raise WriteError(f"a node already exists at {path}", "conflict")
    user, ts = scope.principal.user_id, now()
    an = {"an_head": analyzed_text(values["title"], *values["keywords"]),
          "an_desc": analyzed_text(values["summary"], path.replace("/", " "))}
    node_id = (await conn.execute(nodes.insert().values(
        project_id=project_id, parent_id=parent_id, path=path, depth=depth, **values, **an, rev=1, inject=False,
        is_searchable=True, created_at=ts, created_by=user, updated_at=ts, updated_by=user,
    ).returning(nodes.c.id))).scalar_one()
    await _fts(conn, "nodes", node_id, an)
    await set_links(conn, scope, "n", int(node_id), project_id, new_links)
    await _log(conn, project_id, f"n{node_id}", "create_node", user, path)
    return int(node_id)


async def update_node(conn: AsyncConnection, scope: Scope, node_id: int, base_revision: int | None,
                      fields: NodeFields, new_links: Links = None) -> int:
    node = await get_node(conn, scope, node_id, lock=True)
    await _guard(conn, scope, node)
    _live(node)
    if base_revision != node.rev:
        raise WriteError(f"conflict: node is at revision {node.rev}; read it again", "conflict")
    if fields.slug and fields.slug != node.slug:
        raise WriteError("renaming a slug isn't supported yet; change the title instead")
    current = {"slug": node.slug, "title": node.title, "summary": node.summary, "keywords": node.keywords}
    values = validate_node(fields, current)
    an = {"an_head": analyzed_text(values["title"], *values["keywords"]),
          "an_desc": analyzed_text(values["summary"], node.path.replace("/", " "))}
    user = scope.principal.user_id
    await conn.execute(update(nodes).where(nodes.c.id == node_id)
                       .values(**values, **an, rev=node.rev + 1, updated_at=now(), updated_by=user))
    await _fts(conn, "nodes", node_id, an)
    await set_links(conn, scope, "n", node_id, node.project_id, new_links)
    await _log(conn, node.project_id, f"n{node_id}", "update_node", user, values["title"])
    return int(node.rev + 1)


async def set_links(conn: AsyncConnection, scope: Scope, kind: str, item_id: int, project_id: int,
                    new_links: Links) -> None:
    if new_links is None:
        return
    add, remove = new_links
    for to_kind, to_id in remove:
        await conn.execute(delete(links).where(links.c.from_kind == kind, links.c.from_id == item_id,
                                               links.c.to_kind == to_kind, links.c.to_id == to_id))
    for to_kind, to_id, raw_reason in add:
        reason = clean(raw_reason)
        if not reason or len(reason) > MAX_REASON:
            raise WriteError(f"each link needs a reason (≤ {MAX_REASON} characters): what the dependency does to you")
        _check_secrets(reason)
        if (to_kind, to_id) == (kind, item_id):
            continue
        table = entries if to_kind == "e" else nodes
        target = (await conn.execute(select(table.c.project_id, table.c.is_searchable)
                                     .where(table.c.id == to_id))).first()
        if target is None or target.project_id not in scope.readable or not target.is_searchable:
            raise WriteError(f"link target {to_kind}{to_id} not found, archived, or not visible to you", "not_found")
        same = (links.c.from_kind == kind, links.c.from_id == item_id, links.c.to_kind == to_kind,
                links.c.to_id == to_id)
        if not (await conn.execute(update(links).where(*same).values(reason=reason))).rowcount:
            await conn.execute(links.insert().values(project_id=project_id, from_kind=kind, from_id=item_id,
                                                     to_kind=to_kind, to_id=to_id, reason=reason,
                                                     created_by=scope.principal.user_id, created_at=now()))
    total = (await conn.execute(select(func.count()).select_from(links).where(
        links.c.from_kind == kind, links.c.from_id == item_id))).scalar_one()
    if total > MAX_LINKS:
        raise WriteError(f"at most {MAX_LINKS} links per item; unlink some first")
