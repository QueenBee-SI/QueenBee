"""Mother answering a worker's question, so the GPU does not wait on a human.

Most answers need no model: a command inside the worker's project is approved,
a destructive or outside command is left to the user. File edits never reach
here inside the project (workers run with acceptEdits), so an edit prompt means
a path outside it -- the user decides. Only a real choice ("which setting?")
is put to a small model, with the work order as context; if it is not sure,
the user decides.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Any, Optional

from ..agents.catalog import binary_for

DANGEROUS = re.compile(
    r"(?i)(\brm\b|\bdel\b|\brmdir\b|\brd\b|Remove-Item|\bformat\b|shutdown|Stop-Computer|"
    r"\bcurl\b|\bwget\b|Invoke-WebRequest|Invoke-RestMethod|\biwr\b|\birm\b|git\s+push|"
    r"pip\s+install|npm\s+(i|install)|Set-ExecutionPolicy|\breg\s|takeown|icacls|"
    r"Start-Process|\bRestart-|\bkill\b|Stop-Process|taskkill)")
# A quoted path may hold spaces; an unquoted one ends at the first space, so a
# path like C:\Users\Jane Doe\x unquoted is cut short -- and then judged
# outside the project, which is the safe side (the user decides).
DRIVE_PATH = re.compile(r"(?<=[\"'])[A-Za-z]:[\\/][^\"']*(?=[\"'])|[A-Za-z]:[\\/][^\s\"'|&;<>]*")
# What Claude Code / Codex print when an account cannot run more requests.
LIMIT = re.compile(r"(?i)(hit your .{0,40}limit|usage limit reached|spend(ing)? limit|"
                   r"rate limit exceeded|quota exceeded|limit (resets|will reset))")
PERMISSION = re.compile(r"(?i)do you want to (proceed|run)|would you like to run|allow (this )?command")
# A worker that waits for another one (a loop, a sleep or a watcher on the board or its inbox) reads nothing while it waits:
# the queen's messages pile up unread and its part stalls (seen: nine minutes on a line the queen had already passed on).
# Such a call is refused with what to do instead; it is advice, not a refusal the user has to settle.
NO_WAITING = ("do not wait for the board or another worker: read .queenbee/inbox/ once, build on what your task and the "
              "queen say, post on the board what you assumed, and finish; the queen tells you if it changes")
_WAITS = re.compile(r"\bsleep\b|Start-Sleep|\bwhile\b|\buntil\b|inotifywait|-Wait\b|--follow\b|"
                    r"\btail\b[^|;&\n]*\s-[A-Za-z]*[fF]\b", re.I)


def waits_on_board(command: str) -> bool:
    """Whether a command waits on the hive's files (.queenbee): a loop, a sleep, tail -f, Get-Content -Wait. Words inside
    quotes are text, not the command: a report saying "blocked until W1 lands" is not a loop."""
    unquoted = re.sub(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'', '""', command or "")
    return ".queenbee" in (command or "") and bool(_WAITS.search(unquoted))


_WINDOWS = os.name == "nt"
# Git Bash (Claude Code's shell on Windows), Cygwin and WSL name drive C as /c/..., /cygdrive/c/... and /mnt/c/...
_DRIVE_FORMS = re.compile(r"^(?:/cygdrive|/mnt)?/([A-Za-z])(?=/|$)")


def native_path(p: str) -> str:
    """On Windows, a drive written the Git Bash way (/c/x, /cygdrive/c/x, /mnt/c/x) as Windows writes it (C:/x). Elsewhere,
    and for any other path, as it is: on Linux or a Mac /c is just a folder."""
    s = (p or "").strip("\"' ")
    m = _DRIVE_FORMS.match(s) if _WINDOWS else None
    return m.group(1).upper() + ":" + (s[m.end():] or "/") if m else p


def _norm(p: str) -> str:
    # realpath, so a short Windows name or a link to the project is judged by where it really is.
    return os.path.normcase(os.path.realpath(os.path.abspath(native_path(p.strip("\"' ")))))


def same_folder(a: str, b: str) -> bool:
    """Whether two ways of writing a folder name the same folder: slashes, case, `~`, and on Windows
    the short 8.3 form ("JANEDO~1") against the long one ("Jane Doe"), or a link."""
    if not a or not b:
        return False
    flat = lambda x: x.replace("/", "\\").rstrip("\\").lower()
    if flat(a) == flat(b):
        return True
    try:
        # realpath turns a short name into the long one on Windows; the folder must exist to resolve.
        ra, rb = os.path.realpath(a.strip()), os.path.realpath(b.strip())
        return flat(ra) == flat(rb)
    except OSError:
        return False


def _allowed_roots(project: str) -> list[str]:
    """Folders a worker's *command* may name without the question going to the user: the project, and the places a test run
    touches on its way (the temp folder, the Python it runs with, QueenBee's own code). Not where it may *write*: see _edit_roots."""
    roots = [project, tempfile.gettempdir(), os.path.dirname(sys.executable),
             os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
    home = os.path.expanduser("~")
    roots.append(os.path.join(home, "AppData", "Local", "Microsoft", "WindowsApps"))
    return [_norm(r) for r in roots]


def _edit_roots(project: str) -> list[str]:
    """Where a worker may change files without asking: the project folder, and the agent's own scratch folder (Claude Code
    keeps one per session under the temp folder's `claude/`; a worker told not to write there puts its helper scripts
    into the project instead, which is worse). Not the temp folder itself, not the Python folder."""
    return [_norm(project), _norm(os.path.join(tempfile.gettempdir(), "claude"))]


HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n\2\b", re.S)


def without_heredocs(command: str) -> str:
    """The command with its heredoc bodies taken out: what a script *says* ('del x' in Python) is not a shell command.
    The bodies are still read for where they write (write_targets runs on the whole command)."""
    return HEREDOC.sub(lambda m: f"<<{m.group(2)}\n{m.group(2)}", command)


# Where a shell command writes: redirections, tee, PowerShell's file cmdlets, copies and moves, an inline Python open() for
# writing. The target is the quoted or bare word after the operator or verb.
_TARGET = r"""("[^"]+"|'[^']+'|[^\s;|&<>]+)"""
WRITE_TARGETS = [
    re.compile(r"(?<![<>0-9&])>{1,2}\s*" + _TARGET),                                      # > file, >> file
    re.compile(r"\btee\b(?:\s+-\w+)*\s+" + _TARGET, re.I),
    # PowerShell's file cmdlets take the path first (or after -Path/-LiteralPath/-FilePath); a switch with its value may come
    # before it. Copy-Item/Move-Item: the destination is the second path (or -Destination).
    re.compile(r"\b(?:Set-Content|Out-File|Add-Content|New-Item|Export-Csv)\b"
               r"(?:\s+-(?!Path\b|LiteralPath\b|FilePath\b)\w+(?:\s+(?!-)[^\s;|&]+)?)*"
               r"\s+(?:-(?:Path|LiteralPath|FilePath)\s+)?" + _TARGET, re.I),
    re.compile(r"\b(?:Copy-Item|Move-Item)\b\s+(?:-(?:Path|LiteralPath)\s+)?" + _TARGET
               + r"\s+(?:-Destination\s+)?" + _TARGET, re.I),
    # a command word, not the end of a file name: `TASK.md docs/*.md` is no `md docs/*.md`, `notes.cp x` no copy
    re.compile(r"(?<![\w.\-/\\])(?:cp|mv|copy|move|install)\b(?:\s+-\S+)*\s+(?:" + _TARGET + r"\s+)+" + _TARGET, re.I),
    re.compile(r"\bopen\(\s*" + _TARGET + r"\s*,\s*['\"][wax]", re.I),               # python: open('x', 'w')
    re.compile(r"(?<![\w.\-/\\])(?:mkdir|md|touch|rmdir|New-Item\s+-ItemType\s+Directory)\b(?:\s+-\S+)*\s+" + _TARGET, re.I),
]
_OPEN = WRITE_TARGETS[-2]
# A leading `cd X && ...` or `cd X; ...`: not a write, but the folder the relative write targets after it resolve against.
LEADING_CD = re.compile(r"^\s*cd\s+" + _TARGET + r"\s*(?:2>\S+\s*)?(?:&&|;|\n)", re.I)


def working_dir(command: str, project: str) -> str:
    """The folder a command's relative paths are relative to: the project, or the folder a leading `cd` goes to."""
    m = LEADING_CD.match(command or "")
    if not m:
        return project
    t = native_path(m.group(1).strip("\"' "))
    if re.match(r"^[A-Za-z]:[\\/]", t) or t.startswith(("/", "\\")):
        return t
    if t.startswith("~"):
        return os.path.expanduser(t)
    return os.path.join(project, t)
_NOWHERE = {"&1", "&2", "/dev/null", "nul", "null", "-", "."}


def write_targets(command: str) -> list[str]:
    """The paths a shell command writes to (or moves into), as written."""
    found: list[str] = []
    for rx in WRITE_TARGETS:
        for m in rx.finditer(command):
            # the last group that matched is the destination (cp a b c -> c; Set-Content x -> x)
            groups = [g for g in m.groups() if g]
            if not groups:
                continue
            t = groups[-1].strip("\"' ")
            if rx is _OPEN and groups[-1] == t and re.fullmatch(r"[A-Za-z_]\w*", t):
                # open(p, 'w'): p is a variable. Its value, when the command sets it from a literal (p='minidb/x.py');
                # otherwise it cannot be judged from here and is no path.
                v = re.findall(r"(?<![\w.])" + t + r"\s*=\s*(\"[^\"\n]+\"|'[^'\n]+')", command)
                if not v:
                    continue
                t = v[-1].strip("\"'")
            # a target with no letter in it ("1:", "&2", "2") is a stream or a number, not a file
            if t and t.lower() not in _NOWHERE and not t.startswith(("-", "$")) and (re.search(r"[A-Za-z]", t) or ".." in t or "/" in t or chr(92) in t):
                found.append(t)
    return found


def outside_for_writing(target: str, project: str, roots: list[str]) -> str:
    """Why a write target lies outside the folders a worker may write to; '' when it is inside. Relative paths are
    resolved against the project (the worker's folder); `..` and `~` count as outside, absolute paths are judged as they are."""
    t = native_path(target.strip("\"' "))
    if not t:
        return ""
    parts = t.replace("/", "\\").split("\\")
    if ".." in parts:
        return f"climbs out of the project with ..: {t[:80]}"
    if t.startswith("~"):
        return f"names the home folder: {t[:80]}"
    if re.match(r"^[A-Za-z]:[\\/]", t) or t.startswith(("/", "\\")):
        full = _norm(t)                      # absolute: on POSIX /tmp/p is where the project may well be; judged as is
    else:
        full = _norm(os.path.join(project, t))
    if any(full == r or full.startswith(r + os.sep) for r in roots):
        return ""
    return f"writes outside the project: {t[:80]}"


def classify(question: dict[str, Any], project: str, prepared: str = "") -> tuple[str, str]:
    """('approve'|'human'|'choose', why) for a question seen on a worker's screen.

    `prepared` is the submit line from the work order the user started: the
    folders it names (a dataset outside the project, say) are in scope too."""
    text = question.get("text") or ""
    context = "\n".join(question.get("context") or [])
    # An agent's first start in a folder asks whether to trust it. Trusting
    # the worker's own project is inside its scope; any other folder is not.
    options_text = " ".join(o.get("text", "") for o in question.get("options") or [])
    if re.search(r"(?i)trust (the contents of )?this (directory|folder)|one you trust",
                 text + " " + context + " " + options_text):
        # Paths here contain spaces ("C:\Users\Jane Doe\...") and may be
        # shown with ~ for the home folder, so compare whole strings rather
        # than cutting paths out at the first space.
        seen = (text + " " + context).replace("/", "\\").lower()
        seen = seen.replace("~\\", os.path.expanduser("~").lower() + "\\")
        own = os.path.abspath(project).replace("/", "\\").lower().rstrip("\\")
        m = re.search(r"you are in (.+?)(?: do you trust|$)", seen)
        named = (m.group(1).strip().rstrip("\\") if m else "")
        # Claude shows the folder on its own line under "Accessing workspace:".
        lines = [l.strip() for l in question.get("context") or []]
        wrapped = []                  # the folder as printed, and as it reads if the terminal wrapped it
        for i, l in enumerate(lines[:-1]):
            if l.lower().startswith("accessing workspace"):
                named = lines[i + 1].replace("/", "\\").lower()
                named = named.replace("~\\", os.path.expanduser("~").lower() + "\\").rstrip("\\")
                # A long path is wrapped at the terminal's width onto the lines below it (a wrap may
                # fall on a space of the path or inside a word): try the path joined over 1-3 lines.
                for count in (1, 2, 3):
                    chunk = lines[i + 1:i + 1 + count]
                    for joiner in ("", " "):
                        wrapped.append(joiner.join(chunk).replace("/", "\\").rstrip("\\"))
        # Codex prints "Folder access" and then the folder, on the same line or the next one.
        seen_lines = lines + [l.strip() for l in text.splitlines()]
        for i, l in enumerate(seen_lines):
            if l.lower().startswith("folder access"):
                first = l[len("folder access"):].strip()
                tail = ([first] if first else []) + seen_lines[i + 1:i + 3]
                for count in (1, 2, 3):
                    for joiner in ("", " "):
                        wrapped.append(joiner.join(tail[:count]).replace("/", "\\").rstrip("\\"))
        if named == own or same_folder(named, own) or same_folder(named, project) or any(
                same_folder(w, own) or same_folder(w, project) for w in wrapped):
            return "approve", "trust the worker's own project folder"
        return "human", "asks to trust a folder other than the project"
    # Codex asks "Would you like to make the following edits?" with the files it will change listed as "Destination:". Edits to
    # files inside the project are within the queen's scope, whatever it says about its sandbox: that is how Codex asks again
    # after a sandboxed write failed. One file outside the project, or none named, and it is the user's.
    if re.search(r"(?i)make the following edits", text + "\n" + context):
        # (the paths hold spaces: each one is judged by what follows "Destination:", not cut at the first space)
        dests = [part.strip() for line in (text + "\n" + context).splitlines()
                 for part in re.split(r"(?i)destination\s*:", line)[1:] if part.strip()]
        roots = [r.lower().replace("/", "\\").rstrip("\\") for r in _edit_roots(project)]
        if dests and all(any(d.lower().replace("/", "\\").startswith(r + "\\") for r in roots) and ".." not in d for d in dests):
            return "approve", "Codex edits files inside the project folder"
        return "human", "Codex wants to edit a file outside the project folder, or does not name one"
    if re.search(r"(?i)make this edit|create .+\?|overwrite|write to", text):
        # A file edit is fine when every absolute path it shows lies inside the project (or the other
        # folders a worker may use); a path outside, or one climbing out with "..", is the user's.
        shown = [l for l in (text + "\n" + context).splitlines() if not re.match(r"\s*\d+\s*[+-]?\s", l)]
        blob = "\n".join(shown).replace("/", "\\")
        low = blob.lower()
        roots = [r.lower().replace("/", "\\").rstrip("\\") for r in _edit_roots(project)]
        climbs = re.search(r"(^|[\\\s\"'])\.\.([\\\s\"']|$)", blob)
        outside = [m.group(0) for m in DRIVE_PATH.finditer(blob)
                   if not any(low[m.start():].startswith(r + "\\") or low[m.start():m.start() + len(r) + 1] == r
                              for r in roots)]
        if not outside and not climbs and (context.strip() or DRIVE_PATH.search(blob)):   # a path is shown
            return "approve", "an edit inside the project folder"
        return "human", "edit outside the project folder"
    # Codex puts a "Reason: ...?" line between its question and the choices,
    # so the line picked as "the question" may be that reason; the permission
    # sentence itself is then among the lines above it.
    if PERMISSION.search(text + "\n" + context):
        # Judge the command being asked about: the lines after the permission
        # sentence (Codex prints it there). Earlier output above it -- a
        # previous command's "…"-truncated echo -- is not what is approved.
        lines = question.get("context") or []
        at = max((i for i, l in enumerate(lines) if PERMISSION.search(l)), default=None)
        command = "\n".join(l for l in (lines[at + 1:] if at is not None else lines)
                            if not l.lower().startswith(("environment:", "reason:")))
        if DANGEROUS.search(command):
            return "human", "command could delete, download, install or stop things"
        roots = _allowed_roots(project) + [_norm(p) for p in DRIVE_PATH.findall(prepared)]
        # A long command is wrapped over several screen lines, often inside a
        # quoted path ("C:\Users\Jane" / "Doe\..."). Rejoin wraps inside
        # quotes with the space they replaced; anything still unclear is
        # judged outside, which leaves it to the user.
        joined = re.sub(r"\"[^\"]*\"|'[^']*'", lambda m: m.group(0).replace("\n", " "), command)
        # A shell-escaped space ("C:\Users\Jane\ Doe") is the same path.
        joined = joined.replace("\\ ", " ")
        # An allowed folder whose name holds a space ("C:\Users\Jane
        # Doe\...") written unquoted would be cut at the space and judged
        # outside -- every `cd <project>; ...` went to the user. Read such a
        # folder whole, with what follows it, and judge the resolved path, so
        # "<project>\..\..\x" is still caught.
        for root in sorted({r for r in roots if " " in r}, key=len, reverse=True):
            pattern = re.compile(re.escape(root).replace(r"\\", r"[\\/]") + r"[^\s\"'|&;<>]*", re.I)
            for m in list(pattern.finditer(joined)):
                p = _norm(m.group(0))
                if not any(p == r or p.startswith(r + os.sep) for r in roots):
                    return "human", f"command reaches outside the project: {m.group(0)[:80]}"
            joined = pattern.sub(" ", joined)
        # Where the command *writes* is judged against the project alone (not the temp or Python folders a test may
        # name): a redirection to ../x, Set-Content ..\x, > /tmp/x, cd .., an open('x', 'w') elsewhere.
        write_roots = _edit_roots(project) + [_norm(x) for x in DRIVE_PATH.findall(prepared)]
        base = working_dir(joined, project)
        for target in write_targets(joined):
            why = outside_for_writing(target, base, write_roots)
            if why:
                return "human", f"command {why}"
        for path in DRIVE_PATH.findall(joined):
            p = _norm(path)
            if not any(p == r or p.startswith(r + os.sep) for r in roots):
                return "human", f"command reaches outside the project: {path[:80]}"
        return "approve", "command inside the project scope"
    if question.get("options"):
        return "choose", "a choice about the work"
    return "human", "an open question"


def classify_tool(tool_name: str, tool_input: dict[str, Any], project: str, prepared: str = "") -> tuple[str, str]:
    """('allow'|'deny'|'ask', why) for a tool call a Claude Code hook reports before it runs: the same rules as a question
    seen on the screen, read from the call itself instead of a terminal. 'ask' lets the usual prompt appear."""
    name = (tool_name or "").strip()
    inp = tool_input or {}
    if name in ("Read", "Glob", "Grep", "LS"):
        return "allow", "reads inside the worker's scope"
    if name in ("WebFetch", "WebSearch"):
        return "deny", "the queen does not let a worker reach the network"
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = native_path(str(inp.get("file_path") or inp.get("notebook_path") or ""))
        if not path:
            return "ask", "an edit with no file named"
        roots = _edit_roots(project) + [_norm(x) for x in DRIVE_PATH.findall(prepared)]
        # A relative path is relative to the worker's folder (Claude Code runs there); it is judged resolved, not as written.
        norm = _norm(path if os.path.isabs(path) or re.match(r"^[A-Za-z]:", path) else os.path.join(project, path))
        # Outside the project the queen says no (the rule in QUEEN.md: anything that leaves this folder), so that under
        # the agent's auto mode, where "ask" would simply run, the write does not happen.
        if ".." in path.replace("/", "\\").split("\\"):
            return "deny", "the queen says no: the edit climbs out of the project folder"
        if any(norm == r or norm.startswith(r + os.sep) for r in roots):
            return "allow", "an edit inside the project folder"
        return "deny", "the queen says no: the edit is outside the project folder"
    if name in ("Bash", "PowerShell"):
        command = str(inp.get("command") or "")
        if not command.strip():
            return "ask", "an empty command"
        # What a worker *says* is not what it *runs*: the text of an echo (a progress line such as "next: pip install x")
        # is judged as words, not as a command. The commands around it are judged whole.
        judged = re.sub(r"""echo\s+("[^"]*"|'[^']*')""", 'echo ""', without_heredocs(command))
        if waits_on_board(judged):
            return "deny", NO_WAITING
        verdict, why = classify({"text": "Do you want to proceed?", "context": ["Do you want to proceed?", judged],
                                 "options": [{"n": 1, "text": "Yes"}, {"n": 2, "text": "No"}]}, project, prepared)
        if verdict == "approve":
            # the heredoc bodies were not judged as commands; they are still read for where they write
            write_roots = _edit_roots(project) + [_norm(x) for x in DRIVE_PATH.findall(prepared)]
            base = working_dir(command, project)
            for target in write_targets(command):
                outside = outside_for_writing(target, base, write_roots)
                if outside:
                    return "deny", f"the queen says no: command {outside}"
            return "allow", why
        if DANGEROUS.search(judged) or why.startswith("command climbs") or why.startswith("command writes") \
                or why.startswith("command names"):
            return "deny", f"the queen says no: {why}"
        return "ask", why
    if name == "Monitor" and waits_on_board(without_heredocs(str(inp.get("command") or ""))):
        return "deny", NO_WAITING                        # Claude Code's background watcher, the same wait
    return "ask", f"a {name} call is left to the usual prompt"


def choose(question: dict[str, Any], job: dict[str, Any], model: str) -> Optional[int]:
    """Pick an option with a small model; None when it is not sure."""
    claude = binary_for("claude")
    if not claude:
        return None
    options = "\n".join(f"{o['n']}. {o['text']}" for o in question.get("options") or [])
    prompt = (
        "A worker agent asks a question while doing a job. Pick the option the job's "
        "instructions call for. Reply with only the option number, or 0 if the "
        "instructions do not settle it.\n\n"
        f"Job goal: {job.get('goal')}\n"
        f"Work order:\n{(job.get('work_order') or '')[:4000]}\n"
        f"Decisions the user made: {job.get('answers') or 'none'}\n\n"
        f"Question: {question.get('text')}\n{options}\n")
    try:
        proc = subprocess.run([claude, "-p", "--strict-mcp-config", "--model", model, "--output-format", "json",
                               "--tools", "", "--no-session-persistence"],
                              input=prompt, capture_output=True, text=True, encoding="utf-8",
                              timeout=120, cwd=tempfile.gettempdir(),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        answer = str(json.loads(proc.stdout).get("result") or "").strip()
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None
    m = re.match(r"\d+", answer)
    n = int(m.group()) if m else 0
    valid = {int(o["n"]) for o in question.get("options") or []}
    return n if n in valid else None


def approve_option(question: dict[str, Any]) -> int:
    """The option that says yes (Claude's trust menu puts "No, exit" first)."""
    for o in question.get("options") or []:
        if str(o.get("text", "")).strip().lower().startswith("yes"):
            return int(o["n"])
    return 1


def keys_for_option(n: int, cursor: int = 1) -> str:
    """Move from the highlighted option to option n, then Enter."""
    return ("\x1b[B" * (n - cursor) if n >= cursor else "\x1b[A" * (cursor - n)) + "\r"
