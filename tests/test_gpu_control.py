import unittest
from unittest.mock import patch, Mock

from queenbee.gpu_control import GPUController
from queenbee.model import GpuSample


def card(free, index=0):
    return GpuSample(index, "Test GPU", mem_used_mb=24000-free, mem_total_mb=24000, uuid=f"GPU-{index}")


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.controller = GPUController(clock=lambda: self.now)
        self.proc = patch("queenbee.gpu_control.psutil.Process", side_effect=lambda pid: Mock(create_time=lambda: float(pid)))
        self.proc.start()
        self.addCleanup(self.proc.stop)
        self.pid = 100

    def register(self, **kwargs):
        self.pid += 1
        return self.controller.register("job", self.pid, **kwargs)

    def beat(self, job, **kwargs):
        return self.controller.heartbeat(job["id"], job["token"], **kwargs)

    def tick(self, free=12000, index=0):
        self.now += 1
        self.controller.tick([card(free, index)])

    def admit(self, job):
        self.beat(job, ack=0, released=True)
        for _ in range(3):
            self.tick()
        directive = self.beat(job)
        self.assertEqual(directive["action"], "run")
        self.beat(job, ack=directive["revision"])
        return directive

    def test_primary_is_unrestricted_even_without_telemetry(self):
        job = self.register(role="primary", max_batch=64)
        self.controller.tick([])
        self.now += 100
        self.assertEqual(self.beat(job)["batch"], 64)
        self.assertEqual(self.beat(job)["action"], "run")

    def test_secondary_starts_at_one_without_reserving_memory(self):
        job = self.register(max_batch=32)
        self.assertEqual(self.beat(job)["action"], "wait")
        self.assertEqual(self.admit(job)["batch"], 1)
        self.assertEqual(self.controller.state()["devices"][0]["free_mb"], 12000)

    def test_only_one_waiter_released_before_acknowledgement(self):
        first, second = self.register(), self.register()
        self.beat(first, ack=0, released=True)
        self.beat(second, ack=0, released=True)
        for _ in range(10):
            self.tick()
        actions = [j.action for j in self.controller.jobs.values()]
        self.assertEqual(actions.count("run"), 1)
        self.assertEqual(actions.count("wait"), 1)

    def test_growth_and_pressure_halve_batch_then_wait(self):
        job = self.register(max_batch=8)
        self.admit(job)
        for _ in range(15):
            self.tick()
            directive = self.beat(job)
            self.beat(job, ack=directive["revision"])
        self.assertEqual(directive["batch"], 4)
        for _ in range(5):
            self.tick(750)
        d = self.beat(job)
        self.assertEqual(d["batch"], 2)
        self.assertEqual(d["delay_s"], .25)
        self.beat(job, ack=d["revision"])
        for _ in range(5):
            self.tick(750)
        d = self.beat(job)
        self.assertEqual(d["batch"], 1)
        self.beat(job, ack=d["revision"])
        for _ in range(5):
            self.tick(750)
        self.assertEqual(self.beat(job)["action"], "wait")

    def test_critical_pressure_requires_unload_ack_before_recovery(self):
        job = self.register()
        self.admit(job)
        self.tick(100)
        d = self.beat(job)
        self.assertEqual(d["action"], "wait")
        self.beat(job, ack=d["revision"], released=False)
        for _ in range(10):
            self.tick()
        self.assertEqual(self.beat(job)["action"], "wait")
        self.beat(job, ack=d["revision"], released=True)
        self.tick()
        self.assertEqual(self.beat(job)["action"], "run")

    def test_gpu_memory_is_not_pooled(self):
        job = self.register(gpu=0)
        self.beat(job, ack=0, released=True)
        for _ in range(4):
            self.now += 1
            self.controller.tick([card(200, 0), card(20000, 1)])
        self.assertEqual(self.beat(job)["action"], "wait")

    def test_missing_and_stale_telemetry_fail_closed(self):
        job = self.register()
        self.admit(job)
        self.now += 9
        self.assertEqual(self.beat(job)["action"], "wait")
        self.controller.tick([])
        self.assertEqual(self.beat(job)["action"], "wait")

    def test_invalid_measurement_does_not_look_like_free_memory(self):
        job = self.register()
        self.controller.tick([GpuSample(0, "bad", mem_total_mb=24000)])
        self.assertEqual(self.beat(job)["action"], "wait")
        self.assertEqual(self.controller.state()["devices"], [])

    def test_manual_wait_is_not_automatically_overridden(self):
        job = self.register()
        self.admit(job)
        self.controller.control(job["id"], "wait")
        d = self.beat(job)
        self.beat(job, ack=d["revision"], released=True)
        for _ in range(8):
            self.tick()
        self.assertEqual(self.beat(job)["action"], "wait")
        self.controller.control(job["id"], "auto")
        self.tick()
        self.assertEqual(self.beat(job)["action"], "run")

    def test_primary_cannot_be_paused_from_ui(self):
        job = self.register(role="primary")
        self.assertFalse(self.controller.control(job["id"], "wait")["ok"])

    def test_heartbeat_loss_blocks_other_secondary_growth(self):
        first = self.register()
        self.admit(first)
        self.now += 130
        second = self.register()
        self.beat(second, ack=0, released=True)
        for _ in range(4):
            self.tick()
        self.assertEqual(self.beat(second)["action"], "wait")

    def test_token_validation_and_secret_not_exposed(self):
        job = self.register()
        with self.assertRaises(ValueError):
            self.controller.heartbeat(job["id"], "wrong")
        self.assertNotIn(job["token"], str(self.controller.state()))

    def test_no_duplicate_process_registration(self):
        self.register()
        with self.assertRaises(ValueError):
            self.controller.register("duplicate", self.pid)

    def test_secondary_waits_the_moment_a_primary_registers(self):
        secondary = self.register(max_batch=8)
        self.admit(secondary)
        self.register(role="primary")
        d = self.beat(secondary)
        self.assertEqual(d["action"], "wait")
        self.assertEqual(d["batch"], 1)
        self.assertIn("Primary", d["reason"])

    def test_plenty_of_free_memory_does_not_let_secondary_run_beside_primary(self):
        primary = self.register(role="primary")
        secondary = self.register()
        self.beat(secondary, ack=0, released=True)
        for _ in range(20):
            self.tick(free=23000)
            d = self.beat(secondary)
            self.beat(secondary, ack=d["revision"], released=True)
        self.assertEqual(self.beat(secondary)["action"], "wait")
        self.assertEqual(self.beat(primary)["action"], "run")

    def test_secondary_resumes_gradually_after_primary_ends(self):
        primary = self.register(role="primary")
        secondary = self.register(max_batch=8)
        d = self.beat(secondary)
        self.beat(secondary, ack=d["revision"], released=True)
        self.tick()
        self.controller.unregister(primary["id"], primary["token"])
        for _ in range(4):
            self.tick()
        d = self.beat(secondary)
        self.assertEqual(d["action"], "run")
        self.assertEqual(d["batch"], 1)

    def test_primary_on_another_gpu_does_not_stop_secondary(self):
        self.register(role="primary", gpu=1)
        secondary = self.register(gpu=0)
        self.admit(secondary)
        self.tick()
        self.assertEqual(self.beat(secondary)["action"], "run")

    def test_resumable_secondary_is_preempted_not_held(self):
        resumable = self.register(max_batch=8, resumable=True)
        self.admit(resumable)
        plain = self.register(max_batch=8)
        self.register(role="primary")
        self.assertEqual(self.beat(resumable)["action"], "preempt")
        self.assertEqual(self.beat(plain)["action"], "wait")

    def test_primary_cannot_be_resumable(self):
        job = self.register(role="primary", resumable=True)
        self.assertFalse(self.controller.jobs[job["id"]].resumable)

    def test_can_resume_only_without_primary_and_with_room(self):
        self.tick(free=12000)
        self.assertTrue(self.controller.can_resume(0)["ok"])
        primary = self.register(role="primary")
        self.tick(free=12000)
        self.assertFalse(self.controller.can_resume(0)["ok"])
        self.controller.unregister(primary["id"], primary["token"])
        self.tick(free=500)
        self.assertFalse(self.controller.can_resume(0)["ok"])
        self.assertFalse(self.controller.can_resume(1)["ok"])      # unknown GPU

    def test_oom_unloads_before_retry(self):
        job = self.register()
        self.admit(job)
        directive = self.beat(job, oom=True)
        self.assertEqual(directive["action"], "wait")
        self.assertEqual(directive["batch"], 1)


if __name__ == "__main__":
    unittest.main()
