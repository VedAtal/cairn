"""The local server: `cairn local enable|disable`, the login service (launchd on macOS, a systemd user unit
elsewhere), and starting the server on demand for the startup hook and the header helper."""

from __future__ import annotations

import contextlib
import os
import plistlib
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import NETWORK_ERRORS, CliError, cairn_bin, fetch, home, local_port, read_toml, update_toml, write_private

LABEL = "local.cairn.server"
DEFAULT_PORT = 6804
MAC = sys.platform == "darwin"


def launchd_plist(cairn: str, cairn_home: Path, path: str) -> str:
    log = str(cairn_home / "logs" / "server.log")
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [cairn, "serve", "--local"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "EnvironmentVariables": {"PATH": path, "CAIRN_HOME": str(cairn_home)},
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }).decode()


def systemd_unit(cairn: str, cairn_home: Path) -> str:
    return (
        "[Unit]\nDescription=Cairn local knowledge server\nStartLimitIntervalSec=300\nStartLimitBurst=5\n\n"
        f'[Service]\nExecStart="{cairn}" serve --local\nRestart=on-failure\nRestartSec=5\n'
        f'Environment="CAIRN_HOME={cairn_home}"\n\n[Install]\nWantedBy=default.target\n'
    )


def service_file() -> Path:
    if MAC:
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return Path.home() / ".config" / "systemd" / "user" / "cairn.service"


def _run(*cmd: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", "")


def _launchctl(*args: str) -> bool:
    return _run("launchctl", *args).returncode == 0


def _systemctl(*args: str) -> bool:
    return _run("systemctl", "--user", *args).returncode == 0


def _target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def service_install() -> None:
    path = service_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    (home() / "logs").mkdir(parents=True, exist_ok=True)
    if MAC:
        _launchctl("bootout", _target())  # so a changed plist is reloaded
        path.write_text(launchd_plist(cairn_bin(), home(), os.environ.get("PATH", "/usr/bin:/bin")))
    else:
        path.write_text(systemd_unit(cairn_bin(), home()))
        _systemctl("daemon-reload")
        _systemctl("enable", "cairn")
    service_start(restart=True)


def service_uninstall() -> None:
    path = service_file()
    service_stop()
    if not MAC:
        _systemctl("disable", "cairn")
    path.unlink(missing_ok=True)
    if not MAC:
        _systemctl("daemon-reload")


def service_start(restart: bool = False) -> None:
    for _ in range(10):  # launchd can still be tearing down a job that was just booted out
        if MAC:
            # bootstrap loads and starts it (RunAtLoad); it fails when already loaded, and then kickstart starts it.
            ok = _launchctl("bootstrap", f"gui/{os.getuid()}", str(service_file())) or _launchctl(
                "kickstart", *(["-k"] if restart else []), _target())
        else:
            ok = _systemctl("restart" if restart else "start", "cairn")
        if ok:
            return
        time.sleep(0.5)
    raise CliError("could not start the cairn service")


def service_stop() -> None:
    if MAC:
        _launchctl("bootout", _target())
        for _ in range(50):  # bootout returns before launchd has torn the job down; wait so stop is synchronous
            if not _launchctl("print", _target()):
                break
            time.sleep(0.1)
    else:
        _systemctl("stop", "cairn")


def start(restart: bool = False) -> None:
    """Start the local server through the service if one is installed, else as a detached process."""
    if service_file().exists():
        return service_start(restart)
    pid = _recorded_pid()
    if pid and not restart:
        return
    if pid:
        _kill(pid)
    root = home()
    (root / "logs").mkdir(parents=True, exist_ok=True)
    with (root / "logs" / "server.log").open("ab") as log:
        proc = subprocess.Popen([sys.executable, "-m", "cairn.cli.main", "serve", "--local"], cwd=root,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    write_private(root / "run" / "server.pid", str(proc.pid))


def stop() -> None:
    """Stop the server: uninstall the service, or kill the detached process."""
    if service_file().exists():
        service_uninstall()
    if pid := _recorded_pid():
        _kill(pid)
    (home() / "run" / "server.pid").unlink(missing_ok=True)


def ready(port: int, timeout: float = 1.0, path: str = "/readyz") -> bool:
    try:
        return fetch(f"http://127.0.0.1:{port}{path}", timeout=timeout)[0] == 200
    except NETWORK_ERRORS:
        return False


def wait_ready(port: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not ready(port, timeout=max(0.1, min(1.0, deadline - time.monotonic()))):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def ensure_running(port: int, seconds: float) -> bool:
    if ready(port, timeout=min(1.0, seconds)):
        return True
    start()
    return wait_ready(port, seconds)


def enable(port: int | None, service: bool) -> None:
    root = home()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (root / "run" / "token").exists():
        write_private(root / "run" / "token", secrets.token_urlsafe(32))
    current = read_toml("config.toml").get("local", {}).get("port")
    port = port or (int(current) if current else _pick_port())
    update_toml("config.toml", "local", {"enabled": True, "port": port})
    if service:
        service_install()
    else:
        start(restart=True)
    if not wait_ready(port, 20):
        raise CliError(f"the local server did not become ready; see {root / 'logs' / 'server.log'}")
    print(port)


def disable() -> None:
    stop()
    update_toml("config.toml", "local", {"enabled": False})


def status() -> dict[str, Any]:
    port = local_port()
    return {"installed": service_file().exists(), "running": bool(port and ready(port, path="/healthz")),
            "port": port, "pid": _service_pid() if service_file().exists() else _recorded_pid()}


def _pick_port() -> int:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", DEFAULT_PORT))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _recorded_pid() -> int | None:
    """The detached server's pid, if it is still alive."""
    try:
        pid = int((home() / "run" / "server.pid").read_text())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # A zombie (exited but never reaped, e.g. under an init-less container PID 1) still answers kill(0).
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0] != "Z"
    except OSError:  # no /proc (macOS, where launchd reaps orphans)
        return True


def _service_pid() -> int | None:
    if MAC:
        found = re.search(r'"PID" = (\d+);', _run("launchctl", "list", LABEL).stdout)
        return int(found.group(1)) if found else None
    out = _run("systemctl", "--user", "show", "-p", "MainPID", "--value", "cairn").stdout.strip()
    return int(out) if out.isdigit() and out != "0" else None


def _kill(pid: int) -> None:
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        if not _alive(pid):
            return
        time.sleep(0.1)
