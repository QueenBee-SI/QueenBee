"""What the workers changed in a project folder: a snapshot of its text files taken when a plan starts, and a
unified diff of the folder against it at any time. No git is needed, so it also works in a folder that is not a
repository. Binary and big files are listed as changed but not shown."""

from __future__ import annotations

import difflib
import gzip
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".queenbee", "dist", "build",
             ".mypy_cache", ".pytest_cache", ".idea", ".vscode"}
MAX_FILE = 200_000            # bytes of a text file kept in the snapshot
MAX_FILES = 3000
MAX_DIFF_LINES = 400          # lines of one file's diff sent to the window
MAX_SHOWN = 80                # files listed with their diff

BASELINE = Path(".queenbee") / "baseline.json.gz"


OPAQUE = "\0opaque:"


def _read(path: str) -> str:
    """The text of a file; for a binary or too big file a marker with its hash, so that a change still shows."""
    try:
        digest = hashlib.sha1()
        with open(path, "rb") as fh:
            head = fh.read(MAX_FILE + 1)
            digest.update(head)
            if len(head) > MAX_FILE or b"\0" in head:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
                return OPAQUE + digest.hexdigest()
    except OSError:
        return OPAQUE + "unreadable"
    return head.decode("utf-8", errors="replace")


def snapshot(root: str) -> dict[str, Any]:
    """{relative path: text, or an OPAQUE marker for a file that is binary or too big} for the files under `root`."""
    out: dict[str, Any] = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for name in sorted(files):
            if len(out) >= MAX_FILES:
                return out
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if name.endswith((".pyc", ".pyo")):
                continue
            out[rel] = _read(full)
    return out


def baseline_path(root: str) -> str:
    return str(Path(root) / BASELINE)


def save_baseline(root: str, started_at: float = 0.0) -> bool:
    """Take the snapshot for a plan that starts now, unless this plan already has one (a plan picked up again after
    the window was closed keeps its first snapshot). True when a snapshot was written."""
    path = baseline_path(root)
    try:
        if os.path.exists(path) and os.path.getmtime(path) >= started_at:
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {"taken": time.time(), "files": snapshot(root)}
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def load_baseline(root: str) -> dict[str, Any] | None:
    try:
        with gzip.open(baseline_path(root), "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _diff(old: str, new: str, name: str) -> tuple[int, int, list[str]]:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{name}", f"b/{name}", lineterm="", n=2))
    added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + [f"... {len(lines) - MAX_DIFF_LINES} more lines not shown"]
    return added, removed, lines


def compare(root: str) -> dict[str, Any]:
    """The folder now against its snapshot: {"ok", "taken", "files": [{path, status, added, removed, diff}],
    "total"}. status is "added", "modified" or "deleted"."""
    base = load_baseline(root)
    if base is None:
        return {"ok": True, "taken": 0, "files": [], "total": 0, "note": "No snapshot yet: it is taken when a plan starts."}
    before = base.get("files") or {}
    now = snapshot(root)
    rows = []
    for rel in sorted(set(before) | set(now)):
        if rel in before and rel in now:
            if before[rel] == now[rel]:
                continue
            old, new, status = before[rel], now[rel], "modified"
        elif rel in now:
            old, new, status = "", now[rel], "added"
        else:
            old, new, status = before[rel], "", "deleted"
        if old.startswith(OPAQUE) or new.startswith(OPAQUE):
            rows.append({"path": rel, "status": status, "added": 0, "removed": 0, "diff": [], "binary": True})
            continue
        added, removed, lines = _diff(old, new, rel)
        rows.append({"path": rel, "status": status, "added": added, "removed": removed, "diff": lines})
    return {"ok": True, "taken": base.get("taken") or 0, "total": len(rows), "files": rows[:MAX_SHOWN]}


def touched_by(transcript: str, root: str) -> set[str]:
    """The files a Claude worker wrote or edited (Write, Edit, MultiEdit, NotebookEdit), as paths relative to `root`."""
    names = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
    seen: set[str] = set()
    try:
        with open(transcript, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"tool_use"' not in line:
                    continue
                try:
                    msg = json.loads(line).get("message") or {}
                except ValueError:
                    continue
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in names:
                        target = (block.get("input") or {}).get("file_path") or (block.get("input") or {}).get("notebook_path")
                        if target:
                            seen.add(target)
    except OSError:
        return set()
    out = set()
    for target in seen:
        try:
            rel = os.path.relpath(target, root).replace(os.sep, "/")
        except ValueError:
            continue
        if not rel.startswith(".."):
            out.add(rel)
    return out
