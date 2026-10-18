"""Per-process sampling, rolled up over the process tree a terminal pane owns.

The host tells us a pane's shell PID; everything the agent spawns underneath it
(the agent CLI, a python training script, a local model server) is a
descendant. Attributing the whole subtree to the pane is what turns
machine-wide numbers into "this agent is the one eating the box".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable, Optional

import psutil

from ..model import ProcSample

log = logging.getLogger("queenbee.procs")

# psutil reports a process's CPU as a share of ONE core, so a busy tree on a
# 12-thread machine can read 869%. Next to a machine tile that tops out at 100%
# that looks like a broken number rather than a busy agent. Every per-process
# figure is divided by the logical core count so both speak the same unit: a
# share of the whole machine.
_CORES = max(1, psutil.cpu_count(logical=True) or 1)

# psutil's cpu_percent() measures since the previous call on the same handle.
# Two callers on different schedules (the sampling loop and a UI request) made
# that window anything from milliseconds to seconds, so one reading came back
# 0 and the next 109%. A reading is therefore held for this long and handed to
# every caller in that window, so the measuring interval is always at least this.
_CPU_MIN_WINDOW_S = 2.0
# A handle survives this long after its last use. Pruning to "whatever this tick
# claimed" threw away handles for session and terminal trees every tick, and a
# fresh handle's first reading is always 0.
_HANDLE_TTL_S = 60.0


@dataclass
class _ProcInfo:
    pid: int
    name: str
    cwd: Optional[str]

_MB = 1024 * 1024

# Substrings that mark a process as running a model locally -- i.e. a real
# GPU/VRAM consumer, as opposed to an API-backed agent that mostly waits on
# the network. Matched case-insensitively against the command line.
LOCAL_MODEL_HINTS = (
    "ollama",
    "llama-server",
    "llama_cpp",
    "llama.cpp",
    "llamacpp",
    "vllm",
    "sglang",
    "lmstudio",
    "lm-studio",
    "koboldcpp",
    "text-generation-webui",
    "tabbyapi",
    "exllama",
    "mlx_lm",
    "localai",
    "tgi",
    "triton",
)

# Noise we never want to bill to an agent.
_IGNORED_NAMES = {"conhost.exe", "conpty.exe", "openconsole.exe"}


class ProcessCollector:
    """Samples process trees, keeping psutil handles alive between ticks.

    psutil.Process.cpu_percent() reports usage since the *previous* call on the
    same object, so the handles must be cached or every reading comes back 0.
    """

    def __init__(self, extra_local_hints: Iterable[str] = ()) -> None:
        self._handles: dict[int, psutil.Process] = {}
        self._touched: dict[int, float] = {}
        self._cpu_cache: dict[int, tuple[float, float]] = {}
        self._local_hints = tuple(LOCAL_MODEL_HINTS) + tuple(
            h.lower() for h in extra_local_hints
        )

    def _cpu(self, proc: psutil.Process) -> float:
        """CPU as a share of the machine, over a window of at least two seconds."""
        now = time.monotonic()
        cached = self._cpu_cache.get(proc.pid)
        if cached is not None and now - cached[0] < _CPU_MIN_WINDOW_S:
            return cached[1]
        value = proc.cpu_percent(interval=None) / _CORES
        # The priming call on a brand-new handle always reads 0; do not let it
        # overwrite a real reading from before.
        if cached is None or value > 0 or now - cached[0] >= _CPU_MIN_WINDOW_S * 2:
            self._cpu_cache[proc.pid] = (now, value)
        else:
            value = cached[1]
        return value

    def _handle(self, pid: int) -> Optional[psutil.Process]:
        self._touched[pid] = time.monotonic()
        proc = self._handles.get(pid)
        if proc is not None:
            if proc.is_running():
                return proc
            self._handles.pop(pid, None)
        try:
            proc = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            return None
        self._handles[pid] = proc
        # Prime the CPU counter; this call always returns 0.0.
        try:
            proc.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
        return proc

    def tree_pids(self, root_pid: int) -> list[int]:
        """Root plus every descendant, best effort."""
        proc = self._handle(root_pid)
        if proc is None:
            return []
        pids = [root_pid]
        try:
            pids += [c.pid for c in proc.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return pids

    def sample_tree(
        self,
        root_pid: int,
        gpu_by_pid: Optional[dict[int, tuple[Optional[float], Optional[float]]]] = None,
    ) -> list[ProcSample]:
        return self.sample_pids(self.tree_pids(root_pid), gpu_by_pid)

    def sample_pids(
        self,
        pids: Iterable[int],
        gpu_by_pid: Optional[dict[int, tuple[Optional[float], Optional[float]]]] = None,
    ) -> list[ProcSample]:
        """Sample an explicit set of processes, e.g. a tree plus its orphans."""
        gpu_by_pid = gpu_by_pid or {}
        out: list[ProcSample] = []
        for pid in dict.fromkeys(pids):
            proc = self._handle(pid)
            if proc is None:
                continue
            try:
                with proc.oneshot():
                    name = proc.name()
                    if name.lower() in _IGNORED_NAMES:
                        continue
                    cpu = self._cpu(proc)
                    rss = proc.memory_info().rss / _MB
                    try:
                        cmdline = " ".join(proc.cmdline())
                    except (psutil.AccessDenied, OSError):
                        cmdline = name
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
            vram, gpu_pct = gpu_by_pid.get(pid, (None, None))
            out.append(
                ProcSample(
                    pid=pid,
                    name=name,
                    cmdline=cmdline,
                    cpu_pct=cpu,
                    rss_mb=rss,
                    vram_mb=vram,
                    gpu_pct=gpu_pct,
                )
            )
        return out

    def sample_one(self, pid: int) -> Optional[ProcSample]:
        """Sample a single process that is not part of a pane's tree."""
        proc = self._handle(pid)
        if proc is None:
            return None
        try:
            with proc.oneshot():
                name = proc.name()
                cpu = self._cpu(proc)
                rss = proc.memory_info().rss / _MB
                try:
                    cmdline = " ".join(proc.cmdline())
                except (psutil.AccessDenied, OSError):
                    cmdline = name
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None
        return ProcSample(pid=pid, name=name, cmdline=cmdline, cpu_pct=cpu, rss_mb=rss)

    def describe(self, pid: int) -> Optional[_ProcInfo]:
        """Cheap identity lookup: name and cwd, no resource sampling."""
        proc = self._handle(pid)
        if proc is None:
            return None
        try:
            with proc.oneshot():
                name = proc.name()
                try:
                    cwd = proc.cwd()
                except (psutil.AccessDenied, OSError):
                    # Common on Windows for processes owned by other sessions.
                    cwd = None
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None
        return _ProcInfo(pid=pid, name=name, cwd=cwd)

    def uptime_s(self, pid: int) -> Optional[float]:
        proc = self._handle(pid)
        if proc is None:
            return None
        try:
            return max(0.0, time.time() - proc.create_time())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    def looks_local_model(self, procs: Iterable[ProcSample]) -> bool:
        for proc in procs:
            haystack = f"{proc.name} {proc.cmdline}".lower()
            if any(hint in haystack for hint in self._local_hints):
                return True
        return False

    def prune(self, live_pids: set[int]) -> None:
        """Forget handles that nobody has asked about for a while.

        Keyed on last use, not on the caller's idea of what is live: sessions,
        terminals and panes are sampled by different paths, and dropping
        everything one path did not claim reset the others' readings to zero
        every tick.
        """
        cutoff = time.monotonic() - _HANDLE_TTL_S
        for pid in list(self._handles):
            if pid in live_pids:
                continue
            if self._touched.get(pid, 0) < cutoff:
                self._handles.pop(pid, None)
                self._touched.pop(pid, None)
                self._cpu_cache.pop(pid, None)
