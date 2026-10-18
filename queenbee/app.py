"""QueenBee as a desktop application.

    pythonw -m queenbee.app              # what the Start menu / desktop shortcut runs
    python -m queenbee.app --shortcuts   # create those shortcuts

One entry point that behaves like an installed app:

- no console window: the UI server is started detached if nothing answers on
  its port, with its output in the state directory's ui.log;
- one instance: starting QueenBee again brings the existing window forward;
- quitting ends the UI server only if this app started it. Agents and
  terminals live in the host and are never touched.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .agents.clean_env import scrub_environ
from .config import _state_dir
from .singleton import AlreadyRunning, InstanceLock

log = logging.getLogger("queenbee.app")

ROOT = Path(__file__).resolve().parent.parent
ICON = Path(__file__).resolve().parent / "assets" / "queenbee.ico"


# -- the UI server ---------------------------------------------------------

def server_up(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/decisions", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def start_server(port: int, host_port: int, wait_s: float = 30.0) -> Optional[subprocess.Popen]:
    """Start `queenbee --serve` without a console; returns the process once it answers."""
    log_path = _state_dir() / "ui.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    out = open(log_path, "a", encoding="utf-8")
    out.write(f"--- ui server started by the QueenBee app {time.ctime()}\n")
    out.flush()
    flags = 0
    if os.name == "nt":
        flags = 0x08000000 | 0x00000200      # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [console_python(), "-m", "queenbee", "--serve", "--port", str(port),
         "--host-port", str(host_port)],
        cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=flags,
    )
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log.error("ui server exited with %s; see %s", proc.returncode, log_path)
            return None
        if server_up(port):
            return proc
        time.sleep(0.3)
    log.error("ui server did not answer on port %d", port)
    return proc


def console_python() -> str:
    """python.exe next to pythonw.exe: the server writes to stdout."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def windowed_python() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else str(exe)


# -- one instance ----------------------------------------------------------

def bring_forward(title: str = "QueenBee") -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return False
        SW_SHOW, SW_RESTORE = 5, 9
        user32.ShowWindow(hwnd, SW_SHOW)
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


# -- shortcuts -------------------------------------------------------------

def create_shortcuts() -> list[str]:
    """Start menu and desktop shortcuts that run the app without a console."""
    if os.name != "nt":
        raise SystemExit("shortcuts are only created on Windows")
    appdata = Path(os.environ["APPDATA"])
    targets = [
        appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "QueenBee.lnk",
        Path(os.environ["USERPROFILE"]) / "Desktop" / "QueenBee.lnk",
    ]
    made = []
    for lnk in targets:
        script = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:QUEENBEE_LNK);"
            "$s.TargetPath = $env:QUEENBEE_TARGET;"
            "$s.Arguments = '-m queenbee.app';"
            "$s.WorkingDirectory = $env:QUEENBEE_CWD;"
            "$s.IconLocation = $env:QUEENBEE_ICON;"
            "$s.Description = 'QueenBee - what each coding agent costs';"
            "$s.Save()"
        )
        env = dict(os.environ, QUEENBEE_LNK=str(lnk), QUEENBEE_TARGET=windowed_python(),
                   QUEENBEE_CWD=str(ROOT), QUEENBEE_ICON=f"{ICON},0")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=env, check=True, capture_output=True)
        made.append(str(lnk))
    return made


# -- run -------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="queenbee.app")
    parser.add_argument("--port", type=int, default=7878)
    parser.add_argument("--host-port", type=int, default=7877)
    parser.add_argument("--shortcuts", action="store_true",
                        help="create Start menu and desktop shortcuts, then exit")
    args = parser.parse_args(argv)

    if args.shortcuts:
        for path in create_shortcuts():
            print("created", path)
        return 0

    scrub_environ()
    state = _state_dir()
    state.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(state / "app.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    # Include the UI port in the native window title.  A legacy/Astra build can
    # run beside the current app, and both used to be titled just "QueenBee".
    # FindWindowW would then bring whichever one Windows returned first to the
    # foreground, making the current desktop shortcut appear to open the old UI.
    window_title = f"QueenBee · {args.port}"
    lock = InstanceLock(state / f"app-{args.port}.lock")
    try:
        lock.acquire()
    except AlreadyRunning:
        bring_forward(window_title)
        return 0

    started: Optional[subprocess.Popen] = None
    if not server_up(args.port):
        from .agents.host_client import pick_port

        host_port = pick_port(args.host_port)
        if host_port != args.host_port:
            log.warning("host port %d is held by another program; using %d", args.host_port, host_port)
        started = start_server(args.port, host_port)
        if started is None or not server_up(args.port):
            log.error("QueenBee could not start its UI server")
            lock.release()
            return 2

    from . import shell

    try:
        return shell.run(
            f"http://127.0.0.1:{args.port}/", tray=True, title=window_title
        )
    finally:
        # Only what this app started. The host -- and every terminal in it --
        # keeps running.
        if started is not None and started.poll() is None:
            log.info("stopping the ui server this app started (pid %s)", started.pid)
            started.terminate()
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
