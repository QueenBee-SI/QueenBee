"""A real terminal, for the things a structured stream cannot do.

The structured runner is better at everything it covers: exact state, no screen
scraping, the conversation rendered in QueenBee's own UI. But it only covers
agents that offer a structured mode, and it has no screen -- so signing in,
which is a menu and a browser round trip, cannot happen through it.

This runs the agent the ordinary way instead: in a pseudo-terminal, with its
own TUI, exactly as it behaves in a terminal window. The bytes are streamed to
a terminal emulator in the browser.

The two are not alternatives. A session can be read through the structured
stream and *watched* through a terminal; resource attribution and
transcript-based state work the same either way, because neither depends on
how the agent's output is displayed.

Windows uses ConPTY through `pywinpty`; POSIX uses the stdlib `pty`.

The PTY is owned by the process that runs this -- normally the QueenBee host
(`queenbee/host.py`), which outlives the UI server, so terminals survive UI
restarts. Output is pushed to the page over a WebSocket as it arrives
(`wait_output`), not polled.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from collections import deque
from typing import Any, Optional

log = logging.getLogger("queenbee.agents.pty")

# Bytes of scrollback kept per terminal. Enough to scroll back through a long
# tool run without letting a chatty agent grow without bound.
BUFFER_BYTES = 512 * 1024
DEFAULT_COLS, DEFAULT_ROWS = 120, 32


class PtyUnavailable(RuntimeError):
    pass


def _utf8_safe_len(data: bytes) -> int:
    """How many leading bytes of `data` end on a whole UTF-8 character.

    Walks back at most 3 bytes (the longest possible continuation run) to
    find the lead byte of a trailing sequence and checks whether all of its
    continuation bytes are actually present yet.
    """
    n = len(data)
    if not n:
        return 0
    limit = max(0, n - 3)
    i = n - 1
    while i >= limit:
        b = data[i]
        if b & 0xC0 != 0x80:  # a lead byte (ASCII or a multi-byte sequence start)
            if b & 0x80 == 0:
                seq_len = 1
            elif b & 0xE0 == 0xC0:
                seq_len = 2
            elif b & 0xF0 == 0xE0:
                seq_len = 3
            elif b & 0xF8 == 0xF0:
                seq_len = 4
            else:
                return n  # not a valid UTF-8 lead byte; let "replace" handle it
            return i if n - i < seq_len else n
        i -= 1
    return n  # three trailing continuation bytes with no lead byte in range: not
    # a pending multi-byte character (max sequence is 4 bytes) -- leave as-is.


def terminal_environ(env: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Return an environment suitable for an interactive terminal.

    QueenBee itself is often launched by an agent or a desktop wrapper whose
    standard streams are not terminals.  Those parents commonly export
    ``TERM=dumb``.  Passing that value into ConPTY makes interactive programs
    such as Codex correctly conclude that they cannot draw a TUI, even though
    QueenBee has given them a real terminal.

    Preserve the caller's environment, but repair values that explicitly say
    no terminal is available.  A deliberate, usable TERM value is left alone.
    """
    result = dict(os.environ if env is None else env)
    if result.get("TERM", "").strip().lower() in {"", "dumb", "unknown"}:
        result["TERM"] = "xterm-256color"
    if not result.get("COLORTERM", "").strip():
        result["COLORTERM"] = "truecolor"
    return result


def available() -> tuple[bool, str]:
    """Whether a PTY can be opened here, and why not if it cannot."""
    if os.name == "nt":
        try:
            import winpty  # noqa: F401
        except ImportError:
            return False, "pywinpty is not installed (pip install pywinpty)"
        return True, ""
    try:
        import pty  # noqa: F401
    except ImportError:
        return False, "this platform has no pty module"
    return True, ""


class PtyRunner:
    """One agent in a pseudo-terminal."""

    def __init__(
        self,
        cwd: str,
        argv: Optional[list[str]] = None,
        *,
        cols: int = DEFAULT_COLS,
        rows: int = DEFAULT_ROWS,
        env: Optional[dict[str, str]] = None,
        background: bool = False,
    ) -> None:
        self.cwd = cwd
        self.argv = argv or ["claude"]
        self.cols = cols
        self.rows = rows
        self.env = env
        # Nobody types into a background worker; see start() for why that
        # decides the Windows backend.
        self.background = background
        self.error: Optional[str] = None
        self.started_at: Optional[float] = None

        self._proc: Any = None
        self._chunks: deque[bytes] = deque()
        self._size = 0
        # Monotonic byte offset, so a client can ask for "everything after N"
        # and survive the buffer being trimmed from the front.
        self._offset = 0
        self._lock = threading.Lock()
        # Signalled on new output and on exit, so a streaming reader waits
        # instead of polling.
        self._changed = threading.Condition(self._lock)
        self._reader: Optional[threading.Thread] = None
        self._closed = False
        self._exited = False

    # -- lifecycle -------------------------------------------------------

    @property
    def running(self) -> bool:
        if self._proc is None:
            return False
        try:
            return bool(self._proc.isalive())
        except Exception:
            return False

    @property
    def pid(self) -> Optional[int]:
        try:
            return int(self._proc.pid) if self._proc else None
        except Exception:
            return None

    def start(self) -> bool:
        ok, why = available()
        if not ok:
            self.error = why
            return False
        if not os.path.isdir(self.cwd):
            self.error = f"working directory does not exist: {self.cwd}"
            return False

        binary = shutil.which(self.argv[0]) or self.argv[0]
        argv = [binary, *self.argv[1:]]
        env = terminal_environ(self.env)

        try:
            if os.name == "nt":
                from winpty import PtyProcess
                from winpty.enums import Backend

                self._proc = PtyProcess.spawn(
                    argv, cwd=self.cwd, env=env,
                    dimensions=(self.rows, self.cols),
                    # ConPTY delegates its bundled OpenConsole.exe to the
                    # user's default terminal on recent Windows builds.  When
                    # that default is Windows Terminal, every background GPU
                    # worker appears as an unwanted black tab/window.  The
                    # WinPTY backend stays headless, so background workers
                    # keep using it.
                    #
                    # It is not usable for anything a person types into,
                    # though: winpty's agent scrapes the console and, from the
                    # console-less host, intermittently holds a keystroke's
                    # echo until the *next* keystroke arrives -- measured 17 of
                    # 38 keys stalled 3 s+ in cmd.exe, occasional ones in
                    # PowerShell, 0 of 38 with ConPTY. That read as the
                    # terminal freezing mid-command.
                    backend=Backend.WinPTY if self.background else Backend.ConPTY,
                )
            else:
                from ptyprocess import PtyProcessUnicode  # type: ignore

                self._proc = PtyProcessUnicode.spawn(
                    argv, cwd=self.cwd, env=env,
                    dimensions=(self.rows, self.cols),
                )
        except Exception as exc:
            self.error = f"could not start a terminal: {exc}"
            log.warning("pty spawn failed: %s", exc)
            return False

        self.started_at = time.time()
        self._reader = threading.Thread(target=self._pump, name="queenbee-pty", daemon=True)
        self._reader.start()
        return True

    def _pump(self) -> None:
        while not self._closed:
            try:
                data = self._proc.read(4096)
            except EOFError:
                break
            except Exception:
                break
            if not data:
                if not self.running:
                    break
                continue
            raw = data.encode("utf-8", "replace") if isinstance(data, str) else data
            with self._lock:
                self._chunks.append(raw)
                self._size += len(raw)
                self._offset += len(raw)
                # Trim from the front; the offset keeps client positions valid.
                while self._size > BUFFER_BYTES and len(self._chunks) > 1:
                    self._size -= len(self._chunks.popleft())
                self._changed.notify_all()
        with self._lock:
            self._exited = True
            self._changed.notify_all()

    def wait_output(self, since: int, timeout: float) -> bool:
        """Block until there is output after `since` or the process ended.

        Returns True when there is something to read (or the terminal is
        finished), False on timeout.
        """
        with self._lock:
            if self._offset > since or self._exited:
                return True
            self._changed.wait(timeout)
            return self._offset > since or self._exited

    # -- io --------------------------------------------------------------

    def write(self, data: str) -> bool:
        if not self.running:
            return False
        try:
            self._proc.write(data)
            return True
        except Exception as exc:
            log.debug("pty write failed: %s", exc)
            return False

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = max(20, cols), max(5, rows)
        if not self.running:
            return
        try:
            self._proc.setwinsize(self.rows, self.cols)
        except Exception as exc:
            log.debug("pty resize failed: %s", exc)

    def read_since(self, since: int) -> tuple[int, str]:
        """Output after byte offset `since`, plus the new offset.

        A client that has fallen behind the trimmed window is sent whatever is
        still buffered rather than nothing -- losing scrollback is better than
        a blank terminal.
        """
        with self._lock:
            end = self._offset
            buffered = b"".join(self._chunks)
            start = end - len(buffered)
            if since < start:
                since = start
            chunk = buffered[since - start:] if end > since else b""
            exited = self._exited
        if not exited:
            # The PTY hands over whatever the OS happened to flush, with no
            # regard for character boundaries, so a multi-byte UTF-8 character
            # (Turkish i/s/g/u/o/c with diacritics, among others) can end up
            # split across two reads. decode(..., "replace") then turns each
            # half into its own mangled replacement character -- exactly the
            # per-keystroke corruption seen while typing non-ASCII text. Hold
            # back a trailing incomplete sequence; the next read (same offset
            # onward) sees it whole once the rest of its bytes have arrived.
            safe_len = _utf8_safe_len(chunk)
            if safe_len < len(chunk):
                end -= len(chunk) - safe_len
                chunk = chunk[:safe_len]
        return end, chunk.decode("utf-8", "replace")

    def stop(self) -> None:
        self._closed = True
        if self._proc is None:
            return
        try:
            self._proc.terminate(force=True)
        except Exception:
            pass

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "pid": self.pid,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "cols": self.cols,
            "rows": self.rows,
            "offset": self._offset,
            "error": self.error,
        }
