"""Where the tokens go: the team's spend by role, and each worker's spend by phase.

A worker's run has two phases. Before its first write it is *exploring*: reading the project, the brief, the board, to find
out what it must know. After the first write it is *building*: editing, running, fixing. The tokens of the first phase are the
cost of finding out; a better brief (CONTEXT.md, FINDINGS.md) should shrink it. This module reads the agents' own transcripts
(Claude Code's project transcripts, Codex's rollouts) and says, per worker, how much went to each phase, how many reads came
before the first write, and how large the context was at the end. `python -m queenbee.mother.economy <project> [--since T]`
prints the table for a folder; the queen's report carries the same numbers.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from typing import Any

from .ledger import _slug

EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Glob", "Grep", "LS"}
# a shell command that writes a file (what Codex does instead of an edit tool, and what either agent may do in Bash)
WRITE_CMD = re.compile(r"apply_patch|Set-Content|Out-File|Add-Content|\btee\b|sed\s+-i|(?<![<>])>\s*[\w./\\\"']|cat\s*>", re.I)


def _empty(agent: str) -> dict[str, Any]:
    return {"agent": agent, "model": "", "job": None, "tokens": {"explore": 0, "build": 0}, "turns": {"explore": 0, "build": 0},
            "reads_before_write": 0, "tools": {}, "first_write_at": None, "started": None, "ended": None, "context_end": 0}


def _job_of(text: str) -> int | None:
    m = re.search(r"\bjob W(\d+)\b", text) or re.search(r"\.queenbee/W(\d+)\.json", text)
    return int(m.group(1)) if m else None


def claude_profile(path: str) -> dict[str, Any]:
    """One Claude Code transcript, by phase. Claude writes one line per content block with the same message id and usage;
    a message is counted once, in the phase it was in when it began."""
    out = _empty("claude")
    seen: set[str] = set()
    phase = "explore"
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            ts = rec.get("timestamp")
            if ts:
                out["started"] = out["started"] or ts
                out["ended"] = ts
            if rec.get("type") == "user" and out["job"] is None:
                content = (rec.get("message") or {}).get("content")
                text = content if isinstance(content, str) else " ".join(
                    str(b.get("text") or "") for b in (content or []) if isinstance(b, dict))
                out["job"] = _job_of(text)
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            usage = msg.get("usage") or {}
            mid = msg.get("id") or rec.get("uuid")
            if msg.get("model") == "<synthetic>":
                continue
            out["model"] = out["model"] or msg.get("model") or ""
            # the tools this message calls (one line per block: the usage is counted once, the blocks every time they appear)
            for block in msg.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name") or "")
                out["tools"][name] = out["tools"].get(name, 0) + 1
                inp = block.get("input") or {}
                target = str(inp.get("file_path") or inp.get("notebook_path") or inp.get("command") or "")
                # bookkeeping writes (.queenbee/W<n>.progress, the report) are not the work: they do not end the exploring phase
                writes = (name in EDIT_TOOLS or (name in ("Bash", "PowerShell") and WRITE_CMD.search(target))) and ".queenbee" not in target
                if phase == "explore":
                    if writes:
                        phase = "build"
                        out["first_write_at"] = ts
                    elif name in READ_TOOLS or name in ("Bash", "PowerShell"):
                        out["reads_before_write"] += 1
            if not usage or mid in seen:
                continue
            seen.add(mid)
            tokens = int((usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
                         + (usage.get("cache_read_input_tokens") or 0) + (usage.get("output_tokens") or 0))
            # a message that makes the first write is counted as building: the write is the point of it
            out["tokens"][phase] += tokens
            out["turns"][phase] += 1
            out["context_end"] = int((usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
                                     + (usage.get("cache_read_input_tokens") or 0))
    return out


def codex_profile(path: str) -> dict[str, Any]:
    """One Codex rollout, by phase. Codex reports a `token_count` with the last turn's usage after each turn; a tool call
    (`exec`, `apply_patch`) that writes switches the phase."""
    out = _empty("codex")
    phase = "explore"
    cwd = ""
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            ts = rec.get("timestamp")
            if ts:
                out["started"] = out["started"] or ts
                out["ended"] = ts
            p = rec.get("payload") or {}
            kind = rec.get("type")
            if kind == "session_meta":
                cwd = p.get("cwd") or cwd
            elif kind == "turn_context":
                out["model"] = out["model"] or p.get("model") or ""
            elif kind == "response_item" and p.get("type") == "message" and out["job"] is None:
                text = " ".join(str(b.get("text") or "") for b in (p.get("content") or []) if isinstance(b, dict))
                out["job"] = _job_of(text)
            elif kind == "response_item" and p.get("type") in ("custom_tool_call", "function_call", "local_shell_call"):
                name = str(p.get("name") or p.get("type"))
                out["tools"][name] = out["tools"].get(name, 0) + 1
                blob = str(p.get("input") or p.get("arguments") or "")
                writes = (name == "apply_patch" or WRITE_CMD.search(blob) is not None) and ".queenbee" not in blob
                if phase == "explore":
                    if writes:
                        phase = "build"
                        out["first_write_at"] = ts
                    else:
                        out["reads_before_write"] += 1
            elif p.get("type") == "token_count":
                last = (p.get("info") or {}).get("last_token_usage") or {}
                tokens = int(last.get("total_tokens") or ((last.get("input_tokens") or 0) + (last.get("output_tokens") or 0)))
                if tokens:
                    out["tokens"][phase] += tokens
                    out["turns"][phase] += 1
                    out["context_end"] = int(last.get("input_tokens") or 0)
    out["cwd"] = cwd
    return out


def worker_profiles(project: str, since: float = 0.0) -> list[dict[str, Any]]:
    """Every agent transcript written for this project folder since `since`, profiled; the newest first."""
    home = os.path.expanduser("~")
    found: list[dict[str, Any]] = []
    for f in glob.glob(os.path.join(home, ".claude", "projects", _slug(project), "*.jsonl")):
        if os.path.getmtime(f) >= since - 5:
            prof = claude_profile(f)
            if prof["turns"]["explore"] + prof["turns"]["build"]:
                prof["path"] = f
                found.append(prof)
    want = os.path.normcase(os.path.abspath(project))
    for f in glob.glob(os.path.join(home, ".codex", "sessions", "**", "*.jsonl"), recursive=True):
        if os.path.getmtime(f) < since - 5:
            continue
        prof = codex_profile(f)
        if prof.get("cwd") and os.path.normcase(os.path.abspath(prof["cwd"])) == want and (prof["turns"]["explore"] + prof["turns"]["build"]):
            prof["path"] = f
            found.append(prof)
    found.sort(key=lambda p: os.path.getmtime(p["path"]), reverse=True)
    return found


def team_table(project: str, since: float = 0.0, queen: dict[str, Any] | None = None) -> dict[str, Any]:
    """The team's spend: the queen's own roles (planner, architect, look-ins, beekeeper, completion, as given) and every worker
    by phase. Totals and shares, so the biggest bucket is plain to see."""
    workers = worker_profiles(project, since)
    rows = []
    for p in workers:
        explore, build = p["tokens"]["explore"], p["tokens"]["build"]
        rows.append({"job": p["job"], "agent": p["agent"], "model": p["model"], "explore": explore, "build": build,
                     "total": explore + build, "reads_before_write": p["reads_before_write"], "context_end": p["context_end"],
                     "turns": p["turns"]["explore"] + p["turns"]["build"]})
    rows.sort(key=lambda r: (r["job"] is None, r["job"] or 0))
    queen = {k: int(v or 0) for k, v in (queen or {}).items()}
    total = sum(r["total"] for r in rows) + sum(queen.values())
    explore = sum(r["explore"] for r in rows)
    return {"project": project, "workers": rows, "queen": queen, "total": total,
            "explore_share": round(explore / total, 3) if total else 0.0,
            "queen_share": round(sum(queen.values()) / total, 3) if total else 0.0}


def render(table: dict[str, Any]) -> str:
    """The table as Markdown, for the report and the terminal."""
    lines = ["| Who | Agent / model | Exploring | Building | Total | Reads before first write | Context at end |",
             "|---|---|---|---|---|---|---|"]
    for r in table["workers"]:
        who = f"W{r['job']}" if r["job"] else "(unknown job)"
        lines.append(f"| {who} | {r['agent']} / {r['model'] or '?'} | {r['explore'] / 1e6:.2f} M | {r['build'] / 1e6:.2f} M | "
                     f"{r['total'] / 1e6:.2f} M | {r['reads_before_write']} | {r['context_end'] / 1e3:.0f} k |")
    for role, tokens in table["queen"].items():
        if tokens:
            lines.append(f"| queen: {role} | | | | {tokens / 1e6:.2f} M | | |")
    lines.append(f"| **total** | | | | **{table['total'] / 1e6:.2f} M** | exploring {table['explore_share'] * 100:.0f}%, "
                 f"queen {table['queen_share'] * 100:.0f}% | |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m queenbee.mother.economy <project folder> [--since <epoch seconds>] [--json]")
        return 2
    project = args[0]
    since = 0.0
    if "--since" in args:
        since = float(args[args.index("--since") + 1])
    table = team_table(project, since)
    if "--json" in args:
        print(json.dumps(table, indent=1))
    else:
        print(render(table))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
