"""Run an agent as a child process and talk to it in structured JSON.

The host solves this by being a terminal emulator: it opens a PTY, runs the
agent's interactive TUI inside it, and draws the character grid. That works
with any CLI ever written, but it means the supervisor never actually knows
what the agent is doing -- state has to be guessed by pattern-matching the
text on screen, which is exactly the part that breaks.

Claude Code offers a structured mode instead:

    claude --print --input-format stream-json --output-format stream-json

Each side exchanges one JSON object per line. No ANSI, no terminal grid, no
screen scraping -- and the conversation renders in QueenBee's own UI rather than
as someone else's TUI. The Claude desktop app drives Claude Code exactly this
way, which is the best evidence the mode is solid.

The trade is generality: this only works for agents that expose a structured
mode. A PTY backend can be added later for the ones that do not, behind the
same `AgentRunner` interface.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("queenbee.agents")

# Permission modes Claude Code accepts. `plan` is the default here on purpose:
# it lets the agent read and propose without writing anything, which is the
# only mode that is safe to start from a dashboard button without a human
# having agreed to anything yet.
PERMISSION_MODES = ("plan", "acceptEdits", "auto", "default")
DEFAULT_PERMISSION_MODE = "plan"

_STOP_GRACE_S = 5.0


def find_claude() -> Optional[str]:
    """Locate the Claude Code binary.

    The desktop app ships its own copy under AppData rather than on PATH, so
    look there too before giving up.
    """
    found = shutil.which("claude")
    if found:
        return found

    roots = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "Claude" / "claude-code")
    roots.append(Path.home() / ".claude" / "local")

    best: Optional[Path] = None
    for root in roots:
        if not root.is_dir():
            continue
        for candidate in root.glob("*/claude.exe" if os.name == "nt" else "*/claude"):
            # Directories are version numbers; newest string order wins.
            if best is None or candidate.parent.name > best.parent.name:
                best = candidate
    return str(best) if best else None


@dataclass
class AgentEvent:
    """One line of the agent's output stream, normalised."""

    kind: str                      # system | assistant | user | result | error
    ts: float
    raw: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ts": self.ts,
            "text": self.text,
            "tools": self.tools,
            "subtype": self.raw.get("subtype"),
        }


def _flatten(content: Any) -> tuple[str, list[str]]:
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "", []
    parts: list[str] = []
    tools: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            tools.append(str(block.get("name", "tool")))
        elif block.get("type") == "tool_result":
            tools.append("result")
    return "\n".join(p for p in parts if p).strip(), tools


class AgentRunner:
    """One agent process, owned by QueenBee."""

    def __init__(
        self,
        cwd: str,
        *,
        binary: Optional[str] = None,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        model: Optional[str] = None,
        resume: Optional[str] = None,
        on_event: Optional[Callable[[AgentEvent], None]] = None,
    ) -> None:
        self.cwd = cwd
        self.binary = binary or find_claude()
        self.permission_mode = (
            permission_mode if permission_mode in PERMISSION_MODES else DEFAULT_PERMISSION_MODE
        )
        self.model = model
        self.resume = resume
        self.on_event = on_event

        self.session_id: Optional[str] = None
        self.started_at: Optional[float] = None
        self.events: list[AgentEvent] = []
        self.error: Optional[str] = None

        self._proc: Optional[subprocess.Popen[str]] = None
        self._lock = threading.Lock()
        self._outbox: queue.Queue[Optional[str]] = queue.Queue()
        self._threads: list[threading.Thread] = []

    # -- lifecycle -------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    def argv(self) -> list[str]:
        args = [
            self.binary or "claude",
            "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            # Without --verbose the stream carries only the final result, which
            # would make the UI sit blank for the whole turn.
            "--verbose",
            "--permission-mode", self.permission_mode,
        ]
        if self.model:
            args += ["--model", self.model]
        if self.resume:
            args += ["--resume", self.resume]
        return args

    def start(self) -> bool:
        if self.running:
            return True
        if not self.binary:
            self.error = "claude binary not found"
            return False
        if not Path(self.cwd).is_dir():
            self.error = f"working directory does not exist: {self.cwd}"
            return False

        try:
            self._proc = subprocess.Popen(
                self.argv(),
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except OSError as exc:
            self.error = f"could not start agent: {exc}"
            return False

        self.started_at = time.time()
        self._spawn(self._pump_stdout, "agent-stdout")
        self._spawn(self._pump_stderr, "agent-stderr")
        self._spawn(self._pump_stdin, "agent-stdin")
        return True

    def _spawn(self, target, name: str) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        """Close stdin first: that is how this mode is meant to end."""
        self._outbox.put(None)
        proc = self._proc
        if proc is None:
            return
        try:
            proc.wait(timeout=_STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=_STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()

    # -- io --------------------------------------------------------------

    def send(self, text: str) -> bool:
        if not self.running or not text.strip():
            return False
        payload = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
        self._outbox.put(json.dumps(payload, ensure_ascii=False))
        self._record(AgentEvent(kind="user", ts=time.time(), text=text))
        return True

    def _pump_stdin(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        while True:
            item = self._outbox.get()
            if item is None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                return
            try:
                proc.stdin.write(item + "\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                return

    def _pump_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._record(self._normalise(obj))
        self._record(AgentEvent(kind="result", ts=time.time(), text="",
                                raw={"subtype": "closed"}))

    def _pump_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        for line in proc.stderr:
            line = line.strip()
            if line:
                log.debug("agent stderr: %s", line[:200])
                self.error = line[:300]

    def _normalise(self, obj: dict[str, Any]) -> AgentEvent:
        kind = str(obj.get("type", "system"))
        sid = obj.get("session_id") or obj.get("sessionId")
        if sid and not self.session_id:
            self.session_id = str(sid)

        text, tools = "", []
        if kind in ("assistant", "user"):
            text, tools = _flatten((obj.get("message") or {}).get("content"))
        elif kind == "result":
            text = str(obj.get("result") or "")

        return AgentEvent(kind=kind, ts=time.time(), raw=obj, text=text, tools=tools)

    def _record(self, event: AgentEvent) -> None:
        with self._lock:
            self.events.append(event)
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                log.exception("event callback failed")

    def snapshot(self, since: int = 0) -> dict[str, Any]:
        with self._lock:
            events = self.events[since:]
            total = len(self.events)
        return {
            "running": self.running,
            "pid": self.pid,
            "cwd": self.cwd,
            "session_id": self.session_id,
            "permission_mode": self.permission_mode,
            "error": self.error,
            "total": total,
            "events": [e.to_dict() for e in events],
        }
