"""Stdlib only, so `cairn inject` and `cairn auth header` start fast."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import json
import os
import select
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

from . import gitmode, harness, local
from .config import (
    NETWORK_ERRORS,
    CliError,
    cairn_bin,
    fetch,
    home,
    local_port,
    proven_token,
    read_toml,
    remote,
    update_toml,
    write_private,
)

CACHE_MAX_AGE = 7 * 86400
SOURCE = "git+https://github.com/VedAtal/cairn"  # not on PyPI
# Tool installs ignore the lockfile and [tool.uv], so pin Python and the 7-day buffer.
UV_TOOL_FLAGS = ["--managed-python", "--python", "3.14", "--exclude-newer", "7 days"]


def serve(host: str, port: int | None, local_mode: bool) -> None:
    if local_mode:
        os.environ["CAIRN_MODE"] = "local"
        port = port or int(read_toml("config.toml").get("local", {}).get("port", local.DEFAULT_PORT))
    import uvicorn

    from ..app.build import build_app

    port = port or 8000
    uvicorn.run(build_app(port=port), host=host, port=port, log_config=None)


def auth_header(server: str | None) -> None:
    if server and server.rstrip("/") != (current := harness.endpoint()):
        raise CliError(f"this harness is set up for {server}, but Cairn now serves {current}; run cairn setup")
    if port := local_port():
        with contextlib.suppress(CliError, OSError):
            local.ensure_running(port, 5.0)
        token = proven_token(port)
    elif found := remote():
        token = found[1]
    else:
        raise CliError("Cairn is not configured; run cairn local enable or cairn remote add")
    print(json.dumps({"Authorization": f"Bearer {token}"}))


def inject(budget: int) -> dict[str, Any]:
    """Never raises: a broken hook must not break the session."""
    deadline = time.monotonic() + 4.0
    cache = home() / "cache" / "inject.json"
    try:
        given = _hook_input()
        if port := local_port():
            local.ensure_running(port, min(3.0, deadline - time.monotonic()))
            base, token = f"http://127.0.0.1:{port}", proven_token(port, max(0.2, deadline - time.monotonic()))
        elif found := remote():
            base, token = found
        else:
            return _hook("")
        status, body = fetch(f"{base}/api/v1/inject", {"budget": budget}, token,
                             timeout=max(0.2, deadline - time.monotonic()))
        if status == 200:
            text: str = body["text"]
            write_private(cache, json.dumps(body))
            return _hook("" if _unchanged_on_resume(given, text) else text)
        if status in (401, 403):
            cache.unlink(missing_ok=True)
            return _hook("", "Cairn: credentials rejected; run cairn remote add")
        reason = f"server error {status}"
    except Exception as exc:  # noqa: BLE001
        timed_out = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
        reason = "timed out" if timed_out else "server unreachable"
    try:
        fresh = cache.stat().st_mtime > time.time() - CACHE_MAX_AGE
        cached: str | None = json.loads(cache.read_text())["text"] if fresh else None
    except Exception:  # noqa: BLE001
        cached = None
    if cached is None:
        return _hook("", f"Cairn: {reason}; no cached context")
    return _hook(cached, f"Cairn: {reason}; showing cached context")


def _hook(context: str, message: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
    if message:
        out["systemMessage"] = message
    return out


def _hook_input() -> dict[str, Any]:
    with contextlib.suppress(OSError, ValueError):
        if not sys.stdin.isatty() and select.select([sys.stdin], [], [], 0.2)[0]:
            data = json.loads(os.read(sys.stdin.fileno(), 1 << 20) or b"{}")
            return data if isinstance(data, dict) else {}
    return {}


def _unchanged_on_resume(given: dict[str, Any], text: str) -> bool:
    session = given.get("session_id")
    if not isinstance(session, str) or not session:
        return False
    marker = home() / "cache" / f"session-{hashlib.sha256(session.encode()).hexdigest()[:32]}"
    digest = hashlib.sha256(text.encode()).hexdigest()
    # A resumed session still holds the context it was given, so only send it again if it changed.
    same = given.get("source") == "resume" and marker.exists() and marker.read_text() == digest
    write_private(marker, digest)
    for old in marker.parent.glob("session-*"):
        if old.stat().st_mtime < time.time() - CACHE_MAX_AGE:
            old.unlink(missing_ok=True)
    return same


def remote_add(url: str, key: str | None, replace: bool) -> None:
    url = url.rstrip("/")
    _refuse_second_remote(replace)
    status, hello = fetch(f"{url}/api/v1/hello")
    if status != 200 or not isinstance(hello, dict) or "store" not in hello:
        raise CliError(f"{url} does not look like a Cairn server (GET /api/v1/hello: {status})")
    key = key or getpass.getpass(f"API key for {url}: ").strip()
    status, body = fetch(f"{url}/api/v1/admin", {"op": "whoami"}, key)
    if status != 200:
        raise CliError(f"the API key was rejected: {_message(status, body)}")
    update_toml("config.toml", "remote", {"url": url, "alias": hello["store"]})
    update_toml("credentials.toml", "remote", {"api_key": key})
    print(f"added remote {hello['store']} ({url})")
    _restart_local()
    rewire()


def remote_remove() -> None:
    update_toml("config.toml", "remote", None)
    update_toml("credentials.toml", "remote", None)
    print("removed the remote")
    _restart_local()
    rewire()


def remote_bootstrap(url: str, token: str, email: str, name: str | None) -> None:
    url = url.rstrip("/")
    _refuse_second_remote(False)
    status, body = fetch(f"{url}/api/v1/bootstrap", {"token": token, "email": email, "name": name or email})
    if status != 200:
        raise CliError(f"bootstrap failed: {_message(status, body)}")
    print(f"instance admin created; API key (shown once, keep it safe): {body['api_key']}")
    print(f"set a password for the web UI: {url}{body['password_link']}")
    remote_add(url, body["api_key"], replace=False)


def ui(use_remote: bool) -> None:
    if (port := local_port()) and not use_remote:
        local.ensure_running(port, 5.0)
        base, token = f"http://127.0.0.1:{port}", proven_token(port)
    elif found := remote():
        base, token = found
    else:
        raise CliError("Cairn is not configured; run cairn local enable or cairn remote add")
    status, body = fetch(f"{base}/api/v1/ui/ticket", {}, token)
    if status != 200:
        raise CliError(f"could not open the UI: {_message(status, body)}")
    url = f"{base}/ui/login?ticket={body['ticket']}"
    print(url)
    webbrowser.open(url)


def _refuse_second_remote(replace: bool) -> None:
    if not replace and read_toml("config.toml").get("remote"):
        raise CliError("a remote is already configured (one is allowed); use --replace or cairn remote remove")


def _restart_local() -> None:
    """A running server reads the remote only at startup."""
    if port := local_port():
        local.start(restart=True)
        local.wait_ready(port, 20)


def local_enable(port: int | None, service: bool, from_git: str | None = None) -> None:
    if from_git:
        return restore(from_git, port, service)
    local.enable(port, service)
    rewire()


def restore(source: str | None, port: int | None, service: bool) -> None:
    gitmode.restore(source, port, service)
    rewire()


def local_disable() -> None:
    local.disable()
    rewire()


def rewire() -> None:
    try:
        harness.endpoint()
    except CliError:
        for args in harness.configured():
            harness.setup(args[0], uninstall=True)
        return
    for args in harness.configured():
        harness.setup(args[0], allow_writes="--allow-writes" in args)


def _persist(argv: list[str]) -> None:
    """Under uvx, reinstall as a tool so hooks and services don't point into uv's cache."""
    uv = shutil.which("uv")
    if not uv or "/archive-v" not in cairn_bin():
        return
    source = os.environ.get("CAIRN_SOURCE", SOURCE)
    if subprocess.run([uv, "tool", "install", *UV_TOOL_FLAGS, source]).returncode:
        raise CliError(f"uv tool install {source} failed")
    tool_bin = subprocess.run([uv, "tool", "dir", "--bin"], capture_output=True, text=True).stdout.strip()
    os.execv(f"{tool_bin}/cairn", [f"{tool_bin}/cairn", *argv])


def _message(status: int, body: Any) -> str:
    return str(body.get("message")) if isinstance(body, dict) and body.get("message") else f"HTTP {status}"


def install(use_local: bool, url: str | None, key: str | None, service: bool) -> None:
    _persist(sys.argv[1:])
    if not (use_local or url):
        use_local = _ask("Set up a personal Cairn store on this machine? [Y/n] ").lower() not in ("n", "no")
        url = _ask("Team Cairn URL (blank for none): ") or None
        if not (use_local or url):
            raise CliError("nothing to set up")
    if url:
        remote_add(url, key, replace=False)
    if use_local:
        local.enable(None, service)
    if (Path.home() / ".claude").exists() or shutil.which("claude"):
        harness.setup("claude")
    if harness.codex_home().exists() or shutil.which("codex"):
        harness.setup("codex")


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def uninstall() -> None:
    harness.setup("claude", uninstall=True)
    harness.setup("codex", uninstall=True)
    local.stop()
    print(f"cairn: removed from harnesses; data kept in {home()}")


def upgrade() -> None:
    uv = shutil.which("uv")
    # --reinstall-package makes a git source fetch its latest commit.
    flags = ["--reinstall-package", "cairnkb", "--exclude-newer", "7 days"]
    if not uv or subprocess.run([uv, "tool", "upgrade", "cairnkb", *flags]).returncode:
        raise CliError("uv tool upgrade cairnkb failed")
    if local.service_file().exists():
        local.service_start(restart=True)
    for args in harness.configured():  # the upgraded binary rewrites its own config
        subprocess.run([cairn_bin(), "setup", *args])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cairn", description="Cairn knowledge store")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the server (hosted: CAIRN_DB__URL; local: --local)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int)
    p.add_argument("--local", action="store_true", help="local mode, on the port from CAIRN_HOME/config.toml")
    p.set_defaults(run=lambda a: serve(a.host, a.port, a.local))

    group = sub.add_parser("local", help="the local store and server").add_subparsers(dest="action", required=True)
    p = group.add_parser("enable")
    p.add_argument("--port", type=int)
    p.add_argument("--no-service", action="store_true", help="run detached instead of as a login service")
    p.add_argument("--from-git", metavar="URL", help="restore the store from a git mirror first")
    p.set_defaults(run=lambda a: local_enable(a.port, not a.no_service, a.from_git))
    group.add_parser("disable").set_defaults(run=lambda a: local_disable())

    group = sub.add_parser("remote", help="the team remote").add_subparsers(dest="action", required=True)
    p = group.add_parser("add")
    p.add_argument("url")
    p.add_argument("--key")
    p.add_argument("--replace", action="store_true")
    p.set_defaults(run=lambda a: remote_add(a.url, a.key, a.replace))
    group.add_parser("remove").set_defaults(run=lambda a: remote_remove())
    p = group.add_parser("bootstrap", help="create a new instance's admin with its setup token")
    p.add_argument("url")
    p.add_argument("--token", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--name")
    p.set_defaults(run=lambda a: remote_bootstrap(a.url, a.token, a.email, a.name))

    group = sub.add_parser("git", help="back up the local store to git").add_subparsers(dest="action", required=True)
    p = group.add_parser("enable")
    p.add_argument("--remote", help="a private git URL to push to")
    p.add_argument("--i-know-its-private", action="store_true", help="skip the public-repository check")
    p.set_defaults(run=lambda a: gitmode.enable(a.remote, a.i_know_its_private))
    group.add_parser("disable").set_defaults(run=lambda a: gitmode.disable())
    group.add_parser("status").set_defaults(run=lambda a: gitmode.status())
    group.add_parser("push").set_defaults(run=lambda a: gitmode.push())
    group.add_parser("purge", help="rewrite the mirror's history after a purge").set_defaults(
        run=lambda a: gitmode.purge())
    p = sub.add_parser("restore", help="rebuild an empty local store from a git mirror")
    p.add_argument("source", nargs="?", help="git URL or path; omit to use this machine's ~/.cairn/mirror.git")
    p.add_argument("--port", type=int)
    p.add_argument("--no-service", action="store_true")
    p.set_defaults(run=lambda a: restore(a.source, a.port, not a.no_service))

    p = sub.add_parser("auth", help="print the MCP Authorization header (header helper)")
    p.add_argument("what", choices=["header"])
    p.add_argument("--server", help="the MCP endpoint this harness config was set up for")
    p.set_defaults(run=lambda a: auth_header(a.server))

    p = sub.add_parser("inject", help="SessionStart hook: print the startup context")
    p.add_argument("--hook", choices=["claude-code", "codex"])
    p.add_argument("--budget", type=int, default=8000)
    p.set_defaults(run=lambda a: print(json.dumps(inject(a.budget))))

    p = sub.add_parser("ui", help="open the web UI, logged in")
    p.add_argument("--remote", action="store_true", help="the team server's UI, when local is also set up")
    p.set_defaults(run=lambda a: ui(a.remote))

    p = sub.add_parser("setup", help="connect a harness to Cairn")
    p.add_argument("harness", choices=["claude", "codex"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--allow-writes", action="store_true", help="let the agent write without prompting")
    p.set_defaults(run=lambda a: harness.setup(a.harness, a.dry_run, a.uninstall, a.allow_writes))

    p = sub.add_parser("service", help="the local server's login service")
    p.add_argument("action", choices=["install", "uninstall", "start", "stop", "status"])
    p.set_defaults(run=lambda a: print(json.dumps(local.status())) if a.action == "status"
                   else getattr(local, f"service_{a.action}")())

    p = sub.add_parser("install", help="set up local and/or remote, then the harnesses")
    p.add_argument("--local", action="store_true")
    p.add_argument("--remote")
    p.add_argument("--key")
    p.add_argument("--no-service", action="store_true")
    p.set_defaults(run=lambda a: install(a.local, a.remote, a.key, not a.no_service))
    sub.add_parser("uninstall", help="remove Cairn from the harnesses and stop the server; data is kept").set_defaults(
        run=lambda a: uninstall())
    sub.add_parser("upgrade").set_defaults(run=lambda a: upgrade())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        args.run(args)
    except (CliError, *NETWORK_ERRORS) as exc:
        print(f"cairn: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
