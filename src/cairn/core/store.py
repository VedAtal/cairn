from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..db.schema import entries, entry_revisions, nodes, projects, teams, users, write_ops
from ..models import (
    AdminRequest,
    AdminResponse,
    Archived,
    Brief,
    Change,
    EntryFields,
    GetRequest,
    GetResponse,
    Graph,
    HitOut,
    InjectGroup,
    Item,
    MapNode,
    MapProject,
    MapRequest,
    MapResponse,
    MapStore,
    NodeFields,
    NodeHitOut,
    Revision,
    SearchRequest,
    SearchResponse,
    WriteRequest,
    WriteResponse,
)
from . import admin, inject, views, write
from .access import NotFound, Principal, Scope, scope_for
from .refs import Ref, RefError, parse
from .search import NODE_LIMIT, Hit, parse_concepts, search
from .write import Links

MAX_CHILDREN = 30
_PATH_REF = re.compile(r"^([a-z][a-z0-9-]{0,31}):([a-z0-9_]{1,48})/([a-z0-9_]{1,48})((?:/[a-z0-9_]{1,48})*)$")
REF_KEYS = {"refs", "root", "within", "ref", "node", "project", "to", "unlink"}
M = TypeVar("M", bound=BaseModel)


class StoreUnavailable(Exception):
    pass


class RemoteError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(body.get("message", "remote error"))
        self.status, self.body = status, body


class Store(Protocol):
    alias: str

    async def search(self, p: Principal, req: SearchRequest) -> SearchResponse: ...
    async def get(self, p: Principal, req: GetRequest) -> GetResponse: ...
    async def map(self, p: Principal, req: MapRequest) -> MapResponse: ...
    async def write(self, p: Principal, req: WriteRequest) -> WriteResponse: ...
    async def admin(self, p: Principal, req: AdminRequest) -> AdminResponse: ...
    async def inject(self, p: Principal) -> list[InjectGroup]: ...
    async def changes(self, p: Principal, before: int | None, limit: int) -> list[Change]: ...
    async def archived(self, p: Principal) -> list[Archived]: ...
    async def graph(self, p: Principal, project: str) -> Graph: ...


class SqlStore:
    def __init__(self, engine: AsyncEngine, alias: str, quota: int | None = None) -> None:
        self.quota = quota
        self.engine, self.alias = engine, alias
        self.on_change: Callable[[], None] = lambda: None

    async def _paths(self, conn: AsyncConnection, scope: Scope, req: M) -> M:
        data = req.model_dump()
        fixed = await self._resolve(conn, scope, data)
        return req if fixed == data else type(req).model_validate(fixed)

    async def _resolve(self, conn: AsyncConnection, scope: Scope, value: Any, is_ref: bool = False) -> Any:
        if isinstance(value, dict):
            return {k: await self._resolve(conn, scope, v, k in REF_KEYS) for k, v in value.items()}
        if isinstance(value, list):
            return [await self._resolve(conn, scope, v, is_ref) for v in value]
        if not is_ref or not isinstance(value, str) or not (m := _PATH_REF.match(value.strip())) or m[1] != self.alias:
            return value
        pid = (await conn.execute(select(projects.c.id).join(teams, teams.c.id == projects.c.team_id).where(
            teams.c.slug == m[2], projects.c.slug == m[3],
            projects.c.id.in_(scope.readable) | projects.c.team_id.in_(scope.admin_teams)))).scalar_one_or_none()
        if pid is None or not m[4]:
            return value if pid is None else f"{self.alias}:p{pid}"
        node = (await conn.execute(select(nodes.c.id).where(nodes.c.project_id == pid,
                                                            nodes.c.path == m[4][1:]))).scalar_one_or_none()
        return f"{self.alias}:n{node}" if node else value

    def _ref(self, ref: str, kinds: str) -> Ref:
        parsed = parse(ref, kinds)
        if parsed.store != self.alias:
            raise RefError(f"{ref} belongs to store {parsed.store!r}, not {self.alias!r}")
        return parsed

    async def _within(self, conn: AsyncConnection, scope: Scope, ref: str | None) -> tuple[str, int, str | None] | None:
        if not ref:
            return None
        r = self._ref(ref, "np")
        if r.kind == "p":
            scope.require_read(r.id)
            return ("p", r.id, None)
        node = await write.get_node(conn, scope, r.id)
        return ("n", node.project_id, node.path)

    async def search(self, p: Principal, req: SearchRequest) -> SearchResponse:
        async with self.engine.connect() as conn:
            scope = await scope_for(conn, p)
            req = await self._paths(conn, scope, req)
            found = await search(conn, parse_concepts(req.concepts), sorted(scope.readable),
                                 types=list(req.types or []), within=await self._within(conn, scope, req.within),
                                 limit=req.limit)
        nodes_out = [NodeHitOut(store=self.alias, ref=f"{self.alias}:n{n.id}", title=n.title, summary=n.summary,
                                path=n.path, team=n.team, project=n.project, matched=n.matched, features=n.features)
                     for n in found.nodes]
        return SearchResponse(hits=[self._hit(h) for h in found.hits], nodes=nodes_out, common_only=found.common_only)

    def _hit(self, h: Hit) -> HitOut:
        return HitOut(store=self.alias, ref=f"{self.alias}:e{h.id}", type=h.type, title=h.title,
                      verified_at=h.verified_at, node_ref=f"{self.alias}:n{h.node_id}", node_title=h.node_title,
                      node_summary=h.node_summary, node_path=h.node_path, team=h.team, project=h.project,
                      matched=h.matched, features=h.features)

    async def get(self, p: Principal, req: GetRequest) -> GetResponse:
        async with self.engine.connect() as conn:
            scope = await scope_for(conn, p)
            req = await self._paths(conn, scope, req)
            items = []
            for ref in req.refs:
                try:
                    items.append(await self._get_one(conn, scope, self._ref(ref, "enp"), req.history))
                except (NotFound, RefError):
                    items.append(Item(ref=ref, status="not_found"))
        return GetResponse(items=items)

    async def _get_one(self, conn: AsyncConnection, scope: Scope, r: Ref, history: bool) -> Item:
        ref = str(r)
        if r.kind == "p":
            scope.require_read(r.id)
            project = (await conn.execute(select(projects.c.name).where(projects.c.id == r.id))).one()
            children = await self._children(conn, r.id, None)
            return Item(ref=ref, status="ok", title=project.name, children=children)
        if r.kind == "n":
            node = await write.get_node(conn, scope, r.id)
            if not node.is_searchable:
                return await self._tombstone(conn, ref, node)
            kids = await self._children(conn, node.project_id, node.id)
            rows = (await conn.execute(select(entries.c.id, entries.c.type, entries.c.title).where(
                entries.c.node_id == node.id, entries.c.is_searchable).order_by(entries.c.id))).all()
            out, back = await views.links_of(conn, scope, self.alias, "n", node.id)
            return Item(ref=ref, status="ok", title=node.title, summary=node.summary, keywords=node.keywords,
                        path=node.path, rev=node.rev, inject=node.inject, children=kids, links=out, linked_from=back,
                        entries=[Brief(ref=f"{self.alias}:e{e.id}", type=e.type, title=e.title)
                                 for e in rows[:MAX_CHILDREN]],
                        counts={"entries": len(rows), "children": len(kids)})
        entry, node = await write.get_entry(conn, scope, r.id)
        if not entry.is_searchable:
            return await self._tombstone(conn, ref, entry)
        revisions = []
        if history:
            revs = await conn.execute(select(entry_revisions).where(entry_revisions.c.entry_id == r.id)
                                      .order_by(entry_revisions.c.rev.desc()))
            revisions = [Revision(rev=v.rev, title=v.title, body=v.body, at=str(v.at)[:19]) for v in revs]
        out, back = await views.links_of(conn, scope, self.alias, "e", entry.id)
        return Item(ref=ref, status="ok", type=entry.type, title=entry.title, body=entry.body,
                    keywords=entry.keywords, rev=entry.rev, inject=entry.inject,
                    verified_at=str(entry.verified_at)[:10], node_ref=f"{self.alias}:n{node.id}", path=node.path,
                    history=revisions, links=out, linked_from=back)

    async def _tombstone(self, conn: AsyncConnection, ref: str, row: Any) -> Item:
        if row.archived_at is None:
            return Item(ref=ref, status="archived", tombstone=f"{ref} — inside an archived node")
        who = (await conn.execute(select(users.c.email).where(users.c.id == row.archived_by))).scalar_one_or_none()
        return Item(ref=ref, status="archived",
                    tombstone=f"{ref} — archived {str(row.archived_at)[:10]} by {(who or '?').split('@')[0]}")

    async def _children(self, conn: AsyncConnection, project_id: int, parent: int | None) -> list[Brief]:
        q = select(nodes.c.id, nodes.c.title, nodes.c.summary).where(
            nodes.c.project_id == project_id, nodes.c.is_searchable,
            nodes.c.parent_id.is_(None) if parent is None else nodes.c.parent_id == parent,
        ).order_by(nodes.c.path).limit(MAX_CHILDREN)
        return [Brief(ref=f"{self.alias}:n{n.id}", title=n.title, summary=n.summary[:120])
                for n in await conn.execute(q)]

    async def map(self, p: Principal, req: MapRequest) -> MapResponse:
        async with self.engine.connect() as conn:
            scope = await scope_for(conn, p)
            req = await self._paths(conn, scope, req)
            email = (await conn.execute(select(users.c.email).where(users.c.id == p.user_id))).scalar_one()
            root = self._ref(req.root, "np") if req.root else None
            pids = sorted(scope.readable)
            if root and root.kind == "p":
                scope.require_read(root.id)
                pids = [root.id]
            root_node = await write.get_node(conn, scope, root.id) if root and root.kind == "n" else None
            if root_node:
                pids = [root_node.project_id]
            proj_rows = (await conn.execute(select(projects.c.id, projects.c.name, teams.c.slug).join(
                teams, teams.c.id == projects.c.team_id).where(projects.c.id.in_(pids)).order_by(
                teams.c.slug, projects.c.slug))).all() if pids else []
            out = []
            for pr in proj_rows:
                role = "admin" if scope.is_team_admin(scope.project_team[pr.id]) else (
                    "write" if pr.id in scope.writable else "read")
                tree = await self._tree(conn, pr.id, root_node, req.depth, (req.filter or "").lower())
                out.append(MapProject(ref=f"{self.alias}:p{pr.id}", team=pr.slug, name=pr.name, role=role, nodes=tree))
        return MapResponse(stores=[MapStore(store=self.alias, user=email, projects=out)])

    async def _tree(self, conn: AsyncConnection, project_id: int, root: Any, depth: int, flt: str) -> list[MapNode]:
        q = select(nodes.c.id, nodes.c.parent_id, nodes.c.title, nodes.c.summary, nodes.c.depth, nodes.c.path).where(
            nodes.c.project_id == project_id, nodes.c.is_searchable)
        base = 0
        if root is not None:
            q = q.where((nodes.c.path == root.path) | nodes.c.path.startswith(f"{root.path}/", autoescape=True))
            base = root.depth - 1
        q = q.where(nodes.c.depth <= base + depth).order_by(nodes.c.path)
        rows = (await conn.execute(q)).all()
        counts: dict[int, int] = dict((await conn.execute(select(entries.c.node_id, func.count()).where(
            entries.c.node_id.in_([r.id for r in rows]), entries.c.is_searchable).group_by(entries.c.node_id)
        )).all()) if rows else {}
        built = {r.id: MapNode(ref=f"{self.alias}:n{r.id}", title=r.title, summary=r.summary[:120],
                               entries=counts.get(r.id, 0)) for r in rows}
        top = []
        for r in rows:
            node = built[r.id]
            if r.parent_id in built:
                built[r.parent_id].children.append(node)
            else:
                top.append(node)
        if flt:
            top = [n for n in top if _keep(n, flt)]
        return top

    async def write(self, p: Principal, req: WriteRequest) -> WriteResponse:
        async with self.engine.begin() as conn:
            scope = await scope_for(conn, p)
            req = await self._paths(conn, scope, req)
            done = req.op_id and (await conn.execute(select(write_ops.c.result).where(
                write_ops.c.user_id == p.user_id, write_ops.c.op_id == req.op_id))).scalar_one_or_none()
            if done:
                return WriteResponse.model_validate(done)
            if self.quota:
                await write.check_quota(conn, p.user_id, self.quota)
            try:
                result = await self._write(conn, scope, req)
            except write.WriteError as exc:
                for d in exc.duplicates:
                    d.ref = f"{self.alias}:{d.ref}"
                raise
            if req.op_id:
                await conn.execute(write_ops.insert().values(user_id=p.user_id, op_id=req.op_id, at=write.now(),
                                                             result=result.model_dump()))
        self.on_change()
        return result

    async def _write(self, conn: AsyncConnection, scope: Scope, req: WriteRequest) -> WriteResponse:
        distinct = [self._ref(r, "e").id for r in req.distinct_from]
        target = self._ref(req.ref, "en") if req.ref else None
        node = self._ref(req.node, "n").id if req.node else None
        project = self._ref(req.project, "p").id if req.project else None

        def need_target(kinds: str) -> Ref:
            if target is None or target.kind not in kinds:
                raise write.WriteError(f"{req.op} needs ref: " + " or ".join(f"<store>:{k}<id>" for k in kinds))
            return target

        if req.op == "add":
            if node is None or req.entry is None:
                raise write.WriteError("add needs node and entry")
            new_id = await write.add_entry(conn, scope, node, req.entry, distinct, self._links(req.entry))
            return WriteResponse(ok=True, ref=f"{self.alias}:e{new_id}", rev=1, message="added")
        if req.op == "update":
            t = need_target("e")
            fields = req.entry or EntryFields()
            rev = await write.update_entry(conn, scope, t.id, req.base_revision, fields, distinct, self._links(fields))
            return WriteResponse(ok=True, ref=str(t), rev=rev, message="updated")
        if req.op in ("archive", "unarchive"):
            t = need_target("en")
            await write.archive(conn, scope, t.kind, t.id, req.reason, restore=req.op == "unarchive")
            return WriteResponse(ok=True, ref=str(t), message=f"{req.op}d")
        if req.op == "verify":
            t = need_target("e")
            await write.verify_entry(conn, scope, t.id)
            return WriteResponse(ok=True, ref=str(t), message="verified")
        if req.op == "move":
            t = need_target("en")
            await write.move(conn, scope, t.kind, t.id, node, project)
            return WriteResponse(ok=True, ref=str(t), message="moved")
        if req.op == "create_node":
            node_fields = req.node_fields or NodeFields()
            new_id = await write.create_node(conn, scope, project, node, node_fields, self._links(node_fields))
            return WriteResponse(ok=True, ref=f"{self.alias}:n{new_id}", rev=1, message="created")
        t = need_target("n")
        node_fields = req.node_fields or NodeFields()
        rev = await write.update_node(conn, scope, t.id, req.base_revision, node_fields,
                                      self._links(node_fields))
        return WriteResponse(ok=True, ref=str(t), rev=rev, message="updated")

    async def admin(self, p: Principal, req: AdminRequest) -> AdminResponse:
        async with self.engine.begin() as conn:
            scope = await scope_for(conn, p)
            req = await self._paths(conn, scope, req)
            result = await admin.run(conn, scope, self.alias, req.op, req.args)
        self.on_change()
        return AdminResponse(result=result)

    async def inject(self, p: Principal) -> list[InjectGroup]:
        async with self.engine.connect() as conn:
            scope = await scope_for(conn, p)
            return await inject.collect(conn, self.alias, sorted(scope.readable))

    async def changes(self, p: Principal, before: int | None, limit: int) -> list[Change]:
        async with self.engine.connect() as conn:
            return await views.changes(conn, await scope_for(conn, p), self.alias, before, limit)

    async def archived(self, p: Principal) -> list[Archived]:
        async with self.engine.connect() as conn:
            return await views.archived(conn, await scope_for(conn, p), self.alias)

    async def graph(self, p: Principal, project: str) -> Graph:
        async with self.engine.connect() as conn:
            scope = await scope_for(conn, p)
            ref = self._ref(await self._resolve(conn, scope, project, True), "p")
            return await views.graph(conn, scope, self.alias, ref.id)

    def _links(self, fields: EntryFields | NodeFields) -> Links:
        if fields.links is None and fields.unlink is None:
            return None
        return ([(r.kind, r.id, link.reason) for link in fields.links or [] for r in [self._ref(link.to, "en")]],
                [(r.kind, r.id) for r in (self._ref(ref, "en") for ref in fields.unlink or [])])


def _merge_key(features: tuple[int, ...], order: int, ref: str) -> tuple[int, ...]:
    c, e, s, v, depth = features
    return (-c, -e, -s, -v, -depth, order, -int(ref.rsplit(":", 1)[1][1:]))


def _keep(node: MapNode, flt: str) -> bool:
    node.children = [c for c in node.children if _keep(c, flt)]
    return flt in node.title.lower() or flt in node.summary.lower() or bool(node.children)


class RemoteStore:
    def __init__(self, url: str, api_key: str, alias: str, timeout: float = 1.5) -> None:
        self.alias = alias
        self.client = httpx.AsyncClient(base_url=url.rstrip("/") + "/api/v1", timeout=timeout,
                                        headers={"Authorization": f"Bearer {api_key}"})

    async def _post(self, path: str, body: Any, timeout: float | None = None) -> Any:
        try:
            resp = await self.client.post(path, json=body.model_dump(exclude_none=True),
                                          timeout=timeout or httpx.USE_CLIENT_DEFAULT)
        except httpx.HTTPError as exc:
            raise StoreUnavailable(self.alias) from exc
        if resp.status_code >= 500:
            raise StoreUnavailable(self.alias)
        if resp.status_code >= 400:
            raise RemoteError(resp.status_code, resp.json())
        return resp.json()

    async def search(self, p: Principal, req: SearchRequest) -> SearchResponse:
        return SearchResponse.model_validate(await self._post("/search", req.model_copy(update={"stores": None})))

    async def get(self, p: Principal, req: GetRequest) -> GetResponse:
        return GetResponse.model_validate(await self._post("/get", req, timeout=10))

    async def map(self, p: Principal, req: MapRequest) -> MapResponse:
        return MapResponse.model_validate(await self._post("/map", req.model_copy(update={"store": None}), 10))

    async def write(self, p: Principal, req: WriteRequest) -> WriteResponse:
        req = req.model_copy(update={"op_id": req.op_id or secrets.token_hex(16)})
        try:
            return WriteResponse.model_validate(await self._post("/write", req, timeout=15))
        except StoreUnavailable:  # the op_id makes the retry safe if the first attempt landed
            return WriteResponse.model_validate(await self._post("/write", req, timeout=15))

    async def admin(self, p: Principal, req: AdminRequest) -> AdminResponse:
        return AdminResponse.model_validate(await self._post("/admin", req.model_copy(update={"store": None}), 15))

    async def inject(self, p: Principal) -> list[InjectGroup]:
        from ..models import InjectRequest, InjectResponse  # noqa: PLC0415

        data = await self._post("/inject", InjectRequest(render=False))
        return InjectResponse.model_validate(data).groups

    async def _get(self, path: str, **params: Any) -> Any:
        try:
            resp = await self.client.get(path, params={k: v for k, v in params.items() if v is not None}, timeout=10)
        except httpx.HTTPError as exc:
            raise StoreUnavailable(self.alias) from exc
        if resp.status_code >= 500:
            raise StoreUnavailable(self.alias)
        if resp.status_code >= 400:
            raise RemoteError(resp.status_code, resp.json())
        return resp.json()

    async def changes(self, p: Principal, before: int | None, limit: int) -> list[Change]:
        return [Change.model_validate(c) for c in await self._get("/changes", before=before, limit=limit)]

    async def archived(self, p: Principal) -> list[Archived]:
        return [Archived.model_validate(a) for a in await self._get("/archived")]

    async def graph(self, p: Principal, project: str) -> Graph:
        return Graph.model_validate(await self._get("/graph", project=project))

    async def aclose(self) -> None:
        await self.client.aclose()


class Federation:
    def __init__(self, stores: list[Store]) -> None:
        self.stores = stores

    def _pick(self, alias: str | None) -> Store:
        if alias is None:
            if len(self.stores) == 1:
                return self.stores[0]
            raise RefError(f"several stores ({', '.join(s.alias for s in self.stores)}): say which with store")
        for s in self.stores:
            if s.alias == alias:
                return s
        raise RefError(f"no store {alias!r}; stores: {', '.join(s.alias for s in self.stores)}")

    async def search(self, p: Principal, req: SearchRequest) -> SearchResponse:
        only = [req.within.split(":", 1)[0]] if req.within else req.stores
        targets = [s for s in self.stores if not only or s.alias in only]
        results = await asyncio.gather(*(s.search(p, req) for s in targets), return_exceptions=True)
        hits: list[tuple[tuple[int, ...], HitOut]] = []
        found: list[tuple[tuple[int, ...], NodeHitOut]] = []
        unreachable, common_only = [], False
        for order, (store, res) in enumerate(zip(targets, results, strict=True)):
            if isinstance(res, BaseException):
                if len(targets) == 1 or not isinstance(res, StoreUnavailable | RemoteError):
                    raise res
                unreachable.append(_why(store, res))
                continue
            hits += [(_merge_key(h.features, order, h.ref), h) for h in res.hits]
            found += [(_merge_key(n.features, order, n.ref), n) for n in res.nodes]
            common_only |= res.common_only
        hits.sort(key=lambda pair: pair[0])
        found.sort(key=lambda pair: pair[0])
        return SearchResponse(hits=[h for _, h in hits[: req.limit]], nodes=[n for _, n in found[:NODE_LIMIT]],
                              common_only=common_only, unreachable=unreachable)

    async def get(self, p: Principal, req: GetRequest) -> GetResponse:
        wanted = [_canonical(r) for r in req.refs]
        by_store: dict[str, list[str]] = {}
        for ref in wanted:
            by_store.setdefault(ref.split(":", 1)[0], []).append(ref)
        found: dict[str, Item] = {}
        unreachable = []
        for alias, refs in by_store.items():
            try:
                res = await self._pick(alias).get(p, GetRequest(refs=refs, history=req.history))
                found |= {i.ref: i for i in res.items}
            except RefError:
                found |= {r: Item(ref=r, status="not_found") for r in refs}
            except (StoreUnavailable, RemoteError) as exc:
                unreachable.append(_why(self._pick(alias), exc))
        return GetResponse(items=[found[r] for r in wanted if r in found], unreachable=unreachable)

    async def map(self, p: Principal, req: MapRequest) -> MapResponse:
        alias = req.store or (req.root.split(":", 1)[0] if req.root else None)
        targets = [self._pick(alias)] if alias else self.stores
        out = MapResponse(stores=[])
        for store in targets:
            try:
                res = await store.map(p, req)
                out.stores += res.stores
            except (StoreUnavailable, RemoteError) as exc:
                if len(targets) == 1:
                    raise
                out.unreachable.append(_why(store, exc))
        return out

    async def write(self, p: Principal, req: WriteRequest) -> WriteResponse:
        anchor = req.ref or req.node or req.project
        return await self._pick(anchor.split(":", 1)[0] if anchor else None).write(p, req)

    async def admin(self, p: Principal, req: AdminRequest) -> AdminResponse:
        ref = req.args.get("ref") or req.args.get("project")
        ref_store = str(ref).split(":", 1)[0] if ref else None
        if req.store and ref_store and req.store != ref_store:
            raise RefError(f"{ref} belongs to store {ref_store!r}, not {req.store!r}")
        return await self._pick(req.store or ref_store).admin(p, req)

    async def changes(self, p: Principal, store: str | None, before: int | None, limit: int) -> list[Change]:
        return await self._pick(store).changes(p, before, limit)

    async def archived(self, p: Principal, store: str | None) -> list[Archived]:
        return await self._pick(store).archived(p)

    async def graph(self, p: Principal, project: str) -> Graph:
        return await self._pick(project.split(":", 1)[0]).graph(p, project)

    async def inject(self, p: Principal) -> tuple[list[InjectGroup], list[str]]:
        results = await asyncio.gather(*(s.inject(p) for s in self.stores), return_exceptions=True)
        groups: list[InjectGroup] = []
        unreachable = []
        for store, res in zip(self.stores, results, strict=True):
            if isinstance(res, BaseException):
                if not isinstance(res, StoreUnavailable | RemoteError):
                    raise res
                unreachable.append(_why(store, res))
            else:
                groups += res
        return groups, unreachable


def _why(store: Store, exc: BaseException) -> str:
    code = exc.body.get("code") if isinstance(exc, RemoteError) else None
    return f"{store.alias} ({code})" if code else store.alias


def _canonical(ref: str) -> str:
    try:
        return str(parse(ref))
    except RefError:
        return ref


__all__ = ["Federation", "RemoteError", "RemoteStore", "SqlStore", "Store", "StoreUnavailable"]
