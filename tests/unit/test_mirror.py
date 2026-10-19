from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from cairn.app.gitmirror import GitMirror
from cairn.cli import gitmode
from cairn.cli.config import CliError
from cairn.core import admin, mirror
from cairn.core.access import Principal
from cairn.core.store import SqlStore
from cairn.db import migrate
from cairn.db.engine import make_engine
from cairn.db.schema import entries, links, nodes, projects, teams, term_df
from cairn.models import EntryFields, GetRequest, LinkIn, NodeFields, SearchRequest, WriteRequest
from tests.conftest import Org, sqlite_url


async def seed(org: Org) -> dict[str, str]:
    w = org.w
    child = await w.node(org.writer, org.node, "webhooks", "Webhooks", "Signatures — and retries ✓")
    gone = await w.node(org.writer, org.node, "legacy", "Legacy", "Old flow")
    a = await w.add(org.writer, child, "Signatures expire after five minutes", "Clock skew breaks them.\nUse NTP.",
                    keywords=["hmac", "ntp"])
    b = (await w.write(org.writer, "add", node=org.node, entry=EntryFields(
        type="decision", title="Refunds reuse the webhook queue",
        links=[LinkIn(to=a, reason="its retries apply to refunds")]))).ref
    c = await w.add(org.writer, gone, "Legacy flow used polling")
    await w.add(org.writer, child, "Archived note about retries")
    odd = await w.add(org.writer, child, "Line\u2028separators and NEL\x85 survive", "")
    await w.write(org.writer, "archive", ref=odd)
    await w.write(org.writer, "update", ref=a, base_revision=1, entry=EntryFields(body="Clock skew breaks them."))
    await w.write(org.writer, "archive", ref=gone, reason="replaced")
    assert b and c
    await w.write(org.writer, "update_node", ref=child, base_revision=1, node_fields=NodeFields(
        links=[LinkIn(to=b, reason="refunds ride on these retries")]))
    await w.admin(org.admin, "flag", ref=child)
    return {"a": a, "b": b, "c": c, "child": child}


async def restored(tmp: Path, files: dict[str, str]) -> SqlStore:
    tmp.mkdir()
    url = sqlite_url(tmp)
    await asyncio.to_thread(migrate.upgrade, url)
    store = SqlStore(make_engine(url), "acme")
    async with store.engine.begin() as conn:
        await mirror.restore(conn, files, await admin._create_user(conn, "local@localhost", "local", True))
    return store


async def snapshot(engine: AsyncEngine) -> dict[str, list[str]]:
    by = {"created_by", "updated_by", "verified_by", "archived_by"}  # implied: a local store has one user
    async with engine.connect() as conn:
        out = {t.name: sorted(str([v for k, v in r._mapping.items() if k not in by]) for r in await conn.execute(
            select(t))) for t in (teams, projects, nodes, entries)}
        out["links"] = sorted(str(r[1:7]) for r in await conn.execute(select(links)))
        out["df"] = sorted(str(r) for r in await conn.execute(select(term_df).where(term_df.c.df > 0)))
        for fts in ("SELECT rowid, an_head, an_body FROM entries_fts", "SELECT rowid, an_head, an_desc FROM nodes_fts"):
            out[fts] = sorted(str(r) for r in await conn.execute(text(fts)))
    return out


async def test_render_restore_round_trip(org: Org, tmp_path: Path) -> None:
    refs = await seed(org)
    async with org.w.store.engine.connect() as conn:
        files = await mirror.render(conn)
    assert any(p.endswith("/_node.md") for p in files) and "cairn.json" in files
    store = await restored(tmp_path / "restored", files)
    assert await snapshot(store.engine) == await snapshot(org.w.store.engine)
    async with store.engine.connect() as conn:
        assert await mirror.render(conn) == files
        owner = Principal(await mirror.owner(conn), instance_admin=True)
    hits = (await store.search(owner, SearchRequest(concepts=["signatures", "expire"]))).hits
    assert [h.ref for h in hits] == [refs["a"]]
    b, c = (await store.get(owner, GetRequest(refs=[refs["b"], refs["c"]]))).items
    assert [link.ref for link in b.links] == [refs["a"]] and c.status == "archived"
    new = await store.write(owner, WriteRequest(op="add", node=refs["child"], entry=EntryFields(
        type="general", title="A brand new fact after restore")))
    assert new.ref and int(new.ref.split(":e")[1]) > max(int(refs[k].split(":e")[1]) for k in "abc")
    await store.engine.dispose()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", f"--git-dir={repo}", *args], capture_output=True, text=True, check=True).stdout


async def test_git_mirror_commits_pushes_and_detects_divergence(org: Org, tmp_path: Path) -> None:
    await seed(org)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    m = GitMirror(tmp_path / "mirror.git", str(remote), org.w.store.engine)
    m.init()
    await m.sync()
    assert m.state == "ok" and not m.pending and git(remote, "rev-list", "--count", "main").strip() == "1"
    assert "Cairn-Digest:" in git(m.repo, "log", "-1", "--format=%B")
    await m.sync()
    assert git(m.repo, "rev-list", "--count", "main").strip() == "1"  # nothing changed, nothing committed

    restarted = GitMirror(m.repo, str(remote), org.w.store.engine)
    restarted.init()
    await restarted.sync()
    assert git(m.repo, "rev-list", "--count", "main").strip() == "1"

    await org.w.add(org.writer, org.node, "A second fact worth mirroring")
    await m.sync()
    assert git(remote, "rev-list", "--count", "main").strip() == "2"

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    (other / "x.md").write_text("from another machine\n")
    commit = ["-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "other"]
    for cmd in (["add", "x.md"], commit, ["push", "-q"]):
        subprocess.run(["git", "-C", str(other), *cmd], check=True)
    await org.w.add(org.writer, org.node, "A third fact that will not push")
    await m.sync()
    assert m.state == "diverged" and m.pending and "nothing was merged" in (m.error or "")


async def test_a_store_that_never_wrote_the_mirror_does_not_replace_it(org: Org, tmp_path: Path) -> None:
    await seed(org)
    m = GitMirror(tmp_path / "mirror.git", None, org.w.store.engine)
    m.init()
    await m.sync()
    head = git(m.repo, "rev-parse", "main")
    (tmp_path / "lost").mkdir()
    url = sqlite_url(tmp_path / "lost")
    await asyncio.to_thread(migrate.upgrade, url)
    fresh = make_engine(url)
    async with fresh.begin() as conn:
        await admin.ensure_local(conn)  # what the server does when cairn.db has gone missing
    stranger = GitMirror(m.repo, None, fresh)
    stranger.init()
    await stranger.sync()
    assert stranger.state == "diverged" and git(m.repo, "rev-parse", "main") == head
    await fresh.dispose()


def test_init_without_git_leaves_the_server_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    m = GitMirror(tmp_path / "mirror.git", None, None)  # type: ignore[arg-type]
    m.init()
    assert m.state == "error"


async def test_restore_stays_under_sqlites_default_variable_cap(tmp_path: Path) -> None:
    ts = datetime(2026, 10, 19, 12, 0, 0, 123456)
    common = {"keywords": [], "rev": 1, "inject": False, "created_at": ts, "updated_at": ts, "links": []}
    files = {"cairn.json": json.dumps({"format": mirror.FORMAT}) + "\n",
             "me/_team.md": mirror._doc(mirror.TEAM_KEYS, {"id": 1, "slug": "me", "name": "Personal"}),
             "me/personal/_project.md": mirror._doc(mirror.PROJECT_KEYS, {"id": 1, "slug": "personal", "name": "P"}),
             "me/personal/big/_node.md": mirror._doc(mirror.NODE_KEYS, {"id": 1, "slug": "big", "title": "Big",
                                                                         **common})}
    for i in range(1, 301):  # 12,000 distinct terms in one project
        files[f"me/personal/big/general-e{i}.md"] = mirror._doc(mirror.ENTRY_KEYS, {
            "id": i, "type": "general", "title": f"Fact {i}", "verified_at": ts, **common},
            " ".join(f"w{i}x{j}" for j in range(40)))
    url = sqlite_url(tmp_path)
    await asyncio.to_thread(migrate.upgrade, url)
    engine = make_engine(url)
    async with engine.connect() as conn:  # the uv-managed Python's SQLite keeps the default; Debian's raises it
        raw: Any = (await conn.get_raw_connection()).driver_connection
        await raw._execute(raw._conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32766)
    async with engine.begin() as conn:
        await mirror.restore(conn, files, await admin._create_user(conn, "local@localhost", "local", True))
    async with engine.connect() as conn:
        assert await mirror.render(conn) == files
    await engine.dispose()


async def test_restore_reads_the_mirror_raw_and_cleans_up_after_failure(
        org: Org, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await seed(org)
    async with org.w.store.engine.connect() as conn:
        files = await mirror.render(conn)
    home = tmp_path / "home"
    monkeypatch.setenv("CAIRN_HOME", str(home))
    (tmp_path / "gitconfig").write_text("[core]\n\tautocrlf = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    m = GitMirror(home / "mirror.git", None, org.w.store.engine)
    home.mkdir()
    m.init()
    await m.sync()
    assert gitmode._read(m.repo) == files
    m._commit({**files, "cairn.json": json.dumps({"format": mirror.FORMAT + 1}) + "\n"}, "newer")
    with pytest.raises(CliError, match="newer Cairn"):
        await asyncio.to_thread(gitmode.restore, None, None, False)
    assert not list(home.glob("cairn.db*")) and m.repo.exists()


def test_remote_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for url in ("https://ghp_abcdef@github.com/me/notes.git", "ssh://me:hunter2@example.com/notes.git"):
        with pytest.raises(CliError, match="credential"):
            gitmode._check_remote(url, private_ok=True)
    with pytest.raises(CliError, match="i-know-its-private"):
        gitmode._check_remote("git@example.com:me/notes.git", private_ok=False)
    assert gitmode._check_remote("git@example.com:me/notes.git", private_ok=True) == "git@example.com:me/notes.git"
    monkeypatch.chdir(tmp_path.parent)
    assert gitmode._check_remote(tmp_path.name, private_ok=False) == str(tmp_path.resolve())  # local: kept absolute


def test_cli_restore_rebuilds_the_store_from_its_mirror(tmp_path: Path) -> None:
    env = {**os.environ, "CAIRN_HOME": str(tmp_path / ".cairn"), "HOME": str(tmp_path)}

    def cairn(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "cairn.cli.main", *args], env=env, capture_output=True,
                              text=True, timeout=120)

    assert cairn("local", "enable", "--no-service", "--port", "6871").returncode == 0
    token = (tmp_path / ".cairn/run/token").read_text().strip()

    def api(path: str, body: dict[str, object] | None = None) -> dict[str, object]:
        req = urllib.request.Request(f"http://127.0.0.1:6871/api/v1{path}", None if body is None else
                                     json.dumps(body).encode(), {"Authorization": f"Bearer {token}",
                                                                 "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())  # type: ignore[no-any-return]

    node = api("/write", {"op": "create_node", "project": "me:p1", "node_fields": {"slug": "ops", "title": "Ops"}})
    entry = api("/write", {"op": "add", "node": node["ref"], "entry": {
        "type": "gotcha", "title": "Backups only count once restored", "body": "Test the restore path."}})
    assert cairn("git", "enable").returncode == 0
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and api("/status")["git"]["last_commit"] is None:  # type: ignore[index]
        time.sleep(0.5)
    assert api("/status")["git"]["state"] == "ok"  # type: ignore[index]
    assert cairn("local", "disable").returncode == 0
    (tmp_path / ".cairn/cairn.db").rename(tmp_path / "lost.db")
    for suffix in ("-wal", "-shm"):
        (tmp_path / f".cairn/cairn.db{suffix}").unlink(missing_ok=True)

    result = cairn("restore", "--no-service", "--port", "6871")
    assert result.returncode == 0, result.stderr
    try:
        [item] = api("/get", {"refs": [entry["ref"]]})["items"]  # type: ignore[misc]
        assert item["status"] == "ok" and item["title"] == "Backups only count once restored"
        assert cairn("restore").returncode == 1  # never over an existing store
    finally:
        cairn("local", "disable")
