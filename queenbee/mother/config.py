"""Which models QueenBee uses. Configuration, not code.

Override any of these in `mother.toml` at the repository root:

    planner_model = "sonnet"

    [models.claude]
    low = "haiku"
    medium = "sonnet"
    high = "sonnet"           # "opus" to allow a larger model for hard jobs

    [models.codex]            # empty = the user's own Codex default model
    low = ""
    medium = ""
    high = ""

    agents = ["claude", "codex"]   # which providers Mother may start (default: Claude only)

    [efforts.codex]           # Codex reasoning effort per job effort
    low = "low"
    medium = "low"
    high = "medium"

Effort is the planner's estimate of how hard a job is: "low" for re-running
work another agent already adapted, "medium" for adapting a known script,
"high" for unclear goals or new code.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any

# mother.toml lives beside the code -- or beside QueenBee.exe when packaged.
ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent.parent.parent)
EFFORTS = ("low", "medium", "high")
AGENTS = ("claude", "codex", "gemini", "kimi", "opencode", "cursor", "mistral", "grok")

# Token-lean by default: nothing above Sonnet. A harder job gets Sonnet, not a
# larger model; escalation beyond it is opt-in through mother.toml.
DEFAULTS: dict[str, Any] = {
    # Think once, expensively; execute cheaply. The planner splits the work,
    # the architect (read-only) writes exact work orders, workers run them.
    "planner_model": "sonnet",
    "architect_model": "sonnet",   # opus only as an opt-in escalation (mother.toml)
    "worker_model": "haiku",
    "escalation": ["sonnet"],          # a worker that fails is retried once on this
    "answer_model": "haiku",           # Mother answering a worker's question
    # Providers Mother may start. Claude only for now (the user's choice,
    # 2026-09-27); add "codex" in mother.toml to use both.
    "agents": ["claude"],
    # Nothing above this model runs in any role (planner, architect, workers,
    # escalation, a model picked in the window): the ceiling of a fair
    # comparison with one Sonnet agent. "opus" here lifts it.
    "max_model": "opus",
    # The model of the queen's own thinking and of the strong worker (a package kept whole, the
    # senior, a hard part). Chosen per queen in the window; Sonnet unless the user says otherwise.
    "strong_model": "sonnet",
    # Recursive queens are experimental until a measurement shows a gain:
    # a worker may become a queen only this deep, with at most this many
    # agents, and while the branches' planning has cost less than this.
    "max_depth": 1,
    "max_children": 4,
    "branch_budget_usd": 1.0,
    "models": {
        "claude": {"low": "haiku", "medium": "sonnet", "high": "sonnet"},
        "codex": {"low": "", "medium": "", "high": ""},
    },
    # Codex runs the user's own default model; cost is steered through its
    # reasoning effort instead, kept one step below the job's effort.
    "efforts": {
        "codex": {"low": "low", "medium": "low", "high": "medium"},
    },
    # Relative prices per million tokens, "agent/model": number, from mother.toml's [prices] table. Empty by default: the
    # planner then reasons from ability alone. With prices, it prefers the cheaper of two agents able to do a part.
    "prices": {},
}


def load(path: Path | None = None) -> dict[str, Any]:
    cfg = {k: DEFAULTS[k] for k in ("planner_model", "architect_model", "worker_model", "answer_model", "max_model", "strong_model")}
    for key in ("max_depth", "max_children", "branch_budget_usd"):
        cfg[key] = DEFAULTS[key]
    cfg["escalation"] = list(DEFAULTS["escalation"])
    cfg["agents"] = list(DEFAULTS["agents"])
    cfg["models"] = {a: dict(m) for a, m in DEFAULTS["models"].items()}
    cfg["efforts"] = {a: dict(m) for a, m in DEFAULTS["efforts"].items()}
    cfg["prices"] = dict(DEFAULTS["prices"])
    path = path or ROOT / "mother.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return capped(cfg)
    for key in ("planner_model", "architect_model", "worker_model", "answer_model", "max_model", "strong_model"):
        if isinstance(data.get(key), str):
            cfg[key] = data[key]
    if isinstance(data.get("prices"), dict):
        cfg["prices"] = {str(k): float(v) for k, v in data["prices"].items() if isinstance(v, (int, float)) and v >= 0}
    for key in ("max_depth", "max_children", "branch_budget_usd"):
        if isinstance(data.get(key), (int, float)) and data[key] >= 0:
            cfg[key] = data[key]
    if isinstance(data.get("agents"), list):
        chosen = [a for a in (str(x) for x in data["agents"]) if a in AGENTS]
        cfg["agents"] = chosen or cfg["agents"]
    if isinstance(data.get("escalation"), list):
        cfg["escalation"] = [str(m) for m in data["escalation"]]
    for section in ("models", "efforts"):
        for agent, levels in (data.get(section) or {}).items():
            if agent in cfg[section] and isinstance(levels, dict):
                cfg[section][agent].update({k: str(v) for k, v in levels.items() if k in EFFORTS})
    return capped(cfg)


RANK = {"haiku": 0, "sonnet": 1, "opus": 2}
ROLES = ("planner_model", "architect_model", "worker_model", "answer_model", "strong_model")


def clamp(model: str, cap: str) -> str:
    """`model` itself, or `cap` when it is a larger Claude model."""
    if cap in RANK and model in RANK and RANK[model] > RANK[cap]:
        return cap
    return model


def capped(cfg: dict[str, Any]) -> dict[str, Any]:
    """cfg with every role, escalation step and level held to max_model."""
    cap = cfg.get("max_model") or ""
    for key in ROLES:
        cfg[key] = clamp(cfg.get(key, ""), cap)
    cfg["escalation"] = list(dict.fromkeys(clamp(m, cap) for m in cfg.get("escalation") or []))
    cfg["models"]["claude"] = {k: clamp(v, cap) for k, v in cfg["models"]["claude"].items()}
    return cfg


def model_for(cfg: dict[str, Any], agent: str, effort: str) -> str:
    return cfg["models"].get(agent, {}).get(effort, "")


def reasoning_for(cfg: dict[str, Any], agent: str, effort: str) -> str:
    return (cfg.get("efforts") or {}).get(agent, {}).get(effort, effort)
