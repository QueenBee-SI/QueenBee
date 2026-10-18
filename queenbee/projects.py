"""Projects and their resource budgets.

A budget here is **policy, not a driver-level quota**, and the distinction
matters enough to state plainly:

    MIG partitioning     datacenter cards only (A100/H100)
    MPS thread percent   Linux only, needs the MPS daemon; not on WDDM
    CUDA_VISIBLE_DEVICES whole GPUs, never a fraction
    Windows WDDM         no per-process VRAM quota API exists at all

So nothing can force a process to stay under a share of the GPU on ordinary
hardware. What QueenBee actually controls is the moment an agent is *started*, and
that turns out to be where the decision usually matters: on an 8 GB card the
real question is almost always "is there room to start the second thing", not
"can I shrink the first one".

Budgets therefore do three things:

  1. **Admission** -- refuse or warn before launching into an over-budget
     project. This is the only hard gate, and QueenBee owns it.
  2. **Attribution** -- show each project's usage against its share.
  3. **Advice** -- raise a finding on overrun, carrying a suggested action for
     the decision layer to act on later.

A project is matched by directory, so it covers agents, their detached jobs,
and anything else running under the same tree.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("queenbee.projects")

DEFAULT_FILE = "projects.toml"


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/")


@dataclass
class Project:
    name: str
    paths: list[str] = field(default_factory=list)
    # Either may be set; a percentage is resolved against the device total once
    # the GPU is known, so the same config works on a different machine.
    vram_budget_pct: Optional[float] = None
    vram_budget_mb: Optional[float] = None
    ram_budget_pct: Optional[float] = None
    # Higher wins when the decision layer has to choose what to pause.
    priority: int = 50

    def matches(self, cwd: Optional[str]) -> int:
        """Affinity of a working directory to this project, or -1."""
        if not cwd:
            return -1
        target = _norm(cwd)
        best = -1
        for raw in self.paths:
            root = _norm(raw)
            if target == root or target.startswith(root + os.sep):
                best = max(best, len(root))
        return best

    def vram_limit_mb(self, device_total_mb: Optional[float]) -> Optional[float]:
        if self.vram_budget_mb is not None:
            return self.vram_budget_mb
        if self.vram_budget_pct is not None and device_total_mb:
            return device_total_mb * self.vram_budget_pct / 100.0
        return None


@dataclass
class ProjectUsage:
    name: str
    priority: int = 50
    sessions: int = 0
    cpu_pct: float = 0.0
    rss_mb: float = 0.0
    vram_mb: float = 0.0
    vram_limit_mb: Optional[float] = None
    ram_limit_mb: Optional[float] = None

    @property
    def vram_pct_of_budget(self) -> Optional[float]:
        if not self.vram_limit_mb:
            return None
        return 100.0 * self.vram_mb / self.vram_limit_mb

    @property
    def over_vram(self) -> bool:
        return bool(self.vram_limit_mb and self.vram_mb > self.vram_limit_mb)

    def headroom_mb(self) -> Optional[float]:
        if self.vram_limit_mb is None:
            return None
        return max(0.0, self.vram_limit_mb - self.vram_mb)

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["vram_pct_of_budget"] = self.vram_pct_of_budget
        d["over_vram"] = self.over_vram
        d["headroom_mb"] = self.headroom_mb()
        return d


class ProjectSet:
    def __init__(self, projects: Optional[list[Project]] = None) -> None:
        self.projects = projects or []

    @classmethod
    def load(cls, path: str | Path | None = None) -> "ProjectSet":
        target = Path(path) if path else Path.cwd() / DEFAULT_FILE
        if not target.exists():
            target = Path(__file__).resolve().parent.parent / DEFAULT_FILE
        try:
            with open(target, "rb") as fh:
                raw = tomllib.load(fh)
        except FileNotFoundError:
            return cls()
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.warning("cannot read %s (%s); no project budgets", target, exc)
            return cls()

        out: list[Project] = []
        for entry in raw.get("project", []):
            name = str(entry.get("name") or "").strip()
            paths = [str(p) for p in entry.get("paths", []) if str(p).strip()]
            if not name or not paths:
                log.warning("project entry needs a name and at least one path; skipped")
                continue
            out.append(
                Project(
                    name=name,
                    paths=paths,
                    vram_budget_pct=entry.get("vram_budget_pct"),
                    vram_budget_mb=entry.get("vram_budget_mb"),
                    ram_budget_pct=entry.get("ram_budget_pct"),
                    priority=int(entry.get("priority", 50)),
                )
            )
        log.debug("loaded %d projects", len(out))
        return cls(out)

    # -- editing ---------------------------------------------------------

    def _target_file(self) -> Path:
        explicit = Path.cwd() / DEFAULT_FILE
        if explicit.exists():
            return explicit
        return Path(__file__).resolve().parent.parent / DEFAULT_FILE

    def save(self) -> None:
        """Rewrite projects.toml from the in-memory set.

        The file is regenerated rather than patched, so hand-written comments
        above the entries do not survive an edit made in the UI. The header is
        re-emitted each time to keep the "policy, not a quota" caveat with the
        thing it describes.
        """
        lines = [
            "# QueenBee project budgets. Managed from the QueenBee UI -- edits here are",
            "# replaced the next time a project is changed in the app.",
            "#",
            "# A budget is policy, not a driver quota: no consumer GPU can be told to",
            "# hold a process under a share of its memory. Budgets warn before a launch",
            "# and flag overruns; they cannot cap the driver. See queenbee/projects.py.",
            "",
        ]
        for p in self.projects:
            lines.append("[[project]]")
            lines.append(f'name = "{p.name}"')
            paths = ", ".join(f'"{x.replace(chr(92), "/")}"' for x in p.paths)
            lines.append(f"paths = [{paths}]")
            if p.vram_budget_pct is not None:
                lines.append(f"vram_budget_pct = {float(p.vram_budget_pct)}")
            if p.vram_budget_mb is not None:
                lines.append(f"vram_budget_mb = {float(p.vram_budget_mb)}")
            if p.ram_budget_pct is not None:
                lines.append(f"ram_budget_pct = {float(p.ram_budget_pct)}")
            lines.append(f"priority = {int(p.priority)}")
            lines.append("")

        target = self._target_file()
        target.write_text("\n".join(lines), encoding="utf-8")
        log.info("wrote %d projects to %s", len(self.projects), target)

    def upsert(self, project: Project) -> None:
        for i, existing in enumerate(self.projects):
            if existing.name.lower() == project.name.lower():
                self.projects[i] = project
                break
        else:
            self.projects.append(project)
        self.save()

    def remove(self, name: str) -> bool:
        before = len(self.projects)
        self.projects = [p for p in self.projects if p.name.lower() != name.lower()]
        if len(self.projects) != before:
            self.save()
            return True
        return False

    def listing(self) -> list[dict[str, Any]]:
        return [
            {
                "name": p.name,
                "paths": p.paths,
                "vram_budget_pct": p.vram_budget_pct,
                "vram_budget_mb": p.vram_budget_mb,
                "ram_budget_pct": p.ram_budget_pct,
                "priority": p.priority,
            }
            for p in self.projects
        ]

    def for_cwd(self, cwd: Optional[str]) -> Optional[Project]:
        """Most specific project containing this directory."""
        best: Optional[Project] = None
        best_score = -1
        for project in self.projects:
            score = project.matches(cwd)
            if score > best_score:
                best, best_score = project, score
        return best if best_score >= 0 else None

    def usage(
        self,
        sessions: list[Any],
        device_total_mb: Optional[float],
        ram_total_mb: Optional[float],
    ) -> list[ProjectUsage]:
        """Roll live sessions up into their projects."""
        rows: dict[str, ProjectUsage] = {}
        for project in self.projects:
            rows[project.name] = ProjectUsage(
                name=project.name,
                priority=project.priority,
                vram_limit_mb=project.vram_limit_mb(device_total_mb),
                ram_limit_mb=(
                    ram_total_mb * project.ram_budget_pct / 100.0
                    if project.ram_budget_pct and ram_total_mb
                    else None
                ),
            )

        for s in sessions:
            if not getattr(s, "live", False) or getattr(s, "pid", None) is None:
                continue
            project = self.for_cwd(getattr(s, "cwd", None))
            if project is None:
                continue
            try:
                s.project_name = project.name
            except AttributeError:
                pass
            row = rows[project.name]
            row.sessions += 1
            row.cpu_pct += getattr(s, "cpu_pct", None) or 0.0
            row.rss_mb += getattr(s, "rss_mb", None) or 0.0
            row.vram_mb += getattr(s, "vram_mb", None) or 0.0

        return [r for r in rows.values() if r.sessions or r.vram_limit_mb]

    def admit(
        self,
        cwd: str,
        usage: list[ProjectUsage],
        need_mb: float = 0.0,
    ) -> dict[str, Any]:
        """Should an agent be allowed to start here?

        Returns `ok` plus a reason. Being over budget is reported as a warning
        the caller can override rather than a hard refusal: the budget is the
        user's own policy, and a tool that silently refuses to start their work
        gets uninstalled.
        """
        project = self.for_cwd(cwd)
        if project is None:
            return {"ok": True, "project": None}

        row = next((r for r in usage if r.name == project.name), None)
        if row is None or row.vram_limit_mb is None:
            return {"ok": True, "project": project.name}

        headroom = row.headroom_mb() or 0.0
        if row.over_vram:
            return {
                "ok": True,
                "warn": True,
                "project": project.name,
                "reason": (
                    f"{project.name} is over its VRAM budget "
                    f"({row.vram_mb:.0f} MB of {row.vram_limit_mb:.0f} MB)"
                ),
            }
        if need_mb and headroom < need_mb:
            return {
                "ok": True,
                "warn": True,
                "project": project.name,
                "reason": (
                    f"{project.name} has {headroom:.0f} MB of its VRAM budget left, "
                    f"less than the {need_mb:.0f} MB asked for"
                ),
            }
        return {"ok": True, "project": project.name}
