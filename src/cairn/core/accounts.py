from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from ..db.schema import credentials, users
from .access import AccessError, Principal
from .write import WriteError, now

SESSION_TTL, TICKET_TTL, LINK_TTL = timedelta(days=7), timedelta(seconds=60), timedelta(days=7)
MIN_PASSWORD = 10
_N, _R, _P = 2**14, 8, 1
_DUMMY = "scrypt$16384$8$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(key).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, n, r, p, salt, key = (stored or _DUMMY).split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    except ValueError:
        return False
    return algo == "scrypt" and stored is not None and hmac.compare_digest(got, base64.b64decode(key))


async def _issue(conn: AsyncConnection, user_id: int, kind: str, ttl: timedelta) -> str:
    token = secrets.token_urlsafe(32)
    await conn.execute(credentials.insert().values(user_id=user_id, kind=kind, sha256=hash_token(token),
                                                   token_prefix=token[:8], created_at=now(), expires_at=now() + ttl))
    return token


async def _consume(conn: AsyncConnection, token: str, kind: str) -> int | None:
    row = (await conn.execute(update(credentials).where(
        credentials.c.sha256 == hash_token(token), credentials.c.kind == kind, credentials.c.revoked_at.is_(None),
        credentials.c.expires_at > now(),
    ).values(revoked_at=now()).returning(credentials.c.user_id))).first()
    return int(row.user_id) if row else None


async def login(conn: AsyncConnection, email: str, password: str) -> str | None:
    user = (await conn.execute(select(users.c.id, users.c.password_hash).where(
        users.c.email == email.strip().lower(), users.c.active))).first()
    # Hash even for unknown emails, so timing doesn't reveal which accounts exist.
    if not await asyncio.to_thread(verify_password, password, user.password_hash if user else None) or user is None:
        return None
    return await _issue(conn, user.id, "web_session", SESSION_TTL)


async def session_principal(conn: AsyncConnection, token: str) -> Principal | None:
    row = (await conn.execute(select(users.c.id, users.c.is_instance_admin).join(
        credentials, credentials.c.user_id == users.c.id).where(
        credentials.c.sha256 == hash_token(token), credentials.c.kind == "web_session",
        credentials.c.revoked_at.is_(None), credentials.c.expires_at > now(), users.c.active,
    ))).first()
    return Principal(row.id, row.is_instance_admin) if row else None


async def logout(conn: AsyncConnection, token: str) -> None:
    await conn.execute(update(credentials).where(credentials.c.sha256 == hash_token(token),
                                                 credentials.c.kind == "web_session").values(revoked_at=now()))


async def password_link(conn: AsyncConnection, user_id: int) -> str:
    await conn.execute(update(credentials).where(credentials.c.user_id == user_id, credentials.c.kind == "password_set",
                                                 credentials.c.revoked_at.is_(None)).values(revoked_at=now()))
    return f"/ui/set-password?token={await _issue(conn, user_id, 'password_set', LINK_TTL)}"


async def set_password(conn: AsyncConnection, token: str, password: str) -> int:
    if len(password) < MIN_PASSWORD:
        raise WriteError(f"use at least {MIN_PASSWORD} characters")
    user_id = await _consume(conn, token, "password_set")
    if user_id is None:
        raise AccessError("this link is invalid or expired; ask your admin for a new one", "bad_token")
    hashed = await asyncio.to_thread(hash_password, password)  # scrypt off the event loop
    await conn.execute(update(users).where(users.c.id == user_id).values(password_hash=hashed))
    await conn.execute(update(credentials).where(credentials.c.user_id == user_id,
                                                 credentials.c.kind == "web_session").values(revoked_at=now()))
    return user_id


async def ui_ticket(conn: AsyncConnection, principal: Principal) -> str:
    if principal.read_only or principal.no_admin:  # a session carries full powers; a narrowed key mustn't mint one
        raise AccessError("narrowed keys can't open a UI session", "admin_required")
    return await _issue(conn, principal.user_id, "ui_ticket", TICKET_TTL)


async def redeem_ticket(conn: AsyncConnection, ticket: str) -> str | None:
    user_id = await _consume(conn, ticket, "ui_ticket")
    return await _issue(conn, user_id, "web_session", SESSION_TTL) if user_id else None
