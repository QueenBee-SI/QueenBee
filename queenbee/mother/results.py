"""Comparing finished jobs without an agent.

"Run three settings and pick the best" needs no model: each job's final value
sits in a file the architect named (JSON, or the last row of a CSV). Mother
reads them and ranks the runs -- zero tokens, and no agent that could misread
a number.
"""

from __future__ import annotations

import csv
import json
import os
from typing import Any, Optional


def _lookup(data: Any, key: str) -> Optional[float]:
    """A value by key; a dotted key walks into nested objects."""
    cur = data
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    try:
        return float(cur)
    except (TypeError, ValueError):
        return None


def read_value(project: str, rel_path: str, key: str, goal: str = "max") -> Optional[float]:
    if not rel_path or not key:
        return None
    path = rel_path if os.path.isabs(rel_path) else os.path.join(project, rel_path)
    if path.lower().endswith(".jsonl"):
        # A per-epoch log: the run's result is its best epoch.
        from .sweep import best_of, read_history
        return best_of(read_history(project, rel_path, key), goal)
    try:
        if path.lower().endswith(".csv"):
            with open(path, newline="", encoding="utf-8", errors="replace") as fh:
                rows = list(csv.DictReader(fh))
            return _lookup(rows[-1], key) if rows else None
        with open(path, encoding="utf-8", errors="replace") as fh:
            return _lookup(json.load(fh), key)
    except (OSError, ValueError, IndexError):
        return None


def compare(jobs: list[dict[str, Any]], spec: Optional[dict[str, str]]) -> Optional[dict[str, Any]]:
    if not spec:
        return None
    rows = []
    for job in jobs:
        value = read_value(job.get("project_path") or "", job.get("metrics_path") or "",
                           job.get("metric_key") or "", spec.get("goal") or "max")
        rows.append({"priority": job["priority"], "title": job.get("title"), "value": value,
                     "status": job.get("status"), "source": job.get("metrics_path")})
    have = [r for r in rows if r["value"] is not None]
    best = None
    if have:
        pick = max if spec.get("goal") != "min" else min
        best = pick(have, key=lambda r: r["value"])["priority"]
    return {"metric": spec.get("metric"), "goal": spec.get("goal"), "rows": rows, "best": best,
            "complete": all(r["value"] is not None for r in rows)}
