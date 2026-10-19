from __future__ import annotations

import asyncio
import io
import json
import re
import shutil
import subprocess
import tarfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from . import local
from .config import NETWORK_ERRORS, CliError, fetch, home, local_port, proven_token, read_toml, update_toml

_HOSTED = re.compile(r"^(?:[a-z+]+://)?(?:[^@/]+@)?(github\.com|gitlab\.com)[:/](.+?)(?:\.git)?/?$")


def enable(remote: str | None, private_ok: bool) -> None:
    if not local_port():
        raise CliError("git mode backs up the local store; run cairn local enable first")
    if remote:
        remote = _check_remote(remote, private_ok)
    current = read_toml("config.toml").get("git", {})
    update_toml("config.toml", "git", {"enabled": True, "remote": remote or current.get("remote", "")})
    _restart()
    time.sleep(3)  # the server commits 2 s after it starts; show how that, and the first push, went
    port, deadline = _port(), time.monotonic() + 20
    while (git := _git_status(port))["pending_push"] and git["state"] in ("idle", "ok") and time.monotonic() < deadline:
        time.sleep(0.5)
    if git["state"] in ("error", "diverged"):
        raise CliError(f"git mode is on, but the mirror failed: {git['error']}\nIt retries every 5 minutes; "
                       "change the remote with cairn git enable --remote URL, or check with cairn git status")
    if not git["remote"]:
        print(f"backing up to {home() / 'mirror.git'} on this machine only; "
              "add --remote URL (a private repo) to also push it off this machine")
    elif git["pending_push"]:
        print(f"backing up to {git['remote']}; the first push is still running, see cairn git status")
    else:
        print(f"backing up to {git['remote']} (and {home() / 'mirror.git'})")


def disable() -> None:
    update_toml("config.toml", "git", {**read_toml("config.toml").get("git", {}), "enabled": False})
    _restart()
    print("git mode off; the mirror is kept in", home() / "mirror.git")


def status() -> None:
    print(json.dumps(_git_status(_port()), indent=1))


def _git_status(port: int) -> dict[str, Any]:
    code, body = fetch(f"http://127.0.0.1:{port}/api/v1/status", token=proven_token(port))
    if code != 200:
        raise CliError(f"the local server answered {code}")
    git: dict[str, Any] = body["git"]
    return git


def push() -> None:
    _call("push", 180)


def purge() -> None:
    print("This replaces the mirror's history with one commit of the current store and force-pushes it, so purged "
          "entries leave git too. The git host may keep old objects for a while: rotate any leaked secret.")
    if input("Type purge to continue: ").strip() != "purge":
        raise CliError("cancelled")
    _call("purge", 300)


def _call(action: str, timeout: float) -> None:
    port = _port()
    code, body = fetch(f"http://127.0.0.1:{port}/api/v1/git/{action}", {}, proven_token(port), timeout=timeout)
    if code != 200:
        raise CliError(str(body.get("message", code)) if isinstance(body, dict) else f"HTTP {code}")
    print(json.dumps(body, indent=1))


def restore(source: str | None, port: int | None, service: bool) -> None:
    db, repo = home() / "cairn.db", home() / "mirror.git"
    store_files = [Path(f"{db}{suffix}") for suffix in ("", "-wal", "-shm")]
    if any(f.exists() for f in store_files):
        raise CliError(f"{db} (or its -wal/-shm file) already exists; restore only fills an empty store "
                       "(stop the server with cairn local disable, then move them aside)")
    cloned = False
    if source:
        if repo.exists():
            raise CliError(f"{repo} already exists; restore without a source uses it")
        _refuse_credentials(source)
        home().mkdir(parents=True, exist_ok=True, mode=0o700)
        if subprocess.run(["git", "clone", "--bare", "-q", "--", source, str(repo)]).returncode:
            raise CliError(f"could not clone {source}")
        cloned = True
    elif not repo.exists():
        raise CliError(f"no mirror at {repo}; pass the repo to restore from")
    done = False
    try:
        asyncio.run(_restore(f"sqlite:///{db}", _read(repo)))
        done = True
    except Exception as exc:
        raise CliError(f"restore failed: {exc}") from exc
    finally:
        if not done:  # leave nothing half-built, so the next attempt starts clean
            for f in store_files:
                f.unlink(missing_ok=True)
            if cloned:
                shutil.rmtree(repo, ignore_errors=True)
    remote = read_toml("config.toml").get("git", {}).get("remote", "")
    if source and not Path(source).exists():
        try:
            remote = _check_remote(source, False)
        except CliError as exc:
            remote = ""
            print(f"restored, but not pushing back to {source}: {exc}")
    update_toml("config.toml", "git", {"enabled": True, "remote": remote})
    local.enable(port, service)


def _read(repo: Path) -> dict[str, str]:
    # autocrlf off: archive applies checkout conversions, and a global autocrlf=true would CRLF every file.
    archive = subprocess.run(["git", f"--git-dir={repo}", "-c", "core.autocrlf=false", "archive", "--format=tar",
                              "refs/heads/main"], capture_output=True)
    if archive.returncode:
        raise CliError("the mirror has no commits on main")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        return {m.name: tar.extractfile(m).read().decode() for m in tar.getmembers() if m.isfile()}  # type: ignore[union-attr]


async def _restore(url: str, files: dict[str, str]) -> None:
    from ..core import admin, mirror
    from ..db import migrate
    from ..db.engine import make_engine

    await asyncio.to_thread(migrate.upgrade, url)
    engine = make_engine(url)
    try:
        async with engine.begin() as conn:
            await mirror.restore(conn, files, await admin._create_user(conn, "local@localhost", "local", True))
    finally:
        await engine.dispose()


def _refuse_credentials(url: str) -> None:
    parts = urlsplit(url)
    if parts.password or (parts.scheme in ("http", "https") and parts.username):
        raise CliError("that URL carries a credential, which would be stored in plain text; "
                       "use an SSH URL or a git credential helper")


def _check_remote(url: str, private_ok: bool) -> str:
    _refuse_credentials(url)
    local_repo = Path(url.removeprefix("file://")).expanduser()
    if local_repo.exists():  # absolute, since the server runs from another directory
        return str(local_repo.resolve())
    if private_ok:
        return url
    found = _HOSTED.match(url)
    code = 0
    if found:
        host, path = found[1], found[2]
        api = (f"https://api.github.com/repos/{path}" if host == "github.com"
               else f"https://gitlab.com/api/v4/projects/{quote(path, safe='')}")
        with suppress(*NETWORK_ERRORS):
            code = fetch(api)[0]
    if code == 404:  # asked anonymously, private and missing repos both answer 404
        return url
    raise CliError(f"{url} is public, so your notes would be too; use a private repo" if code == 200 else
                   f"can't confirm that {url} is private; if it is, pass --i-know-its-private")


def _port() -> int:
    port = local_port()
    if not port:
        raise CliError("the local store isn't enabled")
    local.ensure_running(port, 5.0)
    return port


def _restart() -> None:
    if port := local_port():
        local.start(restart=True)
        local.wait_ready(port, 20)
