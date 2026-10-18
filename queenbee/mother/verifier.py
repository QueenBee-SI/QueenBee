"""Deterministic acceptance: "the worker said it is done" proves nothing.

The planner gives each job typed checks; the queen runs them herself, with no
model, when the worker reports done. A job is done only when they pass.

    {"type": "command",       "run": "python -m unittest tests.test_cart", "expect_exit": 0}
    {"type": "file_exists",   "path": "runs/p1/metrics.jsonl"}
    {"type": "file_contains", "path": "README.md", "text": "python -m shop"}
    {"type": "jsonl_rows",    "path": "runs/p1/metrics.jsonl", "min_rows": 2}

Commands come from a model's plan and run with no one approving them, so only
test runners, a syntax check and `<script>.py --help` are allowed. Paths stay
inside the project folder.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Optional

TYPES = ("command", "file_exists", "file_contains", "jsonl_rows")
MAX_CHECKS = 12

# Quotes and the glob characters * ? are fine in the arguments (a test pattern such as
# -p "test_core_*.py"): the command runs without a shell, split like a shell would.
COMMAND_OK = re.compile(
    r"^(python(3|\.exe)? -m (unittest|pytest|py_compile)|pytest|npm (run )?test|cargo test|go test"
    r"|python(3|\.exe)? [\w./\\-]+\.py --help)\b[\w\s./:=\-\\\"'*?,]*$")


def split_command(run: str) -> list[str]:
    """argv of an allowed command: shell-style quoting, no shell."""
    import shlex
    try:
        return shlex.split(run.replace("\\", "/"))
    except ValueError:
        return run.split()


def normalise(raw: Any, trusted: bool = False) -> list[dict[str, Any]]:
    """An acceptance list, cleaned: known types only, bounded. `trusted` is for checks the user typed
    themselves: their commands are not held to the allowlist that guards a model's plan."""
    out: list[dict[str, Any]] = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict) or c.get("type") not in TYPES:
            continue
        check: dict[str, Any] = {"type": c["type"]}
        if c["type"] == "command":
            run = str(c.get("run") or "").strip()
            if not run:
                continue
            if not trusted and not COMMAND_OK.match(run[:200]):
                continue                      # a model's check the queen may not run could never pass: it would only send the worker back for ever
            check.update(run=run[:200], expect_exit=int(c.get("expect_exit") or 0))
            if trusted:
                check["trusted"] = True
        else:
            path = str(c.get("path") or "").strip()
            if not path:
                continue
            check["path"] = path[:200]
            if c["type"] == "file_contains":
                if not str(c.get("text") or ""):
                    continue
                check["text"] = str(c["text"])[:200]
            if c["type"] == "jsonl_rows":
                check["min_rows"] = max(1, int(c.get("min_rows") or 1))
        out.append(check)
    return out[:MAX_CHECKS]


def describe(check: dict[str, Any]) -> str:
    t = check["type"]
    if t == "command":
        return f"`{check['run']}` exits {check.get('expect_exit', 0)}"
    if t == "file_exists":
        return f"{check['path']} exists"
    if t == "file_contains":
        return f"{check['path']} contains {check['text']!r}"
    return f"{check['path']} has at least {check['min_rows']} line(s)"


def _inside(folder: str, rel: str) -> Optional[str]:
    root = os.path.realpath(folder)
    path = os.path.realpath(os.path.join(root, rel))
    return path if path == root or path.startswith(root + os.sep) else None


def run_checks(checks: list[dict[str, Any]], folder: str,
               runner: Optional[Callable[[list[str], str, float], tuple[Optional[int], str]]] = None,
               timeout: float = 300) -> list[dict[str, Any]]:
    """Every check run, in order: [{check, ok, detail}]."""
    results = []
    for c in checks:
        ok, detail = False, ""
        t = c["type"]
        try:
            if t == "command":
                run = c["run"]
                if not c.get("trusted") and not COMMAND_OK.match(run):
                    detail = "not an allowed command (test runners, py_compile, <script>.py --help)"
                elif runner is None:
                    detail = "no runner"
                else:
                    code, out = runner(split_command(run), folder, timeout)
                    ok = code == c.get("expect_exit", 0)
                    detail = "" if ok else (f"exit {code}: " + out.strip()[-600:] if code is not None
                                            else "did not finish in time")
            else:
                path = _inside(folder, c["path"])
                if path is None:
                    detail = "path is outside the project"
                elif not os.path.isfile(path):
                    detail = "file is missing"
                elif t == "file_exists":
                    ok = True
                elif t == "file_contains":
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        ok = c["text"] in fh.read()
                    detail = "" if ok else "text not found"
                else:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        rows = sum(1 for line in fh if line.strip())
                    ok = rows >= c["min_rows"]
                    detail = "" if ok else f"{rows} line(s), needs {c['min_rows']}"
        except (OSError, ValueError) as exc:
            detail = str(exc)
        results.append({"check": describe(c), "ok": ok, "detail": detail})
    return results


def failures(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in results if not r["ok"]]
