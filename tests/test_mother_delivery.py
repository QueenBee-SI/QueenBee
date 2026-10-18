"""The delivery check is not run (and no integration worker is sent) when a worker did not finish."""
import unittest
from unittest import mock

from queenbee.mother import config
from queenbee.mother.dispatch import Dispatch


class DeliveryCheck(unittest.TestCase):
    def make(self, statuses):
        d = Dispatch([], config.load(), mock.MagicMock(), 7878)
        for n, status in enumerate(statuses, 1):
            d.jobs.append({"priority": n, "kind": "task", "title": f"w{n}", "status": status, "project_path": ".",
                           "events": []})
        d.set_verify("python -m unittest discover", ".")
        return d

    def test_stopped_worker_means_no_check_and_no_fixer(self):
        d = self.make(["done", "stopped", "done"])
        with mock.patch("queenbee.mother.dispatch.threading.Thread") as thread:
            pending = d._verify_pending()
        self.assertFalse(pending)
        self.assertEqual(d.verify["state"], "failed")
        self.assertIn("W2", d.verify["log"])
        thread.assert_not_called()
        self.assertEqual(len(d.jobs), 3)

    def test_a_worker_blocked_on_another_part_with_its_files_written_is_left_to_the_delivery_check(self):
        d = self.make(["agent working", "done"])
        d.cfg["escalation"] = ["sonnet"]
        job = d.jobs[0]
        job.update(agent="claude", tier="sonnet", terminal="t1", report={"status": "blocked", "changed_files": ["a.py"]})
        d._escalate(job, "waits for W2's executor")
        self.assertEqual(job["status"], "done")
        self.assertIn("delivery check decides", job["events"][-1][1])

    def test_a_blocked_worker_with_nothing_written_still_fails(self):
        d = self.make(["agent working", "done"])
        d.cfg["escalation"] = ["sonnet"]
        job = d.jobs[0]
        job.update(agent="claude", tier="sonnet", terminal="t1", report={"status": "blocked", "changed_files": []})
        d._escalate(job, "cannot start")
        self.assertEqual(job["status"], "failed")

    def test_a_worker_whose_own_acceptance_failed_has_its_files_in_so_the_check_runs(self):
        # Video run V2: the parser worker's own test was wrong (the queen said so), it ended "failed" after two fix rounds,
        # and the whole-package check was never run: 153 of 154 with no integration fix.
        d = self.make(["done", "done", "failed"])
        d.jobs[2]["error"] = "acceptance failed after 2 fix round(s): python -m unittest tests.test_parser"
        with mock.patch("queenbee.mother.dispatch.threading.Thread") as thread:
            self.assertTrue(d._verify_pending())
        thread.assert_called_once()
        self.assertEqual(d.verify["state"], "running")

    def test_all_done_runs_the_check(self):
        d = self.make(["done", "done"])
        with mock.patch("queenbee.mother.dispatch.threading.Thread") as thread:
            self.assertTrue(d._verify_pending())
        thread.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class SecondQueen(unittest.TestCase):
    def test_a_set_up_queen_is_not_reused_by_the_next_one(self):
        import os
        import tempfile
        from queenbee.mother.server import Hive

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"QUEENBEE_STATE_DIR": tmp}):
            hive = Hive(mock.MagicMock(), 7878)
            first = hive.add("Alpha", "", "", None, None, "the first", True)
            second = hive.add("Beta", "", "", None, None, "the second", True)
            self.assertNotEqual(first["queen"], second["queen"])
            self.assertEqual([q.name for q in hive.queens], ["Alpha", "Beta"])


class ShippedFiles(unittest.TestCase):
    def test_every_file_the_page_asks_for_exists(self):
        from queenbee.mother.server import FILES
        missing = [url for url, path in FILES.items() if not path.is_file()]
        self.assertEqual(missing, [])


class ReportsThatAreNotQuiteJson(unittest.TestCase):
    def test_a_report_with_a_stray_backslash_is_still_read(self):
        import json
        import os
        import tempfile
        from queenbee.mother import config
        from queenbee.mother.dispatch import Dispatch

        with tempfile.TemporaryDirectory() as tmp:
            d = Dispatch([], config.load(), mock.MagicMock(), 7878)
            job = {"priority": 3, "project_path": tmp, "launched_at": 0}
            os.makedirs(os.path.join(tmp, ".queenbee"))
            path = d.report_path(job)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"status": "done", "notes": "mention regex @name(?!\w) case-insensitive"}')
            report = d._read_report(job)
            self.assertEqual(report["status"], "done")
            self.assertIn(r"(?!\w)", report["notes"])
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"status": "blocked", "notes": "broken "quote" inside}')
            self.assertEqual(d._read_report(job)["status"], "blocked")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("not a report at all")
            self.assertIsNone(d._read_report(job))


class PortablePython(unittest.TestCase):
    def test_a_codex_brief_names_a_python_it_can_run_and_a_claude_brief_does_not(self):
        from queenbee.mother import config
        from queenbee.mother.dispatch import Dispatch

        d = Dispatch([], config.load(), mock.MagicMock(), 7878)
        with mock.patch("queenbee.mother.pyenv.portable_python", return_value=r"C:\state\python\python.exe"):
            note = d._python_note({"agent": "codex"})
            self.assertIn(r"C:\state\python\python.exe", note)
            self.assertIn("python", note)
            self.assertEqual(d._python_note({"agent": "claude"}), "")
        with mock.patch("queenbee.mother.pyenv.portable_python", return_value=""):
            self.assertEqual(d._python_note({"agent": "codex"}), "")
