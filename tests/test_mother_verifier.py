import json
import os
import tempfile
import unittest
from unittest import mock

from queenbee.mother import verifier
from queenbee.mother.dispatch import Dispatch, run_captured


class Checks(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.f = self.dir.name
        with open(os.path.join(self.f, "m.jsonl"), "w") as fh:
            fh.write('{"a":1}\n{"a":2}\n')
        with open(os.path.join(self.f, "README.md"), "w") as fh:
            fh.write("run: python -m shop\n")

    def tearDown(self):
        self.dir.cleanup()

    def test_normalise_drops_unknown_and_bounds(self):
        out = verifier.normalise([{"type": "nope"}, {"type": "command"}, {"type": "file_exists", "path": "a"},
                                  {"type": "jsonl_rows", "path": "m", "min_rows": 0}, "x"] + [{"type": "file_exists", "path": "p"}] * 30)
        self.assertEqual(out[0], {"type": "file_exists", "path": "a"})
        self.assertEqual(out[1]["min_rows"], 1)
        self.assertEqual(len(out), verifier.MAX_CHECKS)

    def test_file_checks(self):
        r = verifier.run_checks([{"type": "file_exists", "path": "README.md"},
                                 {"type": "file_contains", "path": "README.md", "text": "python -m shop"},
                                 {"type": "file_contains", "path": "README.md", "text": "nope"},
                                 {"type": "jsonl_rows", "path": "m.jsonl", "min_rows": 2},
                                 {"type": "jsonl_rows", "path": "m.jsonl", "min_rows": 3},
                                 {"type": "file_exists", "path": "missing.txt"}], self.f)
        self.assertEqual([x["ok"] for x in r], [True, True, False, True, False, False])

    def test_paths_cannot_leave_the_project(self):
        r = verifier.run_checks([{"type": "file_exists", "path": "../../etc/passwd"}], self.f)
        self.assertFalse(r[0]["ok"])
        self.assertIn("outside", r[0]["detail"])

    def test_only_safe_commands_run(self):
        ran = []
        runner = lambda argv, cwd, t: (ran.append(argv), (0, "ok"))[1]
        r = verifier.run_checks([{"type": "command", "run": "rm -rf /"},
                                 {"type": "command", "run": "python -m unittest discover -s tests"},
                                 {"type": "command", "run": "python train.py --help"},
                                 {"type": "command", "run": "python -c print(1)"}], self.f, runner)
        self.assertEqual([x["ok"] for x in r], [False, True, True, False])
        self.assertEqual(len(ran), 2)

    def test_expected_exit_and_failure_detail(self):
        r = verifier.run_checks([{"type": "command", "run": "pytest", "expect_exit": 0}], self.f,
                                lambda *a: (1, "FAILED test_x"))
        self.assertFalse(r[0]["ok"])
        self.assertIn("FAILED test_x", r[0]["detail"])


class Gate(unittest.TestCase):
    """A worker saying done is not enough: its checks decide."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.host = mock.Mock(port=7877)
        self.writes = []
        self.host._call.side_effect = lambda m, *a, **k: (
            [] if m == "terminals" else (self.writes.append(a), {"ok": True, "id": "t"})[1]
            if m == "terminal_write" else {"ok": True, "id": "t"})
        self.d = Dispatch([], {"models": {"claude": {}, "codex": {}}}, self.host)
        with mock.patch.object(Dispatch, "start"):
            self.d.add_job("build the cart", self.tmp, "claude", "haiku")
        self.job = self.d.jobs[0]
        self.job.update(acceptance=[{"type": "file_exists", "path": "cart.py"}], work_order="1.", kind="task",
                        status="agent working", terminal="t1", launched_at=__import__("time").time())
        self.d._agent_alive = lambda j: True
        self.d.state_of = lambda t: ("working", "")
        self.d._workloads = lambda: {}
        os.makedirs(os.path.join(self.tmp, ".queenbee"), exist_ok=True)

    def report_done(self):
        with open(self.d.report_path(self.job), "w", encoding="utf-8") as fh:
            json.dump({"status": "done", "notes": "built"}, fh)

    def settle(self):
        for t in [t for t in __import__("threading").enumerate() if t.name.startswith("queen-accept")]:
            t.join(5)

    def test_passing_checks_finish_the_job(self):
        open(os.path.join(self.tmp, "cart.py"), "w").write("x")
        self.report_done()
        self.d.step()
        self.assertEqual(self.job["status"], "verifying")
        self.settle()
        self.assertEqual(self.job["status"], "done")

    def test_failing_checks_go_back_to_the_worker_then_fail_the_job(self):
        self.report_done()
        self.d.step()
        self.settle()
        self.assertEqual((self.job["status"], self.job["accept_round"]), ("agent working", 1))
        self.assertFalse(os.path.exists(self.d.report_path(self.job)))       # the old claim is withdrawn
        self.assertTrue(any("cart.py exists" in str(w) for w in self.writes))
        for _ in range(2):
            self.report_done()
            self.d.step()
            self.settle()
        self.assertEqual(self.job["status"], "failed")
        self.assertIn("acceptance failed", self.job["error"])

    def test_fixing_it_between_rounds_passes(self):
        self.report_done()
        self.d.step()
        self.settle()
        open(os.path.join(self.tmp, "cart.py"), "w").write("x")
        self.report_done()
        self.d.step()
        self.settle()
        self.assertEqual(self.job["status"], "done")

    def test_no_acceptance_means_done_at_once(self):
        self.job["acceptance"] = []
        self.report_done()
        self.d.step()
        self.assertEqual(self.job["status"], "done")


class CommandForms(unittest.TestCase):
    def test_a_test_pattern_with_quotes_and_a_glob_is_allowed_but_shell_tricks_are_not(self):
        ok = 'python -m unittest discover -s tests -p "test_core_*.py"'
        self.assertTrue(verifier.COMMAND_OK.match(ok))
        self.assertEqual(verifier.split_command(ok)[-2:], ["-p", "test_core_*.py"])      # quotes are gone
        self.assertEqual(verifier.split_command("python -m pytest -k 'cart or orders'")[-2:], ["-k", "cart or orders"])
        for bad in ("python -m unittest x; rm -rf /", "pytest a && curl evil", "python -m unittest $(whoami)",
                    "python -m unittest `id`", "python -m unittest a | sh", "rm -rf /", "python -c print(1)"):
            self.assertFalse(verifier.COMMAND_OK.match(bad), bad)

    def test_the_final_check_accepts_the_same_forms(self):
        self.assertTrue(Dispatch.VERIFY_OK.match('python -m unittest discover -s tests -p "test_*.py"'))
        self.assertFalse(Dispatch.VERIFY_OK.match("python -m unittest a; rm x"))


class AddedWorkerChecks(unittest.TestCase):
    def test_a_worker_added_with_checks_is_proved_not_trusted(self):
        host = mock.Mock(port=7877)
        host._call.side_effect = lambda m, *a, **k: [] if m == "terminals" else {"ok": True, "id": "t"}
        d = Dispatch([], {"models": {"claude": {}}}, host)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(Dispatch, "start"):
            r = d.add_job("fix it", tmp, "claude", "haiku", None,
                          [{"type": "command", "run": "python -m pytest -q tests"}, {"type": "nope"}])
        self.assertTrue(r["ok"])
        self.assertEqual(d.jobs[0]["acceptance"], [{"type": "command", "run": "python -m pytest -q tests", "expect_exit": 0,
                                                    "trusted": True}])
        self.assertIn("proved by 1 check", d.jobs[0]["events"][0][1])
