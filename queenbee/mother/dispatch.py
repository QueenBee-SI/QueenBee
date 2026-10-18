"""Plan -> one visible agent terminal per job, started with a single message.

The message carries the brief and the goal together, so no model turn is spent
on "ready, what's the goal?". Priority 1 runs as the primary GPU job, the rest
as secondaries. A job marked `same_as` another starts only once that job's
agent has submitted its workload: two agents adapting the same script at the
same time would edit the same files. It then needs no agent at all: Mother
runs its prepared submit line, and the GPU controller decides by VRAM whether
it trains alongside or waits.

Progress is read from the host -- which terminal is whose, which GPU job a
terminal submitted (`--owner` in the worker's argv) -- never from what an
agent says about itself.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import re
import subprocess
import threading
import time
from typing import Any, Optional

import psutil

from ..agents.catalog import binary_for
from ..agents.host_client import HostClient, HostUnavailable
from ..gpu import _split_windows, agent_task_prompt
from . import sweep as sweeping
from . import board, branches, verifier
from .config import clamp, model_for, reasoning_for

log = logging.getLogger("queenbee.mother.dispatch")
_OWNER = re.compile(r"--owner\s+(term-\d+)\b")

MOTHER_RULES = """
QueenBee requirements for this job:
- Resumable is mandatory. Pass save= to AdaptiveGPU: a function that atomically
  writes everything needed to continue (model, optimizer, LR schedule, AMP scaler,
  RNG state, epoch/step or the set of finished items) and returns only when it is
  on disk. Also pass release=: a function that moves the model and optimizer
  state off the GPU and empties the CUDA cache (QueenBee refuses a secondary job
  without it), and restore=: its inverse, moving them back to the GPU (QueenBee
  calls it before a released job runs again). Make the script load that state at start when it exists. QueenBee may then
  stop the job (it saves and exits) and start the same command again later. If the
  workload cannot be made resumable safely, do not launch it; report the blocker.
- After you submit, report the QueenBee terminal id and stop. QueenBee monitors the job;
  do not poll logs or GPU status yourself.
"""

SAME_AS = """
Another agent has already adapted this workload in this project for job {ref}
("{title}") and submitted it as QueenBee terminal {term}. Do not edit any files.
Reuse that exact launch command with this job's own name, output location and
QueenBee role, and submit it."""

SAME_FOLDER = """
Another agent already worked in this project folder for job {ref} ("{title}")
and submitted it as QueenBee terminal {term}; its changes, including resumable
support, are in place. Do not redo or undo them. Change only what this job's
own goal needs, keep the resumable support, use this job's own output location,
and submit."""


WORKER_BRIEF = """You are a QueenBee worker for job W{n} ("{title}") in {folder}.
The architect already inspected this project and decided everything. Follow the
work order exactly. Do not explore beyond what a step needs, do not redesign, and
do not ask questions: if a step is impossible, report it as blocked (below).
{before}{retry}
Work order:
{order}

Submit line for this job:
{command}

Decisions the user made:
{answers}
"""

REPORT_RULES = """
When finished, write the file .queenbee/W{n}.json in the project folder:
{{"status": "submitted", "terminal": "<QueenBee terminal id from the submit output>",
 "command": "<the submit line you ran>", "changed_files": ["..."], "notes": ""}}
If you cannot complete the work, write status "blocked" with the reason in notes
and do not submit. Then stop: QueenBee monitors the job; do not poll logs or GPU status.
"""


TASK_BRIEF = """You are a worker bee for job W{n} ("{title}") in {folder}, {cluster}of the
queen bee "{queen}" in QueenBee. Your queen is responsible for the whole work
package; you do your part of it and report to her. Your task:

{goal}
{siblings}{map}
Work inside this folder. Do not ask questions; where the task leaves a choice,
take the sensible default and say so in your notes.
{scope}{progress}
{comms}When finished, write the file .queenbee/W{n}.json in the project folder:
{{"status": "done", "changed_files": ["..."], "notes": "what you did, briefly"}}
If you cannot do it, write status "blocked" with the reason in notes.
Then stop and wait: the user may send you follow-ups in this conversation.
"""

PLAIN_BRIEF = """{goal}

Folder: {folder}. Work here. After each meaningful step, append one short line to .queenbee/W{n}.progress (what you just
finished, what is next): QueenBee shows it live. When the work is complete, write the file .queenbee/W{n}.json in this folder:
{{"status": "done", "changed_files": ["..."], "notes": "what you did, briefly"}}
If you stop before it is complete, write status "blocked" with the reason in notes. QueenBee reads that file to know you have
ended; it asks nothing else of you.
"""

PROGRESS = {
    # every step: the queen's live view, one extra tool call per step
    "step": ("Keep your queen informed as you go: after each meaningful step, append one short\n"
             "line to .queenbee/W{n}.progress -- what you just finished and what is next.\n"
             "She reads it live."),
    # a few lines: cheaper, still a live view of where the worker is
    "few": ("Keep your queen informed with at most three short lines appended to .queenbee/W{n}.progress "
            "in all:\nwhen you start (your plan), at the half-way point, and just before you finish. "
            "Do not write more."),
}

COMMS = """
You are not alone: the queen and the other workers follow a shared message board.
- When you learn something another part needs (a decision, an interface you
  changed, a limit you found, a bug in someone's part), tell them at once by
  appending ONE line to .queenbee/board.jsonl:
  {{"from": "W{n}", "to": "all", "text": "<what they need to know, one or two sentences>"}}
  ("to" may also be "queen", "W<number>", or a list of these.) Post findings, not progress.
- A finding that everyone who works here later must know (a fact about the environment, an interface you settled, a way
  that failed and why) gets "kind": "finding" in that line; QueenBee keeps it in .queenbee/FINDINGS.md for every later worker.
- Between your steps, read .queenbee/inbox/W{n}.md if it exists: messages to you from
  the queen and the others. Act on what applies to your part; you need not answer.
- Never wait for the board, your inbox or another worker (no loop, sleep or watcher on .queenbee): while you
  wait you read nothing. If a part you need is not announced yet, build on what your task and the queen say,
  post the shape you assumed, and finish; the queen tells you if it changes.
"""


def run_captured(argv: list[str], cwd: str, timeout: float) -> tuple[Optional[int], str]:
    """Run a command with its output going to a file, not a pipe -- a server a
    test left running would keep a pipe open and outlive any timeout -- and
    kill its whole process tree when it is done or out of time."""
    import tempfile

    with tempfile.TemporaryFile(mode="w+b") as out:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            code: Optional[int] = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            code = None
        finally:
            try:
                root = psutil.Process(proc.pid)
                for child in root.children(recursive=True) + [root]:
                    try:
                        child.kill()
                    except psutil.Error:
                        pass
            except psutil.Error:
                pass
        out.seek(0)
        return code, out.read().decode("utf-8", "replace")


def _folder(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)) if path else ""


class Dispatch:
    """One plan being carried out. Thread-safe snapshot via `view()`."""

    first_priority = 1       # the number a first added worker gets (W<n>): a free worker sharing a folder starts higher

    def __init__(self, jobs: list[dict[str, Any]], cfg: dict[str, Any], host: HostClient,
                 ui_port: int = 7878) -> None:
        self.jobs = [{"parent": None, "detached": False, "role": None,
                      **j, "status": "queued", "terminal": None, "workload": None, "error": None}
                     for j in sorted(jobs, key=lambda j: j["priority"])]
        self.cfg = cfg
        self.host = host
        self.ui_port = ui_port
        self.asks_user = False       # the user answers the workers' prompts: they start with the vendor's own prompts on
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.held = False            # restored after a close with no worker running: nothing starts until the user says so
        self.started_at = time.time()
        # terminal id -> (state, reason) as QueenBee reads the agent's screen;
        # set by the Mother server, which has that view.
        self.state_of = lambda terminal: ("", "")
        self.compare: Optional[dict[str, str]] = None      # what Mother ranks the jobs by
        # For the report: the instruction, what planning cost, GPU busy/idle
        # seconds while the plan ran, and the report once written.
        self.instruction = ""
        self._board_at: dict[str, int] = {}
        # Branch-per-worker mode: {"root": the project, "main": its branch}; None = a shared folder.
        self.branches: Optional[dict[str, str]] = None
        self.merge_check = ""          # run on main after each merge; the delivery check when empty
        self._merge_lock = threading.Lock()
        # A quality pass once the parts built side by side are all in: set to the goal text to enable.
        self.qa_goal = ""
        self._qa_started = False
        self._synced_head = ""
        self.board_log: list[dict[str, Any]] = []
        self.findings: list[dict[str, Any]] = []
        self.costs: dict[str, Any] = {}
        self.gpu = {"busy_s": 0.0, "idle_s": 0.0}
        self.report: Optional[str] = None
        self.finished_at = 0.0
        # A sweep: lanes of values for one setting, run from both ends (see
        # sweep.py). The first job is the first value of the first lane.
        self.sweep: Optional[dict[str, Any]] = None
        self.approvals: list[dict[str, Any]] = []      # what Mother answered, and what it left
        # A job marked as a Mother of its own branch plans its own agents with
        # this: (instruction, folder) -> {"jobs": [...], "costs": {...}}. The
        # server sets it (planner + architect); tests pass a stand-in.
        self.branch_planner = None
        # The queen's check of the whole package once its workers are done:
        # a test command, run by the queen itself (no tokens); failures go to
        # one integration worker, twice at most.
        self.verify: dict[str, Any] = {}
        # The queen's limits: (job, all jobs) -> why it must wait, or None.
        # Called before any worker starts; the server sets it.
        self.gate = None
        self._thread: Optional[threading.Thread] = None
        # Several queens may run at once, each with its own plan. Only the
        # first queen's P1 is the machine's primary GPU job; every other
        # queen's jobs share the GPU as secondaries.
        self.queen = "q1"
        self.queen_name = "Queen 1"
        self.primary = True

    # -- events: what happened to each job, for the report ------------------

    def _event_all(self, text: str) -> None:
        log.info("mother: %s", text)
        if self.sweep is not None:
            self.sweep.setdefault("log", []).append([round(time.time()), text])

    @staticmethod
    def _event(job: dict[str, Any], text: str) -> None:
        job.setdefault("events", []).append([round(time.time()), text])
        log.info("mother: P%s %s", job["priority"], text)

    # -- launching ---------------------------------------------------------

    # A worker's scope. Inside it nothing asks for approval: file edits in the
    # project folder are accepted (--permission-mode acceptEdits) and these
    # commands are allowed. Outside it, the listed tools are refused outright;
    # anything else still asks, and Mother's approver decides or asks the user.
    ALLOWED = ("Read,Glob,Grep,Edit,Write,MultiEdit,"
               "Bash(python:*),Bash(python.exe:*),Bash(py:*),PowerShell(python:*),"
               "Bash(ls:*),Bash(dir:*),Bash(cat:*),Bash(type:*),Bash(head:*),Bash(tail:*),"
               "Bash(mkdir:*),Bash(grep:*),PowerShell(Get-ChildItem:*),PowerShell(Get-Content:*)")
    DENIED = ("WebFetch,WebSearch,Bash(rm:*),Bash(del:*),Bash(rmdir:*),Bash(rd:*),"
              "Bash(curl:*),Bash(wget:*),Bash(git push:*),Bash(pip install:*),"
              "PowerShell(Remove-Item:*),PowerShell(Invoke-WebRequest:*)")

    def _argv(self, job: dict[str, Any], prompt: str) -> list[str]:
        binary = binary_for(job["agent"])
        if not binary:
            raise RuntimeError(f"{job['agent']} is not installed")
        if job["agent"] == "claude":
            model = clamp(job.get("tier") or model_for(self.cfg, "claude", job["effort"]),
                          self.cfg.get("max_model") or "")
            # The prompt goes first: the tool lists are variadic options and
            # would swallow a positional argument after them.
            if job.get("plain"):
                # Claude Code as a person usually runs it: nothing pre-approved, so every read, edit and
                # command asks. Used for the "one agent alone" runs.
                return [binary, prompt, *(["--model", model] if model else []), "--permission-mode", "default"]
            settings = self._hook_settings(job)
            if self.asks_user:
                # The user answers: nothing is pre-approved or pre-refused, so every edit and command shows Claude Code's own
                # prompt. The hook stays, answering "ask" while the user answers and by the queen's rule if she takes over.
                return [binary, prompt, *(["--model", model] if model else []), "--permission-mode", "default",
                        *(["--settings", settings] if settings else [])]
            return [binary, prompt, *(["--model", model] if model else []),
                    "--permission-mode", "acceptEdits",
                    "--allowedTools", self.ALLOWED, "--disallowedTools", self.DENIED,
                    *(["--settings", settings] if settings else [])]
        if job["agent"] == "gemini":
            # `-i` runs the prompt and stays interactive; the default approval mode asks before it edits or runs.
            tier = job.get("tier")
            return [binary, *(["-m", tier] if tier not in (None, "", "default") else []), "-i", prompt]
        if job["agent"] in self.TYPED_FIRST:
            # No flag for a first message in the interactive screen: it is typed once the screen is up (see _launch),
            # so the agent starts with nothing but its own model.
            tier = job.get("tier")
            return [binary, *(["-m", tier] if tier not in (None, "", "default") else [])]
        if job["agent"] in ("cursor", "grok"):
            tier = job.get("tier")                    # `cursor-agent "task"` / `grok "task"`: the task is the first turn
            return [binary, *(["--model" if job["agent"] == "cursor" else "-m", tier] if tier not in (None, "", "default") else []),
                    prompt]
        model = (job.get("tier") if job.get("tier") not in (None, "", "default")
                 else model_for(self.cfg, job["agent"], job["effort"]))
        return [binary, *(["-m", model] if model else []), "-s", "workspace-write",
                # A team worker never waits on a person: the sandbox is the queen's rule (inside the folder yes, outside no);
                # what it blocks comes back to the model as a failure. One agent alone keeps Codex's own prompts.
                # When the user answers the prompts, Codex asks whenever it judges a command needs approval
                # (on-request: codex-cli 0.162 takes only on-request and never; it has no "untrusted").
                *([] if job.get("plain") else ["-a", "on-request"] if self.asks_user else ["-a", "never"]),
                "-c", "shell_environment_policy.inherit=all",
                "-c", f"model_reasoning_effort={reasoning_for(self.cfg, 'codex', job['effort'])}", prompt]

    def _hook_settings(self, job: dict[str, Any]) -> str:
        """A settings file for Claude Code that names the queen's hook: before every edit or command the worker asks her
        server, which answers by her rules; the screen is no longer read for these. Written into the project's .queenbee
        folder; "one agent alone" gets none (its prompts stay the vendor's, so the comparison stays honest)."""
        if job.get("plain"):
            return ""
        folder = os.path.join(job["project_path"], ".queenbee")
        path = os.path.join(folder, f"W{job['priority']}.claude-settings.json")
        exe = sys.executable.replace("\\", "/")
        if getattr(sys, "frozen", False):
            runner = f'"{exe}" -m queenbee.mother.hook'                  # QueenBee.exe runs its own modules
        else:
            # the hook is stdlib-only, so it runs as a file from any folder, whether or not the package is installed
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook.py").replace("\\", "/")
            runner = f'"{exe}" "{script}"'
        from . import auth

        token = auth.read_token(self.ui_port + 2)
        command = f"{runner} --port {self.ui_port + 2} --job {job['priority']}" + (f" --token {token}" if token else "")
        settings = {"hooks": {"PreToolUse": [{"matcher": "Bash|PowerShell|Monitor|Edit|Write|MultiEdit|NotebookEdit|WebFetch|WebSearch",
                                             "hooks": [{"type": "command", "command": command, "timeout": 30}]}]}}
        try:
            os.makedirs(folder, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(settings, fh, indent=1)
        except OSError:
            return ""
        return path

    def _brief(self, job: dict[str, Any], ref: Optional[dict[str, Any]], same_folder: bool) -> str:
        return self._brief_body(job, ref, same_folder) + self._acceptance_note(job) + self._user_notes(job)             + self._python_note(job) + self._context_note(job) + self._findings_note(job)

    @staticmethod
    def _keep_fix_finding(job: dict[str, Any], notes: str) -> bool:
        """What the integration-fix worker found is the seam itself (E1 and E3 of 2026-10-15 found the same one, check 109,
        because nobody had kept it): its closing note goes to FINDINGS.md so the next team's brief carries it."""
        if not str(job.get("title") or "").startswith("integration fix") or not notes.strip():
            return False
        folder = job.get("hive_root") or job.get("project_path") or ""
        return bool(folder) and board.keep_finding(folder, f"W{job['priority']}", notes, source="fix",
                                                   supersedes=board.supersede_hint(notes))

    @staticmethod
    def _findings_note(job: dict[str, Any]) -> str:
        """What earlier workers found out, put into the brief itself: the findings about this worker's files and part first,
        at most twelve, each cut for the brief only; the file keeps them whole. Nothing is learned twice."""
        for folder in (job.get("hive_root"), job.get("project_path")):
            if not folder or not os.path.isfile(os.path.join(folder, ".queenbee", "FINDINGS.md")):
                continue
            words = re.findall(r"[A-Za-z_]{4,}", str(job.get("title") or ""))
            recs = board.relevant_findings(folder, list(job.get("files") or []), words)
            path = os.path.join(folder, ".queenbee", "FINDINGS.md")
            if not recs:
                return (f"\n\nRead {path} first: the findings of the workers before you (environment facts, interface decisions, "
                        "ways that failed and why). Do not learn them again.\n")
            lines = [f"- ({r.get('source_job') or '?'}, {r.get('scope') or 'general'}) {str(r['claim'])[:400]}" for r in recs]
            return ("\n\nWhat the workers before you found out (the ones about your part first; the whole list, with what checked "
                    f"them, is in {path}). Do not learn these again; if one is wrong now, post the correction on the board as a "
                    f"finding that says which it supersedes (its id is in findings.jsonl):\n" + "\n".join(lines) + "\n")
        return ""

    @staticmethod
    def _context_note(job: dict[str, Any]) -> str:
        """The queen's own brief of the project, when she wrote one with the user: read first, so that no worker explores the
        whole project again to find out what the others already know."""
        for folder in (job.get("project_path"), job.get("hive_root")):
            path = os.path.join(folder or "", ".queenbee", "CONTEXT.md")
            if folder and os.path.isfile(path):
                return (f"\n\nStart from the queen's brief of this project: {path}. It holds the architecture, the interfaces "
                        "between the parts and the conventions the queen agreed with the user. Read it first and read other "
                        "files only where you must edit or check them; do not explore the whole project again.\n")
        return ""

    def _python_note(self, job: dict[str, Any]) -> str:
        """Codex's sandbox cannot start the Microsoft Store Python: give its worker a copy it can run."""
        if job.get("agent") != "codex":
            return ""
        from . import pyenv

        exe = pyenv.portable_python()
        if not exe:
            return ""
        return ("\n\nPython: in your shell the command `python` is not found. Run Python with this path instead: "
                f"`{exe}` (for example `{exe} -m unittest discover -s tests`). Wherever these instructions say "
                "`python`, use that path; the checks your queen runs use her own Python.\n")

    @staticmethod
    def _acceptance_note(job: dict[str, Any]) -> str:
        checks = job.get("acceptance") or []
        if not checks:
            return ""
        return ("\nYour queen will run these checks herself when you report done; the part is finished only "
                "when all pass:\n" + "\n".join(f"- {verifier.describe(c)}" for c in checks) + "\n")

    # -- acceptance: done is proved, not claimed -----------------------------

    MAX_ACCEPT = 2

    def _accept(self, job: dict[str, Any]) -> bool:
        """True when a worker's "done" may stand now: it has no checks, or they
        have passed. Otherwise they run in the background (status "verifying")
        and decide the job's fate in _accepted."""
        merging = bool(self.branches and job.get("worktree"))
        if not job.get("acceptance") and not merging:
            return True
        if job.get("accept_state") == "passed":
            return True
        job["status"], job["accept_state"] = "verifying", "running"
        self._event(job, f"reported done; the queen is running its {len(job.get('acceptance') or [])} acceptance check(s)"
                         + (" and merging its branch into main" if merging else ""))
        threading.Thread(target=self._run_accept, args=(job,), name=f"queen-accept-W{job['priority']}",
                         daemon=True).start()
        return False

    def _run_accept(self, job: dict[str, Any]) -> None:
        results = verifier.run_checks(job.get("acceptance") or [], job["project_path"], run_captured)
        bad = verifier.failures(results)
        if not bad and self.branches and job.get("worktree"):
            kept, detail = self._merge_branch(job)
            results.append({"check": f"merge {job['branch']} into main, package check stays green", "ok": kept,
                            "detail": "" if kept else detail})
            if kept:
                self._event(job, detail)
            bad = verifier.failures(results)
        note = None
        with self.lock:
            job["accept_results"] = results
            if job["status"] != "verifying":
                return                                   # stopped by the user meanwhile
            if not bad:
                job.update(status="done", accept_state="passed")
                self._event(job, f"acceptance passed ({len(results)}/{len(results)})")
                return
            text = "\n".join(f"- {r['check']}: {r['detail']}" for r in bad)
            rounds = job.get("accept_round", 0)
            if rounds >= self.MAX_ACCEPT or not self._agent_alive(job):
                job.update(status="failed", accept_state="failed",
                           error=f"acceptance failed after {rounds} fix round(s): " + "; ".join(r["check"] for r in bad))
                self._event(job, "acceptance failed: " + text[:400])
                return
            job.update(status="agent working", accept_state=None, accept_round=rounds + 1)
            self._event(job, f"acceptance failed ({len(bad)}/{len(results)}); sent back to the worker:\n" + text[:400])
            note = ("Your queen ran the acceptance checks for your part and these fail:\n" + text +
                    "\nFix the cause (do not weaken a check), then write your report again.")
            if self.branches and job.get("worktree"):
                note += (f" You work on branch {job['branch']} in your own folder: first bring the others' work in "
                         "with `git merge main`, resolve any conflict, run the tests, then report again.")
            try:
                os.remove(self.report_path(job))         # the old report is no longer true
            except OSError:
                pass
        try:
            self.host._call("terminal_write", job["terminal"], note)
            time.sleep(0.3)
            self.host._call("terminal_write", job["terminal"], "\r")
        except HostUnavailable:
            pass

    @staticmethod
    def _user_notes(job: dict[str, Any]) -> str:
        notes = job.get("user_notes") or []
        if not notes:
            return ("\nAn agent worked on this job before and stopped; what it did is in the folder: check it, keep "
                    "what is right and finish the job.\n" if job.get("ran_before") else "")
        before = ("This job was worked on before and that work is in the folder: keep what is right and "
                  "change what the user asks. " if job.get("ran_before") else "")
        return ("\nThe user changed this job from Mother's window. " + before + "Where it differs from the "
                "work order above, the user's latest word wins:\n"
                + "\n".join(f"- {n}" for n in notes) + "\n")

    @staticmethod
    def _map_for(job: dict[str, Any]) -> str:
        """The queen's map of the project for one worker: the tree, and the
        files that are its own. A worker that knows where to look does not
        spend its tokens searching (each used to re-read the whole project)."""
        tree = job.get("map") or []
        owned = job.get("files") or []
        if not tree and not owned:
            return ""
        out = ["", "Your queen has mapped the project for you. Do not search or read the repository"
               " beyond what your part needs: your own files, and only the parts of other files whose"
               " interfaces you use."]
        if owned:
            out.append("Your files: " + ", ".join(owned))
        if tree:
            out.append("Project tree:")
            out += [f"  {t}" for t in tree[:150]]
        return "\n".join(out) + "\n"

    def _brief_body(self, job: dict[str, Any], ref: Optional[dict[str, Any]], same_folder: bool) -> str:
        cwd = os.path.abspath(job["project_path"])
        if job.get("kind") == "task" and job.get("plain"):
            # "One agent alone": the user's words and the one file QueenBee needs, nothing about a queen, a team or questions.
            # An agent on its own must not be told to report "blocked" the way a worker bee is, nor to ask nobody.
            return PLAIN_BRIEF.format(n=job["priority"], folder=cwd, goal=job["goal"])
        if job.get("kind") == "task":
            # Only workers in the same project (hive) matter to this one: another folder's worker
            # shares no files and no board, so listing it only costs the reader tokens.
            others = [o for o in self.jobs if o is not job and o.get("kind") == "task"
                      and o["status"] not in self.ENDED and not o.get("branched")
                      and self._hive_key(o) == self._hive_key(job)]
            siblings = ("\nOther workers of your queen are at work at the same time; leave their parts to them:\n"
                        + "\n".join(f"- W{o['priority']} ({o.get('group') or 'general'}): {o['title']}"
                                     for o in others[:12]) + "\n") if others else ""
            owned = job.get("files") or []
            goal = job["goal"] + (("\nYour files: " + ", ".join(owned)) if owned else "")
            seniors = [by for by in self.jobs if by.get("senior") and by["priority"] in (job.get("after") or [])]
            if job.get("senior"):
                goal += ("\nYou are the senior of this team: lay the ground only -- the shared data model, the "
                         "interfaces and skeleton, and acceptance tests written from the spec for every part. No "
                         "feature code; be quick, the juniors wait for you.")
            elif seniors:
                goal += (f"\nThe senior (W{seniors[0]['priority']}) has laid the ground and written acceptance "
                         "tests. Build your part until its tests pass. Do not change or weaken the senior's tests.")
            comms = COMMS.format(n=job["priority"]) if others else ""
            progress = PROGRESS.get(self.cfg.get("progress_lines") or "step", PROGRESS["step"]).format(n=job["priority"])
            # read scope "own": a worker with files of its own reads those, its sections of the task and the queen's brief, and
            # nothing else of the project; what it needs from the other parts is in the brief, or asked for on the board.
            scope = ""
            if str(self.cfg.get("read_scope") or "") == "own" and owned:
                scope = ("Read only your own files (listed above), the sections of the task that describe your part, and the "
                         "queen's brief and findings in .queenbee/. Do not open the other parts' modules to learn how they work: "
                         "the brief says what they do, and if it does not, ask on the hive board.\n")
            return TASK_BRIEF.format(n=job["priority"], title=job["title"], folder=cwd, goal=goal, comms=comms,
                                     progress=progress, scope=scope,
                                     cluster=f"in the {job['group']} cluster " if job.get("group") else "",
                                     queen=self.queen_name, siblings=siblings, map=self._map_for(job))
        if not job.get("work_order"):
            # No architect pass: the older, self-directed brief.
            role = "primary" if job["priority"] == 1 and self.primary else "secondary"
            prompt = agent_task_prompt(job["goal"], name=job["title"], gpu=0, role=role,
                                       max_batch=8, port=self.host.port, project_root=cwd)
            prompt += MOTHER_RULES
            if ref is not None:
                template = SAME_FOLDER if same_folder else SAME_AS
                prompt += template.format(ref=ref["priority"], title=ref["title"], term=ref["workload"])
            return prompt + REPORT_RULES.format(n=job["priority"])
        before = ""
        if ref is not None:
            before = (f"\nJob W{ref['priority']} has already made its changes in this folder and "
                      f"submitted as QueenBee terminal {ref['workload']}. Keep its changes.\n")
        retry = f"\nA previous attempt stopped: {job['retry_note']}\n" if job.get("retry_note") else ""
        return WORKER_BRIEF.format(n=job["priority"], title=job["title"], folder=cwd,
                                   order=job["work_order"], command=job.get("command") or "(in the order)",
                                   answers=job.get("answers") or "none", before=before,
                                   retry=retry) + REPORT_RULES.format(n=job["priority"])

    def _launch(self, job: dict[str, Any], ref: Optional[dict[str, Any]] = None,
                same_folder: bool = False) -> None:
        if self.branches and job.get("kind") == "task" and not job.get("worktree"):
            root = self.branches["root"]
            with self._merge_lock:
                path = branches.add_worktree(root, job["priority"])
            job.update(hive_root=root, worktree=path, project_path=path, branch=f"queen/W{job['priority']}")
            self._event(job, f"works on its own branch {job['branch']}")
        cwd = job["project_path"]
        if not cwd or not os.path.isdir(cwd):
            raise RuntimeError(f"project folder does not exist: {cwd or '(empty)'}")
        if not job.get("attempts"):
            self._forget_earlier_run(job)
        prompt = self._brief(job, ref, same_folder)
        result = self.host._call("open_terminal", cwd, self._argv(job, prompt), cols=138, rows=32)
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "the host did not open a terminal")
        job["terminal"] = result["id"]
        if job["agent"] in self.TYPED_FIRST:
            self._type_first_message(job, prompt)
        job["status"] = "agent working"
        job["attempts"] = job.get("attempts", 0) + 1
        job["launched_at"] = time.time()
        self._event(job, f"agent started in {job['terminal']}: {job['agent']} "
                         f"{job.get('tier') or job['effort']}")

    QUIET_S = 180          # a worker sent back that has said and done nothing for this long
    REACCEPT_EVERY_S = 300

    def _reaccept_when_alone(self, job: dict[str, Any]) -> bool:
        """A worker sent back by its acceptance checks may have been failed by a part that was not in yet, which it cannot fix
        and so sits waiting. When every other worker has ended and this one has been quiet a while, the queen runs its checks
        again on the folder as it is now (and they pass, or send it back once more, or fail it: it cannot wait for ever)."""
        if job.get("kind") != "task" or not job.get("accept_round") or job.get("accept_state") == "running":
            return False
        now = time.time()
        quiet = now - max([float(e[0]) for e in job.get("events") or [] if e] or [0.0])
        if quiet < self.QUIET_S or now - float(job.get("reaccept_at") or 0) < self.REACCEPT_EVERY_S:
            return False
        if not all(o["status"] in self.ENDED for o in self.jobs if o is not job and o.get("kind") == "task"):
            return False
        job["reaccept_at"] = now
        job["accept_state"] = None
        self._event(job, "the other parts are in and this worker is quiet: the queen runs its checks again")
        if self._accept(job):
            job["status"] = "done"
            self._event(job, "done: its checks pass now")
        return True

    def _forget_earlier_run(self, job: dict[str, Any]) -> None:
        """A folder that an earlier plan worked in still holds that run's W<n>.progress and inbox. A new worker with the
        same number starts after them: what they say is not this worker's, and the queen must not judge it by them."""
        folder = os.path.join(job["project_path"], ".queenbee")
        try:
            job["progress_at"] = os.path.getsize(os.path.join(folder, f"W{job['priority']}.progress"))
        except OSError:
            pass
        try:
            os.remove(os.path.join(folder, "inbox", f"W{job['priority']}.md"))
        except OSError:
            pass

    def _type_first_message(self, job: dict[str, Any], prompt: str) -> None:
        """An agent that cannot take its first message on the command line (Kimi): the brief goes into a file in the
        project and, once the agent's screen is up, one line pointing at it is typed in."""
        folder = os.path.join(job["project_path"], ".queenbee")
        path = os.path.join(folder, f"W{job['priority']}.brief.md")
        try:
            os.makedirs(folder, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(prompt)
        except OSError:
            return
        term = job["terminal"]
        line = f"Read the file {path} and do exactly what it says."

        def type_it() -> None:
            time.sleep(self.TYPED_FIRST.get(job["agent"], 10))
            try:
                self.host._call("terminal_write", term, line)
                time.sleep(0.4)
                self.host._call("terminal_write", term, "\r")
            except Exception:                          # the terminal is gone: nothing to type into
                pass

        threading.Thread(target=type_it, name=f"queen-first-message-W{job['priority']}", daemon=True).start()

    def _direct_argv(self, job: dict[str, Any]) -> Optional[list[str]]:
        """The prepared submit line as argv, if it is a plain `queenbee.gpu submit`
        for this job's own project -- anything else goes to an agent."""
        argv = _split_windows(re.sub(r"^\s*&\s*", "", job.get("command") or ""))
        if len(argv) < 4 or argv[1:3] != ["-m", "queenbee.gpu"] or "submit" not in argv:
            return None
        if "--project-root" in argv:
            root = argv[argv.index("--project-root") + 1]
            if _folder(root) != _folder(job["project_path"]):
                return None
        if not os.path.isfile(argv[0]):
            return None
        # pythonw has no console to print the new terminal's id to.
        if os.path.basename(argv[0]).lower() == "pythonw.exe":
            argv[0] = os.path.join(os.path.dirname(argv[0]), "python.exe")
        return argv

    def _submit_direct(self, job: dict[str, Any], argv: list[str]) -> None:
        """A `same_as` job whose script the job it copies has already adapted
        and submitted: only its settings differ, so Mother runs the architect's
        submit line itself -- no agent, no tokens. Whether it trains now or
        waits is then the GPU controller's call, by VRAM."""
        proc = subprocess.run(argv, cwd=job["project_path"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            result = json.loads((proc.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            result = {}
        if proc.returncode != 0 or not result.get("id"):
            raise RuntimeError(f"submit failed: {(proc.stderr or proc.stdout or '').strip()[-400:]}")
        now = time.time()
        job.update(workload=result["id"], status="GPU job submitted", submitted_at=now,
                   launched_at=now, direct=True, attempts=job.get("attempts", 0) + 1,
                   seen_offset=0)
        self._event(job, f"GPU job {result['id']} submitted by Mother, no agent")

    # -- reports and escalation --------------------------------------------

    @staticmethod
    def report_path(job: dict[str, Any]) -> str:
        return Dispatch.state_file(job, f"W{job['priority']}.json")

    @staticmethod
    def state_file(job: dict[str, Any], name: str) -> str:
        """Where a worker's own file (its report, its progress) is. A worker on a branch lives in a worktree but may
        write into the project's own .queenbee (it is the folder the others and the queen use), so when the file
        is not in its own folder and is in the hive's root, that one is meant."""
        own = os.path.join(job["project_path"], ".queenbee", name)
        root = job.get("hive_root")
        if root and not os.path.exists(own):
            there = os.path.join(root, ".queenbee", name)
            if os.path.exists(there):
                return there
        return own

    def _read_report(self, job: dict[str, Any]) -> Optional[dict[str, Any]]:
        try:
            # utf-8-sig: a worker on Windows often writes its report with a byte-order mark,
            # and a report that cannot be read leaves a finished worker "working" for ever.
            # errors="replace": Codex on Windows writes its report in the system code page (an en dash is the byte 0x96), which is
            # not UTF-8; one such byte made the whole report unreadable and the pass failed, so a finished worker stayed "working".
            with open(self.report_path(job), encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        except (OSError, ValueError):
            return None
        try:
            report = json.loads(text)
        except ValueError:
            # A worker that quotes a regex in its notes writes an invalid escape (\w) and the whole report
            # became unreadable: the finished worker stayed "working" for ever. Repair stray backslashes;
            # failing that, take the status and notes as they stand.
            try:
                report = json.loads(re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text))
            except ValueError:
                status = re.search(r'"status"\s*:\s*"(\w+)"', text)
                if not status:
                    return None
                notes = re.search(r'"notes"\s*:\s*"(.*)"', text, re.S)
                report = {"status": status.group(1), "notes": notes.group(1)[:2000] if notes else ""}
        if not isinstance(report, dict):
            return None
        # A report from an earlier plan -- or an earlier attempt at this job --
        # must not count for this one.
        try:
            mtime = os.path.getmtime(self.report_path(job))
        except OSError:
            return None
        # 2 s of slack: file times on Windows are coarser than time.time(), so
        # a report written right after a launch can look a few ms older.
        if mtime + 2 < max(self.started_at, job.get("launched_at", 0)):
            return None
        report["_mtime"] = mtime
        return report

    _ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[=>()][0-9A-Za-z]?")
    MIN_RUN_S = 60

    def _proven(self, job: dict[str, Any]) -> bool:
        """Whether a job's GPU run has shown the script works: it finished, or
        it has been running for MIN_RUN_S. Copies of it wait until then, so a
        broken script fails once, not once per copy."""
        if job["status"] in ("done", "stopped early"):
            return True
        return (job["status"] == "GPU job submitted" and bool(job.get("workload"))
                and time.time() - float(job.get("submitted_at") or time.time()) >= self.MIN_RUN_S)

    def _result_written(self, job: dict[str, Any]) -> Optional[bool]:
        """Whether the job's result file (the architect named it) was written
        by this run; None when no file was named, so only the exit is known."""
        rel = job.get("metrics_path")
        if not rel:
            return None
        path = rel if os.path.isabs(rel) else os.path.join(job["project_path"], rel)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return False
        if mtime + 2 < max(self.started_at, job.get("launched_at", 0)):
            return False
        if path.lower().endswith(".jsonl") and job.get("expect_epochs"):
            # A per-epoch log exists from the first epoch; it is the result
            # only once every epoch is in it.
            return len(sweeping.read_history(job["project_path"], rel, job.get("metric_key") or "")) \
                >= int(job["expect_epochs"])
        return True

    def _failed_start(self, job: dict[str, Any]) -> Optional[str]:
        """The tail of a GPU job's output if it ended in an error or ended so
        fast it cannot have trained; None when it looks like a real finish."""
        tail, crashed, quick = self._tail(job)
        return tail if (crashed or quick) else None

    def _tail(self, job: dict[str, Any]) -> tuple[str, bool, bool]:
        """(last lines of the GPU job's output, ended in an error, ended in
        under MIN_RUN_S)."""
        try:
            listing = {t["id"]: t for t in self.host._call("terminals") or []}
            term = listing.get(job["workload"]) or {}
            offset = int(term.get("offset") or 0)
            data = self.host._call("terminal_read", job["workload"], max(0, offset - 4000)).get("data") or ""
            started = float(term.get("started_at") or job.get("submitted_at") or time.time())
        except (HostUnavailable, TypeError, ValueError):
            return "", False, False
        lines = [l.rstrip() for l in self._ANSI.sub("", data).replace("\r", "\n").split("\n") if l.strip()]
        tail = "\n".join(lines[-12:])
        crashed = bool(re.search(r"Traceback|Error:|error:|not found|No such file", tail))
        quick = time.time() - started < self.MIN_RUN_S
        return tail[-1500:], crashed, quick

    _PREEMPTED = re.compile(r"\[queenbee\] .*: preempted after saving its state")
    _RESUMED = re.compile(r"\[queenbee\] .*: resuming \(start \d+\)")

    def _watch_workload(self, job: dict[str, Any], term: dict[str, Any]) -> None:
        """Record the GPU job stepping aside and coming back, from what
        QueenBee's launcher prints in its terminal."""
        offset = int(term.get("offset") or 0)
        seen = int(job.get("seen_offset") or 0)
        if offset <= seen:
            return
        try:
            data = self.host._call("terminal_read", job["workload"], max(seen, offset - 64000)).get("data") or ""
        except HostUnavailable:
            return
        job["seen_offset"] = offset
        for _ in self._PREEMPTED.finditer(data):
            job["preempts"] = job.get("preempts", 0) + 1
            self._event(job, "stepped aside: saved its state and left the GPU")
        for _ in self._RESUMED.finditer(data):
            job["resumes"] = job.get("resumes", 0) + 1
            self._event(job, "resumed from its saved state")

    def _escalate(self, job: dict[str, Any], why: str) -> None:
        """A worker could not finish: the same work order goes one step up.

        Codex -> Claude on the worker model (Haiku) -> the escalation model
        (Sonnet). Codex runs its commands in a sandbox as another Windows
        user, which cannot start a Microsoft Store Python, so a Codex worker
        can be blocked where a Claude worker is not.
        """
        if job.get("bare"):
            # A run meant to be a person's own terminal session: nobody steps in, nobody moves it to another model.
            job["status"], job["error"] = "failed", f"stopped: {why.splitlines()[0][:160]}"
            self._event(job, job["error"])
            return
        if job["agent"] == "codex" and not job.get("crossed"):
            try:
                if job.get("terminal"):
                    self.host._call("terminal_close", job["terminal"])
            except HostUnavailable:
                pass
            self._event(job, f"moved from codex to claude {self.cfg.get('worker_model') or 'haiku'}: "
                             f"{why.splitlines()[0][:160]}")
            job.update(agent="claude", tier=self.cfg.get("worker_model") or "haiku", crossed=True,
                       retry_note=why, error=None,
                       history=(job.get("history") or []) + [f"{job['terminal']} (codex): {why}"])
            try:
                self._launch(job)
            except Exception as exc:
                job["status"], job["error"] = "failed", str(exc)
            return
        ladder = [m for m in (self.cfg.get("escalation") or ["sonnet"]) if m != job.get("tier")]
        if job["agent"] != "claude" or job.get("escalated") or not ladder:
            files = (job.get("report") or {}).get("changed_files")
            if (job["agent"] == "claude" and files and self.verify and self.verify.get("command")
                    and sum(j.get("kind") == "task" for j in self.jobs) > 1):
                # Its own files are written and it waits on another worker's part: a worker cannot fix that, the
                # delivery check over the whole can (and sends a fixer when it fails).
                job["status"] = "done"
                self._event(job, "reported blocked by another part, with its own files written: "
                                 f"the delivery check decides. {why.splitlines()[0][:140]}")
                return
            job["status"], job["error"] = "failed", f"needs you: {why}"
            self._event(job, f"failed; needs you: {why.splitlines()[0][:160]}")
            return
        self._event(job, f"moved up to {ladder[0]}: {why.splitlines()[0][:160]}")
        try:
            if job.get("terminal"):
                self.host._call("terminal_close", job["terminal"])
        except HostUnavailable:
            pass
        job.update(escalated=True, tier=ladder[0], retry_note=why, error=None,
                   history=(job.get("history") or []) + [f"{job['terminal']}: {why}"])
        try:
            self._launch(job)
            job["status"] = "agent working"
        except Exception as exc:
            job["status"], job["error"] = "failed", str(exc)

    # -- following ---------------------------------------------------------

    def _workloads(self) -> dict[str, str]:
        """agent terminal -> the GPU-workload terminal it submitted."""
        found = {}
        for term in self.host._call("terminals") or []:
            if not term.get("gpu_workload") or not term.get("pid"):
                continue
            try:
                m = _OWNER.search(" ".join(psutil.Process(int(term["pid"])).cmdline()))
            except psutil.Error:
                continue
            if m:
                found[m.group(1)] = term["id"]
        return found

    # -- cleaning up after a worker ------------------------------------------------

    REAP_AFTER_S = 3          # let acceptance and the merge finish with the worker's processes first
    CLOSE_AFTER_S = 900       # a finished worker's idle session is closed after this long

    def _tidy(self, listing: dict[str, Any]) -> None:
        """Workers start things and never stop them: test servers, runners, background jobs. Once
        a worker's job has ended, stop whatever descends from its agent (the agent stays, so you can
        still talk to it); long after, close the idle session itself, which holds hundreds of MB."""
        now = time.time()
        close_after = float(self.cfg.get("close_after_s", self.CLOSE_AFTER_S))
        for job in self.jobs:
            if job["status"] not in self.ENDED:
                for key in ("ended_at", "reaped", "session_closed", "screen_saved"):
                    job.pop(key, None)                       # re-tasked: it is at work again
                continue
            ended = job.setdefault("ended_at", now)
            term = job.get("terminal")
            row = listing.get(term) if term else None
            if row is None or not row.get("running"):
                continue
            if not job.get("screen_saved") and now - ended >= 3:
                job["screen_saved"] = True
                self._save_screen(job, term, row)           # keep what it looked like, while it is still there
            if not job.get("reaped") and now - ended >= self.REAP_AFTER_S:
                job["reaped"] = True
                n = self._reap(row)
                if n:
                    self._event(job, f"stopped {n} process(es) it left running")
            if not job.get("session_closed") and now - ended >= close_after:
                job["session_closed"] = True
                self._save_screen(job, term, row)           # once more: it may have been talked to since
                try:
                    self.host._call("terminal_close", term)
                    self._event(job, "its idle session was closed to free memory")
                except HostUnavailable:
                    pass

    def _save_screen(self, job: dict[str, Any], term: str, row: dict[str, Any]) -> None:
        """Write a worker's terminal (everything it printed, and its size) beside its notes, so the window can
        show the same screen after the session is gone."""
        try:
            data = (self.host._call("terminal_read", term, 0) or {}).get("data") or ""
            folder = os.path.join(job["project_path"], ".queenbee", "screens")
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, f"W{job['priority']}.json"), "w", encoding="utf-8") as fh:
                json.dump({"cols": int(row.get("cols") or 120), "rows": int(row.get("rows") or 32),
                           "data": data[-700_000:]}, fh)
        except (HostUnavailable, OSError, ValueError, TypeError):
            pass

    def _reap(self, row: dict[str, Any]) -> int:
        """Stop the processes below a terminal's agent. Returns how many."""
        pid = row.get("pid")
        if not pid:
            return 0
        try:
            pids = self.host._call("descendants", int(pid)) or []
        except (HostUnavailable, ValueError):
            return 0
        stopped = 0
        for child in pids:
            try:
                psutil.Process(int(child)).kill()
                stopped += 1
            except (psutil.Error, ValueError):
                pass
        return stopped

    def step(self) -> None:
        """Advance the plan once: start what can start, record submissions."""
        if self.held:                      # restored with no worker still running: the user says continue or stop
            return
        with self.lock:
            try:
                workloads = self._workloads()
                listing = {t["id"]: t for t in self.host._call("terminals") or []}
                alive = {tid: t.get("running") for tid, t in listing.items()}
                self._tidy(listing)
            except HostUnavailable as exc:
                for job in self.jobs:
                    if job["status"] not in ("failed", "done"):
                        job["error"] = f"QueenBee host unavailable: {exc}"
                return
            by_priority = {j["priority"]: j for j in self.jobs}
            ended = ("done", "failed", "stopped")
            for job in self.jobs:
                if job["terminal"] and job["terminal"] in workloads and not job["workload"]:
                    job["workload"] = workloads[job["terminal"]]
                    job["status"] = "GPU job submitted"
                    job["submitted_at"] = time.time()
                    job["seen_offset"] = 0
                    self._event(job, f"GPU job {job['workload']} submitted by the agent")
                # A job that died before Mother saw it still has a report
                # naming its terminal; the report links it, the host confirms.
                if not job["workload"] and job["status"] == "agent working" and job.get("work_order"):
                    report = self._read_report(job) or {}
                    term = str(report.get("terminal") or "")
                    if report.get("status") == "submitted" and term in alive:
                        job["workload"], job["status"] = term, "GPU job submitted"
                        job["submitted_at"] = time.time()
                        job["seen_offset"] = 0
                        self._event(job, f"GPU job {term} submitted by the agent")
                if job["workload"] and job["status"] == "GPU job submitted" and job["workload"] in listing:
                    self._watch_workload(job, listing[job["workload"]])
                if job.get("detached"):
                    # Let go of by the user: Mother still shows what it does,
                    # but starts, answers, restarts and escalates nothing.
                    if job["workload"] and alive.get(job["workload"]) is False \
                            and job["status"] == "GPU job submitted":
                        job["status"] = "done" if self._result_written(job) else "stopped"
                    continue
                if job.get("role") == "mother":
                    if self._follow_branch(job):
                        continue
                if job["workload"] and alive.get(job["workload"]) is False \
                        and job["status"] == "GPU job submitted":
                    tail, crashed, quick = self._tail(job)
                    written = self._result_written(job)
                    failure = None if (written or (written is None and not crashed and not quick)) \
                        else (tail or "no output")
                    restart = (failure is not None and written is False and not crashed
                               and job.get("restarts", 0) < 1 and self._direct_argv(job))
                    if failure is None:
                        job["status"] = "done"
                        self._event(job, "finished" + ("; result written" if written else ""))
                    elif restart:
                        # It stopped without an error and without its result
                        # (killed, machine asleep): its state is on disk, so
                        # the same line starts it again from there. No agent.
                        job["restarts"] = job.get("restarts", 0) + 1
                        self._event(job, f"GPU job {job['workload']} ended without its result; "
                                         "starting it again from its saved state")
                        try:
                            self._submit_direct(job, restart)
                        except Exception as exc:
                            self._escalate(job, f"could not restart it: {exc}")
                    else:
                        # Not done: it never really ran. Hand the error back.
                        dead = job["workload"]
                        job["workload"], job["status"] = None, "agent working"
                        self._event(job, f"GPU job {dead} failed" + (" at once" if quick else "")
                                         + (" without its result" if written is False else ""))
                        why = (f"its GPU job {dead} "
                               + ("exited at once" if quick else "ended without its result"
                                  if written is False else "failed") + f":\n{failure}\n"
                               "Fix the cause in the project's files -- this is allowed "
                               "even where the work order says not to edit -- then submit again.")
                        fixer = next((o for o in self.jobs if o is not job
                                      and _folder(o["project_path"]) == _folder(job["project_path"])
                                      and o["status"] == "agent working" and o.get("terminal")), None)
                        owner = by_priority.get(job.get("same_as")) if job.get("direct") else None
                        if (fixer is None and owner is not None and owner["status"] not in ("failed", "stopped")
                                and not self._proven(owner)):
                            # The script's owner has not shown it works yet: the
                            # copy failing says the script is broken, which the
                            # owner's agent fixes; wait for it, open no agent.
                            # (A copy failing beside a proven owner has its own
                            # problem and goes to its own agent.)
                            fixer = owner
                        if fixer is not None:
                            # One agent fixes the shared script; this job waits
                            # for it and is then submitted again, with no agent.
                            job.update(status="queued", same_as=fixer["priority"], direct=False,
                                       terminal=None, retry_note=why)
                        else:
                            self._escalate(job, why)
                # Closed before it submitted anything: stopped, not "working".
                # (`alive` was read before this pass, so a terminal opened
                # during it -- an escalation -- is not in it yet.)
                if (job["status"] == "agent working" and not job["workload"]
                        and not alive.get(job["terminal"])        # gone, or listed but its process has exited
                        and time.time() - job.get("launched_at", 0) > 30):
                    job["status"], job["error"] = "stopped", "agent terminal closed before submitting"
                # Reports and escalation are for prepared jobs only: a job with
                # no work order was briefed to explore on its own and to answer
                # to the user, not to report to Mother.
                if job["status"] == "agent working" and job.get("kind") == "task":
                    self._read_progress(job)
                    self._route_board(job)
                if job["status"] == "agent working" and job.get("work_order"):
                    report = self._read_report(job)
                    if report is not None:
                        job["report"] = report
                    state, reason = self.state_of(job["terminal"])
                    turn_over = state in ("waiting", "idle") and (
                        "turn finished" in reason or "stopped" in reason)
                    fresh_report = bool(report)          # _read_report drops stale ones
                    if not fresh_report and self._reaccept_when_alone(job):
                        pass
                    elif fresh_report and report.get("status") == "blocked":
                        self._escalate(job, str(report.get("notes") or "reported blocked"))
                    elif job.get("kind") == "task" and fresh_report and report.get("status") == "done":
                        # Work with no GPU run behind it ends with its report --
                        # once the queen's own checks of it pass.
                        if self._accept(job):
                            job["status"] = "done"
                            self._event(job, "done: " + str(report.get("notes") or "")[:160])
                            self._keep_fix_finding(job, str(report.get("notes") or ""))
                    elif (job.get("kind") == "task" and turn_over
                          and time.time() - float(job.get("steered_at") or 0) > 20):
                        # Its turn ended without the report: it stopped to be
                        # told what next. The queen is the one who knows the
                        # package, so the queen answers -- never the user.
                        if job.get("steered", 0) < self.MAX_STEERS:
                            self._steer(job)
                        elif self._accept(job):
                            job["status"] = "done"
                            self._event(job, "done: its turn ended without a report")
                    elif turn_over and not job["workload"] and job.get("kind") != "task" and not (
                            fresh_report and report.get("status") == "submitted"):
                        # (A task's turn ending is the queen's to steer, above --
                        # just after a nudge it is not a failure to escalate.)
                        self._escalate(job, "finished its turn without submitting or reporting")
                if job["status"] != "queued":
                    continue
                ref = by_priority.get(job["same_as"]) if job["same_as"] else None
                same_folder = False
                if ref is None and job.get("kind") != "task":
                    # Agents sharing a folder edit the same files. However
                    # different the planner judged the jobs, the next one waits
                    # until the one before it in that folder has submitted.
                    for earlier in self.jobs:
                        if earlier["priority"] >= job["priority"]:
                            break
                        if _folder(earlier["project_path"]) != _folder(job["project_path"]):
                            continue
                        # A branch's Mother edits nothing itself once it has
                        # branched; its own agents are ordered among the rest.
                        if earlier.get("branched"):
                            continue
                        if earlier.get("kind") == "task":
                            if earlier["status"] in self.ENDED:
                                continue
                            ref, same_folder = earlier, True
                            continue
                        # Agents may work side by side when the architect says
                        # their files do not overlap; unknown files overlap.
                        mine, theirs = job.get("files"), earlier.get("files")
                        if mine is not None and theirs is not None and not set(mine) & set(theirs):
                            continue
                        # A later job in the folder builds on the earlier one
                        # (its changes, or its results -- "score the best
                        # run"). If that job ended without submitting,
                        # starting this one would build on nothing.
                        if earlier["status"] in ("failed", "stopped") and not earlier.get("workload"):
                            ref, same_folder = earlier, True
                            break
                        if earlier["status"] not in ("failed", "stopped"):
                            ref, same_folder = earlier, True
                before = [by_priority[a] for a in job.get("after") or [] if a in by_priority]
                pending = next((b for b in before if b["status"] not in self.ENDED), None)
                job["waiting_for"] = None            # what it waited for may be over: say so, then decide again below
                if pending is not None:
                    job["waiting_for"] = pending["priority"]
                    continue
                if before and all(b["status"] in ("failed", "stopped") for b in before):
                    job["status"], job["error"] = "failed", "the work it builds on failed"
                    self._event(job, job["error"])
                    continue
                if ref is not None and ref.get("kind") == "task" and ref["status"] not in self.ENDED:
                    job["waiting_for"] = ref["priority"]
                    continue
                if ref is not None and (not ref["workload"] or (job["same_as"] and not self._proven(ref))):
                    job["waiting_for"] = ref["priority"]
                    if ref["status"] in ("failed", "stopped"):
                        job["status"], job["error"] = "failed", f"job {ref['priority']} {ref['status']}"
                    continue
                if self.gate is not None:
                    why = self.gate(job, self.jobs)
                    if why:
                        # Over the queen's limit: it waits, and says so once.
                        if job.get("held") != why:
                            job["held"] = why
                            self._event(job, f"held by the queen's limit: {why}")
                        continue
                    if job.get("held"):
                        job.pop("held", None)
                        self._event(job, "the queen's limit has room again; starting")
                if not self.primary:
                    for key in ("command", "command_template"):
                        if job.get(key):
                            job[key] = re.sub(r"--role\s+primary\b", "--role secondary", job[key])
                try:
                    direct = (self._direct_argv(job) if job["same_as"] and ref is not None
                              and job.get("work_order") else None)
                    if direct:
                        self._submit_direct(job, direct)
                    else:
                        self._launch(job, ref, same_folder)
                except Exception as exc:          # one bad job must not stop the rest
                    job["status"], job["error"] = "failed", str(exc)
            if self.sweep is not None:
                self._advance_sweep()
            ended = ("done", "failed", "stopped", "stopped early")
            if self.branches:
                self._sync_notice()
            if self._verify_pending():
                return
            if self._qa_pending():
                return
            unbranched = any(j.get("role") == "mother" and not j.get("branched") and not j.get("detached")
                             for j in self.jobs)
            if not unbranched and all(j["status"] in ended for j in self.jobs) and not (
                    self.sweep is not None and any(not l.get("stopped") or l.get("active")
                                                   for l in self.sweep["lanes"])):
                self._cleanup_worktrees()
                self.done.set()

    MODELS = {"claude": ("haiku", "sonnet", "opus", "claude-fable-5-1",
                         "claude-sonnet-5", "claude-opus-5", "claude-fable-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6"), "codex": ("default",),
              "gemini": ("default",), "kimi": ("default",), "opencode": ("default",),
              "cursor": ("default",), "mistral": ("default",), "grok": ("default",)}
    # Agents whose first message cannot be given on the command line: it is typed in once their screen is up.
    TYPED_FIRST = {"kimi": 10, "opencode": 10, "mistral": 14}
    MODEL_ID = re.compile(r"[A-Za-z0-9][\w.\-:/]{0,79}")

    def _models(self, agent: str) -> tuple[str, ...]:
        """The models a worker may be given: nothing above the cfg's max_model."""
        cap = self.cfg.get("max_model") or ""
        return tuple(m for m in self.MODELS.get(agent, ()) if clamp(m, cap) == m)

    def _model_ok(self, agent: str, tier: str) -> bool:
        """A listed model, or any id the user typed (the CLI itself rejects one it does not know)."""
        return tier in self._models(agent) or (bool(self.MODEL_ID.fullmatch(tier or "")) and agent in self.MODELS)

    def set_job(self, priority: int, changes: dict[str, Any]) -> dict[str, Any]:
        """The user steering one job from the window: its model (used from its
        next start), whether Mother manages it, and whether it is a Mother of
        its own sub-tree."""
        with self.lock:
            job = next((j for j in self.jobs if j["priority"] == priority), None)
            if job is None:
                return {"ok": False, "error": f"no job W{priority}"}
            said = []
            agent = changes.get("agent")
            if agent is not None:
                if agent not in self.MODELS:
                    return {"ok": False, "error": f"unknown agent {agent}"}
                job["agent"] = agent
                said.append(f"agent {agent}")
            tier = changes.get("tier")
            if tier is not None:
                if not self._model_ok(job["agent"], tier):
                    return {"ok": False, "error": f"{job['agent']} has no model {tier}"}
                job["tier"] = None if tier == "default" else tier
                said.append(f"model {tier} from its next start")
            if "detached" in changes:
                job["detached"] = bool(changes["detached"])
                said.append("let go of by Mother" if job["detached"] else "managed by Mother again")
            reopen = False
            live: Optional[tuple[str, str]] = None
            text = str(changes.get("goal") or "").strip()
            if text:
                if job.get("branched"):
                    return {"ok": False, "error": f"W{priority} is a branch; steer its agents instead"}
                if job.get("lane"):
                    return {"ok": False, "error": f"W{priority} is a sweep value that Mother runs itself"}
                st = job["status"]
                if st in ("agent working", "GPU job submitted") and job.get("terminal") and not job.get("direct"):
                    # Its agent is at work: say it to the agent, as a person
                    # typing into its terminal would. Kept for any restart.
                    live = (job["terminal"], text)
                    job["user_notes"] = (job.get("user_notes") or []) + [text]
                    said.append(f"told its agent: {text[:120]}")
                elif st in self.ENDED and not job.get("direct") and self._agent_alive(job):
                    # Its agent's session is still open: the change goes into
                    # the same conversation, which remembers what was done
                    # ("not like that -- make it ..."). Mother follows and
                    # answers its questions again, as for the first run.
                    job["user_notes"] = (job.get("user_notes") or []) + [text]
                    again = (f"{text}\n\nWhen this is done, write .queenbee/W{job['priority']}.json "
                             "again as before." if job.get("work_order") else text)
                    live = (job["terminal"], again)
                    job.update(status="agent working", workload=None, error=None, attention=None,
                               launched_at=time.time())
                    reopen = self.done.is_set()
                    self.done.clear()
                    self.report, self.finished_at = None, 0.0
                    said.append(f"asked its agent to redo: {text[:120]}")
                elif st == "queued" or st in self.ENDED:
                    if st == "queued":
                        job["goal"] = text
                    job["user_notes"] = (job.get("user_notes") or []) + [text]
                    if st in self.ENDED:
                        # Run again by a new agent: the task stays what it was
                        # and the text is what to change, with the earlier work
                        # still in the folder. A job Mother ran with no agent
                        # now has something to do.
                        job.update(status="queued", terminal=None, workload=None, error=None,
                                   attention=None, waiting_for=None, direct=False, same_as=None,
                                   ran_before=True)
                        reopen = self.done.is_set()
                        self.done.clear()
                        self.report, self.finished_at = None, 0.0
                    said.append(f"runs again, changing: {text[:120]}" if st in self.ENDED
                                else f"new task: {text[:120]}")
                else:
                    return {"ok": False, "error": f"W{priority} is {st}; give it a new task once it ends"}
            if "role" in changes:
                wants = changes["role"] == "mother"
                if not wants and job.get("branched"):
                    return {"ok": False, "error": f"W{priority} has already branched; its agents are running"}
                instruction = str(changes.get("instruction") or "").strip()
                if wants:
                    job["instruction"] = instruction or job.get("instruction") or job.get("goal") or ""
                    if not job["instruction"]:
                        return {"ok": False, "error": "give the branch an instruction"}
                    why = self.branch_refusal(job, job["instruction"])
                    if why:
                        return {"ok": False, "error": f"W{priority} cannot become a queen: {why}"}
                if wants:                              # the user's choices for this branch queen, if any
                    for key in ("queen_model", "worker_model"):
                        if changes.get(key):
                            job[key] = str(changes[key])
                job["role"] = "mother" if wants else None
                if wants and job["status"] in self.ENDED:
                    # Finished work can branch on: its own agents continue from
                    # what it left, so the plan runs again.
                    reopen = self.done.is_set()
                    self.done.clear()
                    self.report, self.finished_at = None, 0.0
                said.append(f"a Mother of its own branch: {job['instruction'][:120]}" if wants
                            else "an agent again")
            if said:
                self._event(job, "you set: " + ", ".join(said))
            out = {"ok": True, "job": dict(job)}
        if live is not None:
            try:
                # The text, then Enter on its own: a TUI takes a burst ending
                # in a newline as pasted text and does not send it.
                self.host._call("terminal_write", live[0], live[1])
                time.sleep(0.3)
                self.host._call("terminal_write", live[0], "\r")
            except HostUnavailable as exc:
                return {"ok": False, "error": f"could not reach its terminal: {exc}"}
        if reopen and not (self._thread and self._thread.is_alive()):
            self.start()
        return out

    # Test runners only: the command comes from a model's plan and runs with
    # no one approving it.
    VERIFY_OK = re.compile(r"^(python(3|\.exe)? -m (unittest|pytest)|pytest|npm (run )?test|cargo test|go test)\b"
                           r"[\w\s./:=\-\\\"'*?,]*$")
    MAX_VERIFY = 2

    def set_verify(self, command: str, folder: str, trusted: bool = False) -> None:
        """The delivery check. A model's plan may only name a test runner; the user's own command is trusted."""
        command = (command or "").strip()
        if not command or not (trusted or self.VERIFY_OK.match(command)):
            if command:
                self._event_all(f"the queen will not run the check {command!r}: only test runners are allowed")
            return
        with self.lock:
            self.verify = {"command": command, "folder": folder, "state": "waiting", "rounds": 0, "log": ""}

    def _verify_pending(self) -> bool:
        """Called under the lock once a pass has settled the jobs: True while
        the package is not yet checked (or being fixed)."""
        v = self.verify
        if not v or v.get("state") in ("passed", "failed", "cancelled"):
            return False
        if not all(j["status"] in self.ENDED for j in self.jobs):
            return v.get("state") == "fixing"
        if v.get("state") == "running":
            return True
        # A worker whose own acceptance check still failed after its fix rounds has its files in: its test may be the wrong one
        # (the queen said so in a video run), and the check of the whole package is the one that decides. A worker that was
        # stopped, or failed before writing anything, did not finish.
        broken = [j for j in self.jobs if j.get("kind") == "task" and j["status"] in ("failed", "stopped")
                  and not (j["status"] == "failed" and str(j.get("error") or "").startswith("acceptance failed"))]
        if broken and v.get("state") != "fixing":
            # A worker that did not finish cannot be "fixed together": the check is not run, and no
            # integration worker is sent to patch stubs.
            v["state"], v["log"] = "failed", "not run: " + ", ".join(f"W{j['priority']}" for j in broken) + " did not finish"
            self._event_all(f"the delivery check was not run: {', '.join('W%d' % j['priority'] for j in broken)} did not finish")
            return False
        v["state"] = "running"
        threading.Thread(target=self._run_verify, name="queen-verify", daemon=True).start()
        return True

    def _run_verify(self) -> None:
        v = self.verify
        try:
            code, out = run_captured(verifier.split_command(v["command"]), v["folder"], 600)
            ok = code == 0
            tail = (out if code is not None else out + "\n(the check did not finish in 10 minutes; stopped)")[-3000:]
        except OSError as exc:
            ok, tail = False, str(exc)
        with self.lock:
            v["log"] = tail
            if v.get("state") == "cancelled":
                return                    # the user stopped the work while the check ran
            if ok:
                v["state"] = "passed"
                self._event_all(f"the queen checked the whole package: `{v['command']}` passes")
                board.validate_findings(v["folder"], f"{v['command']} passes")      # the findings of this run held up
                return
            if v["rounds"] >= self.MAX_VERIFY:
                v["state"] = "failed"
                self._event_all(f"the queen's check still fails after {v['rounds']} fixes; left as it is")
                return
            v["rounds"] += 1
            v["state"] = "fixing"
            first = next((j for j in self.jobs if j.get("kind") == "task"), self.jobs[0] if self.jobs else None)
            agent = (first or {}).get("agent") or "claude"
            tier = "sonnet" if agent == "claude" else None
            n = max((j["priority"] for j in self.jobs), default=0) + 1
            goal = (f"The workers have finished their parts, but the queen's check of the whole package, "
                    f"`{v['command']}`, fails. Make the parts work together so it passes -- fix what connects "
                    f"them; do not rewrite finished parts or weaken tests. Its output ends with:\n{tail[-2000:]}")
            board.flag_conflicts(v["folder"], tail)            # findings whose files the failure names carry the failure
            self.jobs.append({"priority": n, "title": f"integration fix {v['rounds']}", "goal": goal,
                              "project_path": v["folder"], "agent": agent, "effort": "high", "tier": tier,
                              "same_as": None, "parent": None, "detached": False, "role": None, "kind": "task",
                              "group": "integration", "work_order": goal, "command": "", "status": "queued",
                              "terminal": None, "workload": None, "error": None})
            self._event(self.jobs[-1], "the queen's check failed; this worker makes the parts fit")
            restart = self._reopen()
        if restart or not (self._thread and self._thread.is_alive()):
            self.start()

    def _read_progress(self, job: dict[str, Any]) -> None:
        """New lines a worker wrote to its progress file since the last look:
        the queen's live view of what each worker is doing."""
        path = self.state_file(job, f"W{job['priority']}.progress")
        try:
            size = os.path.getsize(path)
            if size < int(job.get("progress_at") or 0):
                job["progress_at"] = 0                # rewritten from the top
            if size == int(job.get("progress_at") or 0):
                return
            with open(path, encoding="utf-8", errors="replace") as fh:
                fh.seek(int(job.get("progress_at") or 0))
                text = fh.read(20_000)
                job["progress_at"] = fh.tell()
        except OSError:
            return
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for line in lines[-5:]:
            self._event(job, f"progress: {line[:200]}")
        if lines:
            job["progress"] = lines[-1][:200]

    # -- a quality pass over parts that were built side by side -----------------

    QA_GOAL = ("Quality pass over the finished package. The original instruction was: {instruction}\n"
               "Several workers built its parts side by side and their work is merged. Act as the first real "
               "client: read the spec the instruction names, start the program, and exercise every documented "
               "behaviour end to end (each endpoint, rule and status code, across parts, in the order a user "
               "would). Fix what breaks at its cause, add a regression test for each fix, and keep every test "
               "passing. Do not rewrite parts that already work.")

    def _qa_pending(self) -> bool:
        """Once, when every worker has finished and some succeeded: a Sonnet
        worker checks the joined package like a client would. True while it is queued."""
        if not self.qa_goal or self._qa_started:
            return False
        if not all(j["status"] in self.ENDED for j in self.jobs) or not any(j["status"] == "done" for j in self.jobs):
            return False
        # A part that failed or was stopped is not a joined package to check: the quality pass is
        # for when every part came in (it once started with half the package missing).
        if any(j["status"] in ("failed", "stopped") for j in self.jobs):
            return False
        first = next((j for j in self.jobs if j.get("kind") == "task"), None)
        if first is None:
            return False
        self._qa_started = True
        n = max(j["priority"] for j in self.jobs) + 1
        goal = self.QA_GOAL.format(instruction=(self.instruction or "")[:1500])
        root = self.branches["root"] if self.branches else (first.get("hive_root") or first["project_path"])
        self.jobs.append({"priority": n, "title": "quality pass", "goal": goal, "project_path": root,
                          "agent": first.get("agent") or "claude", "effort": "high", "tier": "sonnet",
                          "same_as": None, "parent": None, "detached": False, "role": None, "kind": "task",
                          "group": "quality", "work_order": goal, "command": "", "status": "queued",
                          "terminal": None, "workload": None, "error": None, "after": []})
        self._event(self.jobs[-1], "all parts are in; this worker checks the joined package like a client")
        return True

    # -- branch per worker: the queen is the master of main ---------------------

    SYNC_NOTICE = ("main moved on to {head} ({what}). When you reach a good point, bring it into your "
                   "branch with `git merge main` and run the tests, so your part keeps fitting the rest.")

    def enable_branches(self, root: str) -> Optional[str]:
        """Give every task worker its own branch of `root`. None when on, else why not."""
        with self._merge_lock:
            why = branches.prepare(root)
            if why:
                self._event_all(f"branches are off: {why}")
                return why
            self.branches = {"root": os.path.abspath(root), "main": branches.main_branch(root)}
            self._synced_head = branches.head(root)
        self._event_all(f"every worker gets its own branch of {root}; the queen merges into {self.branches['main']}")
        return None

    def _merge_branch(self, job: dict[str, Any]) -> tuple[bool, str]:
        root = self.branches["root"]

        def verify() -> tuple[bool, str]:
            v = self.verify
            command = self.merge_check or (v or {}).get("command")
            if not command:
                return True, ""
            code, out = run_captured(verifier.split_command(command), root, 600)
            return code == 0, out if code is not None else out + "\n(the check did not finish in time)"

        with self._merge_lock:
            branches.commit_all(job["worktree"], f"W{job['priority']}: {job.get('title', '')[:60]}")
            kept, detail = branches.merge(root, job["priority"], job.get("title", ""), verify)
            if kept:
                job["merged_at"] = branches.head(root)
        return kept, detail

    def _sync_notice(self) -> None:
        """When main has moved, tell the workers still at work (they merge it
        themselves, at a good moment, in their own folder)."""
        root = self.branches["root"]
        head = branches.head(root)
        if not head or head == self._synced_head:
            return
        self._synced_head = head
        merged = [j for j in self.jobs if j.get("merged_at") == head]
        what = f"W{merged[0]['priority']} merged" if merged else "new commits"
        msg = {"from": "queen", "to": ["all"], "text": self.SYNC_NOTICE.format(head=head, what=what)}
        for j in self.jobs:
            if j.get("worktree") and j["status"] == "agent working" and j.get("merged_at") != head:
                board.deliver(j["project_path"], j["priority"], msg)
                self._event(j, f"told: main moved to {head}")
                self._nudge(j)
        self.board_log.append({"ts": round(time.time()), "from": "queen", "to": ["all"], "text": msg["text"],
                               "delivered": [j["priority"] for j in self.jobs if j.get("worktree")]})

    def _cleanup_worktrees(self) -> None:
        if not self.branches:
            return
        with self._merge_lock:
            for j in self.jobs:
                if j.get("worktree") and j["status"] in self.ENDED:
                    branches.remove_worktree(self.branches["root"], j["priority"])

    # -- the hive's message board ---------------------------------------------

    NUDGE_EVERY_S = 60

    def _hive_key(self, job: dict[str, Any]) -> str:
        return os.path.normcase(os.path.abspath(job.get("hive_root") or job["project_path"]))

    def _route_board(self, where: Any) -> None:
        """Read what workers (and the queen, and the user) posted to the board
        of this worker's folder and route it: into each addressed worker's
        inbox, and into the stream of the sender and of the queen. Workers on
        their own branches have a folder each; they are one hive."""
        job0 = where if isinstance(where, dict) else next(
            (j for j in self.jobs if j.get("project_path") == where), {"project_path": where})
        folder = job0["project_path"]
        hive = self._hive_key(job0)
        key = os.path.normcase(os.path.abspath(folder))
        at = self._board_at.get(key, 0)
        msgs, self._board_at[key] = board.read_new(folder, at)
        for msg in msgs:
            active = [j["priority"] for j in self.jobs if j.get("kind") == "task"
                      and j["status"] not in self.ENDED and j.get("project_path")
                      and self._hive_key(j) == hive]
            to = board.recipients(msg, active)
            self.board_log.append({"ts": round(time.time()), "from": msg["from"], "to": msg["to"],
                                   "text": msg["text"], "delivered": to, **({"kind": "finding"} if msg.get("kind") == "finding" else {})})
            del self.board_log[:-200]
            if msg.get("kind") == "finding":
                root = job0.get("hive_root") or folder
                if board.keep_finding(root, msg["from"], msg["text"], supersedes=board.supersede_hint(msg["text"])):
                    self.findings.append({"ts": round(time.time()), "from": msg["from"], "text": msg["text"]})
                    del self.findings[:-200]
            by = {j["priority"]: j for j in self.jobs}
            label = ", ".join(msg["to"])
            sender = by.get(int(msg["from"][1:])) if msg["from"][:1] in ("P", "W") else None
            if sender is not None:
                self._event(sender, f"told {label}: {msg['text'][:160]}")
            self._event_all(f"{msg['from']} -> {label}: {msg['text'][:160]}")
            for p in to:
                board.deliver(by[p]["project_path"], p, msg)
                self._event(by[p], f"message from {msg['from']}: {msg['text'][:160]}")
                self._nudge(by[p])

    def _nudge(self, job: dict[str, Any]) -> None:
        """A worker that has stopped to wait is told it has mail; one that is
        working reads its inbox between steps (its brief says so)."""
        if not job.get("terminal") or time.time() - float(job.get("nudged_at") or 0) < self.NUDGE_EVERY_S:
            return
        state, _ = self.state_of(job["terminal"])
        if state not in ("waiting", "idle"):
            return
        job["nudged_at"] = time.time()
        try:
            self.host._call("terminal_write", job["terminal"],
                            f"You have a new message: read .queenbee/inbox/W{job['priority']}.md and act on it.")
            time.sleep(0.3)
            self.host._call("terminal_write", job["terminal"], "\r")
        except HostUnavailable:
            pass

    def say(self, to: Any, text: str, sender: str = "queen") -> dict[str, Any]:
        """The queen (or the user, through her) speaks to workers: same board."""
        text = (text or "").strip()
        msg = board.clean({"from": sender, "to": to, "text": text})
        if msg is None:
            return {"ok": False, "error": "say something, to all, queen or W<number>"}
        with self.lock:
            seen: dict[str, dict[str, Any]] = {}
            for j in self.jobs:
                if j.get("kind") == "task" and j.get("project_path"):
                    seen.setdefault(os.path.normcase(os.path.abspath(j["project_path"])), j)
            for j in seen.values():
                board.post(j["project_path"], sender, msg["to"], text)
                self._route_board(j)
        return {"ok": True}

    MAX_STEERS = 2
    STEER = ("Your queen here. Do not wait for anyone: you decide, as the work package and your part say. "
             "Finish your part completely, run its tests, then write .queenbee/W{n}.json with status "
             "\"done\" (or \"blocked\" with the reason).")

    def _steer(self, job: dict[str, Any]) -> None:
        """The queen tells a worker that stopped mid-part to go on."""
        job["steered"] = job.get("steered", 0) + 1
        job["steered_at"] = time.time()
        try:
            self.host._call("terminal_write", job["terminal"], self.STEER.format(n=job["priority"]))
            time.sleep(0.3)
            self.host._call("terminal_write", job["terminal"], "\r")
        except HostUnavailable:
            return
        self._event(job, "the queen told it to go on: it had stopped without finishing")

    def _agent_alive(self, job: dict[str, Any]) -> bool:
        if not job.get("terminal"):
            return False
        try:
            return any(t.get("id") == job["terminal"] and t.get("running")
                       for t in self.host._call("terminals") or [])
        except HostUnavailable:
            return False

    def _reopen(self) -> bool:
        """New work in a finished plan: follow it again. True when the
        following thread must be started again."""
        was = self.done.is_set()
        self.done.clear()
        self.report, self.finished_at = None, 0.0
        return was

    def add_job(self, goal: str, folder: str, agent: str, tier: Optional[str],
                parent: Optional[int] = None, acceptance: Optional[list[dict[str, Any]]] = None,
                title: str = "", plain: bool = False, bare: bool = False) -> dict[str, Any]:
        """A worker the user adds from the map, with a task in their words."""
        goal = goal.strip()
        if not goal:
            return {"ok": False, "error": "give the worker a task"}
        if not folder or not os.path.isdir(folder):
            return {"ok": False, "error": f"folder does not exist: {folder or '(empty)'}"}
        if agent not in self.MODELS:
            return {"ok": False, "error": f"unknown agent {agent}"}
        if tier and not self._model_ok(agent, tier):
            return {"ok": False, "error": f"{agent} has no model {tier}"}
        with self.lock:
            if parent is not None and not any(j["priority"] == parent for j in self.jobs):
                return {"ok": False, "error": f"no job W{parent}"}
            n = max((j["priority"] for j in self.jobs), default=self.first_priority - 1) + 1
            words = goal.split()
            job = {"priority": n, "title": title.strip()[:60] or (" ".join(words[:5])[:60] + ("…" if len(words) > 5 else "")),
                   "goal": goal, "project_path": folder, "agent": agent, "effort": "medium",
                   "tier": None if tier in (None, "", "default") else tier, "same_as": None,
                   "parent": parent, "detached": False, "role": None, "kind": "task",
                   "work_order": goal, "command": "", "status": "queued", "terminal": None,
                   "workload": None, "error": None, "plain": bool(plain), "bare": bool(bare),
                   # Checks the queen runs herself when the worker says done (see verifier.py).
                   "acceptance": verifier.normalise(acceptance, trusted=True)}
            self.jobs.append(job)
            if self.verify and self.verify.get("state") in ("passed", "failed"):
                self.verify.update(state="waiting", rounds=0)       # a new worker: the whole is checked again
            self._event(job, f"added by you: {agent} {tier or ''}".rstrip()
                             + (f"; done is proved by {len(job['acceptance'])} check(s)" if job["acceptance"] else ""))
            restart = self._reopen()
        if restart or not (self._thread and self._thread.is_alive()):
            self.start()
        return {"ok": True, "priority": n}

    def remove_job(self, priority: int) -> dict[str, Any]:
        """Take a worker that is over (done, failed or stopped) off the hive. Useless workers are
        removed by the user; one that is still at work must be stopped first."""
        with self.lock:
            job = next((j for j in self.jobs if j["priority"] == priority), None)
            if job is None:
                return {"ok": False, "error": f"no job W{priority}"}
            if job["status"] not in self.ENDED:
                return {"ok": False, "error": f"W{priority} is still at work; stop it first"}
            if any(j.get("parent") == priority for j in self.jobs):
                return {"ok": False, "error": f"W{priority} has workers under it; remove those first"}
            for key in ("terminal", "workload"):
                if job.get(key):
                    try:
                        self.host._call("terminal_close", job[key])
                    except HostUnavailable:
                        pass
            self.jobs.remove(job)
            self._event_all(f"W{priority} was removed by you")
            return {"ok": True}

    def kill(self, priority: int) -> dict[str, Any]:
        """Stop a worker and everything under it: close its terminals."""
        with self.lock:
            job = next((j for j in self.jobs if j["priority"] == priority), None)
            if job is None:
                return {"ok": False, "error": f"no job W{priority}"}
            doomed, frontier = [], [job]
            while frontier:
                j = frontier.pop()
                doomed.append(j)
                frontier += [k for k in self.jobs if k.get("parent") == j["priority"]]
            for j in doomed:
                for key in ("terminal", "workload"):
                    if j.get(key):
                        try:
                            self.host._call("terminal_close", j[key])
                        except HostUnavailable:
                            pass
                if j["status"] not in self.ENDED:
                    j["status"], j["error"] = "stopped", "stopped by you"
                    j["branched"] = j.get("branched") or j.get("role") == "mother"
                    self._event(j, "stopped by you")
            # A stop is the user's word: the queen must not run her check and
            # start an integration worker on work that was stopped on purpose.
            if self.verify and self.verify.get("state") in ("waiting", "running", "fixing"):
                self.verify["state"] = "cancelled"
                self._event_all("the queen's check is cancelled: you stopped the work")
            return {"ok": True, "stopped": [j["priority"] for j in doomed]}

    # -- branches: an agent that is a Mother of its own agents ---------------

    ENDED = ("done", "failed", "stopped", "stopped early")

    # -- limits on recursion ---------------------------------------------------

    @staticmethod
    def _norm(text: str) -> str:
        return " ".join((text or "").lower().split())

    def depth_of(self, job: dict[str, Any]) -> int:
        """How many queens sit above this job (0 for the top queen's own agents)."""
        by = {j["priority"]: j for j in self.jobs}
        depth, cur, seen = 0, job, set()
        while cur.get("parent") is not None and cur["priority"] not in seen:
            seen.add(cur["priority"])
            parent = by.get(cur["parent"])
            if parent is None or parent.get("role") != "mother":
                break
            depth, cur = depth + 1, parent
        return depth

    def branch_refusal(self, job: dict[str, Any], instruction: str) -> Optional[str]:
        """Why this job may not become a queen of its own, or None. A queen
        that opens queens multiplies planning cost before any work is done, so
        each branch must be shallow, new and within a budget."""
        max_depth = int(self.cfg.get("max_depth", 1))
        if self.depth_of(job) >= max_depth:
            return f"depth limit: a branch may go {max_depth} level(s) below the top queen"
        norm = self._norm(instruction)
        if norm and norm == self._norm(self.instruction):
            return "it repeats the queen's own instruction; splitting it again adds only cost"
        for other in self.jobs:
            if other is not job and other.get("branched") and self._norm(other.get("instruction", "")) == norm:
                return f"W{other['priority']} already branched on this same instruction"
        spent = float((self.costs.get("branches") or {}).get("cost_usd") or 0)
        cap = float(self.cfg.get("branch_budget_usd", 1.0))
        if spent >= cap:
            return f"branch budget used: ${spent:.2f} of ${cap:g} spent planning branches"
        return None

    def _follow_branch(self, job: dict[str, Any]) -> bool:
        """Advance a job marked as a Mother. True when it is handled here --
        planning or following its own agents -- rather than as an agent. A job
        marked while its agent works finishes that work first, then branches."""
        if not job.get("branched"):
            if job["status"] not in ("queued", *self.ENDED):
                return False
            if self.branch_planner is None:
                job.update(branched=True, status="failed", error="this Mother cannot plan branches")
                return True
            why = self.branch_refusal(job, job.get("instruction") or job.get("goal") or "")
            if why:
                job.update(role=None, error=None)
                self._event(job, f"will not branch: {why}")
                return False
            job.update(branched=True, status="planning branch", error=None, branch_from=job["status"])
            self._event(job, f"planning its own branch: {job.get('instruction') or job.get('goal')}")
            threading.Thread(target=self._plan_branch, args=(job,), name=f"mother-branch-W{job['priority']}",
                             daemon=True).start()
            return True
        if job["status"] != "branch running":
            return True                      # still planning, or its branch has ended
        kids = [j for j in self.jobs if j.get("parent") == job["priority"]]
        if kids and all(k["status"] in self.ENDED for k in kids):
            ok = sum(k["status"] in ("done", "stopped early") for k in kids)
            job["status"] = "done" if ok else "failed"
            job["error"] = None if ok else "none of its agents finished"
            self._event(job, f"branch finished: {ok}/{len(kids)} agents done")
        return True

    def _plan_branch(self, job: dict[str, Any]) -> None:
        """Plan the branch outside the lock (model calls take minutes), then
        graft its jobs under this one."""
        try:
            opts = {k: job[k] for k in ("queen_model", "worker_model") if job.get(k)}
            args = (job.get("instruction") or job.get("goal") or "", job["project_path"])
            plan = self.branch_planner(*args, opts) if opts else self.branch_planner(*args)
        except Exception as exc:
            with self.lock:
                job["status"], job["error"] = "failed", f"could not plan its branch: {exc}"
                self._event(job, job["error"])
            return
        with self.lock:
            self._graft(job, plan)

    def _graft(self, job: dict[str, Any], plan: dict[str, Any]) -> None:
        """Add a branch's planned jobs to the tree, numbered after every job
        so far and hanging under the job that planned them."""
        planned = sorted(plan.get("jobs") or [], key=lambda j: j["priority"])
        if not planned:
            job["status"], job["error"] = "failed", "its branch plan has no jobs"
            self._event(job, job["error"])
            return
        cap = int(self.cfg.get("max_children", 4))
        if len(planned) > cap:
            self._event(job, f"its plan has {len(planned)} agents; only the first {cap} start")
            planned = planned[:cap]
        self._append_planned(planned, job["priority"], f"planned by W{job['priority']}'s branch",
                             job["project_path"])
        costs = plan.get("costs") or {}
        mine = self.costs.setdefault("branches", {"tokens": 0, "cost_usd": 0.0})
        mine["tokens"] += int(costs.get("tokens") or 0)
        mine["cost_usd"] = round(mine["cost_usd"] + float(costs.get("cost_usd") or 0), 4)
        job["status"] = "branch running"
        self._event(job, f"branch planned: {len(planned)} agents"
                         + (f" (${float(costs['cost_usd']):.3f})" if costs.get("cost_usd") else ""))

    def graft(self, planned: list[dict[str, Any]], costs: dict[str, Any], instruction: str = "") -> int:
        """A queen's next command while it already has workers: the new
        workers join its tree at the top, next to the others."""
        planned = sorted(planned, key=lambda j: j["priority"])
        with self.lock:
            self._append_planned(planned, None, "planned by the queen")
            for part in ("planner", "architect"):
                c = costs.get(part) or {}
                mine = self.costs.setdefault(part, {"tokens": 0, "cost_usd": 0.0})
                mine["tokens"] = int(mine.get("tokens") or 0) + int(c.get("tokens") or 0)
                mine["cost_usd"] = round(float(mine.get("cost_usd") or 0) + float(c.get("cost_usd") or 0), 4)
            if instruction:
                self.instruction = f"{self.instruction}\n+ {instruction}" if self.instruction else instruction
            restart = self._reopen()
        if restart or not (self._thread and self._thread.is_alive()):
            self.start()
        return len(planned)

    def _append_planned(self, planned: list[dict[str, Any]], parent: Optional[int], said: str,
                        folder: str = "") -> None:
        base = max((j["priority"] for j in self.jobs), default=0)
        renumber = {p["priority"]: base + i for i, p in enumerate(planned, 1)}
        for p in planned:
            child = {**p, "priority": renumber[p["priority"]], "parent": parent,
                     "detached": False, "role": None, "status": "queued", "terminal": None,
                     "workload": None, "error": None,
                     "same_as": renumber.get(p.get("same_as")) if p.get("same_as") else None,
                     "after": [renumber[a] for a in p.get("after") or [] if a in renumber],
                     "project_path": p.get("project_path") or folder}
            # One primary GPU job per machine, and it is not a branch's.
            for key in ("command", "command_template"):
                if child.get(key):
                    child[key] = re.sub(r"--role\s+primary\b", "--role secondary", child[key])
            if child.get("work_order") and child.get("agent") == "claude" and not child.get("tier"):
                child["tier"] = self.cfg.get("worker_model") or "haiku"
            self.jobs.append(child)
            self._event(child, said)

    def fail(self, priority: int, why: str) -> None:
        """Stop a job that cannot go on (its agent is out of requests)."""
        with self.lock:
            for job in self.jobs:
                if job["priority"] == priority and job["status"] not in ("done", "failed", "stopped early"):
                    job["status"], job["error"] = "failed", f"needs you: {why}"
                    self._event(job, f"failed: {why}")
            if self.sweep is not None:
                for lane in self.sweep["lanes"]:
                    if any(p == priority for p in (lane.get("active") or {}).values()):
                        lane.update(stopped=True, why=why)
                        lane["active"] = {k: p for k, p in lane["active"].items() if p != priority}

    # -- sweeping ------------------------------------------------------------

    def start_sweep(self, spec: dict[str, Any], goal: str = "max") -> None:
        """Turn the plan's first job into the first value of the first lane."""
        self.sweep = dict(spec, goal=goal, log=[])
        # Judge no run before the swept setting acts, when the architect found
        # when that is (a warm-up, a reward that starts later), plus a margin.
        eff = self.jobs[0].get("effective_epoch") if self.jobs else None
        if isinstance(eff, (int, float)) and eff > 0:
            self.sweep["min_epoch"] = max(self.sweep.get("min_epoch", 8), int(eff) + 5)
        # As many values run at once as there are lanes; a lane that stops
        # hands its slot to the others.
        self.sweep.setdefault("slots", max(2, len(self.sweep["lanes"])))
        self.sweep.setdefault("repeats", 1)
        for lane in self.sweep["lanes"]:
            lane.update(runs=[], active={}, stopped=False, why="")
        first = self.sweep["lanes"][0]
        proto = self.jobs[0]
        proto.update(lane=first["name"], value=first["values"][0], expect_epochs=spec.get("epochs"))
        for key in ("command", "command_template"):
            if proto.get(key):
                proto[key] = re.sub(r"--role\s+primary\b", "--role secondary", proto[key])
        # The first value's line comes from the template too: the architect
        # once wrote the script's default (1.0) there, so "lambda 0.1" ran with
        # 1.0 and 0.1 was never run.
        k = self.sweep["repeats"]
        if proto.get("command_template"):
            proto["command"] = sweeping.fill(proto["command_template"], first["values"][0], 1, k,
                                             sweeping.seed_for(self.sweep, 1), self._seed_flag(proto))
            proto["seed"] = sweeping.seed_for(self.sweep, 1)
        if proto.get("metrics_template"):
            proto["metrics_path"] = sweeping.fill(proto["metrics_template"], first["values"][0], 1, k)
        proto["rep"] = 1
        first["active"][sweeping.akey(first["values"][0], 1)] = proto["priority"]

    def _seed_flag(self, proto: dict[str, Any]) -> str:
        return proto.get("seed_flag") or self.sweep.get("seed_flag") or ""

    def _lane_job(self, proto: dict[str, Any], lane: dict[str, Any], value: float, rep: int = 1) -> dict[str, Any]:
        n = max(j["priority"] for j in self.jobs) + 1
        # Sweep runs share the GPU by its caps: none of them is the primary.
        tmpl = re.sub(r"--role\s+primary\b", "--role secondary", proto.get("command_template") or "")
        job = {
            "priority": n, "title": f"{self.sweep['param'].lstrip('-')} = {value:g} ({lane['name']})"
                                    + (f" #{rep}" if self.sweep.get("repeats", 1) > 1 else ""),
            "goal": proto.get("goal", ""), "project_path": proto["project_path"],
            "agent": proto.get("agent"), "effort": "low", "same_as": proto["priority"],
            "work_order": proto.get("work_order", ""),
            "command": sweeping.fill(tmpl, value, rep, self.sweep.get("repeats", 1),
                                     sweeping.seed_for(self.sweep, rep), self._seed_flag(proto)),
            "seed": sweeping.seed_for(self.sweep, rep),
            "metrics_path": sweeping.fill(proto.get("metrics_template") or "", value, rep,
                                          self.sweep.get("repeats", 1)),
            "metric_key": proto.get("metric_key", ""), "files": proto.get("files"),
            "status": "queued", "terminal": None, "workload": None, "error": None,
            "lane": lane["name"], "value": value, "rep": rep, "expect_epochs": self.sweep.get("epochs"),
            "parent": proto["priority"], "detached": False, "role": None,
        }
        self.jobs.append(job)
        lane.setdefault("active", {})[sweeping.akey(value, rep)] = n
        self._event(job, f"lane {lane['name']}: next value {value:g}"
                         + (f" (repeat {rep})" if self.sweep.get("repeats", 1) > 1 else ""))
        return job

    def _score(self, job: dict[str, Any]) -> Optional[float]:
        rel, key = job.get("metrics_path") or "", job.get("metric_key") or ""
        if rel.lower().endswith(".jsonl"):
            hist = sweeping.read_history(job["project_path"], rel, key)
            job["best_score"] = sweeping.best_of(hist, self.sweep["goal"])
            if self.sweep.get("score_on") == "best":
                return job["best_score"]
            return sweeping.final_of(hist)
        from .results import read_value
        return read_value(job["project_path"], rel, key)

    def _advance_sweep(self) -> None:
        sw = self.sweep
        by_priority = {j["priority"]: j for j in self.jobs}
        proto = self.jobs[0]
        lanes = sw["lanes"]
        for lane in lanes:                         # plans saved before slots
            if "active" not in lane:
                lane["active"] = {sweeping.akey(lane["current"]): lane["job"]} \
                    if lane.get("job") and lane.get("current") is not None else {}
        if not proto.get("command_template"):
            if not sw.get("broken"):
                sw["broken"] = True
                self._event_all("sweep stopped: the prepared work order has no command template")
                for lane in lanes:
                    lane.update(stopped=True, why="no command template")
            return
        adapted = bool(proto.get("workload")) or proto["status"] in ("done", "stopped early")
        goal, tol = sw["goal"], sw.get("tolerance", 0.0)
        references = [sweeping.read_history(j["project_path"], j["metrics_path"], j["metric_key"])
                      for j in self.jobs if j.get("lane") and j["status"] == "done"]

        def stop(job: dict[str, Any], why: str) -> None:
            try:
                self.host._call("terminal_close", job["workload"])
            except HostUnavailable:
                pass
            job["status"] = "stopped early"
            self._event(job, f"stopped early: {why}")

        for lane in lanes:
            for key, prio in list(lane["active"].items()):
                job = by_priority.get(prio)
                if job is None:
                    del lane["active"][key]
                    continue
                # A running value that is clearly losing gives its GPU back.
                if job["status"] == "GPU job submitted" and job.get("workload"):
                    hist = sweeping.read_history(job["project_path"], job["metrics_path"], job["metric_key"])
                    why = sweeping.should_stop_early(hist, references, sw, goal)
                    if why:
                        stop(job, why)
                if job["status"] in ("done", "stopped early", "failed", "stopped"):
                    score = self._score(job) if job["status"] in ("done", "stopped early") else None
                    lane["runs"].append([job["value"], score, job["status"] == "stopped early",
                                         job.get("rep", 1), job.get("best_score"), job.get("seed")])
                    del lane["active"][key]
                    self._event(job, f"lane {lane['name']}: {job['value']:g} scored "
                                     + ("—" if score is None else f"{score:.4f}"))
            if lane.get("stopped"):
                continue
            why = sweeping.peak_passed(lane, goal, tol, sw.get("repeats", 1))
            if why:
                lane.update(stopped=True, why=why)
                self._event_all(f"lane {lane['name']} stopped: {why}")
                # Values still running further along this lane are past the
                # peak too: their GPU goes to what is left.
                means = [d for d in sweeping.value_scores(lane, sw.get("repeats", 1)).values()
                         if d["mean"] is not None]
                pick = max if goal != "min" else min
                peak = pick(means, key=lambda d: d["mean"])["value"]
                for key, prio in list(lane["active"].items()):
                    job = by_priority[prio]
                    if sweeping.beyond(lane, job["value"], peak) and job["status"] == "GPU job submitted":
                        stop(job, "its lane is past its peak")

        # Fill the free slots: the lane with the fewest running values first.
        while adapted:
            if sum(len(l["active"]) for l in lanes) >= sw.get("slots", len(lanes)):
                break
            started = False
            for lane in sorted((l for l in lanes if not l.get("stopped")), key=lambda l: len(l["active"])):
                nxt, why = sweeping.next_value(lane, lanes, sw.get("repeats", 1))
                if nxt is None:
                    if not lane["active"]:
                        lane.update(stopped=True, why=why)
                        self._event_all(f"lane {lane['name']} stopped: {why}")
                    continue
                self._lane_job(proto, lane, *nxt)
                started = True
                break
            if not started:
                break
        for lane in lanes:                          # nothing left and nothing running
            if (not lane.get("stopped") and not lane["active"]
                    and sweeping.next_value(lane, lanes, sw.get("repeats", 1))[0] is None):
                lane.update(stopped=True, why=sweeping.next_value(lane, lanes, sw.get("repeats", 1))[1])
                self._event_all(f"lane {lane['name']} stopped: {lane['why']}")
        if all(l.get("stopped") and not l["active"] for l in lanes) and not sw.get("result"):
            sw["result"] = sweeping.summary(lanes, goal, sw.get("repeats", 1))
            best = sw["result"]
            if best.get("best") is not None:
                spread = f" ± {best['std']:.4f}, n={best['n']}" if best.get("std") is not None else ""
                self._event_all(f"sweep finished: best {sw['param']} = {best['best']:g} "
                                f"({best['score']:.4f}{spread}, lane {best['lane']})")

    # -- surviving a closed window ----------------------------------------

    @staticmethod
    def state_path():
        from ..config import _state_dir

        return _state_dir() / "mother_plan.json"

    @classmethod
    def path_for(cls, queen: str = "q1"):
        """The first queen keeps the file plans were always saved in."""
        base = cls.state_path()
        return base if queen == "q1" else base.with_name(f"mother_plan_{queen}.json")

    def save(self) -> None:
        """Write the plan and each job's progress, so closing and reopening
        the Mother window picks up where it was. The agents themselves live
        in the host and never depended on the window."""
        try:
            path = self.path_for(self.queen)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"started_at": self.started_at, "jobs": self.view(),
                                       "compare": self.compare, "instruction": self.instruction,
                                       "costs": self.costs, "gpu": self.gpu, "report": self.report,
                                       "finished_at": self.finished_at, "sweep": self.sweep,
                                       "verify": self.verify, "board": self.board_log,
                                       "approvals": self.approvals}),
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            log.debug("could not save the plan", exc_info=True)

    @classmethod
    def restore(cls, cfg: dict[str, Any], host: HostClient, ui_port: int = 7878,
                queen: str = "q1") -> Optional["Dispatch"]:
        try:
            saved = json.loads(cls.path_for(queen).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        d = cls([], cfg, host, ui_port)
        d.queen = queen
        d.jobs = [dict(j) for j in saved.get("jobs") or []]
        d.started_at = saved.get("started_at") or time.time()
        for j in d.jobs:                         # a check cut short by a restart runs again
            if j.get("status") == "verifying":
                j.update(status="agent working", accept_state=None)
        d.board_log = list(saved.get("board") or [])
        d.compare = saved.get("compare")
        d.instruction = saved.get("instruction") or ""
        d.costs = saved.get("costs") or {}
        d.gpu = saved.get("gpu") or {"busy_s": 0.0, "idle_s": 0.0}
        d.report = saved.get("report")
        d.finished_at = saved.get("finished_at") or 0.0
        d.sweep = saved.get("sweep")
        d.approvals = saved.get("approvals") or []
        d.verify = saved.get("verify") or {}
        if d.verify.get("state") == "running":
            d.verify["state"] = "waiting"          # the check died with the window: run it again
        for j in d.jobs:                     # plans saved before the tree existed
            j.setdefault("detached", False)
            j.setdefault("role", None)
            if "parent" not in j:
                j["parent"] = j.get("same_as") if j.get("lane") and j.get("same_as") else None
            if j.get("status") == "planning branch":
                # The planning call died with the window: plan it again.
                j.update(branched=False, status=j.get("branch_from") or "queued")
        if not d.jobs:
            return None
        if all(j.get("status") in ("done", "failed", "stopped") for j in d.jobs) and not any(
                j.get("role") == "mother" and not j.get("branched") and not j.get("detached") for j in d.jobs):
            d.done.set()
        return d

    def run(self) -> None:
        while not self.done.is_set():
            self.step()
            self.save()
            self.done.wait(3)
        self.save()

    def start(self) -> "Dispatch":
        from . import changes

        # The folders as they are before any worker touches them, so the window can show what was changed.
        for folder in sorted({j.get("project_path") for j in self.jobs if j.get("kind") == "task" and j.get("project_path")}):
            if os.path.isdir(folder) and changes.save_baseline(folder, self.started_at):
                # A new plan: what is already on this folder's board belongs to an earlier one and is not routed.
                try:
                    self._board_at[os.path.normcase(os.path.abspath(folder))] = os.path.getsize(board.board_path(folder))
                except OSError:
                    pass
        self._thread = threading.Thread(target=self.run, name="queenbee-mother-dispatch", daemon=True)
        self._thread.start()
        return self

    def view(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(j) for j in self.jobs]
