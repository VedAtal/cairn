from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.exc import IntegrityError

from .. import __version__
from ..core import admin
from ..core.access import AccessError, NotFound, Principal
from ..core.refs import RefError
from ..core.store import Federation, RemoteError, RemoteStore, SqlStore, Store, StoreUnavailable
from ..core.write import WriteError
from ..db import migrate
from ..db.engine import make_engine
from ..mcp.server import build_mcp
from ..routers import api, health
from .logging import setup_logging
from .security import AuthMiddleware, token_matches
from .settings import Settings

log = structlog.get_logger("cairn")


def build_app(settings: Settings | None = None, port: int | None = None) -> FastAPI:
    settings = settings or Settings()
    setup_logging(settings.log_level, settings.json_logs)
    local = settings.mode == "local"
    if local:
        settings.home_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    engine = make_engine(settings.database_url)
    stores: list[Store] = [SqlStore(engine, settings.alias)]
    remote = settings.remote()
    if remote:
        stores.append(RemoteStore(remote.url, remote.api_key, remote.alias))
    federation = Federation(stores)
    mcp = build_mcp(lambda: federation)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(migrate.upgrade, settings.db.migrate_url or settings.database_url)
        async with engine.begin() as conn:
            if local:
                app.state.local_user = await admin.ensure_local(conn)
                app.state.local_token = _local_token(settings)
            elif not await admin.has_instance_admin(conn):
                if settings.setup_token:
                    await admin.register_setup_token(conn, settings.setup_token)
                else:
                    token = await admin.issue_key(conn, None, kind="setup")
                    log.warning("no instance admin yet; create one with: cairn remote bootstrap <url> --token <token>",
                                token=token)
        app.state.ready = True
        async with mcp.session_manager.run():
            yield
        for store in stores:
            if isinstance(store, RemoteStore):
                await store.aclose()
        await engine.dispose()

    app = FastAPI(title="Cairn", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings, app.state.engine, app.state.federation, app.state.ready = settings, engine, federation, False

    async def authenticate(token: str) -> Principal | None:
        if local:
            ok = token_matches(token, app.state.local_token)
            return Principal(app.state.local_user, instance_admin=True) if ok else None
        async with engine.connect() as conn:
            return await admin.principal_for_key(conn, token)

    app.add_middleware(AuthMiddleware, authenticate=authenticate, local_port=port if local else None)
    _errors(app)
    app.include_router(health.router)
    app.include_router(api.router)
    # Last, at "/": a "/mcp" sub-mount redirects POST /mcp, which clients don't follow.
    app.mount("/", mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),  # AuthMiddleware checks
    ))
    return app


def _local_token(settings: Settings) -> str:
    path = settings.token_file
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(secrets.token_urlsafe(32))
        path.chmod(0o600)
    return path.read_text().strip()


def _errors(app: FastAPI) -> None:
    def handler(status: int, code: str | None = None):  # type: ignore[no-untyped-def]
        async def handle(_: Request, exc: Exception) -> JSONResponse:
            body = {"code": getattr(exc, "code", code), "message": str(exc) or code}
            if isinstance(exc, WriteError):
                body["duplicates"] = [d.model_dump() for d in exc.duplicates]
                status_ = 409 if exc.code in ("duplicate", "conflict", "archived") else 422
                return JSONResponse(body, status_code=status_)
            return JSONResponse(body, status_code=status)

        return handle

    app.add_exception_handler(WriteError, handler(422))
    app.add_exception_handler(AccessError, handler(403))
    app.add_exception_handler(NotFound, handler(404, "not_found"))
    app.add_exception_handler(RefError, handler(422, "invalid"))
    app.add_exception_handler(StoreUnavailable, handler(503, "unreachable"))

    async def integrity_error(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"code": "conflict", "message": "conflicting concurrent change; retry"}, status_code=409)

    app.add_exception_handler(IntegrityError, integrity_error)

    async def remote_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RemoteError)
        return JSONResponse(exc.body, status_code=exc.status)

    app.add_exception_handler(RemoteError, remote_error)
