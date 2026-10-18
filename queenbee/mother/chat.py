"""Talking the job through with the queen before any worker starts.

One question-and-answer turn at a time: the queen reads the project, asks what she needs (what "done" means, what she may
approve on the user's behalf, what to leave alone) and, when she has enough, answers with a line `READY:` followed by the
instruction she will plan from. The user can edit that instruction before it is used. Nothing is started here.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any

from ..agents.catalog import binary_for

MAX_MESSAGES = 24
MAX_TEXT = 4000

SYSTEM = """You are the queen bee of QueenBee: you will direct a team of coding agents (workers) for the user. Before any
worker starts, the user talks the job through with you. You have read the project (below) and have no tools.

Your part in this talk:
- Find out what you need to run the job without coming back to the user: the goal, what "done" means (a command that
  must pass is best), what you may approve on the user's behalf (usually: everything inside the project folder) and what
  you must leave alone or refuse (deleting, downloading, installing, other folders), and any limit on workers or tokens.
- Ask at most three short questions at a time, only about what is missing and cannot be read from the project.
- When it helps, say in one sentence how you would split the work (how many workers, which kind of model for which part:
  a cheap model for plain parts, a strong one for hard parts) and why. Do not claim anything you cannot know.
- Answer in the language of the user's last message (English for English, Turkish for Turkish), briefly and plainly.
- When you have enough (or the user says to go ahead), end your reply with a line that starts with READY: followed on the
  next lines by the complete instruction you will plan from: the goal, the definition of done, the scope and limits agreed.
  Write it in the user's own words and name the files. Do not write READY: before you are ready.
"""

RUNNING = """

The work has already started: the hive is below. Now the user talks to you as the queen of a running team. Answer what they ask
about it plainly and only from what the hive shows (who is doing what, what is done, what is stuck, what it costs). When they
want a change of direction, say what you would change and which worker it concerns; you cannot act from this talk, so tell them
to use the worker's box or the hive board for it. Never write READY: now."""


def _project(folder: str, said: str = "") -> str:
    """The project as the queen sees it: its tree, and the files the user has named so far in the talk."""
    if not folder or not os.path.isdir(folder):
        return "(no project folder given yet)"
    from .planner import _given

    return _given(folder, said)[:60_000] or "(the folder is empty)"


def _clean(messages: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for m in (messages if isinstance(messages, list) else [])[-MAX_MESSAGES:]:
        if not isinstance(m, dict):
            continue
        role = "queen" if m.get("role") == "queen" else "user"
        text = str(m.get("text") or "").strip()[:MAX_TEXT]
        if text:
            out.append({"role": role, "text": text})
    return out


def split_ready(reply: str) -> tuple[str, str]:
    """(what to show, the instruction) -- the instruction is empty until the queen has written READY:."""
    lines = reply.splitlines()
    for i, line in enumerate(lines):
        if line.strip().upper().startswith("READY:"):
            first = line.strip()[len("READY:"):].strip()
            instruction = "\n".join(([first] if first else []) + lines[i + 1:]).strip()
            return "\n".join(lines[:i]).strip(), instruction
    return reply.strip(), ""


def ask_model(prompt: str, model: str, timeout_s: float = 150, agent: str = "claude",
              codex_model: str = "") -> dict[str, Any]:
    """One answer with no tools and no session, the prompt on stdin: {"ok", "text", "tokens"}. From Claude Code, or from
    Codex when the queen is a Codex queen or Codex is the only agent on this computer."""
    claude = binary_for("claude")
    if agent == "codex" or (not claude and binary_for("codex")):
        return _ask_codex(prompt, codex_model, timeout_s)
    if not claude:
        return {"ok": False, "error": "Neither Claude Code nor Codex is installed: the queen needs one to talk (see Get started)"}
    argv = [claude, "-p", "--strict-mcp-config", "--model", model or "sonnet", "--output-format", "json", "--tools", "", "--no-session-persistence"]
    with tempfile.TemporaryDirectory(prefix="queenbee-chat-") as cwd:
        try:
            proc = subprocess.run(argv, input=prompt, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                                  timeout=timeout_s, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "the queen took too long to answer; try again"}
        except OSError as exc:
            return {"ok": False, "error": f"could not start Claude Code: {exc}"}
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        return {"ok": False, "error": (proc.stderr or proc.stdout or "no answer").strip()[:300]}
    if out.get("is_error"):
        return {"ok": False, "error": str(out.get("result") or "the queen could not answer")[:300]}
    usage = out.get("usage") or {}
    tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                                 "cache_read_input_tokens"))
    return {"ok": True, "text": str(out.get("result") or ""), "tokens": tokens}


def _ask_codex(prompt: str, model: str, timeout_s: float) -> dict[str, Any]:
    """`ask_model` with Codex: a read-only sandbox in an empty folder, the answer taken from its last message."""
    from .director import codex_tokens

    codex = binary_for("codex")
    if not codex:
        return {"ok": False, "error": "Codex is not installed"}
    with tempfile.TemporaryDirectory(prefix="queenbee-chat-") as cwd:
        answer = os.path.join(cwd, "answer.txt")
        argv = [codex, "exec", *(["-m", model] if model else []), "--sandbox", "read-only", "--skip-git-repo-check",
                "--output-last-message", answer, "-"]
        try:
            proc = subprocess.run(argv, input=prompt, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                                  timeout=timeout_s, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "the queen took too long to answer; try again"}
        except OSError as exc:
            return {"ok": False, "error": f"could not start Codex: {exc}"}
        try:
            with open(answer, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return {"ok": False, "error": (proc.stderr or proc.stdout or "Codex gave no answer").strip()[-300:]}
    if not text.strip():
        return {"ok": False, "error": "Codex gave no answer"}
    return {"ok": True, "text": text, "tokens": codex_tokens(proc.stdout, proc.stderr)}


COMPLETION = """You are the queen bee of QueenBee. Your team has stopped. Write the completion report for the user from the
facts below, and only from them. Keep it under 350 words, plain, in the language of the task. Use these headings:

**What was asked** (one or two lines)
**What was done** (one line per worker: what it made or changed; name the files that matter)
**How it was checked** (say what YOU ran and what it showed, and keep it apart from what a worker only claims)
**Not proven or left over** (anything failed, blocked, unchecked, or that a worker reported but nothing verified; "nothing" only if true)
**Time, cost, questions to you** (minutes, tokens, how many questions reached the user and how many you answered)

Never say the work is complete or correct beyond what a check showed. If there was no delivery check, say so.

THE FACTS
"""


def complete(facts: str, model: str, timeout_s: float = 180, agent: str = "claude", codex_model: str = "") -> dict[str, Any]:
    """The queen's completion report written from `facts`: {"ok", "text", "tokens"}."""
    return ask_model(COMPLETION + facts, model, timeout_s, agent, codex_model)


def reply(messages: Any, folder: str, model: str, timeout_s: float = 150, hive: str = "", agent: str = "claude",
          codex_model: str = "") -> dict[str, Any]:
    """The queen's next turn: {"ok", "reply", "ready"} -- `ready` is the instruction once she wrote READY:, else "". From
    Claude Code, or from Codex for a Codex queen or when Codex is the only agent here (as `ask_model`)."""
    msgs = _clean(messages)
    if not msgs or msgs[-1]["role"] != "user":
        return {"ok": False, "error": "say something to the queen first"}
    talk = "\n\n".join(f"{'The user' if m['role'] == 'user' else 'You (the queen)'}: {m['text']}" for m in msgs)
    said = " ".join(m["text"] for m in msgs if m["role"] == "user")
    prompt = (f"{SYSTEM}{RUNNING if hive else ''}\nThe project:\n{_project(folder, said)}\n\n"
              f"{('The hive right now:' + chr(10) + hive + chr(10) + chr(10)) if hive else ''}The talk so far:\n\n{talk}\n\n"
              "Write only your next turn as the queen.")
    out = ask_model(prompt, model, timeout_s, agent, codex_model)
    if not out.get("ok"):
        return out
    shown, instruction = split_ready(str(out.get("text") or ""))
    if hive:
        shown, instruction = (shown + ("\n" + instruction if instruction else "")).strip(), ""
    return {"ok": True, "reply": shown or "(ready)", "ready": instruction, "tokens": out.get("tokens", 0)}
