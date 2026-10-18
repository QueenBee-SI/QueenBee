"""Run QueenBee in its own window.

    python -m queenbee.mother               # the window (starts the host if needed)
    python -m queenbee.mother --shortcuts   # Start menu and desktop shortcuts
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from ..agents.clean_env import scrub_environ
from ..agents.host_client import HostClient
from ..config import _state_dir
from ..singleton import AlreadyRunning, InstanceLock

ROOT = Path(__file__).resolve().parent.parent.parent
ICON = Path(__file__).resolve().parent.parent / "assets" / "queenbee.ico"
TITLE = "QueenBee"


def create_shortcuts() -> list[str]:
    if os.name != "nt":
        return _posix_launchers()
    exe = Path(sys.executable)
    frozen = bool(getattr(sys, "frozen", False))          # the packaged QueenBee.exe
    windowed = exe.with_name("pythonw.exe")
    target = str(exe if frozen else windowed if windowed.exists() else exe)
    arguments = "" if frozen else "-m queenbee.mother"
    made = []
    for lnk in (
        Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "QueenBee.lnk",
        Path(os.environ["USERPROFILE"]) / "Desktop" / "QueenBee.lnk",
    ):
        script = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:M_LNK);"
            "$s.TargetPath = $env:M_TARGET; $s.Arguments = $env:M_ARGS;"
            "$s.WorkingDirectory = $env:M_CWD; $s.IconLocation = $env:M_ICON;"
            "$s.Description = 'QueenBee - one instruction; a queen bee plans, worker bees work';"
            "$s.Save()"
        )
        env = dict(os.environ, M_LNK=str(lnk), M_TARGET=target, M_ARGS=arguments,
                   M_CWD=str(exe.parent if frozen else ROOT), M_ICON=f"{ICON},0")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=env, check=True, capture_output=True)
        made.append(str(lnk))
    return made


def _answers(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "api/ping", timeout=3) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


def _tell(text: str) -> None:
    """A message the user can see even though the packaged app has no console."""
    print(text, file=sys.stderr, flush=True)
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, text, TITLE, 0x30)
        except Exception:
            pass


LINUX_WINDOW = ("For QueenBee's own window on Linux, install either Qt's web engine libraries (Ubuntu/Debian: "
                "sudo apt install libnss3 libxkbcommon-x11-0 libxcb-cursor0, then .venv/bin/pip install 'pywebview[qt]') "
                "or GTK's (sudo apt install libgirepository1.0-dev libcairo2-dev gir1.2-webkit2-4.1, then "
                ".venv/bin/pip install PyGObject).")


def _browser_instead(url: str, exc: BaseException, log: Path) -> bool:
    """The window could not open: the page in the default browser instead, and why. True when a browser took it."""
    import webbrowser

    try:
        opened = bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001  (no browser at all, a broken registry entry)
        opened = False
    print(url, flush=True)
    # webbrowser can report a browser that then fails (no default browser set): the address is always given.
    where = (f"It is opening in your browser instead; if it does not, open {url}" if opened
             else f"Open this address in a browser: {url}")
    hint = f"\n\n{LINUX_WINDOW}" if sys.platform.startswith("linux") else ""
    _tell(f"QueenBee could not open its own window ({exc}). {where}\n\nThe details are in {log}.{hint}")
    return opened


def _posix_launchers() -> list[str]:
    """macOS: a `QueenBee.command` on the Desktop (double-click opens it in Terminal and starts the window).
    Linux: a `.desktop` entry in ~/.local/share/applications and a copy on the Desktop."""
    exe = Path(sys.executable)
    cmd = f'"{exe}"' if getattr(sys, "frozen", False) else f'"{exe}" -m queenbee.mother'
    root = Path(__file__).resolve().parent.parent.parent
    made = []
    desktop = Path.home() / "Desktop"
    if sys.platform == "darwin":
        path = desktop / "QueenBee.command"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["#!/bin/bash", "# Opens QueenBee. Made by `python -m queenbee.mother --shortcuts`.", f'cd "{root}"', f"exec {cmd}", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        path.chmod(0o755)
        made.append(str(path))
    else:
        lines = ["[Desktop Entry]", "Type=Application", "Name=QueenBee", "Comment=Queen bees plan, worker bees do the work",
                 f"Exec={cmd}", f"Path={root}", "Terminal=false", "Categories=Development;", ""]
        for folder in (Path.home() / ".local" / "share" / "applications", desktop):
            try:
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / "queenbee.desktop"
                path.write_text("\n".join(lines), encoding="utf-8")
                path.chmod(0o755)
                made.append(str(path))
            except OSError:
                continue
    return made


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="queenbee.mother")
    parser.add_argument("--port", type=int, default=7880)
    parser.add_argument("--host-port", type=int, default=7877)
    parser.add_argument("--shortcuts", action="store_true")
    parser.add_argument("--no-window", action="store_true", help="serve the page only")
    args = parser.parse_args(argv)
    if args.shortcuts:
        for path in create_shortcuts():
            print("created", path)
        return 0

    scrub_environ()
    state = _state_dir()
    state.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(state / "mother.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    lock = InstanceLock(state / f"mother-{args.port}.lock")
    url = f"http://127.0.0.1:{args.port}/"
    try:
        lock.acquire()
    except AlreadyRunning:
        from ..app import bring_forward

        if bring_forward(TITLE):
            return 0
        # No window to bring forward. If the instance holding the lock is serving (a headless
        # server, e.g. a benchmark run), open a window onto it. If it does not answer it is still
        # starting or stuck closing: wait a little, take the lock if it is let go, and never open a
        # window onto a dead server (that is a black screen).
        from .. import shell

        from . import auth

        for _ in range(20):
            if _answers(url):
                token = auth.read_token(args.port)                    # the live server's token, for its page
                return shell.run(url + (f"?k={token}" if token else ""), width=1280, height=860, title=TITLE)
            time.sleep(0.5)
            try:
                lock.acquire()
                break
            except AlreadyRunning:
                continue
        else:
            _tell("QueenBee is still closing from the last time. Wait a few seconds and open it again.\n"
                  "If this keeps happening, end 'QueenBee.exe' in Task Manager.")
            return 1

    # Mother shares QueenBee's host; start it if QueenBee is not running.
    from ..agents.host_client import pick_port

    args.host_port = pick_port(args.host_port)       # not a port another program's host already holds
    HostClient(args.host_port).ensure_running()
    from .server import serve

    # The page server for agent states sits two ports below the queen's (7880 -> 7878, as always), so a
    # second copy on other ports is a whole, separate QueenBee and not a tangle with the first.
    httpd = serve(args.port, args.host_port, with_mother=True, ui_port=args.port - 2)
    threading.Thread(target=httpd.serve_forever, name="mother-http", daemon=True).start()
    url = f"http://127.0.0.1:{args.port}/?k={httpd.token}"             # the page gets the API token once, here
    try:
        if args.no_window:
            print(url, flush=True)
            threading.Event().wait()
        from .. import shell

        return shell.run(url, width=1280, height=860, title=TITLE)
    except Exception as exc:
        # The finally below ends the packaged app with os._exit, which would swallow this: say what went wrong.
        logging.getLogger("queenbee.mother").exception("the window could not open")
        # The page works in any browser: open it there and keep serving, so a missing window library (Linux without
        # GTK or Qt, a broken WebView2 on Windows) does not leave the user with nothing.
        _browser_instead(url, exc, state / "mother.log")
        threading.Event().wait()
        return 0
    finally:
        # shutdown() can wait on a connection that never ends; do not let that keep the lock held.
        threading.Thread(target=httpd.shutdown, daemon=True).start()
        time.sleep(0.5)
        lock.release()
        if getattr(sys, "frozen", False):
            os._exit(0)              # closing the window ends the app (workers live on in the host)


if __name__ == "__main__":
    raise SystemExit(main())
