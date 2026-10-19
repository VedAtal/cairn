from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from ..core import accounts
from ..core.access import AccessError, NotFound, Principal
from ..core.refs import RefError
from ..core.store import Federation, RemoteError, StoreUnavailable
from ..core.write import WriteError

HERE = Path(__file__).parent
# Content hash in asset URLs, so browsers refetch after an upgrade instead of running stale JS and CSS.
ASSETS = hashlib.sha256(b"".join(p.read_bytes() for p in sorted((HERE / "static").iterdir()))).hexdigest()[:12]
templates = Jinja2Templates(directory=HERE / "templates")
router = APIRouter(prefix="/ui")
_REF = re.compile(r"\b([a-z][a-z0-9-]{0,31}:[enp]\d+)\b")


class LoginRequired(Exception):
    pass


class StalePage(Exception):
    pass


def linkrefs(text: str) -> Markup:
    html = _REF.sub(lambda m: f'<a href="/ui/r/{m.group(1)}">{m.group(1)}</a>', str(escape(text)))
    return Markup(html.replace("\n", "<br>"))


templates.env.filters["linkrefs"] = linkrefs


def cookie_name(request: Request) -> str:
    port = request.app.state.local_port
    return f"cairn_session_{port}" if port else "cairn_session"  # cookies ignore ports; keep local servers apart


def _csrf(session: str) -> str:
    return hmac.new(session.encode(), b"csrf", hashlib.sha256).hexdigest()[:32]


async def user(request: Request) -> Principal:
    """Raises LoginRequired; non-GET requests must carry the session's CSRF token."""
    session = request.cookies.get(cookie_name(request))
    if not session:
        raise LoginRequired()
    async with request.app.state.engine.connect() as conn:
        principal = await accounts.session_principal(conn, session)
    if principal is None:
        raise LoginRequired()
    if request.method != "GET":
        given = request.headers.get("x-csrf") or (await form(request)).get("csrf", "")
        if not hmac.compare_digest(given, _csrf(session)):
            raise StalePage()
    request.state.principal, request.state.csrf = principal, _csrf(session)
    return principal


async def form(request: Request) -> dict[str, str]:
    if "form" not in request.state._state:
        raw = parse_qs((await request.body()).decode(), keep_blank_values=True)
        request.state.form = {k: v[-1] for k, v in raw.items()}
    return dict(request.state.form)


def federation(request: Request) -> Federation:
    fed: Federation = request.app.state.federation
    return fed


def render(request: Request, template: str, status: int = 200, **ctx: Any) -> HTMLResponse:
    state = request.app.state
    ctx |= {"assets": ASSETS, "local": state.settings.mode == "local", "path": request.url.path,
            "stores": [s.alias for s in state.federation.stores], "csrf": getattr(request.state, "csrf", "")}
    if principal := getattr(request.state, "principal", None):
        ctx |= {"me": principal}
    return templates.TemplateResponse(request, template, ctx, status_code=status)


def problem(exc: Exception) -> str:
    if isinstance(exc, WriteError | AccessError):
        return str(exc)
    if isinstance(exc, NotFound):
        return "not found (or not visible to you)"
    if isinstance(exc, RefError):
        return str(exc)
    if isinstance(exc, RemoteError):
        return str(exc.body.get("message", exc))
    if isinstance(exc, StoreUnavailable):
        return f"store {exc} is unreachable"
    raise exc


def _session_response(request: Request, session: str, to: str) -> Response:
    response = RedirectResponse(to if to.startswith("/ui") else "/ui", status_code=303)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(cookie_name(request), session, max_age=int(accounts.SESSION_TTL.total_seconds()),
                        httponly=True, samesite="lax", secure=secure, path="/")
    return response


@router.get("/login")
async def login_page(request: Request, ticket: str | None = None, next: str = "/ui") -> Response:
    if ticket:
        async with request.app.state.engine.begin() as conn:
            session = await accounts.redeem_ticket(conn, ticket)
        if session:
            return _session_response(request, session, next)
        return render(request, "login.html", 400, error="That login link is invalid or expired.", next=next)
    return render(request, "login.html", next=next)


@router.post("/login")
async def login_submit(request: Request) -> Response:
    fields = await form(request)
    try:
        async with request.app.state.engine.begin() as conn:
            session = await accounts.login(conn, fields.get("email", ""), fields.get("password", ""))
    except AccessError as exc:
        return render(request, "login.html", 429, error=str(exc), next=fields.get("next", "/ui"))
    if not session:
        return render(request, "login.html", 401, error="Wrong email or password.", next=fields.get("next", "/ui"))
    return _session_response(request, session, fields.get("next", "/ui"))


@router.post("/logout")
async def logout(request: Request) -> Response:
    await user(request)
    async with request.app.state.engine.begin() as conn:
        await accounts.logout(conn, request.cookies[cookie_name(request)])
    response = RedirectResponse("/ui/login", status_code=303)
    response.delete_cookie(cookie_name(request), path="/")
    return response


@router.get("/set-password")
async def set_password_page(request: Request, token: str = "") -> Response:
    return render(request, "set_password.html", token=token)


@router.post("/set-password")
async def set_password_submit(request: Request) -> Response:
    fields = await form(request)
    if fields.get("password") != fields.get("confirm"):
        return render(request, "set_password.html", 400, token=fields.get("token", ""), error="Passwords don't match.")
    try:
        async with request.app.state.engine.begin() as conn:
            await accounts.set_password(conn, fields.get("token", ""), fields.get("password", ""))
    except (WriteError, AccessError) as exc:
        return render(request, "set_password.html", 400, token=fields.get("token", ""), error=str(exc))
    return RedirectResponse("/ui/login?next=/ui", status_code=303)


def stale_page(request: Request) -> Response:
    if request.headers.get("hx-request"):  # htmx doesn't swap error responses, so reload the page instead
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    back = urlsplit(request.headers.get("referer", ""))
    same_site = back.netloc == request.headers.get("host") and back.path.startswith("/ui")
    return RedirectResponse(back.path + (f"?{back.query}" if back.query else "") if same_site else "/ui",
                            status_code=303)


def login_redirect(request: Request) -> Response:
    if request.headers.get("hx-request"):  # an htmx swap can't follow a redirect to a full page; ask it to
        return Response(status_code=204, headers={"HX-Redirect": "/ui/login"})
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/ui/login?next={quote(target)}", status_code=303)
