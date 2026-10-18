"""Instruction -> plan, in one model call with no tools.

The planner only decides what the jobs are; it does not look at the disk or
run anything, which keeps it to a single short, cheap call. The user reviews
and edits the plan before anything starts.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import Any

from ..agents.catalog import binary_for
from ..projects import ProjectSet
from . import hints, modes, verifier
from . import sweep as _sweep
from .config import AGENTS, EFFORTS

PROMPT = """You are a Queen Bee in QueenBee: the boss who runs this work package in the
user's place. The user hands it to you and steps away; you are responsible for
getting it done, but you never do the work yourself: you split it into worker
bees -- sub-agents that each run in their own terminal -- and direct them. Workers
report their results to you; you answer their questions and decide for them
within the package, so nothing waits on the user; only what is outside the
package (another folder, deleting, installing) goes to the user. Plan at least
one worker.

Known projects (name: path):
{projects}
{default}
Rules:
- Never create a job only to compare, rank or score other jobs' results. Mother
  compares finished jobs itself from their output files at no cost. Put what to
  compare in "compare" instead: the metric name and whether higher or lower is
  better; null when the instruction asks for no comparison.
{strategy_rule}
- One job per worker, in the user's priority order (1 = highest). With no
  priorities given, keep the order of the instruction.
- You are the architect of the split: read the project files given below and cut
  the package along its real parts. Each worker's goal names the files it owns
  and the interfaces it must keep to, so workers never edit each other's files;
  put those files in "files".
{team_rule}
- after: the priorities a worker must wait for; [] when it can start at once.
- group: the cluster a worker belongs to, one or two words ("frontend", "backend",
  "tests", "training", "docs"); workers of one part share a group.
- kind: "gpu" for a job that runs a training or other long GPU workload through
  QueenBee; "task" for everything else (writing or changing code, data work, docs,
  reviews). A "task" worker starts at once and ends when it reports done.
- project_path: the known project path that holds this work. If none fits, a path
  the instruction names; otherwise "".
- Spend as few tokens as possible, but never at the cost of the result: each
  worker gets the least capable model that will succeed. effort says how hard
  its part is: "low" (mechanical: moving code, renaming, boilerplate, a re-run),
  "medium" (ordinary code to a clear spec), "high" (intricate rules, algorithms,
  edge cases a light model tends to get wrong). low and medium parts go to a
  light model, high ones to a strong one; a part that fails moves up anyway.
{agents_rule}
- A job that repeats an earlier job's workload in the same project: same_as = that
  job's priority, effort "low". Otherwise same_as = null.
- goal: one or two sentences a sub-agent can act on without this conversation;
  keep every duration, setting and name the user gave.
- A sweep: when the instruction asks to try one setting over a range of values
  (e.g. "lambda from 0.1 upward and from 1.0 downward in parallel"), return ONE job
  -- the first run, which adapts the script -- and a "sweep": the command-line flag
  swept ("param", e.g. "--lambda-reward"), its lanes (each a start and a signed
  step; two lanes from both ends when the user asks for that), the lowest and
  highest value, and the epochs per run when the user gave them. When the user
  lists the values ("0.1, 0.4, 0.7 and 1.0"), give one lane with those "values"
  instead of start and step. When each value is to run several times ("3 times
  each"), set "repeats". Mother runs the other values itself. Otherwise "sweep"
  is null.
- acceptance: per job, 1-4 typed checks the queen runs herself when the worker says
  it is done, so "done" is proved, not claimed. Types: {{"type":"command","run":
  "python -m unittest tests.test_cart"}} (test runners, "python -m py_compile f.py"
  or "python f.py --help" only), {{"type":"file_exists","path":"..."}},
  {{"type":"file_contains","path":"...","text":"..."}}, {{"type":"jsonl_rows",
  "path":"...","min_rows":2}}. Paths are relative to the project. [] when none can
  be stated.
- resource_estimate: per job, {{"gpu_mb": 0, "duration_min": 0}} -- your estimate (0 if unknown).
- verify: one command that proves the whole package works once every worker is
  done -- the project's test runner (e.g. "python -m unittest discover -s tests",
  "python -m pytest", "npm test"); "" when there is none. The queen runs it and
  sends failures back to a worker.
- context: a compact brief for the whole team, under 50 lines: what the project is, its parts and the interfaces between
  them (file and function names), the conventions, how to run the tests. Every worker reads it first, so that none of them
  has to explore the project again to find out what the others already know. "" when there is a single job.
- title: at most five words.
- Write title, goal and notes in {language_rule} with its proper letters
  (accented letters stay as written); never strip diacritics.

Reply with JSON only, no prose:
{{"jobs":[{{"priority":1,"title":"","goal":"","group":"","kind":"task","senior":false,"files":[],"after":[],"project_path":"","agent":"claude","effort":"medium","same_as":null,
   "acceptance":[{{"type":"command","run":"python -m unittest discover -s tests"}}],"resource_estimate":{{"gpu_mb":0,"duration_min":0}}}}],
 "compare":{{"metric":"","goal":"max"}},
 "sweep":{{"param":"--lambda-reward","lanes":[{{"name":"up","start":0.1,"step":0.1}},{{"name":"down","start":1.0,"step":-0.1}}],"min":0.1,"max":1.0,"epochs":20}},
 "verify":"python -m unittest discover -s tests",
 "context":"",
 "notes":""}}

{given}Instruction:
{instruction}
"""


class PlanError(RuntimeError):
    pass


def _projects_text() -> str:
    rows = []
    for p in ProjectSet.load().listing():
        for path in p.get("paths") or []:
            rows.append(f"- {p['name']}: {path}")
    return "\n".join(rows) or "- (none configured)"


def _extract_json(text: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise PlanError("the planner did not return a plan")
    try:
        return json.loads(text[start:end + 1])
    except ValueError as exc:
        raise PlanError(f"the planner returned malformed JSON: {exc}") from exc


def _given(folder: str, instruction: str) -> str:
    """The project's tree and the files the instruction names, read for the
    planner: it has no tools, and cannot split a package it has not seen."""
    if not folder or not os.path.isdir(folder):
        return ""
    from .architect import _tree, given_files

    tree = _tree(folder)
    files = given_files(folder, [instruction], per_file=30_000, total=60_000)
    if not tree and not files:
        return ""
    parts = ["The project, read for you:", "", "Tree:"] + [f"  {t}" for t in tree]
    for rel, text in files:
        parts += ["", f"--- {rel} ---", text]
    return "\n".join(parts) + "\n\n"


def _estimate(raw: Any) -> dict[str, float]:
    out = {"gpu_mb": 0, "duration_min": 0}
    if isinstance(raw, dict):
        for k in out:
            try:
                out[k] = max(0, min(10**7, int(raw.get(k) or 0)))
            except (TypeError, ValueError):
                pass
    return out


def normalise(raw: dict[str, Any], agents: tuple[str, ...] = AGENTS) -> list[dict[str, Any]]:
    """Validate and clean the planner's jobs; the user edits the result."""
    jobs = []
    for i, job in enumerate(raw.get("jobs") or []):
        if not isinstance(job, dict):
            continue
        agent = job.get("agent") if job.get("agent") in agents else agents[0]
        effort = job.get("effort") if job.get("effort") in EFFORTS else "medium"
        try:
            priority = int(job.get("priority") or i + 1)
        except (TypeError, ValueError):
            priority = i + 1
        same_as = job.get("same_as")
        jobs.append({
            "priority": priority,
            "title": str(job.get("title") or f"Job {priority}")[:60],
            "goal": str(job.get("goal") or "").strip(),
            "project_path": str(job.get("project_path") or "").strip(),
            "agent": agent, "effort": effort,
            "group": str(job.get("group") or "").strip()[:24],
            "kind": "gpu" if job.get("kind") == "gpu" else "task",
            "files": [str(f) for f in job.get("files") or [] if isinstance(f, str)][:40],
            "after": [int(a) for a in job.get("after") or [] if isinstance(a, (int, float))],
            "senior": bool(job.get("senior")),
            "acceptance": verifier.normalise(job.get("acceptance")),
            "resource_estimate": _estimate(job.get("resource_estimate")),
            "same_as": int(same_as) if isinstance(same_as, (int, float)) and int(same_as) != priority else None,
        })
    if not jobs:
        raise PlanError("the plan has no jobs")
    jobs.sort(key=lambda j: j["priority"])
    for rank, job in enumerate(jobs, 1):     # 1..n with no gaps or repeats
        job["priority"] = rank
    _drop_circular_checks(jobs)
    return jobs


def _drop_circular_checks(jobs: list[dict[str, Any]]) -> None:
    """A worker that others wait for (the senior of a team) must not be held to tests that only pass once those others are done:
    "python -m unittest discover" as the senior's own check would send her back for ever while the juniors sit queued behind her.
    Its test-running checks are dropped; checks on files stay."""
    waited_for = {a for j in jobs for a in j["after"]}
    for job in jobs:
        if job["priority"] in waited_for:
            job["acceptance"] = [c for c in job["acceptance"]
                                 if not (c["type"] == "command" and ("unittest" in c["run"] or "pytest" in c["run"]))]


def normalise_compare(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict) or not str(raw.get("metric") or "").strip():
        return None
    goal = "min" if str(raw.get("goal") or "").lower() in ("min", "lower", "minimize") else "max"
    return {"metric": str(raw["metric"]).strip()[:80], "goal": goal}


STRATEGY_RULES = {
    # The user chooses how the work runs; the queen does not second-guess it.
    "auto": ("- First judge the size, as a boss would. If one strong agent could finish the\n"
             "  whole package in about fifteen minutes, plan exactly ONE worker with effort\n"
             "  \"high\" for all of it: splitting a small package only adds cost and seams.\n"
             "  Split only a package too large for one agent to hold at once -- many parts,\n"
             "  hours of work -- into parts that different workers can do side by side\n"
             "  (e.g. frontend, backend, tests)."),
    "multi": ("- The user ordered MULTI-AGENT work. Do not judge the package too small to split:\n"
              "  always plan at least TWO workers, cutting the package along its real parts\n"
              "  (e.g. frontend, backend, tests; or independent modules, bugs, files) so that\n"
              "  different workers can do them side by side."),
}
STRATEGIES = ("multi", "single", "auto")

# How a split package is organised. "team": a senior lays the ground and the
# tests first, juniors build to them. "peers": no ground phase -- the spec the
# user gave is the contract, every worker starts at once on its own part.
TEAM_RULE = """- A split package is run as a team: one senior, several juniors.
  * Priority 1 is the senior ("senior": true). It lays the ground only, and
    quickly: the shared data model, the module interfaces and skeleton, and
    acceptance tests written from the spec for every part (they fail until the
    parts are built). No feature code -- keep this phase to a few minutes; the
    team waits on it.
  * The juniors (every other worker, "after": [1]) each build one part until
    the senior's tests for it pass, side by side, without changing those tests.
    Give most of the work to them, in 3-6 parts of similar size.
  * The senior's own "acceptance" lists only checks that pass before any part exists (a file exists, a module
    imports). Never the parts' tests and never a whole-suite run: they fail until the juniors are done, and the
    juniors wait for her.
  * "verify" runs the senior's tests."""
PEERS_RULE = """- A split package is run by PEERS: no senior, no ground phase, every "after" is [] -- all
  workers start at once, each on its own part, working from the spec the user gave (it is the
  contract between the parts). Cut the parts along the spec so their files do not overlap, give
  the parts similar size, and name in each goal the modules or files the worker owns and the
  interfaces it must keep (taken from the spec), so the parts fit together. One worker also
  writes the tests that cover the joins. "verify" runs the whole package's tests."""


def _ask_codex(prompt: str, model: str, timeout_s: float) -> dict[str, Any]:
    """The planner's answer from Codex (`codex exec`, read-only, nothing to read: the prompt holds everything) in the shape
    `claude -p --output-format json` gives, so that the rest of `plan` is the same for both."""
    import time

    codex = binary_for("codex")
    if not codex:
        raise PlanError("Codex is not installed; this queen plans with it")
    began = time.time()
    with tempfile.TemporaryDirectory(prefix="queenbee-mother-") as cwd:
        answer = os.path.join(cwd, "answer.txt")
        argv = [codex, "exec", *(["-m", model] if model and model != "default" else []), "--sandbox", "read-only",
                "--skip-git-repo-check", "--output-last-message", answer, "-"]
        try:
            proc = subprocess.run(argv, input=prompt, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                                  timeout=timeout_s, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise PlanError("the planner took too long") from exc
        try:
            with open(answer, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            raise PlanError((proc.stderr or proc.stdout or "Codex gave no answer").strip()[-300:]) from exc
    from .director import codex_tokens

    return {"result": text, "usage": {"input_tokens": codex_tokens(proc.stdout, proc.stderr)}, "total_cost_usd": 0.0,
            "duration_ms": int((time.time() - began) * 1000)}


def price_hint(prices: dict[str, float] | None) -> str:
    """One line for the planner: relative prices, and the rule that follows from them."""
    if not prices:
        return ""
    listed = ", ".join(f"{k} {v:g}" for k, v in sorted(prices.items(), key=lambda kv: kv[1]))
    return ("\n- price: relative prices per million tokens are " + listed + ". Between two agents or models able to do a part, "
            "take the cheaper one; a hard part (the one the rest depends on) still goes to the stronger one.")


def plan(instruction: str, model: str, timeout_s: float = 240, folder: str = "",
         agents: tuple[str, ...] = AGENTS, strategy: str = "auto", max_workers: int = 0,
         layout: str = "team", planner_agent: str = "claude", prices: dict[str, float] | None = None) -> dict[str, Any]:
    instruction = instruction.strip()
    if not instruction:
        raise PlanError("write an instruction first")
    agents = tuple(x for x in agents if x in AGENTS) or ("claude",)
    # The mode comes first (see modes.py): a small instruction needs no
    # planner, and a plan already made for this instruction and project
    # is reused. Both cost nothing.
    usable = bool(folder) and os.path.isdir(folder)
    strategy = strategy if strategy in STRATEGIES else "auto"
    if usable and (strategy == "single" or (strategy == "auto" and modes.looks_small(instruction))):
        out = modes.direct_plan(instruction, folder, agents)
        out["jobs"] = normalise(out, agents)
        out["mode"] = "DIRECT"
        return out
    max_workers = max(0, min(12, int(max_workers or 0)))
    key = modes.recipe_key(instruction, folder, agents, f"{planner_agent}:{model}/{strategy}/{max_workers}/{layout}") if usable else ""
    if key:
        remembered = modes.recipe_load(folder, key)
        if remembered is not None:
            remembered["mode"] = "RECIPE"
            return remembered
    claude = binary_for("claude")
    if planner_agent != "codex" and not claude:
        raise PlanError("Claude Code is not installed; the planner needs it")
    default = (f"The user's project folder for this instruction: {folder}\n"
               "Use it as project_path for every job unless the instruction names another.\n"
               if folder else "")
    agents = tuple(a for a in agents if a in AGENTS) or ("claude",)
    agents_rule = ('- agent: use both. "codex" for jobs that run or lightly adjust an existing script, or that are a\n'
                   '  self-contained module with clear interfaces and its own tests; "claude" for jobs that must adapt\n'
                   '  existing code or are hard to get right. If the instruction names the agents to use, give every\n'
                   '  one of them at least one job.'
                   if len(agents) > 1 else f'- agent: always "{agents[0]}".') + price_hint(prices)
    prompt = PROMPT.format(projects=_projects_text(), default=default, instruction=instruction,
                           team_rule=PEERS_RULE if layout == "peers" else TEAM_RULE,
                           strategy_rule=STRATEGY_RULES["multi" if strategy == "multi" else "auto"]
                           + (f"\n- Plan at most {max_workers} workers in total (the senior included); "
                              "fewer, larger parts beat many small ones." if max_workers else ""),
                           agents_rule=agents_rule, given=_given(folder, instruction),
                           language_rule=(hints.language(instruction) or "the instruction's language"))
    if planner_agent == "codex":
        out = _ask_codex(prompt, model, timeout_s)
    else:
        # The prompt goes in on stdin: it holds the project's own files and can pass the command line's length limit.
        argv = [claude, "-p", "--strict-mcp-config", "--model", model, "--output-format", "json",
                "--tools", "", "--no-session-persistence"]
        # A scratch directory, so no project's CLAUDE.md or settings shape the plan.
        with tempfile.TemporaryDirectory(prefix="queenbee-mother-") as cwd:
            try:
                proc = subprocess.run(argv, input=prompt, cwd=cwd, capture_output=True, text=True,
                                      encoding="utf-8", timeout=timeout_s,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired as exc:
                raise PlanError("the planner took too long") from exc
        try:
            out = json.loads(proc.stdout)
        except ValueError as exc:
            raise PlanError((proc.stderr or proc.stdout or "no output").strip()[:300]) from exc
        if out.get("is_error"):
            raise PlanError(str(out.get("result") or "the planner failed")[:300])
    raw = _extract_json(str(out.get("result") or ""))
    usage = out.get("usage") or {}
    jobs = normalise(raw, agents)
    if folder and os.path.isdir(folder):
        for job in jobs:
            if not job["project_path"] or not os.path.isdir(job["project_path"]):
                job["project_path"] = folder
    planned = {
        "jobs": jobs,
        "compare": normalise_compare(raw.get("compare")),
        "sweep": _sweep.normalise(raw.get("sweep")),
        "verify": str(raw.get("verify") or "").strip()[:200],
        "notes": str(raw.get("notes") or ""),
        "context": str(raw.get("context") or "").strip()[:6000],
        "planner": {
            "model": model,
            "input_tokens": (usage.get("input_tokens") or 0)
                            + (usage.get("cache_read_input_tokens") or 0)
                            + (usage.get("cache_creation_input_tokens") or 0),
            "output_tokens": usage.get("output_tokens") or 0,
            "cost_usd": out.get("total_cost_usd"),
            "seconds": round((out.get("duration_ms") or 0) / 1000, 1),
        },
    }
    planned["mode"] = modes.mode_of(planned)
    if key:
        modes.recipe_save(folder, key, planned)
    return planned
