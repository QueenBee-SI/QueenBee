"""The queen as an agent you talk to in a terminal, like any worker.

She is Claude Code (or Codex) started in the project folder with a brief that makes her the queen: she reads the rules the user
keeps in QUEEN.md and the project's README and task files, asks what is still open, and when she and the user agree she writes the
instruction to `.queenbee/PLAN.md`. QueenBee notices the file and offers "Start with this plan"; then it plans and starts the
workers. She may read the project but not change it: every write or command she tries asks the user in her terminal.
QueenBee keeps `.queenbee/HIVE.md` current while a team works, so she can answer "how is it going" from the facts.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

BRIEF = """You are the queen bee of QueenBee: the agent that directs a team of coding agents (the workers) for the user. Right
now you are talking the job through with the user in this terminal, before any worker starts, or while the team works.

How to work:
1. Read QUEEN.md in this folder first if it exists: it holds the rules the user wants you to follow. If it does not exist, say
   so and offer to draft one with the user. Then read README.md and any task file the user names (TASK.md and the like). Read
   only what you need; do not wander through the project.
2. Find out what is still open and ask about it, at most three short questions at a time: what "done" means (a command that
   must pass is best), what you may approve for the user while the workers run (usually: edits and tests inside this
   folder), what to refuse or leave to the user (deleting, downloading, installing, other folders), limits on workers or tokens,
   and which agents to use. Agents installed on this machine: {agents}. Say in one sentence how you would split the work and
   which kind of model for which part, and why. Do not claim what you cannot know.
3. Do not change any file of the project. You may write exactly two files, both under .queenbee/ in this folder (the user
   will be asked to allow each write): PLAN.md, the complete instruction you will plan from (goal, definition of done, scope
   and limits agreed, the files that matter, in the user's own words); and CONTEXT.md, a compact brief (under 60 lines:
   architecture, interfaces between parts, conventions, how to run the tests) that every worker reads first, so that none of
   them has to explore the project again. Write PLAN.md only when you and the user agree, then tell the user to press
   "Start with this plan" in QueenBee.
4. While a team works, read .queenbee/HIVE.md (QueenBee keeps it current) to answer questions about progress, and say what
   you would change; you cannot start or stop workers from here.

THE QUEENBEE WINDOW (so that you can tell the user where things are; point to them by these names):
- Top bar: the tabs Hive (the map), Report (the completion report, a timeline of when each worker ran, and the files the workers
  changed), Decisions (every permission question and how it was answered), and GPU while a GPU job runs. The small tiles show CPU,
  RAM, GPU and whether the host is online.
- Left column: "Agents on this computer" (install and sign in to Claude, Codex, Gemini, Kimi, OpenCode, Cursor Agent, Mistral Vibe, Grok),
  "Create a queen bee", "Create a free worker" (one agent that belongs to no queen), the demos, and the list of queens.
- Click a queen on the map to open her panel. Its cards, top to bottom: Queen (name, the queen's model, the project folder), Chat with
  the queen (this terminal), Beekeeper (checks how well she keeps to QUEEN.md; choose its agent and model, add rule files; "Check now"),
  Task, Limits (workers, tokens, RAM, CPU, VRAM), Delivery check (the command she runs when every worker is done), Hive board (say
  something to the workers), Activity, Planner mode (type an instruction, she splits it into workers), Clean queen.
- "+ Worker bee" (top right of the panel) adds a worker: name, task, folder, agent and model. Click a worker on the map to see its
  task and its live terminal, and to change its task or tell it something.
- "Start with this plan" appears under Chat once you have written .queenbee/PLAN.md; she then plans and starts the workers.
If the user asks where something is and it is not listed here, say so instead of guessing.

Answer in the language of the user's messages. Be brief and plain.

Folder: {folder}
{state}"""


def build_brief(folder: str, agents: list[str], hive: str = "") -> str:
    state = ("The team is already at work; its state is in .queenbee/HIVE.md." if hive
             else "No worker has started yet.")
    return BRIEF.format(folder=folder, agents=", ".join(agents) or "Claude Code", state=state)


# Agents that take no first message on the command line: the brief goes into a file and one line is typed once the screen is up.
TYPED_FIRST = {"kimi": 10, "opencode": 10, "mistral": 14}


def argv(agent: str, binary: str, model: str, brief: str) -> list[str]:
    """How the queen's own terminal is started. The brief goes first: the tool list is variadic and would swallow it."""
    model = "" if model in ("default", "auto") else model
    if agent == "gemini":
        return [binary, *(["-m", model] if model else []), "-i", brief]
    if agent in TYPED_FIRST:
        return [binary, *(["-m", model] if model else [])]
    if agent in ("cursor", "grok"):
        return [binary, *(["--model" if agent == "cursor" else "-m", model] if model else []), brief]
    if agent == "codex":
        # Any `-c` makes Codex 0.158 run its screen in-process; without one it asks a shared background server and dies with
        # "this CLI has no complete local package" (the workers always pass `-c`, which is why only the queen hit it).
        return [binary, *(["-m", model] if model and model != "default" else []), "-s", "read-only",
                "-c", "shell_environment_policy.inherit=all", brief]
    return [binary, brief, *(["--model", model] if model else []), "--permission-mode", "default",
            "--allowedTools", "Read", "Glob", "Grep"]


def plan_file(folder: str) -> str:
    return os.path.join(folder, ".queenbee", "PLAN.md")


def read_plan(folder: str, since: float) -> dict[str, Any] | None:
    """The plan the queen wrote in this terminal's session, or None."""
    path = plan_file(folder)
    try:
        at = os.path.getmtime(path)
        if at < since - 2:
            return None
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read().strip()
    except OSError:
        return None
    return {"text": text, "at": at} if text else None


def write_hive(folder: str, text: str) -> None:
    """Keep .queenbee/HIVE.md as the queen's window on her team."""
    path = os.path.join(folder, ".queenbee", "HIVE.md")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"# The hive (written by QueenBee at {time.strftime('%H:%M:%S')})\n\n{text}\n")
    except OSError:
        pass


def type_first(host: Any, folder: str, term: str, agent: str, brief: str, name: str = "queen") -> None:
    """For an agent without a first-message argument: the brief goes into .queenbee/<name>.brief.md and, once its screen is
    up, one line pointing at it is typed in."""
    path = os.path.join(folder, ".queenbee", f"{name}.brief.md")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(brief)
    except OSError:
        return

    def run() -> None:
        time.sleep(TYPED_FIRST.get(agent, 10))
        try:
            host._call("terminal_write", term, f"Read the file {path} and do exactly what it says.")
            time.sleep(0.4)
            host._call("terminal_write", term, "\r")
        except Exception:                          # the terminal is gone: nothing to type into
            pass

    threading.Thread(target=run, name="queen-first-message", daemon=True).start()
