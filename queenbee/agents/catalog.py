"""Which agent CLIs exist on this machine, and whether they are usable.

QueenBee can only drive an agent that exposes a structured mode, and only once
that agent is signed in. Both facts are discoverable, and telling the user
exactly which one is missing is more useful than a start button that fails.

On signing in: an agent's login is an interactive terminal flow -- a menu, a
browser round trip, a pasted code. The structured `--print` mode QueenBee uses has
no such screen, so QueenBee cannot perform a login itself. It detects the state
and names the one command to run. (Hosting that flow would need a PTY backend;
it is the single thing a PTY buys that the structured stream cannot.)
"""

from __future__ import annotations

import glob
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class AgentSpec:
    id: str
    label: str
    binaries: tuple[str, ...]
    # Extra places to look when the binary is not on PATH.
    extra_globs: tuple[str, ...] = ()
    credentials: tuple[str, ...] = ()
    login_command: str = ""
    # False for agents QueenBee knows of but cannot drive yet.
    supported: bool = True
    note: str = ""
    # What the "Get started" panel offers: one command that installs the agent (run in a visible terminal, after the
    # user has seen it and said yes), what it needs on the machine, and what to do to sign in.
    install_command: str = ""
    needs: str = ""
    login_hint: str = ""


def _home(*parts: str) -> str:
    return str(Path.home().joinpath(*parts))


def _appdata(*parts: str) -> str:
    base = os.environ.get("APPDATA")
    return str(Path(base).joinpath(*parts)) if base else ""


def _local_appdata(*parts: str) -> str:
    base = os.environ.get("LOCALAPPDATA")
    return str(Path(base).joinpath(*parts)) if base else ""


WINDOWS = os.name == "nt"


def _posix_bins(name: str) -> tuple[str, ...]:
    """Where macOS and Linux installers put a CLI when it is not on the PATH QueenBee was started with (an app launched from
    the Dock or a desktop file sees a shorter PATH than a shell does)."""
    return (_home(".local", "bin", name), "/opt/homebrew/bin/" + name, "/usr/local/bin/" + name,
            _home(".npm-global", "bin", name), _home(".volta", "bin", name), _home(".nvm", "versions", "node", "*", "bin", name))


def _install(windows: str, posix: str) -> str:
    """The vendor's own install line for this platform (the macOS/Linux ones follow each vendor's documented curl | bash form;
    not yet run on a real Mac here)."""
    return windows if WINDOWS else posix


CATALOG: list[AgentSpec] = [
    AgentSpec(
        id="claude",
        label="Claude Code",
        binaries=("claude",),
        extra_globs=(
            _appdata("Claude", "claude-code", "*", "claude.exe"),
            _appdata("Claude", "claude-code", "*", "*", "claude.exe"),       # the layout with a build folder under the version
            _home(".claude", "local", "*", "claude"),
            *_posix_bins("claude"),
        ),
        credentials=(_home(".claude", ".credentials.json"),),
        login_command="claude",
        install_command=_install("irm https://claude.ai/install.ps1 | iex", "curl -fsSL https://claude.ai/install.sh | bash"),
        login_hint="In the terminal that opens, type /login and follow the browser steps.",
    ),
    AgentSpec(
        id="codex",
        label="Codex",
        binaries=("codex",),
        # winget installs Codex outside PATH: an "app execution alias" in
        # WindowsApps and the real binary under its package directory.
        extra_globs=(
            _local_appdata("Microsoft", "WinGet", "Packages", "OpenAI.Codex_*",
                           "codex-*-windows-msvc.exe"),
            _local_appdata("Microsoft", "WinGet", "Links", "codex.exe"),
            *_posix_bins("codex"),
        ),
        credentials=(_home(".codex", "auth.json"),),
        login_command="codex login",
        supported=False,
        note="runs in a terminal; no structured-stream adapter yet",
        install_command="npm install -g @openai/codex",
        needs="npm",
        login_hint="Follow the browser steps that `codex login` opens.",
    ),
    AgentSpec(
        id="gemini",
        label="Gemini",
        binaries=("gemini",),
        credentials=(_home(".gemini", "oauth_creds.json"),),
        login_command="gemini",
        note="experimental: not tried here with a live account",
        install_command="npm install -g @google/gemini-cli",
        needs="npm",
        login_hint='In the terminal that opens, choose "Login with Google" (or set GEMINI_API_KEY).',
    ),
    AgentSpec(
        id="kimi",
        label="Kimi",
        binaries=("kimi",),
        login_command="kimi login",
        note="experimental: not tried here with a live account",
        install_command=_install("irm https://code.kimi.com/install.ps1 | iex", "curl -fsSL https://code.kimi.com/install.sh | bash"),
        login_hint="Follow the browser steps that `kimi login` opens.",
    ),
    AgentSpec(
        id="opencode",
        label="OpenCode",
        binaries=("opencode",),
        credentials=(_home(".local", "share", "opencode", "auth.json"),),
        login_command="opencode auth login",
        note="experimental: not tried here with a live account",
        install_command="npm install -g opencode-ai",
        needs="npm",
        login_hint="Pick a provider in the terminal that opens and follow its steps.",
    ),
    AgentSpec(
        id="cursor",
        label="Cursor Agent",
        binaries=("cursor-agent",),
        login_command="cursor-agent login",
        note="experimental: not tried here with a live account",
        install_command=_install("irm 'https://cursor.com/install?win32=true' | iex", "curl https://cursor.com/install -fsS | bash"),
        login_hint="Follow the browser steps that `cursor-agent login` opens.",
    ),
    AgentSpec(
        id="mistral",
        label="Mistral Vibe",
        binaries=("vibe",),
        credentials=(_home(".vibe", ".env"),),
        login_command="vibe --setup",
        note="experimental: not tried here with a live account",
        install_command="uv tool install mistral-vibe",
        needs="uv",
        login_hint="Paste your Mistral API key when it asks; it is kept in ~/.vibe/.env.",
    ),
    AgentSpec(
        id="grok",
        label="Grok Build",
        binaries=("grok",),
        extra_globs=(_home(".grok", "bin", "grok.exe"), _home(".grok", "bin", "grok")),          # where its own installer puts it, off PATH
        login_command="grok",
        note="experimental: not tried here with a live account",
        install_command=_install("irm https://x.ai/cli/install.ps1 | iex", "curl -fsSL https://x.ai/cli/install.sh | bash"),
        login_hint="Grok opens a browser to sign in the first time it starts (it needs an eligible xAI plan).",
    ),
]


def _find_binary(spec: AgentSpec) -> Optional[str]:
    # The desktop Codex bundle carries required companion executables such as
    # codex-code-mode-host.exe beside codex.exe.  A stale Winget alias may
    # still be first on PATH after the desktop app updates; choosing it starts
    # Codex but leaves Code Mode half-installed.  Prefer the newest complete
    # desktop bundle when it exists.
    if spec.id == "codex":
        configured = os.environ.get("CODEX_CLI_PATH")
        candidates: list[Path] = []
        if configured:
            candidates.append(Path(configured))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.extend(Path(local).glob("OpenAI/Codex/bin/*/codex.exe"))
        complete = [
            p for p in candidates
            if p.is_file() and p.with_name("codex-code-mode-host.exe").is_file()
        ]
        if complete:
            return str(max(complete, key=lambda p: p.stat().st_mtime))
    if spec.id == "claude":
        return _newest_claude(spec)
    for name in spec.binaries:
        found = shutil.which(name)
        if found:
            return found
    # Agents bundled by a desktop app are not on PATH; pick the newest version
    # directory rather than whichever the filesystem lists first.
    best: Optional[Path] = None
    for pattern in spec.extra_globs:
        if not pattern:
            continue
        root = Path(pattern)
        try:
            matches = sorted(Path(root.anchor).glob(str(root.relative_to(root.anchor))))
        except (ValueError, OSError):
            continue
        for candidate in matches:
            if candidate.is_file() and (best is None or candidate.parent.name > best.parent.name):
                best = candidate
    return str(best) if best else None


_VERSIONS: dict[tuple[str, float], tuple[int, ...]] = {}


def _claude_version(path: str) -> tuple[int, ...]:
    """The version a Claude Code executable reports, e.g. (2, 1, 293); asked once per file (a new version is a new file)."""
    import re
    import subprocess

    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return (0,)
    if key not in _VERSIONS:
        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
            _VERSIONS[key] = tuple(int(x) for x in m.groups()) if m else (0,)
        except (OSError, subprocess.SubprocessError):
            _VERSIONS[key] = (0,)
    return _VERSIONS[key]


def _newest_claude(spec: AgentSpec) -> Optional[str]:
    """Claude Code can be installed twice: on PATH, and inside the Claude desktop app, which is updated separately. They resolve
    the model names (haiku, sonnet, opus) to different releases, so the newest of them is the one to run."""
    found: list[str] = []
    for name in spec.binaries:
        on_path = shutil.which(name)
        if on_path:
            found.append(on_path)
    for pattern in spec.extra_globs:
        if not pattern:
            continue
        try:                                            # a plain glob: the anchor-relative Path.glob used before missed installs on some setups
            found += [c for c in sorted(glob.glob(pattern)) if os.path.isfile(c)]
        except (ValueError, OSError):
            continue
    unique = list(dict.fromkeys(found))
    if len(unique) <= 1:
        return unique[0] if unique else None
    return max(unique, key=lambda p: (_claude_version(p), unique.index(p) == 0))


def _signed_in(spec: AgentSpec) -> Optional[bool]:
    """True/False when we can tell, None when the agent stores creds elsewhere. Only the file's size is looked at: QueenBee
    never opens an agent's credentials (a vendor's sign-in is the vendor's own)."""
    if not spec.credentials:
        return None
    for path in spec.credentials:
        p = Path(path)
        try:
            # A logged-out shell is "{}" or a line of whitespace; any real session or key is far longer.
            if p.is_file() and p.stat().st_size > 16:
                return True
        except OSError:
            continue
    return False


def survey() -> list[dict[str, Any]]:
    """What each known agent looks like right now."""
    out: list[dict[str, Any]] = []
    for spec in CATALOG:
        binary = _find_binary(spec)
        signed = _signed_in(spec) if binary else None
        if not binary:
            status, hint = "missing", f"{spec.label} is not installed"
        elif signed is False and not (spec.id == "gemini" and os.environ.get("GEMINI_API_KEY")):
            # Before "unsupported": an agent QueenBee can only run in a terminal
            # still needs signing in first, and that is the useful next step.
            status, hint = "signed_out", f"run `{spec.login_command}` once in a terminal to sign in"
        elif not spec.supported:
            status, hint = "unsupported", spec.note
        else:
            status, hint = "ready", ""
        out.append({
            "id": spec.id,
            "label": spec.label,
            "binary": binary,
            "signed_in": signed,
            "supported": spec.supported,
            "login_command": spec.login_command,
            "install_command": spec.install_command,
            "needs": spec.needs,
            "needs_ok": (not spec.needs) or bool(shutil.which(spec.needs)),
            "login_hint": spec.login_hint,
            "note": spec.note,
            "status": status,
            "hint": hint,
            # Anything installed can be run in a QueenBee terminal, whether or not
            # QueenBee can drive its structured stream.
            "terminal_ok": bool(binary),
        })
    return out


def binary_for(agent_id: str) -> Optional[str]:
    """The executable to run for an agent id, wherever it was installed."""
    for spec in CATALOG:
        if spec.id == agent_id:
            return _find_binary(spec)
    return None
