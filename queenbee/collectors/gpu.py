"""GPU telemetry, split into two independent questions.

  1. Device-level: how loaded is the GPU right now?  (nvidia-smi, everywhere)
  2. Process-level: which PID is holding the VRAM?   (platform-dependent)

Question 2 is the one that matters for agent attribution and it is *not*
uniformly answerable. `nvidia-smi --query-compute-apps=used_memory` returns
"[N/A]" for every process on Windows consumer GPUs, because per-process VRAM
accounting is unavailable under the WDDM driver model. So on Windows we read
the same PDH performance counters Task Manager uses, which do expose it.

Backends degrade in this order, and `attributable` says which rung we are on:
    Linux/NVIDIA  -> nvidia-smi compute-apps      (exact, compute contexts)
    Windows/NVIDIA-> PDH \\GPU Process Memory     (exact, all GPU clients)
    anything else -> device totals only           (attributable = False)
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import threading
from typing import Optional

from ..model import GpuSample

log = logging.getLogger("queenbee.gpu")

_SMI_TIMEOUT_S = 6.0
_BACKGROUND_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_PID_INSTANCE_RE = re.compile(r"^pid_(\d+)_")


def _to_float(text: str) -> Optional[float]:
    text = text.strip()
    if not text or text.startswith("["):  # [N/A], [Insufficient Permissions]
        return None
    try:
        return float(text)
    except ValueError:
        return None


class ProcessGpuBackend:
    """Maps PID -> (vram_mb, gpu_util_pct). Either value may be None."""

    attributable = False
    name = "none"

    def sample(self) -> dict[int, tuple[Optional[float], Optional[float]]]:
        return {}

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# Device level
# --------------------------------------------------------------------------

_GPU_FIELDS = (
    "index",
    "name",
    "utilization.gpu",
    "memory.used",
    "memory.total",
    "temperature.gpu",
    "power.draw",
    "power.limit",
    "uuid",
)


class NvidiaSmiDevices:
    name = "nvidia-smi"

    def __init__(self) -> None:
        self.available = shutil.which("nvidia-smi") is not None

    def sample(self) -> list[GpuSample]:
        if not self.available:
            return []
        try:
            proc = subprocess.run(
                [
                    "nvidia-smi",
                    f"--query-gpu={','.join(_GPU_FIELDS)}",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                creationflags=_BACKGROUND_FLAGS,
                timeout=_SMI_TIMEOUT_S,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if proc.returncode != 0:
            return []

        gpus: list[GpuSample] = []
        for line in (proc.stdout or "").strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < len(_GPU_FIELDS):
                continue
            index = _to_float(parts[0])
            gpus.append(
                GpuSample(
                    index=int(index) if index is not None else len(gpus),
                    name=parts[1],
                    util_pct=_to_float(parts[2]),
                    mem_used_mb=_to_float(parts[3]),
                    mem_total_mb=_to_float(parts[4]),
                    temp_c=_to_float(parts[5]),
                    power_w=_to_float(parts[6]),
                    power_limit_w=_to_float(parts[7]),
                    uuid=parts[8],
                )
            )
        return gpus


# --------------------------------------------------------------------------
# Process level -- NVIDIA compute apps (works on Linux, not on Windows WDDM)
# --------------------------------------------------------------------------


class NvidiaComputeApps(ProcessGpuBackend):
    name = "nvidia-smi compute-apps"

    def __init__(self) -> None:
        self.attributable = self._probe()

    def _query(self) -> list[list[str]]:
        try:
            proc = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-compute-apps=pid,used_memory",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                creationflags=_BACKGROUND_FLAGS,
                timeout=_SMI_TIMEOUT_S,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if proc.returncode != 0:
            return []
        return [
            [p.strip() for p in line.split(",")]
            for line in (proc.stdout or "").strip().splitlines()
            if line.strip()
        ]

    def _probe(self) -> bool:
        """True only if the driver actually reports per-process memory.

        An empty GPU is not proof of failure, so "no rows" counts as usable.
        """
        if shutil.which("nvidia-smi") is None:
            return False
        rows = self._query()
        if not rows:
            return True
        return any(len(r) > 1 and _to_float(r[1]) is not None for r in rows)

    def sample(self) -> dict[int, tuple[Optional[float], Optional[float]]]:
        out: dict[int, tuple[Optional[float], Optional[float]]] = {}
        for row in self._query():
            if len(row) < 2:
                continue
            pid = _to_float(row[0])
            if pid is None:
                continue
            out[int(pid)] = (_to_float(row[1]), None)
        return out


# --------------------------------------------------------------------------
# Process level -- Windows PDH counters (what Task Manager reads)
# --------------------------------------------------------------------------

# One long-lived PowerShell child, because spawning Get-Counter per tick costs
# ~1s of process startup. It prints one compact JSON object per sample.
_PDH_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$paths = @('\GPU Process Memory(*)\Dedicated Usage', '\GPU Engine(*)\Utilization Percentage')
$owner = __OWNER_PID__
while ($true) {
    # Exit with the daemon. Closing stdout is not enough on Windows: when the
    # parent is killed hard this loop keeps running forever, and every crashed
    # QueenBee leaves another Get-Counter sampler behind.
    if (-not (Get-Process -Id $owner -ErrorAction SilentlyContinue)) { exit 0 }
    $mem = @{}
    $eng = @{}
    try {
        foreach ($s in (Get-Counter -Counter $paths).CounterSamples) {
            if ($s.InstanceName -notmatch '^pid_(\d+)_') { continue }
            $pid_ = $Matches[1]
            if ($s.Path -like '*gpu process memory*') {
                $mem[$pid_] = [double]$mem[$pid_] + $s.CookedValue
            } else {
                $eng[$pid_] = [double]$eng[$pid_] + $s.CookedValue
            }
        }
    } catch { }
    [pscustomobject]@{ mem = $mem; eng = $eng } | ConvertTo-Json -Compress -Depth 3
    [Console]::Out.Flush()
    Start-Sleep -Milliseconds __INTERVAL_MS__
}
"""


class WindowsPdhGpu(ProcessGpuBackend):
    name = "windows pdh"

    def __init__(self, interval_ms: int = 2000) -> None:
        self.attributable = False
        self._latest: dict[int, tuple[Optional[float], Optional[float]]] = {}
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen[str]] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = False

        exe = shutil.which("powershell") or shutil.which("pwsh")
        if exe is None:
            return
        script = _PDH_SCRIPT.replace(
            "__INTERVAL_MS__", str(max(500, interval_ms))
        ).replace("__OWNER_PID__", str(os.getpid()))
        try:
            self._proc = subprocess.Popen(
                [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-Command", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_BACKGROUND_FLAGS,
            )
        except OSError:
            self._proc = None
            return

        self._thread = threading.Thread(target=self._pump, name="queenbee-pdh", daemon=True)
        self._thread.start()
        self.attributable = True

    def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            if self._stopping:
                return
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            parsed: dict[int, tuple[Optional[float], Optional[float]]] = {}
            mem = payload.get("mem") or {}
            eng = payload.get("eng") or {}
            for pid_text, value in mem.items():
                try:
                    parsed[int(pid_text)] = (float(value) / (1024 * 1024), None)
                except (TypeError, ValueError):
                    continue
            for pid_text, value in eng.items():
                try:
                    pid = int(pid_text)
                    util = float(value)
                except (TypeError, ValueError):
                    continue
                vram = parsed.get(pid, (None, None))[0]
                parsed[pid] = (vram, util)
            with self._lock:
                self._latest = parsed

    def sample(self) -> dict[int, tuple[Optional[float], Optional[float]]]:
        with self._lock:
            return dict(self._latest)

    def close(self) -> None:
        self._stopping = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()


# --------------------------------------------------------------------------


def select_process_backend(interval_s: float = 3.0) -> tuple[ProcessGpuBackend, list[str]]:
    """Pick the best per-process GPU backend available, with an explanation."""
    notes: list[str] = []
    has_nvidia = shutil.which("nvidia-smi") is not None

    # On Windows/WDDM, nvidia-smi may return no compute rows while the card is
    # idle and look usable during the probe, then report N/A once CUDA starts.
    # The process silently disappears from attribution at exactly the moment
    # it matters.  PDH is the authoritative per-process source on Windows, so
    # choose it first rather than treating it as a fallback.
    if platform.system() == "Windows":
        pdh = WindowsPdhGpu(interval_ms=int(interval_s * 1000))
        if pdh.attributable:
            notes.append("per-process VRAM via Windows PDH GPU counters")
            return pdh, notes
        pdh.close()
        notes.append("Windows PDH GPU counters unavailable")

    if has_nvidia:
        smi = NvidiaComputeApps()
        if smi.attributable:
            return smi, notes
        notes.append(
            "nvidia-smi reports no per-process VRAM on this driver "
            "(expected on Windows/WDDM consumer GPUs)"
        )

    if not has_nvidia:
        notes.append("no NVIDIA GPU detected; GPU rows disabled")
    else:
        notes.append("per-process GPU attribution unavailable; showing device totals only")
    return ProcessGpuBackend(), notes
