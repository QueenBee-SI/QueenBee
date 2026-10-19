"""The three arms on one job, measured through QueenBee's local API: python bench.py <solo|team|peers> <N> [max minutes]

solo   one Claude agent at default permissions, started as a free worker with the plain brief (the job's words and the
       report file): every permission question is counted and answered by a stand-in person after DELAY seconds.
peers  three free agents in one folder, the same three parts given by hand, no queen, no board, no look-ins, no delivery
       check; the stand-in person answers every prompt. Bare parallelism.
team   a Claude queen in planner mode with at most three workers; Claude workers on CLAUDE_MODEL, Codex workers on
       CODEX_MODEL; the same words as the lone agent. Nobody touches it after the start.
All arms: a fresh copy of the job (~/QueenBeeDemo/video-N, video3-N or big-N), the same checker, the same clock (start to the
end of the last worker), tokens from every transcript plus the queen's own. Result: runs/video-<lane>-<N>.json; the timeline
runs/video-log-<lane>-<N>.jsonl. A QueenBee server must be listening at QB_BASE (README.md).
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import _api

BASE = os.environ.get("QB_BASE", "http://127.0.0.1:7990")
# The brief ablation: BRIEF=none (the planner writes no CONTEXT.md), short (the planner's own, the default), full (a 60-line
# brief written by hand from the project's docs, briefs/minidb2-full.md, put in place before the plan).
BRIEF = os.environ.get("BRIEF", "short")
FULL_BRIEF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "briefs", "minidb2-full.md")
# BRIEF=findings: the planner's short brief plus the findings an earlier team left (briefs/minidb2-findings.md, from the
# full-brief run's own FINDINGS.md), put in place as .queenbee/FINDINGS.md before the plan.
FINDINGS_FILE = os.environ.get("FINDINGS_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "briefs", "minidb2-findings.md"))
READ_SCOPE = os.environ.get("READ_SCOPE", "")          # "own": workers read only their own files, the task and the brief
INTERVAL = os.environ.get("INTERVAL", "")              # the queen's look-in interval in seconds (default 120)
WAKE = os.environ.get("WAKE", "")                      # "off": the queen looks in on the timer only, never on an event
# TASK=3: MiniDB 3 (minidb3: the finished MiniDB 2 of video-31 plus nine unseen features, 219 checks). The folder
# is copied here, not by the server; with BRIEF=findings the .queenbee/FINDINGS.md the earlier teams built stays, otherwise
# .queenbee is removed. The planner writes the brief itself either way: nothing about this task is written by hand.
TASK = os.environ.get("TASK", "2")
CHECKER = "check_minidb3.py" if TASK in ("3", "23") else "check_minidb.py"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.6-sol")
DELAY = float(os.environ.get("DELAY", "0.5"))   # seconds the stand-in person takes to answer each prompt (solo, peers)
HERE = os.path.dirname(os.path.abspath(__file__))

lane = sys.argv[1]
n = sys.argv[2] if len(sys.argv) > 2 else "1"
maxmin = float(sys.argv[3]) if len(sys.argv) > 3 else 40


def call(p, b=None, timeout=120):
    r = urllib.request.Request(BASE + p, json.dumps(b).encode() if b is not None else None, {"Content-Type": "application/json", **_api.auth_headers(BASE)})
    return json.load(urllib.request.urlopen(r, timeout=timeout))


def score(folder):
    r = subprocess.run([sys.executable, CHECKER], cwd=folder, capture_output=True, text=True, timeout=600)
    lines = [l for l in (r.stdout or "").splitlines() if l.strip()]
    return lines[-1] if lines else (r.stderr or "").strip()[-200:]


for q in call("/api/state")["queens"]:
    call("/api/queen", {"action": "remove", "queen": q["id"], "force": True})
time.sleep(3)
if TASK == "23":
    folder = subprocess.run([sys.executable, os.path.join(HERE, "prepare_long_job.py")], capture_output=True, text=True).stdout.strip()
    command = ("Build the changes in TASK.md, and then those in TASK3.md, which come on top of them.\n\n"
               "Follow QUEEN.md, README.md and the existing architecture.\n"
               "Approve edits and tests inside this project.\n"
               "Done means: python check_minidb3.py passes all checks.")
elif TASK == "3":
    base = os.path.join(os.path.expanduser("~"), "QueenBeeDemo")
    os.makedirs(base, exist_ok=True)
    k = 1
    while os.path.exists(os.path.join(base, f"video3-{k}")):
        k += 1
    folder = os.path.join(base, f"video3-{k}")
    shutil.copytree(os.path.join(HERE, "minidb3"), folder, ignore=shutil.ignore_patterns("__pycache__"))
    if BRIEF != "findings":
        shutil.rmtree(os.path.join(folder, ".queenbee"), ignore_errors=True)
    command = ("Build the changes in TASK.md.\n\nFollow QUEEN.md, README.md and the existing architecture.\n"
               "Approve edits and tests inside this project.\n"
               "Done means: python check_minidb3.py passes all checks.")
else:
    base = os.path.join(os.path.expanduser("~"), "QueenBeeDemo")
    os.makedirs(base, exist_ok=True)
    k = 1
    while os.path.exists(os.path.join(base, f"video-{k}")):
        k += 1
    folder = os.path.join(base, f"video-{k}")
    shutil.copytree(os.path.join(HERE, "minidb2"), folder, ignore=shutil.ignore_patterns("__pycache__"))
    command = ("Build the changes in TASK.md.\n\nFollow QUEEN.md, README.md and the existing architecture.\n"
               "Approve edits and tests inside this project.\n"
               "Done means: python check_minidb.py passes all checks.")
print(lane, n, "folder", folder, "start state:", score(folder), flush=True)

PARTS = [("Parser, lexer, expressions, windows", "Your part: the lexer, the parser, expressions, functions and window functions (TASK.md sections for CASE and CAST, "
          "correlated subqueries, CTEs, set operations, window functions). Edit only the lexer/parser/expression modules and their tests."),
         ("Storage, DML, DDL, views, foreign keys", "Your part: storage, INSERT ... SELECT and upserts, ALTER TABLE, views and foreign keys. Edit only the "
          "storage/DML/DDL modules and their tests."),
         ("Executor select side", "Your part: the executor's select side (joins, grouping, subqueries, set operations, window functions at run "
          "time) and the database/CLI glue. Edit only the executor and database modules and their tests.")]
if TASK == "23":
    # the same three parts carry TASK3.md's work on the same files (its section 30)
    PARTS = [(PARTS[0][0], PARTS[0][1] + " In TASK3.md: part 1 of section 30 (lexer, parser, ast.py, expr, functions, windows)."),
             (PARTS[1][0], PARTS[1][1] + " In TASK3.md: part 2 of section 30 (storage, dml, database)."),
             (PARTS[2][0], PARTS[2][1] + " In TASK3.md: part 3 of section 30 (executor, plan, docs/ARCHITECTURE.md).")]
t0 = time.time()
if lane == "peers":
    # No queen: the same three parts, given by hand, three agents in one folder, no board, no look-ins, no delivery check.
    # Every permission question is counted and answered after DELAY seconds (half a second unless set). Carlini-style parallelism without an orchestrator.
    queens_ = []
    for title, part in PARTS:
        r = call("/api/queen", {"action": "free", "name": title[:40], "agent": "claude", "model": CLAUDE_MODEL, "folder": folder,
                                "goal": command + "\n\n" + part + " Two other agents work on the other parts in this folder at the same time; "
                                "leave their files to them.", "delay": DELAY})
        queens_.append(r["queen"])
    queen = queens_[0]
    print("peers:", queens_, flush=True)
elif lane == "solo":
    r = call("/api/queen", {"action": "free", "name": "One agent", "agent": "claude", "model": CLAUDE_MODEL, "folder": folder,
                            "goal": command, "delay": DELAY})
    queen = r["queen"]
else:
    # agent "" lets the planner use every agent in mother.toml (`agents = ["claude", "codex"]`); "claude" pins her team to Claude
    if BRIEF == "full":
        os.makedirs(os.path.join(folder, ".queenbee"), exist_ok=True)
        shutil.copy(FULL_BRIEF, os.path.join(folder, ".queenbee", "CONTEXT.md"))     # the planner never overwrites one that exists
    if BRIEF == "findings" and TASK != "3":
        os.makedirs(os.path.join(folder, ".queenbee"), exist_ok=True)
        shutil.copy(FINDINGS_FILE, os.path.join(folder, ".queenbee", "FINDINGS.md"))
    added = call("/api/queen", {"action": "add", "name": "QueenBee", "agent": os.environ.get("TEAM_AGENT", ""), "model": CLAUDE_MODEL,
                                "limits": {"workers": 3, "budget_tokens": 12_000_000},
                                "roles": {"worker_model": CLAUDE_MODEL, "codex_worker_model": CODEX_MODEL,
                                          **({"write_context": "no"} if BRIEF == "none" else {}),
                                          **({"read_scope": READ_SCOPE} if READ_SCOPE else {}),
                                          **({"director_interval_s": INTERVAL} if INTERVAL else {}),
                                          **({"director_wake": WAKE} if WAKE else {})},
                                "description": ("MiniDB 3: nine more SQL features, 219 checks." if TASK in ("3", "23")
                                                else "MiniDB 2: nine new SQL features, 154 checks."), "director": True})
    queen = added["queen"]
    print("director:", call("/api/queen", {"action": "director", "queen": queen, "model": CLAUDE_MODEL}),
          "models:", call("/api/queen", {"action": "workers", "queen": queen, "claude": CLAUDE_MODEL, "codex": CODEX_MODEL}), flush=True)
    t0 = time.time()
    print("command:", call("/api/command", {"queen": queen, "instruction": command, "folder": folder, "strategy": "multi"}), flush=True)
print(lane, "started", flush=True)

os.makedirs(os.path.join(HERE, "runs"), exist_ok=True)
log = open(os.path.join(HERE, "runs", f"video-log-{lane}-{n}.jsonl"), "w", encoding="utf-8")
done_at = None
while time.time() - t0 < maxmin * 60:
    time.sleep(5)
    try:
        st = call(f"/api/state?queen={queen}")
    except Exception:
        continue
    q = next((x for x in st["queens"] if x["id"] == queen), None)
    m = st.get("mother") or {}
    jobs = [j for j in (m.get("dispatch") or []) if j.get("kind") == "task"]
    ended = bool(jobs) and all(j["status"] in ("done", "failed", "stopped", "stopped early") for j in jobs)
    if lane == "team":
        ended = ended and bool(q and q.get("finished"))
    if lane == "peers":
        peers = [x for x in st["queens"] if x["id"] in queens_]
        ended = len(peers) == 3 and all(x.get("finished") for x in peers)
        q = {"usage": {"tokens": max([(x.get("usage") or {}).get("tokens") or 0 for x in peers] or [0])},   # one folder, counted once
             "waits": sum(x.get("waits") or 0 for x in peers), "finished": ended}
        jobs = [{"priority": i + 1, "title": x.get("name"), "status": "done" if x.get("finished") else "agent working", "agent": "claude",
                 "tier": CLAUDE_MODEL} for i, x in enumerate(peers)]
    row = {"t": round(time.time() - t0, 1), "tokens": (q or {}).get("usage", {}).get("tokens"), "waits": (q or {}).get("waits"),
           "jobs": [[j["priority"], j["title"][:30], j["status"], j.get("agent"), j.get("tier")] for j in jobs],
           "verify": (m.get("verify") or {}).get("state")}
    log.write(json.dumps(row) + "\n")
    log.flush()
    if ended:
        done_at = done_at or time.time()
        if time.time() - done_at > 15:
            break
elapsed = (done_at or time.time()) - t0
time.sleep(30)
st = call(f"/api/state?queen={queen}")
q = next(x for x in st["queens"] if x["id"] == queen)
m = st["mother"]
if lane == "peers":
    peers = [x for x in st["queens"] if x["id"] in queens_]
    q = {"usage": {"tokens": max([(x.get("usage") or {}).get("tokens") or 0 for x in peers] or [0])}, "waits": sum(x.get("waits") or 0 for x in peers)}
    m = {"dispatch": [{"priority": i + 1, "title": x.get("name"), "status": "done" if x.get("finished") else "unfinished", "agent": "claude", "tier": CLAUDE_MODEL} for i, x in enumerate(peers)]}
home = os.path.expanduser("~")
shown = "~" + folder[len(home):].replace(chr(92), "/") if folder.startswith(home) else folder     # no user name in the result
res = {"lane": lane, "n": n, "task": TASK, "brief": BRIEF, "read_scope": READ_SCOPE, "interval": INTERVAL, "wake": WAKE, "director": m.get("director"), "delay": DELAY if lane in ("solo", "peers") else None, "claude": CLAUDE_MODEL, "codex": CODEX_MODEL, "folder": shown, "seconds": round(elapsed),
       "final": score(folder), "tokens": q["usage"].get("tokens"), "questions": q.get("waits"),
       "jobs": [(j["priority"], j["title"], j["status"], j.get("agent"), j.get("tier")) for j in m.get("dispatch") or []],
       "verify": (m.get("verify") or {}).get("state"), "verify_rounds": (m.get("verify") or {}).get("rounds"), "costs": m.get("costs")}
json.dump(res, open(os.path.join(HERE, "runs", f"video-{lane}-{n}.json"), "w", encoding="utf-8"), indent=1)
print(json.dumps(res)[:1500], flush=True)
