"""Keep one Claude Code session's identity out of the agents QueenBee starts.

When QueenBee (or its host) is launched from inside a Claude Code session -- a
tool call in the desktop app, an agent's shell -- it inherits that session's
environment: CLAUDECODE, CLAUDE_CODE_CHILD_SESSION, the parent's session id,
messaging socket and token. Every `claude` QueenBee then starts believes it is a
nested child of that session. Measured: it turned transcript saving off
("inherited CLAUDE_CODE_CHILD_SESSION marker"), so the conversation had no
readable state and the row sat on "working" while the agent waited for input.

These variables describe the launching session, never the user's own setup,
so they are removed from this process's environment before anything is
started from it.
"""

from __future__ import annotations

import os

_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_AGENT_SDK_", "CLAUDE_PREVIEW_")
_NAMES = {"CLAUDECODE", "CLAUDE_PID", "MCP_CONNECTION_NONBLOCKING",
          "MCP_SERVER_CONNECTION_BATCH_SIZE"}


def inherited_session_vars(env: dict[str, str] | None = None) -> list[str]:
    env = os.environ if env is None else env
    return sorted(k for k in env if k in _NAMES or k.startswith(_PREFIXES))


def scrub_environ() -> list[str]:
    """Remove a launching Claude Code session's variables; returns their names."""
    removed = inherited_session_vars()
    for name in removed:
        os.environ.pop(name, None)
    return removed
