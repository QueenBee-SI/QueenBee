"""The user steering jobs from the window: model, letting a job go, and making
a job the Mother of its own branch; sweep values hang under their adapter."""

import tempfile
import time
import unittest
from unittest import mock

from queenbee.mother import sweep
from queenbee.mother.dispatch import Dispatch


def job(n, **kw):
    base = {"priority": n, "title": f"j{n}", "goal": "g", "project_path": ".", "agent": "claude",
            "effort": "low", "same_as": None, "work_order": "1."}
    base.update(kw)
    return base


class SteerTests(unittest.TestCase):
    def setUp(self):
        self.host = mock.Mock(port=7877)
        self.host._call.side_effect = lambda m, *a, **k: [] if m == "terminals" else {"ok": True, "id": "term-9"}
        self.d = Dispatch([job(1), job(2)], {"models": {"claude": {}, "codex": {}}}, self.host)
        self.d._workloads = lambda: {}

    def test_every_job_starts_under_mother_and_managed(self):
        self.assertEqual([(j["parent"], j["detached"], j["role"]) for j in self.d.jobs],
                         [(None, False, None)] * 2)

    def test_model_and_role_changes_are_recorded(self):
        r = self.d.set_job(2, {"tier": "sonnet", "role": "mother"})
        self.assertTrue(r["ok"])
        self.assertEqual((self.d.jobs[1]["tier"], self.d.jobs[1]["role"]), ("sonnet", "mother"))
        self.assertIn("you set", self.d.jobs[1]["events"][-1][1])
        self.assertFalse(self.d.set_job(2, {"tier": "not a model!"})["ok"])
        self.assertFalse(self.d.set_job(7, {"tier": "haiku"})["ok"])

    def test_a_detached_job_is_not_started(self):
        self.d.set_job(1, {"detached": True})
        self.d.step()
        self.assertEqual(self.d.jobs[0]["status"], "queued")
        opened = [c for c in self.host._call.call_args_list if c[0][0] == "open_terminal"]
        self.assertEqual(opened, [])                         # W2 waits for W1 in the same folder
        self.d.set_job(1, {"detached": False})
        self.d.step()
        self.assertNotEqual(self.d.jobs[0]["status"], "queued")


class BranchTests(unittest.TestCase):
    """A job made a Mother plans its own agents, which hang under it."""

    def setUp(self):
        self.host = mock.Mock(port=7877)
        self.host._call.side_effect = lambda m, *a, **k: [] if m == "terminals" else {"ok": True, "id": "term-9"}
        self.tmp = tempfile.mkdtemp()
        self.d = Dispatch([job(1), job(2, project_path=self.tmp)], {"models": {"claude": {}, "codex": {}},
                                                                    "worker_model": "haiku"}, self.host)
        self.d._workloads = lambda: {}
        self.asked = []

        def planner(instruction, folder):
            self.asked.append((instruction, folder))
            return {"jobs": [job(1, title="front", project_path="", command="py -m queenbee.gpu submit --role primary x"),
                             job(2, title="back", project_path="", same_as=1)],
                    "costs": {"tokens": 1200, "cost_usd": 0.05}}
        self.d.branch_planner = planner

    def settle(self, prio):
        j = next(j for j in self.d.jobs if j["priority"] == prio)
        for _ in range(200):
            if j["status"] != "planning branch":
                return j
            time.sleep(0.01)
        self.fail("the branch never finished planning")

    def test_a_queued_branch_plans_instead_of_starting_an_agent(self):
        self.assertTrue(self.d.set_job(2, {"role": "mother", "instruction": "split front and back"})["ok"])
        self.d.step()
        p2 = self.settle(2)
        self.assertEqual(p2["status"], "branch running")
        self.assertIsNone(p2["terminal"])
        self.assertEqual(self.asked, [("split front and back", self.tmp)])
        kids = [j for j in self.d.jobs if j["parent"] == 2]
        self.assertEqual([(k["priority"], k["title"], k["same_as"]) for k in kids],
                         [(3, "front", None), (4, "back", 3)])
        self.assertIn("--role secondary", kids[0]["command"])
        self.assertEqual(kids[0]["tier"], "haiku")
        self.assertEqual(self.d.costs["branches"], {"tokens": 1200, "cost_usd": 0.05})
        # The branch's own agents start like any others.
        self.d.step()
        self.assertEqual(kids[0]["status"], "agent working")

    def test_a_branch_ends_with_its_agents(self):
        self.d.set_job(2, {"role": "mother"})
        self.d.step()
        self.settle(2)
        for k in self.d.jobs:
            if k["parent"] == 2:
                k["status"] = "done"
        self.d.step()
        self.assertEqual(self.d.jobs[1]["status"], "done")
        self.assertEqual(self.d.jobs[1]["instruction"], "g")       # the job's own goal by default

    def test_finished_work_branches_on_and_reopens_the_plan(self):
        for j in self.d.jobs:
            j["status"] = "done"
        self.d.done.set()
        with mock.patch.object(Dispatch, "start") as start:
            self.assertTrue(self.d.set_job(1, {"role": "mother", "instruction": "now test it"})["ok"])
        start.assert_called_once()
        self.assertFalse(self.d.done.is_set())
        self.d.step()
        self.assertEqual(self.settle(1)["status"], "branch running")

    def test_a_marked_job_does_not_let_the_plan_end_before_it_branches(self):
        self.d.jobs[0]["status"] = "done"
        self.d.jobs[1].update(status="agent working", terminal="t5", launched_at=0)
        self.d.set_job(2, {"role": "mother"})           # marked while its agent works
        self.d.step()                                   # ...whose terminal has closed
        self.assertEqual(self.d.jobs[1]["status"], "stopped")
        self.assertFalse(self.d.done.is_set())
        self.d.step()
        self.assertEqual(self.settle(2)["status"], "branch running")

    def test_a_branch_cannot_be_undone_once_planned(self):
        self.d.set_job(2, {"role": "mother"})
        self.d.step()
        self.settle(2)
        self.assertFalse(self.d.set_job(2, {"role": None})["ok"])

    def test_a_failed_branch_plan_fails_the_job(self):
        def broken(instruction, folder):
            raise RuntimeError("planner down")
        self.d.branch_planner = broken
        self.d.set_job(1, {"role": "mother"})
        self.d.step()
        p1 = self.settle(1)
        self.assertEqual(p1["status"], "failed")
        self.assertIn("planner down", p1["error"])


class NewTaskTests(unittest.TestCase):
    """The user changing what an agent does: before it starts, while it works,
    and after it has finished."""

    def setUp(self):
        self.writes = []
        self.host = mock.Mock(port=7877)

        def call(m, *a, **k):
            if m == "terminal_write":
                self.writes.append(a)
            return [] if m == "terminals" else {"ok": True, "id": "term-9"}
        self.host._call.side_effect = call
        self.d = Dispatch([job(1), job(2)], {"models": {"claude": {}, "codex": {}}}, self.host)
        self.d._workloads = lambda: {}

    def test_a_queued_job_starts_with_the_new_task(self):
        self.assertTrue(self.d.set_job(1, {"goal": "only the backend"})["ok"])
        self.assertEqual(self.d.jobs[0]["goal"], "only the backend")
        brief = self.d._brief(self.d.jobs[0], None, False)
        self.assertIn("the user's latest word wins", brief)
        self.assertIn("- only the backend", brief)

    def test_a_working_agent_is_told_in_its_terminal(self):
        self.d.jobs[0].update(status="agent working", terminal="term-3")
        with mock.patch("queenbee.mother.dispatch.time.sleep"):
            self.assertTrue(self.d.set_job(1, {"goal": "use port 8080"})["ok"])
        self.assertEqual(self.writes, [("term-3", "use port 8080"), ("term-3", "\r")])
        self.assertEqual(self.d.jobs[0]["goal"], "g")           # the plan's goal stays; the note is kept
        self.assertEqual(self.d.jobs[0]["user_notes"], ["use port 8080"])

    def test_a_finished_job_runs_again_with_its_new_task(self):
        for j in self.d.jobs:
            j.update(status="done", terminal="t1", workload="t2", direct=True, same_as=None)
        self.d.done.set()
        with mock.patch.object(Dispatch, "start") as start:
            self.assertTrue(self.d.set_job(2, {"goal": "add tests"})["ok"])
        start.assert_called_once()
        p2 = self.d.jobs[1]
        self.assertEqual((p2["status"], p2["terminal"], p2["workload"], p2["direct"]),
                         ("queued", None, None, False))
        self.assertEqual(p2["goal"], "g")                       # the task stays; the text is what to change
        brief = self.d._brief(p2, None, False)
        self.assertIn("worked on before", brief)
        self.assertIn("- add tests", brief)
        self.d.step()
        self.assertEqual(p2["status"], "agent working")

    def test_a_finished_agent_still_open_redoes_it_in_the_same_conversation(self):
        self.host._call.side_effect = lambda m, *a, **k: (
            [{"id": "term-3", "running": True}] if m == "terminals"
            else self.writes.append(a) or {"ok": True})
        p1 = self.d.jobs[0]
        p1.update(status="done", terminal="term-3", workload="term-4")
        with mock.patch("queenbee.mother.dispatch.time.sleep"):
            self.assertTrue(self.d.set_job(1, {"goal": "the button should be blue"})["ok"])
        self.assertEqual((p1["status"], p1["terminal"], p1["workload"]), ("agent working", "term-3", None))
        self.assertEqual(self.writes[0][0], "term-3")
        self.assertTrue(self.writes[0][1].startswith("the button should be blue"))
        self.assertIn(".queenbee/W1.json", self.writes[0][1])
        self.assertEqual(self.writes[1], ("term-3", "\r"))

    def test_branches_and_sweep_values_are_steered_elsewhere(self):
        self.d.jobs[0]["branched"] = True
        self.d.jobs[1]["lane"] = "up"
        self.assertFalse(self.d.set_job(1, {"goal": "x"})["ok"])
        self.assertFalse(self.d.set_job(2, {"goal": "x"})["ok"])


class MapWorkerTests(unittest.TestCase):
    """Workers added from the map: a task in the user's words, started at
    once, done when it reports; killing stops a worker and all under it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.closed = []
        self.host = mock.Mock(port=7877)

        def call(m, *a, **k):
            if m == "terminal_close":
                self.closed.append(a[0])
            return [] if m == "terminals" else {"ok": True, "id": "term-9"}
        self.host._call.side_effect = call
        self.d = Dispatch([], {"models": {"claude": {}, "codex": {}}}, self.host)
        self.d._workloads = lambda: {}

    def test_a_task_starts_at_once_beside_others_and_ends_with_its_report(self):
        with mock.patch.object(Dispatch, "start"):
            self.assertTrue(self.d.add_job("build the login page with a form", self.tmp, "claude", "haiku")["ok"])
            self.assertTrue(self.d.add_job("write the API", self.tmp, "claude", "sonnet")["ok"])
            self.assertFalse(self.d.add_job("x", "no/such/folder", "claude", "haiku")["ok"])
            self.assertFalse(self.d.add_job("x", self.tmp, "claude", "not a model!")["ok"])
        self.d.step()
        a, b = self.d.jobs
        self.assertEqual((a["status"], b["status"]), ("agent working", "agent working"))
        self.assertEqual(a["title"], "build the login page with…")
        self.d.queen_name = "Shop"
        brief = self.d._brief(b, None, False)
        self.assertIn('queen bee "Shop"', brief)
        self.assertIn("W1 (general): build the login page with…", brief)     # the worker beside it
        brief = self.d._brief(a, None, False)
        self.assertIn("build the login page with a form", brief)
        self.assertIn('"status": "done"', brief)
        with mock.patch.object(self.d, "_read_report", return_value={"status": "done", "notes": "made it"}):
            self.d.state_of = lambda t: ("working", "")
            self.d.step()
        self.assertEqual(a["status"], "done")
        self.assertIn("made it", a["events"][-1][1])

    def test_a_worker_that_stops_mid_part_is_told_by_the_queen_to_go_on(self):
        writes = []
        self.host._call.side_effect = lambda m, *a, **k: (
            writes.append(a) if m == "terminal_write" else None) or ([] if m == "terminals" else {"ok": True, "id": "term-9"})
        with mock.patch.object(Dispatch, "start"):
            self.d.add_job("build the cart", self.tmp, "claude", "haiku")
        self.d.step()
        job = self.d.jobs[0]
        self.d.state_of = lambda t: ("idle", "turn finished")
        with mock.patch("queenbee.mother.dispatch.time.sleep"), mock.patch.object(self.d, "_read_report", return_value=None):
            self.d.step()                                 # stopped without its report
            self.assertEqual(job["status"], "agent working")
            self.assertEqual(job["steered"], 1)
            self.assertIn("Your queen here", writes[0][1])
            self.assertEqual(writes[1][1], "\r")
            self.d.step()                                 # not again at once
            self.assertEqual(job["steered"], 1)
            for _ in range(2):
                job["steered_at"] -= 30
                self.d.step()
        self.assertEqual(job["status"], "done")              # after two nudges it is left done
        self.assertEqual(job["steered"], 2)
        self.assertIn("the queen told it to go on", " ".join(e[1] for e in job["events"]))

    def test_a_hung_check_is_stopped_and_its_tree_killed(self):
        import sys
        from queenbee.mother.dispatch import run_captured
        code, out = run_captured([sys.executable, "-c", "import time; print('started', flush=True); time.sleep(60)"],
                                 self.tmp, 1)
        self.assertIsNone(code)
        self.assertIn("started", out)

    def test_the_queen_checks_the_whole_and_sends_failures_to_an_integration_worker(self):
        with mock.patch.object(Dispatch, "start"):
            self.d.add_job("build the cart", self.tmp, "claude", "haiku")
        self.d.set_verify("rm -rf /", self.tmp)                 # not a test runner: refused
        self.assertEqual(self.d.verify, {})
        self.d.set_verify("python -m unittest discover -s tests", self.tmp)
        self.d.jobs[0]["status"] = "done"
        runs = []
        with mock.patch("queenbee.mother.dispatch.threading.Thread",
                        side_effect=lambda target, **k: mock.Mock(start=lambda: runs.append(target))):
            self.d.step()
        self.assertFalse(self.d.done.is_set())                    # not done before the check
        self.assertEqual(self.d.verify["state"], "running")
        with mock.patch("queenbee.mother.dispatch.run_captured", return_value=(1, "FAIL: test_checkout (api)")), \
                mock.patch.object(Dispatch, "start"):
            runs.pop()()
        fixer = self.d.jobs[-1]
        self.assertEqual((fixer["group"], fixer["tier"], fixer["status"]), ("integration", "sonnet", "queued"))
        self.assertIn("FAIL: test_checkout", fixer["goal"])
        self.assertEqual(self.d.verify["state"], "fixing")
        fixer["status"] = "done"
        with mock.patch("queenbee.mother.dispatch.threading.Thread",
                        side_effect=lambda target, **k: mock.Mock(start=lambda: runs.append(target))):
            self.d.step()
        with mock.patch("queenbee.mother.dispatch.run_captured", return_value=(0, "OK")):
            runs.pop()()
        self.assertEqual(self.d.verify["state"], "passed")
        self.d.step()
        self.assertTrue(self.d.done.is_set())

    def test_a_stop_by_the_user_cancels_the_armed_check(self):
        with mock.patch.object(Dispatch, "start"):
            self.d.add_job("build the cart", self.tmp, "claude", "haiku")
        self.d.set_verify("python -m unittest discover -s tests", self.tmp)
        self.d.kill(1)
        self.assertEqual(self.d.verify["state"], "cancelled")
        with mock.patch("queenbee.mother.dispatch.threading.Thread") as thread:
            self.d.step()
        thread.assert_not_called()                              # no check run, no integration worker
        self.assertEqual(len(self.d.jobs), 1)
        self.assertTrue(self.d.done.is_set())

    def test_a_check_already_running_when_the_user_stops_starts_no_fixer(self):
        with mock.patch.object(Dispatch, "start"):
            self.d.add_job("build the cart", self.tmp, "claude", "haiku")
        self.d.set_verify("python -m unittest discover -s tests", self.tmp)
        self.d.jobs[0]["status"] = "done"
        runs = []
        with mock.patch("queenbee.mother.dispatch.threading.Thread",
                        side_effect=lambda target, **k: mock.Mock(start=lambda: runs.append(target))):
            self.d.step()
        self.d.kill(1)
        with mock.patch("queenbee.mother.dispatch.run_captured", return_value=(1, "FAIL")),                 mock.patch.object(Dispatch, "start") as start:
            runs.pop()()
        self.assertEqual(len(self.d.jobs), 1)
        start.assert_not_called()

    def test_a_phased_plan_waits_and_the_queen_reads_progress_live(self):
        import os
        with mock.patch.object(Dispatch, "start"):
            self.d.add_job("lay the data model", self.tmp, "claude", "sonnet")
            self.d.add_job("enrolment", self.tmp, "claude", "haiku")
        self.d.jobs[1]["after"] = [1]
        self.d.step()
        first, second = self.d.jobs
        self.assertEqual((first["status"], second["status"], second["waiting_for"]),
                         ("agent working", "queued", 1))
        os.makedirs(os.path.join(self.tmp, ".queenbee"), exist_ok=True)
        with open(os.path.join(self.tmp, ".queenbee", "W1.progress"), "w", encoding="utf-8") as fh:
            fh.write("schema written; next: the app skeleton\n")
        self.d.state_of = lambda t: ("working", "")
        self.d.step()
        self.assertEqual(first["progress"], "schema written; next: the app skeleton")
        self.assertIn("progress: schema written", first["events"][-1][1])
        with open(os.path.join(self.tmp, ".queenbee", "W1.progress"), "a", encoding="utf-8") as fh:
            fh.write("skeleton done\n")
        self.d.step()
        self.assertEqual(sum("progress:" in e[1] for e in first["events"]), 2)     # only new lines
        first["status"] = "done"
        self.d.step()
        self.assertEqual(second["status"], "agent working")
        self.assertIn(".queenbee/W1.progress", self.d._brief(first, None, False))   # the worker is told
        first.update(map=["shop/cart.py", "tests/test_cart.py"], files=["shop/cart.py"])
        brief = self.d._brief(first, None, False)
        self.assertIn("Your files: shop/cart.py", brief)                # the queen's map, not a search
        self.assertIn("  tests/test_cart.py", brief)

    def test_the_senior_lays_the_ground_and_the_juniors_build_to_its_tests(self):
        with mock.patch.object(Dispatch, "start"):
            self.d.add_job("the ground", self.tmp, "claude", "sonnet")
            self.d.add_job("enrolment", self.tmp, "claude", "haiku")
        senior, junior = self.d.jobs
        senior["senior"], junior["after"] = True, [1]
        self.assertIn("You are the senior of this team", self.d._brief(senior, None, False))
        brief = self.d._brief(junior, None, False)
        self.assertIn("The senior (W1) has laid the ground", brief)
        self.assertIn("Do not change or weaken the senior's tests", brief)

    def test_kill_stops_a_worker_and_its_hive(self):
        with mock.patch.object(Dispatch, "start"):
            self.d.add_job("site", self.tmp, "claude", "haiku")
            self.d.add_job("header", self.tmp, "claude", "haiku", parent=1)
        self.d.step()
        r = self.d.kill(1)
        self.assertEqual(r["stopped"], [1, 2])
        self.assertEqual([j["status"] for j in self.d.jobs], ["stopped", "stopped"])
        self.assertEqual(self.closed, ["term-9", "term-9"])
        self.assertFalse(self.d.kill(7)["ok"])


class SweepTreeTests(unittest.TestCase):
    def test_sweep_values_hang_under_the_adapter(self):
        host = mock.Mock(port=7877)
        host._call.side_effect = lambda m, *a, **k: [] if m == "terminals" else {"ok": True}
        proto = job(1, command_template="py x.py --lam {value} --run-name r{tag}",
                    metrics_template="r{tag}.jsonl", metric_key="iou")
        d = Dispatch([proto], {"models": {"claude": {}, "codex": {}}}, host)
        d._workloads = lambda: {}
        d.start_sweep(sweep.normalise({"param": "--lam", "min": 0.1, "max": 1, "epochs": 4,
                                       "lanes": [{"start": 0.1, "step": 0.1}, {"start": 1, "step": -0.1}]}))
        d.jobs[0].update(status="GPU job submitted", terminal="t1", workload="t2",
                         submitted_at=time.time() - 600)
        d._advance_sweep()
        self.assertEqual(d.jobs[1]["parent"], 1)


if __name__ == "__main__":
    unittest.main()


class RecursionLimits(unittest.TestCase):
    """A queen that opens queens multiplies planning cost: shallow, new, budgeted."""

    def setUp(self):
        self.host = mock.Mock(port=7877)
        self.host._call.side_effect = lambda m, *a, **k: [] if m == "terminals" else {"ok": True, "id": "term-9"}
        self.cfg = {"models": {"claude": {}, "codex": {}}, "worker_model": "haiku",
                    "max_depth": 1, "max_children": 2, "branch_budget_usd": 0.10}
        self.d = Dispatch([job(1), job(2)], self.cfg, self.host)
        self.d._workloads = lambda: {}
        self.d.instruction = "Build the shop"

    def test_a_branch_agent_may_not_open_a_branch_of_its_own(self):
        self.d.jobs.append(job(3, parent=1))
        self.d.jobs[0].update(role="mother", branched=True, instruction="Do A")
        r = self.d.set_job(3, {"role": "mother", "instruction": "Do A1"})
        self.assertFalse(r["ok"])
        self.assertIn("depth limit", r["error"])

    def test_the_top_level_can_branch_once_with_a_new_instruction(self):
        self.assertTrue(self.d.set_job(1, {"role": "mother", "instruction": "Do A"})["ok"])

    def test_repeating_the_queens_own_instruction_is_refused(self):
        r = self.d.set_job(1, {"role": "mother", "instruction": "  build   the SHOP "})
        self.assertFalse(r["ok"])
        self.assertIn("repeats", r["error"])

    def test_the_same_branch_instruction_is_not_split_twice(self):
        self.d.jobs[0].update(role="mother", branched=True, instruction="Do A")
        r = self.d.set_job(2, {"role": "mother", "instruction": "do a"})
        self.assertFalse(r["ok"])
        self.assertIn("already branched", r["error"])

    def test_branch_budget(self):
        self.d.costs["branches"] = {"tokens": 10, "cost_usd": 0.12}
        r = self.d.set_job(1, {"role": "mother", "instruction": "Do A"})
        self.assertFalse(r["ok"])
        self.assertIn("budget", r["error"])

    def test_a_branch_plan_is_cut_to_max_children(self):
        self.d.jobs[0].update(role="mother", instruction="Do A", status="branch running")
        plan = {"jobs": [job(i, title=f"k{i}", project_path="") for i in (1, 2, 3, 4)], "costs": {}}
        self.d._graft(self.d.jobs[0], plan)
        self.assertEqual(len([j for j in self.d.jobs if j.get("parent") == 1]), 2)


class QualityPass(unittest.TestCase):
    def setUp(self):
        self.host = mock.Mock(port=7877)
        self.host._call.side_effect = lambda m, *a, **k: [] if m == "terminals" else {"ok": True, "id": "term-9"}
        self.d = Dispatch([job(1, kind="task"), job(2, kind="task")], {"models": {"claude": {}, "codex": {}}}, self.host)
        self.d._workloads = lambda: {}
        self.d.instruction = "Build Campus as API.md describes"
        self.d.qa_goal = "on"

    def test_one_quality_worker_follows_once_every_part_is_in(self):
        self.d.jobs[0]["status"] = self.d.jobs[1]["status"] = "done"
        self.d.step()
        qa = self.d.jobs[-1]
        self.assertEqual((len(self.d.jobs), qa["title"], qa["tier"], qa["status"]), (3, "quality pass", "sonnet", "queued"))
        self.assertIn("API.md", qa["goal"])
        self.assertFalse(self.d.done.is_set())
        qa["status"] = "done"
        self.d.step()
        self.assertEqual(len(self.d.jobs), 3)                       # not again
        self.assertTrue(self.d.done.is_set())

    def test_no_quality_pass_while_parts_are_running_or_when_off(self):
        self.d.jobs[0]["status"] = "done"
        self.d.jobs[1]["status"] = "agent working"
        self.assertFalse(self.d._qa_pending())
        self.d.jobs[1]["status"] = "done"
        self.d.qa_goal = ""
        self.assertFalse(self.d._qa_pending())

    def test_no_quality_pass_when_a_part_failed_or_was_stopped(self):
        self.d.jobs[0]["status"] = "done"
        self.d.jobs[1]["status"] = "stopped"
        self.assertFalse(self.d._qa_pending())
        self.d.jobs[1]["status"] = "failed"
        self.assertFalse(self.d._qa_pending())


class LoneAgentBrief(unittest.TestCase):
    """"One agent alone" (a free worker, the solo lane of a benchmark) gets the user's words and the one report file QueenBee
    needs, nothing about a queen, a team, or reporting "blocked" the way a worker bee is told to."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.host = mock.Mock(port=7877)
        self.host._call.side_effect = lambda m, *a, **k: [] if m == "terminals" else {"ok": True, "id": "term-9"}
        self.d = Dispatch([], {"models": {"claude": {}, "codex": {}}}, self.host)
        self.d.queen_name = "Free worker"

    def test_the_plain_brief_is_the_users_words_and_the_report_file(self):
        with mock.patch("queenbee.mother.dispatch.binary_for", return_value="x.exe"):
            self.assertTrue(self.d.add_job("Implement the release. Done means `python check.py` prints 40/40.", self.tmp,
                                           "codex", "gpt-x", None, [], "One agent", plain=True, bare=True)["ok"])
            self.assertTrue(self.d.add_job("Build the parser", self.tmp, "codex", "gpt-x", None, [], "W2")["ok"])
        plain = self.d._brief_body(self.d.jobs[0], None, False)
        team = self.d._brief_body(self.d.jobs[1], None, False)
        self.assertTrue(plain.startswith("Implement the release."))
        self.assertIn(".queenbee/W1.json", plain)
        for phrase in ("worker bee", "your queen", "queen bee", "Do not ask questions", "your part"):
            self.assertNotIn(phrase, plain)
        self.assertIn("worker bee", team)
        self.assertIn("Do not ask questions", team)


class ReadScope(unittest.TestCase):
    """read_scope "own": a worker with files of its own is told to read those and the brief, not the other parts' modules."""

    def test_the_rule_appears_only_with_the_role_and_with_owned_files(self):
        host = mock.Mock(port=7877)
        host._call.side_effect = lambda m, *a, **k: [] if m == "terminals" else {"ok": True, "id": "term-9"}
        base = {"models": {"claude": {}, "codex": {}}}
        job = {"priority": 1, "title": "parser", "goal": "g", "project_path": ".", "agent": "claude", "effort": "low",
               "same_as": None, "work_order": "", "kind": "task", "files": ["minidb/parser.py"]}
        d = Dispatch([dict(job)], dict(base, read_scope="own"), host)
        d.queen_name = "Q"
        self.assertIn("Read only your own files", d._brief_body(d.jobs[0], None, False))
        d2 = Dispatch([dict(job)], dict(base), host)
        d2.queen_name = "Q"
        self.assertNotIn("Read only your own files", d2._brief_body(d2.jobs[0], None, False))
        d3 = Dispatch([dict(job, files=[])], dict(base, read_scope="own"), host)
        d3.queen_name = "Q"
        self.assertNotIn("Read only your own files", d3._brief_body(d3.jobs[0], None, False))


class CleanQueenConfig(unittest.TestCase):
    """A role of one queen (the 300 s look-in of a benchmark arm) must not reach the queen added after her."""

    def test_adding_a_queen_rebuilds_her_cfg_from_her_own_roles(self):
        from queenbee.mother.server import Hive, Mother
        with mock.patch("queenbee.mother.server.threading.Thread"), mock.patch.object(Hive, "_save"):
            import threading
            hive = Hive.__new__(Hive)
            hive.host, hive.ui_port, hive.lock = mock.MagicMock(), 7880, threading.RLock()
            hive.queens = [Mother(hive.host, 7880, "q1", "Queen 1", primary=True)]
            hive.add("A", "claude", "sonnet", None, {"director_interval_s": "300"}, "", True)
            a = hive.queens[-1]
            self.assertEqual(a.cfg.get("director_interval_s"), "300")
            hive.remove(a.queen, True)
            hive.add("B", "claude", "sonnet", None, None, "", True)
            b = hive.queens[-1]
            self.assertIsNone(b.cfg.get("director_interval_s"))

