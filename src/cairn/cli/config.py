from __future__ import annotations

import hmac
import json
import os
import secrets
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
    # JSON escapes are valid TOML; non-ASCII stays raw since TOML rejects \u surrogates.
    return str(value) if isinstance(value, int) else json.dumps(value, ensure_ascii=False)


def dump_toml(data: dict[str, Table]) -> str:
    lines: list[str] = []
    for table, values in data.items():
        lines += [f"[{table}]", *(f"{key} = {toml_value(value)}" for key, value in values.items()), ""]
    return "\n".join(lines)


def update_toml(name: str, table: str, values: Table | None) -> None:
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
    local = read_toml("config.toml").get("local", {})
    return int(local["port"]) if local.get("enabled") and local.get("port") else None


def local_token() -> str:
    return (home() / "run" / "token").read_text().strip()


def proven_token(port: int, timeout: float = 2.0) -> str:
    token, nonce = local_token(), secrets.token_hex(16)
    try:
        status, body = fetch(f"http://127.0.0.1:{port}/api/v1/hello?nonce={nonce}", timeout=timeout)
    except NETWORK_ERRORS as exc:
        raise CliError(f"the local Cairn server isn't answering on port {port}") from exc
    proof = body.get("proof") if status == 200 and isinstance(body, dict) else None
    # Another process could hold the port while Cairn is down; it must not learn the token.
    if not hmac.compare_digest(str(proof), hmac.new(token.encode(), nonce.encode(), "sha256").hexdigest()):
        raise CliError(f"something other than this machine's Cairn answers on port {port}; not sending the token")
    return token


def remote() -> tuple[str, str] | None:
    url = read_toml("config.toml").get("remote", {}).get("url")
    key = read_toml("credentials.toml").get("remote", {}).get("api_key")
    return (str(url), str(key)) if url and key else None


def cairn_bin() -> str:
    """Absolute, for hooks and services: GUI apps often lack ~/.local/bin on PATH."""
    argv0 = Path(sys.argv[0])
    # Prefer the running binary: PATH may hold another cairn.
    found = str(argv0) if argv0.name == "cairn" else shutil.which("cairn")
    return str(Path(found or argv0).resolve())


def fetch(url: str, body: dict[str, Any] | None = None, token: str | None = None,
          timeout: float = 10.0) -> tuple[int, Any]:
    request = urllib.request.Request(url, None if body is None else json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    # Bypass proxies for loopback: a corporate HTTP_PROXY would swallow local calls.
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
