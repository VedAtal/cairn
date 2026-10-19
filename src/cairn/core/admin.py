"""Accounts, teams, projects, membership, API keys and flags. Every op is reachable through the `admin` MCP tool."""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from ..db.schema import credentials, entries, nodes, project_members, projects, team_members, teams, users
from .access import AccessError, NotFound, Principal, Scope
from .refs import Ref, RefError, parse
from .write import _SLUG as SLUG
from .write import WriteError, _log, clean, get_entry, get_node, now

KEY_PREFIX = "cairn_pat_"


def _check_slug(slug: str) -> None:
    if not SLUG.match(slug):
        raise WriteError("slug must match [a-z0-9_]{1,48}")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_key(conn: AsyncConnection, user_id: int | None, kind: str = "api_key", name: str = "",
                    scopes: dict[str, bool] | None = None) -> str:
    token = (KEY_PREFIX if kind == "api_key" else "cairn_setup_") + secrets.token_urlsafe(32)
    await conn.execute(credentials.insert().values(
        user_id=user_id, kind=kind, sha256=hash_token(token), token_prefix=token[:16], name=name,
        scopes=scopes or {}, created_at=now(),
    ))
    return token


async def register_setup_token(conn: AsyncConnection, token: str) -> None:
    exists = await conn.execute(select(credentials.c.id).where(credentials.c.sha256 == hash_token(token)))
    if exists.first() is None:
        await conn.execute(credentials.insert().values(user_id=None, kind="setup", sha256=hash_token(token),
                                                       token_prefix=token[:8], created_at=now()))


async def principal_for_key(conn: AsyncConnection, token: str) -> Principal | None:
    row = (await conn.execute(
        select(credentials.c.scopes, users.c.id, users.c.is_instance_admin)
        .join(users, users.c.id == credentials.c.user_id)
        .where(credentials.c.sha256 == hash_token(token), credentials.c.kind == "api_key",
               credentials.c.revoked_at.is_(None), users.c.active)
    )).first()
    if row is None:
        return None
    scopes = row.scopes or {}
    return Principal(row.id, row.is_instance_admin, bool(scopes.get("read_only")), bool(scopes.get("no_admin")))


async def has_instance_admin(conn: AsyncConnection) -> bool:
    return (await conn.execute(select(users.c.id).where(users.c.is_instance_admin))).first() is not None


async def bootstrap(conn: AsyncConnection, token: str, email: str, name: str) -> str:
    """Exchange the one-time setup token (logged at first start) for the instance admin account and its API key."""
    if await has_instance_admin(conn):
        raise AccessError("this instance is already set up", "already_setup")
    # Consume the token atomically, so two concurrent bootstraps can't both create an instance admin.
    consumed = await conn.execute(update(credentials).where(
        credentials.c.sha256 == hash_token(token), credentials.c.kind == "setup", credentials.c.revoked_at.is_(None)
    ).values(revoked_at=now()))
    if consumed.rowcount != 1:
        raise AccessError("invalid setup token", "bad_token")
    user_id = await _create_user(conn, email, name, instance_admin=True)
    await conn.execute(update(credentials).where(credentials.c.kind == "setup").values(revoked_at=now()))
    return await issue_key(conn, user_id, name="bootstrap")


async def ensure_local(conn: AsyncConnection) -> int:
    """Local mode: one user who is instance admin, team `me`, project `personal`. Idempotent; returns the user id."""
    row = (await conn.execute(select(users.c.id).where(users.c.is_instance_admin))).first()
    if row:
        return int(row.id)
    user_id = await _create_user(conn, "local@localhost", "local", instance_admin=True)
    team_id = (await conn.execute(teams.insert().values(slug="me", name="Personal").returning(teams.c.id))).scalar_one()
    await conn.execute(team_members.insert().values(team_id=team_id, user_id=user_id, role="admin"))
    await conn.execute(projects.insert().values(team_id=team_id, slug="personal", name="Personal"))
    return user_id


async def _create_user(conn: AsyncConnection, email: str, name: str, instance_admin: bool = False) -> int:
    email = clean(email).lower()
    if "@" not in email:
        raise WriteError("email looks invalid")
    if (await conn.execute(select(users.c.id).where(users.c.email == email))).first():
        raise WriteError(f"user {email} already exists", "conflict")
    return int((await conn.execute(users.insert().values(
        email=email, name=clean(name) or email, is_instance_admin=instance_admin, active=True, created_at=now()
    ).returning(users.c.id))).scalar_one())


async def _user_id(conn: AsyncConnection, email: str) -> int:
    row = (await conn.execute(select(users.c.id).where(users.c.email == clean(email).lower()))).first()
    if row is None:
        raise NotFound()
    return int(row.id)


def _arg(args: dict[str, Any], name: str, kind: type = str, required: bool = True) -> Any:
    value = args.get(name)
    if value is None:
        if required:
            raise WriteError(f"missing argument {name!r}")
        return None
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise WriteError(f"argument {name!r} must be {kind.__name__}")
    return value


async def _team(conn: AsyncConnection, scope: Scope, slug: str) -> Any:
    """A team the caller may know about: its admin, the instance admin, or a reader of one of its projects."""
    row = (await conn.execute(select(teams).where(teams.c.slug == slug))).first()
    visible = row is not None and (row.id in scope.admin_teams
                                   or any(scope.project_team.get(p) == row.id for p in scope.readable))
    if not visible:
        raise NotFound()
    return row


def _ref(alias: str, ref: str, kinds: str) -> Ref:
    parsed = parse(ref, kinds)
    if parsed.store != alias:
        raise RefError(f"{ref} belongs to store {parsed.store!r}, not {alias!r}")
    return parsed


async def _project(conn: AsyncConnection, scope: Scope, alias: str, ref: str) -> Any:
    row = (await conn.execute(select(projects).where(projects.c.id == _ref(alias, ref, "p").id))).first()
    if row is None or (row.team_id not in scope.admin_teams and row.id not in scope.readable):
        raise NotFound()
    return row


async def _set_role(conn: AsyncConnection, table: Any, key: str, key_id: int, user_id: int, role: str | None,
                    allowed: tuple[str, ...]) -> None:
    if role is not None and role not in allowed:
        raise WriteError(f"role must be one of {', '.join(allowed)} (or null to remove)")
    await conn.execute(delete(table).where(table.c[key] == key_id, table.c.user_id == user_id))
    if role is not None:
        await conn.execute(table.insert().values({key: key_id, "user_id": user_id, "role": role}))


async def _team_role(conn: AsyncConnection, team_id: int, user_id: int) -> str | None:
    return (await conn.execute(select(team_members.c.role).where(
        team_members.c.team_id == team_id, team_members.c.user_id == user_id))).scalar_one_or_none()


OPS = ("whoami", "users", "create_user", "set_user_active", "create_team", "archive_team", "unarchive_team",
       "set_team_member", "create_project", "archive_project", "unarchive_project", "set_project_member", "members",
       "keys", "create_key", "revoke_key", "flag", "unflag")


async def run(conn: AsyncConnection, scope: Scope, alias: str, op: str, args: dict[str, Any]) -> dict[str, Any]:
    user, narrowed = scope.principal.user_id, scope.principal.read_only or scope.principal.no_admin
    if op not in OPS:
        raise WriteError(f"unknown admin op {op!r}; ops: {', '.join(OPS)}")
    if op == "whoami":
        me = (await conn.execute(select(users.c.email, users.c.name).where(users.c.id == user))).one()
        team_rows = (await conn.execute(select(teams.c.slug, team_members.c.role).join(
            team_members, team_members.c.team_id == teams.c.id).where(team_members.c.user_id == user))).all()
        return {"email": me.email, "name": me.name, "instance_admin": scope.instance_admin,
                "teams": {t.slug: t.role for t in team_rows}}
    if op == "keys":
        rows = await conn.execute(select(credentials.c.token_prefix, credentials.c.name, credentials.c.scopes,
                                         credentials.c.created_at, credentials.c.revoked_at).where(
            credentials.c.user_id == user, credentials.c.kind == "api_key").order_by(credentials.c.id))
        return {"keys": [{**r._mapping, "created_at": str(r.created_at)[:19],
                          "revoked_at": r.revoked_at and str(r.revoked_at)[:19]} for r in rows]}
    if op in ("create_key", "revoke_key"):
        if narrowed:  # a narrowed key must not mint keys or cut off its owner's other keys
            raise AccessError("this key is narrowed (read_only or no_admin); manage keys with a full key",
                              "admin_required")
        if op == "create_key":
            scopes = {k: True for k in ("read_only", "no_admin") if args.get(k) is True}
            return {"api_key": await issue_key(conn, user, name=_arg(args, "name", required=False) or "",
                                               scopes=scopes), "note": "shown once; store it now"}
        q = update(credentials).where(credentials.c.token_prefix == _arg(args, "prefix"),
                                      credentials.c.kind == "api_key")
        if not scope.instance_admin:
            q = q.where(credentials.c.user_id == user)
        return {"revoked": (await conn.execute(q.values(revoked_at=now()))).rowcount}
    if op == "users":
        scope.require_instance_admin()
        rows = await conn.execute(select(users.c.email, users.c.name, users.c.active, users.c.is_instance_admin)
                                  .order_by(users.c.id))
        return {"users": [dict(r._mapping) for r in rows]}
    if op == "create_user":
        scope.require_instance_admin()
        new_id = await _create_user(conn, _arg(args, "email"), _arg(args, "name", required=False) or "")
        return {"email": args["email"], "api_key": await issue_key(conn, new_id, name="initial"),
                "note": "hand this key to the user; it is shown once"}
    if op == "set_user_active":
        scope.require_instance_admin()
        active = _arg(args, "active", bool)
        await conn.execute(update(users).where(users.c.id == await _user_id(conn, _arg(args, "email")))
                           .values(active=active))
        return {"email": args["email"], "active": active}
    if op == "create_team":
        scope.require_instance_admin()
        slug = _arg(args, "slug")
        _check_slug(slug)
        if (await conn.execute(select(teams.c.id).where(teams.c.slug == slug))).first():
            raise WriteError(f"team {slug} already exists", "conflict")
        team_id = (await conn.execute(teams.insert().values(slug=slug, name=_arg(args, "name", required=False) or slug)
                                      .returning(teams.c.id))).scalar_one()
        if admin_email := _arg(args, "admin", required=False):
            await _set_role(conn, team_members, "team_id", team_id, await _user_id(conn, admin_email), "admin",
                            ("admin",))
        return {"team": slug}
    if op in ("archive_team", "unarchive_team"):
        scope.require_instance_admin()
        team = await _team(conn, scope, _arg(args, "team"))
        await conn.execute(update(teams).where(teams.c.id == team.id)
                           .values(archived_at=None if op.startswith("un") else now()))
        return {"team": team.slug, "archived": not op.startswith("un")}
    if op == "set_team_member":
        team = await _team(conn, scope, _arg(args, "team"))
        scope.require_team_admin(team.id)
        member, role = await _user_id(conn, _arg(args, "email")), _arg(args, "role", required=False)
        if "admin" in (role, await _team_role(conn, team.id, member)):
            scope.require_instance_admin()  # only the instance admin appoints or removes team admins
        await _set_role(conn, team_members, "team_id", team.id, member, role, ("admin", "member"))
        if role is None:  # leaving the team ends access to its projects
            await conn.execute(delete(project_members).where(
                project_members.c.user_id == member,
                project_members.c.project_id.in_(select(projects.c.id).where(projects.c.team_id == team.id))))
        return {"team": team.slug, "email": args["email"], "role": role}
    if op == "create_project":
        team = await _team(conn, scope, _arg(args, "team"))
        scope.require_team_admin(team.id)
        slug = _arg(args, "slug")
        _check_slug(slug)
        taken = select(projects.c.id).where(projects.c.team_id == team.id, projects.c.slug == slug)
        if (await conn.execute(taken)).first():
            raise WriteError(f"project {slug} already exists in {team.slug}", "conflict")
        pid = (await conn.execute(projects.insert().values(team_id=team.id, slug=slug,
                                                           name=_arg(args, "name", required=False) or slug)
                                  .returning(projects.c.id))).scalar_one()
        return {"project": f"{alias}:p{pid}"}
    if op in ("archive_project", "unarchive_project"):
        project = await _project(conn, scope, alias, _arg(args, "project"))
        scope.require_team_admin(project.team_id)
        await conn.execute(update(projects).where(projects.c.id == project.id)
                           .values(archived_at=None if op.startswith("un") else now()))
        return {"project": args["project"], "archived": not op.startswith("un")}
    if op == "set_project_member":
        project = await _project(conn, scope, alias, _arg(args, "project"))
        scope.require_team_admin(project.team_id)
        member, role = await _user_id(conn, _arg(args, "email")), _arg(args, "role", required=False)
        if role is not None and await _team_role(conn, project.team_id, member) is None:
            raise WriteError(f"{args['email']} isn't in this project's team; add them to the team first")
        await _set_role(conn, project_members, "project_id", project.id, member, role, ("write", "read"))
        return {"project": args["project"], "email": args["email"], "role": role}
    if op == "members":
        if args.get("project"):
            project = await _project(conn, scope, alias, _arg(args, "project"))
            table, key, key_id = project_members, "project_id", project.id
        else:
            team = await _team(conn, scope, _arg(args, "team"))
            table, key, key_id = team_members, "team_id", team.id
        rows = await conn.execute(select(users.c.email, table.c.role).join(users, users.c.id == table.c.user_id)
                                  .where(table.c[key] == key_id).order_by(users.c.email))
        return {"members": {r.email: r.role for r in rows}}
    # flag / unflag: what gets injected into every reader's sessions. Team admins only.
    ref = _ref(alias, _arg(args, "ref"), "en")
    if ref.kind == "e":
        _, node = await get_entry(conn, scope, ref.id)
        table = entries
    else:
        node = await get_node(conn, scope, ref.id)
        table = nodes
    scope.require_team_admin(scope.project_team[node.project_id])
    await conn.execute(update(table).where(table.c.id == ref.id).values(inject=op == "flag"))
    await _log(conn, node.project_id, f"{ref.kind}{ref.id}", op, user, str(ref))
    return {"ref": str(ref), "inject": op == "flag"}


async def counts(conn: AsyncConnection, pids: list[int]) -> dict[str, int]:
    n = (await conn.execute(select(func.count()).select_from(entries).where(
        entries.c.is_searchable, entries.c.project_id.in_(pids)))).scalar_one()
    return {"entries": int(n), "projects": len(pids)}


__all__ = ["run", "bootstrap", "ensure_local", "issue_key", "principal_for_key", "has_instance_admin", "counts"]
