from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Query, Request

from .. import __version__
from ..core import accounts, admin
from ..core.access import Principal, scope_for
from ..core.inject import render
from ..core.write import WriteError
from ..models import (
    AdminRequest,
    AdminResponse,
    Archived,
    BootstrapRequest,
    Change,
    GetRequest,
    GetResponse,
    Graph,
    InjectRequest,
    InjectResponse,
    MapRequest,
    MapResponse,
    SearchRequest,
    SearchResponse,
    WriteRequest,
    WriteResponse,
)
from .health import API_VERSION

router = APIRouter(prefix="/api/v1")


def _principal(request: Request) -> Principal:
    principal: Principal = request.state.principal
    return principal


@router.get("/hello")
async def hello(request: Request, nonce: str = Query("", max_length=64)) -> dict[str, Any]:
    state = request.app.state
    out = {"store": state.settings.alias, "mode": state.settings.mode, "version": __version__,
           "api_version": API_VERSION}
    if nonce and state.local_port is not None:  # proves to clients that this port is ours before they send the token
        out["proof"] = hmac.new(state.local_token.encode(), nonce.encode(), "sha256").hexdigest()
    return out


@router.post("/bootstrap")
async def bootstrap(request: Request, body: BootstrapRequest) -> dict[str, str]:
    async with request.app.state.engine.begin() as conn:
        key, link = await admin.bootstrap(conn, body.token, body.email, body.name)
    return {"api_key": key, "password_link": link, "store": request.app.state.settings.alias}


@router.post("/search")
async def search(request: Request, body: SearchRequest) -> SearchResponse:
    return await request.app.state.federation.search(_principal(request), body)  # type: ignore[no-any-return]


@router.post("/get")
async def get(request: Request, body: GetRequest) -> GetResponse:
    return await request.app.state.federation.get(_principal(request), body)  # type: ignore[no-any-return]


@router.post("/map")
async def map_(request: Request, body: MapRequest) -> MapResponse:
    return await request.app.state.federation.map(_principal(request), body)  # type: ignore[no-any-return]


@router.post("/write")
async def write(request: Request, body: WriteRequest) -> WriteResponse:
    return await request.app.state.federation.write(_principal(request), body)  # type: ignore[no-any-return]


@router.post("/admin")
async def admin_(request: Request, body: AdminRequest) -> AdminResponse:
    return await request.app.state.federation.admin(_principal(request), body)  # type: ignore[no-any-return]


@router.post("/inject")
async def inject(request: Request, body: InjectRequest) -> InjectResponse:
    groups, unreachable = await request.app.state.federation.inject(_principal(request))
    text = render(groups, body.budget) if body.render else ""
    if body.render and unreachable:
        text += f"\n(Cairn store unreachable: {', '.join(unreachable)})"
    return InjectResponse(text=text, groups=[] if body.render else groups)


@router.get("/changes")
async def changes(request: Request, store: str | None = None, before: int | None = None,
                  limit: int = Query(50, ge=1, le=200)) -> list[Change]:
    return await request.app.state.federation.changes(_principal(request), store, before, limit)  # type: ignore[no-any-return]


@router.get("/archived")
async def archived(request: Request, store: str | None = None) -> list[Archived]:
    return await request.app.state.federation.archived(_principal(request), store)  # type: ignore[no-any-return]


@router.get("/graph")
async def graph(request: Request, project: str) -> Graph:
    return await request.app.state.federation.graph(_principal(request), project)  # type: ignore[no-any-return]


@router.post("/ui/ticket")
async def ui_ticket(request: Request) -> dict[str, str]:
    async with request.app.state.engine.begin() as conn:
        return {"ticket": await accounts.ui_ticket(conn, _principal(request))}


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    state = request.app.state
    remote = state.settings.remote()
    async with state.engine.connect() as conn:
        counts = await admin.counts(conn, sorted((await scope_for(conn, _principal(request))).readable))
    return {"store": state.settings.alias, "mode": state.settings.mode, "version": __version__, "counts": counts,
            "remote": remote.url if remote else None,
            "git": state.git.status() if state.git else {"enabled": False}}


@router.post("/git/push")
async def git_push(request: Request) -> dict[str, Any]:
    _principal(request)
    if not request.app.state.git:
        raise WriteError("git mode is off; turn it on with cairn git enable")
    return await request.app.state.git.push_now()  # type: ignore[no-any-return]


@router.post("/git/purge")
async def git_purge(request: Request) -> dict[str, Any]:
    _principal(request)
    if not request.app.state.git:
        raise WriteError("git mode is off")
    return await request.app.state.git.purge_history()  # type: ignore[no-any-return]
