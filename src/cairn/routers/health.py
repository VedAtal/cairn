from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from .. import __version__

API_VERSION = 1

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "version": __version__, "api_version": API_VERSION}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, Any]:
    state = request.app.state
    try:
        async with state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        response.status_code = 503
        return {"status": "db_unavailable", "error": str(exc)[:200]}
    if not state.ready:
        response.status_code = 503
        return {"status": "migrating"}
    return {"status": "ready"}
