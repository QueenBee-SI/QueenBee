"""Cooperative GPU pressure control. No per-project VRAM reservations.

Primary jobs are observed, never throttled. Secondary jobs must cooperate at
batch boundaries and unload on wait. This is not a CUDA allocation firewall.

While a primary job is registered on a GPU, secondary jobs on that GPU wait
with their memory released. A primary is allowed to grow without limit, and
nothing on a consumer card can make a secondary give memory back faster than
its next safe boundary -- so the only way to make sure a primary never hits an
OOM because of a secondary is for the secondary not to hold memory while the
primary runs. Slowing a secondary down frees no memory; it only trades compute.
"""
from __future__ import annotations

import json
import math
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass

import psutil

from .collectors.gpu import NvidiaSmiDevices


@dataclass
class Workload:
    id: str
    name: str
    pid: int
    born: float
    gpu: int
    role: str
    max_batch: int
    batch: int = 1
    delay_s: float = 0.0
    action: str = "wait"
    reason: str = "Waiting for fresh GPU telemetry"
    revision: int = 0
    ack: int = -1
    heartbeat: float = 0.0
    released: bool = True
    manual_wait: bool = False
    changed: float = 0.0
    # Saves its state on request and can be restarted from it. Such a job is
    # preempted (checkpoint + exit) instead of held in memory while it waits.
    resumable: bool = False
    # Measuring what this job adds once it runs: the device before it was
    # admitted, and the most memory seen since.
    run_since: float = 0.0
    base_used: float = 0.0
    base_util: float = 0.0
    peak_used: float = 0.0
    learned: bool = True
    # The name QueenBee launched it under (`queenbee.gpu run --name`); what it
    # adds is learned under that name, which is what a relaunch asks with.
    launch_name: str = ""
    # What the job is, apart from what it is called: footprints are learned
    # under this, so every run of one script and shape shares one measurement.
    signature: str = ""
    peak_changed: float = 0.0

    @property
    def fp_key(self) -> str:
        return self.signature or self.launch_name


class GPUController:
    def __init__(self, *, clock=time.monotonic, monitor=None,
                 pressure_mb=1024.0, recovery_mb=2048.0, settle_s=5.0,
                 share_warmup_s=90.0, share_settle_s=60.0, learn_min_s=8.0, learn_stable_s=10.0,
                 cal_window_s=20.0, min_parallel_gain=0.15, secondary_estimate_mb=2048.0,
                 cap_pct=90.0, unknown_util_pct=40.0, footprints_path=None, silent_s=120.0):
        self.clock = clock
        self.monitor = monitor or NvidiaSmiDevices()
        self.pressure_mb = pressure_mb
        self.recovery_mb = recovery_mb
        self.settle_s = settle_s
        # Sharing a GPU with its primary: a resumable secondary may run beside
        # it once the primary has shown its peak (warm-up) and there is room
        # for that peak, the secondary and a margin. One is admitted at a time
        # (settle), so each one's memory shows before the next is judged.
        self.share_warmup_s = share_warmup_s
        self.share_settle_s = share_settle_s
        # A job's share is known once its memory peak has stopped growing
        # (after learn_min_s, stable for learn_stable_s), not after a fixed
        # wait; share_settle_s stays the longest a measurement may take.
        self.learn_min_s = learn_min_s
        # Running jobs side by side is only worth it if the work finished per
        # second goes up: after cal_window_s together, the jobs' rates (each
        # against its own solo rate) must add up to 1 + min_parallel_gain.
        # GPU utilisation alone proves nothing: two jobs can show 90% and run
        # at half speed each.
        self.cal_window_s = cal_window_s
        self.min_parallel_gain = min_parallel_gain
        self.beats: dict[str, deque] = {}          # job id -> (time, samples) while running
        self.solo: dict[str, float] = {}           # fp_key -> samples/s measured alone
        self.verdicts: dict[tuple, dict] = {}  # sorted fp_keys of the jobs run together -> {"parallel": bool, "speedup": x}
        self.together_since: dict[int, tuple] = {}  # gpu -> (set of running ids, since)
        self.learn_stable_s = learn_stable_s
        self.secondary_estimate_mb = secondary_estimate_mb
        # Neither memory nor compute is filled past cap_pct by admitting a
        # secondary; each job's share is learned the first time it runs.
        # A worker heartbeats between batches. Training runs go quiet for a
        # while at each epoch's evaluation, still holding the memory they have
        # (a "wait" would free nothing before their next batch); a worker
        # silent longer than this is taken as out of control.
        self.silent_s = silent_s
        self.cap_pct = cap_pct
        self.unknown_util_pct = unknown_util_pct
        self.footprints_path = footprints_path
        self.footprints: dict[str, dict] = self._load_footprints()
        self.util_hist: dict[int, deque] = {}
        self.primary_since: dict[int, float] = {}
        self.primary_peak: dict[int, float] = {}
        self.granted: dict[int, float] = {}
        self.jobs: dict[str, Workload] = {}
        self.tokens: dict[str, str] = {}
        self.devices: dict[int, dict] = {}
        self.sampled = float("-inf")
        self.healthy: dict[int, int] = {}
        self.last_growth: dict[int, float] = {}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None

    def register(self, name, pid, gpu=0, role="secondary", max_batch=1, resumable=False,
                 launch_name=None, signature=None):
        if role not in ("primary", "secondary"):
            raise ValueError("role must be primary or secondary")
        if not isinstance(gpu, int) or gpu < 0:
            raise ValueError("gpu must be a nonnegative device index")
        if not isinstance(max_batch, int) or not 1 <= max_batch <= 65536:
            raise ValueError("max_batch must be 1..65536")
        born = psutil.Process(int(pid)).create_time()
        now = self.clock()
        with self.lock:
            # A process cannot register repeatedly to escape its previous wait.
            if any(j.pid == int(pid) and j.born == born for j in self.jobs.values()):
                raise ValueError("process already registered")
            key, token = secrets.token_hex(12), secrets.token_urlsafe(24)
            job = Workload(key, str(name)[:160], int(pid), born, gpu, role,
                           max_batch, heartbeat=now, changed=now,
                           resumable=bool(resumable) and role == "secondary",
                           launch_name=str(launch_name or name)[:160],
                           signature=str(signature or "")[:64])
            if role == "primary":
                job.action, job.reason, job.batch = "run", "Primary job: unrestricted", max_batch
            self.jobs[key], self.tokens[key] = job, token
            if role == "primary":
                # Now, not at the next tick: the primary starts allocating as
                # soon as this returns. Its peak is measured afresh.
                self.primary_since[gpu], self.primary_peak[gpu] = now, 0.0
                self._yield_to_primary(gpu)
            return {"id": key, "token": token}

    PRIMARY_WAIT = "Primary job running on this GPU: waiting with memory released"
    PRIMARY_PREEMPT = "Primary job running on this GPU: checkpoint and exit, resume afterwards"

    def _primary_on(self, gpu):
        return any(j.gpu == gpu and j.role == "primary" for j in self.jobs.values())

    def _yield_to_primary(self, gpu):
        for j in self.jobs.values():
            if j.gpu != gpu or j.role != "secondary":
                continue
            # A resumable job leaves entirely: its RAM is freed too, and a
            # crash or reboot while it waits costs nothing. Others wait in
            # memory with their GPU allocations released, as before.
            action, reason = (("preempt", self.PRIMARY_PREEMPT) if j.resumable
                              else ("wait", self.PRIMARY_WAIT))
            if j.action != action or j.reason != reason:
                self._change(j, action, reason, 1)

    SHARE_RUN = "Sharing the GPU with the primary: fits under the caps"
    SHARE_PRESSURE = "GPU memory past the cap: checkpoint and exit, resume when there is room"
    SHARE_NO_ROOM = "Does not fit yet: checkpoint and exit, resume when it does"

    # -- what a job adds, learned -----------------------------------------

    def _load_footprints(self):
        if not self.footprints_path:
            return {}
        try:
            with open(self.footprints_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_footprints(self):
        if not self.footprints_path:
            return
        try:
            tmp = str(self.footprints_path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.footprints, fh, indent=1)
            os.replace(tmp, self.footprints_path)
        except OSError:
            pass

    def estimate(self, name):
        """(MB, util %) a job is expected to add: learned, or the defaults."""
        known = self.footprints.get(name) or {}
        mb = known.get("mb") or self.secondary_estimate_mb
        util = known.get("util")
        return float(mb), float(self.unknown_util_pct if util is None else util)

    def _util(self, gpu):
        hist = self.util_hist.get(gpu)
        return sum(hist) / len(hist) if hist else 0.0

    def _fits(self, gpu, name, now):
        """(ok, reason): whether `name` fits on `gpu` now -- memory in use,
        the primary back at its measured peak and the job's expected size
        under cap_pct of the card; compute likewise."""
        device = self.devices[gpu]
        headroom = 0.0
        if self._primary_on(gpu):
            if now - self.primary_since.get(gpu, now) < self.share_warmup_s:
                return False, "Primary warming up: measuring its VRAM peak"
            headroom = max(0.0, self.primary_peak.get(gpu, 0.0) - device["used_mb"])
        mb, util = self.estimate(name)
        cap_mb = device["total_mb"] * self.cap_pct / 100.0
        after = device["used_mb"] + headroom + mb
        if after > cap_mb:
            return False, (f"VRAM: {device['used_mb']:.0f} MB used + {mb:.0f} MB for it"
                           + (f" + {headroom:.0f} MB primary peak" if headroom else "")
                           + f" > {self.cap_pct:.0f}% ({cap_mb:.0f} MB)")
        now_util = self._util(gpu)
        if now_util + util > self.cap_pct:
            return False, f"GPU busy: {now_util:.0f}% + {util:.0f}% for it > {self.cap_pct:.0f}%"
        return True, f"Fits: {after:.0f}/{cap_mb:.0f} MB, {now_util + util:.0f}% compute"

    def _admit(self, job, reason, batch, now):
        device = self.devices[job.gpu]
        self._change(job, "run", reason, batch)
        job.released = False
        self.last_growth[job.gpu] = now
        if job.fp_key in self.footprints:
            job.learned = True                    # its share is known
        elif not job.run_since:
            # First run: measure what it adds from here. A later re-admission
            # after a short wait keeps the measurement it already started.
            job.run_since, job.learned = now, False
            job.base_used, job.base_util = device["used_mb"], self._util(job.gpu)
            job.peak_used = device["used_mb"]
            job.peak_changed = now

    def _learn(self, gpu, now):
        """A job admitted alone (admissions are a settle window apart) shows
        its own share as the change since it started."""
        for j in self.jobs.values():
            if j.gpu != gpu or j.role != "secondary" or j.action != "run" or j.learned:
                continue
            if self.devices[gpu]["used_mb"] > j.peak_used + 64:
                j.peak_changed = now              # still growing
            j.peak_used = max(j.peak_used, self.devices[gpu]["used_mb"])
            elapsed = now - j.run_since
            stable = elapsed >= self.learn_min_s and now - j.peak_changed >= self.learn_stable_s
            if stable or elapsed >= self.share_settle_s:
                self.footprints[j.fp_key] = {
                    "mb": round(max(256.0, j.peak_used - j.base_used)),
                    "util": round(min(100.0, max(5.0, self._util(gpu) - j.base_util)))}
                j.learned = True
                self._save_footprints()

    def _measuring(self, gpu):
        """A job still being measured: its share is not yet in the numbers
        the next admission would be judged on."""
        return any(j.gpu == gpu and j.role == "secondary" and j.action == "run" and not j.learned
                   for j in self.jobs.values())

    # -- is running together faster than running one after the other? --------

    def rate(self, job, now, window=None):
        """Samples per second this job has completed lately (None: too few
        steps to say)."""
        window = window or self.cal_window_s
        pts = [(t, w) for t, w in self.beats.get(job.id, ()) if now - t <= window]
        if len(pts) < 5 or pts[-1][0] - pts[0][0] < window / 3:
            return None
        return sum(w for _, w in pts[1:]) / (pts[-1][0] - pts[0][0])

    def _running(self, gpu, now):
        return [j for j in self.jobs.values() if j.gpu == gpu and j.action == "run"
                and j.ack == j.revision and now - j.heartbeat <= 10]

    def _calibrate(self, gpu, now):
        """Learn solo rates; judge every set of jobs that has run together."""
        running = self._running(gpu, now)
        ids = frozenset(j.id for j in running)
        if self.together_since.get(gpu, (None,))[0] != ids:
            self.together_since[gpu] = (ids, now)       # the mix changed: measure again
            return
        if now - self.together_since[gpu][1] < self.cal_window_s:
            return
        if any(j.batch < j.max_batch and j.role == "secondary" for j in running):
            return                                      # still ramping up: not steady
        rates = [self.rate(j, now) for j in running]
        if not running or any(r is None for r in rates):
            return
        if len(running) == 1:
            fp = running[0].fp_key
            self.solo[fp] = rates[0]
            if fp in self.footprints:            # kept with the share, for the next run
                self.footprints[fp]["solo_rate"] = round(rates[0], 3)
                self._save_footprints()
            return
        solos = [self._solo(j.fp_key) for j in running]
        if any(v is None for v in solos):
            return                                      # never seen alone: nothing to compare with
        speedup = sum(r / s for r, s in zip(rates, solos))
        key = tuple(sorted(j.fp_key for j in running))
        worth = speedup >= 1.0 + self.min_parallel_gain
        self.verdicts[key] = {"parallel": worth, "speedup": round(speedup, 2)}
        if not worth:
            # Sequential is better: the newest leaves and waits its turn.
            newest = max(running, key=lambda j: j.changed)
            self._change(newest, "preempt" if newest.resumable else "wait",
                         f"Together they finish only {speedup:.2f}x one job's work; running one at a time", 1)

    def _solo(self, fp):
        return self.solo.get(fp) or (self.footprints.get(fp) or {}).get("solo_rate")

    def _solo_pending(self, gpu, now):
        """A job is running alone and its solo rate is not yet known: the next
        one waits for it (at most three windows), or there is nothing to
        compare running together with."""
        running = self._running(gpu, now)
        return len(running) == 1 and self._solo(running[0].fp_key) is None             and now - running[0].changed < 3 * self.cal_window_s

    def _parallel_ok(self, gpu, name, now):
        """False when this job with those now running was measured slower or
        no faster than running them one after the other."""
        fps = [j.fp_key for j in self._running(gpu, now)]
        if not fps:
            return True
        v = self.verdicts.get(tuple(sorted(fps + [name])))
        return v is None or v["parallel"]

    def _admitted_recently(self, gpu, now):
        recent = [j.changed for j in self.jobs.values()
                  if j.gpu == gpu and j.role == "secondary" and j.action == "run"]
        recent.append(self.granted.get(gpu, float("-inf")))
        return now - max(recent) < self.share_settle_s

    def can_resume(self, gpu=0, name=None):
        """Whether a preempted secondary may start again on `gpu` now."""
        with self.lock:
            if self.clock() - self.sampled > 8 or gpu not in self.devices:
                return {"ok": False, "reason": "GPU telemetry unavailable or stale"}
            now = self.clock()
            ok, why = self._fits(gpu, name or "", now)
            if ok and not self._parallel_ok(gpu, name or "", now):
                ok, why = False, "Measured: together no faster than one after the other"
            if ok and (self._measuring(gpu) or now - self.granted.get(gpu, float("-inf")) < self.share_settle_s):
                ok, why = False, "Another job was just admitted; measuring what it adds"
            if ok:
                self.granted[gpu] = now              # one relaunch per settle window
            return {"ok": ok, "reason": why}

    def _job(self, key, token):
        if key not in self.jobs or not secrets.compare_digest(self.tokens[key], str(token)):
            raise ValueError("unknown workload or invalid token")
        return self.jobs[key]

    def _change(self, job, action, reason, batch=None, delay=0.0):
        batch = job.batch if batch is None else batch
        if (job.action, job.batch, job.delay_s) != (action, batch, delay):
            job.revision += 1
            job.changed = self.clock()
        job.action, job.batch, job.delay_s, job.reason = action, batch, delay, reason

    def heartbeat(self, key, token, ack=-1, released=False, oom=False):
        with self.lock:
            job = self._job(key, token)
            job.heartbeat = self.clock()
            if ack == job.revision:
                job.ack, job.released = ack, bool(released)
            if job.role == "secondary":
                if oom:
                    self._change(job, "wait", "OOM reported: unload before retry", 1)
                    self.last_growth[job.gpu] = self.clock()
                    self.healthy[job.gpu] = 0
                if self.clock() - self.sampled > 8 or job.gpu not in self.devices:
                    self._change(job, "wait", "GPU telemetry unavailable or stale")
            if job.action == "run" and job.ack == job.revision:
                self.beats.setdefault(job.id, deque(maxlen=400)).append((job.heartbeat, job.batch))
            return {"action": job.action, "batch": job.batch, "delay_s": job.delay_s,
                    "revision": job.revision, "reason": job.reason}

    def unregister(self, key, token):
        with self.lock:
            self._job(key, token)
            del self.jobs[key]
            del self.tokens[key]
        return {"ok": True}

    def control(self, key, action):
        with self.lock:
            job = self.jobs.get(key)
            if job is None or job.role != "secondary":
                return {"ok": False, "error": "Only secondary jobs can be controlled"}
            if action not in ("wait", "auto"):
                return {"ok": False, "error": "action must be wait or auto"}
            job.manual_wait = action == "wait"
            if job.manual_wait:
                self._change(job, "wait", "Waiting at your request")
            return {"ok": True}

    def tick(self, samples):
        now = self.clock()
        with self.lock:
            self.devices = {}
            for g in samples:
                used, total = g.mem_used_mb, g.mem_total_mb
                if (used is None or total is None or not math.isfinite(used)
                        or not math.isfinite(total) or not 0 <= used <= total or total <= 0):
                    continue
                self.devices[g.index] = {"index": g.index, "name": g.name,
                                         "uuid": g.uuid,
                                         "used_mb": used, "total_mb": total,
                                         "free_mb": total - used}
                if g.util_pct is not None and math.isfinite(g.util_pct):
                    self.util_hist.setdefault(g.index, deque(maxlen=10)).append(float(g.util_pct))
            self.sampled = now
            for gpu in list(self.healthy):
                if gpu not in self.devices:
                    self.healthy[gpu] = 0
            for job in list(self.jobs.values()):
                try:
                    alive = psutil.Process(job.pid).create_time() == job.born
                except psutil.NoSuchProcess:
                    alive = False
                except psutil.AccessDenied:
                    alive = True
                if not alive:
                    del self.jobs[job.id]
                    del self.tokens[job.id]
                    continue
                if job.role == "secondary" and job.gpu not in self.devices:
                    self._change(job, "wait", "GPU telemetry unavailable")
            for gpu, device in self.devices.items():
                jobs = [j for j in self.jobs.values() if j.gpu == gpu and j.role == "secondary"]
                free = device["free_mb"]
                self.healthy[gpu] = self.healthy.get(gpu, 0) + 1 if free >= self.recovery_mb else 0
                for j in jobs:
                    if now - j.heartbeat > self.silent_s:
                        self._change(j, "wait", "Worker heartbeat missing; control unconfirmed")
                    elif j.manual_wait:
                        self._change(j, "wait", "Waiting at your request")
                self._learn(gpu, now)
                self._calibrate(gpu, now)
                if self._primary_on(gpu):
                    self._share_with_primary(gpu, jobs, device, now)
                    # Recovery starts from scratch once the primary is gone.
                    self.healthy[gpu] = 0
                    continue
                cap_mb = device["total_mb"] * self.cap_pct / 100.0
                if free < self.pressure_mb or device["used_mb"] > cap_mb:
                    resumable = sorted((j for j in jobs if j.resumable and j.action == "run"),
                                       key=lambda j: j.changed, reverse=True)
                    if resumable and free >= self.pressure_mb / 2:
                        # A job that can checkpoint leaves; the newest first.
                        self._change(resumable[0], "preempt", self.SHARE_PRESSURE, 1)
                        continue
                    for j in jobs:
                        if j.action in ("wait", "preempt"):
                            continue
                        # Critical pressure unloads immediately at the next safe boundary.
                        if free < self.pressure_mb / 2:
                            self._change(j, "wait", "Critical GPU pressure: release secondary memory", 1)
                        elif j.ack == j.revision and now - j.changed >= self.settle_s:
                            if j.batch > 1:
                                self._change(j, "run", "GPU pressure: smaller batch and slower execution",
                                             max(1, j.batch // 2), 0.25)
                            else:
                                self._change(j, "wait", "Minimum batch reached: release memory and wait")
                    continue
                # Grow only one worker per GPU after acknowledgement and settling.
                # This is serialized probing, not a memory reservation or OOM guarantee.
                if self.healthy[gpu] < 3 or now - self.last_growth.get(gpu, float("-inf")) < self.settle_s:
                    continue
                if any(j.ack != j.revision or now - j.heartbeat > self.silent_s or
                       (j.action == "wait" and not j.released) for j in jobs):
                    continue
                eligible = [j for j in jobs if not j.manual_wait]
                eligible.sort(key=lambda j: (j.action != "wait", j.changed))
                for j in eligible:
                    if j.action == "wait":
                        # Waiting jobs in order; the first that fits starts,
                        # so a small job is not held behind a big one.
                        if self._measuring(gpu) or self._solo_pending(gpu, now):
                            break
                        if self._parallel_ok(gpu, j.fp_key, now) and self._fits(gpu, j.fp_key, now)[0]:
                            self._admit(j, "Capacity recovered: gradual secondary execution", 1, now)
                            break
                    elif j.batch < j.max_batch or j.delay_s:
                        self._change(j, "run", "Capacity recovered: gradual secondary execution",
                                     min(j.max_batch, j.batch + 1))
                        self.last_growth[gpu] = now
                        break

    def _share_with_primary(self, gpu, jobs, device, now):
        """A primary runs on `gpu`. Jobs that cannot checkpoint wait in memory
        with their GPU allocations released. Resumable ones run beside it
        while there is room, and checkpoint and exit when there is not."""
        running = [j for j in jobs if j.resumable and j.action == "run"]
        if not running:
            # Its own peak, measured while nothing runs beside it.
            self.primary_peak[gpu] = max(self.primary_peak.get(gpu, 0.0), device["used_mb"])
        free = device["free_mb"]
        over = device["used_mb"] > device["total_mb"] * self.cap_pct / 100.0
        if (over or free < self.pressure_mb) and running:
            # The newest leaves first; all of them when pressure is critical.
            running.sort(key=lambda j: j.changed, reverse=True)
            for j in running if free < self.pressure_mb / 2 else running[:1]:
                self._change(j, "preempt", self.SHARE_PRESSURE, 1)
        for j in jobs:
            if not j.resumable:
                if j.action != "wait" or j.reason != self.PRIMARY_WAIT:
                    self._change(j, "wait", self.PRIMARY_WAIT, 1)
                continue
            if j.action in ("preempt", "run") or j.manual_wait or now - j.heartbeat > self.silent_s:
                continue                          # a silent worker stays waiting
            ok, why = self._fits(gpu, j.fp_key, now)
            if ok and self._measuring(gpu):
                continue                          # let the last one be measured first
            if ok:
                self._admit(j, self.SHARE_RUN, j.max_batch, now)
            else:
                self._change(j, "preempt", self.SHARE_NO_ROOM + f" ({why})", 1)

    def state(self):
        with self.lock:
            return {"ok": True, "strategy": "adaptive-no-reservations",
                    "cap_pct": self.cap_pct, "footprints": dict(self.footprints),
                    "solo_rates": dict(self.solo),
                    "parallel_verdicts": [{"jobs": list(k), **v} for k, v in self.verdicts.items()],
                    "telemetry_fresh": self.clock() - self.sampled <= 8,
                    "devices": list(self.devices.values()),
                    "jobs": [{**asdict(j), "heartbeat_age_s": max(0, self.clock() - j.heartbeat),
                              "confirmed": j.ack == j.revision} for j in self.jobs.values()]}

    def start(self):
        def loop():
            while not self.stop.is_set():
                try:
                    self.tick(self.monitor.sample())
                except Exception:
                    self.tick([])
                self.stop.wait(1)
        self.thread = threading.Thread(target=loop, name="queenbee-gpu-control", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=8)
