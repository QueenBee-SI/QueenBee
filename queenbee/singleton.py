"""Single-instance guard.

Two QueenBee daemons on one machine is not a harmless duplicate: both publish
under source "queenbee", so their metadata reports race on the host's sequence
number and whichever loses is silently discarded; both append to the same
JSONL, so the history interleaves two different sampling intervals; and both
pay the full subprocess cost of sampling. It happens by accident constantly --
a daemon left running in a terminal pane, then another started from a terminal.

An OS-level byte-range lock is used rather than a pidfile, because the kernel
releases it when the process exits *however* it exits. A pidfile left behind by
a hard kill would block every later start, and validating it by inspecting the
recorded process is unreliable on Windows, where reading another process's
command line is often denied outright.

Layout matters here: byte 0 is the lock byte and is never written, and the PID
is recorded from byte 1 on. Truncating or rewriting the locked byte range drops
the lock on Windows -- which made an earlier version hand the lock to a second
daemon and then fail on the write instead of refusing to start.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("queenbee.singleton")

_PID_OFFSET = 1
_PID_WIDTH = 16


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            # Without somewhere to put the lock, run unguarded rather than
            # refusing to start at all.
            log.debug("cannot open lock file; running without a guard")
            return

        if not _try_lock(fd):
            os.close(fd)
            raise AlreadyRunning(f"another QueenBee daemon holds {self.path}")

        try:
            os.lseek(fd, _PID_OFFSET, os.SEEK_SET)
            os.write(fd, str(os.getpid()).encode().ljust(_PID_WIDTH))
        except OSError:
            pass
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            _unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


if os.name == "nt":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def read_holder(path: str | os.PathLike[str]) -> Optional[int]:
    """PID recorded in the lock file, for a useful error message."""
    try:
        with open(path, "rb") as fh:
            fh.seek(_PID_OFFSET)
            return int(fh.read(_PID_WIDTH).strip())
    except (OSError, ValueError):
        return None
