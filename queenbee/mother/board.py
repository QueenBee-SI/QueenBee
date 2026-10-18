"""The hive's message board: workers and the queen talk to each other.

Workers do not meet; they share a project folder. A worker that learns
something others need appends one JSON line to `.queenbee/board.jsonl`:

    {"from": "W3", "to": "all", "text": "SNR is below 6 dB under frame 400; crop there"}
    {"from": "W2", "to": ["W4", "queen"], "text": "..."}          # to: "all" | "queen" | "W<n>" | a list

The queen reads the board as it grows and routes each message: into the
inbox file of every addressed worker (`.queenbee/inbox/W<n>.md`, which
workers read between steps) and into her own stream, where she -- and the user
-- see everything. Her own messages (and the user's) go through the same
board with "from": "queen". Nothing here calls a model.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional, Iterable

MAX_TEXT = 700
TARGET = re.compile(r"^(all|queen|[PW]\d+)$", re.I)


def board_path(folder: str) -> str:
    return os.path.join(folder, ".queenbee", "board.jsonl")


def inbox_path(folder: str, priority: int) -> str:
    return os.path.join(folder, ".queenbee", "inbox", f"W{priority}.md")


def post(folder: str, sender: str, to: Any, text: str, kind: str = "") -> None:
    """Append a message (the queen's and the user's way in; workers write the line themselves)."""
    path = board_path(folder)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    msg = {"from": sender, "to": to, "text": text}
    if kind:
        msg["kind"] = kind
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(msg, ensure_ascii=False) + "\n")


def findings_path(folder: str) -> str:
    return os.path.join(folder, ".queenbee", "FINDINGS.md")


def records_path(folder: str) -> str:
    return os.path.join(folder, ".queenbee", "findings.jsonl")


_SCOPE = re.compile(r"\b([A-Za-z_][\w-]*)\.(?:py|js|ts|md|toml|json|yaml|yml|sql)\b")
_SUPERSEDES = re.compile(r"(?i)\b(?:supersedes|replaces|corrects)\s+(F\d+)\b")


_FILE = re.compile(r"(?<![\w/\\.])((?:[\w.-]+[/\\])*[\w-]+\.(?:py|js|ts|md|toml|json|yaml|yml|sql))\b")


def guess_scope(text: str) -> str:
    """The part a finding is about: the first file it names, without its extension; else 'general'."""
    m = _SCOPE.search(text or "")
    return m.group(1).lower() if m else "general"


def files_named(text: str) -> list[str]:
    """The files a finding names, as written (parser.py, minidb/storage.py), each once."""
    out: list[str] = []
    for m in _FILE.finditer(text or ""):
        f = m.group(1).replace("\\", "/")
        if f not in out:
            out.append(f)
    return out


def _digest(path: str) -> str:
    """A short content hash of a file; '' when it cannot be read."""
    import hashlib
    try:
        with open(path, "rb") as fh:
            return hashlib.sha1(fh.read()).hexdigest()[:16]
    except OSError:
        return ""


def file_hashes(folder: str, names: list[str]) -> dict[str, str]:
    """name -> content hash for the files a finding names, as they are now ('' for a file not found)."""
    out: dict[str, str] = {}
    for name in names:
        path = _find_file(folder, name)
        out[name] = _digest(path) if path else ""
    return out


def _find_file(folder: str, name: str) -> str:
    """Where a file a finding names lives under the folder: the path as given, else the first file of that base name."""
    direct = os.path.join(folder, name)
    if os.path.isfile(direct):
        return direct
    base = os.path.basename(name)
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "node_modules", ".venv")]
        if base in files:
            return os.path.join(root, base)
    return ""


def refresh_findings(folder: str) -> int:
    """Mark as stale every active finding one of whose files changed after it was recorded (or after it was last validated).
    Returns how many were marked. A stale finding stays in FINDINGS.md under its own heading and out of the brief's first
    list; a later check that passes validates it again."""
    try:
        recs = load_findings(folder)
    except OSError:
        return 0
    n = 0
    for r in recs:
        if r.get("status", "active") != "active":
            continue
        names = r.get("files") or files_named(r.get("claim", ""))
        known = r.get("hashes") or {}
        if not names:
            continue
        if not known:                                  # an older record, or one carried in from a file: take its files as they are now
            r["hashes"] = file_hashes(folder, names)
            continue
        now = file_hashes(folder, names)
        for name in names:
            if name not in known:
                continue
            if now.get(name, "") != known[name]:
                r["status"], r["stale_since"] = "stale", int(time.time())
                r["stale_reason"] = (f"{name} no longer exists" if not now.get(name) else f"{name} changed after this finding")
                n += 1
                break
    _write_records(folder, recs)
    if n:
        render_findings(folder, recs)
    return n


def flag_conflicts(folder: str, check_output: str) -> int:
    """The delivery check failed: every active or stale finding whose file the failure names gets a conflict note, so the
    next reader (a worker, the queen, the user) sees the finding and the failure side by side."""
    try:
        recs = load_findings(folder)
    except OSError:
        return 0
    tail = " ".join((check_output or "").split())[-600:]
    named = {os.path.basename(f) for f in files_named(check_output or "")}
    n = 0
    for r in recs:
        if r.get("status", "active") not in ("active", "stale"):
            continue
        mine = {os.path.basename(f) for f in (r.get("files") or files_named(r.get("claim", "")))}
        if mine & named:
            r["conflict"] = tail
            n += 1
    if n:
        _write_records(folder, recs)
        render_findings(folder, recs)
    return n


def supersede_hint(text: str) -> Optional[str]:
    """The id of an earlier finding this one replaces, when the text says so ("supersedes F3")."""
    m = _SUPERSEDES.search(text or "")
    return m.group(1) if m else None


def _git_head(folder: str) -> str:
    try:
        import subprocess
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=folder, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def load_findings(folder: str) -> list[dict[str, Any]]:
    """Every finding record of this folder. A FINDINGS.md written before the records existed (or carried in by hand) is
    read in once, its lines becoming records with source "carried"."""
    path = records_path(folder)
    recs: list[dict[str, Any]] = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("claim"):
                    recs.append(rec)
        return recs
    md = findings_path(folder)
    if os.path.isfile(md):
        with open(md, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.match(r"-\s+\(([^)]*)\)\s+(.*\S)", line.strip())
                if m:
                    recs.append({"id": f"F{len(recs) + 1}", "ts": 0, "scope": guess_scope(m.group(2)), "claim": m.group(2),
                                 "source_job": m.group(1), "source": "carried", "validated_by": "", "git_commit": "",
                                 "status": "active", "supersedes": None})
        if recs:
            _write_records(folder, recs)
    return recs


def _write_records(folder: str, recs: list[dict[str, Any]]) -> None:
    path = records_path(folder)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def render_findings(folder: str, recs: Optional[list[dict[str, Any]]] = None) -> None:
    """FINDINGS.md from the active records: what a worker reads."""
    recs = load_findings(folder) if recs is None else recs
    active = [r for r in recs if r.get("status", "active") == "active"]
    gone = len(recs) - len(active)
    stale = [r for r in recs if r.get("status") == "stale"]
    gone -= len(stale)
    lines = ["# Findings", "",
             "What the workers found out, kept for everyone who works here next: environment facts, interface decisions, ways that "
             "failed and why. Written by QueenBee from the hive board and the integration-fix worker's notes; the records with their "
             "source, validation and history are in findings.jsonl.", ""]

    def line(r: dict[str, Any]) -> str:
        tag = f"{r.get('source_job') or '?'}, {r.get('scope') or 'general'}"
        check = f" [checked: {r['validated_by']}]" if r.get("validated_by") else ""
        conflict = f" [a check failed after this: {str(r['conflict'])[:160]}]" if r.get("conflict") else ""
        return f"- ({tag}) {r['claim']}{check}{conflict}"

    lines += [line(r) for r in active]
    if stale:
        lines += ["", "## May be out of date", "",
                  "The file each of these is about changed after it was written and no check has passed since. Verify before relying on one.", ""]
        lines += [line(r) + f" [{r.get('stale_reason', 'stale')}]" for r in stale]
    if gone:
        lines += ["", f"({gone} earlier finding{'s' if gone != 1 else ''} superseded or retired; see findings.jsonl)"]
    try:
        os.makedirs(os.path.join(folder, ".queenbee"), exist_ok=True)
        with open(findings_path(folder), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass


def keep_finding(folder: str, sender: str, text: str, *, scope: str = "", source: str = "board", validated_by: str = "",
                 supersedes: Optional[str] = None) -> bool:
    """Keep a finding as a record and in FINDINGS.md; once per claim; True when it was new. A finding that names an earlier
    one it supersedes retires that one."""
    claim = " ".join((text or "").split())
    if not claim:
        return False
    try:
        recs = load_findings(folder)
        if any(r.get("claim") == claim and r.get("status", "active") == "active" for r in recs):
            return False
        names = files_named(claim)
        rec = {"id": f"F{len(recs) + 1}", "ts": int(time.time()), "scope": scope or guess_scope(claim), "claim": claim,
               "files": names, "hashes": file_hashes(folder, names), "source_job": sender, "source": source,
               "validated_by": validated_by, "git_commit": _git_head(folder), "status": "active", "supersedes": supersedes}
        if supersedes:
            for r in recs:
                if r.get("id") == supersedes:
                    r["status"], r["superseded_by"] = "superseded", rec["id"]
        recs.append(rec)
        _write_records(folder, recs)
        render_findings(folder, recs)
    except OSError:
        return False
    return True


def validate_findings(folder: str, validated_by: str) -> int:
    """The whole package passed its check. The findings that are *current* (active, their files as they were when the
    finding was made or last validated) are stamped with it, and a conflict a failed check had put on one of them is
    cleared, since the same check now passes. A stale finding is not revived by this: a passing suite does not prove
    every claim inside it; a person, the queen, or a new finding that supersedes it settles a stale one. Returns how
    many findings were stamped for the first time or cleared of a conflict."""
    try:
        refresh_findings(folder)
        recs = load_findings(folder)
    except OSError:
        return 0
    n = 0
    now = int(time.time())
    for r in recs:
        if r.get("status", "active") != "active":
            continue
        if not r.get("validated_by") or r.get("conflict"):
            n += 1
        r["validated_by"], r["validated_ts"] = validated_by, now
        r.pop("conflict", None)
        if r.get("files"):
            r["hashes"] = file_hashes(folder, r["files"])          # validated as the files are now
    if n:
        _write_records(folder, recs)
        render_findings(folder, recs)
    return n


def settle_finding(folder: str, finding_id: str, action: str, by: str = "user") -> bool:
    """A person or the queen settles a finding by hand: 'validate' makes a stale one current again (its files taken as they
    are now), 'retire' ends it. True when the record was found and changed."""
    try:
        recs = load_findings(folder)
    except OSError:
        return False
    for r in recs:
        if r.get("id") != finding_id:
            continue
        if action == "validate":
            r["status"], r["validated_by"], r["validated_ts"] = "active", f"{by} confirmed it", int(time.time())
            r.pop("stale_reason", None)
            r.pop("stale_since", None)
            r.pop("conflict", None)
            if r.get("files"):
                r["hashes"] = file_hashes(folder, r["files"])
        elif action == "retire":
            r["status"], r["retired_by"], r["retired_ts"] = "retired", by, int(time.time())
        else:
            return False
        _write_records(folder, recs)
        render_findings(folder, recs)
        return True
    return False


def relevant_findings(folder: str, files: list[str], words: list[str], limit: int = 12) -> list[dict[str, Any]]:
    """The active findings for a worker's brief: the ones about its files or its part first, then the rest, newest first."""
    try:
        refresh_findings(folder)
        recs = [r for r in load_findings(folder) if r.get("status", "active") == "active"]
    except OSError:
        return []
    keys = {os.path.splitext(os.path.basename(f))[0].lower() for f in files or [] if f}
    keys |= {w.lower() for w in words or [] if len(w) > 3}

    def rank(r: dict[str, Any]) -> tuple[int, int]:
        blob = (str(r.get("scope") or "") + " " + str(r.get("claim") or "")).lower()
        hit = any(k in blob for k in keys)
        return (0 if hit else 1, -int(r.get("ts") or 0))

    return sorted(recs, key=rank)[:limit]


def read_new(folder: str, offset: int) -> tuple[list[dict[str, Any]], int]:
    """(valid messages since `offset`, the new offset). A last line still being
    written is left for the next look."""
    path = board_path(folder)
    try:
        size = os.path.getsize(path)
        if size < offset:
            offset = 0                              # rewritten from the top
        if size == offset:
            return [], offset
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(200_000)
    except OSError:
        return [], offset
    end = data.rfind(b"\n") + 1
    out = []
    for raw in data[:end].splitlines():
        try:
            m = json.loads(raw.decode("utf-8-sig", "replace").lstrip("﻿"))
        except ValueError:
            continue
        msg = clean(m)
        if msg:
            out.append(msg)
    return out, offset + end


def clean(m: Any) -> dict[str, Any] | None:
    if not isinstance(m, dict):
        return None
    # An agent may name the fields its own way ({"job": "W1", "message": ...}, seen from Codex): the line is still read, or
    # the workers it was for never get it.
    sender = str(m.get("from") or m.get("sender") or m.get("job") or m.get("worker") or "").strip()
    text = str(m.get("text") or m.get("message") or m.get("msg") or m.get("body") or "").strip()
    to = m.get("to", "all")
    to = [to] if isinstance(to, str) else to if isinstance(to, list) else ["all"]
    to = [str(t).strip() for t in to if isinstance(t, (str, int)) and TARGET.match(str(t).strip())]
    if not text or not (sender.lower() in ("queen", "user") or re.fullmatch(r"[PW]\d+", sender, re.I)):
        return None
    def worker(x: str) -> str:                         # W3 (P3 is the old spelling of the same thing)
        return "W" + x[1:] if x[:1] in ("P", "p", "W", "w") else x
    out = {"from": sender if sender.lower() in ("queen", "user") else worker(sender).upper(),
           "to": [t.lower() if t.lower() in ("all", "queen") else worker(t).upper() for t in to] or ["all"],
           "text": text[:MAX_TEXT]}
    # A finding: something later workers must know (an environment fact, an interface decision, a way that failed and why).
    # Marked by "kind": "finding", or by a text that starts with "finding:". Kept apart from the chatter.
    if str(m.get("kind") or "").strip().lower() == "finding" or re.match(r"(?i)finding\s*:", text):
        out["kind"] = "finding"
        out["text"] = re.sub(r"(?i)^finding\s*:\s*", "", out["text"])
    return out


def recipients(msg: dict[str, Any], active: Iterable[int]) -> list[int]:
    """The workers a message goes to, in order. "all" is every active worker
    but the sender; "queen" alone goes to no worker."""
    active = sorted(set(active))
    me = int(msg["from"][1:]) if re.fullmatch(r"[PW]\d+", msg["from"]) else None
    out: list[int] = []
    for t in msg["to"]:
        if t == "all":
            out += [p for p in active if p != me]
        elif t[:1] in ("P", "W"):                   # (P is the old spelling)
            p = int(t[1:])
            if p in active and p != me:
                out.append(p)
    return list(dict.fromkeys(out))


def to_queen(msg: dict[str, Any]) -> bool:
    """Whether the queen is addressed (everything is visible to her anyway)."""
    return "queen" in msg["to"] or "all" in msg["to"]


def deliver(folder: str, priority: int, msg: dict[str, Any]) -> None:
    path = inbox_path(folder, priority)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"- [{time.strftime('%H:%M:%S')}] from {msg['from']}: {msg['text']}\n")


def unread(folder: str, priority: int, offset: int) -> tuple[list[str], int]:
    """Lines of a worker's inbox past `offset` (for tests and the UI)."""
    try:
        with open(inbox_path(folder, priority), encoding="utf-8") as fh:
            fh.seek(offset)
            lines = fh.read().splitlines()
            return lines, fh.tell()
    except OSError:
        return [], offset
