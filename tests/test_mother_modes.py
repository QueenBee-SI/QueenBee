import json
import os
import tempfile
import unittest
from unittest import mock

from queenbee.mother import modes, planner

BIG = ("Build the whole thing:\n- a cart module\n- an orders module\n- an HTTP API for all of it\n"
       "and a front end, each with tests.")


class Classify(unittest.TestCase):
    def test_small_vs_big(self):
        self.assertTrue(modes.looks_small("Fix the typo in README.md title"))
        self.assertTrue(modes.looks_small("fix the wrong assert inside tests/test_cart.py"))
        self.assertFalse(modes.looks_small(BIG))
        self.assertFalse(modes.looks_small("Try lambda from 0.1 upward and 1.0 downward in parallel"))
        self.assertFalse(modes.looks_small("Migrate every report module to the new API"))
        self.assertFalse(modes.looks_small("x" * 400))
        self.assertFalse(modes.looks_small(""))

    def test_mode_of(self):
        self.assertEqual(modes.mode_of({"jobs": [{"kind": "task"}]}), "DIRECT")
        self.assertEqual(modes.mode_of({"jobs": [{"kind": "task"}, {"kind": "task"}]}), "PARALLEL")
        self.assertEqual(modes.mode_of({"jobs": [{"kind": "gpu"}]}), "COMPILE")
        self.assertEqual(modes.mode_of({"jobs": [{"kind": "task"}], "sweep": {"param": "--x"}}), "COMPILE")


class PlanRouting(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.folder = self.dir.name
        open(os.path.join(self.folder, "a.py"), "w").write("x = 1\n")

    def tearDown(self):
        self.dir.cleanup()

    def run_plan(self, text, calls):
        reply = {"result": json.dumps({"jobs": [{"priority": 1, "title": "t", "goal": "g", "effort": "high"}],
                                       "verify": ""}), "usage": {"output_tokens": 9}, "total_cost_usd": 0.05}

        def fake(argv, **kw):
            calls.append(argv)
            return mock.Mock(stdout=json.dumps(reply), stderr="")
        with mock.patch.object(planner, "binary_for", return_value="claude"), \
                mock.patch.object(planner.subprocess, "run", side_effect=fake):
            return planner.plan(text, "sonnet", folder=self.folder)

    def test_small_instruction_makes_no_model_call(self):
        calls = []
        out = self.run_plan("Rename x to count in a.py", calls)
        self.assertEqual(calls, [])
        self.assertEqual(out["mode"], "DIRECT")
        self.assertEqual(len(out["jobs"]), 1)
        self.assertEqual(out["jobs"][0]["effort"], "high")
        self.assertEqual(out["planner"]["cost_usd"], 0.0)

    def test_big_instruction_is_planned_once_then_reused(self):
        calls = []
        first = self.run_plan(BIG, calls)
        again = self.run_plan(BIG, calls)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["mode"], "DIRECT")
        self.assertEqual(again["mode"], "RECIPE")
        self.assertEqual(again["planner"]["cost_usd"], 0.0)
        self.assertEqual(again["jobs"], first["jobs"])

    def test_changed_project_is_planned_again(self):
        calls = []
        self.run_plan(BIG, calls)
        open(os.path.join(self.folder, "b.py"), "w").write("y = 2\n")
        self.run_plan(BIG, calls)
        self.assertEqual(len(calls), 2)


class Strategy(PlanRouting):
    def test_single_never_calls_the_planner_even_for_a_big_instruction(self):
        calls = []
        with mock.patch.object(planner, "binary_for", return_value="claude"), \
                mock.patch.object(planner.subprocess, "run", side_effect=lambda *a, **k: calls.append(a)):
            out = planner.plan(BIG, "sonnet", folder=self.folder, strategy="single")
        self.assertEqual((calls, len(out["jobs"]), out["mode"]), ([], 1, "DIRECT"))

    def test_multi_plans_even_a_small_instruction_and_tells_the_planner_to_split(self):
        calls = []
        out = None
        reply = {"result": json.dumps({"jobs": [{"priority": 1, "title": "a", "goal": "g", "kind": "task"},
                                                {"priority": 2, "title": "b", "goal": "g", "kind": "task",
                                                 "after": [1]}]}), "usage": {}}

        def fake(argv, **kw):
            calls.append(kw["input"])        # the prompt goes in on stdin
            return mock.Mock(stdout=json.dumps(reply), stderr="")
        with mock.patch.object(planner, "binary_for", return_value="claude"), \
                mock.patch.object(planner.subprocess, "run", side_effect=fake):
            out = planner.plan("Rename x to count in a.py", "sonnet", folder=self.folder, strategy="multi")
        self.assertEqual(len(calls), 1)
        self.assertIn("MULTI-AGENT", calls[0])
        self.assertEqual(len(out["jobs"]), 2)


class WorkerCap(PlanRouting):
    def test_the_cap_reaches_the_planner_and_is_part_of_the_plan_key(self):
        calls = []
        reply = {"result": json.dumps({"jobs": [{"priority": 1, "title": "a", "goal": "g", "kind": "task"},
                                                {"priority": 2, "title": "b", "goal": "g", "kind": "task"}]}),
                 "usage": {}}

        def fake(argv, **kw):
            calls.append(kw["input"])        # the prompt goes in on stdin
            return mock.Mock(stdout=json.dumps(reply), stderr="")
        with mock.patch.object(planner, "binary_for", return_value="claude"), \
                mock.patch.object(planner.subprocess, "run", side_effect=fake):
            planner.plan(BIG, "sonnet", folder=self.folder, strategy="multi", max_workers=3)
            planner.plan(BIG, "sonnet", folder=self.folder, strategy="multi", max_workers=2)   # other cap: not reused
            planner.plan(BIG, "sonnet", folder=self.folder, strategy="multi", max_workers=2)   # same cap: reused
        self.assertEqual(len(calls), 2)
        self.assertIn("at most 3 workers", calls[0])


class Layout(PlanRouting):
    def prompt_for(self, layout):
        calls = []
        reply = {"result": json.dumps({"jobs": [{"priority": 1, "title": "a", "goal": "g", "kind": "task"},
                                                {"priority": 2, "title": "b", "goal": "g", "kind": "task"}]}),
                 "usage": {}}

        def fake(argv, **kw):
            calls.append(kw["input"])        # the prompt goes in on stdin
            return mock.Mock(stdout=json.dumps(reply), stderr="")
        with mock.patch.object(planner, "binary_for", return_value="claude"), \
                mock.patch.object(planner.subprocess, "run", side_effect=fake):
            planner.plan(BIG, "sonnet", folder=self.folder, strategy="multi", layout=layout)
        return calls[0]

    def test_team_has_a_senior_and_peers_do_not(self):
        team, peers = self.prompt_for("team"), self.prompt_for("peers")
        self.assertIn("one senior, several juniors", team)
        self.assertNotIn("PEERS", team)
        self.assertIn("run by PEERS", peers)
        self.assertNotIn("one senior, several juniors", peers)
