"""The QueenBee page, its state, and plan / start.

POST /api/plan turns an instruction into a plan (one model call); POST
/api/start carries a plan the user reviewed out. Like QueenBee's own UI server
these refuse other websites (Host/Origin/JSON checks); other programs on this machine are kept out by the token of
auth.py, which every /api/ request must carry (the page gets it in the URL the app opens, the hook and the drivers read it
from the state folder).
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import sys
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import psutil

from ..agents.host_client import HostClient, HostUnavailable
from .screen import render
from . import auth, ledger

log = logging.getLogger("queenbee.mother")

STATIC = Path(__file__).resolve().parent / "static"
_VENDOR = Path(__file__).resolve().parent / "static" / "vendor"
FILES = {"/": STATIC / "index.html", "/index.html": STATIC / "index.html",
         "/mother.css": STATIC / "mother.css", "/mother.js": STATIC / "mother.js",
         "/queenbee.png": STATIC / "queenbee.png",
         # The terminal a worker runs in, drawn with QueenBee's own xterm.js.
         "/vendor/xterm.js": _VENDOR / "xterm.js", "/vendor/xterm.css": _VENDOR / "xterm.css",
         "/vendor/xterm-addon-fit.js": _VENDOR / "xterm-addon-fit.js"}


def _agent_ready(agent: str) -> bool:
    """Whether an agent the instruction names is installed here."""
    from ..agents.catalog import binary_for

    return bool(binary_for(agent))


def _providers() -> list[dict[str, Any]]:
    """The agent tools on this machine and what can run under each."""
    from ..agents.catalog import binary_for

    claude_models = list(CLAUDE_MODELS) + [m for m in ledger.models_seen()[:12] if m not in CLAUDE_MODELS and MODEL_ID.fullmatch(m)]
    rows = [("claude", "Claude Code", claude_models),
            ("codex", "Codex", ["default", *_codex_models()]),
            ("gemini", "Gemini", ["default"]),
            ("kimi", "Kimi", ["default"]),
            ("opencode", "OpenCode", ["default"]),
            ("cursor", "Cursor Agent", ["default"]),
            ("mistral", "Mistral Vibe", ["default"]),
            ("grok", "Grok Build", ["default"])]
    return [{"id": pid, "name": name, "installed": bool(binary_for(pid)), "models": models,
             "experimental": pid not in ("claude", "codex")} for pid, name, models in rows]


# Every model a Claude Code worker or queen can be given: the three aliases (always the newest of each
# family) and Fable. Any other id can be typed in the window.
CLAUDE_MODELS = ("haiku", "sonnet", "opus", "claude-fable-5-1",
                 "claude-sonnet-5", "claude-opus-5", "claude-fable-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6")
MODEL_ID = re.compile(r"[A-Za-z0-9][\w.\-:/]{0,79}")


def _codex_models() -> list[str]:
    """Models the installed Codex knows, from the list it caches itself; empty when there is none."""
    path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "models_cache.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("models") if isinstance(data, dict) else data
        slugs = [str(r.get("slug") or r.get("id") or "") if isinstance(r, dict) else str(r) for r in rows or []]
        return [s for s in dict.fromkeys(slugs) if MODEL_ID.fullmatch(s)][:30]
    except (OSError, ValueError, AttributeError):
        return []


def _codex_model() -> str:
    """The model Codex runs when none is passed: `model` in its config."""
    path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r'\s*model\s*=\s*"([^"]+)"', line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return ""


def _local(value: str) -> bool:
    host = (value or "").rsplit(":", 1)[0].strip("[]").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def _local_origin(origin: str) -> bool:
    if not origin:
        return True
    parts = urlsplit(origin)
    return parts.scheme == "http" and (parts.hostname or "") in ("127.0.0.1", "localhost", "::1")


def _apply_order(job: dict[str, Any], order: dict[str, Any]) -> None:
    """Copy the architect's work order for one job onto it."""
    for key in ("work_order", "command", "metrics_path", "metric_key",
                "command_template", "metrics_template", "seed_flag"):
        job[key] = order.get(key, "")
    job["files"] = order.get("files") or []
    job["effective_epoch"] = order.get("effective_epoch")


_LOCAL: dict[str, Any] = {"at": 0.0, "value": {}, "collector": None, "refreshing": False}


def local_machine() -> dict[str, Any]:
    """CPU, RAM and every GPU measured here, cached 3 s: used when QueenBee's UI
    server, which normally measures them, is not running. Measuring takes a
    second or more, so only the first call waits; later ones get the last
    figures at once and the measuring goes on behind them."""
    with _SNAP_LOCK:
        if _LOCAL["at"] and (time.time() - _LOCAL["at"] < 3 or _LOCAL["refreshing"]):
            return _LOCAL["value"]
        first = not _LOCAL["at"]
        if first and _LOCAL["refreshing"]:
            return _LOCAL["value"]
        _LOCAL["refreshing"] = True
    if first:
        _sample_local()
    else:
        threading.Thread(target=_sample_local, name="machine-refresh", daemon=True).start()
    with _SNAP_LOCK:
        return _LOCAL["value"]


def _sample_local() -> None:
    from ..collectors.system import SystemCollector

    value = None
    try:
        if _LOCAL["collector"] is None:
            _LOCAL["collector"] = SystemCollector()
        smp = _LOCAL["collector"].sample()
        value = {
            "cpu_pct": smp.cpu_pct, "cpu_cores": smp.cpu_cores,
            "ram_used_mb": smp.ram_used_mb, "ram_total_mb": smp.ram_total_mb,
            "gpus": [{"index": g.index, "name": g.name, "util_pct": g.util_pct,
                      "mem_used_mb": g.mem_used_mb, "mem_total_mb": g.mem_total_mb,
                      "temp_c": g.temp_c, "power_w": g.power_w} for g in smp.gpus],
        }
    except Exception:
        log.debug("could not measure the machine", exc_info=True)
    with _SNAP_LOCK:
        if value is not None:
            _LOCAL["value"] = value
        _LOCAL["at"], _LOCAL["refreshing"] = time.time(), False


# The QueenBee snapshot, shared by every queen: building it takes QueenBee seconds.
_SNAP: dict[str, Any] = {"at": 0.0, "states": {}, "paths": {}, "usage": {}, "system": {}, "refreshing": False}
_SNAP_LOCK = threading.Lock()


def _refresh_snapshot(ui_port: int) -> None:
    """Fetch QueenBee's snapshot (the agents' screen states, their transcripts, the machine) into `_SNAP`."""
    import urllib.request

    states: dict[str, tuple[str, str]] = {}
    paths: dict[str, str] = {}
    usage: dict[str, tuple] = {}
    system = None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{ui_port}/api/snapshot", timeout=4) as r:
            snap = json.loads(r.read())
        # The machine as QueenBee measures it: CPU, RAM and each GPU.
        system = snap.get("system") or {}
        for row in snap.get("sessions") or []:
            if not row.get("terminal_id"):
                continue
            if row.get("live") is not False:
                usage[row["terminal_id"]] = (row.get("cpu_pct"), row.get("rss_mb"), row.get("vram_mb"))
            states[row["terminal_id"]] = (str(row.get("state") or ""), str(row.get("state_reason") or ""))
            if row.get("path"):
                paths[row["terminal_id"]] = str(row["path"])
    except (OSError, ValueError):
        pass
    with _SNAP_LOCK:
        _SNAP.update(at=time.time(), states=states, paths=paths, usage=usage, refreshing=False)
        if system is not None:
            _SNAP["system"] = system


LIMIT_KEYS = ("workers", "budget_tokens", "budget_usd", "ram_mb", "cpu_pct", "vram_mb")
ROLE_KEYS = ("planner_model", "architect_model", "worker_model", "progress_lines", "strong_model", "max_model", "director_agent", "director_model", "queen_agent",
             "beekeeper_agent", "beekeeper_model", "beekeeper_rules", "codex_worker_model", "write_context", "read_scope", "director_interval_s",
             "director_wake", "director_min_gap_s")


def clean_limits(raw: dict[str, Any]) -> dict[str, float]:
    """Positive numbers only; anything else means no limit."""
    out = {}
    for key in LIMIT_KEYS:
        try:
            value = float(raw.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            out[key] = int(value) if key in ("workers", "budget_tokens") else value
    return out


class Mother:
    """The plan and its dispatch for this window. One plan at a time."""

    def __init__(self, host: HostClient, ui_port: int = 7878, queen: str = "q1",
                 name: str = "", primary: bool = True, agent: str = "", model: str = "",
                 limits: Optional[dict[str, Any]] = None, roles: Optional[dict[str, str]] = None) -> None:
        from . import config

        self.queen, self.name, self.primary = queen, name or f"Queen {queen[1:]}", primary
        self.agent, self.model = agent, model        # empty: the configured defaults
        self.description = ""                        # what this queen is for, in the user's words
        self.manual_approvals = False                # a "one agent, no queen" run: every question waits for the user
        self.user_answers = False                    # the user chose to answer the workers' prompts; the queen only watches
        self.solo = False                            # a free worker: one agent on the map that belongs to no queen
        self.manual_delay_s = 0.0                    # recorded demos: a person at the desk answers after this long
        self.director_on = False                     # her own model looks in on the hive (director.py)
        self.director = None
        self.beekeeper = None                        # watches the queen against the user's rules (beekeeper.py)
        # What this queen may use at most; a missing key has no limit.
        self.limits: dict[str, float] = clean_limits(limits or {})
        self.host = host
        self.ui_port = ui_port
        # Which model plans, which prepares, which works -- the configured
        # ones unless this queen was given its own (a fair comparison fixes
        # the planner to the model the single agent runs on).
        self.roles = {k: str(v) for k, v in (roles or {}).items() if k in ROLE_KEYS and v}
        self.cfg = config.capped({**config.load(), **self.roles})
        self.plan: Optional[dict[str, Any]] = None
        self._dispatch = None
        self.planning = False
        self.lock = threading.Lock()
        self._states: dict[str, tuple[str, str]] = {}
        self._paths: dict[str, str] = {}
        self._states_at = 0.0
        # A plan started before the window was closed carries on being followed.
        from .dispatch import Dispatch

        self.preparing = False
        self.prepared: Optional[dict[str, Any]] = None
        # The queen's own conversation: what the user told it, what it did.
        self.commanding: Optional[str] = None
        self.command_error: Optional[str] = None
        self.commands: list[dict[str, Any]] = []
        self._usage: dict[str, tuple] = {}
        self._cost: dict[str, Any] = {}
        self._cost_at = 0.0
        self.completion: Optional[dict[str, Any]] = None     # the queen's completion report once her team has stopped
        self.queen_term: Optional[dict[str, Any]] = None     # her own terminal: {"id", "folder", "started", "agent"}
        self._hive_written = ("", 0.0)
        self._hint_agents: tuple[str, ...] = ()               # the agents a command itself named
        self.approvals: list[dict[str, Any]] = []
        self._answered: dict[tuple[str, str], dict[str, Any]] = {}
        self._pending: dict[tuple, float] = {}
        restored = Dispatch.restore(self.cfg, host, ui_port, queen)
        if restored is not None:
            self._wire(restored)
            if not restored.done.is_set():
                # A team at work when the window closed. Workers whose terminals are still open never stopped, and
                # she keeps following them; if none is, nothing starts again until the user says continue or stop.
                try:
                    open_ = {t.get("id") for t in host._call("terminals") or [] if t.get("running")}
                except HostUnavailable:
                    open_ = set()
                restored.held = not any(j.get("terminal") in open_ for j in restored.jobs
                                        if j.get("terminal") and j.get("status") not in restored.ENDED)
                restored.start()
            self.dispatch = restored
        threading.Thread(target=self._approve_loop, name="mother-approver", daemon=True).start()

    def make_plan(self, instruction: str, folder: str = "") -> dict[str, Any]:
        from .planner import PlanError, plan

        with self.lock:
            if self.planning:
                return {"ok": False, "error": "already planning"}
            if self.dispatch is not None and not self.dispatch.done.is_set():
                return {"ok": False, "error": "a plan is already running"}
            if self.preparing:
                return {"ok": False, "error": "the architect is still preparing the current plan"}
            self.planning = True
        try:
            folder = folder.strip()
            result = plan(instruction, prices=self.cfg.get("prices"), model=planner_model or self.cfg["planner_model"],
                          folder=folder if os.path.isdir(folder) else "",
                          agents=self._agents())
            result["instruction"] = instruction
            # Every plan has its own identity; work orders prepared for one
            # plan must never reach another (a new plan once picked up the
            # previous plan's orders by priority number).
            result["plan_id"] = f"plan-{int(time.time() * 1000)}"
            self.plan = result
            self.prepared = None
            return {"ok": True, "plan": result}
        except PlanError as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            self.planning = False

    def _agents(self) -> tuple[str, ...]:
        if getattr(self, "_hint_agents", ()):          # the instruction itself named the agents to use
            return tuple(self._hint_agents)
        if self.agent == "auto":                       # "let the queen advise": any agent that is installed
            found = tuple(p["id"] for p in _providers() if p["installed"] and p["models"] and p["id"] in ("claude", "codex"))
            return found or ("claude",)
        return (self.agent,) if self.agent else tuple(self.cfg.get("agents") or ("claude",))

    def _advising(self) -> bool:
        """The user left the workers' models to the queen: each part gets the least capable model that
        suits how hard the planner rated it."""
        return self.model == "auto"

    def _tier(self, job: dict[str, Any]) -> None:
        """The queen's chosen model for a worker on the queen's agent: for the
        parts of a split package. A package kept whole stays on the strong
        model -- that is the point of keeping it whole."""
        if job.get("alone"):
            return
        if self._advising():
            return
        if self.agent and self.agent != "auto" and self.model and job.get("agent") == self.agent:
            job["tier"] = None if self.model == "default" else self.model

    def add_worker(self, goal: str, folder: str, agent: str = "", model: str = "",
                   parent: Optional[int] = None, acceptance: Optional[list] = None,
                   instruction: str = "", title: str = "", plain: bool = False, bare: bool = False) -> dict[str, Any]:
        """A worker bee added from the map, with no plan needed first."""
        from .dispatch import Dispatch

        with self.lock:
            if self.dispatch is None:
                self.dispatch = Dispatch([], self.cfg, self.host, self.ui_port)
                self.dispatch.first_priority = getattr(self, "first_priority", 1)
                self._wire(self.dispatch)
                if not self.dispatch.instruction:
                    self.dispatch.instruction = (self.plan or {}).get("instruction", "")
        queen_agent = "" if self.agent == "auto" else self.agent
        agent = agent or queen_agent or (self.cfg.get("agents") or ["claude"])[0]
        model = model or (self.model if agent == queen_agent and self.model != "auto" else "") or \
            (self.cfg.get("worker_model") if agent == "claude" else "default")
        self.ensure_director()
        if instruction.strip() and not self.dispatch.instruction:
            self.dispatch.instruction = instruction.strip()          # what the queen's box shows
        return self.dispatch.add_job(goal, folder, agent, model, parent, acceptance, title, plain, bare)

    def ensure_director(self) -> None:
        """Start the queen's periodic look at her hive (once), when she has a model to do it with."""
        if not self.director_on or self.director is not None:
            return
        from .director import Director

        model = self.roles.get("strong_model") or self.cfg.get("strong_model") or "sonnet"

        def may_spend() -> bool:
            cap, spent = self.limits.get("budget_tokens"), (self._cost or {}).get("tokens")
            usd_cap, usd = self.limits.get("budget_usd"), (self._cost or {}).get("cost_usd")
            return not ((cap and spent is not None and spent >= cap) or (usd_cap and usd is not None and usd >= usd_cap))

        self.director = Director(lambda: self.dispatch, model, float(self.cfg.get("director_interval_s") or 120), may_spend,
                                 self.roles.get("director_agent") or "claude",
                                 task=lambda: self.description or (self.dispatch.instruction if self.dispatch else ""),
                                 wake_on_events=str(self.cfg.get("director_wake") or "on").lower() not in ("off", "no", "0", "false"),
                                 min_gap_s=float(self.cfg.get("director_min_gap_s") or 45))
        self.director.codex_model = self.roles.get("director_model", "")
        threading.Thread(target=self.director.run, name=f"queen-{self.queen}-director", daemon=True).start()

    def _beekeeper(self):
        if self.beekeeper is None:
            from .beekeeper import Beekeeper

            def folder() -> str:
                qt = self.queen_term
                if qt:
                    return qt["folder"]
                d = self.dispatch
                return next((j["project_path"] for j in (d.view() if d else []) if j.get("project_path")), "")

            self.beekeeper = Beekeeper(lambda: self.dispatch, folder,
                                       lambda: self.description or (self.dispatch.instruction if self.dispatch else ""),
                                       open_terminal=self._open_beekeeper_terminal)
            self.beekeeper.close_terminal = self._close_terminal
            self.beekeeper.agent = self.roles.get("beekeeper_agent", "")
            self.beekeeper.model = self.roles.get("beekeeper_model", "")
            self.beekeeper.rules = self.roles.get("beekeeper_rules", "")
            threading.Thread(target=self.beekeeper.run, name=f"queen-{self.queen}-beekeeper", daemon=True).start()
        return self.beekeeper

    def _open_beekeeper_terminal(self, agent: str, model: str, folder: str, brief: str) -> str:
        """A terminal for a beekeeper that is not Codex or Claude, started like the queen's."""
        from ..agents.catalog import binary_for
        from . import queen_term

        binary = binary_for(agent)
        if not binary:
            raise RuntimeError(f"{agent} is not installed")
        result = self.host._call("open_terminal", folder, queen_term.argv(agent, binary, model, brief), cols=120, rows=26)
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "the host did not open a terminal")
        if agent in queen_term.TYPED_FIRST:
            queen_term.type_first(self.host, folder, result["id"], agent, brief, name="beekeeper")
        return result["id"]

    def _close_terminal(self, term: str) -> None:
        try:
            self.host._call("terminal_close", term)
        except Exception:                              # already closed, or the host is gone
            pass

    def set_beekeeper(self, agent: str, model: str, rules: str) -> dict[str, Any]:
        """Who judges the queen (any agent) and which extra files hold her rules. Codex and Claude run quietly; the others
        work in a terminal you can watch."""
        from ..agents.catalog import binary_for

        if agent not in ("", "codex", "claude") and agent not in {p["id"] for p in _providers()}:
            return {"ok": False, "error": f"unknown agent {agent}"}
        if agent and not binary_for(agent):
            return {"ok": False, "error": f"{agent} is not installed"}
        if model and not MODEL_ID.fullmatch(model):
            return {"ok": False, "error": f"unknown model {model}"}
        bk = self._beekeeper()
        bk.agent, bk.model, bk.rules = agent, model, rules.strip()[:400]
        self.roles.update(beekeeper_agent=agent, beekeeper_model=model, beekeeper_rules=bk.rules)
        return {"ok": True}

    def ask_beekeeper(self) -> dict[str, Any]:
        bk = self._beekeeper()
        if bk.busy:
            return {"ok": True, "busy": True}
        threading.Thread(target=bk.audit, name=f"queen-{self.queen}-beekeeper-now", daemon=True).start()
        return {"ok": True}

    def set_director(self, model: str) -> dict[str, Any]:
        """Change the model the queen directs with: any Claude model (haiku, sonnet, opus, an id) or
        "codex" / "codex:<model>"."""
        from ..agents.catalog import binary_for

        if model.startswith("agent:"):
            return self._set_queen_agent(model[6:])
        self.roles.pop("queen_agent", None)
        codex = model == "codex" or model.startswith("codex:")
        if not MODEL_ID.fullmatch(model.replace("codex:", "", 1) or "x"):
            return {"ok": False, "error": f"unknown model {model}"}
        if codex and not binary_for("codex"):
            return {"ok": False, "error": "Codex is not installed"}
        agent = "codex" if codex else "claude"
        name = "sonnet" if codex else model                  # Claude names stay for the planner's own steps
        self.roles.update(strong_model=name, planner_model=name, architect_model=name, director_agent=agent,
                          director_model=model.split(":", 1)[1] if model.startswith("codex:") else "")
        self.cfg.update(strong_model=name, planner_model=name, architect_model=name)
        self.director_on = True
        self._drop_queen_terminal()
        if self.director is not None:
            self.director.model, self.director.agent = name, agent
            self.director.codex_model = self.roles.get("director_model", "")
            self.director.last_digest = ""             # look again with the new model
        else:
            self.ensure_director()
        return {"ok": True}

    def set_final_check(self, command: str) -> dict[str, Any]:
        """The delivery check: when every worker is in, the queen runs this test command over the whole
        folder and reports one result (and sends a fixer if it fails)."""
        d = self.dispatch
        task = next((j for j in (d.jobs if d else []) if j.get("kind") == "task"), None)
        if task is None:
            return {"ok": False, "error": "add a worker first: the check runs in its folder"}
        command = command.strip()
        if not command:
            with d.lock:
                d.verify = {}
            return {"ok": True}
        d.set_verify(command, task["project_path"], trusted=True)           # typed by the user
        if not d.verify:
            return {"ok": False, "error": "only test runners are allowed here "
                                           "(python -m unittest / pytest, npm test, cargo test, go test)"}
        with d.lock:
            restart = d._reopen()
        if restart or not (d._thread and d._thread.is_alive()):
            d.start()
        return {"ok": True}

    # -- architect -------------------------------------------------------

    def prepare(self, jobs: list[dict[str, Any]], plan_id: str = "") -> dict[str, Any]:
        """Run the architect over each project folder, in the background."""
        from .planner import PlanError, normalise

        if not self.plan or plan_id != self.plan.get("plan_id"):
            return {"ok": False, "error": "this plan is no longer current; make a new plan"}
        try:
            jobs = normalise({"jobs": jobs}, self._agents())
        except PlanError as exc:
            return {"ok": False, "error": str(exc)}
        missing = [j["priority"] for j in jobs if not os.path.isdir(j["project_path"])]
        if missing:
            return {"ok": False, "error": f"set an existing project folder for job(s) {missing}"}
        with self.lock:
            if self.preparing:
                return {"ok": False, "error": "already preparing"}
            self.preparing = True
            prep = {"plan_id": plan_id, "jobs": jobs, "folders": {}, "questions": [],
                    "notes": [], "usage": [], "error": None, "done": False}
            self.prepared = prep

        def work() -> None:
            # Writes only to its own `prep`: a new plan may replace
            # self.prepared while the architect is still running.
            from . import architect

            groups: dict[str, list[dict[str, Any]]] = {}
            for job in jobs:
                groups.setdefault(os.path.normcase(os.path.abspath(job["project_path"])), []).append(job)
            try:
                for folder, members in groups.items():
                    prep["folders"][folder] = "inspecting"
                    metric = ((self.plan or {}).get("compare") or {}).get("metric", "")
                    result = architect.prepare(members[0]["project_path"], members,
                                               self.cfg["architect_model"], port=self.host.port,
                                               metric=metric, sweep=(self.plan or {}).get("sweep"))
                    matched = architect.match_orders(members, result["orders"])
                    for job in members:
                        _apply_order(job, matched.get(job["priority"]) or {})
                    prep["questions"] += result["questions"]
                    if result["notes"]:
                        prep["notes"].append(result["notes"])
                    prep["usage"].append(result["usage"])
                    prep["folders"][folder] = "done"
            except Exception as exc:
                prep["error"] = str(exc)
            finally:
                prep["done"] = True
                self.preparing = False

        threading.Thread(target=work, name="mother-architect", daemon=True).start()
        return {"ok": True}

    def start(self, jobs: list[dict[str, Any]], answers: Optional[dict[str, str]] = None,
              plan_id: str = "") -> dict[str, Any]:
        from .dispatch import Dispatch
        from .planner import PlanError, normalise

        with self.lock:
            if self.dispatch is not None and not self.dispatch.done.is_set():
                return {"ok": False, "error": "a plan is already running"}
            if not self.plan or plan_id != self.plan.get("plan_id"):
                return {"ok": False, "error": "this plan is no longer current; make a new plan"}
            try:
                jobs = normalise({"jobs": jobs}, self._agents())
            except PlanError as exc:
                return {"ok": False, "error": str(exc)}
            missing = [j["priority"] for j in jobs if not os.path.isdir(j["project_path"])]
            if missing:
                return {"ok": False, "error": f"set an existing project folder for job(s) {missing}"}
            # Work orders come from this server's own architect pass for this
            # very plan -- never from the page, and only while the job still
            # points at the folder the architect inspected.
            prepared = self.prepared or {}
            usable = (prepared.get("plan_id") == plan_id and prepared.get("done")
                      and not prepared.get("error"))
            orders = {int(j["priority"]): j for j in prepared.get("jobs") or []} if usable else {}
            answers = answers or {}
            questions = prepared.get("questions") or [] if usable else []
            for job in jobs:
                src = orders.get(job["priority"]) or {}
                same_folder = os.path.normcase(os.path.abspath(src.get("project_path") or "")) == \
                    os.path.normcase(os.path.abspath(job["project_path"]))
                job["work_order"] = str(src.get("work_order") or "") if same_folder else ""
                job["command"] = str(src.get("command") or "") if same_folder else ""
                job["metrics_path"] = str(src.get("metrics_path") or "") if same_folder else ""
                job["metric_key"] = str(src.get("metric_key") or "") if same_folder else ""
                if same_folder and isinstance(src.get("files"), list):
                    job["files"] = [str(f) for f in src["files"]]
                if same_folder:
                    job["command_template"] = str(src.get("command_template") or "")
                    job["metrics_template"] = str(src.get("metrics_template") or "")
                mine = [f"- {q['text']} -> {answers[q['id']]}" for q in questions
                        if q.get("id") in answers and int(q.get("priority") or 0) in (0, job["priority"])]
                job["answers"] = "\n".join(mine)
                if job["work_order"] and job["agent"] == "claude":
                    job["tier"] = self.cfg["worker_model"]      # prepared: a cheap worker executes
                self._tier(job)
            # Agents submit GPU work through QueenBee's UI server; start it if closed.
            from ..app import server_up, start_server

            if not server_up(self.ui_port):
                start_server(self.ui_port, self.host.port)
            self.dispatch = Dispatch(jobs, self.cfg, self.host, self.ui_port)
            self.dispatch.compare = self.plan.get("compare")
            self.dispatch.instruction = str(self.plan.get("instruction") or "")
            if self.plan.get("sweep") and self.dispatch.jobs:
                self.dispatch.start_sweep(self.plan["sweep"], (self.plan.get("compare") or {}).get("goal", "max"))

            def tokens(u):
                return {"tokens": (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0),
                        "cost_usd": u.get("cost_usd")}

            planner = self.plan.get("planner") or {}
            usage = (self.prepared or {}).get("usage") or [] if usable else []
            self.dispatch.costs = {
                "planner": tokens(planner),
                "architect": {"tokens": sum(tokens(u)["tokens"] for u in usage),
                              "cost_usd": round(sum(u.get("cost_usd") or 0 for u in usage), 4)},
            }
            self._wire(self.dispatch)
            self.dispatch.start()
            return {"ok": True}

    def _wire(self, d) -> None:
        d.queen, d.primary, d.queen_name = self.queen, self.primary, self.name
        d.gate = self._gate
        d.state_of = lambda t: self._agent_states().get(t, ("", ""))
        d.branch_planner = self._branch_plan

    def _branch_plan(self, instruction: str, folder: str, opts: Optional[dict[str, str]] = None) -> dict[str, Any]:
        """A branch queen's own plan: the same planner and architect as the
        top queen (or the model the user chose for this branch), over the branch's instruction and folder."""
        opts = opts or {}
        pm = opts.get("queen_model", "")
        pm = "" if pm.startswith("codex") or pm == "__other__" else pm      # the planner step runs on Claude
        out = self._plan_and_prepare(instruction, folder, planner_model=pm,
                                     worker_model=opts.get("worker_model", ""))
        costs = out["costs"]
        return {"jobs": out["jobs"], "costs": {"tokens": costs["planner"]["tokens"] + costs["architect"]["tokens"],
                                               "cost_usd": round((costs["planner"]["cost_usd"] or 0)
                                                                 + (costs["architect"]["cost_usd"] or 0), 4)}}

    def _write_context(self, folder: str, text: str, jobs: list[dict[str, Any]]) -> None:
        """The planner's brief of the project, once, for every worker to read first (Dispatch._context_note). A brief the queen wrote
        with the user in her chat is never overwritten."""
        if not text.strip() or sum(1 for j in jobs if j.get("kind") == "task") < 2 or not os.path.isdir(folder):
            return
        if str(self.roles.get("write_context", "")).lower() in ("no", "off", "false", "0"):   # the brief ablation: no shared brief
            return
        path = os.path.join(folder, ".queenbee", "CONTEXT.md")
        if os.path.exists(path):
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text.strip() + "\n")
        except OSError:
            pass

    def set_worker_models(self, claude: str, codex: str) -> dict[str, Any]:
        """The models the queen gives the parts of a split job: a Claude model for Claude workers, a Codex model for Codex workers."""
        if claude and not MODEL_ID.fullmatch(claude) or codex and not MODEL_ID.fullmatch(codex):
            return {"ok": False, "error": "unknown model"}
        if claude:
            self.roles["worker_model"] = claude
            self.cfg["worker_model"] = claude
        if codex:
            self.roles["codex_worker_model"] = "" if codex == "default" else codex
        return {"ok": True}

    def _plan_and_prepare(self, instruction: str, folder: str, strategy: str = "multi",
                          max_workers: int = 0, layout: str = "team", planner_model: str = "",
                          worker_model: str = "") -> dict[str, Any]:
        """Instruction -> workers ready to start, with no one reviewing: the
        planner splits the package; general tasks go straight to workers; GPU
        runs get the architect's work orders, whose open questions the
        workers settle with the safe default (the queen's log says which)."""
        from . import architect
        from .planner import plan

        codex_queen = self.roles.get("director_agent") == "codex" and not self.roles.get("queen_agent")
        result = plan(instruction, prices=self.cfg.get("prices"), model=self.roles.get("director_model", "") if codex_queen else self.cfg["planner_model"],
                      folder=folder if os.path.isdir(folder) else "", agents=self._agents(), strategy=strategy,
                      max_workers=max_workers,
                      layout="peers" if layout in ("peers", "branches") else "team",
                      planner_agent="codex" if codex_queen else "claude")        # a Codex queen plans with Codex: no Claude tokens
        jobs = result["jobs"]
        planner = result.get("planner") or {}
        usage: list[dict[str, Any]] = []
        groups: dict[str, list[dict[str, Any]]] = {}
        for job in jobs:
            if not os.path.isdir(job["project_path"]):
                job["project_path"] = folder
            if job.get("kind") == "task" and not result.get("sweep"):
                job["work_order"] = job["goal"]          # the task, in the planner's words
                if job["agent"] == "claude":
                    # A package kept whole is one strong worker's: the cheap
                    # model is for the parts of a package split up.
                    # A package kept whole, or the senior of a split one, is
                    # the strong model's; the juniors get the cheap one.
                    alone = sum(1 for j in jobs if j.get("kind") == "task") == 1 or bool(job.get("senior"))
                    job["alone"] = alone
                    # The sweet spot: the least capable model that succeeds.
                    # A hard part goes to the strong model from the start.
                    strong = alone or job.get("effort") == "high"
                    job["alone"] = strong
                    advised = {"low": self.cfg.get("worker_model") or "haiku", "medium": "sonnet", "high": "opus"}
                    if self._advising() and not alone:
                        job["tier"] = advised.get(job.get("effort"), "sonnet")      # the queen's own advice
                        continue
                    job["tier"] = ((self.cfg.get("strong_model") or (self.cfg.get("escalation") or ["sonnet"])[0]) if strong
                                   else self.cfg.get("worker_model") or "haiku")
            else:
                job["kind"] = "gpu"
                groups.setdefault(os.path.normcase(os.path.abspath(job["project_path"])), []).append(job)
            self._tier(job)
        codex_tier = self.roles.get("codex_worker_model", "")
        if codex_tier and sum(1 for j in jobs if j.get("kind") == "task") >= 2:       # the models the user picked for the workers
            have = set(_codex_models())
            ladder = {"low": "gpt-5.6-luna", "medium": "gpt-5.6-terra", "high": "gpt-5.6-sol"}      # the strength each part needs
            for job in jobs:
                if job.get("kind") == "task" and job.get("agent") == "codex":
                    if codex_tier == "auto":
                        pick = ladder.get(job.get("effort") or "medium", "gpt-5.6-terra")
                        job["tier"] = pick if pick in have else None
                    else:
                        job["tier"] = codex_tier
        if worker_model not in ("", "auto") and MODEL_ID.fullmatch(worker_model):   # the user's choice beats the queen's advice
            for job in jobs:
                if job.get("kind") == "task" and job.get("agent") == "claude":
                    job["tier"] = worker_model
        # Map the project once, for every worker (see Dispatch._map_for).
        from .architect import _tree

        tree = _tree(folder, depth=4, limit=150) if os.path.isdir(folder) else []
        for job in jobs:
            if job.get("kind") == "task":
                job["map"] = tree
        questions: list[str] = []
        metric = (result.get("compare") or {}).get("metric", "")
        for members in groups.values():
            prepared = architect.prepare(members[0]["project_path"], members, self.cfg["architect_model"],
                                         port=self.host.port, metric=metric, sweep=result.get("sweep"))
            matched = architect.match_orders(members, prepared["orders"])
            for job in members:
                _apply_order(job, matched.get(job["priority"]) or {})
                if job["work_order"] and job["agent"] == "claude":
                    job["tier"] = self.cfg["worker_model"]
                self._tier(job)
                if prepared["questions"]:
                    job["answers"] = "none given; where the work order leaves a choice, take the safe default"
            questions += [q.get("text", "") for q in prepared["questions"]]
            usage.append(prepared["usage"])

        def tokens(u):
            return (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)

        return {"mode": result.get("mode"), "jobs": jobs, "compare": result.get("compare"), "sweep": result.get("sweep"),
                "verify": result.get("verify") or "", "context": result.get("context") or "",
                "notes": result.get("notes", ""), "questions": questions,
                "costs": {"planner": {"tokens": tokens(planner), "cost_usd": planner.get("cost_usd")},
                          "architect": {"tokens": sum(tokens(u) for u in usage),
                                        "cost_usd": round(sum(u.get("cost_usd") or 0 for u in usage), 4)}}}

    def command(self, instruction: str, folder: str, strategy: str = "multi",
                max_workers: int = 0, layout: str = "team", qa: bool = False) -> dict[str, Any]:
        """The user hands this queen a work package: it plans, prepares and
        starts its workers on its own. Given to a queen that already has
        workers, the new ones join them."""
        from .dispatch import Dispatch

        from . import hints

        instruction, folder = instruction.strip(), folder.strip()
        if not instruction:
            return {"ok": False, "error": "write what this queen should get done"}
        said = hints.parse(instruction)                      # the user's own words: a cap on workers, agents, what "done" means
        # The cap the user set in the queen's Limits is also the size of the plan: a bigger plan only waits at the limit and
        # every extra worker is tokens spent on the same project again.
        max_workers = max_workers or said["max_workers"] or int(self.limits.get("workers") or 0)
        if layout == "team" and hints.wants_peers(folder):          # QUEEN.md says: all start at once, no senior
            layout = "peers"
        if not os.path.isdir(folder):
            return {"ok": False, "error": f"folder does not exist: {folder or '(empty)'}"}
        usd, usd_cap = self._cost.get("cost_usd"), self.limits.get("budget_usd")
        if usd_cap is not None and usd is not None and usd >= usd_cap:
            return {"ok": False, "error": f"{self.name} has spent its budget (${usd:.2f} of ${usd_cap:g}); "
                                          "raise it in the queen's limits"}
        spent, budget = self._cost.get("tokens"), self.limits.get("budget_tokens")
        if budget is not None and spent is not None and spent >= budget:
            return {"ok": False, "error": f"{self.name} has used its token budget ({spent / 1e6:.2f}M of {budget / 1e6:g}M); "
                                          "raise it in the queen's limits"}
        with self.lock:
            if self.commanding or self.planning or self.preparing:
                return {"ok": False, "error": f"{self.name} is still planning the last command"}
            self.commanding, self.command_error = "splitting your instruction into worker bees", None
        self.commands.append({"ts": time.time(), "text": instruction})
        ready = [a for a in said["agents"] if _agent_ready(a)]
        self._hint_agents = tuple(ready) if len(ready) >= 1 and (len(ready) > 1 or said["agents"]) else ()
        if said["check"]:
            self.commands.append({"ts": time.time(), "queen": f"done means: {said['check']}"})

        def work() -> None:
            try:
                out = self._plan_and_prepare(instruction, folder, strategy, max_workers, layout)
                self._write_context(folder, out.get("context") or "", out["jobs"])
                if out.get("mode"):
                    self.commands.append({"ts": time.time(), "queen": f"mode {out['mode']}: "
                                          f"{len(out['jobs'])} worker(s), planner ${(out['costs']['planner']['cost_usd'] or 0):.2f}"})
                for q in out["questions"]:
                    self.commands.append({"ts": time.time(), "queen": f"decided on its own: {q[:200]}"})
                self.commanding = "starting its workers"
                from ..app import server_up, start_server

                if not server_up(self.ui_port):
                    start_server(self.ui_port, self.host.port)
                with self.lock:
                    d = self.dispatch
                    if d is not None and d.jobs and not (out["sweep"] and d.done.is_set()):
                        pass
                    else:
                        d = Dispatch(out["jobs"], self.cfg, self.host, self.ui_port)
                        d.compare = out["compare"]
                        d.instruction = instruction
                        d.set_verify(out.get("verify") or "", folder)
                        if said["check"]:
                            d.set_verify(said["check"], folder, trusted=True)      # typed by the user: their own definition of done
                        if qa and sum(1 for j in out["jobs"] if j.get("kind") == "task") >= 2:
                            d.qa_goal = d.QA_GOAL.split("{instruction}")[0]      # on; the text is built when it runs
                        if layout == "branches":
                            d.enable_branches(folder)
                        d.costs = dict(out["costs"])
                        if out["sweep"] and d.jobs:
                            d.start_sweep(out["sweep"], (out["compare"] or {}).get("goal", "max"))
                        self._wire(d)
                        self.dispatch = d
                        self.plan = {"instruction": instruction, "jobs": out["jobs"], "compare": out["compare"],
                                     "notes": out["notes"], "plan_id": f"plan-{int(time.time() * 1000)}"}
                        d.start()
                        out = None
                if out is not None:
                    d.set_verify(out.get("verify") or "", folder)
                    d.graft(out["jobs"], out["costs"], instruction)
                n = len(self.dispatch.jobs)
                self.commands.append({"ts": time.time(), "queen": f"working on it: {n} worker bee{'s' if n != 1 else ''} in its hive"})
            except Exception as exc:
                self.command_error = str(exc)
                self.commands.append({"ts": time.time(), "queen": f"could not plan it: {exc}"})
            finally:
                self.commanding = None
                self._hint_agents = ()

        threading.Thread(target=work, name=f"queen-{self.queen}-command", daemon=True).start()
        return {"ok": True}

    # -- approver --------------------------------------------------------

    def _approve_loop(self) -> None:
        last = time.time()
        while True:
            time.sleep(3)
            now = time.time()
            try:
                self._track(now - last)
            except Exception:
                log.exception("tracking pass failed")
            last = now
            try:
                self._approve_once()
            except Exception:
                log.exception("approver pass failed")

    GPU_BUSY_PCT = 20.0

    def _track(self, dt: float) -> None:
        """GPU busy and idle time while a plan runs; the report once it ends."""
        d = self.dispatch
        if not d:
            return
        if not d.done.is_set():
            gpus = (self._machine().get("gpus") or [])
            util = gpus[0].get("util_pct") if gpus else None
            if util is not None and dt < 30:
                d.gpu["busy_s" if util >= self.GPU_BUSY_PCT else "idle_s"] += dt
            return
        if d.report:
            return
        from . import ledger, report, results

        jobs = d.view()
        d.finished_at = d.finished_at or time.time()
        costs = dict(d.costs)
        folders = {j["project_path"] for j in jobs if j.get("project_path")}
        workers = [ledger.worker_costs(f, d.started_at) for f in folders]
        priced = [w["cost_usd"] for w in workers if w["cost_usd"] is not None]
        costs["workers"] = {"tokens": sum(w["tokens"] for w in workers),
                            "cost_usd": round(sum(priced), 4) if priced or not workers else None}
        text = report.build(jobs, results.compare(jobs, d.compare), instruction=d.instruction,
                            approvals=d.approvals,
                            gpu=d.gpu, costs=costs, started_at=d.started_at, finished_at=d.finished_at,
                            sweep=d.sweep)
        home = sorted(folders)[0] if folders else ""
        # Where the tokens went: the queen's own roles and every worker by phase (exploring before its first write, building after)
        try:
            from . import economy
            roles = {"planner": (costs.get("planner") or {}).get("tokens"), "architect": (costs.get("architect") or {}).get("tokens"),
                     "look-ins": getattr(self.director, "tokens", 0) if self.director is not None else 0}
            tables = [economy.team_table(f, d.started_at, queen=roles) for f in sorted(folders)]
            if tables and any(t["workers"] for t in tables):
                head = ["", "## Where the tokens went", "",
                        "Exploring is what a worker spends before its first write (reading the project, the brief, the board); "
                        "building is the rest. A shared brief should shrink the first column.", ""]
                text += "\n".join(head) + "\n\n".join(economy.render(t) for t in tables) + "\n"
        except Exception:                                   # the report is written whatever the transcripts look like
            log.debug("economy table failed", exc_info=True)
        path = os.path.join(home, ".queenbee", "report.md") if home else ""
        if path:
            report.write(path, text)
        d.report = path or "(no project folder)"
        d.save()
        log.info("mother: report written to %s", d.report)

    DECLINE = ("Your queen here: no -- {why}; that is outside what you may do. Do the work without it "
               "(no installing, downloading, deleting or leaving the project folder; run tests with "
               "python -m unittest) and carry on.")

    def _approve_once(self) -> None:
        from . import approver

        if not self.dispatch or self.dispatch.done.is_set():
            return
        states = self._agent_states()
        for job in self.dispatch.view():
            term = job.get("terminal")
            if job.get("status") != "agent working" or not term or job.get("detached"):
                continue
            # Only jobs the architect prepared: without a work order there is
            # nothing to judge a question against, so the user answers.
            if not job.get("work_order"):
                continue
            state, _ = states.get(term, ("", ""))
            q = self._question(term)
            # An agent that has hit its provider's limit will not move again;
            # stop the job now rather than wait for it.
            limit = next((l for l in self._screen_tail if approver.LIMIT.search(l)), None)
            if limit:
                self.dispatch.fail(job["priority"], f"the agent hit its provider's limit: {limit[:200]}")
                continue
            if state in ("idle", "done"):
                continue
            if not q:
                continue
            # QueenBee reads Claude's state from its transcript, where a tool call
            # waiting for approval looks like one still running ("running
            # PowerShell"). The screen is what counts: a question with choices
            # at its bottom is waiting, whatever the state says.
            if state not in ("waiting", "blocked") and not q.get("options"):
                continue
            # Claude asks every command with the same words ("Do you want to
            # proceed?"); what is being asked is the command above it.
            key = (term, q["text"], tuple(q.get("context") or ()))
            prev = self._answered.get(key)
            if prev is not None and prev.get("yours") and not self.user_answers:
                # Left to the user while they answered, and they have handed the answering back to the queen: the
                # question still on screen is hers now, or the worker waits on it until somebody looks.
                prev = None
            if prev is not None:
                # An answer that did not land: keys sent while the agent's
                # screen was still starting up are lost, and the same question
                # sat there for eight minutes. Still on screen 8 s after an
                # approval, it is answered again (three times at most).
                if (prev.get("decision") == "approved" and time.time() - prev["ts"] > 8
                        and prev.get("retries", 0) < 3):
                    prev["retries"] = prev.get("retries", 0) + 1
                    prev["ts"] = time.time()
                    n = approver.approve_option(q)
                    self.host._call("terminal_write", term, approver.keys_for_option(n, int(q.get("cursor") or 1)))
                    _SNAP["at"] = 0.0
                    log.info("mother approver: P%s %s: answered again (%d)", job["priority"], term, prev["retries"])
                continue
            verdict, why = approver.classify(q, job["project_path"], job.get("command") or "")
            if self.manual_approvals and self.manual_delay_s > 0:
                # A recorded run with a person at the desk: the question sits for `manual_delay_s` seconds,
                # then is answered as the queen would (yes inside the project, no to anything risky).
                first = self._pending.setdefault(key, time.time())
                if time.time() - first < self.manual_delay_s:
                    continue
            elif self.manual_approvals:
                verdict, why = "manual", "a run without the queen: every question is yours"
            elif self.user_answers:
                verdict, why = "manual", "you answer this queen's workers"
            if verdict != "approve" and not (self.manual_approvals or self.user_answers):
                # Right after an answer the old question can linger on screen
                # for a moment; only one that is still there on the next pass
                # is handed to the user (or to the answering model).
                first = self._pending.setdefault(key, time.time())
                if time.time() - first < 5:
                    continue
            entry = {"ts": time.time(), "priority": job["priority"], "terminal": term,
                     "question": q["text"][:200], "why": why}
            if self.manual_approvals and self.manual_delay_s > 0:
                entry["manual"], entry["delay"] = True, self.manual_delay_s
            if verdict == "manual":
                entry["decision"] = "left to you"
                if self.user_answers and not self.manual_approvals:
                    entry["yours"] = True
            elif verdict == "approve":
                n = approver.approve_option(q)
                self.host._call("terminal_write", term, approver.keys_for_option(n, int(q.get("cursor") or 1)))
                entry["decision"] = "approved"
            elif verdict == "choose":
                n = approver.choose(q, job, self.cfg["answer_model"])
                if n is None:
                    entry["decision"] = "left to you"
                else:
                    self.host._call("terminal_write", term, approver.keys_for_option(n))
                    option = next((o["text"] for o in q["options"] if int(o["n"]) == n), str(n))
                    entry["decision"] = f"chose {n}. {option[:80]}"
            else:
                # Outside what a worker may do (delete, download, install,
                # another folder): the queen, as the boss, says no herself and
                # tells the worker to do without -- nothing risky runs and no
                # one waits. Only a question with no "no" to give is the user's.
                no = next((o for o in q.get("options") or []
                           if re.match(r"(?i)\s*(no\b|deny|reject|don't|do not)", o.get("text", ""))), None)
                if no is not None:
                    self.host._call("terminal_write", term,
                                    approver.keys_for_option(int(no["n"]), int(q.get("cursor") or 1)))
                    time.sleep(1.5)
                    self.host._call("terminal_write", term, self.DECLINE.format(why=why))
                    time.sleep(0.3)
                    self.host._call("terminal_write", term, "\r")
                    # Claude can hold a typed line back with "review and press Enter to send" (a character it removed from the
                    # text): one Enter more, or the worker sits on an unsent message for ever. On an empty box it does nothing.
                    time.sleep(1.2)
                    self.host._call("terminal_write", term, "\r")
                    entry["decision"] = "declined by the queen"
                    self.dispatch._event(next(j for j in self.dispatch.jobs if j["priority"] == job["priority"]),
                                         f"the queen said no ({why}) and told it to do without")
                else:
                    entry["decision"] = "left to you"
            self._answered[key] = entry
            self.approvals.append(entry)
            # Kept with the plan too, so a restarted Mother still reports them.
            self.dispatch.approvals.append(entry)
            _SNAP["at"] = 0.0                 # the screen changes after an answer
            log.info("mother approver: P%s %s: %s (%s)", job["priority"], term,
                     entry["decision"], why)

    def _agent_states(self) -> dict[str, tuple[str, str]]:
        """terminal id -> (state, reason) as QueenBee reads it from the agent's
        screen. That is where "waiting for your approval" is known; cached
        briefly, and shared by every queen, because the snapshot takes QueenBee
        seconds to build. Only a first (or forced, `_SNAP["at"] = 0`) read waits
        for it; otherwise the last snapshot is answered at once and a new one is
        fetched behind the caller, so the window's poll never waits seconds."""
        with _SNAP_LOCK:
            wait = not _SNAP["at"]
            start = not wait and time.time() - _SNAP["at"] >= 3 and not _SNAP["refreshing"]
            if start:
                _SNAP["refreshing"] = True
        if wait:
            _refresh_snapshot(self.ui_port)
        elif start:
            threading.Thread(target=_refresh_snapshot, args=(self.ui_port,), name="snapshot-refresh", daemon=True).start()
        with _SNAP_LOCK:
            self._states, self._paths = _SNAP["states"], _SNAP["paths"]
            self._usage, self._system = _SNAP["usage"], _SNAP["system"]
            return self._states

    def _report_view(self) -> Optional[dict[str, str]]:
        """The finished plan's report, for the window."""
        path = self.dispatch and self.dispatch.report
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                return {"path": path, "text": fh.read()[:20000]}
        except OSError:
            return None

    def _machine(self) -> dict[str, Any]:
        """CPU, RAM and GPU figures for the hardware strip: QueenBee's own
        measurement when its UI server runs, this server's otherwise -- the
        strip must never go blank."""
        self._agent_states()                       # refreshes the snapshot (cached 3 s)
        s = self._system or local_machine()
        gpus = [{k: g.get(k) for k in ("index", "name", "util_pct", "mem_used_mb", "mem_total_mb",
                                        "temp_c", "power_w")} for g in s.get("gpus") or []]
        return {"cpu_pct": s.get("cpu_pct"), "cpu_cores": s.get("cpu_cores"),
                "ram_used_mb": s.get("ram_used_mb"), "ram_total_mb": s.get("ram_total_mb"),
                "gpus": gpus}

    def conversation(self, terminal: str) -> dict[str, Any]:
        """The chat behind an agent terminal, read from the agent's own
        transcript through QueenBee. Read-only: showing a terminal a second time
        would resize it and could replay terminal replies into the agent."""
        import urllib.parse
        import urllib.request

        _SNAP["at"] = 0.0                       # a fresh lookup: the path may be new
        self._agent_states()
        state = self._states.get(terminal, ("", ""))
        path = self._paths.get(terminal)
        if not path:
            return {"ok": True, "messages": [], "state": state[0], "reason": state[1],
                    "note": "The agent has not written its transcript yet."}
        from . import transcript

        try:
            items = transcript.read(path)
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"cannot read the transcript: {exc}"}
        return {"ok": True, "items": items, "state": state[0], "reason": state[1]}

    def job_screen(self, priority: int) -> dict[str, Any]:
        """The saved screen of a worker whose session has been closed: {"cols", "rows", "data"}."""
        job = next((j for j in (self.dispatch.view() if self.dispatch else []) if j["priority"] == priority), None)
        if job is None:
            return {"ok": False, "error": "no such worker"}
        path = os.path.join(job.get("project_path") or "", ".queenbee", "screens", f"W{priority}.json")
        try:
            with open(path, encoding="utf-8") as fh:
                return {"ok": True, **json.load(fh)}
        except (OSError, ValueError):
            return {"ok": True, "data": ""}

    def _finding_folders(self) -> list[str]:
        """The folders this queen's workers keep findings in (a hive's root, else the project folder)."""
        d = self.dispatch
        return sorted({j.get("hive_root") or j["project_path"] for j in (d.view() if d else [])
                       if j.get("kind") == "task" and j.get("project_path")})

    def finding_records(self) -> dict[str, Any]:
        """Every finding kept in this queen's folders, each with its state: current, out of date (a file it names changed),
        in conflict with a failed check, retired or superseded. Checked against the files now, as a brief would be."""
        from . import board

        out = []
        for folder in self._finding_folders():
            if not os.path.isfile(board.records_path(folder)) and not os.path.isfile(board.findings_path(folder)):
                continue
            board.refresh_findings(folder)
            out.append({"folder": folder, "records": board.load_findings(folder)})
        return {"ok": True, "folders": out}

    def settle_finding(self, folder: str, finding_id: str, action: str) -> dict[str, Any]:
        """The user confirms a finding (it is current again, its files taken as they are now) or retires it."""
        from . import board

        known = {os.path.normcase(os.path.abspath(f)): f for f in self._finding_folders()}
        folder = known.get(os.path.normcase(os.path.abspath(folder or "")))
        if folder is None:
            return {"ok": False, "error": "that folder is not this queen's"}
        if action not in ("validate", "retire"):
            return {"ok": False, "error": "validate or retire"}
        if not board.settle_finding(folder, finding_id, action, by="you"):
            return {"ok": False, "error": f"no finding {finding_id} there"}
        return {"ok": True}

    def changes(self) -> dict[str, Any]:
        """What the workers changed since the plan started, file by file as a diff, with the Claude workers that wrote
        each file (a Codex worker's edits are shown but not attributed)."""
        from . import changes, ledger

        d = self.dispatch
        jobs = [j for j in (d.view() if d else []) if j.get("kind") == "task" and j.get("project_path")]
        folders = sorted({j["project_path"] for j in jobs})
        if not folders:
            return {"ok": True, "folders": []}
        out = []
        for root in folders:
            diff = changes.compare(root)
            who: dict[str, list[str]] = {}
            for j in jobs:
                if j["project_path"] != root or j.get("agent") != "claude":
                    continue
                path = ledger.find_worker_transcript(root, j["priority"], float(d.started_at or 0))
                for rel in changes.touched_by(path, root) if path else ():
                    who.setdefault(rel, []).append(f"W{j['priority']}")
            for row in diff["files"]:
                row["workers"] = who.get(row["path"], [])
            out.append({"root": root, **diff})
        return {"ok": True, "folders": out}

    @property
    def dispatch(self) -> Any:
        return self._dispatch

    @dispatch.setter
    def dispatch(self, d: Any) -> None:
        # Whoever answers the prompts decides how the workers start, whichever way this queen's dispatch was made.
        self._dispatch = d
        if d is not None:
            d.asks_user = self.user_answers

    def resume(self, go: bool) -> dict[str, Any]:
        """The user's answer for a team that was at work when QueenBee closed: carry on where it left off, or stop it
        (its work stays in the folder)."""
        d = self.dispatch
        if d is None or not d.held:
            return {"ok": False, "error": "nothing is waiting to continue"}
        if not go:
            for j in d.jobs:
                if j["status"] not in d.ENDED:
                    d.kill(j["priority"])
            d.done.set()
        else:
            with d.lock:
                for j in d.jobs:
                    # Its terminal closed with the window's host: a new agent takes the job, the earlier work in the folder.
                    if j["status"] == "agent working" and not j.get("workload"):
                        j.update(status="queued", terminal=None, error=None, attention=None, ran_before=True)
        d.held = False
        d.save()
        return {"ok": True}

    def set_answers(self, mode: str) -> dict[str, Any]:
        """Who answers the workers' prompts: "queen" (her rule) or "me" (the user, in each worker's terminal; the queen still
        watches, keeps the board, steers and checks). Workers that start from now on start that way; the Claude workers
        already running follow it at once through their hook."""
        if mode not in ("queen", "me"):
            return {"ok": False, "error": "queen or me"}
        self.user_answers = mode == "me"
        if self._dispatch is not None:
            self._dispatch.asks_user = self.user_answers
            self._dispatch._event_all(f"the workers' prompts are answered by {'you' if self.user_answers else 'the queen'}")
        return {"ok": True, "answers": mode}

    def hook_decision(self, job_n: int, cwd: str, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        """A worker's tool call, reported by its Claude Code hook before it runs: the queen answers by her rules and records the
        decision with the others. 'ask' leaves the usual prompt, which her screen reader then handles as before."""
        from . import approver

        d = self.dispatch
        job = next((j for j in (d.jobs if d else []) if j.get("priority") == job_n), None)
        if job is None or d is None:
            return {"ok": False, "decision": "ask", "reason": "no such worker"}
        if self.manual_approvals:
            return {"ok": True, "decision": "ask", "reason": "this agent's questions are the user's"}
        if self.user_answers:
            # "force": the hook says "ask" out loud, which makes Claude Code prompt even for what the worker's start flags
            # pre-approved (a worker started while the queen answered); a silent "ask" would let those run unasked.
            return {"ok": True, "decision": "ask", "force": True, "reason": "you answer this queen's workers (QueenBee)"}
        project = job.get("project_path") or cwd
        shown = tool_input.get("command") or tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        call = f"{tool_name}: {shown}"
        if call in (job.get("allowed_calls") or []):
            verdict, why = "allow", "you allowed this after the queen had said no"
        else:
            verdict, why = approver.classify_tool(tool_name, tool_input, project, job.get("command") or "")
            if verdict == "allow" and tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                verdict, why = self._write_conflict(job, str(shown), why)
        entry = {"ts": time.time(), "priority": job["priority"], "terminal": job.get("terminal"), "source": "hook",
                 "question": f"{tool_name}: {str(shown)[:160]}", "why": why,
                 "decision": {"allow": "approved", "deny": "declined by the queen"}.get(verdict, "left to you")}
        if verdict in ("allow", "deny"):
            self.approvals.append(entry)
            d.approvals.append(entry)
            if verdict == "deny" and why == approver.NO_WAITING:
                d._event(job, "was told not to wait for the board and to carry on")   # advice: no mark for the user to settle
            elif verdict == "deny":
                d._event(job, f"the queen said no ({why})")
                # Shown on the worker's node until the user allows it or agrees with her: a wrong "no" must not pass unseen.
                refused = [r for r in job.get("refused") or [] if r["call"] != call]
                job["refused"] = (refused + [{"call": call, "question": entry["question"], "why": why, "ts": entry["ts"]}])[-5:]
        return {"ok": True, "decision": verdict, "reason": why}

    ALLOWED_AFTER_NO = ("The user has allowed what the queen refused: {q}. If you still need it, run it again exactly as "
                        "before; it will be allowed.")

    def settle_refusal(self, job_n: int, call: str, allow: bool) -> dict[str, Any]:
        """The user's answer to a refusal on a worker's node: allow that exact call from now on and tell the worker, or agree
        with the queen and clear the mark."""
        d = self.dispatch
        job = next((j for j in (d.jobs if d else []) if j.get("priority") == job_n), None)
        if job is None or d is None:
            return {"ok": False, "error": "no such worker"}
        hit = next((r for r in job.get("refused") or [] if r["call"] == call), None)
        if hit is None:
            return {"ok": False, "error": "that refusal is no longer open"}
        job["refused"] = [r for r in job["refused"] if r is not hit]
        if not allow:
            d._event(job, f"you agreed with the queen's no to {hit['question']}")
            return {"ok": True}
        job.setdefault("allowed_calls", []).append(call)
        for entry in d.approvals:
            if entry.get("priority") == job_n and entry.get("question") == hit["question"] and entry.get("ts") == hit["ts"]:
                entry["decision"] = "declined by the queen, then allowed by you"
        d._event(job, f"you allowed what the queen refused: {hit['question']}")
        if not job.get("terminal") or job.get("status") != "agent working":
            return {"ok": True, "told": False}
        try:
            self.host._call("terminal_write", job["terminal"], self.ALLOWED_AFTER_NO.format(q=hit["question"]))
            time.sleep(0.3)
            self.host._call("terminal_write", job["terminal"], "\r")
        except HostUnavailable:
            return {"ok": True, "told": False}
        return {"ok": True, "told": True}

    def _write_conflict(self, job: dict[str, Any], path: str, why: str) -> tuple[str, str]:
        """At the moment a worker is about to write a file (its hook asked): a file that the plan gave to another worker is
        refused and the matter sent to the hive board; a file that another worker has already written in this run is allowed,
        but both are told once, so they agree before the next change. The delivery check still decides the whole."""
        from . import board

        d = self.dispatch
        project = job.get("project_path") or ""
        try:
            rel = os.path.relpath(path, project).replace(os.sep, "/")
        except ValueError:
            return "allow", why
        if rel.startswith(".."):
            return "allow", why
        me = job["priority"]
        others = [j for j in d.jobs if j.get("kind") == "task" and j["priority"] != me and j["status"] not in d.ENDED
                  and os.path.normcase(os.path.abspath(j.get("project_path") or "")) == os.path.normcase(os.path.abspath(project))]
        mine = {str(f).replace(os.sep, "/").lstrip("./") for f in (job.get("files") or [])}
        for other in others:
            theirs = {str(f).replace(os.sep, "/").lstrip("./") for f in (other.get("files") or [])}
            if rel in theirs and rel not in mine:
                note = (f"{rel} is W{other['priority']}'s file in the plan. W{me} wanted to edit it; W{me}, say on the board what "
                        f"you need changed and let W{other['priority']} make the change.")
                try:
                    board.post(os.path.join(project, ".queenbee"), "queen", [f"W{me}", f"W{other['priority']}"], note)
                except OSError:
                    pass
                d._event(job, f"the queen refused an edit of {rel}: it is W{other['priority']}'s file")
                return "deny", f"{rel} belongs to W{other['priority']} in the plan: ask on the hive board instead of editing it"
        writes = d.__dict__.setdefault("_writes", {})
        before = writes.setdefault(rel, set())
        clash = sorted(p for p in before if p != me and any(o["priority"] == p for o in others))
        if clash and (rel, me) not in d.__dict__.setdefault("_clash_told", set()):
            d.__dict__["_clash_told"].add((rel, me))
            who = ", ".join(f"W{p}" for p in clash)
            note = f"W{me} and {who} are both editing {rel}. Agree on the board who changes what before the next edit."
            try:
                board.post(os.path.join(project, ".queenbee"), "queen", [f"W{me}", *[f"W{p}" for p in clash]], note)
            except OSError:
                pass
            d._event(job, f"W{me} edits {rel}, which {who} also changed: both told on the board")
        before.add(me)
        return "allow", why

    def completion_facts(self) -> str:
        """Everything the completion report may say, as plain lines: nothing is added to it by the model."""
        from . import changes

        d = self.dispatch
        jobs = [j for j in (d.view() if d else []) if j.get("kind") == "task"]
        now = time.time()
        started = float(d.started_at or 0) if d else 0
        # A free worker (one agent alone) has no instruction and no queen's description: its task is the job's own goal.
        goals = " / ".join(str(j.get("goal") or "") for j in (d.jobs if d else []) if j.get("kind") == "task" and j.get("goal"))
        lines = [f"Task: {((d.instruction if d else '') or self.description or goals or '')[:600]}",
                 f"Time: {round(((self._finished_at(jobs) or now) - started) / 60, 1)} minutes",
                 f"Tokens: {(self._cost or {}).get('tokens', 'unknown')}"]
        answered = sum(1 for a in (d.approvals if d else []) if a.get("decision") == "approved")
        left = sum(1 for a in (d.approvals if d else []) if a.get("decision") == "left to you")
        lines.append(f"Permission questions: {answered} answered by the queen, {left} left to the user")
        for j in jobs:
            rep = j.get("report") or {}
            files = ", ".join((rep.get("changed_files") or [])[:12])
            lines.append(f"Worker W{j['priority']} {j.get('title', '')[:60]} ({j.get('agent')} {j.get('tier') or ''}): "
                         f"{j.get('status')}" + (f"; error: {j['error']}" if j.get("error") else "")
                         + (f"; reported: {str(rep.get('notes') or '')[:500]}" if rep.get("notes") else "")
                         + (f"; files: {files}" if files else ""))
            for r in (j.get("accept_results") or [])[:4]:
                lines.append(f"  acceptance check {r.get('check', '')[:80]}: {'passed' if r.get('ok') else 'FAILED'}")
        v = (d.verify if d else None) or {}
        if v.get("command"):
            lines.append(f"Delivery check (run by the queen) `{v['command']}`: {v.get('state')}"
                         + (f"; output: {str(v.get('log') or '')[-600:]}" if v.get("state") not in ("passed", None) else ""))
        else:
            lines.append("Delivery check: none was set")
        roots = sorted({j["project_path"] for j in jobs if j.get("project_path")})
        for root in roots[:1]:
            c = changes.compare(root)
            lines.append(f"Files changed in {root} since the plan started: {c.get('total', 0)}"
                         + (": " + ", ".join(f['path'] for f in c.get("files", [])[:25]) if c.get("files") else ""))
        return "\n".join(lines)

    def result_card(self) -> str:
        """The run's result as a few lines of Markdown to paste into a pull request or a post. Facts only, nothing from
        the model: the queen's check, the prompts a person had to answer, the workers, the integration fixes, the time
        and the tokens."""
        d = self.dispatch
        jobs = [j for j in (d.view() if d else []) if j.get("kind") == "task"]
        if d is None or not jobs:
            return ""
        started = float(d.started_at or 0)
        secs = max(0, int((self._finished_at(jobs) or time.time()) - started)) if started else 0
        v = d.verify or {}
        fixes = int(v.get("rounds") or 0)
        workers = jobs[:-fixes] if 0 < fixes < len(jobs) else jobs        # the integration-fix workers come last
        names = {"claude": "Claude Code", "codex": "Codex"}
        who = [names.get(j.get("agent") or "", (j.get("agent") or "agent").title()) + (f" ({j['tier']})" if j.get("tier") else "")
               for j in workers]
        left = sum(1 for a in d.approvals if a.get("decision") == "left to you")
        tokens = (self._cost or {}).get("tokens")
        if v.get("command"):
            check = f"`{v['command']}` " + str(v.get("state") or "not run")
        else:
            check = "none was set"
        task = (((d.instruction or self.description or "").strip().splitlines() or [""])[0])[:100]
        lines = ["**QueenBee run" + (f": {task}" if task else "") + "**",
                 f"- Check: {check}",
                 f"- Prompts to a person: {left}",
                 f"- Workers: {', '.join(who)}",
                 f"- Integration fixes: {fixes}",
                 f"- Time: {secs // 60} min {secs % 60} s",
                 f"- Tokens: {tokens / 1e6:.2f} M" if isinstance(tokens, (int, float)) and tokens else "- Tokens: not counted",
                 "",
                 "Built with [QueenBee](https://github.com/QueenBee-SI/QueenBee)"]
        return "\n".join(lines)

    def _completion_view(self) -> Optional[dict[str, Any]]:
        """The completion report: written once, in the background, when the team has stopped; kept in the project."""
        d = self.dispatch
        plan = float(d.started_at or 0) if d is not None else 0.0
        if self.completion is not None and self.completion.get("plan") != plan:
            self.completion = None                     # a report belongs to the plan it was written for
        if d is None or self.completion is not None or not d.jobs:
            return self.completion
        jobs = d.view()
        if not self._finished_at(jobs):
            return None
        folder = next((j["project_path"] for j in jobs if j.get("kind") == "task" and j.get("project_path")), "")
        path = os.path.join(folder, ".queenbee", "completion.md") if folder else ""
        try:
            if path and os.path.getmtime(path) >= float(d.started_at or 0):
                with open(path, encoding="utf-8") as fh:
                    self.completion = {"state": "done", "text": fh.read(), "at": os.path.getmtime(path), "plan": plan}
                return self.completion
        except OSError:
            pass
        self.completion = {"state": "writing", "plan": plan}

        def write() -> None:
            from . import chat

            out = chat.complete(self.completion_facts(), self.roles.get("strong_model") or self.cfg.get("strong_model") or "sonnet",
                                agent=self.roles.get("director_agent") or "claude", codex_model=self.roles.get("director_model", ""))
            if not out.get("ok"):
                self.completion = {"state": "failed", "error": out.get("error", ""), "plan": plan}
                return
            self.completion = {"state": "done", "text": out["text"].strip(), "at": time.time(), "tokens": out.get("tokens"),
                               "plan": plan}
            if path:
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w", encoding="utf-8", newline="\n") as fh:
                        fh.write(self.completion["text"])
                except OSError:
                    pass

        threading.Thread(target=write, name=f"queen-{self.queen}-completion", daemon=True).start()
        return self.completion

    def _set_queen_agent(self, agent: str) -> dict[str, Any]:
        """Another agent (Gemini, Kimi, OpenCode, Cursor, Mistral Vibe, Grok) as the queen in her terminal. Her periodic
        look-ins and the planner keep running on Claude: those need a non-interactive mode only Claude and Codex have here."""
        from ..agents.catalog import binary_for
        from . import queen_term

        if agent not in queen_term.TYPED_FIRST and agent not in ("gemini", "cursor", "grok"):
            return {"ok": False, "error": f"unknown agent {agent}"}
        if not binary_for(agent):
            return {"ok": False, "error": f"{agent} is not installed (see Agents on this computer)"}
        self.roles["queen_agent"] = agent
        self.roles.update(director_agent="claude", director_model="")
        self.director_on = True
        if self.director is not None:
            self.director.agent, self.director.codex_model = "claude", ""
        self._drop_queen_terminal()
        return {"ok": True}

    def _drop_queen_terminal(self) -> None:
        """Another agent was chosen for the queen: the terminal of the old one is closed, so that "Start talking" opens the new."""
        qt, self.queen_term = self.queen_term, None
        if qt:
            try:
                self.host._call("terminal_close", qt["id"])
            except Exception:                          # the host is gone or the terminal already closed
                pass

    def open_queen_terminal(self, folder: str) -> dict[str, Any]:
        """The queen as an agent in a terminal of her own, started in the project folder: the user talks the job through with
        her there. An open one is reused."""
        from ..agents.catalog import binary_for
        from . import queen_term

        folder = (folder or "").strip()
        if not folder or not os.path.isdir(folder):
            return {"ok": False, "error": f"folder does not exist: {folder or '(empty)'}"}
        qt = self.queen_term
        if qt and os.path.normcase(os.path.abspath(qt["folder"])) == os.path.normcase(os.path.abspath(folder)):
            try:
                if any(t.get("id") == qt["id"] and t.get("running") for t in (self.host._call("terminals") or [])):
                    return {"ok": True, "id": qt["id"]}
                self._drop_queen_terminal()              # it has ended (the agent quit or failed): start her again
            except HostUnavailable:
                pass
        agent = self.roles.get("queen_agent") or ("codex" if self.roles.get("director_agent") == "codex" else "claude")
        binary = binary_for(agent)
        if not binary:
            return {"ok": False, "error": f"{agent} is not installed (see Agents on this computer)"}
        model = ("" if agent not in ("claude", "codex") else self.roles.get("director_model") if agent == "codex"
                 else self.roles.get("strong_model") or self.cfg.get("strong_model") or "sonnet") or ""
        installed = [p["name"] for p in _providers() if p["installed"]]
        brief = queen_term.build_brief(folder, installed, self.hive_text())
        try:
            result = self.host._call("open_terminal", folder, queen_term.argv(agent, binary, model, brief), cols=138, rows=32)
        except HostUnavailable as exc:
            return {"ok": False, "error": str(exc)}
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error") or "the host did not open a terminal"}
        self.queen_term = {"id": result["id"], "folder": folder, "started": time.time(), "agent": agent}
        if agent in queen_term.TYPED_FIRST:
            queen_term.type_first(self.host, folder, result["id"], agent, brief)
        return {"ok": True, "id": result["id"]}

    def _queen_term_view(self) -> Optional[dict[str, Any]]:
        """Her terminal, the plan she has written in it, and (while a team works) her window on the hive."""
        from . import queen_term

        qt = self.queen_term
        if not qt:
            return None
        text = self.hive_text()
        if text and (text != self._hive_written[0] or time.time() - self._hive_written[1] > 30) \
                and time.time() - self._hive_written[1] > 4:
            queen_term.write_hive(qt["folder"], text)
            self._hive_written = (text, time.time())
        try:
            ended = not any(t.get("id") == qt["id"] and t.get("running") for t in (self.host._call("terminals") or []))
        except HostUnavailable:
            ended = False
        return {"id": qt["id"], "folder": qt["folder"], "agent": qt["agent"], "ended": ended, "started": qt["started"],
                "plan": queen_term.read_plan(qt["folder"], qt["started"])}

    def hive_text(self) -> str:
        """What the queen can see of her hive, as plain lines for her to talk from: empty before any worker has started."""
        d = self.dispatch
        jobs = [j for j in (d.view() if d else []) if j.get("kind") == "task"]
        if not jobs:
            return ""
        now = time.time()
        lines = [f"Task: {(d.instruction or self.description or '')[:400]}"]
        for j in jobs:
            last = (j.get("progress") or "")[:140]
            lines.append(f"- W{j['priority']} {j.get('title', '')[:50]}: {j.get('agent')} {j.get('tier') or ''}, {j.get('status')}"
                         + (f", started {round((now - j['launched_at']) / 60)} min ago" if j.get("launched_at") else "")
                         + (f", last note: {last}" if last else ""))
        v = d.verify or {}
        if v.get("command"):
            lines.append(f"Delivery check `{v['command']}`: {v.get('state')}")
        usage = self._cost or {}
        if usage.get("tokens"):
            lines.append(f"Tokens used so far: {usage['tokens']}")
        return "\n".join(lines)

    def timeline(self) -> dict[str, Any]:
        """When each worker started and stopped and what it cost, for the Timeline tab: the bars show which worker
        sets the finish time. Tokens are read from a worker's own Claude transcript; a Codex worker shares its folder's
        sessions with its neighbours, so its tokens are not told apart (None)."""
        from . import ledger

        d = self.dispatch
        if d is None:
            return {"ok": True, "started": 0, "end": 0, "workers": []}
        jobs = [j for j in d.view() if j.get("kind") == "task"]
        started = float(d.started_at or 0) or min((float(j.get("launched_at") or 0) for j in jobs if j.get("launched_at")),
                                                  default=0.0)
        now = time.time()
        finished = self._finished_at(d.view())
        workers = []
        for j in jobs:
            begin = float(j.get("launched_at") or 0)
            if not begin:
                continue
            ended = j.get("status") in d.ENDED
            stamps = [float(e[0]) for e in (j.get("events") or []) if e and e[0]]
            end = (max(stamps) if stamps else begin) if ended else now
            tokens = None
            if j.get("agent") == "claude":
                path = ledger.find_worker_transcript(j.get("project_path") or "", j["priority"], started)
                if path:
                    tokens = ledger._cached("claude", ledger.transcript_cost, path)["tokens"]
            workers.append({"priority": j["priority"], "title": j.get("title") or "", "agent": j.get("agent"),
                            "model": j.get("tier") or "", "status": j.get("status"), "start": begin,
                            "end": max(end, begin), "ended": ended, "tokens": tokens})
        last = max((w for w in workers if w["ended"]), key=lambda w: w["end"], default=None)
        end = float(finished) if finished else max([w["end"] for w in workers] + [started])
        return {"ok": True, "started": started, "end": end, "finished": bool(finished),
                "critical": last["priority"] if last and len(workers) > 1 and all(w["ended"] for w in workers) else None,
                "workers": workers}

    def job_conversation(self, priority: int) -> dict[str, Any]:
        """What a worker said and did, read from its agent's own transcript: for a worker whose terminal is
        gone (finished, or its host restarted) this is what there is to read."""
        from . import ledger, transcript

        job = next((j for j in (self.dispatch.view() if self.dispatch else []) if j["priority"] == priority), None)
        if job is None:
            return {"ok": False, "error": "no such worker"}
        path = ledger.find_worker_transcript(job.get("project_path") or "", priority,
                                             float(self.dispatch.started_at or 0))
        if not path:
            return {"ok": True, "items": [], "note": "This worker has no saved conversation yet."}
        try:
            return {"ok": True, "items": transcript.read(path)}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"cannot read the transcript: {exc}"}

    def _results(self) -> Optional[dict[str, Any]]:
        """The plan's comparison, read from the jobs' own output files."""
        if not self.dispatch:
            return None
        from . import results

        return results.compare(self.dispatch.view(), self.dispatch.compare)

    def _dispatch_view(self) -> Optional[list[dict[str, Any]]]:
        if not self.dispatch:
            return None
        rows = self.dispatch.view()
        states = self._agent_states()
        for row in rows:
            state, reason = states.get(row.get("terminal") or "", ("", ""))
            row["term_state"], row["term_reason"] = self._term_state(row, states)
            if row.get("status") == "agent working" and state in ("waiting", "blocked"):
                row["attention"] = reason or "waiting for you"
                row["question"] = self._question(row["terminal"])
                if not row["question"]:
                    # Not a question Mother can read: show the bottom of the
                    # agent's screen as it looks, not QueenBee's one-line summary.
                    row["screen"] = self._screen_tail
        return rows

    _ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[=>()][0-9A-Za-z]?")
    _OPTION = re.compile(r"^[❯›>]?\s*(\d+)\.\s+(.+)$")
    _screen_tail: list[str] = []
    _system: dict[str, Any] = {}

    def _question(self, terminal: str) -> Optional[dict[str, Any]]:
        """The question an agent is waiting on, from the bottom of its screen:
        the last line that asks something, and the numbered choices after it."""
        self._screen_tail = []
        try:
            listing = {t["id"]: t for t in self.host._call("terminals") or []}
            offset = int(listing[terminal]["offset"])
            # The whole buffer (the host keeps 512 KB): while it waits, Codex
            # rewrites its window title several times a second -- after half
            # an hour the question sat 76 KB back, 74 KB of it title updates.
            data = self.host._call("terminal_read", terminal, max(0, offset - 512 * 1024)).get("data") or ""
        except (HostUnavailable, KeyError, TypeError, ValueError):
            return None
        # Agent TUIs draw with cursor moves (Codex places each word of a
        # wrapped command separately), so replay the stream onto a screen the
        # terminal's size and read its rows, as a person would.
        lines = []
        rows = render(data, int(listing[terminal].get("cols") or 120),
                      int(listing[terminal].get("rows") or 32))
        for raw in rows:
            # Box-drawing borders of the agents' TUIs are not part of the text.
            raw = re.sub(r"[─-╿▀-▟]+", " ", raw)
            line = re.sub(r"\s+", " ", raw).strip(" >›")
            if len(line) > 2 and (not lines or lines[-1] != line):
                lines.append(line)
        self._screen_tail = [l for l in lines if "for shortcuts" not in l][-8:]
        # The choices are the last run of "1. …", "2. …" lines; the question is
        # the nearest line above them that asks something ("?" anywhere, since
        # agents often add a note after it).
        first_option = None
        for i in range(len(lines) - 1, -1, -1):
            m = self._OPTION.match(lines[i])
            if m and m.group(1) == "1":
                first_option = i
                break
        if first_option is None:
            # Claude draws some menus without numbers ("❯ No, exit" /
            # "Yes, I trust this folder"): read them from the cursor mark.
            menu = self._cursor_menu(rows)
            if menu is not None:
                options, cursor, top = menu
                above = [re.sub(r"\s+", " ", r).strip(" >›❯") for r in rows[max(0, top - 14):top] if r.strip()]
                ask = next((l for l in reversed(above) if "?" in l), None)
                if ask is None:
                    return None
                return {"text": ask[:400], "options": options, "context": above, "cursor": cursor}
        start = first_option if first_option is not None else len(lines)
        # Codex prints the whole command between its question and the
        # choices; a long one wraps over a dozen rows.
        ask = next((i for i in range(start - 1, max(-1, start - 30), -1)
                    if ("?" in lines[i] or "Do you want" in lines[i])
                    # Codex's idle footer ("? for shortcuts") is not a question.
                    and "for shortcuts" not in lines[i]), None)
        if ask is not None:
            # A question often wraps over lines; keep the sentence that asks.
            m = re.search(r"[^.?!]*\?[^?]*$", lines[ask])
            if m and len(lines[ask]) > 200:
                lines[ask] = m.group(0).strip()
        if ask is None:
            return None
        options = []
        for line in lines[start:]:
            m = self._OPTION.match(line)
            if m and m.group(1) not in {o["n"] for o in options}:
                options.append({"n": m.group(1), "text": m.group(2)[:160]})
        # The lines above the question hold what is being asked about: the
        # command or the file. The approver judges scope from them.
        # ...and the lines between it and the choices: Codex prints the
        # command it wants to run there ("$ & python ..."), and the approver
        # must judge that command, not only the text above the question.
        # Claude frames the command under a "Bash command" / "PowerShell
        # command" header; take all of it, however many rows it wraps to, so
        # the approver judges the whole command and not only its tail.
        top = next((i for i in range(ask - 1, max(-1, ask - 60), -1)
                    if re.match(r"(?i)^(bash|powershell|shell) command\b", lines[i])), None)
        context = lines[top + 1 if top is not None else max(0, ask - 8):ask] + lines[ask + 1:start]
        return {"text": lines[ask][:400], "options": options[:6], "context": context}

    @staticmethod
    def _cursor_menu(rows: list[str]) -> Optional[tuple[list[dict[str, str]], int, int]]:
        """(options, cursor position from 1, row of the first option) of a
        menu drawn as rows at one indent with a ❯ on the chosen one."""
        def indent(row: str) -> int:
            return len(row) - len(row.lstrip())

        filled = [i for i, r in enumerate(rows) if r.strip()]
        last = filled[-1] if filled else -1
        for i in range(last, max(-1, last - 16), -1):
            m = re.match(r"^(\s*)❯\s+(\S.*)$", rows[i])
            if not m:
                continue
            col = m.start(2)
            items = [(i, m.group(2).strip())]
            j = i - 1
            while j >= 0 and rows[j].strip() and indent(rows[j]) == col:
                items.insert(0, (j, rows[j].strip()))
                j -= 1
            j = i + 1
            while j < len(rows) and rows[j].strip() and indent(rows[j]) == col:
                items.append((j, rows[j].strip()))
                j += 1
            if len(items) < 2:
                return None
            cursor = [r for r, _ in items].index(i) + 1
            return ([{"n": str(k), "text": t[:160]} for k, (_, t) in enumerate(items, 1)],
                    cursor, items[0][0])
        return None

    def usage(self) -> dict[str, Any]:
        """What this queen's workers use right now -- the CPU, RAM and VRAM of
        their terminals as QueenBee measures them -- and what they have cost."""
        self._agent_states()
        jobs = self.dispatch.view() if self.dispatch else []
        self._refresh_cost()
        return {**self._measure(jobs), "held": sum(bool(j.get("held")) for j in jobs), **self._cost}

    RUNNING = ("agent working", "GPU job submitted")        # a worker at work in a terminal

    def _measure(self, jobs: list[dict[str, Any]]) -> dict[str, Any]:
        cpu = ram = vram = 0.0
        any_vram = False
        for j in jobs:
            for term in {j.get("terminal"), j.get("workload")} - {None}:
                u = self._usage.get(term)
                if not u:
                    continue
                cpu += float(u[0] or 0)
                ram += float(u[1] or 0)
                if u[2] is not None:
                    vram += float(u[2])
                    any_vram = True
        return {"cpu_pct": round(cpu, 1), "ram_mb": round(ram), "vram_mb": round(vram) if any_vram else None,
                "running": sum(j.get("status") in self.RUNNING and not j.get("detached") for j in jobs),
                "workers": len(jobs)}

    def _gate(self, job: dict[str, Any], jobs: list[dict[str, Any]]) -> Optional[str]:
        """Why a worker may not start yet under this queen's limits, or None.
        Runs inside the plan's own step: it reads the jobs it is handed and the
        shared QueenBee figures, never the plan's lock."""
        lim = self.limits
        if not lim:
            return None
        self._agent_states()
        use = self._measure(jobs)
        if "workers" in lim and use["running"] >= lim["workers"]:
            return f"{use['running']}/{lim['workers']:g} workers at work"
        cost = self._cost.get("cost_usd")
        used = self._cost.get("tokens")
        if "budget_tokens" in lim and used is not None and used >= lim["budget_tokens"]:
            return f"token budget {lim['budget_tokens'] / 1e6:g}M reached ({used / 1e6:.2f}M used)"
        if "budget_usd" in lim and cost is not None and cost >= lim["budget_usd"]:
            return f"budget ${lim['budget_usd']:g} reached (${cost:.2f} spent)"
        if "ram_mb" in lim and use["ram_mb"] >= lim["ram_mb"]:
            return f"RAM {use['ram_mb'] / 1024:.1f} of {lim['ram_mb'] / 1024:g} GB in use"
        if "cpu_pct" in lim and use["cpu_pct"] >= lim["cpu_pct"]:
            return f"CPU {use['cpu_pct']:.0f}% of {lim['cpu_pct']:g}% in use"
        gpu = job.get("kind") == "gpu" or "queenbee.gpu" in (job.get("command") or "")
        if gpu and "vram_mb" in lim and (use["vram_mb"] or 0) >= lim["vram_mb"]:
            return f"VRAM {(use['vram_mb'] or 0) / 1024:.1f} of {lim['vram_mb'] / 1024:g} GB in use"
        return None

    def set_limits(self, limits: dict[str, Any]) -> dict[str, Any]:
        self.limits = clean_limits(limits)
        return {"ok": True, "limits": self.limits}

    def _refresh_cost(self) -> None:
        """Tokens and dollars so far, re-read from the transcripts at most
        every 30 s and off the request's thread."""
        d = self.dispatch
        if d is None or time.time() - self._cost_at < 30:
            return
        self._cost_at = time.time()

        def work() -> None:
            from . import ledger

            folders = {j["project_path"] for j in d.view() if j.get("project_path")}
            spent = [ledger.worker_costs(f, d.started_at) for f in folders]
            own = [c for c in (d.costs or {}).values() if isinstance(c, dict)]
            tokens = sum(w["tokens"] for w in spent) + sum(int(c.get("tokens") or 0) for c in own)
            usd = sum(w["cost_usd"] or 0 for w in spent) + sum(float(c.get("cost_usd") or 0) for c in own)
            self._cost = {"tokens": tokens, "cost_usd": round(usd, 3)}

        threading.Thread(target=work, name=f"queen-{self.queen}-cost", daemon=True).start()

    SUMMARY_KEYS = ("priority", "title", "status", "parent", "role", "branched", "detached", "agent",
                    "tier", "effort", "direct", "terminal", "lane", "instruction", "goal", "group", "kind", "held",
                    "progress", "after", "waiting_for")

    def summary(self) -> dict[str, Any]:
        """What the hive map shows of a queen that is not the one selected:
        its instruction and its tree, without reading any agent's screen."""
        jobs = self.dispatch.view() if self.dispatch else []
        return {
            "id": self.queen, "name": self.name, "primary": self.primary,
            "agent": self.agent, "model": self.model, "limits": self.limits,
            # The model she directs with (as her panel's picker shows it), named on her box on the map.
            "director_agent": self.roles.get("director_agent") or "claude",
            "director_model": self.roles.get("strong_model") or "sonnet",
            "director_codex_model": self.roles.get("director_model", ""),
            "queen_terminal_agent": self.roles.get("queen_agent", ""),
            "instruction": (self.dispatch.instruction if self.dispatch else "")
                           or (self.plan or {}).get("instruction", ""),
            "description": self.description,
            "manual": self.manual_approvals, "solo": self.solo, "answers": "me" if self.user_answers else "queen",
            "waits": sum(1 for a in (self.dispatch.approvals if self.dispatch else [])
                         if a.get("decision") == "left to you" or a.get("manual")),
            "waited_s": round(sum(float(a.get("delay") or 0) for a in (self.dispatch.approvals if self.dispatch else []))),
            "started": float(self.dispatch.started_at or 0) if self.dispatch else 0,
            "finished": self._finished_at(jobs),
            "running": bool(self.dispatch and not self.dispatch.done.is_set()),
            "held": bool(self.dispatch and self.dispatch.held),
            "planning": self.planning or bool(self.commanding), "preparing": self.preparing,
            "commanding": self.commanding, "usage": self.usage(),
            "jobs": [{**{k: j.get(k) for k in self.SUMMARY_KEYS},
                      "term_state": self._term_state(j, self._states)[0]} for j in jobs],
            "activity": self._activity(jobs),
            # The latest few things that happened, for the user's notices.
            "feed": self._feed(jobs, 12),
        }

    def _finished_at(self, jobs: list[dict[str, Any]]) -> float:
        """When the queen's work stopped: the report time, or else the last thing that happened once every worker
        has ended (the clock on her box must stop with the work, not with the report being written)."""
        d = self.dispatch
        if d is None:
            return 0.0
        if d.finished_at:
            return float(d.finished_at)
        ended = ("done", "failed", "stopped", "stopped early")
        tasks = [j for j in jobs if j.get("kind") == "task"]
        if not tasks or not all(j.get("status") in ended for j in tasks):
            return 0.0
        if (d.verify or {}).get("state") in ("waiting", "running", "fixing"):
            return 0.0
        return float(max((e[0] for j in jobs for e in (j.get("events") or [])), default=0) or 0)

    @staticmethod
    def _term_state(job: dict[str, Any], states: dict[str, tuple[str, str]]) -> tuple[str, str]:
        """What a worker's terminal is doing now, as QueenBee reads its screen:
        working, waiting (for an answer or approval), idle, or closed."""
        term = job.get("terminal") if not job.get("direct") else job.get("workload")
        if not term:
            return ("", "")
        state, reason = states.get(term, ("", ""))
        if state in ("blocked", "waiting"):
            return ("waiting", reason)
        if state == "idle" and "exited" in (reason or ""):
            return ("closed", reason)
        return (state or "", reason)

    def _activity(self, jobs: list[dict[str, Any]]) -> str:
        """The queen's own state, from its workers' terminals."""
        seen = {self._term_state(j, self._states)[0] for j in jobs
                if j.get("status") not in ("done", "failed", "stopped", "stopped early")}
        if self.commanding or "working" in seen:
            return "working"
        if self.director_on and seen:                  # workers are at it and she is looking after them
            return "looking in" if self.director and self.director.busy else "directing"
        if "waiting" in seen:
            return "waiting"
        if jobs and all(j.get("status") in ("done", "failed", "stopped", "stopped early") for j in jobs):
            verify = (self.dispatch.verify or {}).get("state") if self.dispatch else None
            if verify == "passed":
                return "delivered"
            if verify == "failed" or any(j.get("status") in ("failed", "stopped") for j in jobs):
                return "needs you"
            return "finished"
        return "idle" if jobs else "new"

    def _feed(self, jobs: list[dict[str, Any]], n: int) -> list[list[Any]]:
        rows = [[t, j["priority"], j.get("title") or "", text] for j in jobs for t, text in j.get("events") or []]
        rows += [[c["ts"], None, "", c["queen"]] for c in self.commands if c.get("queen")]
        rows.sort(key=lambda r: r[0])
        return rows[-n:]

    def view(self) -> dict[str, Any]:
        return {
            "commanding": self.commanding, "command_error": self.command_error,
            "activity": self._activity(self.dispatch.view() if self.dispatch else []),
            "commands": self.commands[-60:], "usage": self.usage(),
            "costs": dict(self.dispatch.costs) if self.dispatch else {},
            "queen": self.queen,
            "solo": self.solo,
            "queen_name": self.name,
            "queen_agent": self.agent, "queen_model": self.model, "limits": self.limits,
            "planning": self.planning,
            "plan": self.plan,
            "dispatch": self._dispatch_view(),
            "board": list(self.dispatch.board_log[-60:]) if self.dispatch else [],
            "findings": list(self.dispatch.findings[-60:]) if self.dispatch else [],
            "answers": "me" if self.user_answers else "queen",
            "results": self._results(),
            "running": bool(self.dispatch and not self.dispatch.done.is_set()),
            "held": bool(self.dispatch and self.dispatch.held),
            "models": self.cfg["models"],
            "model_ids": ledger.model_ids(),
            "efforts": self.cfg.get("efforts") or {},
            "codex_model": _codex_model(),
            "providers": _providers(),
            "instruction": (self.dispatch.instruction if self.dispatch else "")
                           or (self.plan or {}).get("instruction", ""),
            "description": self.description,
            "director": self.director.status() if self.director else None,
            "director_agent": self.roles.get("director_agent") or "claude",
            "director_model": self.roles.get("strong_model") or "sonnet",
            "director_codex_model": self.roles.get("director_model", ""), "queen_terminal_agent": self.roles.get("queen_agent", ""),
            "director_on": self.director_on,
            "worker_models": {"claude": self.roles.get("worker_model") or self.cfg.get("worker_model") or "haiku",
                              "codex": self.roles.get("codex_worker_model") or "default"},
            "beekeeper": self.beekeeper.status() if self.beekeeper else
            {"busy": False, "audits": 0, "report": None, "error": "", "agent": self.roles.get("beekeeper_agent", ""),
             "model": self.roles.get("beekeeper_model", ""), "rules": self.roles.get("beekeeper_rules", ""), "next_in": 0},
            "system": self._machine(),
            "roles": {k: self.cfg[k] for k in ("planner_model", "architect_model",
                                               "worker_model", "answer_model")},
            "escalation": self.cfg.get("escalation"),
            "preparing": self.preparing,
            "prepared": self.prepared,
            "report": self._report_view(),
            "completion": self._completion_view(),
            "queen_term": self._queen_term_view(),
            "sweep": self.dispatch.sweep if self.dispatch else None,
            "verify": ({k: v for k, v in self.dispatch.verify.items() if k != "log"} if self.dispatch and self.dispatch.verify else None),
            "approvals": self.approvals[-30:],
            "planner_model": self.cfg["planner_model"],
            "projects": _project_paths(),
        }


def _project_paths() -> list[str]:
    from ..projects import ProjectSet

    return sorted({p for proj in ProjectSet.load().listing() for p in proj.get("paths") or []})


def _ancestors(pid: int) -> set[int]:
    try:
        return {p.pid for p in psutil.Process(pid).parents()}
    except psutil.Error:
        return set()


_VIEW: dict[str, Any] = {"at": 0.0, "value": None, "refreshing": False}
_VIEW_LOCK = threading.Lock()


def cached_job_view(host: HostClient, ttl: float = 2.5) -> dict[str, Any]:
    """job_view, built by one request at a time and reused briefly: it takes the host seconds, and the page asks every two.
    Only the very first request waits for it; afterwards a stale view is answered at once and refreshed behind the request,
    so the window never freezes for the seconds the host's GPU reading takes."""
    with _VIEW_LOCK:
        value = _VIEW["value"]
        if value is not None:
            if time.time() - _VIEW["at"] > ttl and not _VIEW["refreshing"]:
                _VIEW["refreshing"] = True
                threading.Thread(target=_refresh_job_view, args=(host,), name="job-view-refresh", daemon=True).start()
            return dict(value)
    fresh = job_view(host)
    with _VIEW_LOCK:
        _VIEW["value"], _VIEW["at"] = fresh, time.time()
        return dict(fresh)


def _refresh_job_view(host: HostClient) -> None:
    try:
        fresh = job_view(host)
    except Exception:                                  # the host hiccuped: the stale view stays until the next try
        fresh = None
    with _VIEW_LOCK:
        if fresh is not None:
            _VIEW["value"], _VIEW["at"] = fresh, time.time()
        else:
            _VIEW["at"] = time.time()                  # do not hammer a failing host: try again after a ttl
        _VIEW["refreshing"] = False


def job_view(host: HostClient) -> dict[str, Any]:
    """What the window shows: host health, the GPU and every GPU job.

    A preempted job has exited and is no longer registered, but the terminal
    running its `queenbee.gpu run` launcher is still there, waiting to start it
    again; such a terminal is shown as "waiting to resume".
    """
    info = host.ping()
    if info is None:
        return {"ok": True, "host": None, "devices": [], "jobs": [], "between": []}
    try:
        gpu = host._call("gpu_state")
        terminals = host._call("terminals") or []
    except HostUnavailable as exc:
        return {"ok": False, "error": str(exc), "host": info, "devices": [], "jobs": [], "between": []}

    jobs = gpu.get("jobs") or []
    owners = {}
    for job in jobs:
        for term in terminals:
            if term.get("gpu_workload") and term.get("pid") in _ancestors(int(job["pid"])):
                owners[job["id"]] = term.get("id")
    registered_terms = set(owners.values())
    between = [
        {"terminal": t.get("id"), "cwd": t.get("cwd"), "started_at": t.get("started_at")}
        for t in terminals
        if t.get("gpu_workload") and t.get("running") and t.get("id") not in registered_terms
    ]
    return {
        "ok": True,
        "host": {"pid": info.get("pid"), "uptime_s": info.get("uptime_s"),
                 "version": info.get("version")},
        "telemetry_fresh": gpu.get("telemetry_fresh"),
        "devices": gpu.get("devices") or [],
        "jobs": [{
            "id": j["id"], "name": j.get("name"), "role": j.get("role"),
            "action": j.get("action"), "reason": j.get("reason"),
            "batch": j.get("batch"), "max_batch": j.get("max_batch"),
            "resumable": j.get("resumable", False), "confirmed": j.get("confirmed"),
            "heartbeat_age_s": j.get("heartbeat_age_s"), "gpu": j.get("gpu"),
            "terminal": owners.get(j["id"]),
        } for j in jobs],
        "between": between,
        "planner": {"ready": False},
        "ts": time.time(),
    }


def _archive_plan(queen: str) -> None:
    """Move a queen's plan file aside (never delete it): a removed or reset
    queen must not come back with its old work."""
    from .dispatch import Dispatch

    path = Dispatch.path_for(queen)
    if path.exists():
        try:
            os.replace(path, path.with_name(f"{path.stem}.removed-{int(time.time())}.json"))
        except OSError:
            log.debug("could not move %s aside", path, exc_info=True)


def agent_setup(agent: str, action: str) -> dict[str, Any]:
    """Open a PowerShell window that installs an agent or signs it in. The command is never taken from the window: it is the
    catalog's own for that agent, and the window showed it to the user before asking."""
    import shutil
    import subprocess

    from ..agents.catalog import CATALOG

    spec = next((s for s in CATALOG if s.id == agent), None)
    if spec is None or action not in ("install", "login"):
        return {"ok": False, "error": "unknown agent or action"}
    command = spec.install_command if action == "install" else spec.login_command
    if not command:
        return {"ok": False, "error": f"there is no {action} command for {spec.label}"}
    if action == "install" and spec.needs and not shutil.which(spec.needs):
        where = {"npm": "Node.js from https://nodejs.org", "uv": "uv from https://docs.astral.sh/uv/"}.get(spec.needs, spec.needs)
        return {"ok": False, "error": f"{spec.label} needs {spec.needs} on this computer first (install {where}, then try again)"}
    if os.name == "nt":
        subprocess.Popen(["powershell.exe", "-NoLogo", "-NoExit", "-Command", command],
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    elif sys.platform == "darwin":
        # Terminal.app, a new window, the command typed in: the user sees it run and can answer its prompts.
        quoted = command.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen(["osascript", "-e", 'tell application "Terminal" to do script "' + quoted + '"',
                          "-e", 'tell application "Terminal" to activate'])
    else:
        term = next((t for t in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm") if shutil.which(t)), None)
        if not term:
            return {"ok": False, "error": f"run this in a terminal: {command}"}
        hold = command + '; echo; read -p "Press Enter to close"'
        subprocess.Popen([term, "--", "bash", "-lc", hold] if term == "gnome-terminal" else [term, "-e", f"bash -lc '{hold}'"])
    return {"ok": True, "command": command, "hint": spec.login_hint if action == "login" else ""}


class Hive:
    """Every queen this window runs, each with its own plan, workers and saved
    state. The list itself is kept in queens.json so reopening the window
    brings every queen back."""

    def __init__(self, host: HostClient, ui_port: int = 7878) -> None:
        self.host, self.ui_port = host, ui_port
        self.lock = threading.Lock()
        saved = self._load()
        self.queens: list[Mother] = []
        for i, q in enumerate(saved or [{"id": "q1", "name": "Queen 1"}]):
            self.queens.append(Mother(host, ui_port, q["id"], q.get("name") or "", primary=i == 0,
                                      agent=str(q.get("agent") or ""), model=str(q.get("model") or ""),
                                      limits=q.get("limits") if isinstance(q.get("limits"), dict) else None,
                                      roles=q.get("roles") if isinstance(q.get("roles"), dict) else None))
            self.queens[-1].description = str(q.get("description") or "")[:4000]
            self.queens[-1].director_on = bool(q.get("director"))
            self.queens[-1].manual_approvals = bool(q.get("manual"))
            self.queens[-1].user_answers = q.get("answers") == "me"
            if self.queens[-1].dispatch is not None:               # restored in her constructor, before this was known
                self.queens[-1].dispatch.asks_user = self.queens[-1].user_answers
            self.queens[-1].solo = bool(q.get("solo"))
            self.queens[-1].manual_delay_s = float(q.get("manual_delay") or 0)

    @staticmethod
    def _path():
        from ..config import _state_dir

        return _state_dir() / "queens.json"

    def _load(self) -> list[dict[str, str]]:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
            return [q for q in data if isinstance(q, dict) and re.fullmatch(r"q\d+", str(q.get("id")))]
        except (OSError, ValueError, TypeError):
            return []

    def _save(self) -> None:
        try:
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps([{"id": q.queen, "name": q.name, "agent": q.agent, "model": q.model,
                                         **({"limits": q.limits} if q.limits else {}),
                                         **({"roles": q.roles} if q.roles else {}),
                                         **({"description": q.description} if q.description else {}),
                                         **({"director": True} if q.director_on else {}),
                                         **({"manual": True} if q.manual_approvals else {}),
                                         **({"answers": "me"} if q.user_answers else {}),
                                         **({"solo": True} if q.solo else {}),
                                         **({"manual_delay": q.manual_delay_s} if q.manual_delay_s else {})}
                                        for q in self.queens]),
                            encoding="utf-8")
        except OSError:
            log.debug("could not save the queens", exc_info=True)

    def get(self, queen: str = "") -> Mother:
        return next((q for q in self.queens if q.queen == queen), self.queens[0])

    def set_answers(self, queen: str, mode: str) -> dict[str, Any]:
        with self.lock:
            q = next((q for q in self.queens if q.queen == queen), None)
            if q is None:
                return {"ok": False, "error": "no such queen"}
            result = q.set_answers(mode)
            self._save()
            return result

    def limit(self, queen: str, limits: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            q = next((q for q in self.queens if q.queen == queen), None)
            if q is None:
                return {"ok": False, "error": "no such queen"}
            result = q.set_limits(limits)
            self._save()
            return result

    DEMO_PARTS = (
        ("Store: keeps the notes in a file", "tests.test_store", "Build notes/store.py (the store part). Decide what a note looks like inside and "
         "post it on the hive board as soon as you decide."),
        ("Search: finds notes by text and tag", "tests.test_search", "Build notes/search.py (the search part). Wait for the store worker's note shape "
         "on the hive board, then follow it."),
        ("Export: writes the notes as Markdown", "tests.test_export", "Build notes/export.py (the export part). Wait for the store worker's note shape "
         "on the hive board, then follow it."),
    )

    def demo(self, kind: str = "notes") -> dict[str, Any]:
        """The example project, ready to run: three workers whose parts must agree (teamwork), and a queen."""
        if kind == "notes-plan":
            return self._demo_notes_plan()
        import shutil

        # The team is whichever of the two is here, so that the first click works with either agent the README names:
        # with both, one team of both (the store part, which decides what a note is, on Codex; search and export on Claude
        # Haiku; a Claude queen); with Claude Code alone, Haiku workers and a Sonnet queen; with Codex alone, Codex for the
        # workers and the queen alike.
        providers = {p["id"]: p for p in _providers()}
        claude, codex = (bool(providers.get(a, {}).get("installed")) for a in ("claude", "codex"))
        if claude and codex:
            seats, roles = [("codex", "default"), ("claude", "haiku"), ("claude", "haiku")], {"strong_model": "sonnet"}
        elif claude:
            seats, roles = [("claude", "haiku")] * 3, {"strong_model": "sonnet"}
        elif codex:
            seats, roles = [("codex", "default")] * 3, {"strong_model": "sonnet", "director_agent": "codex"}
        else:
            return {"ok": False, "error": "the demo needs Claude Code or Codex: install one and sign in first "
                                          "(Agents on this computer, in the left column)"}
        src = Path(__file__).parent / "demo_free"
        if not src.is_dir():
            return {"ok": False, "error": "the demo project is missing from this install"}
        base = Path.home() / "QueenBeeDemo"
        base.mkdir(exist_ok=True)
        dest, n = base / "free-demo", 1
        while dest.exists():
            n += 1
            dest = base / f"free-demo-{n}"
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
        added = self.add("Notes demo", "", "", {"workers": 3, "budget_tokens": 20_000_000}, roles,
                         ("A notebook built by Codex and Claude Code as one team: three workers that must agree on what "
                          "a note is. " if claude and codex else
                          "A notebook built by three workers that must agree on what a note is. ") +
                         "She looks in every couple of minutes, settles what they disagree on, answers their "
                         "permission prompts, and runs the whole project's tests when all three are in.",
                         True)
        if not added.get("ok"):
            return added
        queen = self.get(added["queen"])
        common = ("Read README.md first. The README leaves open what a note looks like inside: the store worker "
                  "decides it and announces it on the hive board in one line; the others read the board and follow it. ")
        for (name, test, goal), (agent, worker_model) in zip(self.DEMO_PARTS, seats):
            r = queen.add_worker(common + goal, str(dest), agent, worker_model,
                                 None, [{"type": "command", "run": f"python -m unittest {test}"}], "", name)
            if not r.get("ok"):
                return r
        queen.dispatch.save()
        queen.set_final_check("python -m unittest discover -s tests -t .")
        queen.dispatch.save()
        return {"ok": True, "queen": added["queen"], "folder": str(dest)}

    # (title, parts it must make pass, agent, model) -- a mix of models on purpose: simple parts to the cheap ones
    BUILD_PARTS = (
        ("Accounts and permissions", ("auth", "permissions"), "claude", "haiku"),
        ("Projects and reports", ("projects", "reports"), "codex", "gpt-5.6-terra"),
        ("Issues", ("issues",), "claude", "sonnet"),
        ("Search language", ("query",), "claude", "sonnet"),
        ("Import, export, command line, README", ("transfer", "cli", "readme"), "codex", "gpt-5.6-terra"),
        ("HTTP API", ("api",), "claude", "sonnet"),
        ("Browser front end", ("frontend",), "claude", "haiku"),
    )

    # thirty independent tickets in six loads of about the same weight (heavy 3, medium 2, light 1)
    MARATHON_LOADS = (
        (1, 29, 2, 7, 14), (3, 23, 6, 15, 16), (4, 19, 8, 21, 22), (5, 17, 9, 24, 27), (11, 13, 10, 28, 30),
        (12, 18, 20, 25, 26),
    )
    MARATHON_MODEL = "gpt-5.6-terra"

    AUDIT_PARTS = (
        ("Accounts", "section 1 (trackr/auth.py)"),
        ("Projects and permissions", "sections 2 and 5 (trackr/projects.py, trackr/permissions.py)"),
        ("Issues", "section 3 (trackr/issues.py)"),
        ("Search", "section 4 (trackr/query.py)"),
        ("Reports and import/export", "sections 6 and 8 (trackr/reports.py, trackr/transfer.py)"),
        ("HTTP API", "section 7 (trackr/api.py), including how it uses permissions and the other parts"),
    )

    # (title, agent, model, what it writes, acceptance command or None). The cheap models take the plain parts, the
    # strong ones the parts that are hard to get right.
    SQL_PARTS = (
        ("Lexer", "codex", "gpt-5.6-terra", "minidb/lexer.py (section 1)", "python -m unittest tests.test_lexer"),
        ("Parser", "claude", "sonnet", "minidb/parser.py (every statement and the expression grammar)",
         "python -m unittest tests.test_parser"),
        ("Functions and expressions", "codex", "gpt-5.6-terra",
         "minidb/functions.py and minidb/expr.py (section 4: NULL logic, types, scalar functions)",
         "python -m unittest tests.test_functions tests.test_expr"),
        ("Storage, indexes, persistence", "claude", "sonnet",
         "minidb/storage.py (section 12: tables, constraints, indexes, snapshots, dump/load)",
         "python -m unittest tests.test_storage"),
        ("Executor", "claude", "opus",
         "minidb/executor.py (sections 5 to 8 and 10: select pipeline, joins, grouping, subqueries, update/delete, "
         "explain, index lookups)", None),
        ("Database, command line, docs", "claude", "haiku",
         "minidb/database.py (transactions, atomic statements, saving; section 9 and 12), minidb/cli.py and "
         "minidb/__main__.py (section 11) and docs/ARCHITECTURE.md", None),
    )

    # MiniDB 2: nine new features on top of a finished MiniDB (TASK2.md, 154 checks in all).
    SQL2_PARTS = (
        ("Parser and lexer", "claude", "sonnet", "minidb/parser.py and minidb/lexer.py: all the new syntax of TASK2.md"),
        ("Expressions and functions", "codex", "gpt-5.6-terra",
         "minidb/expr.py and minidb/functions.py: CASE, CAST, EXISTS, the new scalar functions (section 13)"),
        ("Window functions", "claude", "sonnet",
         "minidb/windows.py with compute_window(func, rows, eval_expr) (section 17, and section 20 for the interface)"),
        ("Storage: schema changes, foreign keys", "claude", "sonnet",
         "minidb/storage.py: ALTER TABLE, views, foreign keys, persistence (section 19)"),
        ("Executor: select side", "claude", "opus",
         "minidb/executor.py, the SELECT side only: correlated subqueries, derived tables, CTEs, views, set operations, "
         "RIGHT and CROSS joins, calling compute_window (sections 14 to 17)"),
        ("INSERT, upsert, DDL", "claude", "sonnet",
         "minidb/dml.py: INSERT ... SELECT, ON CONFLICT, the ALTER TABLE and view statements, and the hand-over from "
         "executor.execute (sections 18 and 19); in executor.py touch only the dispatch of those statements"),
        ("Tests and docs", "claude", "haiku",
         "tests/test_v2_*.py (regression tests for each section of TASK2.md through the public API, written from the "
         "spec; they will fail until the other parts are in) and docs/ARCHITECTURE.md"),
    )

    def _demo_notes_plan(self) -> dict[str, Any]:
        """The small Notes project as a folder for a queen in planner mode, with the rules (QUEEN.md) a Codex-only team follows:
        short enough for a one-minute film. No queen is made: the person, or the recording script, types the instruction."""
        import shutil

        src = Path(__file__).parent / "demo_free"
        if not src.is_dir():
            return {"ok": False, "error": "the demo project is missing from this install"}
        base = Path.home() / "QueenBeeDemo"
        base.mkdir(exist_ok=True)
        dest, n = base / "notes-plan", 1
        while dest.exists():
            n += 1
            dest = base / f"notes-plan-{n}"
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
        (dest / "QUEEN.md").write_text(
            "# QUEEN.md: how a team works on this project\n"
            "\n"
            "## Scope\n"
            "- Work only inside this folder. Never delete files that are not your own, never install packages, never touch the network. Python 3.11, standard library only.\n"
            "\n"
            "## How to split the work\n"
            "- One worker per part (store, search, export); all start at once.\n"
            "- Each worker edits only its own file and writes its own tests (tests/test_<part>.py).\n"
            "\n"
            "## Talking to each other\n"
            "- The store worker decides what a note looks like inside and posts it on the hive board in one line before the others rely on it.\n"
            "\n"
            "## Done means\n"
            "- python -m unittest discover -s tests -t . is green.\n"
            "\n"
            "## What the queen answers by herself\n"
            "- Edits and tests inside this folder: yes. Anything that deletes, installs, downloads or leaves this folder: no.\n",
            encoding="utf-8", newline="\n")
        return {"ok": True, "folder": str(dest)}

    def add(self, name: str = "", agent: str = "", model: str = "",
            limits: Optional[dict[str, Any]] = None, roles: Optional[dict[str, str]] = None,
            description: str = "", director: bool = False) -> dict[str, Any]:
        providers = {p["id"]: p for p in _providers()}
        if agent and agent != "auto" and (agent not in providers or not providers[agent]["installed"]):
            return {"ok": False, "error": f"{agent} is not installed"}
        if agent and agent != "auto" and model and model != "auto" and model not in providers[agent]["models"]:
            return {"ok": False, "error": f"{agent} has no model {model}"}
        with self.lock:
            # Only a queen nobody has set up yet is reused; one the user created (named, described, given a
            # model) stays, and the new queen is a second one.
            unused = [q for q in self.queens if q.plan is None and q.dispatch is None and not q.planning
                      and not q.description and not q.director_on and not q.agent and not q.limits
                      and re.fullmatch(r"Queen \d+", q.name or "")]
            if len(self.queens) == 1 and unused:
                queen = unused[0]           # the empty first queen becomes this one
                if name.strip():
                    queen.name = name.strip()[:40]
            else:
                n = max(int(q.queen[1:]) for q in self.queens) + 1
                # A removed queen's plan file must not come back to life under
                # a new queen that happens to get its id.
                _archive_plan(f"q{n}")
                queen = Mother(self.host, self.ui_port, f"q{n}", name.strip()[:40], primary=False)
                self.queens.append(queen)
            queen.agent, queen.model = agent, model
            queen.description = description.strip()[:4000]
            queen.director_on = bool(director)
            if limits:
                queen.set_limits(limits)
            queen.roles = {k: str(v) for k, v in (roles or {}).items() if k in ROLE_KEYS and v}
            from . import config as _config
            queen.cfg = _config.capped({**_config.load(), **queen.roles})    # never the roles of the queen before her (TODO 6)
            self._save()
            return {"ok": True, "queen": queen.queen}

    def add_free_worker(self, name: str, agent: str, model: str, folder: str, goal: str, check: str = "",
                        delay: float = 0.0) -> dict[str, Any]:
        """A worker that belongs to no queen: one agent on the map, started the way you would start it yourself. Nobody answers its
        permission questions but you. It lives in a container of its own (a queen entry that is never shown as a queen)."""
        providers = {p["id"]: p for p in _providers()}
        if agent not in providers or not providers[agent]["installed"]:
            return {"ok": False, "error": f"{agent or 'that agent'} is not installed"}
        folder, goal = (folder or "").strip(), (goal or "").strip()
        if not goal:
            return {"ok": False, "error": "write what this worker should do"}
        if not os.path.isdir(folder):
            return {"ok": False, "error": f"folder does not exist: {folder or '(empty)'}"}
        with self.lock:
            n = max(int(q.queen[1:]) for q in self.queens) + 1
            _archive_plan(f"q{n}")
            queen = Mother(self.host, self.ui_port, f"q{n}", (name or "").strip()[:40] or "Free worker", primary=False)
            # delay 0: every question waits for you. A delay (a benchmark) answers it after that many seconds and counts it.
            queen.solo, queen.manual_approvals, queen.manual_delay_s = True, True, max(0.0, min(30.0, float(delay or 0)))
            queen.set_limits({"workers": 1})
            # Free workers in one folder each get their own W<n> (report, progress, settings): with the same number the first
            # one's report would end them all.
            here = os.path.normcase(os.path.abspath(folder))
            used = {j["priority"] for q in self.queens if q.solo and q.dispatch for j in q.dispatch.jobs
                    if os.path.normcase(os.path.abspath(j.get("project_path") or "")) == here}
            queen.first_priority = next(k for k in range(1, len(used) + 2) if k not in used)
            self.queens.append(queen)
            self._save()
        acceptance = [{"type": "command", "run": check.strip(), "trusted": True}] if check.strip() else []
        r = queen.add_worker(goal, folder, agent, model or "default", None, acceptance, "", (name or "").strip()[:40] or "Free worker", True, True)
        if not r.get("ok"):
            return r
        queen.dispatch.save()
        return {"ok": True, "queen": queen.queen}

    @staticmethod
    def _stop_workers(q: "Mother") -> int:
        """Stop every worker of a queen that has not ended. Returns how many."""
        d = q.dispatch
        if d is None:
            return 0
        n = 0
        for j in d.view():
            if j["status"] not in d.ENDED:
                d.kill(j["priority"])
                n += 1
        return n

    def remove(self, queen: str, force: bool = False) -> dict[str, Any]:
        """Take a queen off the map. `force` stops her workers first (the window asks the user)."""
        with self.lock:
            q = next((q for q in self.queens if q.queen == queen), None)
            if q is None:
                return {"ok": False, "error": "no such queen"}
            if q.planning or q.preparing or q.commanding:
                return {"ok": False, "error": f"{q.name} is still planning; try again in a minute"}
            if q.dispatch is not None and not q.dispatch.done.is_set():
                if not force:
                    return {"ok": False, "error": f"{q.name} is still at work"}
                self._stop_workers(q)
            _archive_plan(q.queen)
            if q.director is not None:
                q.director.stop()                          # her look-ins end with her
            if q.beekeeper is not None:
                q.beekeeper.stop()
            q._drop_queen_terminal()                       # the chat with her ends with her
            if len(self.queens) == 1:
                # The last queen cannot leave an empty map: she goes back to a blank one.
                q.dispatch, q.plan, q.prepared = None, None, None
                q.commands, q.approvals, q._cost = [], [], {}
                q.name, q.description, q.agent, q.model = f"Queen {q.queen[1:]}", "", "", ""
                q.limits, q.roles, q.director_on, q.director = {}, {}, False, None
                from . import config as _config
                q.cfg = _config.capped(_config.load())                       # her roles went with her (TODO 6)
                q.beekeeper, q._hive_written = None, ("", 0.0)
                q.manual_approvals, q.solo, q.manual_delay_s = False, False, 0.0    # a cleaned free worker is a queen again
            else:
                was_first = q is self.queens[0]
                self.queens.remove(q)
                if was_first:
                    self.queens[0].primary = True          # the next queen takes the first place
            self._save()
            return {"ok": True}

    def reset(self, queen: str, force: bool = False) -> dict[str, Any]:
        """A clean start for a queen that stays (the first): its plan and
        workers are forgotten, the plan file kept aside, not deleted. `force` stops its workers first."""
        with self.lock:
            q = next((q for q in self.queens if q.queen == queen), None)
            if q is None:
                return {"ok": False, "error": "no such queen"}
            if q.planning or q.preparing or q.commanding:
                return {"ok": False, "error": f"{q.name} is still planning; try again in a minute"}
            if q.dispatch is not None and not q.dispatch.done.is_set():
                if not force:
                    return {"ok": False, "error": f"{q.name} is still at work; stop its workers first"}
                self._stop_workers(q)
            q.dispatch, q.plan, q.prepared = None, None, None
            q.commands, q.approvals, q._cost = [], [], {}
            q._drop_queen_terminal()                       # a clean start: the old chat goes too
            _archive_plan(q.queen)
            return {"ok": True}

    def resume(self, queen: str, go: bool) -> dict[str, Any]:
        q = self.get(queen)
        return q.resume(go) if q else {"ok": False, "error": "no such queen"}

    def describe(self, queen: str, text: str) -> dict[str, Any]:
        """The task the user gave this queen, in their words: kept with her, shown in her panel, read by her."""
        with self.lock:
            q = next((q for q in self.queens if q.queen == queen), None)
            if q is None:
                return {"ok": False, "error": "no such queen"}
            q.description = (text or "").strip()[:4000]
            self._save()
            return {"ok": True}

    def set_director(self, queen: str, model: str) -> dict[str, Any]:
        q = self.get(queen)
        result = q.set_director(model) if q else {"ok": False, "error": "no such queen"}
        if result.get("ok"):
            with self.lock:
                self._save()
        return result

    def set_worker_models(self, queen: str, claude: str, codex: str) -> dict[str, Any]:
        q = self.get(queen)
        result = q.set_worker_models(claude, codex) if q else {"ok": False, "error": "no such queen"}
        if result.get("ok"):
            with self.lock:
                self._save()
        return result

    def rename(self, queen: str, name: str) -> dict[str, Any]:
        with self.lock:
            q = next((q for q in self.queens if q.queen == queen), None)
            if q is None or not name.strip():
                return {"ok": False, "error": "no such queen"}
            q.name = name.strip()[:40]
            if q.dispatch is not None:
                q.dispatch.queen_name = q.name
            self._save()
            return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    host: HostClient
    hive: Optional[Hive] = None
    protocol_version = "HTTP/1.1"
    server_version = "QueenBeeMother"

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    token: Optional[str] = None          # set by serve(); a request to /api/ without it is refused (auth.py)

    def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        # Same rule as QueenBee: only a page served from this machine, by name.
        if not _local(self.headers.get("Host", "")):
            self._send(b'{"ok":false}', "application/json", 403)
            return
        from urllib.parse import parse_qs

        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query)
        if path == "/api/ping":                       # the one open route: is a server alive on this port?
            self._send(b'{"ok":true}', "application/json")
            return
        if path.startswith("/api/") and not auth.allowed(self.token, self.headers, query):
            self._send(b'{"ok":false,"error":"unauthorized: this is QueenBee\'s local API; open the page from the app"}',
                       "application/json", 401)
            return
        mother = self.hive.get((query.get("queen") or [""])[0]) if self.hive else None
        if path == "/api/screen" and mother is not None and (query.get("job") or [""])[0].isdigit():
            self._send(json.dumps(mother.job_screen(int(query["job"][0]))).encode(), "application/json")
            return
        if path == "/api/changes" and mother is not None:
            self._send(json.dumps(mother.changes(), default=str).encode(), "application/json")
            return
        if path == "/api/findings" and mother is not None:
            self._send(json.dumps(mother.finding_records(), default=str).encode(), "application/json")
            return
        if path == "/api/card" and mother is not None:
            self._send(json.dumps({"ok": True, "text": mother.result_card()}).encode(), "application/json")
            return
        if path == "/api/agents":
            from ..agents.catalog import survey

            self._send(json.dumps({"ok": True, "agents": survey()}).encode(), "application/json")
            return
        if path == "/api/timeline" and mother is not None:
            self._send(json.dumps(mother.timeline(), default=str).encode(), "application/json")
            return
        if path == "/api/conversation" and mother is not None and (query.get("job") or [""])[0].isdigit():
            self._send(json.dumps(mother.job_conversation(int(query["job"][0])), default=str).encode(),
                       "application/json")
            return
        if path == "/api/conversation" and mother is not None:
            term = (query.get("terminal") or [""])[0]
            if not re.fullmatch(r"term-\d+", term):
                self._send(b'{"ok":false,"error":"bad terminal"}', "application/json", 400)
                return
            self._send(json.dumps(mother.conversation(term), default=str).encode(),
                       "application/json")
            return
        if path == "/api/state":
            state = cached_job_view(self.host)
            if mother is not None:
                state["mother"] = mother.view()
                state["queens"] = [q.summary() for q in self.hive.queens]
            self._send(json.dumps(state, default=str).encode(), "application/json")
            return
        if path == "/api/terminal/ws":
            info = self.host.ws_info() if hasattr(self.host, "ws_info") else None
            self._send(json.dumps({"ok": bool(info), "ws": info}).encode(), "application/json")
            return
        if path == "/api/terminal/read":
            term = (query.get("id") or [""])[0]
            if not self._workers_terminal(term):
                self._send(b'{"ok":false,"error":"not a worker terminal"}', "application/json", 403)
                return
            try:
                since = max(0, int((query.get("since") or ["0"])[0]))
                result = self.host._call("terminal_read", term, since)
            except (ValueError, HostUnavailable) as exc:
                result = {"ok": False, "error": str(exc)}
            self._send(json.dumps(result, default=str).encode(), "application/json")
            return
        name = FILES.get(path)
        if name is None:
            self._send(b"not found", "text/plain", 404)
            return
        ctype = mimetypes.guess_type(str(name))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        body = name.read_bytes()
        if path in ("/", "/index.html") and self.token and (query.get("k") or [""])[0] == self.token:
            # The app opened this page with the token in its URL: hand it to the page once; the page keeps it for the
            # session and drops it from the address bar. Any other way of asking for the page gets it without a token.
            tag = ("<script>window.QB_TOKEN=" + json.dumps(self.token) + ";</script>").encode()
            body = body.replace(b"<head>", b"<head>" + tag, 1) if b"<head>" in body else tag + body
        self._send(body, ctype)

    def _workers_terminal(self, term: str) -> bool:
        """Only the terminals a queen's workers run in, never any other."""
        if not re.fullmatch(r"term-\d+", term or "") or self.hive is None:
            return False
        for q in self.hive.queens:
            if q.queen_term and q.queen_term.get("id") == term:
                return True
            for j in q.dispatch.view() if q.dispatch else []:
                if term in (j.get("terminal"), j.get("workload")):
                    return True
        return False

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
        except (ValueError, OSError):
            self._send(b'{"ok":false,"error":"bad request"}', "application/json", 400)
            return
        if (not _local(self.headers.get("Host", "")) or not _local_origin(self.headers.get("Origin", ""))
                or "application/json" not in (self.headers.get("Content-Type") or "")):
            self._send(b'{"ok":false,"error":"forbidden"}', "application/json", 403)
            return
        if not auth.allowed(self.token, self.headers):
            self._send(b'{"ok":false,"error":"unauthorized: this is QueenBee\'s local API; open the page from the app"}',
                       "application/json", 401)
            return
        if self.hive is None:
            self._send(b'{"ok":false,"error":"read-only"}', "application/json", 501)
            return
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._send(b'{"ok":false,"error":"bad JSON"}', "application/json", 400)
            return
        path = urlsplit(self.path).path
        mother = self.hive.get(str(body.get("queen") or ""))
        if path == "/api/hook":
            # A worker's Claude Code hook asks before a tool call. The worker is found by its folder; no window is involved.
            cwd = os.path.normcase(os.path.abspath(str(body.get("cwd") or "")))
            answer = {"ok": False, "decision": "ask", "reason": "no worker in that folder"}
            for q in self.hive.queens:
                d = q.dispatch
                for j in (d.jobs if d else []):
                    root = os.path.normcase(os.path.abspath(str(j.get("project_path") or "")))
                    if root and (cwd == root or cwd.startswith(root + os.sep)) and j.get("priority") == int(body.get("job") or 0):
                        answer = q.hook_decision(j["priority"], cwd, str(body.get("tool_name") or ""),
                                                 body.get("tool_input") if isinstance(body.get("tool_input"), dict) else {})
                        break
                if answer.get("ok"):
                    break
            self._send(json.dumps(answer).encode(), "application/json")
            return
        if path == "/api/queen":
            action = body.get("action")
            limits = body.get("limits") if isinstance(body.get("limits"), dict) else None
            roles = body.get("roles") if isinstance(body.get("roles"), dict) else None
            result = (self.hive.add(str(body.get("name") or ""), str(body.get("agent") or ""),
                                    str(body.get("model") or ""), limits, roles,
                                    str(body.get("description") or ""),
                                    bool(body.get("director"))) if action == "add"
                      else self.hive.limit(str(body.get("queen") or ""), limits or {}) if action == "limits"
                      else self.hive.set_answers(str(body.get("queen") or ""), str(body.get("mode") or ""))
                      if action == "answers"
                      else self.hive.reset(str(body.get("queen") or ""), bool(body.get("force"))) if action == "reset"
                      else self.hive.remove(str(body.get("queen") or ""), bool(body.get("force"))) if action == "remove"
                      else self.hive.set_director(str(body.get("queen") or ""), str(body.get("model") or ""))
                      if action == "director"
                      else self.hive.add_free_worker(str(body.get("name") or ""), str(body.get("agent") or ""),
                                                     str(body.get("model") or ""), str(body.get("folder") or ""),
                                                     str(body.get("goal") or ""), str(body.get("check") or ""),
                                                     float(body.get("delay") or 0))
                      if action == "free"
                      else self.hive.set_worker_models(str(body.get("queen") or ""), str(body.get("claude") or ""),
                                                       str(body.get("codex") or ""))
                      if action == "workers"
                      else self.hive.describe(str(body.get("queen") or ""), str(body.get("text") or ""))
                      if action == "describe"
                      else self.hive.rename(str(body.get("queen") or ""), str(body.get("name") or ""))
                      if action == "rename"
                      else self.hive.resume(str(body.get("queen") or ""), bool(body.get("go")))
                      if action == "resume" else {"ok": False, "error": "unknown action"})
        elif path == "/api/worker":
            parent = body.get("parent")
            result = mother.add_worker(str(body.get("goal") or ""), str(body.get("folder") or "").strip(),
                                       str(body.get("agent") or ""), str(body.get("model") or ""),
                                       int(parent) if isinstance(parent, int) else None,
                                       body.get("acceptance") if isinstance(body.get("acceptance"), list) else None,
                                       str(body.get("instruction") or ""), str(body.get("title") or ""))
            if result.get("ok"):
                mother.dispatch.save()
        elif path == "/api/demo":
            result = self.hive.demo(str(body.get("kind") or "notes"))
        elif path == "/api/verify":
            result = mother.set_final_check(str(body.get("command") or ""))
            if result.get("ok") and mother.dispatch:
                mother.dispatch.save()
        elif path == "/api/worker/remove":
            try:
                priority = int(body.get("priority"))
            except (TypeError, ValueError):
                priority = -1
            d = mother.dispatch
            result = d.remove_job(priority) if d else {"ok": False, "error": "this queen has no workers"}
            if result.get("ok"):
                d.save()
                # A free worker's container has no reason to stay once its one agent is gone (unless it is the last entry).
                if mother.solo and not [j for j in d.view() if j.get("kind") == "task"] and len(self.hive.queens) > 1:
                    self.hive.remove(mother.queen, force=True)
        elif path == "/api/kill":
            try:
                priority = int(body.get("priority"))
            except (TypeError, ValueError):
                priority = -1
            d = mother.dispatch
            result = d.kill(priority) if d else {"ok": False, "error": "this queen has no workers"}
            if result.get("ok"):
                d.save()
        elif path == "/api/finding":
            result = mother.settle_finding(str(body.get("folder") or ""), str(body.get("id") or ""), str(body.get("action") or ""))
        elif path == "/api/refusal":
            try:
                priority = int(body.get("priority"))
            except (TypeError, ValueError):
                priority = -1
            result = mother.settle_refusal(priority, str(body.get("call") or ""), bool(body.get("allow")))
            if result.get("ok"):
                mother.dispatch.save()
        elif path == "/api/beekeeper":
            result = (mother.set_beekeeper(str(body.get("agent") or ""), str(body.get("model") or ""), str(body.get("rules") or ""))
                      if body.get("action") == "set" else mother.ask_beekeeper())
            if body.get("action") == "set" and result.get("ok"):
                with self.hive.lock:
                    self.hive._save()
        elif path == "/api/queen/terminal":
            result = mother.open_queen_terminal(str(body.get("folder") or ""))
        elif path == "/api/agent/setup":
            result = agent_setup(str(body.get("agent") or ""), str(body.get("action") or ""))
        elif path == "/api/chat":
            from . import chat

            result = chat.reply(body.get("messages"), str(body.get("folder") or ""),
                                mother.roles.get("strong_model") or mother.cfg.get("strong_model") or "sonnet",
                                hive=mother.hive_text(), agent=mother.roles.get("director_agent") or "claude",
                                codex_model=mother.roles.get("director_model", ""))
        elif path == "/api/command":
            result = mother.command(str(body.get("instruction") or ""), str(body.get("folder") or ""),
                                    str(body.get("strategy") or "multi"), int(body.get("max_workers") or 0),
                                    body.get("layout") if body.get("layout") in ("peers", "branches") else "team",
                                    bool(body.get("qa")))
        elif path in ("/api/terminal/write", "/api/terminal/resize"):
            term = str(body.get("id") or "")
            if not self._workers_terminal(term):
                result = {"ok": False, "error": "not a worker terminal"}
            else:
                try:
                    result = (self.host._call("terminal_write", term, str(body.get("data") or ""))
                              if path.endswith("write") else
                              self.host._call("terminal_resize", term, int(body.get("cols") or 120),
                                              int(body.get("rows") or 32)))
                except (ValueError, HostUnavailable) as exc:
                    result = {"ok": False, "error": str(exc)}
        elif path == "/api/plan":
            result = mother.make_plan(str(body.get("instruction") or ""),
                                           str(body.get("folder") or ""))
        elif path == "/api/prepare":
            result = mother.prepare(list(body.get("jobs") or []), str(body.get("plan_id") or ""))
        elif path == "/api/say":
            d = mother.dispatch
            result = d.say(body.get("to") or "all", str(body.get("text") or ""),
                           "user" if body.get("as") == "user" else "queen") if d else                 {"ok": False, "error": "no plan is running"}
        elif path == "/api/job":
            try:
                priority = int(body.get("priority"))
            except (TypeError, ValueError):
                priority = -1
            d = mother.dispatch
            result = d.set_job(priority, dict(body.get("changes") or {})) if d else \
                {"ok": False, "error": "no plan is running"}
            if result.get("ok") and d:
                d.save()
        elif path == "/api/start":
            answers = body.get("answers") if isinstance(body.get("answers"), dict) else {}
            result = mother.start(list(body.get("jobs") or []),
                                       {str(k): str(v) for k, v in answers.items()},
                                       str(body.get("plan_id") or ""))
        else:
            self._send(b'{"ok":false,"error":"not found"}', "application/json", 404)
            return
        self._send(json.dumps(result, default=str).encode(), "application/json")


def serve(port: int, host_port: int, *, with_mother: bool = False, ui_port: int = 7878) -> ThreadingHTTPServer:
    host = HostClient(host_port)
    attrs: dict[str, Any] = {"host": host}
    if with_mother:
        attrs["hive"] = Hive(host, ui_port)
    attrs["token"] = auth.make_token(port)
    handler = type("BoundMotherHandler", (Handler,), attrs)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    if port == 0:                                   # an ephemeral port (tests): the token file is named after the real one
        handler.token = auth.make_token(httpd.server_port)
    httpd.token = handler.token                     # type: ignore[attr-defined]
    return httpd
