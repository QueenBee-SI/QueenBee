"""The user chooses the queen's model; for the workers the user chooses or lets the queen advise.
And a queen can always be cleared or removed, with her workers stopped first."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from queenbee.mother import server
from queenbee.mother.dispatch import Dispatch
from queenbee.mother.server import Hive, Mother


class WorkerChoice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patches = [mock.patch("queenbee.config._state_dir", return_value=Path(self.tmp.name)),
                   mock.patch("queenbee.mother.server.threading.Thread"),
                   mock.patch("queenbee.mother.server._providers", return_value=[
                       {"id": "claude", "installed": True, "models": ["haiku", "sonnet", "opus"]},
                       {"id": "codex", "installed": True, "models": ["default"]},
                       {"id": "kimi", "installed": False, "models": []}])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.host = mock.Mock(port=7877)
        self.host._call.side_effect = lambda m, *a, **k: []
        self.hive = Hive(self.host)

    def test_the_queen_may_advise_on_agent_and_model_and_it_is_remembered(self):
        out = self.hive.add("Site", "auto", "auto")
        self.assertTrue(out["ok"])
        q = self.hive.queens[0]
        self.assertEqual((q.agent, q.model), ("auto", "auto"))
        self.assertEqual(q._agents(), ("claude", "codex"))              # every installed agent, per part
        self.assertTrue(q._advising())
        saved = json.loads((Path(self.tmp.name) / "queens.json").read_text(encoding="utf-8"))
        self.assertEqual((saved[0]["agent"], saved[0]["model"]), ("auto", "auto"))

    def test_a_chosen_model_is_not_advice(self):
        self.hive.add("Site", "claude", "haiku")
        q = self.hive.queens[0]
        self.assertFalse(q._advising())
        self.assertEqual(q._agents(), ("claude",))

    def test_a_worker_added_to_an_advising_queen_gets_the_defaults_not_the_word_auto(self):
        self.hive.add("Site", "auto", "auto")
        q = self.hive.queens[0]
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(Dispatch, "start"):
            q.add_worker("do a thing", folder)
        job = q.dispatch.jobs[0]
        self.assertEqual(job["agent"], "claude")
        self.assertNotEqual(job["tier"], "auto")


class ClearAndRemove(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for p in (mock.patch("queenbee.config._state_dir", return_value=Path(self.tmp.name)),
                  mock.patch("queenbee.mother.server.threading.Thread"),
                  mock.patch("queenbee.mother.server._providers", return_value=[
                      {"id": "claude", "installed": True, "models": ["haiku", "sonnet", "opus"]}])):
            p.start()
            self.addCleanup(p.stop)
        self.host = mock.Mock(port=7877)
        self.host._call.side_effect = lambda m, *a, **k: [] if m == "terminals" else {"ok": True, "id": "t"}
        self.hive = Hive(self.host)
        self.hive.add("First", "claude", "haiku")
        self.hive.queens[0].plan = {"instruction": "a first job"}          # so the next one is a new queen
        self.hive.add("Second", "claude", "haiku")
        self.first, self.second = self.hive.queens

    def work(self, queen):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(Dispatch, "start"):
            queen.add_worker("busy work", folder)
        queen.dispatch.jobs[0]["status"] = "agent working"

    def test_a_queen_at_work_is_removed_only_when_the_user_says_stop_them(self):
        self.work(self.second)
        refused = self.hive.remove(self.second.queen)
        self.assertFalse(refused["ok"])
        self.assertIn("still at work", refused["error"])
        self.assertTrue(self.hive.remove(self.second.queen, force=True)["ok"])
        self.assertEqual([q.queen for q in self.hive.queens], ["q1"])

    def test_the_first_queen_is_cleared_not_removed(self):
        self.work(self.first)
        self.assertIn("still at work", self.hive.remove(self.first.queen)["error"])
        self.assertFalse(self.hive.reset(self.first.queen)["ok"])               # workers still running
        self.assertTrue(self.hive.reset(self.first.queen, force=True)["ok"])
        self.assertIsNone(self.first.dispatch)

    def test_a_queen_that_is_still_planning_cannot_be_removed(self):
        self.second.planning = True
        self.assertIn("planning", self.hive.remove(self.second.queen, force=True)["error"])


class UserChecksAndRemoval(unittest.TestCase):
    def test_a_command_the_user_typed_is_run_but_one_from_a_plan_must_be_on_the_allowlist(self):
        from queenbee.mother import verifier
        runner = lambda argv, cwd, t: (0, "ok")
        mine = verifier.normalise([{"type": "command", "run": "python run_sim.py --seed 7 --check"}], trusted=True)
        theirs = verifier.normalise([{"type": "command", "run": "python run_sim.py --seed 7 --check"}])
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(verifier.run_checks(mine, d, runner)[0]["ok"])
            self.assertEqual(theirs, [])                     # off the allowlist from a plan: dropped, it could never pass
            forced = [{"type": "command", "run": "python run_sim.py --seed 7 --check", "expect_exit": 0}]
            self.assertFalse(verifier.run_checks(forced, d, runner)[0]["ok"])      # and still refused if one slips through

    def make(self):
        host = mock.Mock(port=7877)
        host._call.side_effect = lambda m, *a, **k: [] if m == "terminals" else {"ok": True, "id": "t"}
        d = Dispatch([], {"models": {"claude": {}}}, host)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(Dispatch, "start"):
            d.add_job("one", tmp, "claude", "haiku")
            d.add_job("two", tmp, "claude", "haiku")
        return d, host

    def test_only_a_finished_worker_can_be_removed_and_the_others_stay(self):
        d, host = self.make()
        d.jobs[0]["status"] = "agent working"
        self.assertIn("stop it first", d.remove_job(1)["error"])
        d.jobs[0]["status"] = "stopped"
        d.jobs[0]["terminal"] = "term-7"
        self.assertTrue(d.remove_job(1)["ok"])
        self.assertEqual([j["priority"] for j in d.jobs], [2])
        self.assertIn(mock.call("terminal_close", "term-7"), host._call.call_args_list)
        self.assertFalse(d.remove_job(99)["ok"])

    def test_a_worker_with_workers_under_it_cannot_be_removed(self):
        d, _ = self.make()
        d.jobs[0]["status"] = "done"
        d.jobs[1].update(parent=1, status="done")
        self.assertIn("workers under it", d.remove_job(1)["error"])
