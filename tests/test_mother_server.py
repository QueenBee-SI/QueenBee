"""QueenBee's page server: read-only view of the host's GPU jobs."""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from queenbee.mother.server import job_view, serve


def fake_host(jobs, terminals, alive=True):
    host = mock.Mock()
    host.ping.return_value = {"ok": True, "pid": 1, "version": 4, "uptime_s": 5} if alive else None
    host._call.side_effect = lambda m, *a, **k: (
        {"jobs": jobs, "devices": [], "telemetry_fresh": True} if m == "gpu_state" else terminals)
    return host


class JobViewTests(unittest.TestCase):
    def test_host_down(self):
        self.assertIsNone(job_view(fake_host([], [], alive=False))["host"])

    def test_registered_job_is_tied_to_its_launcher_terminal(self):
        # This process stands in for the job; its parent for the launcher.
        jobs = [{"id": "j", "pid": os.getpid(), "role": "secondary", "action": "run",
                 "resumable": True, "name": "train"}]
        terms = [{"id": "term-3", "pid": os.getppid(), "gpu_workload": True, "running": True}]
        view = job_view(fake_host(jobs, terms))
        self.assertEqual(view["jobs"][0]["terminal"], "term-3")
        self.assertTrue(view["jobs"][0]["resumable"])
        self.assertEqual(view["between"], [])

    def test_preempted_job_shows_as_waiting_to_resume(self):
        terms = [{"id": "term-5", "pid": 999999, "gpu_workload": True, "running": True, "cwd": "C:/p"}]
        view = job_view(fake_host([], terms))
        self.assertEqual([b["terminal"] for b in view["between"]], ["term-5"])


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.httpd = serve(0, 1)
        self.httpd.RequestHandlerClass.host = fake_host([], [])
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_page_and_state(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn("QueenBee", r.read().decode())
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/api/state", timeout=10)
        self.assertEqual(ctx.exception.code, 401)

    def test_page_and_state_with_the_token(self):
        req = urllib.request.Request(self.base + "/api/state", headers={"X-QueenBee-Token": self.httpd.token})
        with urllib.request.urlopen(req) as r:
            self.assertTrue(json.load(r)["ok"])

    def test_nothing_writable(self):
        req = urllib.request.Request(self.base + "/api/state", data=b"{}",
                                     headers={"Content-Type": "application/json", "X-QueenBee-Token": self.httpd.token})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 501)

    def test_foreign_host_header_refused(self):
        req = urllib.request.Request(self.base + "/api/state", headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 403)


if __name__ == "__main__":
    unittest.main()


class CachedViewTests(unittest.TestCase):
    """The window asks for /api/state every two seconds; the host's GPU reading takes seconds. Only the first request may wait."""

    def setUp(self):
        from queenbee.mother import server
        self.server = server
        server._VIEW.update(at=0.0, value=None, refreshing=False)

    def tearDown(self):
        self.server._VIEW.update(at=0.0, value=None, refreshing=False)

    def test_a_stale_view_is_answered_at_once_and_refreshed_behind_the_request(self):
        import time
        host = fake_host([], [])
        calls = []

        def slow_view(h):
            calls.append(time.time())
            if len(calls) > 1:
                time.sleep(0.3)
            return {"ok": True, "n": len(calls)}

        with mock.patch.object(self.server, "job_view", side_effect=slow_view):
            self.assertEqual(self.server.cached_job_view(host, ttl=0.01)["n"], 1)      # the first request builds the view
            time.sleep(0.05)
            t0 = time.time()
            self.assertEqual(self.server.cached_job_view(host, ttl=0.01)["n"], 1)      # stale, but answered at once
            self.assertLess(time.time() - t0, 0.1)
            self.assertEqual(self.server.cached_job_view(host, ttl=0.01)["n"], 1)      # no second refresh is started meanwhile
            time.sleep(0.5)
            self.assertEqual(self.server.cached_job_view(host, ttl=60)["n"], 2)        # the refresh landed
            self.assertEqual(len(calls), 2)

    def test_a_failing_refresh_keeps_the_old_view(self):
        import time
        host = fake_host([], [])
        good = {"ok": True, "n": 1}
        with mock.patch.object(self.server, "job_view", side_effect=[good, RuntimeError("host hiccup")]):
            self.assertEqual(self.server.cached_job_view(host, ttl=0.01), good)
            time.sleep(0.05)
            self.assertEqual(self.server.cached_job_view(host, ttl=0.01), good)
            time.sleep(0.2)
            self.assertEqual(self.server.cached_job_view(host, ttl=60), good)
            self.assertFalse(self.server._VIEW["refreshing"])
