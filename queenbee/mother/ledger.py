"""What each agent cost, from its own Claude Code transcript.

Every assistant message in a transcript carries the API's `usage`: fresh input,
cache writes, cache reads and output tokens, and the model that answered. The
ledger prices them per model, so Mother's savings are measured, not claimed.

Prices are Anthropic first-party rates per million tokens (cached 2026-06-24;
update PRICES when they change). Cache writes cost 1.25x input for the 5-minute
cache and 2x for the 1-hour cache; reads are listed per model.
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path
from typing import Any

# model id prefix -> (input, output, cache read) $/MTok
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4": (5.0, 25.0, 0.50),
    "claude-fable-5": (10.0, 50.0, 1.00),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4": (3.0, 15.0, 0.30),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}


def price_for(model: str) -> tuple[float, float, float]:
    for prefix in sorted(PRICES, key=len, reverse=True):
        if (model or "").startswith(prefix):
            return PRICES[prefix]
    return PRICES["claude-sonnet-5"]            # unknown: priced as Sonnet, flagged


def message_cost(model: str, usage: dict[str, Any]) -> float:
    inp, out, read = price_for(model)
    fresh = usage.get("input_tokens") or 0
    cache_read = usage.get("cache_read_input_tokens") or 0
    creation = usage.get("cache_creation") or {}
    one_hour = creation.get("ephemeral_1h_input_tokens") or 0
    five_min = creation.get("ephemeral_5m_input_tokens")
    if five_min is None:
        five_min = max(0, (usage.get("cache_creation_input_tokens") or 0) - one_hour)
    output = usage.get("output_tokens") or 0
    return (fresh * inp + five_min * inp * 1.25 + one_hour * inp * 2.0
            + cache_read * read + output * out) / 1_000_000


# Codex model prices are not known to QueenBee; fill them in (input, output,
# cached input $/MTok) to get dollars. Without them Codex runs report tokens.
CODEX_PRICES: dict[str, tuple[float, float, float]] = {}


def codex_session_cost(path: str | Path) -> dict[str, Any]:
    """Tokens (and dollars when priced) for one Codex rollout file. Codex
    writes running totals; the last `token_count` event holds the session's."""
    model = cwd = None
    total: dict[str, Any] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            payload = rec.get("payload") or {}
            if rec.get("type") == "session_meta":
                cwd = payload.get("cwd")
            elif rec.get("type") == "turn_context":
                model = payload.get("model") or model
            elif payload.get("type") == "token_count":
                total = ((payload.get("info") or {}).get("total_token_usage")) or total
    fresh = (total.get("input_tokens") or 0) - (total.get("cached_input_tokens") or 0)
    cached = total.get("cached_input_tokens") or 0
    output = total.get("output_tokens") or 0
    price = CODEX_PRICES.get(model or "")
    cost = None if price is None else round((fresh * price[0] + output * price[1] + cached * price[2]) / 1e6, 4)
    return {"path": str(path), "model": model, "cwd": cwd, "input": fresh, "cached_input": cached,
            "output": output, "tokens": fresh + cached + output, "cost_usd": cost}


def transcript_cost(path: str | Path) -> dict[str, Any]:
    """Tokens and dollars for one transcript, split by model.

    Claude Code writes one line per content block of a message, all carrying
    the same message id and usage; each message is counted once.
    """
    seen: set[str] = set()
    by_model: dict[str, dict[str, float]] = {}
    first_ts = last_ts = None
    turns = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            ts = rec.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            usage = msg.get("usage")
            mid = msg.get("id") or rec.get("uuid")
            if not usage or mid in seen or msg.get("model") == "<synthetic>":
                continue
            seen.add(mid)
            turns += 1
            model = msg.get("model") or "unknown"
            row = by_model.setdefault(model, {"input": 0, "cache_write": 0, "cache_read": 0,
                                              "output": 0, "cost_usd": 0.0, "messages": 0})
            row["input"] += usage.get("input_tokens") or 0
            row["cache_write"] += usage.get("cache_creation_input_tokens") or 0
            row["cache_read"] += usage.get("cache_read_input_tokens") or 0
            row["output"] += usage.get("output_tokens") or 0
            row["cost_usd"] += message_cost(model, usage)
            row["messages"] += 1
    total = sum(r["cost_usd"] for r in by_model.values())
    tokens = sum(r["input"] + r["cache_write"] + r["cache_read"] + r["output"] for r in by_model.values())
    return {"path": str(path), "by_model": by_model, "cost_usd": round(total, 4),
            "tokens": int(tokens), "turns": turns, "first": first_ts, "last": last_ts}


def _slug(path: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in os.path.abspath(path).rstrip("\\/"))


_MEMO: dict[tuple[str, str, float, int], dict[str, Any]] = {}


def _cached(kind: str, fn, path: str) -> dict[str, Any]:
    """A file's cost, re-read only when the file changed: the window asks
    every half minute and transcripts run to megabytes."""
    try:
        st = os.stat(path)
    except OSError:
        return fn(path)
    key = (kind, path, st.st_mtime, st.st_size)
    if key not in _MEMO:
        if len(_MEMO) > 4000:
            _MEMO.clear()
        _MEMO[key] = fn(path)
    return _MEMO[key]


def find_worker_transcript(project: str, priority: int, since: float = 0.0) -> str:
    """The Claude Code transcript of worker W<priority> in this project: the newest one written since
    `since` whose opening brief names that job. Empty when there is none."""
    home = os.path.expanduser("~")
    files = glob.glob(os.path.join(home, ".claude", "projects", _slug(project), "*.jsonl"))
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    needle = f"worker bee for job W{priority} ("
    for f in files:
        if os.path.getmtime(f) < since - 5:
            continue
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                head = fh.read(60000)
        except OSError:
            continue
        if needle in head:
            return f
    return ""


def worker_costs(project: str, since: float) -> dict[str, Any]:
    """What the workers of a plan cost: Claude transcripts written in the
    project's folder since `since`, and Codex sessions that ran there."""
    home = os.path.expanduser("~")
    claude = []
    for f in glob.glob(os.path.join(home, ".claude", "projects", _slug(project), "*.jsonl")):
        if os.path.getmtime(f) >= since:
            c = _cached("claude", transcript_cost, f)
            if c["turns"]:
                claude.append(c)
    codex = []
    for f in glob.glob(os.path.join(home, ".codex", "sessions", "**", "*.jsonl"), recursive=True):
        if os.path.getmtime(f) >= since:
            c = _cached("codex", codex_session_cost, f)
            if c["cwd"] and os.path.normcase(os.path.abspath(c["cwd"])) == os.path.normcase(os.path.abspath(project)):
                codex.append(c)
    priced = [c["cost_usd"] for c in claude + codex if c.get("cost_usd") is not None]
    unpriced = any(c.get("cost_usd") is None and c.get("tokens") for c in codex)
    return {"claude": len(claude), "codex": len(codex),
            "tokens": sum(c["tokens"] for c in claude + codex),
            "cost_usd": None if unpriced and not priced else round(sum(priced), 4),
            "unpriced_codex": unpriced}


_NAMES: tuple[float, dict[str, str]] = (0.0, {})
_SEEN: tuple[float, list[str]] = (0.0, [])


def model_ids(max_files: int = 40) -> dict[str, str]:
    """The newest full model id Claude Code has written for each family (haiku, sonnet, opus), read
    from its own transcripts, e.g. {"sonnet": "claude-sonnet-5-5"}. This is how the window can say
    "Sonnet 5.5" instead of guessing. Re-read at most once a minute."""
    import time
    global _NAMES, _SEEN
    if time.time() - _NAMES[0] < 60:
        return _NAMES[1]
    files = glob.glob(os.path.join(os.path.expanduser("~"), ".claude", "projects", "*", "*.jsonl"))
    files.sort(key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0, reverse=True)
    found: dict[str, str] = {}
    seen: list[str] = []
    for f in files[:max_files]:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"model"' not in line:
                        continue
                    try:
                        model = (json.loads(line).get("message") or {}).get("model") or ""
                    except ValueError:
                        continue
                    if model.startswith("claude-") and model not in seen:
                        seen.append(model)                      # every Claude model that has run here, newest files first
                    for fam in ("haiku", "sonnet", "opus"):
                        if f"claude-{fam}-" in model and fam not in found:
                            found[fam] = model
        except OSError:
            continue
    _NAMES = (time.time(), found)
    _SEEN = (time.time(), seen)
    return found


def models_seen() -> list[str]:
    """Every full Claude model id Claude Code has run on this machine (newest transcripts first): how a model released
    after this window was built still shows up in its lists, without anyone editing them."""
    model_ids()
    return list(_SEEN[1])
