"""The QueenBee host: the process that keeps agents and terminals alive.

Everything that must survive a restart lives here and nowhere else: running
agents, open terminals, and their output buffers. The UI server attaches to it
over a local socket and can be stopped, restarted or upgraded without touching
any of them.

This split exists because the alternative cost real work twice. Terminals were
owned by the UI server, so every restart -- including the ones needed to pick
up a code change -- closed their pseudo-consoles and ended the training runs
inside them. A multiplexer's server exists for exactly this reason, and so does
this one.

The host is kept deliberately small, because while it is the one part that
cannot be restarted without cost, it is also the one part that should rarely
need to be. Measurement, merging, decisions and the UI all live in the server.

Run:
    python -m queenbee.host              # normally started for you by --serve
    python -m queenbee.host --status
    python -m queenbee.host --stop       # ends every agent and terminal it holds
"""

from __future__ import annotations

import argparse
import atexit
import faulthandler
import json
import logging
import os
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from .agents.registry import Registry
from .config import _state_dir
from .lineage import Lineage
from .gpu_control import GPUController
from .singleton import AlreadyRunning, InstanceLock
from .wsproto import WebSocket, accept_key
from .agents.host_client import TOKEN_HEADER, token_path

log = logging.getLogger("queenbee.host")

DEFAULT_PORT = 7877
HOST_VERSION = 4      # 4: host-owned cooperative GPU pressure controller

# The only operations a client may invoke. Anything not listed is refused, so
# the socket is not a general way to call into the process.
METHODS = {
    "available", "start", "send", "stop", "events", "worktree_for",
    "pty_supported", "open_terminal", "terminal_read", "terminal_write",
    "terminal_resize", "terminal_close", "terminals", "listing",
    "descendants", "ws_info",
    "gpu_register", "gpu_heartbeat", "gpu_unregister", "gpu_state", "gpu_control",
    "gpu_can_resume",
}


class HostState:
    def __init__(self, api_token: Optional[str] = None) -> None:
        # Required on every /call and /shutdown. Kept apart from `token` below,
        # which is handed to the QueenBee page for its terminal socket: this one
        # can start processes and never reaches a browser. `serve` writes it
        # to a file only this user can read (host_client.token_path).
        self.api_token = api_token or secrets.token_urlsafe(32)
        self.registry = Registry()
        # Who started whom, remembered past a dead parent. It lives here rather
        # than in the UI server because it is only useful if it spans the
        # moment a job is launched and the moment someone asks about it -- and
        # a UI restart in between would otherwise erase the link between a
        # running training job and the conversation that started it.
        self.lineage = Lineage()
        self.lineage.start()
        self.started_at = time.time()
        self.stop = threading.Event()
        # Required to open a terminal WebSocket. Handed to the UI server over
        # the local API and from there to the QueenBee page; a web page on some
        # other site cannot read either response (no CORS headers), so it
        # cannot learn it.
        self.token = secrets.token_urlsafe(24)
        from .config import _state_dir

        # What each job adds to the GPU, learned once and kept across restarts.
        self.gpu = GPUController(footprints_path=_state_dir() / "gpu_footprints.json")
        self.gpu.start()

    def call(self, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        if method not in METHODS:
            raise ValueError(f"method not allowed: {method}")
        if method.startswith("gpu_"):
            return getattr(self.gpu, method[4:])(*args, **kwargs)
        if method == "descendants":
            return sorted(self.lineage.descendants(int(args[0])))
        if method == "ws_info":
            return {"token": self.token}
        if method == "terminals":
            # Measurement belongs to the server; the host only reports what it
            # holds, so it never needs updating when measurement changes.
            return self.registry.terminals(None)
        return getattr(self.registry, method)(*args, **kwargs)


def _local_host(value: str) -> bool:
    host = (value or "").rsplit(":", 1)[0].strip("[]").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def _local_origin(origin: str) -> bool:
    if not origin:
        return True           # not a browser: curl, the UI server, tests
    parts = urlsplit(origin)
    return parts.scheme == "http" and (parts.hostname or "") in ("127.0.0.1", "localhost", "::1")


class Handler(BaseHTTPRequestHandler):
    state: HostState
    protocol_version = "HTTP/1.1"
    server_version = "QueenBeeHost"

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _reply(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, separators=(",", ":"), default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _refuse_foreign(self) -> bool:
        """Turn away requests a web page on another site could make.

        Everything here can type into the user's terminals. A page on any site
        can send requests to 127.0.0.1, and with DNS rebinding can even make
        them look same-origin. So: the Host header must name this machine
        (defeats rebinding), a browser Origin must be local, and bodies must
        be JSON -- which a cross-site page cannot send without a CORS
        preflight this server never approves.
        """
        if not _local_host(self.headers.get("Host", "")):
            self._reply({"ok": False, "error": "forbidden host"}, 403)
            return True
        if not _local_origin(self.headers.get("Origin", "")):
            self._reply({"ok": False, "error": "forbidden origin"}, 403)
            return True
        return False

    def do_GET(self) -> None:  # noqa: N802
        if self._refuse_foreign():
            return
        if self.path.split("?", 1)[0] == "/ws/terminal":
            self._terminal_socket()
            return
        if self.path.split("?", 1)[0] == "/ping":
            reg = self.state.registry
            self._reply({
                "ok": True,
                "pid": os.getpid(),
                "version": HOST_VERSION,
                "started_at": self.state.started_at,
                "uptime_s": time.time() - self.state.started_at,
                "agents": len(reg.listing()),
                "terminals": len(reg.terminals(None)),
            })
            return
        self._reply({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if self._refuse_foreign():
            return
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            self._reply({"ok": False, "error": "JSON body required"}, 415)
            return
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
        except (ValueError, OSError):
            self._reply({"ok": False, "error": "bad request"}, 400)
            return
        # Origin and Host checks stop other websites; this stops other
        # programs on the machine, which can send any header they like but
        # cannot read the token file. Checked after the body is read: replying
        # while the client is still sending makes Windows reset the connection,
        # and the client then sees a dead host instead of a 401 it can retry.
        sent = self.headers.get(TOKEN_HEADER) or ""
        if not secrets.compare_digest(sent, self.state.api_token):
            self._reply({"ok": False, "error": "missing or wrong host token"}, 401)
            return
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._reply({"ok": False, "error": "bad request"}, 400)
            return

        if path == "/shutdown":
            log.warning("stop requested over HTTP from %s; ending all sessions",
                        self.address_string())
            self._reply({"ok": True})
            threading.Thread(target=self.state.stop.set, daemon=True).start()
            return
        if path != "/call":
            self._reply({"ok": False, "error": "not found"}, 404)
            return

        try:
            result = self.state.call(
                str(body.get("method") or ""),
                list(body.get("args") or []),
                dict(body.get("kwargs") or {}),
            )
            self._reply({"ok": True, "result": result})
        except Exception as exc:
            log.exception("call %s failed", body.get("method"))
            self._reply({"ok": False, "error": str(exc)}, 500)


    # -- terminal streaming ---------------------------------------------

    def _terminal_socket(self) -> None:
        """Keystrokes in and output out over one WebSocket, output pushed.

        Messages are JSON text frames. Client -> host: {"t":"i","d":text} input,
        {"t":"r","c":cols,"r":rows} resize. Host -> client: {"t":"o","d":text,
        "o":offset} output, {"t":"x"} the process exited.
        """
        query = parse_qs(urlsplit(self.path).query)
        token = (query.get("token") or [""])[0]
        key = (query.get("id") or [""])[0]
        try:
            since = int((query.get("since") or ["0"])[0])
        except ValueError:
            since = 0
        if not secrets.compare_digest(token, self.state.token):
            self._reply({"ok": False, "error": "bad token"}, 403)
            return
        runner = self.state.registry.terminal(key)
        if runner is None:
            self._reply({"ok": False, "error": "no such terminal"}, 404)
            return
        client_key = self.headers.get("Sec-WebSocket-Key")
        if "websocket" not in (self.headers.get("Upgrade") or "").lower() or not client_key:
            self._reply({"ok": False, "error": "websocket upgrade required"}, 400)
            return

        self.close_connection = True
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_key(client_key))
        self.end_headers()
        self.wfile.flush()

        sock = self.connection
        sock.settimeout(None)
        ws = WebSocket(sock)
        registry = self.state.registry

        def inbound() -> None:
            while True:
                text = ws.recv_text()
                if text is None:
                    return
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                kind = msg.get("t")
                if kind == "i":
                    registry.terminal_write(key, str(msg.get("d") or ""))
                elif kind == "r":
                    try:
                        registry.terminal_resize(key, int(msg.get("c")), int(msg.get("r")))
                    except (TypeError, ValueError):
                        pass

        reader = threading.Thread(target=inbound, name=f"queenbee-ws-in-{key}", daemon=True)
        reader.start()
        offset = since
        try:
            while not ws.closed and not self.state.stop.is_set():
                if not runner.wait_output(offset, 1.0):
                    if registry.terminal(key) is None:
                        break
                    continue
                # Let a burst finish so a redraw goes out as one message
                # rather than dozens.
                time.sleep(0.004)
                end, data = runner.read_since(offset)
                if data:
                    ws.send_text(json.dumps({"t": "o", "d": data, "o": end},
                                            separators=(",", ":")))
                offset = end
                if not runner.running and runner.read_since(offset)[0] == offset:
                    ws.send_text('{"t":"x"}')
                    break
                if registry.terminal(key) is None:
                    break
        except Exception:
            log.debug("terminal socket %s ended", key, exc_info=True)
        finally:
            ws.close()


def lock_path():
    return _state_dir() / "host.lock"


def write_token(path, token: str) -> None:
    """Replace the token file in one step, readable by this user only.

    The state directory is under the user's profile, whose default ACL on
    Windows already keeps other accounts out; on POSIX the mode does it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    os.replace(tmp, path)


def serve(port: int = DEFAULT_PORT) -> int:
    lock = InstanceLock(lock_path())
    try:
        lock.acquire()
    except AlreadyRunning:
        print("queenbee host: already running", file=sys.stderr)
        return 3

    state = HostState()
    try:
        write_token(token_path(port), state.api_token)
    except OSError as exc:
        print(f"queenbee host: cannot write its token: {exc}", file=sys.stderr)
        lock.release()
        return 2
    handler = type("BoundHostHandler", (Handler,), {"state": state})
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        print(f"queenbee host: cannot bind 127.0.0.1:{port}: {exc}", file=sys.stderr)
        lock.release()
        return 2

    # The host outlives consoles on purpose, so a Ctrl+C or Ctrl+Break aimed at
    # whatever console it came from must not end every session in it. A host
    # that shared the UI server's console once took Ctrl+C meant for the UI
    # server as a stop, and ended the terminals and the training inside them
    # without a word in the log. Only /shutdown (`--stop`) ends it now.
    for name in ("SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (OSError, ValueError):
                pass
    # A hard crash (out of memory, a fault in a native library) leaves no
    # Python traceback in host.log; write what faulthandler can see instead.
    try:
        crash_log = open(_state_dir() / "host_crash.log", "a", encoding="utf-8")
        crash_log.write(f"--- host {os.getpid()} started {time.ctime()}\n")
        crash_log.flush()
        faulthandler.enable(crash_log)
    except OSError:
        pass
    atexit.register(lambda: log.warning("queenbee host %s exiting", os.getpid()))

    thread = threading.Thread(target=httpd.serve_forever, name="queenbee-host-http", daemon=True)
    thread.start()
    log.info("queenbee host %s listening on 127.0.0.1:%d", os.getpid(), port)
    try:
        state.stop.wait()
    finally:
        # Only an explicit stop reaches here, so ending the sessions is what
        # was asked for.
        log.warning("queenbee host %s stopping: ending %d terminals",
                    os.getpid(), len(state.registry.terminals(None) or []))
        state.registry.shutdown()
        state.gpu.close()
        httpd.shutdown()
        httpd.server_close()
        lock.release()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="queenbee.host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--stop", action="store_true",
                        help="end the host and every agent and terminal it holds")
    parser.add_argument("--spawn", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    # Before anything is spawned: terminals and agents inherit this process's
    # environment, and a host started from inside a Claude Code session would
    # otherwise hand every `claude` that session's identity (see clean_env).
    from .agents.clean_env import scrub_environ

    scrub_environ()

    if args.spawn:
        # Intermediate step: start the real host and exit at once. The host's
        # recorded parent is then a process that no longer exists, so it is
        # not a descendant of the UI server -- and "end process tree" on the
        # UI server, which walks recorded parents, cannot reach it. Before
        # this, killing the UI server's tree killed the host with it.
        from .agents.host_client import spawn_detached

        spawn_detached([sys.executable, "-m", "queenbee.host", "--port", str(args.port)])
        return 0

    if args.status or args.stop:
        from .agents.host_client import HostClient

        client = HostClient(args.port, autostart=False)
        info = client.ping()
        if info is None:
            print("queenbee host: not running")
            return 1
        if args.status:
            print(json.dumps(info, indent=1))
            return 0
        client.shutdown_host()
        print("queenbee host: stopping")
        return 0

    log_dir = _state_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_dir / "host.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return serve(args.port)


if __name__ == "__main__":
    raise SystemExit(main())
