from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..core.refs import RefError, parse
from ..core.store import RemoteError
from ..core.write import WriteError
from ..models import (
    ENTRY_TYPES,
    AdminRequest,
    Duplicate,
    EntryFields,
    GetRequest,
    HitOut,
    LinkIn,
    MapProject,
    MapRequest,
    NodeFields,
    SearchRequest,
    WriteRequest,
)
from .base import federation, form, problem, render, user

router = APIRouter(prefix="/ui")


def _kind(ref: str) -> str:
    try:
        return parse(ref).kind
    except RefError:
        return ""


def _split(text: str) -> list[str]:
    return [s.strip() for s in text.split(",") if s.strip()]


def concepts(q: str) -> list[str]:
    q = re.sub(r"\s*\|\s*", "|", q.strip())
    return (_split(q) if "," in q else q.split())[:8]


def _links(f: dict[str, str]) -> dict[str, Any]:
    rows = [LinkIn(to=v.strip(), reason=f.get("reason" + k[2:], ""))
            for k, v in f.items() if k[:2] == "to" and k[2:].isdigit() and v.strip()]
    kept = {link.to for link in rows}
    return {"links": rows or None, "unlink": [v for k, v in f.items() if k[:3] == "was" and v not in kept] or None}


def _entry(f: dict[str, str]) -> EntryFields:
    return EntryFields.model_validate({
        "type": f.get("type") if f.get("type") in ENTRY_TYPES else None, "title": f.get("title", ""),
        "body": f.get("body", ""), "keywords": _split(f.get("keywords", "")), **_links(f)})


def _write_request(action: str, ref: str, f: dict[str, str]) -> WriteRequest:
    rev = int(f["base_revision"]) if f.get("base_revision", "").isdigit() else None
    distinct = _split(f.get("distinct_from", ""))
    if action == "add":
        return WriteRequest(op="add", node=ref, entry=_entry(f), distinct_from=distinct)
    if action == "edit":
        return WriteRequest(op="update", ref=ref, base_revision=rev, entry=_entry(f), distinct_from=distinct)
    if action == "node":
        fields = NodeFields(slug=f.get("slug", "").strip(), title=f.get("title", ""), summary=f.get("summary", ""),
                            keywords=_split(f.get("keywords", "")))
        top = _kind(ref) == "p"
        return WriteRequest(op="create_node", project=ref if top else None, node=None if top else ref,
                            node_fields=fields)
    if action == "update-node":
        fields = NodeFields(title=f.get("title", ""), summary=f.get("summary", ""),
                            keywords=_split(f.get("keywords", "")), **_links(f))
        return WriteRequest(op="update_node", ref=ref, base_revision=rev, node_fields=fields)
    if action in ("archive", "unarchive", "verify", "move"):
        return WriteRequest.model_validate({"op": action, "ref": ref, "node": f.get("node") or None,
                                            "reason": f.get("reason") or None})
    raise WriteError(f"unknown action {action!r}")


def _done(to: str | None = None) -> Response:
    return Response(status_code=204, headers={"HX-Redirect": to} if to else {"HX-Refresh": "true"})


@router.get("")
async def home(request: Request) -> Response:
    p = await user(request)
    try:
        tree = await federation(request).map(p, MapRequest(depth=3))
    except Exception as exc:
        return render(request, "home.html", tree=None, error=problem(exc))
    return render(request, "home.html", tree=tree)


@router.get("/r/{ref}")
async def item_page(request: Request, ref: str) -> Response:
    p = await user(request)
    fed = federation(request)
    try:
        got = await fed.get(p, GetRequest(refs=[ref]))
        if not got.items:
            return render(request, "item.html", ref=ref, item=None,
                          error=f"store {', '.join(got.unreachable)} is unreachable")
        item = got.items[0]
        kind = _kind(item.ref)
        projects: list[MapProject] = []
        if item.status == "ok":
            req = MapRequest(root=item.node_ref or item.ref, depth=3 if kind == "p" else 1)
        elif kind == "n":
            req = MapRequest(root=item.ref, depth=1)
        else:  # an archived entry's project is unknown: offer unarchive to anyone who writes in its store
            req = MapRequest(store=item.ref.split(":")[0], depth=1)
        if item.status != "not_found":
            projects = [pr for s in (await fed.map(p, req)).stores for pr in s.projects]
    except Exception as exc:
        return render(request, "item.html", ref=ref, item=None, error=problem(exc))
    roles = {pr.role for pr in projects}
    return render(request, "item.html", ref=item.ref, item=item, kind=kind, types=ENTRY_TYPES,
                  project=projects[0] if item.status == "ok" and projects else None,
                  writer=bool(roles - {"read"}), admin="admin" in roles)


@router.get("/r/{ref}/history")
async def history(request: Request, ref: str) -> Response:
    p = await user(request)
    got = await federation(request).get(p, GetRequest(refs=[ref], history=True))
    return render(request, "pages_history.html", revisions=[r for i in got.items for r in i.history])


@router.get("/r/{ref}/move")
async def move_form(request: Request, ref: str, project: str, node: str = "") -> Response:
    p = await user(request)
    try:
        res = await federation(request).map(p, MapRequest(root=project, depth=8))
    except Exception as exc:
        return render(request, "pages_error.html", message=problem(exc))
    nodes = [n for s in res.stores for pr in s.projects for n in pr.nodes]
    return render(request, "pages_move.html", ref=ref, current=node, nodes=nodes)


@router.post("/r/{ref}/{action}")
async def act(request: Request, ref: str, action: str) -> Response:
    p = await user(request)
    f = await form(request)
    fed = federation(request)
    try:
        if action in ("flag", "unflag"):
            await fed.admin(p, AdminRequest(op=action, args={"ref": ref}))
            return _done()
        if action == "purge":
            await fed.admin(p, AdminRequest(op="purge_entry", args={"ref": ref}))
            return _done(f"/ui/r/{f['node']}" if f.get("node") else "/ui")
        res = await fed.write(p, _write_request(action, ref, f))
    except Exception as exc:
        message = problem(exc)
        body = exc.body if isinstance(exc, RemoteError) else {}
        dups = exc.duplicates if isinstance(exc, WriteError) else [
            Duplicate.model_validate(d) for d in body.get("duplicates", [])]
        return render(request, "pages_error.html", message=message, code=getattr(exc, "code", body.get("code")),
                      duplicates=dups)
    return _done(f"/ui/r/{res.ref}" if action in ("add", "node") else None)


@router.get("/search")
async def search_page(request: Request, q: str = "", types: str = "", store: str = "") -> Response:
    p = await user(request)
    chosen = [t for t in types.split(",") if t in ENTRY_TYPES]
    ctx: dict[str, Any] = {"q": q, "chosen": chosen, "store": store, "types": ENTRY_TYPES}
    wanted = concepts(q)
    if not wanted:
        return render(request, "search.html", groups=[], **ctx)
    try:
        res = await federation(request).search(p, SearchRequest.model_validate(
            {"concepts": wanted, "types": chosen or None, "stores": [store] if store else None, "limit": 30}))
    except Exception as exc:
        return render(request, "search.html", groups=[], error=problem(exc), **ctx)
    groups: dict[str, list[HitOut]] = {}
    for hit in res.hits:
        groups.setdefault(hit.node_ref, []).append(hit)
    return render(request, "search.html", groups=list(groups.values()), unreachable=res.unreachable, **ctx)
