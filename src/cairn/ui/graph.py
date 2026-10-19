from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..core.access import NotFound
from ..models import MapRequest, MapResponse
from .base import federation, problem, render, user

router = APIRouter(prefix="/ui")


@router.get("/graph")
async def graph_page(request: Request, project: str = "") -> Response:
    p = await user(request)
    error = ""
    try:
        found = await federation(request).map(p, MapRequest(depth=1))
    except Exception as exc:
        error, found = problem(exc), MapResponse(stores=[])
    many = len(found.stores) > 1
    projects = {pr.ref: (f"{s.store} / " if many else "") + f"{pr.team} / {pr.name}"
                for s in found.stores for pr in s.projects}
    project = project or next(iter(projects), "")
    if project and project not in projects:
        error = error or problem(NotFound())
    return render(request, "graph.html", projects=projects, project=project, error=error,
                  unreachable=found.unreachable)


@router.get("/graph.json")
async def graph_json(request: Request, project: str = "") -> Response:
    p = await user(request)
    try:
        graph = await federation(request).graph(p, project)
    except Exception as exc:
        status = 404 if isinstance(exc, NotFound) else getattr(exc, "status", 400)
        return JSONResponse({"error": problem(exc)}, status_code=status)
    return JSONResponse(graph.model_dump())
