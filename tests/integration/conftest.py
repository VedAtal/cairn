from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

SERVER = os.environ.get("CAIRN_TEST_SERVER", "").rstrip("/")
if not SERVER:
    collect_ignore_glob = ["test_*.py"]


class Api:
    def __init__(self, key: str | None = None, base: str = SERVER) -> None:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.http = httpx.Client(base_url=f"{base}/api/v1", headers=headers, timeout=30)

    def post(self, path: str, body: dict[str, Any] | None = None, status: int = 200) -> Any:
        resp = self.http.post(path, json=body or {})
        assert resp.status_code == status, f"{path}: {resp.status_code} {resp.text}"
        return resp.json()

    def admin(self, op: str, **args: Any) -> Any:
        return self.post("/admin", {"op": op, "args": args})["result"]

    def node(self, project: str, slug: str, title: str, summary: str) -> str:
        fields = {"slug": slug, "title": title, "summary": summary}
        return str(self.post("/write", {"op": "create_node", "project": project, "node_fields": fields})["ref"])

    def add(self, node: str, title: str, body: str, type_: str = "gotcha", status: int = 200) -> Any:
        resp = self.post("/write", {"op": "add", "node": node, "entry": {"type": type_, "title": title, "body": body}},
                         status)
        return resp["ref"] if status == 200 else resp


@asynccontextmanager
async def mcp_session(key: str, base: str = SERVER) -> AsyncIterator[ClientSession]:
    client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=30)
    async with streamable_http_client(f"{base}/mcp", http_client=client) as streams, \
            ClientSession(streams[0], streams[1]) as session:
        await session.initialize()
        yield session


async def mcp_text(key: str, tool: str, args: dict[str, Any], base: str = SERVER) -> str:
    async with mcp_session(key, base) as s:
        res = await s.call_tool(tool, args)
    return str(res.content[0].text)  # type: ignore[union-attr]


@dataclass
class World:
    alias: str
    admin: str
    alice: str
    bob: str
    dave: str
    carol: str
    eve: str
    billing: str
    search: str


def email(name: str) -> str:
    return f"{name}@acme.dev"


@pytest.fixture(scope="session")
def admin_key() -> str:
    resp = httpx.post(f"{SERVER}/api/v1/bootstrap", json={
        "token": os.environ["CAIRN_TEST_SETUP_TOKEN"], "email": email("root"), "name": "Root"})
    assert resp.status_code == 200, f"bootstrap failed ({resp.text}); start from a fresh stack (docker compose down -v)"
    return str(resp.json()["api_key"])


@pytest.fixture(scope="session")
async def world(admin_key: str) -> World:
    root = Api(admin_key)
    alias = root.http.get("/hello").json()["store"]
    keys = {n: root.admin("create_user", email=email(n), name=n.title())["api_key"]
            for n in ("alice", "bob", "carol", "dave")}
    root.admin("create_team", slug="platform", name="Platform", admin=email("alice"))
    for n in ("bob", "carol", "dave"):
        root.admin("set_team_member", team="platform", email=email(n), role="member")
    billing = Api(keys["alice"]).admin("create_project", team="platform", slug="billing", name="Billing")["project"]
    Api(keys["alice"]).admin("set_project_member", project=billing, email=email("bob"), role="write")
    Api(keys["alice"]).admin("set_project_member", project=billing, email=email("dave"), role="read")
    async def admin(key: str, op: str, **args: Any) -> Any:
        return json.loads(await mcp_text(key, "admin", {"op": op, "args": args}))

    eve = (await admin(admin_key, "create_user", email=email("eve")))["api_key"]
    await admin(keys["alice"], "set_team_member", team="platform", email=email("eve"), role="member")
    search = (await admin(keys["alice"], "create_project", team="platform", slug="search", name="Search"))["project"]
    await admin(keys["alice"], "set_project_member", project=search, email=email("eve"), role="write")
    return World(alias, admin_key, keys["alice"], keys["bob"], keys["dave"], keys["carol"], eve, billing, search)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def write_config(home: Path, url: str | None = None, key: str | None = None, alias: str | None = None) -> None:
    """Mirrors what `cairn remote add` writes."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(f'[remote]\nurl = "{url}"\nalias = "{alias}"\n' if url else "")
    creds = home / "credentials.toml"
    creds.write_text(f'[remote]\napi_key = "{key}"\n' if key else "")
    creds.chmod(0o600)


def wait_ready(url: str, proc: subprocess.Popen[bytes] | None = None, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert proc is None or proc.poll() is None, f"server exited with {proc.returncode}"
        try:
            if httpx.get(f"{url}/readyz", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"{url} not ready after {timeout}s")


@dataclass
class Local:
    url: str
    token: str
    home: Path


def start_local(home: Path) -> Iterator[Local]:
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    home.mkdir(parents=True, exist_ok=True)
    log = (home / "server.log").open("wb")
    proc = subprocess.Popen(["cairn", "serve", "--local", "--port", str(port)], stdout=log, stderr=subprocess.STDOUT,
                            env=os.environ | {"CAIRN_HOME": str(home)})
    try:
        wait_ready(url, proc)
        yield Local(url, (home / "run" / "token").read_text().strip(), home)
    finally:
        proc.terminate()
        proc.wait(10)
        log.close()
