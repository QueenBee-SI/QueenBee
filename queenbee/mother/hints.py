"""What a short instruction already says about how to run it: at most how many workers, which agents, and what "done" means.

    Build the changes in TASK.md.
    Use at most 3 workers and use both Claude and Codex.
    Done means: python check_minidb.py passes all checks.

The user's own words decide these, so the queen does not guess or ask: a worker cap, the agents named, and the command that
proves the work (run by the queen when the workers have finished). Only a test-runner-like command is taken as a check.
"""

from __future__ import annotations

import re
from typing import Any

AGENT_WORDS = {"claude": "claude", "codex": "codex", "gemini": "gemini", "kimi": "kimi", "opencode": "opencode",
               "cursor": "cursor", "mistral": "mistral", "vibe": "mistral", "grok": "grok"}
CAP = re.compile(r"(?:at most|up to|no more than|max(?:imum)? of?|max)\s+(\d{1,2})\s+(?:workers?|agents?|bees?)"
                 r"|(\d{1,2})\s+(?:workers?|agents?)\s+(?:at most|max)|en fazla\s+(\d{1,2})\s+(?:worker|işçi|agent)", re.I)
DONE = re.compile(r"(?:done|finished|complete)\s+means\s*:?\s*(?P<cmd>[^\n]+)|bitti(?:ği)?\s*:?\s*(?P<cmd2>[^\n]+)", re.I)
RUNNER = re.compile(r"^(?:python(?:3)?(?:\.exe)?|py|pytest|npm\s+(?:run\s+)?test|cargo\s+test|go\s+test|dotnet\s+test|make\s+test)\b", re.I)
UNSAFE = re.compile(r"[;&|<>`$]|\.\.[\/]")
TAIL = re.compile(r"\s+(?:passes?|succeeds?|is green|runs? clean|exits? 0|geçer|geçmeli)\b.*$", re.I)


STOP = {
    "English": "the and to of in for with is are use at most build follow that this all it be on as by from or an",
    "Turkish": "ve bir bu için ile de da en fazla kullan yap yapı olarak çok gibi daha sonra ama ya ki mi mı mu mü",
    "German": "und der die das ist nicht mit für von zu den dem ein eine auf im sich auch es",
    "French": "et le la les de des un une est pour avec dans sur pas que qui au aux",
    "Spanish": "y el la los las de un una es para con en por que no se del al",
}


def language(text: str) -> str:
    """The language an instruction is written in, by common words ("" when it cannot be told): the planner is told it instead of
    guessing, because a model once answered an English instruction with German titles."""
    words = re.findall(r"[^\W\d_]+", (text or "").lower())
    if len(words) < 4:
        return ""
    scores = {name: sum(1 for w in words if w in set(stops.split())) for name, stops in STOP.items()}
    best = max(scores, key=scores.get)
    ordered = sorted(scores.values(), reverse=True)
    return best if ordered[0] >= 2 and ordered[0] > ordered[1] else ""


def parse(text: str) -> dict[str, Any]:
    """{"max_workers": int (0 = not said), "agents": [ids named, in order], "check": the command or ""}."""
    out: dict[str, Any] = {"max_workers": 0, "agents": [], "check": ""}
    m = CAP.search(text or "")
    if m:
        out["max_workers"] = int(next(g for g in m.groups() if g))
    lowered = (text or "").lower()
    found = [(lowered.find(w), a) for w, a in AGENT_WORDS.items() if re.search(rf"\b{re.escape(w)}\b", lowered)]
    named: list[str] = []
    for _, agent in sorted(found):
        if agent not in named:
            named.append(agent)
    out["agents"] = named
    d = DONE.search(text or "")
    if d:
        cmd = (d.group("cmd") or d.group("cmd2") or "").strip().rstrip(".")
        ticks = re.search(r"`([^`]+)`", cmd)            # "Done means `python check.py` prints 40/40": the command is what is in ticks
        cmd = ticks.group(1).strip() if ticks else TAIL.sub("", cmd).strip().strip("`").strip()
        cmd = re.sub(r"\s+(?:prints?|reports?|shows?|outputs?)\b.*$", "", cmd, flags=re.I).strip()
        if cmd and RUNNER.match(cmd) and not UNSAFE.search(cmd):
            out["check"] = cmd
    return out


PEERS = re.compile(r"(?:all\s+workers?\s+start\s+at\s+once|start\s+at\s+once|do\s+not\s+plan\s+a\s+senior|no\s+senior|no\s+foundation\s+worker)", re.I)


def wants_peers(folder: str) -> bool:
    """True when the project's QUEEN.md asks for a team with no senior: every worker starts at once on its own part."""
    import os

    try:
        with open(os.path.join(folder, "QUEEN.md"), encoding="utf-8", errors="replace") as fh:
            return bool(PEERS.search(fh.read(20000)))
    except OSError:
        return False
