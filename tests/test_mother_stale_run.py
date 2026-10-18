"""A folder an earlier plan worked in still holds that run's progress, inbox and board: a new worker does not inherit them
(the queen once judged a new worker "drifted" by the old run's progress lines and told it to stop)."""
import os
import tempfile
import unittest
from unittest import mock

from queenbee.mother import board, config
from queenbee.mother.dispatch import Dispatch


def put(folder, rel, text):
    path = os.path.join(folder, ".queenbee", *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class StaleRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        put(self.root, "W1.progress", "old line about thermal images\nanother old line\n")
        put(self.root, "inbox/W1.md", "an old message")
        put(self.root, "board.jsonl", '{"from": "queen", "to": ["W1"], "text": "old"}\n')

    def tearDown(self):
        self.tmp.cleanup()

    def dispatch(self):
        d = Dispatch([], config.load(), mock.MagicMock(), 7878)
        d.jobs.append({"priority": 1, "kind": "task", "title": "t", "status": "queued", "project_path": self.root,
                       "agent": "claude", "events": []})
        return d

    def test_a_new_worker_skips_the_old_progress_and_loses_the_old_inbox(self):
        d = self.dispatch()
        job = d.jobs[0]
        d._forget_earlier_run(job)
        self.assertEqual(job["progress_at"], os.path.getsize(os.path.join(self.root, ".queenbee", "W1.progress")))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".queenbee", "inbox", "W1.md")))
        d._read_progress(job)
        self.assertFalse(job.get("progress"))                           # nothing of the old run is shown as its progress
        with open(os.path.join(self.root, ".queenbee", "W1.progress"), "a", encoding="utf-8") as fh:
            fh.write("W1: really starting\n")
        d._read_progress(job)
        self.assertIn("really starting", job["progress"])

    def test_a_new_plan_does_not_route_the_old_board(self):
        d = self.dispatch()
        with mock.patch("queenbee.mother.dispatch.threading.Thread"):
            d.start()
        key = os.path.normcase(os.path.abspath(self.root))
        self.assertEqual(d._board_at[key], os.path.getsize(board.board_path(self.root)))
        msgs, _ = board.read_new(self.root, d._board_at[key])
        self.assertEqual(msgs, [])


if __name__ == "__main__":
    unittest.main()


class Reaccept(unittest.TestCase):
    def make(self, other_status="done", quiet_s=600, accept_round=1):
        d = Dispatch([], config.load(), mock.MagicMock(), 7878)
        old = __import__("time").time() - quiet_s
        d.jobs.append({"priority": 1, "kind": "task", "title": "a", "status": "agent working", "project_path": ".",
                       "agent": "claude", "accept_round": accept_round, "acceptance": [{"type": "command", "run": "x"}],
                       "events": [[old, "acceptance failed; sent back"]]})
        d.jobs.append({"priority": 2, "kind": "task", "title": "b", "status": other_status, "project_path": ".",
                       "agent": "claude", "events": []})
        return d

    def test_a_quiet_worker_sent_back_is_checked_again_once_the_others_are_in(self):
        d = self.make()
        with mock.patch.object(Dispatch, "_accept", return_value=True) as accept:
            self.assertTrue(d._reaccept_when_alone(d.jobs[0]))
        accept.assert_called_once()
        self.assertEqual(d.jobs[0]["status"], "done")

    def test_not_while_another_worker_is_still_at_work(self):
        d = self.make(other_status="agent working")
        with mock.patch.object(Dispatch, "_accept") as accept:
            self.assertFalse(d._reaccept_when_alone(d.jobs[0]))
        accept.assert_not_called()

    def test_not_for_a_worker_that_was_never_sent_back_or_that_is_not_quiet(self):
        with mock.patch.object(Dispatch, "_accept") as accept:
            self.assertFalse(self.make(accept_round=0)._reaccept_when_alone(self.make(accept_round=0).jobs[0]))
            d = self.make(quiet_s=30)
            self.assertFalse(d._reaccept_when_alone(d.jobs[0]))
        accept.assert_not_called()

    def test_not_again_within_the_wait(self):
        d = self.make()
        with mock.patch.object(Dispatch, "_accept", return_value=False):
            self.assertTrue(d._reaccept_when_alone(d.jobs[0]))
            self.assertFalse(d._reaccept_when_alone(d.jobs[0]))
