"""A Python that Codex's sandbox can run.

On Windows the Python many people have comes from the Microsoft Store and lives in a protected folder; the sandbox
Codex runs its commands in is another Windows user and cannot start it ("python is not recognized"), so a Codex
worker can write code but never run it or its tests. A plain copy of that interpreter in an ordinary folder works.
This makes the copy once and says where it is.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path


log = logging.getLogger(__name__)

# (the Appx* package files stay: without them this copy of the Store Python does not start)
_JUNK = ("__pycache__", "idlelib", "turtledemo", "test", "tests", "Doc", "AppxManifest.xml", "VFS")


def _real_prefix() -> str:
    """The folder of the Python the user's `python` command starts."""
    exe = shutil.which("python") or ""
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "-c", "import sys; print(sys.base_prefix)"], capture_output=True, text=True,
                             timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.stdout or "").strip()


def portable_python() -> str:
    """Path of a Python.exe that runs inside Codex's sandbox, or "" when there is none to offer."""
    if os.name != "nt":
        return ""
    target = Path.home() / ".queenbee" / "python"          # (a folder under AppData\Local does not start it)
    exe = target / "python.exe"
    if exe.is_file():
        return str(exe)
    prefix = _real_prefix()
    if not prefix or not Path(prefix, "python.exe").is_file():
        return ""
    try:
        shutil.copytree(prefix, target, ignore=shutil.ignore_patterns(*_JUNK), dirs_exist_ok=True)
        ok = subprocess.run([str(exe), "-c", "import sqlite3, unittest, json"], capture_output=True, timeout=60,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode == 0
    except (OSError, shutil.Error, subprocess.SubprocessError):
        log.warning("could not make a portable Python for Codex", exc_info=True)
        return ""
    return str(exe) if ok else ""
