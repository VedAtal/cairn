from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from cairn.cli import config, harness, local
from cairn.cli.main import main

BIN = "/opt/my tools/cairn"
MATCHER = "startup|resume|clear|compact|fork"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAIRN_HOME", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setattr(harness, "cairn_bin", lambda: BIN)
    config.update_toml("config.toml", "local", {"enabled": True, "port": 6804})
    return tmp_path


def snapshot(root: Path) -> dict[str, str]:
    files = (p for p in root.rglob("*") if p.is_file() and p.relative_to(root).parts[0] != ".cairn")
    return {str(p.relative_to(root)): p.read_text() for p in sorted(files)}


def write_files(files: dict[Path, str]) -> None:
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def cli(*args: str, cairn_home: Path, stdin: str = "{}") -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "cairn.cli.main", *args], input=stdin, capture_output=True,
                          text=True, timeout=30, env={**os.environ, "CAIRN_HOME": str(cairn_home)})


def set_remote(url: str = "http://127.0.0.1:9") -> None:  # port 9: nothing listens
    config.update_toml("config.toml", "remote", {"url": url, "alias": "acme"})
    config.update_toml("credentials.toml", "remote", {"api_key": "k"})


def test_launchd_plist() -> None:
    plist = plistlib.loads(local.launchd_plist(BIN, Path("/h/.cairn"), "/usr/bin:/bin").encode())
    assert plist == {
        "Label": "local.cairn.server",
        "ProgramArguments": [BIN, "serve", "--local"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin", "CAIRN_HOME": "/h/.cairn"},
        "StandardOutPath": "/h/.cairn/logs/server.log",
        "StandardErrorPath": "/h/.cairn/logs/server.log",
    }


def test_systemd_unit() -> None:
    lines = local.systemd_unit(BIN, Path("/h/.cairn")).splitlines()
    for line in (f'ExecStart="{BIN}" serve --local', "Restart=on-failure", "RestartSec=5",
                 'Environment="CAIRN_HOME=/h/.cairn"', "[Install]", "WantedBy=default.target"):
        assert line in lines


def test_cairn_bin_prefers_the_running_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["/opt/tools/bin/cairn", "setup", "claude"])
    assert config.cairn_bin() == "/opt/tools/bin/cairn"


def test_toml_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data: dict[str, config.Table] = {
        "remote": {"url": "https://cairn.acme.dev", "alias": 'q"uote \\ back\nline\ttab\x01 é 😀'},
        "local": {"enabled": True, "port": 6804, "off": False},
    }
    assert tomllib.loads(config.dump_toml(data)) == data
    monkeypatch.setenv("CAIRN_HOME", str(tmp_path))
    config.update_toml("credentials.toml", "remote", {"api_key": "k"})
    config.update_toml("credentials.toml", "other", {"x": 1})
    config.update_toml("credentials.toml", "remote", None)
    assert config.read_toml("credentials.toml") == {"other": {"x": 1}}
    assert (tmp_path / "credentials.toml").stat().st_mode & 0o777 == 0o600


def test_setup_claude(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    claude_json, settings, md = home / ".claude.json", home / ".claude/settings.json", home / ".claude/CLAUDE.md"
    other_hook = {"matcher": "startup", "hooks": [{"type": "command", "command": "other"}]}
    original = {
        claude_json: json.dumps({"numStartups": 3, "mcpServers": {"other": {"type": "stdio", "command": "x"}}}),
        settings: json.dumps({"model": "opus", "hooks": {"SessionStart": [other_hook]},
                              "permissions": {"allow": ["Bash(ls)"]}}),
        md: "# Mine\n",
    }
    write_files(original)

    before = snapshot(home)
    harness.setup("claude", dry_run=True)
    assert snapshot(home) == before
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 4 and all(line.startswith("would ") for line in lines)

    harness.setup("claude")
    config_ = json.loads(claude_json.read_text())
    assert config_["numStartups"] == 3 and "other" in config_["mcpServers"]
    assert config_["mcpServers"]["cairn"] == {
        "type": "http", "url": "http://127.0.0.1:6804/mcp",
        "headersHelper": f'"{BIN}" auth header --server http://127.0.0.1:6804/mcp', "alwaysLoad": True}
    settings_ = json.loads(settings.read_text())
    assert settings_["model"] == "opus"
    assert settings_["hooks"]["SessionStart"] == [other_hook, {"matcher": MATCHER, "hooks": [
        {"type": "command", "command": BIN, "args": ["inject", "--hook", "claude-code"], "timeout": 15}]}]
    assert settings_["permissions"]["allow"] == ["Bash(ls)", *harness.READ_TOOLS]
    skill = (home / ".claude/skills/cairn-wrapup/SKILL.md").read_text()
    assert skill.startswith("---\nname: cairn-wrapup\ndescription: ")
    assert "\ndisable-model-invocation: true\n---\n" in skill
    assert md.read_text() == f"# Mine\n\n<!-- cairn:begin -->\n{harness.POINTER}\n<!-- cairn:end -->\n"
    assert (home / ".claude.json.cairn-bak").read_text() == original[claude_json]
    assert (home / ".claude/settings.json.cairn-bak").read_text() == original[settings]

    after = snapshot(home)
    harness.setup("claude")
    assert snapshot(home) == after

    harness.setup("claude", allow_writes=True)
    assert json.loads(settings.read_text())["permissions"]["allow"] == ["Bash(ls)", *harness.READ_TOOLS,
                                                                         "mcp__cairn__write"]
    assert "mcp__cairn__admin" not in settings.read_text()

    harness.setup("claude", uninstall=True)
    assert json.loads(claude_json.read_text()) == json.loads(original[claude_json])
    assert json.loads(settings.read_text()) == json.loads(original[settings])
    assert md.read_text() == original[md]
    assert not (home / ".claude/skills/cairn-wrapup").exists()


def test_setup_claude_fresh_home_uninstalls_cleanly(home: Path) -> None:
    harness.setup("claude")
    harness.setup("claude", uninstall=True)
    assert snapshot(home) == {}


def test_uninstall_without_setup_touches_nothing(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files({home / ".claude.json": '{"numStartups":3}', home / ".codex/config.toml": 'model = "o3"'})
    before = snapshot(home)
    harness.setup("claude", uninstall=True)
    harness.setup("codex", uninstall=True)
    assert snapshot(home) == before and capsys.readouterr().out == ""


def test_setup_codex(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    codex = home / ".codex"
    toml_text = 'model = "o3"\n\n[mcp_servers.other]\nurl = "https://other/mcp"\n'
    other_hook = {"matcher": "startup", "hooks": [{"type": "command", "command": "other"}]}
    original = {
        codex / "config.toml": toml_text,
        codex / "hooks.json": json.dumps({"hooks": {"SessionStart": [other_hook]}}),
        codex / "AGENTS.md": "Be terse.\n",
    }
    write_files(original)

    before = snapshot(home)
    harness.setup("codex", dry_run=True)
    assert snapshot(home) == before
    assert len(capsys.readouterr().out.splitlines()) == 4

    harness.setup("codex")
    assert "open /hooks in codex and trust the cairn hook" in capsys.readouterr().out
    text = (codex / "config.toml").read_text()
    assert text.startswith(toml_text)
    toml = tomllib.loads(text)
    assert toml["model"] == "o3" and toml["mcp_servers"]["other"] == {"url": "https://other/mcp"}
    assert toml["mcp_servers"]["cairn"] == {
        "url": "http://127.0.0.1:6804/mcp",
        "http_headers_helper": f"'{BIN}' auth header --server http://127.0.0.1:6804/mcp",
        "startup_timeout_sec": 20,
        "tools": {"write": {"approval_mode": "prompt"}, "admin": {"approval_mode": "prompt"}},
    }
    assert "# >>> cairn (managed by cairn setup codex)\n[mcp_servers.cairn]" in text and text.endswith("# <<< cairn\n")
    assert json.loads((codex / "hooks.json").read_text())["hooks"]["SessionStart"] == [other_hook, {
        "matcher": MATCHER, "hooks": [{"type": "command", "command": f"'{BIN}' inject --hook codex", "timeout": 15}]}]
    skill = (home / ".agents/skills/cairn-wrapup/SKILL.md").read_text()
    assert skill.startswith("---\nname: cairn-wrapup\n") and "disable-model-invocation" not in skill
    pointer = f"<!-- cairn:begin -->\n{harness.POINTER}\n<!-- cairn:end -->\n"
    assert (codex / "AGENTS.md").read_text() == f"Be terse.\n\n{pointer}"

    after = snapshot(home)
    harness.setup("codex")
    assert snapshot(home) == after
    assert harness.configured() == [["codex"]]

    harness.setup("codex", allow_writes=True)
    assert tomllib.loads((codex / "config.toml").read_text())["mcp_servers"]["cairn"]["tools"]["write"] == {
        "approval_mode": "approve"}
    assert harness.configured() == [["codex", "--allow-writes"]]

    harness.setup("codex", uninstall=True)
    assert (codex / "config.toml").read_text() == toml_text
    assert json.loads((codex / "hooks.json").read_text()) == json.loads(original[codex / "hooks.json"])
    assert (codex / "AGENTS.md").read_text() == original[codex / "AGENTS.md"]
    assert not (home / ".agents/skills/cairn-wrapup").exists()
    assert harness.configured() == []


def test_setup_endpoint_follows_mode(home: Path) -> None:
    assert harness.endpoint() == "http://127.0.0.1:6804/mcp"
    config.update_toml("config.toml", "local", {"enabled": False})
    set_remote("https://cairn.acme.dev")
    assert harness.endpoint() == "https://cairn.acme.dev/mcp"


def test_inject_with_nothing_configured(tmp_path: Path) -> None:
    result = cli("inject", "--hook", "claude-code", cairn_home=tmp_path / "none")
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                                "additionalContext": ""}}
    assert not (tmp_path / "none").exists()


def test_inject_unreachable_falls_back_to_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_HOME", str(tmp_path))
    set_remote()
    hook = {"hookEventName": "SessionStart", "additionalContext": ""}
    result = cli("inject", "--hook", "codex", cairn_home=tmp_path)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"hookSpecificOutput": hook,
                                         "systemMessage": "Cairn: server unreachable; no cached context"}

    cache = tmp_path / "cache/inject.json"
    config.write_private(cache, json.dumps({"text": "cached notes", "groups": []}))
    assert json.loads(cli("inject", cairn_home=tmp_path).stdout) == {
        "hookSpecificOutput": {**hook, "additionalContext": "cached notes"},
        "systemMessage": "Cairn: server unreachable; showing cached context"}

    os.utime(cache, (0, 0))  # older than 7 days
    assert json.loads(cli("inject", cairn_home=tmp_path).stdout)["hookSpecificOutput"]["additionalContext"] == ""


def test_no_command_prints_help(tmp_path: Path) -> None:
    result = cli(cairn_home=tmp_path)
    assert result.returncode == 0 and result.stdout.startswith("usage: cairn") and "upgrade" in result.stdout


def test_auth_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = cli("auth", "header", cairn_home=tmp_path)
    assert result.returncode == 1 and result.stdout == "" and "not configured" in result.stderr
    monkeypatch.setenv("CAIRN_HOME", str(tmp_path))
    set_remote()
    result = cli("auth", "header", cairn_home=tmp_path)
    assert result.returncode == 0 and json.loads(result.stdout) == {"Authorization": "Bearer k"}


def test_fast_paths_do_not_import_server_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_HOME", str(tmp_path))
    set_remote()
    code = (
        "import contextlib, io, sys\n"
        "from cairn.cli.main import main\n"
        "with contextlib.redirect_stdout(io.StringIO()):\n"
        "    assert main(['inject', '--hook', 'claude-code']) == 0 and main(['auth', 'header']) == 0\n"
        "print(sorted({m.split('.')[0] for m in sys.modules} & {'fastapi', 'sqlalchemy', 'pydantic', 'httpx', 'mcp'}))"
    )
    result = subprocess.run([sys.executable, "-c", code], input="", capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_rewire_follows_local_and_remote_changes(home: Path) -> None:
    from cairn.cli import main as cli_main

    set_remote("https://cairn.acme.dev")
    harness.setup("claude", allow_writes=True)
    claude_json = home / ".claude.json"
    url = lambda: json.loads(claude_json.read_text())["mcpServers"]["cairn"]["url"]  # noqa: E731
    assert url() == "http://127.0.0.1:6804/mcp"
    config.update_toml("config.toml", "local", {"enabled": False, "port": 6804})  # what `local disable` leaves
    cli_main.rewire()
    assert url() == "https://cairn.acme.dev/mcp"
    assert "mcp__cairn__write" in json.loads((home / ".claude/settings.json").read_text())["permissions"]["allow"]
    stale = cli("auth", "header", "--server", "http://127.0.0.1:6804/mcp", cairn_home=home / ".cairn")
    assert stale.returncode == 1 and stale.stdout == "" and "now serves https://cairn.acme.dev/mcp" in stale.stderr
    config.update_toml("config.toml", "remote", None)
    cli_main.rewire()
    assert not claude_json.exists()  # it held only Cairn's entry


def test_service_restart_kickstarts_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(local, "service_start", lambda restart=False: calls.append(restart))
    main(["service", "restart"])
    assert calls == [True]
