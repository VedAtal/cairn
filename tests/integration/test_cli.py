"""CLI flows against the hosted server, each in a throwaway HOME and CAIRN_HOME: remote add/remove, local enable,
auth header, inject output, and harness setup for Claude Code and Codex."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from .conftest import SERVER, Api, World, free_port, write_config

BIN = str(Path(shutil.which("cairn") or "cairn").resolve())
POINTER = ("Before non-trivial work in an unfamiliar area, search Cairn (the cairn MCP search tool) for prior "
           "decisions and gotchas; cite refs you use.")
MATCHER = "startup|resume|clear|compact|fork"
BEGIN, END = "# >>> cairn (managed by cairn setup codex)", "# <<< cairn"


@dataclass
class Cli:
    home: Path  # HOME
    cairn: Path  # CAIRN_HOME

    def __call__(self, *args: str, stdin: str = "", ok: bool = True) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k != "CODEX_HOME"} | {
            "HOME": str(self.home), "CAIRN_HOME": str(self.cairn)}
        res = subprocess.run(["cairn", *args], env=env, input=stdin, capture_output=True, text=True, timeout=60)
        if ok:
            assert res.returncode == 0, f"cairn {' '.join(args)}: {res.returncode}\n{res.stdout}\n{res.stderr}"
        return res

    def toml(self, name: str) -> dict[str, Any]:
        path = self.cairn / name
        return tomllib.loads(path.read_text()) if path.exists() else {}

    def remote(self, key: str, world: World) -> None:
        write_config(self.cairn, SERVER, key, world.alias)


@pytest.fixture
def cli(tmp_path: Path) -> Iterator[Cli]:
    c = Cli(tmp_path / "home", tmp_path / "home" / ".cairn")
    c.home.mkdir()
    yield c
    pid = c.cairn / "run" / "server.pid"
    if pid.exists():  # a detached server left behind by a failed test
        with contextlib.suppress(ProcessLookupError, ValueError):
            os.kill(int(pid.read_text().strip()), signal.SIGTERM)


@pytest.fixture(scope="module")
def flagged(world: World) -> str:
    bob = Api(world.bob)
    node = bob.node(world.billing, "cli_notes", "CLI notes", "Notes that tests flag for injection.")
    ref = bob.add(node, "Card network tokens replace PANs for recurring charges",
                  "Network tokens survive card reissue, so recurring charges keep working after expiry.")
    Api(world.alice).admin("flag", ref=ref)
    return str(ref)


def snapshot(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


def hook_output(res: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(res.stdout)  # exactly one JSON object
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert set(out) <= {"hookSpecificOutput", "systemMessage"}
    return out


# --- remote ---------------------------------------------------------------------------------------------------------


def test_remote_add_and_remove(cli: Cli, world: World) -> None:
    cli("remote", "add", SERVER, "--key", world.bob)
    remote = cli.toml("config.toml")["remote"]
    assert (remote["url"], remote["alias"]) == (SERVER, world.alias)
    assert cli.toml("credentials.toml")["remote"]["api_key"] == world.bob
    assert (cli.cairn / "credentials.toml").stat().st_mode & 0o777 == 0o600

    again = cli("remote", "add", SERVER, "--key", world.alice, ok=False)
    assert again.returncode != 0 and again.stderr
    assert cli.toml("credentials.toml")["remote"]["api_key"] == world.bob
    cli("remote", "add", SERVER, "--key", world.alice, "--replace")
    assert cli.toml("credentials.toml")["remote"]["api_key"] == world.alice

    cli("remote", "remove")
    assert "remote" not in cli.toml("config.toml") and "remote" not in cli.toml("credentials.toml")


def test_remote_add_rejects_bad_key(cli: Cli) -> None:
    res = cli("remote", "add", SERVER, "--key", "cairn_pat_not-a-real-key", ok=False)
    assert res.returncode != 0 and res.stderr
    assert "remote" not in cli.toml("credentials.toml")


# --- auth header ------------------------------------------------------------------------------------------------------


def test_auth_header(cli: Cli, world: World) -> None:
    missing = cli("auth", "header", ok=False)
    assert (missing.returncode, missing.stdout) == (1, "") and missing.stderr
    cli.remote(world.bob, world)
    assert json.loads(cli("auth", "header").stdout) == {"Authorization": f"Bearer {world.bob}"}


# --- local ----------------------------------------------------------------------------------------------------------


def test_local_enable_and_disable(cli: Cli) -> None:
    port = free_port()
    res = cli("local", "enable", "--no-service", "--port", str(port))
    assert str(port) in res.stdout
    url = f"http://127.0.0.1:{port}"
    assert httpx.get(f"{url}/readyz").json() == {"status": "ready"}
    local = cli.toml("config.toml")["local"]
    assert (local["enabled"], local["port"]) == (True, port)
    assert cli.cairn.stat().st_mode & 0o777 == 0o700
    token_file = cli.cairn / "run" / "token"
    assert token_file.stat().st_mode & 0o777 == 0o600 and (cli.cairn / "run" / "server.pid").exists()

    header = json.loads(cli("auth", "header").stdout)
    assert header == {"Authorization": f"Bearer {token_file.read_text().strip()}"}
    assert Api(header["Authorization"].split()[1], url).http.get("/hello").json()["store"] == "me"
    cli("setup", "claude")
    assert json.loads((cli.home / ".claude.json").read_text())["mcpServers"]["cairn"]["url"] == f"{url}/mcp"

    cli("local", "disable")
    assert cli.toml("config.toml")["local"]["enabled"] is False
    assert (cli.cairn / "cairn.db").exists()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{url}/healthz", timeout=1)
        except httpx.HTTPError:
            break
        time.sleep(0.2)
    else:
        raise AssertionError("local server still running after disable")


# --- inject ---------------------------------------------------------------------------------------------------------


def test_inject_unconfigured(cli: Cli) -> None:
    for hook in ("claude-code", "codex"):
        res = cli("inject", "--hook", hook, stdin='{"hook_event_name": "SessionStart", "source": "startup"}')
        assert json.loads(res.stdout) == {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                                 "additionalContext": ""}}


def test_inject_remote_cache_and_rejection(cli: Cli, world: World, flagged: str) -> None:
    cli.remote(world.bob, world)
    small = hook_output(cli("inject", "--budget", "600"))["hookSpecificOutput"]["additionalContext"]
    assert small == Api(world.bob).post("/inject", {"budget": 600})["text"]
    fresh = hook_output(cli("inject", "--hook", "claude-code", stdin="{}"))
    context = fresh["hookSpecificOutput"]["additionalContext"]
    assert "systemMessage" not in fresh and flagged in context
    assert context.startswith("Cairn knowledge flagged for every session.")
    cache = cli.cairn / "cache" / "inject.json"
    assert cache.stat().st_mode & 0o777 == 0o600

    write_config(cli.cairn, f"http://127.0.0.1:{free_port()}", world.bob, world.alias)  # remote down
    cached = hook_output(cli("inject", "--hook", "claude-code"))
    assert cached["hookSpecificOutput"]["additionalContext"] == context
    assert cached["systemMessage"].startswith("Cairn: ")
    assert cached["systemMessage"].endswith("; showing cached context")

    cli.remote("cairn_pat_revoked-or-wrong", world)
    rejected = hook_output(cli("inject", "--hook", "codex"))
    assert rejected["systemMessage"] == "Cairn: credentials rejected; run cairn remote add"
    assert rejected["hookSpecificOutput"]["additionalContext"] == "" and not cache.exists()


# --- setup ----------------------------------------------------------------------------------------------------------

OTHER_HOOK = {"matcher": "startup", "hooks": [{"type": "command", "command": "/usr/bin/true"}]}


def test_setup_claude(cli: Cli, world: World) -> None:
    cli.remote(world.bob, world)
    claude_json, claude = cli.home / ".claude.json", cli.home / ".claude"
    claude.mkdir()
    seeds = {
        claude_json: {"theme": "dark", "mcpServers": {"other": {"type": "stdio", "command": "other"}}},
        claude / "settings.json": {"model": "opus", "permissions": {"allow": ["Bash(ls:*)"]},
                                   "hooks": {"SessionStart": [OTHER_HOOK]}},
    }
    for path, data in seeds.items():
        path.write_text(json.dumps(data))
    (claude / "CLAUDE.md").write_text("# My rules\n")
    before = snapshot(cli.home)

    dry = cli("setup", "claude", "--dry-run")
    assert snapshot(cli.home) == before and ".claude.json" in dry.stdout
    cli("setup", "claude")
    cli("setup", "claude")  # idempotent

    cfg = json.loads(claude_json.read_text())
    assert cfg["theme"] == "dark" and "other" in cfg["mcpServers"]
    assert cfg["mcpServers"]["cairn"].items() >= {
        "type": "http", "url": f"{SERVER}/mcp", "headersHelper": f'"{BIN}" auth header --server {SERVER}/mcp',
        "alwaysLoad": True}.items()
    settings = json.loads((claude / "settings.json").read_text())
    assert settings["model"] == "opus"
    assert settings["hooks"]["SessionStart"] == [OTHER_HOOK, {"matcher": MATCHER, "hooks": [
        {"type": "command", "command": BIN, "args": ["inject", "--hook", "claude-code"], "timeout": 15}]}]
    allow = settings["permissions"]["allow"]
    assert sorted(allow) == ["Bash(ls:*)", "mcp__cairn__get", "mcp__cairn__map", "mcp__cairn__search"]
    skill = (claude / "skills" / "cairn-wrapup" / "SKILL.md").read_text()
    front = skill.split("---")[1]
    assert skill.startswith("---\n") and "name:" in front and "description:" in front
    assert "disable-model-invocation: true" in front
    md = (claude / "CLAUDE.md").read_text()
    assert md.startswith("# My rules\n") and md.count("<!-- cairn:begin -->") == 1 and POINTER in md
    for path, data in seeds.items():
        assert json.loads(path.with_name(path.name + ".cairn-bak").read_text()) == data

    cli("setup", "claude", "--uninstall")
    cfg = json.loads(claude_json.read_text())
    assert "cairn" not in cfg["mcpServers"] and cfg["theme"] == "dark" and "other" in cfg["mcpServers"]
    settings = json.loads((claude / "settings.json").read_text())
    assert settings["hooks"]["SessionStart"] == [OTHER_HOOK]
    assert settings["permissions"]["allow"] == ["Bash(ls:*)"]
    assert not (claude / "skills" / "cairn-wrapup" / "SKILL.md").exists()
    assert (claude / "CLAUDE.md").read_text().strip() == "# My rules"


def test_setup_claude_allow_writes(cli: Cli, world: World) -> None:
    cli.remote(world.bob, world)
    cli("setup", "claude", "--allow-writes")
    allow = json.loads((cli.home / ".claude" / "settings.json").read_text())["permissions"]["allow"]
    assert "mcp__cairn__write" in allow and "mcp__cairn__admin" not in allow


def test_setup_codex(cli: Cli, world: World) -> None:
    cli.remote(world.bob, world)
    codex = cli.home / ".codex"
    codex.mkdir()
    seed = 'model = "o3"\n\n[mcp_servers.other]\nurl = "http://other.test/mcp"\n'
    (codex / "config.toml").write_text(seed)
    (codex / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [OTHER_HOOK]}}))
    (codex / "AGENTS.md").write_text("# Codex rules\n")
    before = snapshot(cli.home)

    cli("setup", "codex", "--dry-run")
    assert snapshot(cli.home) == before
    cli("setup", "codex")
    out = cli("setup", "codex").stdout
    assert "/hooks" in out

    text = (codex / "config.toml").read_text()
    assert text.count(BEGIN) == 1 and text.count(END) == 1 and seed.strip() in text
    cfg = tomllib.loads(text)
    assert cfg["model"] == "o3" and cfg["mcp_servers"]["other"] == {"url": "http://other.test/mcp"}
    cairn = cfg["mcp_servers"]["cairn"]
    assert (cairn["url"], cairn["http_headers_helper"], cairn["startup_timeout_sec"]) == (
        f"{SERVER}/mcp", f"'{BIN}' auth header --server {SERVER}/mcp", 20)
    assert cairn["tools"] == {"write": {"approval_mode": "prompt"}, "admin": {"approval_mode": "prompt"}}
    hooks = json.loads((codex / "hooks.json").read_text())["hooks"]["SessionStart"]
    assert hooks == [OTHER_HOOK, {"matcher": MATCHER, "hooks": [
        {"type": "command", "command": f"'{BIN}' inject --hook codex", "timeout": 15}]}]
    skill = (cli.home / ".agents" / "skills" / "cairn-wrapup" / "SKILL.md").read_text()
    assert skill.startswith("---\n") and "disable-model-invocation" not in skill
    agents = (codex / "AGENTS.md").read_text()
    assert agents.startswith("# Codex rules\n") and agents.count(POINTER) == 1

    cli("setup", "codex", "--allow-writes")
    tools = tomllib.loads((codex / "config.toml").read_text())["mcp_servers"]["cairn"]["tools"]
    assert tools["write"] == {"approval_mode": "approve"} and tools["admin"] == {"approval_mode": "prompt"}

    cli("setup", "codex", "--uninstall")
    text = (codex / "config.toml").read_text()
    assert BEGIN not in text and tomllib.loads(text) == tomllib.loads(seed)
    assert json.loads((codex / "hooks.json").read_text())["hooks"]["SessionStart"] == [OTHER_HOOK]
    assert not (cli.home / ".agents" / "skills" / "cairn-wrapup" / "SKILL.md").exists()
    assert (codex / "AGENTS.md").read_text().strip() == "# Codex rules"
