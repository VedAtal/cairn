"""Local mode also checks Host and Origin because any web page can reach localhost."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..core.access import Principal

PUBLIC = ("/healthz", "/readyz", "/api/v1/hello", "/api/v1/bootstrap")

Authenticate = Callable[[str], Awaitable[Principal | None]]


class AuthMiddleware:
    def __init__(self, app: ASGIApp, authenticate: Authenticate, local_port: int | None = None) -> None:
        self.app, self.authenticate, self.local_port = app, authenticate, local_port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        if self.local_port is not None and (problem := self._local_check(request)):
            return await JSONResponse(problem[1], status_code=problem[0])(scope, receive, send)
        path = request.url.path
        if path in PUBLIC or path == "/" or path == "/ui" or path.startswith("/ui/"):  # the UI authenticates by cookie
            return await self.app(scope, receive, send)
        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        principal = await self.authenticate(token) if token else None
        if principal is None:
            body = {"code": "unauthorized", "message": "missing or invalid API key"}
            return await JSONResponse(body, status_code=401, headers={"WWW-Authenticate": "Bearer"})(
                scope, receive, send)
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)

    def _local_check(self, request: Request) -> tuple[int, dict[str, Any]] | None:
        allowed = {f"127.0.0.1:{self.local_port}", f"localhost:{self.local_port}"}
        if request.headers.get("host") not in allowed:
            return 421, {"code": "bad_host", "message": "unexpected Host header"}
        origin = request.headers.get("origin")
        if origin and origin.removeprefix("http://") not in allowed:
            return 403, {"code": "bad_origin", "message": "cross-origin requests are refused"}
        return None


def token_matches(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())
