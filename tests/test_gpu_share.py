"""A resumable secondary beside a primary: VRAM decides, not the role alone."""

import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from queenbee.gpu import AdaptiveGPU
from queenbee.gpu_control import GPUController
from queenbee.model import GpuSample


def card(used, total=8000, util=None):
    return GpuSample(0, "Test GPU", util_pct=util, mem_used_mb=used, mem_total_mb=total, uuid="GPU-0")


class ShareTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.c = GPUController(clock=lambda: self.now)
        p = patch("queenbee.gpu_control.psutil.Process", side_effect=lambda pid: Mock(create_time=lambda: float(pid)))
        p.start()
        self.addCleanup(p.stop)
        self.pid = 100

    def register(self, **kw):
        self.pid += 1
        return self.c.register("job", self.pid, **kw)

    def run_for(self, seconds, used):
        for _ in range(int(seconds)):
            self.now += 1
            for j in self.c.jobs.values():
                j.heartbeat = self.now                               # workers alive
            self.c.tick([card(used)])

    def action(self, job):
        return self.c.jobs[job["id"]].action

    def test_waits_through_warm_up_then_runs_where_there_is_room(self):
        self.register(role="primary")
        self.run_for(10, used=1500)
        self.assertFalse(self.c.can_resume(0)["ok"])                 # warming up
        self.run_for(100, used=1500)
        self.assertTrue(self.c.can_resume(0)["ok"])
        self.assertFalse(self.c.can_resume(0)["ok"])                 # one relaunch at a time
        job = self.register(resumable=True, max_batch=8)
        self.run_for(1, used=1500)
        self.assertEqual(self.action(job), "run")
        self.assertEqual(self.c.jobs[job["id"]].batch, 8)

    def test_no_room_for_the_primary_peak_means_no_sharing(self):
        self.register(role="primary")
        self.run_for(100, used=5500)                                 # the primary's peak
        self.run_for(5, used=2000)                                   # between phases
        verdict = self.c.can_resume(0)
        self.assertFalse(verdict["ok"])
        self.assertIn("primary peak", verdict["reason"])

    def test_pressure_preempts_the_newest_sharer(self):
        self.register(role="primary")
        self.run_for(100, used=1500)
        first = self.register(resumable=True)
        self.run_for(61, used=3500)
        second = self.register(resumable=True)
        self.run_for(1, used=3500)
        self.assertEqual((self.action(first), self.action(second)), ("run", "run"))
        self.run_for(1, used=7300)                                   # 700 MB free
        self.assertEqual((self.action(first), self.action(second)), ("run", "preempt"))

    def test_a_silent_sharer_is_not_started(self):
        self.register(role="primary")
        self.run_for(100, used=1500)
        job = self.register(resumable=True)
        self.now += 150
        self.c.tick([card(1500)])
        self.assertNotEqual(self.action(job), "run")

    def test_jobs_that_cannot_checkpoint_still_wait_beside_a_primary(self):
        self.register(role="primary")
        plain = self.register()
        self.run_for(200, used=1000)
        self.assertEqual(self.action(plain), "wait")


class CapTests(unittest.TestCase):
    """No primary: secondaries fill the card up to 90% of memory and compute,
    and the first waiting job that fits starts."""

    def setUp(self):
        self.now = 0.0
        self.c = GPUController(clock=lambda: self.now)
        p = patch("queenbee.gpu_control.psutil.Process", side_effect=lambda pid: Mock(create_time=lambda: float(pid)))
        p.start()
        self.addCleanup(p.stop)
        self.pid = 100

    def register(self, name, **kw):
        self.pid += 1
        job = self.c.register(name, self.pid, **kw)
        self.c.jobs[job["id"]].released = True
        return job

    def run_for(self, seconds, used, util=None):
        for _ in range(int(seconds)):
            self.now += 1
            for j in self.c.jobs.values():
                j.heartbeat, j.ack = self.now, j.revision
            self.c.tick([card(used, util=util)])

    def action(self, job):
        return self.c.jobs[job["id"]].action

    def test_the_small_job_that_fits_starts_when_the_big_one_does_not(self):
        self.c.footprints = {"reem": {"mb": 3000, "util": 40}, "mobilenet": {"mb": 800, "util": 30}}
        reem, mobilenet = self.register("reem"), self.register("mobilenet")
        self.run_for(5, used=4500, util=20)            # 7200 MB cap: 4500 + 3000 does not fit
        self.assertEqual((self.action(reem), self.action(mobilenet)), ("wait", "run"))

    def test_compute_cap(self):
        self.c.footprints = {"a": {"mb": 500, "util": 30}}
        job = self.register("a")
        self.run_for(5, used=1000, util=70)            # 70 + 30 > 90
        self.assertEqual(self.action(job), "wait")
        self.run_for(12, used=1000, util=50)           # the average falls to 50
        self.assertEqual(self.action(job), "run")

    def test_memory_past_the_cap_preempts_the_newest_resumable(self):
        a = self.register("a", resumable=True)
        self.run_for(5, used=1000)
        self.run_for(61, used=3000)
        b = self.register("b", resumable=True)
        self.run_for(2, used=3000)
        self.assertEqual((self.action(a), self.action(b)), ("run", "run"))
        self.run_for(1, used=7400)                     # past 90% of 8000
        self.assertEqual((self.action(a), self.action(b)), ("run", "preempt"))

    def test_a_job_s_share_is_learned_and_kept(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "footprints.json")
            self.c.footprints_path = path
            job = self.register("reem")
            self.run_for(4, used=1000, util=0)
            self.assertEqual(self.action(job), "run")
            self.run_for(70, used=2600, util=60)
            self.assertEqual(self.c.footprints["reem"]["mb"], 1600)
            self.assertGreater(self.c.footprints["reem"]["util"], 40)
            again = GPUController(footprints_path=path, monitor=Mock())
            self.assertEqual(again.estimate("reem")[0], 1600)


class RestoreTests(unittest.TestCase):
    def test_restore_runs_before_a_released_job_trains_again(self):
        order = []
        client = Mock()
        client._call.side_effect = [{"id": "job", "token": "t"},
                                    {"action": "wait", "revision": 1},
                                    {"action": "run", "revision": 2, "batch": 8, "delay_s": 0}]
        sdk = AdaptiveGPU(role="secondary", max_batch=8, client=client, sleep=Mock(),
                          release=lambda: order.append("release"), restore=lambda: order.append("restore"))
        self.assertEqual(sdk.before_batch(), 8)
        self.assertEqual(order, ["release", "restore"])


if __name__ == "__main__":
    unittest.main()


class SignatureTests(unittest.TestCase):
    def test_sweep_values_share_one_signature_and_shape_changes_split_it(self):
        from queenbee.gpu import workload_signature
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "t.py"), "w").write("print(1)\n")
            base = ["python", "t.py", "--lambda", "0.1", "--run-name", "a", "--seed", "1", "--batch-size", "32"]
            other = ["python", "t.py", "--lambda", "0.9", "--run-name", "b", "--seed", "7", "--batch-size", "32"]
            bigger = ["python", "t.py", "--lambda", "0.1", "--run-name", "a", "--batch-size", "64"]
            self.assertEqual(workload_signature(base, d), workload_signature(other, d))
            self.assertNotEqual(workload_signature(base, d), workload_signature(bigger, d))
            open(os.path.join(d, "t.py"), "a").write("print(2)\n")
            self.assertNotEqual(workload_signature(base, d), workload_signature(other[:2] + base[2:], d) + "x")


class FastLearning(unittest.TestCase):
    def test_share_is_learned_when_the_peak_is_stable_not_after_a_fixed_minute(self):
        now = [0.0]
        c = GPUController(clock=lambda: now[0])
        p = patch("queenbee.gpu_control.psutil.Process", side_effect=lambda pid: Mock(create_time=lambda: float(pid)))
        p.start()
        self.addCleanup(p.stop)
        job = c.register("run-a-lambda0.1", 101, signature="sigA")
        c.jobs[job["id"]].released = True
        for _ in range(40):
            now[0] += 1
            for j in c.jobs.values():
                j.heartbeat, j.ack = now[0], j.revision
            c.tick([card(2600 if now[0] > 8 else 1000, util=60 if now[0] > 8 else 0)])
        self.assertIn("sigA", c.footprints)                # learned well inside 60 s
        self.assertNotIn("run-a-lambda0.1", c.footprints)


class ThroughputTests(unittest.TestCase):
    """Two jobs run together only while together they finish more work."""

    def setUp(self):
        self.now = 0.0
        self.c = GPUController(clock=lambda: self.now)
        p = patch("queenbee.gpu_control.psutil.Process", side_effect=lambda pid: Mock(create_time=lambda: float(pid)))
        p.start()
        self.addCleanup(p.stop)
        self.c.footprints = {"S": {"mb": 500, "util": 20}}
        self.tokens = {}
        self.ids = {}
        for n, pid in (("A", 101), ("B", 102)):
            r = self.c.register(n, pid, max_batch=1, resumable=True, signature="S")
            self.ids[n], self.tokens[n] = r["id"], r["token"]
            self.c.jobs[r["id"]].released = True

    def run_for(self, seconds, solo_beats, together_beats):
        for _ in range(seconds):
            self.now += 1
            running = [j for j in self.c.jobs.values() if j.action == "run"]
            for j in self.c.jobs.values():
                beats = (solo_beats if len(running) <= 1 else together_beats) if j.action == "run" else 1
                for _ in range(beats):
                    self.c.heartbeat(j.id, self.tokens[[k for k, v in self.ids.items() if v == j.id][0]], ack=j.revision, released=j.action != "run")
            self.c.tick([card(2000, util=30)])

    def actions(self):
        return {n: self.c.jobs[i].action for n, i in self.ids.items()}

    def test_no_gain_together_means_one_at_a_time(self):
        self.run_for(120, solo_beats=8, together_beats=4)        # 4 + 4 = one job's work
        verdict = self.c.verdicts[("S", "S")]
        self.assertFalse(verdict["parallel"])
        self.assertEqual(list(self.actions().values()).count("run"), 1)
        self.assertLess(verdict["speedup"], 1.15)

    def test_real_gain_keeps_both_running(self):
        self.run_for(120, solo_beats=8, together_beats=6)        # 6 + 6 = 1.5x
        self.assertTrue(self.c.verdicts[("S", "S")]["parallel"])
        self.assertEqual(self.actions(), {"A": "run", "B": "run"})
