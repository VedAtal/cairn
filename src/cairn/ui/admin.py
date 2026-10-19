from __future__ import annotations

from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..core.access import Principal
from ..models import AdminRequest, Archived, Change, WriteRequest
from .base import federation, form, problem, render, user

router = APIRouter(prefix="/ui")
PAGE = 50
_FLAGS = ("read_only", "no_admin", "active")
_PLACE = {"create_key": "account", "revoke_key": "account", "create_user": "users", "password_link": "users",
          "set_user_active": "users", "create_team": "teams", "set_team_member": "team:{team}",
          "create_project": "projects", "archive_project": "projects", "unarchive_project": "projects",
          "set_project_member": "project:{project}"}
_DONE = {"create_key": "Key created. Copy it now: it is shown only once.", "revoke_key": "Key revoked.",
         "create_user": "Created {email}. Hand them this API key (shown only once) and set-password link.",
         "password_link": "New set-password link for {email}:", "set_user_active": "Updated {email}.",
         "create_team": "Created team {team}.", "set_team_member": "Updated {email} in {team}.",
         "create_project": "Created project {project}.", "archive_project": "Archived {project}.",
         "unarchive_project": "Restored {project}.", "set_project_member": "Updated {email} in {project}."}


def _store(request: Request, store: str | None) -> str:
    return store or federation(request).stores[0].alias


@router.get("/changes")
async def changes(request: Request, store: str | None = None, before: int | None = None) -> HTMLResponse:
    p = await user(request)
    store, rows, error = _store(request, store), list[Change](), None
    try:
        rows = await federation(request).changes(p, store, before, PAGE)
    except Exception as exc:
        error = problem(exc)
    older = rows[-1].seq if len(rows) == PAGE else None
    return render(request, "changes.html", store=store, rows=rows, before=before, older=older, error=error)


@router.get("/archived")
async def archived(request: Request, store: str | None = None) -> HTMLResponse:
    return await _archived(request, await user(request), store, None)


@router.post("/archived")
async def unarchive(request: Request, store: str | None = None) -> HTMLResponse:
    p = await user(request)
    ref = (await form(request)).get("ref", "")
    try:
        await federation(request).write(p, WriteRequest(op="unarchive", ref=ref))
        notice: dict[str, Any] = {"at": "top", "text": f"Restored {ref}."}
    except Exception as exc:
        notice = {"at": ref, "error": problem(exc)}
    return await _archived(request, p, store, notice)


async def _archived(request: Request, p: Principal, store: str | None, notice: dict[str, Any] | None) -> HTMLResponse:
    store, rows, error = _store(request, store), list[Archived](), None
    try:
        rows = await federation(request).archived(p, store)
    except Exception as exc:
        error = problem(exc)
    if notice and notice["at"] not in {a.ref for a in rows}:
        notice["at"] = "top"
    return render(request, "archived.html", store=store, rows=rows, error=error, notice=notice)


@router.get("/admin")
async def admin_page(request: Request, store: str | None = None) -> HTMLResponse:
    return await _admin(request, await user(request), _store(request, store), None)


@router.post("/admin")
async def admin_action(request: Request, store: str | None = None) -> HTMLResponse:
    p = await user(request)
    store, fields = _store(request, store), await form(request)
    op = fields.get("op", "")
    args = {k: v == "true" if k in _FLAGS else v for k, v in fields.items()
            if k not in ("op", "csrf") and v not in ("", "remove")}
    notice: dict[str, Any] = {"at": _PLACE.get(op, "top").format_map(defaultdict(str, args))}
    try:
        result = (await federation(request).admin(p, AdminRequest(op=op, store=store, args=args))).result
        notice |= {"text": _DONE.get(op, "Done.").format_map(defaultdict(str, result)),
                   "secrets": _secrets(request, store, result)}
    except Exception as exc:
        notice["error"] = problem(exc)
    return await _admin(request, p, store, notice)


def _secrets(request: Request, store: str, result: dict[str, Any]) -> list[tuple[str, str]]:
    out = [("API key", str(result["api_key"]))] if "api_key" in result else []
    if link := result.get("password_link"):
        remote = request.app.state.settings.remote()
        base = remote.url if remote and remote.alias == store else str(request.base_url)
        out.append(("Set-password link", base.rstrip("/") + link))
    return out


async def _admin(request: Request, p: Principal, store: str, notice: dict[str, Any] | None) -> HTMLResponse:
    fed = federation(request)

    async def call(op: str, **args: Any) -> dict[str, Any]:
        return (await fed.admin(p, AdminRequest(op=op, store=store, args=args))).result

    async def members(**args: Any) -> dict[str, str] | str:
        try:
            found: dict[str, str] = (await call("members", **args))["members"]
            return found
        except Exception as exc:
            return problem(exc)

    try:
        who = await call("whoami")
        keys = (await call("keys"))["keys"]
        users = (await call("users"))["users"] if who["instance_admin"] else []
        listing = (await call("teams"))["teams"]
    except Exception as exc:
        return render(request, "admin.html", store=store, error=problem(exc), notice=notice)
    slugs = [t["slug"] for t in listing]
    teams = {slug: await members(team=slug) for slug in slugs}
    admin_of = [t["slug"] for t in listing if t["admin"]]
    managed = [(pr, await members(project=pr["ref"])) for t in listing if t["admin"] for pr in t["projects"]]
    shown = {"account", "teams", *(f"team:{s}" for s in slugs), *(f"project:{pr["ref"]}" for pr, _ in managed),
             *(["users"] if who["instance_admin"] else []), *(["projects"] if admin_of else [])}
    if notice and notice["at"] not in shown:
        notice["at"] = "top"
    return render(request, "admin.html", store=store, notice=notice, who=who, keys=keys, users=users, teams=teams,
                  admin_of=admin_of, managed=managed)
