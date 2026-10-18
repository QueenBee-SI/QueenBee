"""Git worktrees, so two agents in one repo stop overwriting each other.

QueenBee launches agents straight into a project directory. That is fine for one
agent and quietly destructive for two: both edit the same files on the same
branch, and whoever writes last wins. The usual answer is to give each agent
worktrees, and the reason is the same for us.

A worktree is a second checkout of the same repository, on its own branch,
sharing one object store. So each agent gets real files it can edit freely
while the history stays single.

There is a second gain that is specific to QueenBee: an agent in its own
directory makes ownership unambiguous. Detached work that today lands in
"unattributed GPU" because two sessions share a path becomes attributable, since
the path now identifies exactly one agent.

Nothing here is destructive by default. Removing a worktree with uncommitted
changes is refused unless explicitly forced -- an agent's unfinished work is
still the user's work.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("queenbee.worktrees")

_TIMEOUT_S = 30.0
# Where worktrees live by default: beside the repo, not inside it, so they are
# never picked up by the repo's own tooling or committed by accident.
_DIRNAME = ".queenbee-worktrees"


@dataclass
class Worktree:
    path: str
    branch: str
    repo: str
    created: float

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _git(args: list[str], cwd: str) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            timeout=_TIMEOUT_S,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return 127, "", "git is not installed"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def repo_root(path: str) -> Optional[str]:
    """Top level of whatever checkout `path` is in -- worktree or main."""
    code, out, _ = _git(["rev-parse", "--show-toplevel"], path)
    return out if code == 0 and out else None


def main_root(path: str) -> Optional[str]:
    """Top level of the *main* checkout, even when called inside a worktree.

    `--show-toplevel` answers "which checkout am I in", which inside a worktree
    is the worktree itself -- and `git worktree remove` cannot run from within
    the worktree it is deleting. `--git-common-dir` points at the shared .git
    directory, whose parent is the main checkout, which is where the command
    has to run from.
    """
    code, out, _ = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], path)
    if code != 0 or not out:
        return repo_root(path)
    common = Path(out)
    # A bare repo has no working tree to step back into.
    return str(common.parent) if common.name == ".git" else repo_root(path)


def is_dirty(path: str) -> bool:
    code, out, _ = _git(["status", "--porcelain"], path)
    return code == 0 and bool(out)


def current_branch(path: str) -> Optional[str]:
    code, out, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], path)
    return out if code == 0 and out else None


def slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-").lower()
    return slug[:40] or "agent"


def _unique_branch(repo: str, base: str) -> str:
    """A branch name nothing else is using."""
    candidate = base
    n = 2
    while True:
        code, _, _ = _git(["rev-parse", "--verify", "--quiet", candidate], repo)
        if code != 0:
            return candidate
        candidate = f"{base}-{n}"
        n += 1


def create(cwd: str, label: str = "", base: Optional[str] = None) -> dict[str, Any]:
    """Make a worktree for an agent to work in.

    Returns the path to use as the agent's working directory, or an error the
    caller can show. A directory that is not a git repository is not an error
    worth blocking on -- the agent simply runs where it was going to anyway.
    """
    repo = repo_root(cwd)
    if repo is None:
        return {"ok": False, "reason": "not a git repository", "path": cwd, "fallback": True}

    branch = _unique_branch(repo, f"queenbee/{slugify(label) or 'agent'}")
    root = Path(repo).parent / _DIRNAME
    target = root / branch.replace("/", "-")

    args = ["worktree", "add", "-b", branch, str(target)]
    if base:
        args.append(base)
    code, _, err = _git(args, repo)
    if code != 0:
        log.warning("worktree add failed: %s", err)
        return {"ok": False, "reason": err or "git worktree add failed",
                "path": cwd, "fallback": True}

    log.info("worktree %s on %s", target, branch)
    return {
        "ok": True,
        "path": str(target),
        "branch": branch,
        "repo": repo,
        "created": time.time(),
    }


def remove(path: str, *, force: bool = False) -> dict[str, Any]:
    """Drop a worktree.

    Refuses while it holds uncommitted changes unless forced: an agent's
    half-finished work is still work, and a dashboard button should not be able
    to throw it away by accident.
    """
    repo = main_root(path)
    if repo is None:
        return {"ok": False, "error": "not a git worktree"}
    if not force and is_dirty(path):
        return {"ok": False, "error": "worktree has uncommitted changes",
                "dirty": True}

    branch = current_branch(path)

    args = ["worktree", "remove", path]
    if force:
        args.insert(2, "--force")
    code, _, err = _git(args, repo)
    if code != 0:
        return {"ok": False, "error": err or "git worktree remove failed"}

    dropped = _drop_empty_branch(repo, branch)
    _prune_container(repo)
    return {"ok": True, "branch_deleted": dropped}


def _drop_empty_branch(repo: str, branch: Optional[str]) -> bool:
    """Delete the agent's branch only if it never committed anything.

    A branch that holds commits is the output of the run and is kept, on its
    own name, for the user to merge or discard. A branch that is still exactly
    where it started is just litter from a session that produced nothing.
    """
    if not branch or not branch.startswith("queenbee/"):
        return False
    # -d refuses to delete anything not already merged, so an unmerged branch
    # with real commits survives this call by construction.
    code, _, _ = _git(["branch", "-d", branch], repo)
    return code == 0


def _prune_container(repo: str) -> None:
    container = Path(repo).parent / _DIRNAME
    try:
        if container.is_dir() and not any(container.iterdir()):
            container.rmdir()
    except OSError:
        pass


def listing(cwd: str) -> list[dict[str, Any]]:
    """Worktrees of the repository containing `cwd`, QueenBee's own marked."""
    repo = main_root(cwd)
    if repo is None:
        return []
    code, out, _ = _git(["worktree", "list", "--porcelain"], repo)
    if code != 0:
        return []

    trees: list[dict[str, Any]] = []
    entry: dict[str, Any] = {}
    for line in out.splitlines() + [""]:
        if not line:
            if entry.get("path"):
                entry["ours"] = str(entry.get("branch", "")).startswith("refs/heads/queenbee/")
                trees.append(entry)
            entry = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            entry["path"] = value
        elif key == "branch":
            entry["branch"] = value
        elif key == "detached":
            entry["branch"] = "(detached)"
    return trees
