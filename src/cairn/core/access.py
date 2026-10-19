"""Who can see and change what. Every query filters by the scope computed here (never by RLS)."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from ..db.schema import nodes, project_members, projects, team_members, teams


class AccessError(Exception):
    """Maps to 403 (or 404 when the target is unreadable, so unauthorized looks like absent)."""

    def __init__(self, message: str, code: str = "forbidden") -> None:
        super().__init__(message)
        self.code = code


class NotFound(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: int
    instance_admin: bool = False
    # Limits carried by the API key. Keys otherwise carry their owner's full powers, admin included.
    read_only: bool = False
    no_admin: bool = False


@dataclass
class Scope:
    principal: Principal
    readable: set[int] = field(default_factory=set)  # project ids (non-archived projects in non-archived teams)
    writable: set[int] = field(default_factory=set)
    admin_teams: set[int] = field(default_factory=set)  # includes archived teams, so admins can unarchive
    project_team: dict[int, int] = field(default_factory=dict)

    @property
    def instance_admin(self) -> bool:
        return self.principal.instance_admin and not (self.principal.no_admin or self.principal.read_only)

    def is_team_admin(self, team_id: int) -> bool:
        return team_id in self.admin_teams

    def require_read(self, project_id: int) -> None:
        if project_id not in self.readable:
            raise NotFound()

    def require_write(self, project_id: int) -> None:
        self.require_read(project_id)
        if project_id not in self.writable:
            raise AccessError("you have read-only access to this project", "read_only")

    def require_team_admin(self, team_id: int) -> None:
        if not self.is_team_admin(team_id):
            raise AccessError("team admin required", "admin_required")

    def require_instance_admin(self) -> None:
        if not self.instance_admin:
            raise AccessError("instance admin required", "admin_required")


async def scope_for(conn: AsyncConnection, principal: Principal) -> Scope:
    scope = Scope(principal)
    admin_ok = not (principal.no_admin or principal.read_only)
    team_roles = dict((await conn.execute(
        select(team_members.c.team_id, team_members.c.role).where(team_members.c.user_id == principal.user_id)
    )).all())
    project_roles = dict((await conn.execute(
        select(project_members.c.project_id, project_members.c.role).where(
            project_members.c.user_id == principal.user_id
        )
    )).all())
    all_teams = (await conn.execute(select(teams.c.id, teams.c.archived_at))).all()
    if admin_ok:
        scope.admin_teams = {
            tid for tid, _ in all_teams if principal.instance_admin or team_roles.get(tid) == "admin"
        }
    live_teams = {tid for tid, archived in all_teams if archived is None}
    rows = (await conn.execute(select(projects.c.id, projects.c.team_id, projects.c.archived_at))).all()
    for pid, tid, archived in rows:
        scope.project_team[pid] = tid
        if archived is not None or tid not in live_teams:
            continue
        # Team admins (and the instance admin) see and write everything in the team, even with a no_admin key.
        full = principal.instance_admin or team_roles.get(tid) == "admin"
        # Project roles count only while the user is still in the project's team.
        role = "write" if full else (project_roles.get(pid) if tid in team_roles else None)
        if role:
            scope.readable.add(pid)
            if role == "write" and not principal.read_only:
                scope.writable.add(pid)
    return scope


async def inject_locked(conn: AsyncConnection, node_path: str, project_id: int) -> bool:
    """True if the node at `node_path`, or any ancestor, is flagged for injection."""
    parts = node_path.split("/")
    ancestors = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    flagged = await conn.execute(
        select(nodes.c.id).where(nodes.c.project_id == project_id, nodes.c.path.in_(ancestors), nodes.c.inject)
    )
    return flagged.first() is not None
