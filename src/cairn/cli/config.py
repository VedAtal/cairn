"""Client state under CAIRN_HOME (config.toml, credentials.toml, run/token), a tiny TOML writer, and JSON over HTTP."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
import urllib.error
import urllib.request
from http.client import HTTPException
from pathlib import Path
from typing import Any

Table = dict[str, str | int | bool]
NETWORK_ERRORS = (OSError, HTTPException)


class CliError(Exception):
    """A user-facing failure: printed as one line on stderr, exit 1."""


def home() -> Path:
    return Path(os.environ.get("CAIRN_HOME") or "~/.cairn").expanduser()


def read_toml(name: str) -> dict[str, Table]:
    path = home() / name
    return tomllib.loads(path.read_text()) if path.exists() else {}


def toml_value(value: str | int | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    # JSON string escapes are valid TOML basic-string escapes (non-ASCII stays raw: TOML rejects \u surrogates).
    return str(value) if isinstance(value, int) else json.dumps(value, ensure_ascii=False)


def dump_toml(data: dict[str, Table]) -> str:
    """Flat tables of string/int/bool values only."""
    lines: list[str] = []
    for table, values in data.items():
        lines += [f"[{table}]", *(f"{key} = {toml_value(value)}" for key, value in values.items()), ""]
    return "\n".join(lines)


def update_toml(name: str, table: str, values: Table | None) -> None:
    """Merge `values` into one table of a CAIRN_HOME TOML file; None removes the table."""
    data = read_toml(name)
    if values is None:
        data.pop(table, None)
    else:
        data[table] = {**data.get(table, {}), **values}
    write_private(home() / name, dump_toml(data))


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(text)
    path.chmod(0o600)


def local_port() -> int | None:
    """The local server's port, when local mode is enabled."""
    local = read_toml("config.toml").get("local", {})
    return int(local["port"]) if local.get("enabled") and local.get("port") else None


def local_token() -> str:
    return (home() / "run" / "token").read_text().strip()


def remote() -> tuple[str, str] | None:
    """(url, api_key) of the remote added with `cairn remote add`."""
    url = read_toml("config.toml").get("remote", {}).get("url")
    key = read_toml("credentials.toml").get("remote", {}).get("api_key")
    return (str(url), str(key)) if url and key else None


def cairn_bin() -> str:
    """Absolute path of the running `cairn`, for hooks and services: GUI apps often lack ~/.local/bin on PATH."""
    argv0 = Path(sys.argv[0])
    # Prefer the running executable: install.sh runs it by absolute path, and PATH may hold another cairn.
    found = str(argv0) if argv0.name == "cairn" else shutil.which("cairn")
    return str(Path(found or argv0).resolve())


def fetch(url: str, body: dict[str, Any] | None = None, token: str | None = None,
          timeout: float = 10.0) -> tuple[int, Any]:
    """GET, or POST `body` as JSON. Returns (status, parsed JSON or None); network failures raise."""
    request = urllib.request.Request(url, None if body is None else json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    # The local server is never behind a proxy; a corporate HTTP_PROXY would otherwise swallow loopback calls.
    no_proxy = [urllib.request.ProxyHandler({})] if url.startswith("http://127.0.0.1:") else []
    opener = urllib.request.build_opener(*no_proxy)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, _json(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _json(exc.read())


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None
