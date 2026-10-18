"""QueenBee configuration: defaults, TOML loading, threshold definitions."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_NAME = "queenbee.toml"


@dataclass
class Thresholds:
    vram_warn_pct: float = 80.0
    vram_crit_pct: float = 92.0
    ram_warn_pct: float = 85.0
    # 94% was too late to be useful: by the time Windows is that deep into swap
    # the UI has already stopped responding, so the warning arrives after the
    # damage. Fire while there is still room to act.
    ram_crit_pct: float = 90.0
    swap_warn_pct: float = 25.0
    cpu_warn_pct: float = 90.0
    gpu_temp_warn_c: float = 84.0
    # A new local-model agent needs at least this much free VRAM to be safe.
    local_agent_headroom_mb: float = 2048.0
    # Sustained-pressure rules only fire after this many consecutive ticks.
    sustain_ticks: int = 3


@dataclass
class Config:
    # Each tick spawns a host snapshot plus an `nvidia-smi`, and process
    # creation is expensive on Windows. 5s keeps QueenBee's own cost off the
    # measurement it is trying to report.
    interval_s: float = 5.0
    host_bin: str = ""
    source_id: str = "queenbee"
    # Metadata TTL: if QueenBee dies, the host expires our rows instead of leaving
    # numbers on screen that stopped being true.
    ttl_multiplier: float = 4.0
    show_pane_rows: bool = True
    show_workspace_rows: bool = True
    # Writing `display_agent` overwrites the label the host uses for the agent's
    # own identity, so the sidebar shows QueenBee's numbers *instead of* "claude".
    # Tokens render on their own row and clobber nothing, so they are the
    # default; this stays available for setups with no custom row template.
    write_display_agent: bool = False
    # Every snapshot is appended here; the future standalone QueenBee UI reads it.
    jsonl_path: str | None = None
    jsonl_max_mb: float = 32.0
    thresholds: Thresholds = field(default_factory=Thresholds)
    # Extra substrings that mark a process as a local model runner.
    local_model_hints: list[str] = field(default_factory=list)
    # Decision layer: off | advise | queue | act. See queenbee/decisions.py.
    decisions: dict = field(default_factory=dict)

    @property
    def ttl_ms(self) -> int:
        return max(1, int(self.interval_s * self.ttl_multiplier * 1000))


def _state_dir() -> Path:
    """The state folder: QUEENBEE_STATE_DIR when set, else a per-user path."""
    env = os.environ.get("QUEENBEE_STATE_DIR")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    return Path(base) / "queenbee" if base else Path.home() / ".local" / "state" / "queenbee"


def lock_path() -> Path:
    """Where the single-instance lock lives. One per user, not per project."""
    return _state_dir() / "queenbee.lock"


def config_search_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("QUEENBEE_CONFIG")
    if env:
        paths.append(Path(env))
    cfg_dir = os.environ.get("QUEENBEE_CONFIG_DIR")
    if cfg_dir:
        paths.append(Path(cfg_dir) / DEFAULT_CONFIG_NAME)
    paths.append(Path.cwd() / DEFAULT_CONFIG_NAME)
    paths.append(Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_NAME)
    return paths


def load(path: str | os.PathLike[str] | None = None) -> Config:
    raw: dict[str, Any] = {}
    candidates = [Path(path)] if path else config_search_paths()
    for candidate in candidates:
        try:
            with open(candidate, "rb") as fh:
                raw = tomllib.load(fh)
            break
        except FileNotFoundError:
            continue
        except (OSError, tomllib.TOMLDecodeError):
            # A broken config degrades to defaults; it never kills the daemon.
            break

    cfg = Config()
    section = raw.get("queenbee", raw)
    for key in (
        "interval_s",
        "host_bin",
        "source_id",
        "ttl_multiplier",
        "show_pane_rows",
        "show_workspace_rows",
        "write_display_agent",
        "jsonl_path",
        "jsonl_max_mb",
        "local_model_hints",
        "decisions",
    ):
        if key in section:
            cfg = replace(cfg, **{key: section[key]})

    if "decisions" in raw:
        cfg = replace(cfg, decisions=raw["decisions"])

    th_raw = raw.get("thresholds", section.get("thresholds", {}))
    for key, value in th_raw.items():
        if hasattr(cfg.thresholds, key):
            setattr(cfg.thresholds, key, value)

    if cfg.jsonl_path is None:
        cfg = replace(cfg, jsonl_path=str(_state_dir() / "snapshots.jsonl"))

    # The environment, when set, is the source of truth for the host binary's path.
    if os.environ.get("QUEENBEE_HOST_BIN"):
        cfg = replace(cfg, host_bin=os.environ["QUEENBEE_HOST_BIN"])
    return cfg
