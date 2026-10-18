"""The UI server's view of the host: the same interface as Registry, over a socket.

The server was written against `Registry`. This class offers the same methods,
so the server does not need to know whether agents live in its own process or
in the host. The difference that matters is `shutdown`: here it does nothing,
because the server stopping -- for a restart, an upgrade, a closed window -- is
precisely the event the agents are meant to survive.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("queenbee.agents.host_client")

DEFAULT_PORT = 7877
_ROOT = Path(__file__).resolve().parent.parent.parent

# Windows process creation flags for a process that must outlive the console
# and the job it was started from.
#
# CREATE_NO_WINDOW, not DETACHED_PROCESS: both keep the host off the launching
# console, but a DETACHED host has no console at all, and every ConPTY it then
# opens is handed to the user's default terminal -- each agent terminal showed
# up as a visible Windows Terminal window, and closing those windows ended the
# agents inside them. With a hidden console of its own the host's ConPTYs stay
# headless. Measured: ConPTY from a detached parent opened a visible window,
# from a CREATE_NO_WINDOW parent none; WinPTY opened none either way.
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class HostUnavailable(RuntimeError):
    pass


def spawn_detached(argv: list[str]) -> None:
    """Start a process that outlives this one, its console and its job."""
    kwargs: dict[str, Any] = {
        "cwd": str(_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        flags = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(argv, creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except OSError:
            # The job this process runs in forbids breakaway. Detaching is
            # still the best available.
            log.warning("could not break away from the current job")
            subprocess.Popen(argv, creationflags=flags, **kwargs)
    else:
        subprocess.Popen(argv, start_new_session=True, **kwargs)


# The host's API can start processes, so every call carries a secret. The host
# writes a fresh one at each start to a file only this user can read; a worker
# the host launched may instead get it in this variable (see queenbee.gpu run),
# since a workload's own interpreter can see a different state directory.
TOKEN_HEADER = "X-QueenBee-Token"
TOKEN_ENV = "QUEENBEE_HOST_TOKEN"


def token_path(port: int = DEFAULT_PORT) -> Path:
    from ..config import _state_dir

    return _state_dir() / f"host-{int(port)}.token"


def read_token(port: int = DEFAULT_PORT) -> Optional[str]:
    try:
        return token_path(port).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


class HostClient:
    def __init__(self, port: int = DEFAULT_PORT, *, autostart: bool = True,
                 token: Optional[str] = None) -> None:
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.autostart = autostart
        self._explicit_token = token
        self._token = token or os.environ.get(TOKEN_ENV) or None

    def token(self, reload: bool = False) -> Optional[str]:
        if self._explicit_token:
            return self._explicit_token
        if reload or not self._token:
            self._token = read_token(self.port) or os.environ.get(TOKEN_ENV) or None
        return self._token

    # -- transport -------------------------------------------------------

    def _request(self, path: str, payload: Optional[dict] = None, timeout: float = 10.0) -> Any:
        data = None if payload is None else json.dumps(payload).encode()
        for attempt in (1, 2):
            headers = {"Content-Type": "application/json"} if data else {}
            token = self.token(reload=attempt == 2)
            if token:
                headers[TOKEN_HEADER] = token
            req = urllib.request.Request(
                self.base + path, data=data, headers=headers,
                method="POST" if data is not None else "GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as exc:
                # A restarted host has a new token; read it once and retry.
                if exc.code == 401 and attempt == 1 and not self._explicit_token:
                    continue
                raise
        raise HostUnavailable("unreachable")

    def ping(self) -> Optional[dict[str, Any]]:
        try:
            info = self._request("/ping", timeout=2.0)
            return info if info.get("ok") else None
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        payload = {"method": method, "args": list(args), "kwargs": kwargs}
        for attempt in (1, 2):
            try:
                reply = self._request("/call", payload)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                if attempt == 1 and self.autostart and self.ensure_running():
                    continue
                raise HostUnavailable(str(exc)) from exc
            if not reply.get("ok"):
                raise HostUnavailable(reply.get("error") or "host error")
            return reply.get("result")
        raise HostUnavailable("unreachable")

    # -- lifecycle -------------------------------------------------------

    def authorized(self) -> bool:
        """True when a host answers on this port AND accepts our token (another program's host on the port says no)."""
        try:
            self._request("/call", {"method": "terminals", "args": [], "kwargs": {}}, timeout=3.0)
            return True
        except urllib.error.HTTPError:
            return False
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def ensure_running(self, wait_s: float = 12.0) -> bool:
        """Start the host if nothing answers, detached from this process.

        Detached, in its own process group and -- where the job allows it --
        broken away from the job, so that closing the terminal that launched
        the UI server does not take the host down with it.
        """
        if self.ping() is not None:
            return True

        # Launched through an intermediate that exits immediately, so the host
        # is not recorded as this process's child.
        spawn_detached([sys.executable, "-m", "queenbee.host", "--port", str(self.port), "--spawn"])

        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self.ping() is not None:
                log.info("queenbee host started on port %d", self.port)
                return True
            time.sleep(0.25)
        log.error("queenbee host did not come up on port %d", self.port)
        return False

    def shutdown_host(self) -> None:
        """Explicitly end the host and everything in it."""
        try:
            self._request("/shutdown", {})
        except (urllib.error.URLError, OSError, ValueError):
            pass

    def shutdown(self) -> None:
        """Called when the UI server stops. Deliberately does nothing."""

    # -- Registry interface ----------------------------------------------

    def _dict_call(self, method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return self._call(method, *args, **kwargs)
        except HostUnavailable as exc:
            return {"ok": False, "error": f"queenbee host unavailable: {exc}"}

    def available(self) -> bool:
        try:
            return bool(self._call("available"))
        except HostUnavailable:
            return False

    def start(self, cwd: str, **kwargs: Any) -> dict[str, Any]:
        return self._dict_call("start", cwd, **kwargs)

    def send(self, key: str, text: str) -> dict[str, Any]:
        return self._dict_call("send", key, text)

    def stop(self, key: str, *, drop_worktree: bool = False) -> dict[str, Any]:
        return self._dict_call("stop", key, drop_worktree=drop_worktree)

    def events(self, key: str, since: int = 0) -> dict[str, Any]:
        return self._dict_call("events", key, since)

    def worktree_for(self, key: str) -> Optional[dict[str, Any]]:
        try:
            return self._call("worktree_for", key)
        except HostUnavailable:
            return None

    def pty_supported(self) -> dict[str, Any]:
        return self._dict_call("pty_supported")

    def open_terminal(self, cwd: str, argv: Optional[list[str]] = None, *,
                      cols: int = 120, rows: int = 32) -> dict[str, Any]:
        return self._dict_call("open_terminal", cwd, argv, cols=cols, rows=rows)

    def terminal_read(self, key: str, since: int = 0) -> dict[str, Any]:
        return self._dict_call("terminal_read", key, since)

    def terminal_write(self, key: str, data: str) -> dict[str, Any]:
        return self._dict_call("terminal_write", key, data)

    def terminal_resize(self, key: str, cols: int, rows: int) -> dict[str, Any]:
        return self._dict_call("terminal_resize", key, cols, rows)

    def terminal_close(self, key: str) -> dict[str, Any]:
        return self._dict_call("terminal_close", key)

    def descendants(self, root: int) -> Optional[set[int]]:
        """Remembered descendants from the host, or None if it cannot say."""
        try:
            return set(self._call("descendants", root) or [])
        except HostUnavailable:
            return None

    def ws_info(self) -> Optional[dict[str, Any]]:
        """Where and how the page may open terminal WebSockets, if it may.

        Cached briefly: the token only changes when the host restarts, and
        the page asks on every terminal refresh.
        """
        now = time.monotonic()
        cached = getattr(self, "_ws_cache", None)
        if cached and now - cached[0] < 10.0:
            return cached[1]
        try:
            # Hosts before version 3 have no socket; asking would only log an
            # error in their log every few seconds.
            if ((self.ping() or {}).get("version") or 0) < 3:
                raise HostUnavailable("host has no terminal socket")
            info = self._call("ws_info")
            result = {"port": self.port, "token": info["token"]}
        except (HostUnavailable, KeyError, TypeError):
            # An older host has no terminal socket; the page polls instead.
            result = None
        self._ws_cache = (now, result)
        return result

    def listing(self) -> list[dict[str, Any]]:
        try:
            return self._call("listing") or []
        except HostUnavailable:
            return []

    def terminals(self, sampler: Optional[Callable[[int], Any]] = None) -> list[dict[str, Any]]:
        """The host reports terminals; measuring them stays on this side."""
        try:
            rows = self._call("terminals") or []
        except HostUnavailable:
            return []
        if sampler is None:
            return rows
        for row in rows:
            pid = row.get("pid")
            if not pid:
                continue
            try:
                procs = sampler(pid)
            except Exception:
                log.debug("terminal sampling failed", exc_info=True)
                continue
            row["cpu_pct"] = sum(p.cpu_pct for p in procs)
            row["rss_mb"] = sum(p.rss_mb for p in procs)
            vram = [p.vram_mb for p in procs if p.vram_mb is not None]
            row["vram_mb"] = sum(vram) if vram else None
            gpu = [p.gpu_pct for p in procs if p.gpu_pct is not None]
            # Per-engine shares summed can pass 100; the card cannot.
            row["gpu_pct"] = min(100.0, sum(gpu)) if gpu else None
        return rows


def pick_port(preferred: int = DEFAULT_PORT, tries: int = 20) -> int:
    """The port QueenBee's own host should use: the preferred one if it is free or already ours, else the next that is.

    Another program's host (another tool built on the same host, say) may hold the preferred port and refuse our token; using it would fail every
    terminal call with 401. Such a port is skipped.
    """
    import socket

    for port in range(preferred, preferred + tries):
        client = HostClient(port, autostart=False)
        if client.ping() is not None:
            if client.authorized():
                return port
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))                  # binds: nothing holds it, so a new host can take it
                return port
            except OSError:
                continue
    return preferred
