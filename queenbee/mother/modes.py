"""How the queen runs a package: the cheapest way that is still reliable.

    DIRECT    one strong worker does all of it; no planner call at all when the
              instruction is plainly small (this module decides, by rules)
    RECIPE    the same instruction over the same project was planned before:
              the stored plan is reused, no model call
    COMPILE   GPU work: one agent adapts/prepares it, QueenBee schedules and runs it
    PARALLEL  independent parts for several workers (the planner's split)

The first decision is the mode, not the plan. Most small instructions never
need a planner (about $0.05 and 20-30 s) and a repeat needs nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Optional

MODES = ("DIRECT", "RECIPE", "COMPILE", "PARALLEL")

# Words that say the work is big, many-part, a sweep or GPU training: the
# planner must look at those.
_BIG = re.compile(
    r"(?i)\b(every|each|all|whole|entire|migrat\w*|refactor\w*|rewrite|from scratch|in parallel|parallel|"
    r"sweep|train\w*|epochs?|lambda|grid|hyper\w*|benchmark\w*)\b")
_LIST = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+\S")
MAX_DIRECT_CHARS = 280


def looks_small(instruction: str) -> bool:
    """Plainly one agent's job: short, one sentence or two, no list of parts,
    no words of scale, sweeps or training."""
    text = instruction.strip()
    if not text or len(text) > MAX_DIRECT_CHARS:
        return False
    if len(_LIST.findall(text)) >= 2 or _BIG.search(text):
        return False
    return text.count(" and ") + text.count(" ve ") + text.count(";") <= 2


def direct_plan(instruction: str, folder: str, agents: tuple[str, ...]) -> dict[str, Any]:
    """The one-worker plan for a small instruction, made without a model."""
    return {
        "jobs": [{"priority": 1, "title": " ".join(instruction.split()[:5])[:60] or "Task",
                  "goal": instruction.strip(), "project_path": folder, "agent": agents[0], "effort": "high",
                  "group": "", "kind": "task", "files": [], "after": [], "senior": False, "same_as": None}],
        "compare": None, "sweep": None, "verify": "", "notes": "direct: small package, no planner call",
        "planner": {"model": "rules", "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "seconds": 0.0},
    }


def mode_of(plan: dict[str, Any]) -> str:
    """Which mode a plan amounts to."""
    jobs = plan.get("jobs") or []
    if plan.get("sweep") or any(j.get("kind") == "gpu" for j in jobs):
        return "COMPILE"
    if sum(1 for j in jobs if j.get("kind") != "gpu") > 1:
        return "PARALLEL"
    return "DIRECT"


# -- RECIPE: a plan remembered per (instruction, project shape) ---------------

def _shape(folder: str) -> str:
    from .architect import _tree

    h = hashlib.sha256()
    for rel in _tree(folder, depth=4, limit=2000):
        try:
            h.update(f"{rel}:{os.path.getsize(os.path.join(folder, rel))}\n".encode())
        except OSError:
            continue
    return h.hexdigest()


def recipe_key(instruction: str, folder: str, agents: tuple[str, ...], model: str) -> str:
    h = hashlib.sha256()
    h.update(json.dumps([" ".join(instruction.split()), os.path.normcase(os.path.abspath(folder)),
                         list(agents), model, _shape(folder)]).encode())
    return h.hexdigest()[:24]


def _path(folder: str, key: str) -> str:
    return os.path.join(folder, ".queenbee", "plans", f"{key}.json")


def recipe_load(folder: str, key: str) -> Optional[dict[str, Any]]:
    try:
        with open(_path(folder, key), encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(plan, dict) or not plan.get("jobs"):
        return None
    plan["planner"] = {"model": "recipe", "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                       "seconds": 0.0, "cached": True}
    return plan


def recipe_save(folder: str, key: str, plan: dict[str, Any]) -> None:
    path = _path(folder, key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=1)
        os.replace(path + ".tmp", path)
    except (OSError, TypeError):
        pass
