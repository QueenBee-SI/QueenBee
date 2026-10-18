"""Branch-per-worker projects: the queen is the master of `main`.

    root/                 the project, `main` checked out -- only the queen merges into it
    root/.queenbee_wt/P2/    worker P2's own worktree on branch `queen/P2`

A worker's mistake stays on its branch. When a worker reports done and its
acceptance checks pass, the queen commits its work, merges the branch into
`main`, runs the package's verify command, and keeps the merge only if that
passes; otherwise the merge is undone and the failure goes back to the
worker, who first brings `main` into its branch. Whenever `main` moves, the
workers still at work are told. Everything is plain git; no model is called.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Callable, Optional

IDENT = ["-c", "user.name=QueenBee", "-c", "user.email=queen@queenbee.local", "-c", "commit.gpgsign=false"]
EXCLUDE = [".queenbee_wt/", ".queenbee/"]


def git(cwd: str, *args: str, timeout: float = 120) -> tuple[int, str]:
    proc = subprocess.run(["git", *IDENT, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _exclude(root: str) -> None:
    code, path = git(root, "rev-parse", "--git-path", "info/exclude")
    if code:
        return
    path = path if os.path.isabs(path) else os.path.join(root, path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        have = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        with open(path, "a", encoding="utf-8") as fh:
            for line in EXCLUDE:
                if line not in have:
                    fh.write(line + "\n")
    except OSError:
        pass


def prepare(root: str) -> Optional[str]:
    """Make `root` ready to be a master repository. None when it is, otherwise why not."""
    if not os.path.isdir(root):
        return "the project folder does not exist"
    code, top = git(root, "rev-parse", "--show-toplevel")
    if code:
        if git(root, "init", "-q")[0]:
            return "git is not available"
        git(root, "checkout", "-q", "-b", "main")
        _exclude(root)
        git(root, "add", "-A")
        git(root, "commit", "-q", "--allow-empty", "-m", "Project as the queen found it")
        return None
    _exclude(root)
    code, dirty = git(root, "status", "--porcelain")
    if dirty.strip():
        return "the project has uncommitted changes; commit or stash them first"
    return None


def main_branch(root: str) -> str:
    return git(root, "rev-parse", "--abbrev-ref", "HEAD")[1] or "main"


def head(root: str) -> str:
    return git(root, "rev-parse", "--short", "HEAD")[1]


def add_worktree(root: str, priority: int) -> str:
    """The worktree path of worker W<priority>, on a new branch from `main`."""
    path = os.path.join(root, ".queenbee_wt", f"W{priority}")
    if os.path.isdir(path):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    code, out = git(root, "worktree", "add", "-q", "-b", f"queen/W{priority}", path, "HEAD")
    if code:
        raise RuntimeError(f"could not give W{priority} a branch: {out[-300:]}")
    return path


def commit_all(worktree: str, message: str) -> bool:
    """Commit what the worker left; True when there was something."""
    git(worktree, "add", "-A")
    if not git(worktree, "status", "--porcelain")[1].strip():
        return False
    return git(worktree, "commit", "-q", "-m", message)[0] == 0


def merge(root: str, priority: int, title: str,
          verify: Optional[Callable[[], tuple[bool, str]]] = None) -> tuple[bool, str]:
    """Merge branch queen/W<n> into main and verify; undo on any failure.
    Returns (kept, why not / what happened)."""
    before = head(root)
    code, out = git(root, "merge", "--no-ff", "-m", f"Merge W{priority}: {title[:60]}", f"queen/W{priority}")
    if code:
        git(root, "merge", "--abort")
        files = git(root, "diff", "--name-only", "--diff-filter=U")[1]
        return False, ("merge conflict with main" + (f" in {files.replace(chr(10), ', ')}" if files else "")
                       + ": " + out[-300:])
    if verify is not None:
        ok, detail = verify()
        if not ok:
            git(root, "reset", "-q", "--hard", before)
            return False, "the package's check fails with this merged in: " + detail[-600:]
    return True, f"merged into main at {head(root)}"


def remove_worktree(root: str, priority: int) -> None:
    path = os.path.join(root, ".queenbee_wt", f"W{priority}")
    if os.path.isdir(path):
        if git(root, "worktree", "remove", "--force", path)[0]:
            shutil.rmtree(path, ignore_errors=True)
            git(root, "worktree", "prune")
