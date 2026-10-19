from __future__ import annotations

import hashlib
import hmac
import json
import threading
from collections.abc import Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update

from cairn.cli import config
from cairn.cli import main as cli_main
from cairn.core import accounts, maintenance
from cairn.core.access import Principal
from cairn.core.store import RemoteStore, SqlStore
from cairn.core.write import now
from cairn.db.schema import audit_log, change_log, credentials, entry_revisions, links, meta, term_df, vocab, write_ops
from cairn.models import EntryFields, LinkIn, WriteRequest
from tests.conftest import UI, Org, outcome


async def test_quota_and_idempotent_retries(org: Org) -> None:
    w = org.w
    store = SqlStore(w.store.engine, w.store.alias, quota=3)
    request = WriteRequest(op="add", node=org.node, op_id="op-1",
                           entry=EntryFields(type="gotcha", title="Refunds take ten days to settle"))
    first = await store.write(org.writer, request)
    assert (await store.write(org.writer, request)).ref == first.ref  # replayed, not written twice
    async with w.store.engine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(change_log).where(
            change_log.c.by == org.writer.user_id))).scalar_one() == 1
    await store.write(org.writer, WriteRequest(op="verify", ref=first.ref))
    for title in ("Invoices are numbered per account", "Payouts batch at midnight UTC"):
        await store.write(org.writer, WriteRequest(op="add", node=org.node, entry=EntryFields(
            type="general", title=title)))
    assert await outcome(store.write(org.writer, WriteRequest(op="add", node=org.node, entry=EntryFields(
        type="general", title="Disputes freeze the payout")))) == "quota"
    assert await outcome(w.store.write(org.writer, WriteRequest(op="add", node=org.node, entry=EntryFields(
        type="general", title="Disputes freeze the payout")))) == "ok"  # local stores have no quota


async def test_remote_writes_retry_with_the_same_op_id() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["op_id"])
        if len(seen) == 1:
            raise httpx.ConnectError("reset")
        return httpx.Response(200, json={"ok": True, "ref": "acme:e1"})

    store = RemoteStore("http://remote", "k", "acme")
    store.client = httpx.AsyncClient(base_url="http://remote/api/v1", transport=httpx.MockTransport(handle))
    assert (await store.write(Principal(1), WriteRequest(op="verify", ref="acme:e1"))).ref == "acme:e1"
    assert len(seen) == 2 and seen[0] == seen[1] and len(seen[0]) == 32
    await store.aclose()


async def test_admin_changes_are_audited(org: Org) -> None:
    w = org.w
    await w.admin(w.root, "create_team", slug="ops")
    await w.admin(w.root, "users")
    row = (await w.admin(w.root, "audit"))["audit"][0]
    assert (row["by"], row["op"], row["detail"]) == ("root@acme.dev", "create_team", {"slug": "ops"})
    assert (await w.admin(w.root, "audit", before=row["id"]))["audit"][0]["id"] < row["id"]
    assert await outcome(w.admin(org.admin, "audit")) == "admin_required"
    async with w.store.engine.connect() as conn:
        ops = [r.op for r in await conn.execute(select(audit_log.c.op).order_by(audit_log.c.id))]
    assert ops[:3] == ["create_team", "create_user", "set_team_member"] and "users" not in ops


async def test_purge_removes_every_trace(org: Org) -> None:
    w = org.w
    keep = await w.add(org.writer, org.node, "Payouts batch at midnight UTC")
    gone = (await w.write(org.writer, "add", node=org.node, entry=EntryFields(
        type="gotcha", title="Quokkafoo signing uses a shared key", links=[LinkIn(to=keep, reason="same batch")],
    ))).ref
    assert gone
    await w.write(org.writer, "update", ref=keep, base_revision=1, entry=EntryFields(
        links=[LinkIn(to=gone, reason="shares the key")]))
    assert await outcome(w.admin(org.writer, "purge_entry", ref=gone)) == "admin_required"
    await w.admin(org.admin, "purge_entry", ref=gone)
    entry_id = int(gone.split(":e")[1])
    assert (await w.get(org.reader, gone))[0].status == "not_found"
    assert await w.search(org.reader, "quokkafoo") == []
    async with w.store.engine.connect() as conn:
        assert (await conn.execute(select(entry_revisions).where(entry_revisions.c.entry_id == entry_id))).first() \
            is None
        assert (await conn.execute(select(links))).all() == []
        assert (await conn.execute(select(term_df).where(term_df.c.term == "quokkafoo"))).first() is None
        assert (await conn.execute(select(vocab).where(vocab.c.term == "quokkafoo"))).first() is None
        summaries = [r.summary for r in await conn.execute(select(change_log).where(
            change_log.c.ref == f"e{entry_id}"))]
    assert summaries == ["(purged)"] * len(summaries) and len(summaries) == 2


async def test_maintenance_runs_once_an_hour_and_prunes(org: Org) -> None:
    engine = org.w.store.engine
    async with engine.begin() as conn:
        await accounts.ui_ticket(conn, org.writer)
        await conn.execute(write_ops.insert().values(user_id=org.writer.user_id, op_id="old",
                                                     at=now() - timedelta(days=2), result={}))
        await conn.execute(update(credentials).where(credentials.c.kind == "ui_ticket")
                           .values(expires_at=now() - timedelta(seconds=1)))
        assert await maintenance.claim(conn)
        await maintenance.prune(conn)
        assert not await maintenance.claim(conn)
        assert (await conn.execute(select(credentials).where(credentials.c.kind == "ui_ticket"))).first() is None
        assert (await conn.execute(select(write_ops))).first() is None
        assert (await conn.execute(select(credentials).where(credentials.c.kind == "api_key"))).first()
        await conn.execute(update(meta).where(meta.c.key == "prune_due").values(value="2000-01-01T00:00:00+00:00"))
        assert await maintenance.claim(conn)


async def test_logins_are_throttled_per_account(hosted_ui: UI) -> None:
    ui = hosted_ui
    for _ in range(accounts.LOGIN_LIMIT):
        bad = await ui.client.post("/ui/login", data={"email": "Root@acme.dev", "password": "wrong password"})
        assert bad.status_code == 401
    blocked = await ui.client.post("/ui/login", data={"email": "root@acme.dev", "password": "wrong password"})
    assert blocked.status_code == 429 and "too many failed logins" in blocked.text
    assert (await ui.client.post("/ui/login", data={"email": "other@acme.dev", "password": "x"})).status_code == 401
    accounts._failures.clear()


async def test_hello_proves_the_local_token(local_ui: UI) -> None:
    body = (await local_ui.client.get("/api/v1/hello?nonce=abc")).json()
    assert body["proof"] == hmac.new(local_ui.api_token.encode(), b"abc", "sha256").hexdigest()
    assert "proof" not in (await local_ui.client.get("/api/v1/hello")).json()


@pytest.fixture
def squatter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, dict[str, str]]]:
    monkeypatch.setenv("CAIRN_HOME", str(tmp_path))
    config.write_private(tmp_path / "run" / "token", "the-token")
    answer: dict[str, str] = {"key": "wrong"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            nonce = self.path.split("nonce=")[1]
            proof = hmac.new(answer["key"].encode(), nonce.encode(), "sha256").hexdigest()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"proof": proof}).encode())

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], answer
    server.shutdown()


def test_clients_send_the_token_only_to_a_proven_server(squatter: tuple[int, dict[str, str]]) -> None:
    port, answer = squatter
    with pytest.raises(config.CliError, match="not sending the token"):
        config.proven_token(port)
    answer["key"] = "the-token"
    assert config.proven_token(port) == "the-token"


def test_resumed_sessions_skip_unchanged_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_HOME", str(tmp_path))
    started, resumed = {"session_id": "s1", "source": "startup"}, {"session_id": "s1", "source": "resume"}
    assert not cli_main._unchanged_on_resume(started, "notes")
    assert cli_main._unchanged_on_resume(resumed, "notes")
    assert not cli_main._unchanged_on_resume(resumed, "new notes")
    assert not cli_main._unchanged_on_resume({**resumed, "session_id": "s2"}, "new notes")
    assert not cli_main._unchanged_on_resume({"source": "resume"}, "new notes")
    marker = tmp_path / "cache" / f"session-{hashlib.sha256(b's1').hexdigest()[:32]}"
    assert marker.read_text() == hashlib.sha256(b"new notes").hexdigest()
