from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from ..core import mirror
from ..core.write import WriteError

QUIET, RETRY, PUSH_TIMEOUT = 2.0, 300.0, 120.0
FOREIGN = ("mirror.git holds a store this database never wrote (was cairn.db lost?); restore it with cairn restore "
           "after moving cairn.db aside, or move mirror.git aside to start a new mirror")
log = structlog.get_logger("cairn.git")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class GitMirror:
    """Mirrors the local store into a bare git repo after writes settle, and pushes it. Writes never wait on git."""

    def __init__(self, repo: Path, remote: str | None, engine: AsyncEngine) -> None:
        self.repo, self.remote, self.engine = repo, remote, engine
        self.state = "idle"
        self.error: str | None = None
        self.last_commit: str | None = None
        self.last_push: str | None = None
        self.pending = False
        self.digest: str | None = None
        self._dirty = asyncio.Event()
        self._lock = asyncio.Lock()

    def git(self, *args: str, data: bytes = b"", timeout: float | None = None) -> subprocess.CompletedProcess[bytes]:
        env = os.environ | {"GIT_TERMINAL_PROMPT": "0"}
        return subprocess.run(["git", f"--git-dir={self.repo}", *args], input=data, capture_output=True, env=env,
                              timeout=timeout)

    def init(self) -> None:
        try:
            if not self.repo.exists():
                subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(self.repo)], check=True,
                               capture_output=True)
            self.git("remote", "remove", "origin")
            if self.remote:
                self.git("remote", "add", "--", "origin", self.remote)
            head = self.git("log", "-1", "--format=%B", "refs/heads/main")
        except Exception as exc:  # noqa: BLE001 - a missing git must not keep the server down
            self.state, self.error = "error", str(exc)[:300]
            return
        for line in head.stdout.decode().splitlines():
            if line.startswith("Cairn-Digest: "):
                self.digest = line.split(": ", 1)[1]
        self.pending = bool(self.remote)  # a push that's already up to date is a cheap no-op

    def changed(self) -> None:
        self._dirty.set()

    def status(self) -> dict[str, Any]:
        return {"enabled": True, "state": self.state, "error": self.error, "remote": self.remote,
                "last_commit": self.last_commit, "last_push": self.last_push, "pending_push": self.pending}

    async def run(self) -> None:
        self._dirty.set()  # reconcile at startup: a crash may have left a write uncommitted
        while True:
            retry = self.state == "error" or (self.pending and self.state != "diverged")
            try:
                await asyncio.wait_for(self._dirty.wait(), RETRY if retry else None)
                await asyncio.sleep(QUIET)  # one commit per burst of writes
            except TimeoutError:
                pass
            self._dirty.clear()
            await self.sync()

    async def sync(self) -> None:
        async with self._lock:  # push_now and the loop would otherwise race two fast-imports onto main
            try:
                async with self.engine.connect() as conn:
                    files, claimed = await mirror.render(conn), await mirror.claimed(conn)
                digest = hashlib.sha256("".join(f"{p}\0{t}\0" for p, t in sorted(files.items())).encode()).hexdigest()
                if not claimed:
                    if digest != self.digest and await asyncio.to_thread(self._has_main):
                        self.state, self.error = "diverged", FOREIGN
                        return
                    async with self.engine.begin() as conn:
                        await mirror.claim(conn)
                if digest != self.digest:
                    await asyncio.to_thread(self._commit, files, digest)
                if self.pending and self.remote and self.state != "diverged":
                    await asyncio.to_thread(self._push)
            except Exception as exc:  # noqa: BLE001 - the mirror must never take the server down
                self.state, self.error = "error", str(exc)[:300]
                log.warning("git mirror failed", error=self.error)

    def _has_main(self) -> bool:
        return self.git("rev-parse", "--verify", "-q", "refs/heads/main").returncode == 0

    def _identity(self) -> tuple[str, str]:
        return (self.git("config", "user.name").stdout.decode().strip() or "Cairn",
                self.git("config", "user.email").stdout.decode().strip() or "cairn@localhost")

    def _commit(self, files: dict[str, str], digest: str) -> None:
        name, email = self._identity()
        message = f"cairn: sync {len(files) - 1} files\n\nCairn-Digest: {digest}\n".encode()
        stream = [b"commit refs/heads/main\n", f"committer {name} <{email}> {int(time.time())} +0000\n".encode(),
                  f"data {len(message)}\n".encode(), message, b"from refs/heads/main^0\n" if self._has_main() else b"",
                  b"deleteall\n"]
        for path, content in sorted(files.items()):
            body = content.encode()
            stream += [f"M 100644 inline {path}\ndata {len(body)}\n".encode(), body, b"\n"]
        result = self.git("fast-import", "--quiet", data=b"".join(stream))
        if result.returncode:
            raise RuntimeError(result.stderr.decode()[:300])
        self.git("-c", "gc.autoDetach=false", "gc", "--auto", "--quiet")  # fast-import never runs gc itself
        self.digest, self.last_commit, self.pending = digest, _now(), bool(self.remote)
        self.state, self.error = ("ok", None) if self.state != "diverged" else (self.state, self.error)

    def _push(self) -> None:
        result = self.git("push", "--porcelain", "origin", "refs/heads/main:refs/heads/main", timeout=PUSH_TIMEOUT)
        if result.returncode == 0:
            self.state, self.error, self.pending, self.last_push = "ok", None, False, _now()
            return
        out = (result.stdout + result.stderr).decode()
        diverged = "[rejected]" in out or "non-fast-forward" in out or "fetch first" in out
        self.state = "diverged" if diverged else "error"
        self.error = ("the remote has commits this machine doesn't (another machine writing to it?); "
                      "nothing was merged" if diverged else out.strip()[-300:])
        log.warning("git push failed", state=self.state, error=self.error)

    async def purge_history(self) -> dict[str, Any]:
        self.pending = bool(self.remote)
        await self.sync()  # after an interrupted purge this push is rejected; the squash below finishes the job
        async with self._lock:
            if self.state == "error" or self.error == FOREIGN or not self.digest:
                raise WriteError(f"the mirror isn't in sync ({self.error or self.state}); fix that first")
            await asyncio.to_thread(self._squash)
        return self.status()

    def _squash(self) -> None:
        old = self.git("rev-parse", "refs/heads/main").stdout.decode().strip()
        name, email = self._identity()
        made = self.git("-c", f"user.name={name}", "-c", f"user.email={email}", "commit-tree", "refs/heads/main^{tree}",
                        "-m", f"cairn: history purged\n\nCairn-Digest: {self.digest}\n")
        new = made.stdout.decode().strip()
        if made.returncode:
            raise WriteError(made.stderr.decode()[:300])
        if self.remote:  # remote first, so a refused push leaves the local history as it was
            try:
                listed = self.git("ls-remote", "origin", "refs/heads/main", timeout=PUSH_TIMEOUT)
                theirs = listed.stdout.decode().split("\t")[0].strip()
                # Overwrite only what this machine wrote: its old history, or an interrupted purge's commit.
                if listed.returncode or (theirs and self.git("cat-file", "-e", f"{theirs}^{{commit}}").returncode):
                    raise WriteError("the remote has commits this machine doesn't; nothing was rewritten")
                pushed = self.git("push", "--porcelain", f"--force-with-lease=refs/heads/main:{theirs}", "origin",
                                  f"{new}:refs/heads/main", timeout=PUSH_TIMEOUT)
            except subprocess.TimeoutExpired as exc:
                raise WriteError("the remote timed out; run cairn git purge again") from exc
            if pushed.returncode:
                raise WriteError("the remote refused the rewrite: " + (pushed.stdout + pushed.stderr).decode()[-300:])
            self.last_push = _now()
        if self.git("update-ref", "refs/heads/main", new, old).returncode:
            raise WriteError("couldn't move main to the rewritten history; run cairn git purge again")
        self.git("gc", "--prune=now", "--quiet")
        self.state, self.error, self.pending, self.last_commit = "ok", None, False, _now()

    async def push_now(self) -> dict[str, Any]:
        if self.state == "diverged":
            self.state = "error"  # an explicit push retries once, after the user has looked at the remote
        self.pending = bool(self.remote)
        await self.sync()
        return self.status()
