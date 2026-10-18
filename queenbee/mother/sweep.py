"""Sweeping one setting from both ends at once, deciding with numbers only.

"Try lambda from 0.1 upward and from 1.0 downward" is two lanes run side by
side. Each lane runs one value at a time; the GPU holds both. A lane stops
when its score falls (the best value is behind it) or when it meets the other
lane; the best value found is the sweet spot. Every next value is the same
command with one number changed, so Mother submits it itself -- no agent, no
tokens.

A run that is clearly losing is stopped before it finishes: at the same epoch
it trails the best finished run by more than a margin (the median-stopping
idea, with the best run as the bar). Its GPU goes to the next value.

With repeats, each value runs several times (the script's own randomness
gives each a different seed) and is scored by the mean; the report gives the
spread. A lane given as an explicit list of values runs all of them. Early
stopping then only catches collapsed runs, since single runs differ by noise.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from .. import benchstats


def lane_values(start: float, step: float, lo: float, hi: float) -> list[float]:
    out, v = [], start
    while lo - 1e-9 <= v <= hi + 1e-9 and len(out) < 100:
        out.append(round(v, 6))
        v += step
    return out


def normalise(raw: Any) -> Optional[dict[str, Any]]:
    """The planner's sweep, checked; None when there is none."""
    if not isinstance(raw, dict) or not str(raw.get("param") or "").strip():
        return None
    try:
        listed = [float(v) for lane in raw.get("lanes") or [] for v in (lane.get("values") or [])]
        lo = float(raw["min"]) if raw.get("min") is not None else min(listed)
        hi = float(raw["max"]) if raw.get("max") is not None else max(listed)
        lanes = []
        for lane in raw.get("lanes") or []:
            if isinstance(lane.get("values"), list) and lane["values"]:
                # An explicit list: every value runs, in the order given.
                lanes.append({"name": str(lane.get("name") or "grid")[:12], "exhaustive": True,
                              "values": [round(float(v), 6) for v in lane["values"]][:100]})
                continue
            start, step = float(lane["start"]), float(lane["step"])
            if step == 0:
                continue
            lanes.append({"name": str(lane.get("name") or ("up" if step > 0 else "down"))[:12],
                          "values": lane_values(start, step, min(lo, hi), max(lo, hi))})
    except (TypeError, ValueError, KeyError):
        return None
    lanes = [l for l in lanes if l["values"]]
    if not lanes:
        return None
    epochs = raw.get("epochs")
    repeats = max(1, min(10, int(raw.get("repeats") or 1)))
    return {"param": str(raw["param"]).strip(), "lanes": lanes, "repeats": repeats,
            # Runs at once: what the GPU holds, not how many lanes there are.
            "slots": max(2, len(lanes), int(raw.get("slots") or 0)),
            "epochs": int(epochs) if isinstance(epochs, (int, float)) and epochs > 0 else None,
            # No run is judged before a third of its epochs: early epochs are
            # warm-up, and a setting may not act at all until later.
            "min_epoch": int(raw.get("min_epoch") or max(8, (int(epochs) if isinstance(epochs, (int, float))
                                                              and epochs > 0 else 24) // 3)),
            # With repeats a single run is noise-level evidence: stop only
            # runs that collapsed, not ones a little behind.
            "margin": float(raw.get("margin") or (0.15 if repeats > 1 else 0.03)),
            # A gap is judged against the metric's own scale (a fraction of the
            # best run's value) unless the user fixed an absolute margin: 0.03
            # means nothing for a loss of 40 or an accuracy of 0.95.
            "margin_rel": (float(raw["margin_rel"]) if raw.get("margin_rel")
                           else None if raw.get("margin") else (0.2 if repeats > 1 else 0.05)),
            "tolerance": float(raw.get("tolerance") or 0.0),
            # A run is scored by where it ended (mean of its last epochs), not
            # by its luckiest epoch; the best epoch is kept beside it.
            "score_on": "best" if raw.get("score_on") == "best" else "final",
            # Every repeat gets its own recorded seed: base + repeat number.
            "seed": int(raw.get("seed") or 1000), "seed_flag": str(raw.get("seed_flag") or "").strip()}


def seed_for(spec: dict[str, Any], rep: int) -> int:
    """The seed of repeat `rep`: the same across values (paired runs), different
    across repeats, and written down."""
    return int(spec.get("seed") or 1000) + int(rep) - 1


def fill(template: str, value: float, rep: int = 1, repeats: int = 1, seed: Optional[int] = None,
         seed_flag: str = "") -> str:
    tag = f"{value:g}".replace(".", "p").replace("-", "m") + (f"_r{rep}" if repeats > 1 else "")
    out = template.replace("{value}", f"{value:g}").replace("{tag}", tag)
    if seed is not None:
        out = out.replace("{seed}", str(seed))
        if seed_flag and seed_flag not in out:
            out = f"{out} {seed_flag} {seed}"
    return out


def read_history(project: str, rel_path: str, key: str) -> list[float]:
    """Per-epoch values of `key` from a JSON-lines log, in order."""
    if not rel_path:
        return []
    path = rel_path if os.path.isabs(rel_path) else os.path.join(project, rel_path)
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    v = json.loads(line).get(key)
                except (ValueError, AttributeError):
                    continue
                if isinstance(v, (int, float)):
                    out.append(float(v))
    except OSError:
        return []
    return out


def final_of(history: list[float], last: int = 3) -> Optional[float]:
    """Where a run ended: the mean of its last few epochs."""
    tail = history[-last:]
    return sum(tail) / len(tail) if tail else None


def best_of(history: list[float], goal: str = "max") -> Optional[float]:
    if not history:
        return None
    return max(history) if goal != "min" else min(history)


def should_stop_early(history: list[float], references: list[list[float]], spec: dict[str, Any],
                      goal: str = "max") -> Optional[str]:
    """Why a running job should be stopped now, or None.

    Compared at the same epoch with the best finished run: behind by more
    than the margin, once past `min_epoch`, it is not going to catch up.
    """
    epoch = len(history)
    if epoch < spec.get("min_epoch", 8) or not references:
        return None
    mine = best_of(history, goal)
    bars = [best_of(r[:epoch], goal) for r in references if len(r) >= epoch]
    bars = [b for b in bars if b is not None]
    if mine is None or not bars:
        return None
    bar = max(bars) if goal != "min" else min(bars)
    gap = (bar - mine) if goal != "min" else (mine - bar)
    limit = spec["margin_rel"] * max(abs(bar), 1e-9) if spec.get("margin_rel") else spec.get("margin", 0.03)
    if gap > limit:
        return (f"at epoch {epoch} its best is {mine:.4f}, the best finished run had "
                f"{bar:.4f} by then (behind by {gap:.4f})")
    return None


def _key(value: float) -> str:
    return f"{value:g}"


def akey(value: float, rep: int = 1) -> str:
    """The key of one run: its value and which repeat it is."""
    return f"{value:g}#{rep}"


def parse_key(key: str) -> tuple[float, int]:
    v, _, r = str(key).partition("#")
    return float(v), int(r or 1)


def active_values(lane: dict[str, Any]) -> list[float]:
    return [parse_key(k)[0] for k in (lane.get("active") or {})]


def _reps_used(lane: dict[str, Any], value: float) -> set[int]:
    used = {int(r[3]) if len(r) > 3 else 1 for r in lane.get("runs") or [] if _key(r[0]) == _key(value)}
    used |= {parse_key(k)[1] for k in (lane.get("active") or {}) if _key(parse_key(k)[0]) == _key(value)}
    return used


def next_value(lane: dict[str, Any], lanes: list[dict[str, Any]], repeats: int = 1):
    """(value, repeat) of the lane's next run, or (None, why not)."""
    for value in lane["values"]:
        used = _reps_used(lane, value)
        free = [r for r in range(1, repeats + 1) if r not in used]
        if not free:
            continue
        if not used and not lane.get("exhaustive"):
            # A value this lane has not touched: stop where the lanes meet.
            taken = [r[0] for other in lanes if other is not lane for r in (other.get("runs") or [])]
            taken += [v for other in lanes if other is not lane for v in active_values(other)]
            if any(_between(lane, value, t) for t in taken):
                return None, "met the other lane"
        return (value, free[0]), ""
    return None, "no values left"


def value_scores(lane: dict[str, Any], repeats: int = 1) -> dict[str, dict[str, Any]]:
    """Per value: its full-run scores, mean, spread and whether all repeats ended."""
    out: dict[str, dict[str, Any]] = {}
    for r in lane.get("runs") or []:
        d = out.setdefault(_key(r[0]), {"value": r[0], "scores": [], "ended": 0, "early": 0})
        d["ended"] += 1
        if len(r) > 2 and r[2]:
            d["early"] += 1
        elif r[1] is not None:
            d["scores"].append(r[1])
    for d in out.values():
        sc = d["scores"]
        d["complete"] = d["ended"] >= repeats
        d["mean"] = sum(sc) / len(sc) if sc else None
        d["std"] = (sum((x - d["mean"]) ** 2 for x in sc) / (len(sc) - 1)) ** 0.5 if len(sc) > 1 else None
        d["ci95"] = benchstats.mean_ci(sc)["ci95"]
    return out


def peak_passed(lane: dict[str, Any], goal: str = "max", tolerance: float = 0.0,
                repeats: int = 1) -> Optional[str]:
    """Why the lane is past its peak, or None.

    Values finish out of order when a lane runs several at once, so only the
    unbroken run of finished values from the lane's start is judged: the
    first one scoring below the one before it means the best is behind. A
    value counts once all its repeats ended; its score is their mean. A run
    stopped early has only its first epochs' best, a lower bound that cannot
    be set against a full run's score, so it shows no trend. An explicit list
    of values runs whole.
    """
    if lane.get("exhaustive"):
        return None
    scores = value_scores(lane, repeats)
    prev = None
    for v in lane["values"]:
        d = scores.get(_key(v))
        if d is None or not d["complete"]:
            return None
        s = d["mean"]
        if s is not None and prev is not None:
            worse = s < prev[1] - tolerance if goal != "min" else s > prev[1] + tolerance
            # With repeats a fall counts only when it is larger than the noise:
            # the two means' confidence intervals together.
            if worse and d.get("ci95") and prev[2]:
                worse = abs(s - prev[1]) > (d["ci95"] ** 2 + prev[2] ** 2) ** 0.5
            if worse:
                return f"score fell after {prev[0]:g}: the best is behind this lane"
        if s is not None:
            prev = (v, s, d.get("ci95"))
    return None


def _between(lane: dict[str, Any], value: float, other: float) -> bool:
    """Whether `value` reaches or passes `other` going this lane's way."""
    vals = lane["values"]
    rising = len(vals) < 2 or vals[1] > vals[0]
    return value >= other - 1e-9 if rising else value <= other + 1e-9


def beyond(lane: dict[str, Any], value: float, than: float) -> bool:
    """Whether `value` lies further along the lane than `than`."""
    vals = lane["values"]
    rising = len(vals) < 2 or vals[1] > vals[0]
    return value > than + 1e-9 if rising else value < than - 1e-9


def summary(lanes: list[dict[str, Any]], goal: str = "max", repeats: int = 1) -> dict[str, Any]:
    """The best value by its mean over repeats, with the spread."""
    rows = []
    for lane in lanes:
        for d in value_scores(lane, repeats).values():
            if d["mean"] is not None:
                rows.append((d["value"], d["mean"], lane["name"], d["std"], len(d["scores"])))
    if not rows:
        return {"best": None}
    pick = max if goal != "min" else min
    v, m, name, std, n = pick(rows, key=lambda r: r[1])
    ci = benchstats.mean_ci(next(d["scores"] for lane in lanes for d in value_scores(lane, repeats).values()
                                 if d["value"] == v and d["mean"] == m))["ci95"]
    return {"best": v, "score": m, "lane": name, "std": std, "n": n, "ci95": ci}
