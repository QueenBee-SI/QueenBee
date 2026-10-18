"""Regressions from the first real Mother runs (2026-09-27)."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from queenbee.gpu import normalise_command
from queenbee.mother.architect import match_orders
from queenbee.mother.dispatch import Dispatch


class SubmitCommandTests(unittest.TestCase):
    def test_whole_command_in_one_string_is_split(self):
        cmd = normalise_command([f'"{sys.executable}" train.py --epochs 3'])
        self.assertEqual(cmd, [sys.executable, "train.py", "--epochs", "3"])

    def test_missing_program_is_refused_before_submit(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            normalise_command(["../../AppData/python.exe Reem_Training.py"])

    def test_normal_argv_is_untouched(self):
        self.assertEqual(normalise_command([sys.executable, "a b.py"]), [sys.executable, "a b.py"])


class OrderMatchTests(unittest.TestCase):
    def test_plan_numbers_used_when_present(self):
        jobs = [{"priority": 1}, {"priority": 3}]
        orders = {1: {"work_order": "a"}, 3: {"work_order": "c"}}
        self.assertEqual(match_orders(jobs, orders)[3]["work_order"], "c")

    def test_folder_renumbered_from_one_still_matches(self):
        self.assertEqual(match_orders([{"priority": 4}], {1: {"work_order": "x"}})[4]["work_order"], "x")


class EscalationTests(unittest.TestCase):
    def test_blocked_codex_worker_moves_to_claude_then_sonnet(self):
        host = mock.Mock(port=7877)
        host._call.return_value = {"ok": True, "id": "term-9"}
        cfg = {"models": {"claude": {}, "codex": {}}, "worker_model": "haiku", "escalation": ["sonnet"]}
        with tempfile.TemporaryDirectory() as folder:
            job = {"priority": 1, "title": "t", "goal": "g", "project_path": folder, "agent": "codex",
                   "effort": "low", "same_as": None, "work_order": "1. run", "terminal": "term-1"}
            d = Dispatch([job], cfg, host)
            d._argv = lambda j, p: ["x"]
            d._escalate(d.jobs[0], "sandbox could not start python")
            self.assertEqual((d.jobs[0]["agent"], d.jobs[0]["tier"]), ("claude", "haiku"))
            d._escalate(d.jobs[0], "still blocked")
            self.assertEqual(d.jobs[0]["tier"], "sonnet")
            d._escalate(d.jobs[0], "blocked again")
            self.assertEqual(d.jobs[0]["status"], "failed")


class VerifySubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = self.tmp.name
        self.terms = {"term-1": {"id": "term-1", "running": True, "offset": 10, "started_at": time.time()}}
        self.output = "Traceback (most recent call last):\nFileNotFoundError: [WinError 2]"
        host = mock.Mock(port=7877)
        host._call.side_effect = lambda m, *a, **k: (
            list(self.terms.values()) if m == "terminals"
            else {"data": self.output} if m == "terminal_read" else {"ok": True})
        job = {"priority": 1, "title": "t", "goal": "g", "project_path": self.folder,
               "agent": "claude", "effort": "low", "same_as": None, "work_order": "1. run"}
        self.d = Dispatch([job], {"models": {"claude": {}, "codex": {}}}, host)
        self.d._workloads = lambda: {}
        self.d.jobs[0].update(terminal="term-1", status="agent working", launched_at=time.time() - 5)
        self.d._escalate = mock.Mock()

    def report(self, term):
        p = Path(Dispatch.report_path(self.d.jobs[0]))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"status": "submitted", "terminal": "%s"}' % term, encoding="utf-8")

    def test_report_links_the_job_and_a_crash_is_not_done(self):
        self.terms["term-2"] = {"id": "term-2", "running": True, "offset": 50, "started_at": time.time()}
        self.report("term-2")
        self.d.step()
        self.assertEqual(self.d.jobs[0]["workload"], "term-2")
        self.terms["term-2"]["running"] = False           # it died at once
        self.d.step()
        self.assertNotEqual(self.d.jobs[0]["status"], "done")
        why = self.d._escalate.call_args[0][1]
        self.assertIn("exited at once", why)
        self.assertIn("FileNotFoundError", why)

    def test_a_long_clean_run_is_done(self):
        self.terms["term-2"] = {"id": "term-2", "running": True, "offset": 50,
                                "started_at": time.time() - 600}
        self.output = "Epoch 10 done\nWrote summary"
        self.report("term-2")
        self.d.step()
        self.terms["term-2"]["running"] = False
        self.d.step()
        self.assertEqual(self.d.jobs[0]["status"], "done")
        self.d._escalate.assert_not_called()

    def test_stale_report_from_an_earlier_attempt_is_ignored(self):
        self.report("term-2")
        self.d.jobs[0]["launched_at"] = time.time() + 30  # relaunched well after the report
        self.assertIsNone(self.d._read_report(self.d.jobs[0]))


class DirectSubmitTests(unittest.TestCase):
    def test_same_as_job_is_submitted_by_mother_without_an_agent(self):
        host = mock.Mock(port=7877)
        host._call.side_effect = lambda m, *a, **k: [] if m == "terminals" else {"ok": True, "id": "term-5"}
        with tempfile.TemporaryDirectory() as folder:
            cmd = (f'"{sys.executable}" -m queenbee.gpu --port 7877 submit --project-root "{folder}" '
                   f'--name "b" --role secondary -- "{sys.executable}" train.py --lam 0.7')
            jobs = [{"priority": 1, "title": "a", "goal": "g", "project_path": folder, "agent": "codex",
                     "effort": "low", "same_as": None, "work_order": "1."},
                    {"priority": 2, "title": "b", "goal": "g", "project_path": folder, "agent": "codex",
                     "effort": "low", "same_as": 1, "work_order": "1.", "command": cmd}]
            d = Dispatch(jobs, {"models": {"claude": {}, "codex": {}}}, host)
            d._workloads = lambda: {}
            d.jobs[0].update(status="GPU job submitted", terminal="term-1", workload="term-2",
                             submitted_at=time.time() - 600)       # its run has proven the script
            ran = mock.Mock(returncode=0, stdout='{"ok": true, "id": "term-7"}' + chr(10), stderr="")
            with mock.patch("queenbee.mother.dispatch.subprocess.run", return_value=ran) as run:
                d.step()
            self.assertEqual(run.call_args[0][0][-1], "0.7")
            self.assertEqual((d.jobs[1]["workload"], d.jobs[1]["status"]), ("term-7", "GPU job submitted"))
            self.assertIsNone(d.jobs[1]["terminal"])
            opened = [c for c in host._call.call_args_list if c[0][0] == "open_terminal"]
            self.assertEqual(opened, [])

    def _pair(self, folder, host):
        cmd = (f'"{sys.executable}" -m queenbee.gpu --port 7877 submit --project-root "{folder}" '
               f'--name "b" --role secondary -- "{sys.executable}" train.py --lam 0.7')
        jobs = [{"priority": 1, "title": "a", "goal": "g", "project_path": folder, "agent": "claude",
                 "effort": "low", "same_as": None, "work_order": "1."},
                {"priority": 2, "title": "b", "goal": "g", "project_path": folder, "agent": "claude",
                 "effort": "low", "same_as": 1, "work_order": "1.", "command": cmd}]
        d = Dispatch(jobs, {"models": {"claude": {}, "codex": {}}, "escalation": ["sonnet"]}, host)
        d._workloads = lambda: {}
        return d

    def test_a_copy_waits_until_the_script_has_proven_itself(self):
        host = mock.Mock(port=7877)
        host._call.side_effect = lambda m, *a, **k: [] if m == "terminals" else {"ok": True}
        with tempfile.TemporaryDirectory() as folder:
            d = self._pair(folder, host)
            d.jobs[0].update(status="GPU job submitted", terminal="term-1", workload="term-2",
                             submitted_at=time.time() - 5)          # just started
            with mock.patch("queenbee.mother.dispatch.subprocess.run") as run:
                d.step()
            run.assert_not_called()
            self.assertEqual((d.jobs[1]["status"], d.jobs[1]["waiting_for"]), ("queued", 1))

    def test_a_failed_copy_waits_for_the_owner_instead_of_opening_an_agent(self):
        terms = {"term-2": {"id": "term-2", "running": True, "offset": 1, "started_at": time.time()},
                 "term-7": {"id": "term-7", "running": False, "offset": 1, "started_at": time.time() - 5}}
        opened = []
        host = mock.Mock(port=7877)

        def call(m, *a, **k):
            if m == "terminals":
                return list(terms.values())
            if m == "terminal_read":
                return {"data": ""}
            if m == "open_terminal":
                opened.append(a)
            return {"ok": True, "id": "term-9"}
        host._call.side_effect = call
        with tempfile.TemporaryDirectory() as folder:
            d = self._pair(folder, host)
            d.jobs[0].update(status="GPU job submitted", terminal="term-1", workload="term-2",
                             submitted_at=time.time() - 5)          # not yet proven
            d.jobs[1].update(status="GPU job submitted", workload="term-7", direct=True,
                             launched_at=time.time() - 5, submitted_at=time.time() - 5)
            d.step()
            self.assertEqual(opened, [])
            self.assertEqual((d.jobs[1]["status"], d.jobs[1]["same_as"]), ("queued", 1))

    def test_other_commands_still_go_to_an_agent(self):
        d = Dispatch([], {"models": {}}, mock.Mock())
        with tempfile.TemporaryDirectory() as folder:
            job = {"project_path": folder, "command": f'"{sys.executable}" train.py'}
            self.assertIsNone(d._direct_argv(job))
            job["command"] = fr'"{sys.executable}" -m queenbee.gpu submit --project-root "C:\Windows" -- x'
            self.assertIsNone(d._direct_argv(job))


class PreparedScopeTests(unittest.TestCase):
    def test_folder_named_in_the_started_work_order_is_in_scope(self):
        from queenbee.mother.approver import classify
        with tempfile.TemporaryDirectory() as project:
            data = r"D:\datasets\irstd"
            cmd = f'$ python train.py --dataset-dir "{data}"'
            q = {"text": "Reason: submit?", "options": [{"n": "1", "text": "Yes"}],
                 "context": ["Would you like to run the following command?", cmd]}
            self.assertEqual(classify(q, project)[0], "human")
            self.assertEqual(classify(q, project, f'python train.py --dataset-dir "{data}"')[0], "approve")


class ArchitectContextTests(unittest.TestCase):
    def test_named_files_are_read_for_the_architect(self):
        from queenbee.mother.architect import _given_block, given_files
        with tempfile.TemporaryDirectory() as folder:
            os.makedirs(os.path.join(folder, "training"))
            os.makedirs(os.path.join(folder, "weights"))
            with open(os.path.join(folder, "training", "train_reem.py"), "w") as f:
                f.write("print('train')" + chr(10))
            with open(os.path.join(folder, "weights", "big.py"), "w") as f:
                f.write("x")
            files = given_files(folder, ["run train_reem.py with lambda 0.1", "missing.py"])
            self.assertEqual([r for r, _ in files], [os.path.join("training", "train_reem.py")])
            block = _given_block(folder, [{"title": "t", "goal": "training/train_reem.py sweep"}])
            self.assertIn("print('train')", block)
            self.assertNotIn("big.py", block)


if __name__ == "__main__":
    unittest.main()
