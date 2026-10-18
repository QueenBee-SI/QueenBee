"""A worker on its own branch lives in a worktree; if it writes its report into the project's own .queenbee
instead of its worktree's, the queen still finds it (a run stalled for 40 minutes on exactly this)."""
import json
import os
import tempfile
import unittest

from queenbee.mother.dispatch import Dispatch


def put(folder, name, text):
    os.makedirs(os.path.join(folder, ".queenbee"), exist_ok=True)
    with open(os.path.join(folder, ".queenbee", name), "w", encoding="utf-8") as fh:
        fh.write(text)


class BranchReports(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.wt = os.path.join(self.root, ".queenbee_wt", "W3")
        os.makedirs(self.wt)
        self.job = {"priority": 3, "project_path": self.wt, "hive_root": self.root}

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_workers_own_folder_comes_first(self):
        put(self.wt, "W3.json", "{}")
        put(self.root, "W3.json", "{}")
        self.assertEqual(Dispatch.report_path(self.job), os.path.join(self.wt, ".queenbee", "W3.json"))

    def test_a_report_written_in_the_root_is_found(self):
        put(self.root, "W3.json", json.dumps({"status": "done"}))
        self.assertEqual(Dispatch.report_path(self.job), os.path.join(self.root, ".queenbee", "W3.json"))

    def test_no_report_anywhere_means_the_workers_own_path(self):
        self.assertEqual(Dispatch.report_path(self.job), os.path.join(self.wt, ".queenbee", "W3.json"))

    def test_a_worker_without_a_hive_root_is_unchanged(self):
        job = {"priority": 1, "project_path": self.root}
        put(self.root, "W1.json", "{}")
        self.assertEqual(Dispatch.report_path(job), os.path.join(self.root, ".queenbee", "W1.json"))


if __name__ == "__main__":
    unittest.main()


class ReportEncoding(unittest.TestCase):
    def test_a_report_with_a_non_utf8_byte_is_still_read(self):
        with tempfile.TemporaryDirectory() as folder:
            os.makedirs(os.path.join(folder, ".queenbee"))
            raw = b'{"status": "done", "changed_files": ["a.py"], "notes": "storage \x96 dml done"}'
            with open(os.path.join(folder, ".queenbee", "W2.json"), "wb") as fh:
                fh.write(raw)
            d = Dispatch.__new__(Dispatch)
            d.started_at = 0.0
            report = d._read_report({"priority": 2, "project_path": folder})
        self.assertEqual(report["status"], "done")
        self.assertIn("storage", report["notes"])
