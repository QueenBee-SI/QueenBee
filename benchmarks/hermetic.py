"""A hermetic benchmark runner: one fresh QueenBee per run, nothing carried over, a manifest next to every result.

    python hermetic.py --runs 3 [--port 7990] [--seed N] [--minutes 45] NAME=KEY=VAL[,KEY=VAL...] ...

Each arm is a name and the driver's switches (TASK, BRIEF, READ_SCOPE, INTERVAL, WAKE, LANE, CLAUDE_MODEL, CODEX_MODEL,
TEAM_AGENT); every switch not named gets its explicit default, so no run inherits a setting from the run before it. The
(arm, repetition) pairs run in a shuffled order (the seed is recorded). For every run: a new state folder, a new server
process (and its host), `bench.py` against it, then the server and host are stopped. What is kept, under
`runs/hermetic/<run id>/`: the driver's result and `manifest.json` (git commit and whether the tree was clean, the arm
and its effective switches, model ids as the result reports them, Python and platform, start and end times, the result's
headline numbers, and whether the run is valid); the heavy files (the driver's timeline and output, the queen's and the
host's logs) go to `runs/hermetic-logs/<run id>/`, which the repository ignores. The summary at the end gives each arm's
median time, tokens and integration rounds over its valid runs.

The exclusion rule, fixed before any run and applied by the runner, not by a person afterwards: a run is **invalid** when
a worker or the queen met the provider's usage limit (the queen's log says "hit its provider's limit" or "session limit"),
when no worker produced a token, or when the server did not answer. An invalid run keeps its manifest, with the reason,
and counts for nothing.

Example (the look-in experiment and the unseen task, twice each):
    python hermetic.py --runs 2 wake=TASK=2,BRIEF=findings timer=TASK=2,BRIEF=findings,WAKE=off \\
                                t3short=TASK=3,BRIEF=short t3find=TASK=3,BRIEF=findings
"""
from __future__ import annotations

import json
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
PY = sys.executable
DEFAULTS = {"TASK": "2", "BRIEF": "short", "READ_SCOPE": "", "INTERVAL": "", "WAKE": "", "LANE": "team",
            "CLAUDE_MODEL": "sonnet", "CODEX_MODEL": "gpt-5.6-sol", "TEAM_AGENT": "claude"}


def parse_arm(spec: str) -> tuple[str, dict[str, str]]:
    name, _, rest = spec.partition("=")
    switches = dict(DEFAULTS)
    for part in filter(None, rest.split(",")):
        k, _, v = part.partition("=")
        if k not in DEFAULTS:
            raise SystemExit(f"unknown switch {k!r} in arm {name!r}; known: {', '.join(DEFAULTS)}")
        switches[k] = v
    return name, switches


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def ping(port: int, timeout_s: float = 90) -> bool:
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=3) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def port_free(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def end_hosts(low: int, high: int) -> list[int]:
    """End every `queenbee.host` process on a port in [low, high]: the hosts of this runner's runs, never the user's own
    (its default port, 7877, lies outside). A host that outlives its run takes the next port, and the run after it
    finds its own port busy; this is what happened to five runs of eight in the first chain, set aside under
    `runs/hermetic/invalid-1015/`."""
    import re
    import psutil
    ended = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        cl = " ".join(p.info["cmdline"] or [])
        m = re.search(r"queenbee\.host.*?--port\D*(\d+)", cl)
        if m and low <= int(m.group(1)) <= high:
            try:
                p.kill()
                ended.append(p.info["pid"])
            except psutil.Error:
                pass
    return ended


def stop(proc: subprocess.Popen, port: int) -> None:
    """End the run's server and its host, and wait until the ports are free again."""
    try:
        from queenbee.agents.host_client import HostClient
        HostClient(port - 3).shutdown_host()
    except Exception:  # noqa: BLE001
        pass
    for end in (proc.terminate, proc.kill):
        try:
            end()
            proc.wait(timeout=15)
            break
        except Exception:  # noqa: BLE001
            continue
    end_hosts(port - 3, port + 40)
    deadline = time.time() + 30
    while time.time() < deadline and not (port_free(port) and port_free(port - 3) and port_free(port - 2)):
        time.sleep(1)


LIMIT_SIGNS = ("hit its provider's limit", "session limit", "usage limit", "rate limit exceeded")


def invalid_reason(manifest: dict, log_path: str) -> str:
    """Why a run counts for nothing, or '' when it counts (the rule in the module's docstring)."""
    if manifest.get("error"):
        return manifest["error"]
    r = manifest.get("result") or {}
    if not r:
        return "no result"
    if not (r.get("tokens") or 0):
        return "no worker produced a token"
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if any(sign in line for sign in LIMIT_SIGNS):
                    return "the provider's limit was met: " + line.strip()[-120:]
    except OSError:
        pass
    return ""


def one_run(run_id: str, arm: str, switches: dict[str, str], port: int, minutes: float, out_root: str) -> dict:
    out = os.path.join(out_root, run_id)
    logs = os.path.join(os.path.dirname(out_root), "hermetic-logs", run_id)     # heavy files, outside the repository's eye
    os.makedirs(out, exist_ok=True)
    os.makedirs(logs, exist_ok=True)
    state = tempfile.mkdtemp(prefix="qb-hermetic-")
    env = {**os.environ, **{k: v for k, v in switches.items() if k != "LANE"},
           "QUEENBEE_STATE_DIR": state, "QB_BASE": f"http://127.0.0.1:{port}"}
    for k in ("READ_SCOPE", "INTERVAL", "WAKE"):
        if not env.get(k):
            env.pop(k, None)                          # the driver treats an empty switch as "not given"
    started = time.time()
    manifest = {"run_id": run_id, "arm": arm, "switches": switches, "git_commit": git("rev-parse", "HEAD"),
                "git_clean": git("status", "--porcelain") == "", "python": sys.version.split()[0],
                "platform": f"{platform.system()} {platform.release()} {platform.machine()}", "port": port,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started))}
    end_hosts(port - 3, port + 40)                     # nothing of an earlier run may hold a port
    if not (port_free(port) and port_free(port - 3)):
        manifest["error"] = f"port {port} or {port - 3} is held before the start"
        json.dump(manifest, open(os.path.join(out, "manifest.json"), "w", encoding="utf-8"), indent=1, default=str)
        return manifest
    server_log = open(os.path.join(logs, "server.out"), "w", encoding="utf-8")
    proc = subprocess.Popen([PY, "-m", "queenbee.mother", "--port", str(port), "--host-port", str(port - 3), "--no-window"],
                            cwd=ROOT, env=env, stdout=server_log, stderr=subprocess.STDOUT)
    try:
        if not ping(port):
            manifest["error"] = "the server did not answer"
            json.dump(manifest, open(os.path.join(out, "manifest.json"), "w", encoding="utf-8"), indent=1, default=str)
            return manifest
        lane = switches["LANE"]
        with open(os.path.join(logs, "driver.out"), "w", encoding="utf-8") as drv:
            subprocess.run([PY, os.path.join(HERE, "bench.py"), lane, run_id, str(minutes)], cwd=HERE, env=env,
                           stdout=drv, stderr=subprocess.STDOUT, timeout=(minutes + 10) * 60)
        result_path = os.path.join(HERE, "runs", f"video-{lane}-{run_id}.json")
        if os.path.exists(result_path):
            shutil.move(result_path, os.path.join(out, "result.json"))
            manifest["result"] = json.load(open(os.path.join(out, "result.json"), encoding="utf-8"))
        timeline = os.path.join(HERE, "runs", f"video-log-{lane}-{run_id}.jsonl")
        if os.path.exists(timeline):
            shutil.move(timeline, os.path.join(logs, "timeline.jsonl"))
    except subprocess.TimeoutExpired:
        manifest["error"] = "the driver did not finish in time"
    finally:
        stop(proc, port)
        server_log.close()
        for name in ("mother.log", "host.log"):
            src = os.path.join(state, name)
            if os.path.exists(src):
                try:
                    shutil.copy(src, os.path.join(logs, name))
                except OSError:
                    pass
        shutil.rmtree(state, ignore_errors=True)
    manifest["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    manifest["invalid"] = invalid_reason(manifest, os.path.join(logs, "mother.log"))
    manifest["logs"] = os.path.relpath(logs, HERE).replace("\\", "/")
    manifest["wall_s"] = round(time.time() - started)
    r = manifest.get("result") or {}
    manifest["headline"] = {"seconds": r.get("seconds"), "final": r.get("final"), "tokens": r.get("tokens"),
                            "questions": r.get("questions"), "verify_rounds": r.get("verify_rounds"),
                            "director": r.get("director")}
    json.dump(manifest, open(os.path.join(out, "manifest.json"), "w", encoding="utf-8"), indent=1, default=str)
    return manifest


def main(argv: list[str]) -> int:
    runs, port, seed, minutes, specs = 1, 7990, int(time.time()), 45.0, []
    it = iter(argv)
    for a in it:
        if a == "--runs":
            runs = int(next(it))
        elif a == "--port":
            port = int(next(it))
        elif a == "--seed":
            seed = int(next(it))
        elif a == "--minutes":
            minutes = float(next(it))
        else:
            specs.append(a)
    if not specs:
        print(__doc__)
        return 2
    arms = [parse_arm(s) for s in specs]
    order = [(name, sw, i + 1) for name, sw in arms for i in range(runs)]
    random.Random(seed).shuffle(order)
    stamp = time.strftime("%m%d-%H%M")
    out_root = os.path.join(HERE, "runs", "hermetic")
    print(f"hermetic: {len(order)} runs, seed {seed}, order: {[f'{n}#{i}' for n, _, i in order]}", flush=True)
    done: list[dict] = []
    for name, sw, i in order:
        run_id = f"H{stamp}-{name}-{i}"
        print(f"=== {run_id} {time.strftime('%H:%M:%S')} {sw}", flush=True)
        m = one_run(run_id, name, sw, port, minutes, out_root)
        done.append(m)
        print("   ", ("INVALID: " + m["invalid"] + " ") if m.get("invalid") else "", json.dumps(m.get("headline") or m.get("error"))[:300], flush=True)
    bad = [m["run_id"] + ": " + m["invalid"] for m in done if m.get("invalid")]
    if bad:
        print("\ninvalid (excluded by the rule in the docstring):\n  " + "\n  ".join(bad))
    print("\n| arm | runs | median time | median tokens | integration rounds | finals |")
    print("|---|---|---|---|---|---|")
    for name, _ in arms:
        rs = [m["result"] for m in done if m["arm"] == name and m.get("result") and not m.get("invalid")]
        if not rs:
            print(f"| {name} | 0 | | | | |")
            continue
        t = statistics.median(r["seconds"] for r in rs)
        tok = statistics.median(r["tokens"] or 0 for r in rs)
        print(f"| {name} | {len(rs)} | {int(t // 60)}:{int(t % 60):02d} | {tok / 1e6:.2f} M | "
              f"{[r.get('verify_rounds') for r in rs]} | {[r.get('final') for r in rs]} |")
    json.dump({"seed": seed, "order": [f"{n}#{i}" for n, _, i in order], "manifests": done},
              open(os.path.join(out_root, f"H{stamp}-summary.json"), "w", encoding="utf-8"), indent=1, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
