"""Plain statistics and run ordering for benchmarks: no I/O, no models.

A comparison of two agents on a handful of runs is only evidence if the order
is balanced (a warm cache or a busy API favours whoever runs second), every
run is tied to one trace id, a failed run is counted as an unfinished task
rather than a cheap one, and the spread is reported beside the mean.
"""

from __future__ import annotations

import math
import re
import random
import time
import uuid
from typing import Any, Iterable, Sequence

# Two-sided 95% Student t, by degrees of freedom (1..30); 1.96 beyond.
_T95 = [12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160,
        2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056,
        2.052, 2.048, 2.045, 2.042]


def trace_id(arm: str, rep: int) -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{arm}-r{rep}-{uuid.uuid4().hex[:6]}"


def schedule(arms: Sequence[str], reps: int, seed: int | None = None) -> list[tuple[str, int]]:
    """The run order as (arm, repeat). Two arms go A-B-B-A, A-B-B-A ... so each
    sits equally early and late; more arms are shuffled within each round,
    reproducibly from `seed`."""
    arms = list(arms)
    out: list[tuple[str, int]] = []
    counts = {a: 0 for a in arms}
    rng = random.Random(seed)
    for rnd in range(reps):
        if len(arms) == 2:
            order = arms if rnd % 2 == 0 else arms[::-1]
        else:
            order = arms[:]
            rng.shuffle(order)
        for a in order:
            counts[a] += 1
            out.append((a, counts[a]))
    return out


def mean_ci(xs: Iterable[float]) -> dict[str, Any]:
    xs = [float(x) for x in xs]
    n = len(xs)
    if n == 0:
        return {"n": 0, "mean": None, "std": None, "ci95": None}
    m = sum(xs) / n
    if n == 1:
        return {"n": 1, "mean": m, "std": None, "ci95": None}
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    t = _T95[n - 2] if n - 1 <= len(_T95) else 1.96
    return {"n": n, "mean": m, "std": sd, "ci95": t * sd / math.sqrt(n)}


def passed(row: dict[str, Any], threshold: float = 1.0) -> bool:
    """A run succeeds only when the acceptance score reaches the threshold and
    the run did not time out. The agent saying it finished proves nothing."""
    s = (row.get("score") or {}).get("score")
    return s is not None and s >= threshold and not row.get("timed_out")


def aggregate(rows: list[dict[str, Any]], threshold: float = 1.0) -> dict[str, dict[str, Any]]:
    """Per arm: success rate, time/cost over ALL runs (a failure is not free),
    and cost per successful task -- total spend divided by the tasks that
    were actually completed."""
    out: dict[str, dict[str, Any]] = {}
    for arm in dict.fromkeys(r["arm"] for r in rows):
        mine = [r for r in rows if r["arm"] == arm]
        ok = [r for r in mine if passed(r, threshold)]
        cost = sum(float(r.get("cost_usd") or 0) for r in mine)
        out[arm] = {
            "runs": len(mine), "succeeded": len(ok), "success_rate": len(ok) / len(mine),
            "minutes": mean_ci(float(r.get("minutes") or 0) for r in mine),
            "cost_usd": mean_ci(float(r.get("cost_usd") or 0) for r in mine),
            "score": mean_ci(float((r.get("score") or {}).get("score") or 0) for r in mine),
            "interventions": mean_ci(float(r.get("left_to_user") or r.get("interventions") or 0) for r in mine),
            "cost_per_success": (cost / len(ok)) if ok else None,
        }
    return out


def compare(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """b against a on the headline numbers, as ratios (b / a)."""
    def ratio(x, y):
        return (y / x) if x and y is not None else None
    return {"time": ratio(a["minutes"]["mean"], b["minutes"]["mean"]),
            "cost": ratio(a["cost_usd"]["mean"], b["cost_usd"]["mean"]),
            "success_rate_delta": b["success_rate"] - a["success_rate"]}


# -- the user's time: every moment an agent waits for a person ---------------------

_KINDS = (("write", r"(?i)\b(edit|writ|creat|overwrit|modif|apply|sav)\w*"),
          ("read", r"(?i)\b(read|open|list|search|glob|grep|view|access)\w*"),
          ("command", r"(?i)\b(bash|powershell|command|run|execut|install|delet|remov|download|curl)\w*"))


def stop_kind(text: str) -> str:
    """What an agent stopped to ask: write / read / command permission, or a question."""
    for kind, pattern in _KINDS:
        if re.search(pattern, text or ""):
            return kind
    return "question"


def human_stops(run: dict[str, Any]) -> dict[str, Any]:
    """Every stop of one run in a common form. A single agent's summary lists
    its permission prompts (`interventions`), the times it handed a choice back
    (`steering_stops`) and the folder-trust prompt (`setup_approvals`); a
    queen result counts the stops at her workers and who answered them."""
    if "stops" in run:
        return {"total": run.get("stops") or 0, "to_human": run.get("left_to_user") or 0,
                "answered_by_queen": run.get("answered_by_queen") or 0,
                "kinds": {}, "steers": run.get("queen_steers") or 0}
    kinds: dict[str, int] = {}
    for text in run.get("interventions") or []:
        k = stop_kind(str(text))
        kinds[k] = kinds.get(k, 0) + 1
    steering = len(run.get("steering_stops") or [])
    if steering:
        kinds["handed back a choice"] = steering
    setup = int(run.get("setup_approvals") or 0)
    if setup:
        kinds["folder trust"] = setup
    total = sum(kinds.values())
    return {"total": total, "to_human": total, "answered_by_queen": 0, "kinds": kinds, "steers": 0}


def with_human_wait(minutes: float, to_human: int, latency_s: float, answered_in_s: float = 3.0) -> float:
    """Wall minutes plus what a real person's reaction time would add: each stop
    that goes to a human costs `latency_s` instead of the few seconds a
    scripted answer took."""
    return minutes + to_human * max(0.0, latency_s - answered_in_s) / 60.0
