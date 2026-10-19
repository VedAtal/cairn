from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from .. import __version__
from ..core import accounts, admin
from ..core.access import Principal
from ..core.inject import render
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
async def hello(request: Request) -> dict[str, Any]:
    state = request.app.state
    return {"store": state.settings.alias, "mode": state.settings.mode, "version": __version__,
            "api_version": API_VERSION}


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
    return {"store": state.settings.alias, "mode": state.settings.mode, "version": __version__,
            "remote": remote.url if remote else None}
