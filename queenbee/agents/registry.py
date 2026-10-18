"""Agents QueenBee started, and the ones it only watches.

QueenBee sees two kinds of session. Ones it launched itself, which it can talk to
over the structured stream, and ones that already existed, which it reads from
their transcripts. Both appear in the same list; only the first accepts input.
"""

from __future__ import annotations

import itertools
import logging
import threading
from typing import Any, Optional

from . import worktrees
from .pty_runner import PtyRunner, available as pty_available
from .runner import DEFAULT_PERMISSION_MODE, AgentRunner, find_claude

log = logging.getLogger("queenbee.agents.registry")

# Agent ids QueenBee can actually drive today.
SUPPORTED_AGENTS = {"claude"}

# Keep a bounded amount of event history per runner in memory. A long session
# is still fully on disk in its transcript; this is just the live tail.
MAX_EVENTS = 4000


def is_gpu_workload_argv(argv: Optional[list[str]]) -> bool:
    """Recognise a QueenBee-managed GPU worker before it produces output.

    Resource sampling and output heuristics arrive a poll or two after a PTY
    opens.  Marking the launcher command here prevents the UI from briefly
    presenting that worker as a new chat and then making it appear to vanish
    when it is folded under its owning agent.
    """
    parts = [str(part).lower() for part in (argv or [])]
    return (
        "-m" in parts
        and "queenbee.gpu" in parts
        and "run" in parts
        and "--cooperative" in parts
    )


class Registry:
    def __init__(self) -> None:
        self._runners: dict[str, AgentRunner] = {}
        self._worktrees: dict[str, dict[str, Any]] = {}
        self._ptys: dict[str, PtyRunner] = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    def available(self) -> bool:
        return find_claude() is not None

    def start(
        self,
        cwd: str,
        *,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        model: Optional[str] = None,
        agent: str = "claude",
        isolate: bool = False,
        label: str = "",
        resume: Optional[str] = None,
    ) -> dict[str, Any]:
        # Only agents with a structured-stream adapter can be driven. Accepting
        # an id we cannot run and starting Claude instead would be worse than
        # refusing: the session would be labelled as something it is not.
        if agent not in SUPPORTED_AGENTS:
            return {"ok": False, "error": f"no adapter for {agent} yet"}

        # Isolate first, then launch into the isolated copy. Two agents on one
        # branch overwrite each other silently, which is the failure this
        # avoids; a directory that is not a repository just runs in place.
        tree = None
        workdir = cwd
        # Resuming continues an existing conversation, which is tied to the
        # directory it was held in. Moving it into a fresh worktree would
        # silently change the files underneath it mid-thread.
        if isolate and resume:
            isolate = False
        if isolate:
            result = worktrees.create(cwd, label or "agent")
            if result.get("ok"):
                tree = result
                workdir = result["path"]
            elif not result.get("fallback"):
                return {"ok": False, "error": result.get("reason", "worktree failed")}

        runner = AgentRunner(workdir, permission_mode=permission_mode, model=model,
                             resume=resume)
        if not runner.start():
            # Do not leave an empty worktree behind for an agent that never ran.
            if tree:
                worktrees.remove(tree["path"], force=True)
            return {"ok": False, "error": runner.error or "could not start"}

        key = f"queenbee-{next(self._ids)}"
        with self._lock:
            self._runners[key] = runner
            if tree:
                self._worktrees[key] = tree
        log.info("started agent %s in %s (%s)", key, workdir, permission_mode)
        return {"ok": True, "id": key, "worktree": tree, **runner.snapshot()}

    def get(self, key: str) -> Optional[AgentRunner]:
        with self._lock:
            return self._runners.get(key)

    def send(self, key: str, text: str) -> dict[str, Any]:
        runner = self.get(key)
        if runner is None:
            return {"ok": False, "error": "no such agent"}
        if not runner.running:
            return {"ok": False, "error": "agent has exited"}
        if not runner.send(text):
            return {"ok": False, "error": "message not accepted"}
        return {"ok": True}

    def stop(self, key: str, *, drop_worktree: bool = False) -> dict[str, Any]:
        runner = self.get(key)
        if runner is None:
            return {"ok": False, "error": "no such agent"}
        runner.stop()

        # The worktree outlives the agent by default. Whatever it wrote is the
        # user's work, on its own branch, and deleting it on exit would throw
        # away the output of the run that just finished.
        tree = self._worktrees.get(key)
        if tree and drop_worktree:
            result = worktrees.remove(tree["path"])
            if not result.get("ok"):
                return {"ok": True, "worktree_kept": result.get("error")}
            self._worktrees.pop(key, None)
        return {"ok": True}

    def worktree_for(self, key: str) -> Optional[dict[str, Any]]:
        return self._worktrees.get(key)

    def events(self, key: str, since: int = 0) -> dict[str, Any]:
        runner = self.get(key)
        if runner is None:
            return {"ok": False, "error": "no such agent"}
        return {"ok": True, "id": key, **runner.snapshot(since)}


    # -- terminals -------------------------------------------------------

    def pty_supported(self) -> dict[str, Any]:
        ok, why = pty_available()
        return {"ok": ok, "reason": why}

    def open_terminal(
        self,
        cwd: str,
        argv: Optional[list[str]] = None,
        *,
        cols: int = 120,
        rows: int = 32,
    ) -> dict[str, Any]:
        """Run an agent the ordinary way, in a real terminal.

        Separate from `start`: a terminal session is watched rather than
        driven, so it has no structured stream to read state from. It exists
        for what the stream cannot do -- signing in, and agents that offer no
        structured mode at all.
        """
        runner = PtyRunner(cwd, argv, cols=cols, rows=rows,
                           background=is_gpu_workload_argv(argv))
        if not runner.start():
            return {"ok": False, "error": runner.error or "could not open a terminal"}
        key = f"term-{next(self._ids)}"
        with self._lock:
            self._ptys[key] = runner
        log.info("terminal %s in %s (%s)", key, cwd, " ".join(runner.argv))
        return {
            "ok": True,
            "id": key,
            **runner.snapshot(),
            "gpu_workload": is_gpu_workload_argv(runner.argv),
        }

    def terminal(self, key: str) -> Optional[PtyRunner]:
        with self._lock:
            return self._ptys.get(key)

    def terminal_read(self, key: str, since: int = 0) -> dict[str, Any]:
        runner = self.terminal(key)
        if runner is None:
            return {"ok": False, "error": "no such terminal"}
        offset, data = runner.read_since(since)
        return {"ok": True, "id": key, "offset": offset, "data": data,
                "running": runner.running}

    def terminal_write(self, key: str, data: str) -> dict[str, Any]:
        runner = self.terminal(key)
        if runner is None:
            return {"ok": False, "error": "no such terminal"}
        if not runner.write(data):
            return {"ok": False, "error": "terminal has exited"}
        return {"ok": True}

    def terminal_resize(self, key: str, cols: int, rows: int) -> dict[str, Any]:
        runner = self.terminal(key)
        if runner is None:
            return {"ok": False, "error": "no such terminal"}
        runner.resize(cols, rows)
        return {"ok": True}

    def terminal_close(self, key: str) -> dict[str, Any]:
        runner = self.terminal(key)
        if runner is None:
            return {"ok": False, "error": "no such terminal"}
        runner.stop()
        with self._lock:
            self._ptys.pop(key, None)
        return {"ok": True}

    def terminals(self, sampler: Any = None) -> list[dict[str, Any]]:
        """Open terminals, with their resource cost when a sampler is given.

        A terminal holds a process tree exactly like a chat does, so leaving
        its figures blank made the rail look like terminals were free.
        """
        with self._lock:
            items = list(self._ptys.items())
        out = []
        for key, runner in items:
            row = {
                "id": key,
                **runner.snapshot(),
                "gpu_workload": is_gpu_workload_argv(runner.argv),
            }
            if sampler is not None and runner.pid:
                try:
                    procs = sampler(runner.pid)
                    row["cpu_pct"] = sum(p.cpu_pct for p in procs)
                    row["rss_mb"] = sum(p.rss_mb for p in procs)
                    vram = [p.vram_mb for p in procs if p.vram_mb is not None]
                    row["vram_mb"] = sum(vram) if vram else None
                    gpu = [p.gpu_pct for p in procs if p.gpu_pct is not None]
                    row["gpu_pct"] = sum(gpu) if gpu else None
                except Exception:
                    log.debug("terminal sampling failed", exc_info=True)
            out.append(row)
        return out

    def listing(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._runners.items())
        out = []
        for key, runner in items:
            out.append({
                "id": key,
                "cwd": runner.cwd,
                "worktree": self._worktrees.get(key),
                "running": runner.running,
                "pid": runner.pid,
                "session_id": runner.session_id,
                "permission_mode": runner.permission_mode,
                "started_at": runner.started_at,
                "error": runner.error,
            })
        return out

    def shutdown(self) -> None:
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            try:
                runner.stop()
            except Exception:
                log.exception("stopping an agent failed")
        with self._lock:
            terminals = list(self._ptys.values())
        for term in terminals:
            try:
                term.stop()
            except Exception:
                log.exception("stopping a terminal failed")
