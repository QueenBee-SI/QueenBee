"""Remember who started whom, after the middle of the chain is gone.

An agent that launches a long job usually does it as a background command: a
shell starts the job and exits straight away. The job keeps running, but its
parent is dead, so it is no longer a descendant of the agent -- measured on a
real training run, the chain was `python <- timeout <- timeout <- (gone)`. A
walk of the live process tree finds nothing, and the one process actually
costing CPU and VRAM is billed to nobody.

This keeps a short memory of the parent of every process seen, refreshed four times
a second. Asking "does this process descend from that one" then walks the
remembered chain, so a job stays attributed to the agent that launched it even
after the shell in between has exited.

It is cheap because the whole parent table comes from one call -- about 9 ms
for 300 processes on Windows -- where asking each process for its creation
time costs hundreds of milliseconds and up to several seconds -- so a creation
time is asked for once, when a process is first seen, and never again.

PID reuse is guarded by that creation time. A link from a process to its
remembered parent only counts if the parent was still seen alive when the
child started. Without this, a parent that had died and whose pid Windows then
reused for a shell too short-lived to be swept kept its old ancestry: a
training run launched from one conversation was billed to another,
7.8 GB of VRAM on the wrong row.

The honest limit: a shell that lives for less than one sweep interval is never
seen, and a job it launched cannot be traced through it.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Iterable, Optional

import psutil

log = logging.getLogger("queenbee.lineage")

# 9 ms per sweep, so four a second costs about 4% of one core. A launching
# shell that lives less than this is still missed; at one sweep a second it
# was missed routinely.
SWEEP_S = 0.25
# How long a process's parentage is remembered after it was last seen alive.
MEMORY_S = 15 * 60.0
_MAX_DEPTH = 40


def _parent_table() -> dict[int, int]:
    """pid -> ppid for every process, in one call where the platform allows."""
    fast = getattr(psutil._psplatform, "ppid_map", None)
    if fast is not None:
        try:
            return dict(fast())
        except Exception:
            log.debug("ppid_map failed; falling back", exc_info=True)
    table: dict[int, int] = {}
    for proc in psutil.process_iter(["pid", "ppid"]):
        info = proc.info
        if info.get("pid") is not None:
            table[info["pid"]] = info.get("ppid") or 0
    return table


class Lineage:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # pid -> (ppid, last_seen wall time)
        self._parents: dict[int, tuple[int, float]] = {}
        # pid -> creation time (wall), asked once when a process is first seen
        self._created: dict[int, float] = {}
        self._alive: set[int] = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- sampling --------------------------------------------------------

    def sweep(self) -> None:
        table = _parent_table()
        # Wall time, because it is compared with process creation times.
        now = time.time()
        with self._lock:
            fresh = [pid for pid, ppid in table.items()
                     if pid not in self._alive or self._parents.get(pid, (None,))[0] != ppid]
        created = {}
        for pid in fresh:
            try:
                created[pid] = psutil.Process(pid).create_time()
            except (psutil.Error, OSError):
                pass
        with self._lock:
            for pid, ppid in table.items():
                # A pid new since the last sweep, or with a new parent, is a
                # new process: its creation time replaces the old one's.
                if pid in created:
                    self._created[pid] = created[pid]
                self._parents[pid] = (ppid, now)
            self._alive = set(table)
            cutoff = now - MEMORY_S
            for pid, (_, seen) in list(self._parents.items()):
                if pid not in self._alive and seen < cutoff:
                    del self._parents[pid]
                    self._created.pop(pid, None)

    def start(self) -> None:
        if self._thread is not None:
            return
        self.sweep()

        def loop() -> None:
            while not self._stop.wait(SWEEP_S):
                try:
                    self.sweep()
                except Exception:
                    log.exception("lineage sweep failed")

        self._thread = threading.Thread(target=loop, name="queenbee-lineage", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- questions -------------------------------------------------------

    def descends_from(self, pid: int, root: int) -> bool:
        with self._lock:
            seen = set()
            current = pid
            # Every process on a real path to the root started after it. A
            # step through an older process means the walk has climbed above
            # the root -- e.g. through the host, whose dead launcher's pid was
            # reused by a terminal -- and must not end at it.
            root_born = self._created.get(root)
            for _ in range(_MAX_DEPTH):
                if current == root:
                    return True
                if current in seen or current <= 0:
                    return False
                born = self._created.get(current)
                if root_born is not None and born is not None and born < root_born - 1.0:
                    return False
                seen.add(current)
                entry = self._parents.get(current)
                if entry is None:
                    return False
                parent = entry[0]
                if not self._parent_plausible(current, parent):
                    return False
                current = parent
        return False

    def _parent_plausible(self, child: int, parent: int) -> bool:
        """Could `parent` really have started `child`? (lock held)

        The parent must have been alive when the child was created. A dead
        pid's record lingers for MEMORY_S; if the pid is reused by a process
        that exits before a sweep sees it, its children would otherwise
        inherit the dead process's ancestry.
        """
        born = self._created.get(child)
        if born is None:
            return True               # unknown: keep the old behaviour
        if parent in self._alive:
            started = self._created.get(parent)
            return started is None or started <= born + 1.0
        seen = self._parents.get(parent)
        last_seen = seen[1] if seen else None
        return last_seen is not None and last_seen >= born - 2 * SWEEP_S - 0.5

    def descendants(self, root: int, among: Optional[Iterable[int]] = None) -> set[int]:
        """Live processes whose remembered ancestry reaches `root`.

        Includes processes that have been orphaned since they were started,
        which is the case a live tree walk misses.
        """
        with self._lock:
            candidates = set(among) if among is not None else set(self._alive)
        return {pid for pid in candidates if pid != root and self.descends_from(pid, root)}
