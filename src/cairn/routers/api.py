from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from .. import __version__
from ..core import admin
from ..core.access import Principal
from ..core.inject import render
from ..models import (
    AdminRequest,
    AdminResponse,
    BootstrapRequest,
    GetRequest,
    GetResponse,
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
        key = await admin.bootstrap(conn, body.token, body.email, body.name)
    return {"api_key": key, "store": request.app.state.settings.alias}


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


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    state = request.app.state
    remote = state.settings.remote()
    return {"store": state.settings.alias, "mode": state.settings.mode, "version": __version__,
            "remote": remote.url if remote else None}
