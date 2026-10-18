"""The architect: an expensive model thinks once, so cheap models can execute.

For each project folder in the plan, one Opus call with read-only tools
(Read, Glob, Grep; nothing that writes or runs) inspects the project and writes
a work order per job: exactly which file to change and how, the exact command,
and the exact QueenBee submit line. Facts it cannot settle from the project come
back as questions with options, asked once in the Mother window before anything
starts -- not by a worker mid-run while the GPU sits idle.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from typing import Any

from ..agents.catalog import binary_for
from ..gpu import submit_argv
from .planner import PlanError, _extract_json

PROMPT = """You are the architect for QueenBee. Inspect this project read-only and
write work orders that a small, fast model (Haiku) can follow WITHOUT exploring.

Project folder: {folder}
Jobs in this folder, highest priority first:
{jobs}

Every job must meet QueenBee's contract:
- Resumable: the script passes save= to queenbee.gpu.AdaptiveGPU (a function that
  atomically writes model, optimizer, LR schedule, AMP scaler, RNG state and
  epoch/step) and loads that state at start when it exists. If the script already
  does this, say so and do not change it.
- Releasable: the script also passes release= (a function that moves the model
  and optimizer state off the GPU and empties the CUDA cache) and restore= (its
  inverse: moves them back to the GPU; QueenBee calls it before a released job
  runs again). QueenBee refuses a secondary job without release=, and any job that
  shares a script with another one runs as a secondary; so every adapted
  script gets both.
- Launched only through this exact submit line (fill the workload command):
{submit}
  The workload command after `--` is separate arguments, starting with the absolute
  interpreter path already shown in the line; never wrap the whole command in one
  pair of quotes, and never reach the interpreter through a relative path.
- Each job writes to its own output location; never overwrite another run.

{given}Read economically: Grep for what you need first, then Read only those line ranges
(offset/limit). Do not read a large file whole more than once.

For each job write `work_order`: numbered, exact steps. For edits: the file, the
anchor lines to find (quoted), and the exact code to insert or replace. Then the
exact submit line. When several jobs use the same script, only the first job
adapts it; later jobs only run it with their own settings and output location.

For each job also give `metrics_path`: the file (relative to the project folder) in
which that run's final result lands -- a JSON file, or a CSV whose last row holds
the final values -- and `metric_key`: the key or column there that holds {metric}.
Pick them from how the script actually writes its outputs; if it writes no such
file, add writing one to the work order. Mother reads these files to compare runs.

`questions`: only facts you cannot determine from the project that change what
runs (e.g. which configuration, how many epochs). 2-4 concrete options each,
your recommendation first. No questions about things you can read yourself.

For each job also give `files`: the project files the job edits or runs (e.g. the
training script). Mother lets agents whose files do not overlap work at the same
time, and makes a job wait for an earlier one that touches the same file.

{sweep}Reply with JSON only:
{{"jobs":[{{"priority":1,"work_order":"1. ...\\n2. ...","command":"<full submit line>",
   "metrics_path":"runs/p1/metrics.json","metric_key":"val_acc","files":["train.py"]}}],
 "questions":[{{"id":"q1","priority":1,"text":"...","options":["...","..."]}}],
 "notes":""}}
"""


def match_orders(jobs: list[dict[str, Any]], orders: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Plan priority -> work order.

    The architect sometimes numbers the jobs of one folder from 1 on its own
    (a folder holding only P4 came back as "priority 1"), so the order it
    returned never reached P4. Use its numbers when they are the plan's;
    otherwise pair jobs and orders in the order both are listed.
    """
    wanted = [j["priority"] for j in sorted(jobs, key=lambda j: j["priority"])]
    if all(p in orders for p in wanted):
        return {p: orders[p] for p in wanted}
    listed = [orders[k] for k in sorted(orders)]
    return {p: listed[i] for i, p in enumerate(wanted) if i < len(listed)}


def _submit_line(folder: str, job: dict[str, Any], port: int) -> str:
    role = "primary" if job["priority"] == 1 else "secondary"
    argv = submit_argv(["<workload command>"], cwd=".", project_root=folder, name=job["title"],
                       role=role, max_batch=8, port=port)
    return subprocess.list2cmdline(argv)


SWEEP = """This plan is a SWEEP of {param}: Mother itself runs the job again with other
values of {param} ({values}), {epochs}. For the first job also return
`command_template`: its submit line with the swept value written as {{value}} and
every name or output location that must differ per run containing {{tag}} (e.g.
--run-name sweep_{{tag}}), and `metrics_template`: its result file the same way.
The first job's `command` is the template with the first value filled in. Prefer a
per-epoch log (JSON lines, one object per epoch holding {metric}) as the result
file, so Mother can stop a clearly losing run early. Also return `seed_flag`: the script's command-line flag for its random seed (e.g. --seed), or ""
when it has none; write it into command_template as `<flag> {{seed}}`. Mother gives every repeat
its own recorded seed. Also return `effective_epoch`:
the first epoch at which {param} changes training at all (after any warm-up or
delayed start the script has), so no run is judged before it.

"""


SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "env", "node_modules", "weights", "results",
             "runs", "checkpoints", "data", "dataset", "datasets", "wandb", ".queenbee"}
FILE_RE = re.compile(r"[\w./\\-]+\.(?:py|sh|ya?ml|json|toml|cfg|ini|md|txt|js|ts|tsx|html|css)\b")


def _tree(folder: str, depth: int = 3, limit: int = 120) -> list[str]:
    out = []
    for root, dirs, files in os.walk(folder):
        rel = os.path.relpath(root, folder)
        level = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")) if level < depth else []
        for f in sorted(files):
            if f.endswith((".py", ".sh", ".yaml", ".yml", ".toml", ".cfg", ".txt", ".md", ".js", ".ts", ".html", ".css", ".json")):
                out.append(os.path.normpath(os.path.join(rel, f)))
                if len(out) >= limit:
                    return out
    return out


def given_files(folder: str, texts: list[str], per_file: int = 40_000, total: int = 80_000) -> list[tuple[str, str]]:
    """Files the instruction or the jobs name, read by Mother so the architect
    need not spend turns finding and reading them. Found at the named path,
    or by name anywhere in the project's tree."""
    tree = _tree(folder, depth=4, limit=2000)
    picked, used = [], 0
    for name in dict.fromkeys(m for t in texts for m in FILE_RE.findall(t or "")):
        rel = name.replace("/", os.sep).replace("\\", os.sep)
        if not os.path.isfile(os.path.join(folder, rel)):
            rel = next((t for t in tree if os.path.basename(t) == os.path.basename(rel)), "")
        path = os.path.join(folder, rel) if rel else ""
        if not path or not os.path.isfile(path) or any(p == rel for p, _ in picked):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read(per_file + 1)
        except OSError:
            continue
        if len(text) > per_file:
            text = text[:per_file] + "\n... (truncated; Read the rest if needed)"
        if used + len(text) > total:
            break
        picked.append((rel, text))
        used += len(text)
    return picked


def _given_block(folder: str, jobs: list[dict[str, Any]]) -> str:
    files = given_files(folder, [f"{j.get('title', '')} {j.get('goal', '')}" for j in jobs])
    tree = _tree(folder)
    if not files and not tree:
        return ""
    parts = ["Mother has read these for you. Do not Read them again; if they are enough,",
             "answer without using any tool.", "", "Project tree (code and config files):"]
    parts += [f"  {t}" for t in tree]
    for rel, text in files:
        parts += ["", f"--- {rel} (full text) ---", text]
    return "\n".join(parts) + "\n\n"


def recipe_key(folder: str, jobs: list[dict[str, Any]], metric: str, sweep: Any, submits: str) -> str:
    """What makes two architect calls the same call: the jobs, the sweep, the
    submit lines and the exact text of every file the jobs name. Change the
    script and the key changes; nothing else does, so a repeat costs no model."""
    h = hashlib.sha256()
    h.update(json.dumps([[j["title"], j["goal"], j["priority"]] for j in jobs], sort_keys=True).encode())
    h.update(json.dumps([metric, sweep, submits], sort_keys=True, default=str).encode())
    for rel, text in given_files(folder, [f"{j.get('title', '')} {j.get('goal', '')}" for j in jobs],
                                 per_file=10**7, total=10**8):
        h.update(rel.encode())
        h.update(text.encode("utf-8", "replace"))
    return h.hexdigest()[:24]


def _recipe_path(folder: str, key: str) -> str:
    return os.path.join(folder, ".queenbee", "recipes", f"{key}.json")


def _recipe_load(folder: str, key: str) -> dict[str, Any] | None:
    try:
        with open(_recipe_path(folder, key), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("orders"), dict):
        return None
    data["orders"] = {int(k): v for k, v in data["orders"].items()}
    return data


def _recipe_save(folder: str, key: str, result: dict[str, Any]) -> None:
    path = _recipe_path(folder, key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"orders": result["orders"], "questions": result["questions"],
                       "notes": result["notes"]}, fh, indent=1)
        os.replace(path + ".tmp", path)
    except OSError:
        pass            # a cache that cannot be written is only a cache miss next time


def prepare(folder: str, jobs: list[dict[str, Any]], model: str, port: int = 7877,
            timeout_s: float = 900, metric: str = "", sweep: Any = None) -> dict[str, Any]:
    """Work orders and questions for the jobs in one folder. The same jobs over
    the same files are answered from the recipe cache with no model call."""
    claude = binary_for("claude")
    if not claude:
        raise PlanError("Claude Code is not installed; the architect needs it")
    if not os.path.isdir(folder):
        raise PlanError(f"project folder does not exist: {folder}")
    listing = "\n".join(f"- W{j['priority']} {j['title']}: {j['goal']}" for j in jobs)
    submits = "\n".join(f"  W{j['priority']}: {_submit_line(folder, j, port)}" for j in jobs)
    rkey = recipe_key(folder, jobs, metric, sweep, submits)
    cached = _recipe_load(folder, rkey)
    if cached is not None:
        return {**cached, "usage": {"model": "recipe", "input_tokens": 0, "output_tokens": 0,
                                    "cost_usd": 0.0, "seconds": 0.0, "cached": True}}
    prompt = PROMPT.format(folder=folder, jobs=listing, submit=submits, given=_given_block(folder, jobs),
                           metric=metric or "the run's main final score",
                           sweep=(SWEEP.format(
                               param=sweep["param"], metric=metric or "the metric",
                               values=", ".join(f"{v:g}" for lane in sweep["lanes"] for v in lane["values"][:3]) + ", ...",
                               epochs=f"{sweep['epochs']} epochs each" if sweep.get("epochs") else "same epochs each")
                               if sweep else ""))
    # Prompt on stdin: `--tools` takes several values and would swallow a
    # positional prompt after it.
    # --tools only decides which tools exist; with nobody to approve in print
    # mode they must also be pre-approved, or every read is denied. Nothing
    # that writes or runs is available at all.
    argv = [claude, "-p", "--strict-mcp-config", "--model", model, "--output-format", "json",
            "--no-session-persistence", "--add-dir", os.path.abspath(folder),
            "--tools", "Read,Glob,Grep", "--allowedTools", "Read,Glob,Grep"]
    try:
        proc = subprocess.run(argv, input=prompt, cwd=folder, capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout_s,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise PlanError("the architect took too long") from exc
    try:
        out = json.loads(proc.stdout)
    except ValueError as exc:
        raise PlanError((proc.stderr or proc.stdout or "no output").strip()[:300]) from exc
    if out.get("is_error"):
        raise PlanError(str(out.get("result") or "the architect failed")[:300])
    raw = _extract_json(str(out.get("result") or ""))
    usage = out.get("usage") or {}
    orders = {int(j.get("priority")): j for j in raw.get("jobs") or [] if isinstance(j, dict)}
    questions = [q for q in raw.get("questions") or [] if isinstance(q, dict) and q.get("text")]
    for i, q in enumerate(questions, 1):
        q["id"] = f"{os.path.basename(folder)}-{q.get('id') or i}"
        q["options"] = [str(o) for o in (q.get("options") or [])][:4]
    def console(text: str) -> str:
        # A workload under pythonw.exe has no stdout/stderr and dies silently.
        return re.sub(r"(?i)pythonw\.exe", "python.exe", text) if os.path.isfile(
            os.path.join(os.path.dirname(sys.executable), "python.exe")) else text

    for o in orders.values():
        for key in ("work_order", "command", "command_template"):
            if o.get(key):
                o[key] = console(str(o[key]))
    result = {
        "orders": {p: {"work_order": str(o.get("work_order") or ""), "command": str(o.get("command") or ""),
                       "metrics_path": str(o.get("metrics_path") or ""),
                       "metric_key": str(o.get("metric_key") or ""),
                       "files": [str(f) for f in (o.get("files") or []) if f],
                       "command_template": str(o.get("command_template") or ""),
                       "effective_epoch": o.get("effective_epoch"),
                       "seed_flag": str(o.get("seed_flag") or ""),
                       "metrics_template": str(o.get("metrics_template") or "")}
                   for p, o in orders.items()},
        "questions": questions,
        "notes": str(raw.get("notes") or ""),
        "usage": {
            "model": model,
            "input_tokens": (usage.get("input_tokens") or 0)
                            + (usage.get("cache_read_input_tokens") or 0)
                            + (usage.get("cache_creation_input_tokens") or 0),
            "output_tokens": usage.get("output_tokens") or 0,
            "cost_usd": out.get("total_cost_usd"),
            "seconds": round((out.get("duration_ms") or 0) / 1000, 1),
        },
    }
    _recipe_save(folder, rkey, result)
    return result
