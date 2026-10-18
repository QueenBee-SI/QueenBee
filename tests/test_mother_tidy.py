import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import psutil

from queenbee.agents.host_client import HostUnavailable
from queenbee.mother.dispatch import Dispatch


def make(listing, descendants=()):
    host = mock.Mock(port=7877)
    closed = []

    def call(method, *a, **k):
        if method == "terminals":
            return listing
        if method == "descendants":
            return list(descendants)
        if method == "terminal_close":
            closed.append(a[0])
        return {"ok": True, "id": "t"}
    host._call.side_effect = call
    d = Dispatch([], {"models": {"claude": {}}}, host)
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(Dispatch, "start"):
        d.add_job("serve something", tmp, "claude", "haiku")
    d._workloads = lambda: {}
    return d, closed


class Tidy(unittest.TestCase):
    def test_a_server_a_finished_worker_left_behind_is_stopped(self):
        server = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            d, closed = make([{"id": "t1", "pid": 1234, "running": True}], [server.pid])
            job = d.jobs[0]
            job.update(status="done", terminal="t1", ended_at=time.time() - 10)
            d.step()
            server.wait(timeout=10)                          # it was killed
            self.assertTrue(job["reaped"])
            self.assertIn("stopped 1 process", job["events"][-1][1])
            self.assertEqual(closed, [])                     # the agent session itself stays for now
        finally:
            if server.poll() is None:
                server.kill()

    def test_nothing_is_touched_while_the_job_is_still_at_work(self):
        d, closed = make([{"id": "t1", "pid": 1234, "running": True}], [999999])
        d.jobs[0].update(status="agent working", terminal="t1")
        with mock.patch.object(psutil, "Process") as proc:
            d._tidy({"t1": {"id": "t1", "pid": 1234, "running": True}})
        proc.assert_not_called()
        self.assertNotIn("ended_at", d.jobs[0])

    def test_an_idle_session_is_closed_after_a_long_while_and_a_retask_resets_it(self):
        d, closed = make([{"id": "t1", "pid": 1, "running": True}])
        job = d.jobs[0]
        job.update(status="done", terminal="t1", ended_at=time.time() - 2000, reaped=True)
        d._tidy({"t1": {"id": "t1", "pid": 1, "running": True}})
        self.assertEqual(closed, ["t1"])
        self.assertTrue(job["session_closed"])
        job["status"] = "agent working"                     # given a new task
        d._tidy({"t1": {"id": "t1", "pid": 1, "running": True}})
        self.assertNotIn("session_closed", job)
        self.assertNotIn("ended_at", job)

    def test_a_dead_host_does_not_break_the_pass(self):
        d, _ = make([])
        d.host._call.side_effect = HostUnavailable("gone")
        d.jobs[0].update(status="done", terminal="t1", ended_at=time.time() - 10)
        d._tidy({"t1": {"id": "t1", "pid": 5, "running": True}})        # no exception
