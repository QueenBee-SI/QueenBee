"""The queen's answer to a worker's permission question, as a Claude Code hook: python -m queenbee.mother.hook --port N --job W --token T

Claude Code runs this before a tool call (a PreToolUse hook named in the settings file QueenBee passes to the worker with
--settings). It reads the call from stdin (tool_name, tool_input, cwd), asks the queen's server, and answers in the shape Claude
Code expects: allow, deny (with the queen's reason, which the agent reads), or ask (the usual prompt appears, and the queen's
screen reader handles it as before). When the user answers this queen's workers, ask is said out loud: Claude Code then
prompts even for a call its start flags pre-approved. If the server cannot be reached, it says nothing: the prompt appears, nothing is lost.
"""
from __future__ import annotations

import json
import sys
import urllib.request


def decide(port: int, job: int, call: dict, token: str = "") -> dict | None:
    body = json.dumps({"job": job, "cwd": call.get("cwd") or "", "tool_name": call.get("tool_name") or "",
                       "tool_input": call.get("tool_input") or {}}).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-QueenBee-Token"] = token            # the queen's local API refuses requests without it
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/hook", body, headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    port, job, token = 7880, 0, ""
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
        if a == "--job" and i + 1 < len(args):
            job = int(args[i + 1])
        if a == "--token" and i + 1 < len(args):
            token = args[i + 1]
    try:
        call = json.loads(sys.stdin.read() or "{}")
        out = decide(port, job, call, token)
    except Exception:                                  # no server, bad JSON, a timeout: the prompt appears as before
        return 0
    if not out or out.get("decision") not in ("allow", "deny", "ask"):
        return 0
    if out["decision"] == "ask" and not out.get("force"):
        return 0                                       # the vendor's own prompt, as if there were no hook
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": out["decision"],
                                             "permissionDecisionReason": str(out.get("reason") or "the queen's rules")[:300]}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
