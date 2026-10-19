"""`cairn setup claude|codex`: connect a harness to Cairn (MCP server, startup hook, permissions, wrap-up skill,
pointer line). Edits merge into the user's files, and --uninstall removes exactly what setup added."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import CliError, cairn_bin, local_port, remote, toml_value

POINTER = ("Before non-trivial work in an unfamiliar area, search Cairn (the cairn MCP search tool) for prior "
           "decisions and gotchas; cite refs you use.")
MD_MARKERS = ("<!-- cairn:begin -->", "<!-- cairn:end -->")
TOML_MARKERS = ("# >>> cairn (managed by cairn setup codex)", "# <<< cairn")
MATCHER = "startup|resume|clear|compact|fork"
READ_TOOLS = ["mcp__cairn__search", "mcp__cairn__get", "mcp__cairn__map"]
WRITE_TOOL = "mcp__cairn__write"
HINTS = {"claude": "cairn: start a new Claude Code session to load Cairn",
         "codex": "cairn: open /hooks in codex and trust the cairn hook"}

Changes = dict[Path, str | None]  # new file contents; None deletes the file


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def endpoint() -> str:
    if port := local_port():
        return f"http://127.0.0.1:{port}/mcp"
    if found := remote():
        return f"{found[0]}/mcp"
    raise CliError("nothing to connect to; run cairn local enable or cairn remote add first")


def setup(harness: str, dry_run: bool = False, uninstall: bool = False, allow_writes: bool = False) -> None:
    url = None if uninstall else endpoint()
    changes = (_claude if harness == "claude" else _codex)(cairn_bin(), url, allow_writes)
    for path, text in changes.items():
        old = path.read_text() if path.exists() else None
        if text == old:
            continue
        action = "create" if old is None else "remove" if text is None else "update"
        print(f"{'would ' if dry_run else ''}{action} {path}")
        if dry_run:
            continue
        backup = path.with_name(path.name + ".cairn-bak")
        if old is not None and path.suffix == ".json" and not (uninstall or backup.exists()):
            shutil.copy2(path, backup)
        if text is None:
            path.unlink()
            if path.name == "SKILL.md":
                path.parent.rmdir()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    if not (dry_run or uninstall):
        print(HINTS[harness])


def configured() -> list[list[str]]:
    """`cairn setup` arguments that redo each harness's current cairn setup (for `cairn upgrade`)."""
    found = []
    if "cairn" in _load_json(Path.home() / ".claude.json").get("mcpServers", {}):
        allow = _load_json(Path.home() / ".claude/settings.json").get("permissions", {}).get("allow", [])
        found.append(["claude", *(["--allow-writes"] if WRITE_TOOL in allow else [])])
    toml = _read(codex_home() / "config.toml")
    if TOML_MARKERS[0] in toml:
        found.append(["codex", *(["--allow-writes"] if 'approval_mode = "approve"' in toml else [])])
    return found


def _claude(cairn: str, url: str | None, writes: bool) -> Changes:
    home = Path.home()
    config_path, settings_path = home / ".claude.json", home / ".claude/settings.json"
    md_path = home / ".claude/CLAUDE.md"
    config = _load_json(config_path)
    # The helper names its endpoint, so a stale config can't hand one server's credential to another.
    # alwaysLoad: the harness waits for Cairn at startup, so `search` is there for the first prompt.
    server = {"type": "http", "url": url, "headersHelper": f'"{cairn}" auth header --server {url}', "alwaysLoad": True}
    _put(config, ["mcpServers", "cairn"], server if url else None)

    settings = _load_json(settings_path)
    hook = {"type": "command", "command": cairn, "args": ["inject", "--hook", "claude-code"], "timeout": 15}
    _put(settings, ["hooks", "SessionStart"], _hook_entries(
        settings.get("hooks", {}).get("SessionStart", []),
        lambda h: h.get("command") == cairn and h.get("args", [])[:1] == ["inject"],
        {"matcher": MATCHER, "hooks": [hook]} if url else None))
    allow: list[str] = settings.get("permissions", {}).get("allow", [])
    if url:
        allow = allow + [t for t in (READ_TOOLS + [WRITE_TOOL] if writes else READ_TOOLS) if t not in allow]
    else:
        allow = [t for t in allow if t not in [*READ_TOOLS, WRITE_TOOL]]
    _put(settings, ["permissions", "allow"], allow)

    skill = _skill().replace("\n---\n", "\ndisable-model-invocation: true\n---\n", 1)
    return {
        config_path: _dump_json(config_path, config),
        settings_path: _dump_json(settings_path, settings),
        home / ".claude/skills/cairn-wrapup/SKILL.md": skill if url else None,
        md_path: _block(_read(md_path), MD_MARKERS, POINTER if url else None),
    }


def _codex(cairn: str, url: str | None, writes: bool) -> Changes:
    root = codex_home()
    toml = None
    if url:
        helper = f"'{cairn}' auth header --server {url}"
        toml = "\n".join([
            "[mcp_servers.cairn]",
            f"url = {toml_value(url)}",
            f"http_headers_helper = {toml_value(helper)}",
            "startup_timeout_sec = 20",
            "",
            "[mcp_servers.cairn.tools.write]",
            f'approval_mode = "{"approve" if writes else "prompt"}"',
            "",
            "[mcp_servers.cairn.tools.admin]",
            'approval_mode = "prompt"',
        ])
    hooks = _load_json(root / "hooks.json")
    command = f"'{cairn}' inject --hook codex"
    _put(hooks, ["hooks", "SessionStart"], _hook_entries(
        hooks.get("hooks", {}).get("SessionStart", []),
        lambda h: h.get("command") == command,
        {"matcher": MATCHER, "hooks": [{"type": "command", "command": command, "timeout": 15}]} if url else None))
    return {
        root / "config.toml": _block(_read(root / "config.toml"), TOML_MARKERS, toml),
        root / "hooks.json": _dump_json(root / "hooks.json", hooks),
        Path.home() / ".agents/skills/cairn-wrapup/SKILL.md": _skill() if url else None,
        root / "AGENTS.md": _block(_read(root / "AGENTS.md"), MD_MARKERS, POINTER if url else None),
    }


def _skill() -> str:
    from importlib import resources  # slow to import, and the startup hook never needs it

    return (resources.files("cairn") / "assets" / "wrapup.md").read_text()


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def _load_json(path: Path) -> dict[str, Any]:
    text = _read(path)
    return json.loads(text) if text.strip() else {}


def _dump_json(path: Path, data: dict[str, Any]) -> str | None:
    """The file's new text; unchanged data keeps the user's formatting."""
    if data == _load_json(path):
        return path.read_text() if path.exists() else None
    return json.dumps(data, indent=2) + "\n" if data else None


def _put(data: dict[str, Any], keys: list[str], value: Any) -> None:
    """Set a nested key, creating parents; an empty value deletes it and prunes parents it leaves empty."""
    parents = [data]
    for key in keys[:-1]:
        parents.append(parents[-1].setdefault(key, {}))
    if value:
        parents[-1][keys[-1]] = value
        return
    parents[-1].pop(keys[-1], None)
    for parent, key in reversed(list(zip(parents, keys, strict=True))[:-1]):
        if parent[key]:
            break
        del parent[key]


def _hook_entries(entries: list[Any], ours: Callable[[dict[str, Any]], bool], new: dict[str, Any] | None) -> list[Any]:
    """Replace the cairn SessionStart entry in place (append if absent); None removes it."""
    out, placed = [], False
    for entry in entries:
        if not any(ours(h) for h in entry.get("hooks", [])):
            out.append(entry)
        elif new and not placed:
            out.append(new)
            placed = True
    if new and not placed:
        out.append(new)
    return out


def _block(text: str, markers: tuple[str, str], body: str | None) -> str | None:
    """Insert, replace or (body None) remove the managed block between two marker lines. None: file now empty."""
    begin, end = markers
    pattern = re.compile(rf"(\n?){re.escape(begin)}\n.*?{re.escape(end)}\n?", re.S)
    if body is None:
        text = pattern.sub("", text, count=1)
    elif pattern.search(text):
        text = pattern.sub(lambda m: f"{m.group(1)}{begin}\n{body}\n{end}\n", text, count=1)
    else:
        text += ("\n" if text.endswith("\n") else "\n\n" if text else "") + f"{begin}\n{body}\n{end}\n"
    return text or None
