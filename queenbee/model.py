"""Core data model for QueenBee.

Everything the engine produces is a plain dataclass here. Sinks (the host
sidebar today, a standalone QueenBee UI later) consume these and nothing else --
no sink is allowed to reach back into a collector.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal, Optional

AgentKind = Literal["api", "local", "shell", "unknown"]
Level = Literal["ok", "warn", "crit"]


@dataclass
class GpuSample:
    index: int
    name: str
    util_pct: Optional[float] = None
    mem_used_mb: Optional[float] = None
    mem_total_mb: Optional[float] = None
    temp_c: Optional[float] = None
    power_w: Optional[float] = None
    power_limit_w: Optional[float] = None
    uuid: Optional[str] = None

    @property
    def mem_pct(self) -> Optional[float]:
        if not self.mem_total_mb:
            return None
        return 100.0 * (self.mem_used_mb or 0.0) / self.mem_total_mb


@dataclass
class SystemSample:
    cpu_pct: float = 0.0
    cpu_cores: int = 0
    load1: Optional[float] = None
    ram_used_mb: float = 0.0
    ram_total_mb: float = 0.0
    swap_used_mb: float = 0.0
    swap_total_mb: float = 0.0
    disk_read_mbs: Optional[float] = None
    disk_write_mbs: Optional[float] = None
    net_recv_mbs: Optional[float] = None
    net_sent_mbs: Optional[float] = None
    battery_pct: Optional[float] = None
    on_battery: Optional[bool] = None
    gpus: list[GpuSample] = field(default_factory=list)

    @property
    def ram_pct(self) -> float:
        return 100.0 * self.ram_used_mb / self.ram_total_mb if self.ram_total_mb else 0.0

    @property
    def swap_pct(self) -> float:
        return 100.0 * self.swap_used_mb / self.swap_total_mb if self.swap_total_mb else 0.0

    @property
    def vram_free_mb(self) -> Optional[float]:
        """Free VRAM across all GPUs, or None when no GPU telemetry is available."""
        if not self.gpus:
            return None
        total = 0.0
        seen = False
        for g in self.gpus:
            if g.mem_total_mb is None:
                continue
            seen = True
            total += g.mem_total_mb - (g.mem_used_mb or 0.0)
        return total if seen else None

    @property
    def vram_pct(self) -> Optional[float]:
        pcts = [g.mem_pct for g in self.gpus if g.mem_pct is not None]
        return max(pcts) if pcts else None


@dataclass
class ProcSample:
    pid: int
    name: str
    cmdline: str = ""
    cpu_pct: float = 0.0
    rss_mb: float = 0.0
    vram_mb: Optional[float] = None
    gpu_pct: Optional[float] = None
    # "tree" when the process really is a descendant of the pane's shell;
    # "cwd" when ownership was inferred from a matching working directory.
    # Inferred attribution is a good guess, not proof, and is labelled as such
    # everywhere it surfaces.
    attribution: Literal["tree", "cwd"] = "tree"


@dataclass
class AgentSample:
    """One terminal pane, with the resource cost of everything running inside it."""

    pane_id: str
    workspace_id: str
    tab_id: str = ""
    agent: Optional[str] = None
    agent_status: str = "unknown"
    kind: AgentKind = "unknown"
    label: Optional[str] = None
    cwd: Optional[str] = None
    shell_pid: Optional[int] = None
    procs: list[ProcSample] = field(default_factory=list)
    # Rollups over the whole process tree owned by the pane.
    cpu_pct: float = 0.0
    rss_mb: float = 0.0
    vram_mb: Optional[float] = None
    gpu_pct: Optional[float] = None
    uptime_s: Optional[float] = None
    # True when the platform cannot attribute VRAM to processes at all.
    vram_attributable: bool = True
    # Some of this agent's load came from cwd matching rather than the process
    # tree, so the numbers are a best guess.
    has_inferred: bool = False


@dataclass
class Advice:
    """A finding. V1 only displays these; the decision layer will act on them.

    `suggested_action` is deliberately inert in V1 -- it is the seam the future
    scheduler plugs into, so the rules never have to be rewritten.
    """

    level: Level
    code: str
    message: str
    scope: Literal["system", "pane"] = "system"
    pane_id: Optional[str] = None
    suggested_action: Optional[str] = None


@dataclass
class Snapshot:
    ts: float
    system: SystemSample
    agents: list[AgentSample] = field(default_factory=list)
    # GPU memory held by processes that belong to no pane -- an `ollama serve`
    # daemon, a stray notebook kernel. Without this the numbers look like they
    # do not add up, and the user blames the wrong agent.
    external_gpu: list[ProcSample] = field(default_factory=list)
    advice: list[Advice] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # False when QueenBee is running without its host: machine telemetry is still
    # real, there are just no panes to attribute it to.
    host_connected: bool = True
    # Agent sessions read from the agents' own transcripts. Independent of
    # the host entirely -- this is the list that survives without it.
    sessions: list[Any] = field(default_factory=list)
    # Per-project rollups against the budgets in projects.toml.
    projects: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # asdict only copies fields, so every @property is dropped -- including
        # ones the UI reads by name. Nested objects that compute their own
        # extras are re-serialised through their own to_dict rather than left
        # silently incomplete (a missing `age_s` showed up as a blank column,
        # not as an error).
        d["system"]["ram_pct"] = self.system.ram_pct
        d["system"]["swap_pct"] = self.system.swap_pct
        d["system"]["vram_pct"] = self.system.vram_pct
        d["system"]["vram_free_mb"] = self.system.vram_free_mb
        for src, dst in zip(self.system.gpus, d["system"]["gpus"]):
            dst["mem_pct"] = src.mem_pct
        d["sessions"] = [s.to_dict() for s in self.sessions]
        d["projects"] = [p.to_dict() for p in self.projects]
        return d
