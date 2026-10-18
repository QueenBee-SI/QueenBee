"""Machine-wide telemetry: CPU, RAM, swap, disk, network, battery."""

from __future__ import annotations

import time
from typing import Optional

import psutil

from ..model import SystemSample
from .gpu import NvidiaSmiDevices

_MB = 1024 * 1024


class SystemCollector:
    def __init__(self) -> None:
        self._gpus = NvidiaSmiDevices()
        self._cores = psutil.cpu_count(logical=True) or 0
        self._last_ts: Optional[float] = None
        self._last_disk = None
        self._last_net = None
        # Prime the CPU counter so the first real sample is not a bogus 0.0.
        psutil.cpu_percent(interval=None)

    @property
    def gpu_available(self) -> bool:
        return self._gpus.available

    def _rate(self, now: float, prev, cur, attrs: tuple[str, str]):
        """Bytes/s for a pair of monotonic counters, as MB/s."""
        if prev is None or self._last_ts is None:
            return None, None
        dt = now - self._last_ts
        if dt <= 0:
            return None, None
        a = (getattr(cur, attrs[0]) - getattr(prev, attrs[0])) / dt / _MB
        b = (getattr(cur, attrs[1]) - getattr(prev, attrs[1])) / dt / _MB
        return max(0.0, a), max(0.0, b)

    def sample(self) -> SystemSample:
        now = time.monotonic()
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()

        sample = SystemSample(
            cpu_pct=psutil.cpu_percent(interval=None),
            cpu_cores=self._cores,
            ram_used_mb=(vm.total - vm.available) / _MB,
            ram_total_mb=vm.total / _MB,
            swap_used_mb=sw.used / _MB,
            swap_total_mb=sw.total / _MB,
        )

        # Load average: real on Unix, an emulated approximation on Windows.
        try:
            sample.load1 = psutil.getloadavg()[0]
        except (AttributeError, OSError):
            sample.load1 = None

        disk = psutil.disk_io_counters()
        if disk is not None:
            read, write = self._rate(now, self._last_disk, disk, ("read_bytes", "write_bytes"))
            sample.disk_read_mbs, sample.disk_write_mbs = read, write
            self._last_disk = disk

        net = psutil.net_io_counters()
        if net is not None:
            recv, sent = self._rate(now, self._last_net, net, ("bytes_recv", "bytes_sent"))
            sample.net_recv_mbs, sample.net_sent_mbs = recv, sent
            self._last_net = net

        try:
            battery = psutil.sensors_battery()
        except (AttributeError, OSError):
            battery = None
        if battery is not None:
            sample.battery_pct = battery.percent
            sample.on_battery = not battery.power_plugged

        sample.gpus = self._gpus.sample()
        self._last_ts = now
        return sample
