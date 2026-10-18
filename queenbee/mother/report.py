"""The result report of a finished plan -- written by Mother, not by a model.

Everything in it is read from what happened: each job's configuration and
result (from the file the architect named), which agent and model ran it and
what moved it up, when its GPU job stepped aside and came back, the answers
Mother gave workers and the ones it left to the user, the GPU's busy and idle
time, and what every model call cost, from the agents' own transcripts.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional


def _fmt_value(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:.4f}" if abs(v) < 10 else f"{v:.2f}"


def _duration(s: float) -> str:
    s = int(round(s or 0))
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"


def _who(job: dict[str, Any]) -> str:
    if job.get("direct") and not job.get("terminal"):
        return "Mother (no agent)"
    if job.get("agent") == "codex":
        return "codex"
    return f"claude {job.get('tier') or job.get('effort') or ''}".strip()


def build(jobs: list[dict[str, Any]], results: Optional[dict[str, Any]], *,
          instruction: str = "", approvals: Optional[list[dict[str, Any]]] = None,
          gpu: Optional[dict[str, float]] = None, costs: Optional[dict[str, Any]] = None,
          started_at: float = 0.0, finished_at: float = 0.0,
          sweep: Optional[dict[str, Any]] = None) -> str:
    approvals = approvals or []
    gpu = gpu or {}
    costs = costs or {}
    rows = {r["priority"]: r for r in (results or {}).get("rows") or []}
    metric = (results or {}).get("metric") or "result"
    best = (results or {}).get("best")
    out = ["# QueenBee report", ""]
    if instruction:
        out += [f"> {instruction}", ""]
    wall = (finished_at or time.time()) - (started_at or time.time())
    done = sum(1 for j in jobs if j.get("status") == "done")
    out += [f"**{done} of {len(jobs)} jobs finished** in {_duration(wall)}"
            + (f"; best by {metric}: **W{best}**" if best else "") + ".", ""]

    out += ["## Results", "", f"| Job | Status | {metric} | Run by | Attempts |", "|---|---|---|---|---|"]
    for j in jobs:
        r = rows.get(j["priority"]) or {}
        star = " ★" if best == j["priority"] else ""
        out.append(f"| W{j['priority']} {j.get('title', '')} | {j.get('status')} | "
                   f"{_fmt_value(r.get('value'))}{star} | {_who(j)} | {j.get('attempts', 0)} |")
    out.append("")

    if sweep:
        res = sweep.get("result") or {}
        out += [f"## Sweep of `{sweep.get('param')}`", ""]
        if res.get("best") is not None:
            spread = (f" ± {_fmt_value(res['std'])} (sd), 95% CI ± {_fmt_value(res.get('ci95'))}, "
                      f"over {res.get('n')} runs" if res.get("std") is not None else "")
            out += [f"**Best value: {res['best']:g}** ({metric} {_fmt_value(res.get('score'))}{spread}, "
                    f"found by the {res.get('lane')} lane).", ""]
        k = sweep.get("repeats") or 1
        if k > 1:
            from .sweep import value_scores
            seeds = f"seeds {sweep.get('seed', 1000)}..{sweep.get('seed', 1000) + k - 1} (same seed for every value)"
            out += [f"Each value ran {k} times; its score is the mean of the full runs, each scored where it ended "
                    f"(last epochs, not the best epoch); {seeds}.", "",
                    f"| {sweep.get('param')} | mean | spread (sd) | 95% CI ± | full runs | stopped early |",
                    "|---|---|---|---|---|---|"]
            for lane in sweep.get("lanes") or []:
                rows = sorted(value_scores(lane, k).values(), key=lambda d: d["value"])
                for d in rows:
                    star = " ★" if res.get("best") is not None and abs(d["value"] - res["best"]) < 1e-9 else ""
                    out.append(f"| {d['value']:g}{star} | {_fmt_value(d['mean'])} | "
                               f"{_fmt_value(d['std'])} | {_fmt_value(d.get('ci95'))} | {len(d['scores'])} | {d['early']} |")
            out.append("")
        for lane in sweep.get("lanes") or []:
            path = " → ".join(f"{r[0]:g}" + (f"#{r[3]}" if k > 1 and len(r) > 3 else "")
                              + f": {_fmt_value(r[1])}" + (" (stopped early)" if len(r) > 2 and r[2] else "")
                              for r in lane.get("runs") or [])
            out.append(f"- **{lane.get('name')}**: {path or '—'}"
                       + (f"  _(stopped: {lane.get('why')})_" if lane.get("why") else ""))
        early = [j for j in jobs if j.get("status") == "stopped early"]
        out += [f"- stopped early to free the GPU: {len(early)}"
                + (" (" + ", ".join(f"{j.get('value', '?'):g}" if isinstance(j.get('value'), (int, float))
                                     else str(j.get('value')) for j in early) + ")" if early else ""),
                "- every value after the first ran with no agent: 0 tokens", ""]

    busy, idle = gpu.get("busy_s", 0.0), gpu.get("idle_s", 0.0)
    if busy + idle:
        out += ["## GPU", "",
                f"- busy {_duration(busy)}, idle {_duration(idle)} "
                f"({100 * busy / (busy + idle):.0f}% of the run busy)",
                f"- steps aside: {sum(j.get('preempts', 0) for j in jobs)}, "
                f"resumed from saved state: {sum(j.get('resumes', 0) for j in jobs)}, "
                f"restarted after dying: {sum(j.get('restarts', 0) for j in jobs)}", ""]

    human = [a for a in approvals if a.get("decision") == "left to you"]
    out += ["## Approvals", "",
            f"- answered by Mother: {len(approvals) - len(human)}",
            f"- left to you: {len(human)}"]
    for a in human:
        out.append(f"  - W{a.get('priority')}: {a.get('question', '')[:120]} ({a.get('why')})")
    out.append("")

    if costs:
        out += ["## Tokens", "", "| Part | Tokens |", "|---|---|"]
        total_usd, unknown = 0.0, False
        parts = [p for p in ("planner", "architect", "branches", "workers") if p != "branches" or p in costs]
        for part in parts:
            c = costs.get(part) or {}
            usd = c.get("cost_usd")
            if usd is None:
                unknown = unknown or bool(c.get("tokens"))
            else:
                total_usd += usd
            out.append(f"| {part} | {c.get('tokens', 0):,} |")
        out += [f"| **total** | **{sum((costs.get(p) or {}).get('tokens', 0) for p in parts):,}** |", ""]

    out += ["## What happened", ""]
    for j in jobs:
        events = j.get("events") or []
        if not events:
            continue
        out.append(f"**W{j['priority']} {j.get('title', '')}**")
        t0 = events[0][0]
        for ts, text in events:
            out.append(f"- +{_duration(ts - t0)}  {text}")
        if j.get("error"):
            out.append(f"- error: {j['error'][:300]}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return path
