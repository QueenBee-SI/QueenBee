"""What is worth interrupting the user for, decided from successive snapshots.

The page shows its own toasts, but only while it is on screen. The QueenBee app
lives in the tray, so it watches the same snapshot and turns the few changes
that want a human into desktop notifications:

- a conversation started in QueenBee now waits for the user (its agent finished a
  turn, or the background job it was running ended), or is blocked on a
  permission prompt;
- the machine is about to run out of memory.

Nothing fires on the first snapshot: a conversation that was already waiting
when QueenBee started is not news.
"""

from __future__ import annotations

import time
from typing import Any, Optional

WANTS_HUMAN = {"waiting", "blocked"}
MEMORY_WARN_PCT = 92.0
MEMORY_REPEAT_S = 10 * 60


def _name(row: dict[str, Any]) -> str:
    title = row.get("title")
    if title:
        return str(title)
    cwd = str(row.get("cwd") or "").replace("\\", "/").rstrip("/")
    return cwd.rsplit("/", 1)[-1] or str(row.get("agent") or "a conversation")


class Watcher:
    def __init__(self) -> None:
        self._states: Optional[dict[str, str]] = None
        self._memory_at = float("-inf")    # the first warning is never held back
        self._risks: set[tuple[int, float]] = set()   # (pid, create time) already told

    def feed(self, snapshot: dict[str, Any], now: Optional[float] = None) -> list[tuple[str, str]]:
        """(title, message) for each change worth a notification."""
        now = time.time() if now is None else now
        out: list[tuple[str, str]] = []
        rows = [r for r in snapshot.get("sessions") or [] if r.get("queenbee_owned") and r.get("key")]
        states = {r["key"]: str(r.get("state") or "") for r in rows}

        if self._states is not None:
            for row in rows:
                key, state = row["key"], states[row["key"]]
                before = self._states.get(key)
                if state in WANTS_HUMAN and before is not None and before not in WANTS_HUMAN:
                    verb = "needs your permission" if state == "blocked" else "is waiting for you"
                    out.append((f"{_name(row)} {verb}", str(row.get("state_reason") or "")))
        self._states = states

        # A program outside QueenBee holding GPU memory while a primary job runs.
        # Once per process: the page carries the Stop button; this only makes
        # sure it is seen when the window is in the tray. Unlike the rows
        # above, one already present at startup is news -- it is a live risk.
        guard = snapshot.get("primary_guard") or {}
        risks = set()
        for threat in guard.get("threats") or []:
            key = (int(threat.get("pid") or 0), float(threat.get("born") or 0))
            risks.add(key)
            if key not in self._risks:
                primary = ", ".join(str(p) for p in guard.get("primaries") or []) or "the primary job"
                gb = (threat.get("vram_mb") or 0) / 1024
                out.append((f"{primary} is at risk",
                            f"{threat.get('name')} (pid {key[0]}) holds {gb:.1f} GB of GPU memory "
                            "outside QueenBee. Open QueenBee to stop it."))
        self._risks = risks

        ram = (snapshot.get("system") or {}).get("ram_pct")
        if ram is not None and ram >= MEMORY_WARN_PCT and now - self._memory_at >= MEMORY_REPEAT_S:
            self._memory_at = now
            heavy = max(rows, key=lambda r: r.get("rss_mb") or 0, default=None)
            detail = f"RAM {ram:.0f}% used."
            if heavy and (heavy.get("rss_mb") or 0) > 0:
                detail += f" Largest: {_name(heavy)} at {heavy['rss_mb'] / 1024:.1f} GB."
            out.append(("Memory is nearly full", detail))
        return out
