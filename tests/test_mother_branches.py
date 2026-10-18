"""Branch-per-worker mode with real git in a temp directory."""

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from queenbee.mother import board, branches
from queenbee.mother.dispatch import Dispatch


def run(cwd, *args):
    return subprocess.run(["git", *branches.IDENT, *args], cwd=cwd, capture_output=True, text=True).stdout.strip()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class Git(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.dir.name, "proj")
        write(os.path.join(self.root, "a.py"), "A = 1\n")
        write(os.path.join(self.root, "b.py"), "B = 1\n")

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.root, capture_output=True)
        self.dir.cleanup()

    def test_a_plain_folder_becomes_a_repository_and_a_dirty_one_is_refused(self):
        self.assertIsNone(branches.prepare(self.root))
        self.assertTrue(os.path.isdir(os.path.join(self.root, ".git")))
        self.assertIn("a.py", run(self.root, "ls-files"))
        write(os.path.join(self.root, "a.py"), "A = 2\n")
        self.assertIn("uncommitted", branches.prepare(self.root))

    def test_workers_work_apart_and_the_queen_merges_what_passes(self):
        branches.prepare(self.root)
        p2, p3 = branches.add_worktree(self.root, 2), branches.add_worktree(self.root, 3)
        self.assertTrue(p2.endswith(os.path.join(".queenbee_wt", "W2")))
        write(os.path.join(p2, "a.py"), "A = 2\n")
        write(os.path.join(p3, "b.py"), "B = 3\n")
        write(os.path.join(p3, ".queenbee", "W3.json"), "{}")                 # never committed
        self.assertEqual(open(os.path.join(self.root, "a.py")).read(), "A = 1\n")  # main untouched meanwhile
        self.assertTrue(branches.commit_all(p2, "W2: a"))
        self.assertTrue(branches.commit_all(p3, "W3: b"))
        self.assertNotIn(".queenbee", run(p3, "ls-files"))
        ok, why = branches.merge(self.root, 2, "a", lambda: (True, ""))
        self.assertTrue(ok, why)
        ok, why = branches.merge(self.root, 3, "b", lambda: (True, ""))
        self.assertTrue(ok, why)
        self.assertEqual(open(os.path.join(self.root, "a.py")).read(), "A = 2\n")
        self.assertEqual(open(os.path.join(self.root, "b.py")).read(), "B = 3\n")

    def test_a_merge_that_breaks_the_check_is_undone(self):
        branches.prepare(self.root)
        p2 = branches.add_worktree(self.root, 2)
        write(os.path.join(p2, "a.py"), "A = 'broken'\n")
        branches.commit_all(p2, "W2")
        before = branches.head(self.root)
        ok, why = branches.merge(self.root, 2, "a", lambda: (False, "FAIL test_a"))
        self.assertFalse(ok)
        self.assertIn("FAIL test_a", why)
        self.assertEqual(branches.head(self.root), before)
        self.assertEqual(open(os.path.join(self.root, "a.py")).read(), "A = 1\n")

    def test_a_conflict_is_reported_and_main_stays_clean(self):
        branches.prepare(self.root)
        p2, p3 = branches.add_worktree(self.root, 2), branches.add_worktree(self.root, 3)
        write(os.path.join(p2, "a.py"), "A = 2\n")
        write(os.path.join(p3, "a.py"), "A = 3\n")
        branches.commit_all(p2, "W2")
        branches.commit_all(p3, "W3")
        self.assertTrue(branches.merge(self.root, 2, "a")[0])
        ok, why = branches.merge(self.root, 3, "a")
        self.assertFalse(ok)
        self.assertIn("conflict", why)
        self.assertEqual(run(self.root, "status", "--porcelain"), "")
        # W3 brings main in, resolves, and then it merges
        run(p3, "merge", "main")
        write(os.path.join(p3, "a.py"), "A = 23\n")
        run(p3, "add", "-A")
        run(p3, "commit", "-q", "-m", "resolve")
        self.assertTrue(branches.merge(self.root, 3, "a")[0])
        self.assertEqual(open(os.path.join(self.root, "a.py")).read(), "A = 23\n")


class Flow(unittest.TestCase):
    """The dispatch: a worktree per task worker, merge on done, notices when main moves."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.dir.name, "proj")
        write(os.path.join(self.root, "a.py"), "A = 1\n")
        write(os.path.join(self.root, "b.py"), "B = 1\n")
        self.host = mock.Mock(port=7877)
        self.host._call.side_effect = lambda m, *a, **k: [] if m == "terminals" else {"ok": True, "id": "t"}
        self.d = Dispatch([], {"models": {"claude": {}, "codex": {}}}, self.host)
        with mock.patch.object(Dispatch, "start"):
            self.d.add_job("part a", self.root, "claude", "haiku")
            self.d.add_job("part b", self.root, "claude", "haiku")
        for j in self.d.jobs:
            j.update(kind="task", work_order="1.", project_path=self.root)
        self.assertIsNone(self.d.enable_branches(self.root))
        self.d.state_of = lambda t: ("working", "")
        self.d._workloads = lambda: {}

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.root, capture_output=True)
        self.dir.cleanup()

    def settle(self):
        import threading
        for t in [t for t in threading.enumerate() if t.name.startswith("queen-accept")]:
            t.join(10)

    def test_each_worker_gets_a_branch_and_a_folder_of_its_own(self):
        with mock.patch.object(Dispatch, "_brief", return_value="go"):
            self.d.step()
        a, b = self.d.jobs
        self.assertEqual((a["branch"], b["branch"]), ("queen/W1", "queen/W2"))
        self.assertNotEqual(a["project_path"], b["project_path"])
        self.assertTrue(os.path.isdir(a["project_path"]) and a["project_path"].startswith(self.root))
        self.assertIn(a["project_path"], [c[0][1] for c in self.host._call.call_args_list if c[0][0] == "open_terminal"])

    def test_done_merges_into_main_then_the_other_worker_is_told(self):
        with mock.patch.object(Dispatch, "_brief", return_value="go"):
            self.d.step()
        a, b = self.d.jobs
        write(os.path.join(a["project_path"], "a.py"), "A = 2\n")
        write(a["project_path"] + os.sep + ".queenbee" + os.sep + "W1.json",
              '{"status": "done", "notes": "built"}')
        self.d._agent_alive = lambda j: True
        self.d.step()
        self.assertEqual(a["status"], "verifying")
        self.settle()
        self.assertEqual(a["status"], "done")
        self.assertEqual(open(os.path.join(self.root, "a.py")).read(), "A = 2\n")
        self.d.step()                                           # the sync notice
        mail = board.unread(b["project_path"], 2, 0)[0]
        self.assertTrue(any("git merge main" in line and "W1 merged" in line for line in mail), mail)
        self.assertIsNone(board.unread(a["project_path"], 1, 0)[0] or None)    # not told about its own merge

    def test_a_failing_merge_check_sends_the_worker_back_and_keeps_main_green(self):
        self.d.set_verify("python -m unittest discover -s tests", self.root)
        with mock.patch.object(Dispatch, "_brief", return_value="go"):
            self.d.step()
        a = self.d.jobs[0]
        write(os.path.join(a["project_path"], "a.py"), "A = 'x'\n")
        write(a["project_path"] + os.sep + ".queenbee" + os.sep + "W1.json", '{"status": "done"}')
        self.d._agent_alive = lambda j: True
        with mock.patch("queenbee.mother.dispatch.run_captured", return_value=(1, "FAIL: test_a")):
            self.d.step()
            self.settle()
        self.assertEqual((a["status"], a["accept_round"]), ("agent working", 1))
        self.assertEqual(open(os.path.join(self.root, "a.py")).read(), "A = 1\n")      # main untouched
        self.assertTrue(any("git merge main" in str(c) for c in self.host._call.call_args_list))

    def test_a_dirty_project_keeps_the_shared_folder(self):
        write(os.path.join(self.root, "a.py"), "A = 9\n")
        d = Dispatch([], {"models": {"claude": {}}}, self.host)
        self.assertIn("uncommitted", d.enable_branches(self.root))
        self.assertIsNone(d.branches)
