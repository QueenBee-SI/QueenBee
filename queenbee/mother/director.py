"""The director: the queen's own model, looking in on her hive every couple of minutes.

The rule-based part of the queen answers permission prompts and runs checks for free. This is the part
that thinks: every `interval` seconds, and only when something changed, the queen's model reads the
hive (each worker's task, status and latest progress, the hive board, failed checks) and the project
files read-only, and decides whether a worker needs a word -- a decision two workers made differently,
a part waiting on another's output, a worker off track. Her words go out on the hive board like anyone's.

It never edits, runs or deletes anything: it can read files and speak. Each look costs tokens, which are
counted against the queen's budget.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from typing import Any

from ..agents.catalog import binary_for

log = logging.getLogger(__name__)

PROMPT = """You are the queen bee of a hive of AI coding agents (worker bees) building one project together.
You look in on them every few minutes. You can read the project files (read-only) and you can speak to the
workers; you cannot change anything yourself.

Your job is the connections between the parts: workers must agree on things the task does not fix (data
shapes, names, file formats, who calls whom), no worker should wait on something nobody is doing, and a
worker that drifts from its task should be pulled back. Speak only when a word will change what a worker does.
A worker marked as a sub-queen runs a hive of its own: you are the queen above her. Address a message to her
as "W<number>" and it reaches her whole hive. Feed her what her hive needs from the rest of the project
(interfaces, decisions, results others produce); do not manage her workers one by one.
Do not repeat what the hive board already says. Do not praise. Be specific: name the file, the field, the function.

THE HIVE NOW
{state}

REPLY with one JSON object and nothing else:
{{"messages": [{{"to": "all" or "W<number>", "text": "one or two sentences, an instruction or a decision"}}],
  "summary": "one short line: what you saw"}}
An empty "messages" list is the right answer when the hive is fine.
"""


def digest(jobs: list[dict[str, Any]], board: list[dict[str, Any]], verify: dict[str, Any] | None) -> str:
    """The state the director reads, short enough to be cheap."""
    lines = []
    for j in jobs:
        if j.get("kind") != "task":
            continue
        lead = ""
        if j.get("role") == "mother":
            lead = " (a sub-queen: she plans and runs a hive of her own below)"
        elif j.get("parent") is not None:
            lead = f" (in the hive of W{j['parent']})"
        row = [f"W{j['priority']} {j.get('title', '')}{lead} [{j.get('status')}]",
               f"  task: {(j.get('goal') or '')[:280]}"]
        if j.get("progress"):
            row.append(f"  latest progress: {str(j['progress'])[-300:]}")
        if j.get("error"):
            row.append(f"  error: {str(j['error'])[:200]}")
        failed = [r for r in (j.get("accept_results") or []) if not r.get("ok")]
        if failed:
            row.append("  failing checks: " + "; ".join(f"{r['check']}: {str(r.get('detail'))[:120]}" for r in failed))
        for ts, text in (j.get("events") or [])[-3:]:
            row.append(f"  event: {str(text)[:160]}")
        lines.append("\n".join(row))
    if board:
        lines.append("HIVE BOARD (latest):\n" + "\n".join(
            f"  {m.get('from')} -> {','.join(m.get('to') or [])}: {str(m.get('text'))[:240]}" for m in board[-12:]))
    if verify:
        lines.append(f"Delivery check `{verify.get('command')}`: {verify.get('state')}")
    return "\n".join(lines)


def codex_tokens(*texts: str) -> int:
    """The total `codex exec` prints at its end ("tokens used" and then a number such as 16.496 or 1,234), so that a queen's or a
    planner's own Codex calls are counted like every worker's. 0 when it printed none."""
    import re

    for text in texts:
        found = re.findall(r"tokens used\s*[:\n ]\s*([\d.,]+)", text or "")
        if found:
            digits = re.sub(r"\D", "", found[-1])
            return int(digits) if digits else 0
    return 0


def _parse(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return {}


def _look_codex(state: str, folder: str, timeout_s: float, model: str = "") -> dict[str, Any]:
    """The same look, with Codex as the queen: read-only sandbox, the answer taken from its last message."""
    import tempfile

    codex = binary_for("codex")
    if not codex:
        raise RuntimeError("Codex is not installed")
    with tempfile.TemporaryDirectory(prefix="queenbee-director-") as tmp:
        answer = os.path.join(tmp, "answer.txt")
        argv = [codex, "exec", *(["-m", model] if model else []), "--sandbox", "read-only", "--skip-git-repo-check",
                "--output-last-message", answer]
        if folder and os.path.isdir(folder):
            argv += ["--cd", os.path.abspath(folder)]
        proc = subprocess.run(argv + ["-"], input=PROMPT.format(state=state), capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout_s, cwd=tmp,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            with open(answer, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            raise RuntimeError((proc.stderr or proc.stdout or "Codex gave no answer").strip()[-300:]) from exc
    raw = _parse(text)
    msgs = [{"to": m.get("to") or "all", "text": str(m["text"]).strip()[:600]}
            for m in raw.get("messages") or [] if isinstance(m, dict) and str(m.get("text") or "").strip()]
    return {"messages": msgs[:4], "summary": str(raw.get("summary") or "")[:200], "cost_usd": 0.0,
            "tokens": codex_tokens(proc.stdout, proc.stderr)}


def look(state: str, model: str, folder: str, timeout_s: float = 180, agent: str = "claude",
         codex_model: str = "") -> dict[str, Any]:
    """One look at the hive. Returns {"messages": [...], "summary": str, "cost_usd": float, "tokens": int}."""
    if agent == "codex":
        return _look_codex(state, folder, timeout_s, codex_model)
    claude = binary_for("claude")
    if not claude:
        raise RuntimeError("Claude Code is not installed")
    argv = [claude, "-p", "--strict-mcp-config", "--model", model, "--output-format", "json", "--no-session-persistence",
            "--tools", "Read,Glob,Grep", "--allowedTools", "Read,Glob,Grep"]
    if folder and os.path.isdir(folder):
        argv += ["--add-dir", os.path.abspath(folder)]
    proc = subprocess.run(argv, input=PROMPT.format(state=state), cwd=folder if os.path.isdir(folder) else None,
                          capture_output=True, text=True, encoding="utf-8", timeout=timeout_s,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        out = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError((proc.stderr or proc.stdout or "no output").strip()[:300]) from exc
    if out.get("is_error"):
        raise RuntimeError(str(out.get("result") or "the director failed")[:300])
    raw = _parse(str(out.get("result") or ""))
    usage = out.get("usage") or {}
    msgs = []
    for m in raw.get("messages") or []:
        if isinstance(m, dict) and str(m.get("text") or "").strip():
            msgs.append({"to": m.get("to") or "all", "text": str(m["text"]).strip()[:600]})
    return {"messages": msgs[:4], "summary": str(raw.get("summary") or "")[:200],
            "cost_usd": float(out.get("total_cost_usd") or 0),
            "tokens": (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
                      + (usage.get("cache_creation_input_tokens") or 0) + (usage.get("output_tokens") or 0)}


class Director:
    """Runs `look` for one queen's dispatch while workers are active: on a timer, and sooner when something happens.

    The timer (`interval_s`) is the fallback. An *event* pulls the next look forward to a few seconds from now: a worker
    posting to the queen or to everyone on the hive board, or a worker's state changing (done, blocked, failed). Looks stay
    at least `min_gap_s` apart, so a burst of events costs one look. Measured: a look costs
    about 7 k tokens and the interval is a latency lever, not a cost one, which is why waking on events is the default."""

    def __init__(self, get_dispatch, model: str = "sonnet", interval_s: float = 120, budget=None,
                 agent: str = "claude", task=None, wake_on_events: bool = True, min_gap_s: float = 45) -> None:
        self.task = task                          # callable -> the job as the user wrote it
        self.get_dispatch, self.model, self.interval_s, self.agent = get_dispatch, model, interval_s, agent
        self.wake_on_events, self.min_gap_s = wake_on_events, min_gap_s
        self.wakes = 0                            # looks pulled forward by an event
        self.last_look_at = 0.0
        self._seen_board = 0                      # board entries already judged for events
        self._seen_status: dict[int, str] = {}    # job priority -> status at the last watch
        self.codex_model = ""
        self.budget = budget                     # callable -> True while the queen may still spend
        self.last_digest = ""
        self.looks = 0
        self.cost_usd = 0.0
        self.tokens = 0
        self.last_summary = ""
        self.next_at = time.time() + 20          # the first look soon after workers start
        self.busy = False
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def status(self) -> dict[str, Any]:
        return {"model": self.model, "agent": self.agent, "looks": self.looks, "cost_usd": round(self.cost_usd, 3), "tokens": self.tokens,
                "summary": self.last_summary, "next_in": max(0, int(self.next_at - time.time())),
                "interval_s": self.interval_s, "busy": self.busy, "wakes": self.wakes, "wake_on_events": self.wake_on_events,
                "min_gap_s": self.min_gap_s}

    def events_pending(self, d) -> bool:
        """True when, since the last watch, a worker spoke to the queen or to everyone, or a task's state changed."""
        found = False
        board = list(getattr(d, "board_log", None) or [])
        if len(board) < self._seen_board:                       # the log was cut or replaced: start over
            self._seen_board = 0
        for m in board[self._seen_board:]:
            sender = str(m.get("from") or "")
            tos = [str(t).lower() for t in (m.get("to") or [])]
            if sender != "queen" and any(t in ("queen", "all", "q") for t in tos):
                found = True
        self._seen_board = len(board)
        now_status = {int(j["priority"]): str(j.get("status") or "") for j in d.view() if j.get("kind") == "task"}
        if self._seen_status and any(now_status.get(k) != v for k, v in self._seen_status.items()) \
                or (self._seen_status and set(now_status) - set(self._seen_status)):
            found = True
        self._seen_status = now_status
        return found

    def nudge(self) -> None:
        """Pull the next look forward: a few seconds from now, but never closer than `min_gap_s` to the last look."""
        earliest = max(time.time() + 5, self.last_look_at + self.min_gap_s)
        if earliest < self.next_at:
            self.next_at = earliest
            self.wakes += 1

    def _watch(self) -> None:
        if not self.wake_on_events:
            return
        d = self.get_dispatch()
        if d is None:
            return
        try:
            if self.events_pending(d):
                self.nudge()
        except Exception as exc:                   # watching must never stop the loop
            log.debug("director watch failed: %s", exc)

    def run(self) -> None:
        while not self._stop:
            time.sleep(2)
            self._watch()
            if time.time() < self.next_at:
                continue
            self.next_at = time.time() + self.interval_s
            try:
                self.tick()
            except Exception as exc:               # a failed look must never stop the hive
                log.warning("director look failed: %s", exc)
                d = self.get_dispatch()
                if d is not None:
                    d._event_all(f"the queen could not look in on the hive: {str(exc)[:160]}")

    def tick(self) -> None:
        d = self.get_dispatch()
        if d is None:
            return
        jobs = d.view()
        active = [j for j in jobs if j.get("kind") == "task" and j.get("status") not in d.ENDED]
        if not active:
            return                                  # nothing running: no tokens spent
        if self.budget is not None and not self.budget():
            return
        state = digest(jobs, d.board_log, d.verify or None)
        job_text = (self.task() if self.task else "") or ""
        if job_text.strip():
            state = "THE JOB, as the user wrote it:\n" + job_text.strip()[:3000] + "\n\n" + state
        if state == self.last_digest:
            return                                  # nothing changed since the last look
        self.last_digest = state
        folder = next((j["project_path"] for j in jobs if j.get("project_path")), "")
        self.busy = True
        self.last_look_at = time.time()
        try:
            result = look(state, self.model, folder, agent=self.agent, codex_model=self.codex_model)
        finally:
            self.busy = False
        self.looks += 1
        self.cost_usd += result["cost_usd"]
        self.tokens += result["tokens"]
        self.last_summary = result["summary"]
        with d.lock:
            d.costs["director"] = {"tokens": int((d.costs.get("director") or {}).get("tokens", 0)) + result["tokens"],
                                   "cost_usd": round(self.cost_usd, 4)}
        d._event_all(f"the queen looked in: {result['summary'] or 'all fine'}")
        subs = {j["priority"]: [c["priority"] for c in jobs if c.get("parent") == j["priority"]]
                for j in jobs if j.get("role") == "mother"}
        for m in result["messages"]:
            to = m["to"]
            tos = [to] if isinstance(to, str) else list(to)
            expanded = []
            for t in tos:                           # a message to a sub-queen reaches her whole hive
                num = int(t[1:]) if isinstance(t, str) and t[:1] in "PpWw" and t[1:].isdigit() else None
                expanded += [f"W{c}" for c in subs[num]] if num in subs and subs[num] else [t]
            d.say(expanded if len(expanded) > 1 else expanded[0], m["text"], "queen")
        self.last_digest = state if not result["messages"] else self.last_digest    # her own words are not news
        if result["messages"]:
            job_text = (self.task() if self.task else "") or ""
            head = ("THE JOB, as the user wrote it:\n" + job_text.strip()[:3000] + "\n\n") if job_text.strip() else ""
            self.last_digest = head + digest(d.view(), d.board_log, d.verify or None)
