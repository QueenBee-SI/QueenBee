"""A readable account of what an agent did, from its Claude Code transcript.

QueenBee's conversation view keeps only tool names ("Bash", "Write"). To follow a
sub-agent the user needs what it ran and which file it wrote, so this reads the
transcript itself: the agent's words, each tool call with its command, file or
pattern, and a short head of each result. Read-only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MAX_BYTES = 4 * 1024 * 1024     # the tail of a long transcript is what matters
DETAIL_KEYS = ("command", "file_path", "path", "pattern", "url", "description", "query", "prompt")


def _detail(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in DETAIL_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c.get("text") or "") for c in content if isinstance(c, dict))
    return ""


def read(path: str, limit: int = 150) -> list[dict[str, Any]]:
    p = Path(path)
    size = p.stat().st_size
    with p.open("rb") as fh:
        if size > MAX_BYTES:
            fh.seek(size - MAX_BYTES)
            fh.readline()                      # drop a partial first line
        raw = fh.read().decode("utf-8", "replace")
    items: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") not in ("user", "assistant") or rec.get("isMeta"):
            continue
        msg = rec.get("message") or {}
        content = msg.get("content")
        ts = rec.get("timestamp")
        role = rec["type"]
        if isinstance(content, str):
            if content.strip():
                items.append({"kind": "text", "role": role, "text": content.strip(), "ts": ts})
            continue
        for block in content or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and str(block.get("text") or "").strip():
                items.append({"kind": "text", "role": role, "text": block["text"].strip(), "ts": ts})
            elif kind == "tool_use":
                items.append({"kind": "tool", "role": role, "tool": block.get("name"),
                              "detail": _detail(block.get("input"))[:400], "ts": ts})
            elif kind == "tool_result":
                text = _result_text(block.get("content")).strip()
                items.append({"kind": "result", "role": role, "error": bool(block.get("is_error")),
                              "text": text[:600], "ts": ts})
    return items[-limit:]
