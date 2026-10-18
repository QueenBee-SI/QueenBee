"""Several queens in one window: each keeps its own plan and saved state,
and only the first queen's P1 is the machine's primary GPU job."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from queenbee.mother.dispatch import Dispatch


def job(n, **kw):
    base = {"priority": n, "title": f"j{n}", "goal": "g", "project_path": ".", "agent": "claude",
            "effort": "low", "same_as": None, "work_order": "1.",
            "command": "py -m queenbee.gpu submit --role primary --name x -- py t.py"}
    base.update(kw)
    return base


class QueenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch("queenbee.config._state_dir", lambda: Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.host = mock.Mock(port=7877)
        self.host._call.side_effect = lambda m, *a, **k: [] if m == "terminals" else {"ok": True, "id": "term-9"}
        self.host.ping.return_value = None

    def test_each_queen_saves_its_own_plan(self):
        for queen in ("q1", "q2"):
            d = Dispatch([job(1, title=queen)], {"models": {"claude": {}, "codex": {}}}, self.host)
            d.queen = queen
            d.save()
        self.assertTrue((Path(self.tmp.name) / "mother_plan.json").exists())
        self.assertTrue((Path(self.tmp.name) / "mother_plan_q2.json").exists())
        back = Dispatch.restore({"models": {}}, self.host, queen="q2")
        self.assertEqual((back.queen, back.jobs[0]["title"]), ("q2", "q2"))

    def _reopen_with(self, running):
        from queenbee.mother.server import Hive

        d = Dispatch([job(1), job(2)], {"models": {"claude": {}, "codex": {}}}, self.host)
        d.jobs[0].update(status="agent working", terminal="term-1")
        d.save()
        self.host._call.side_effect = lambda m, *a, **k: (
            [{"id": t, "running": True} for t in running] if m == "terminals" else {"ok": True, "id": "term-9"})
        with mock.patch("queenbee.mother.server.threading.Thread"):
            return Hive(self.host).queens[0]

    def test_a_team_reopened_with_no_worker_running_waits_for_the_user(self):
        q = self._reopen_with([])
        self.assertTrue(q.dispatch.held)
        q.dispatch.step()
        self.assertEqual([j["status"] for j in q.dispatch.jobs], ["agent working", "queued"])  # nothing started
        self.assertTrue(q.resume(False)["ok"])
        self.assertEqual([j["status"] for j in q.dispatch.jobs], ["stopped", "stopped"])
        self.assertTrue(q.dispatch.done.is_set())
        self.assertFalse(q.dispatch.held)

    def test_continuing_a_reopened_team_lets_it_start_again(self):
        q = self._reopen_with([])
        self.assertTrue(q.resume(True)["ok"])
        self.assertFalse(q.dispatch.held or q.dispatch.done.is_set())
        w1 = q.dispatch.jobs[0]                                 # its terminal is gone: a new agent picks it up
        self.assertEqual((w1["status"], w1["terminal"]), ("queued", None))
        self.assertIn("worked on this job before", q.dispatch._user_notes(w1))
        self.assertFalse(q.resume(True)["ok"])                  # asked once

    def test_a_team_whose_workers_still_run_is_followed_at_once(self):
        q = self._reopen_with(["term-1"])
        self.assertFalse(q.dispatch.held)

    def test_a_queen_added_with_roles_is_held_to_the_model_ceiling(self):
        from queenbee.mother.server import Hive

        with mock.patch("queenbee.mother.server.threading.Thread"):
            hive = Hive(self.host)
            with mock.patch("queenbee.mother.server._providers",
                            return_value=[{"id": "claude", "installed": True, "models": ["haiku", "sonnet", "opus"]}]):
                out = hive.add("Site", "claude", "sonnet", roles={"max_model": "sonnet", "architect_model": "opus",
                                                   "planner_model": "sonnet"})
        self.assertTrue(out["ok"])
        cfg = hive.queens[0].cfg
        self.assertEqual((cfg["architect_model"], cfg["planner_model"]), ("sonnet", "sonnet"))

    def test_only_the_first_queen_runs_a_primary_job(self):
        d = Dispatch([job(1)], {"models": {"claude": {}, "codex": {}}}, self.host)
        d._workloads = lambda: {}
        d.primary = False
        d.step()
        self.assertIn("--role secondary", d.jobs[0]["command"])
        self.assertNotIn("--role primary", d.jobs[0]["command"])

    def test_the_hive_adds_renames_removes_and_remembers_queens(self):
        from queenbee.mother.server import Hive

        with mock.patch("queenbee.mother.server.threading.Thread"):
            hive = Hive(self.host)
            self.assertEqual([q.queen for q in hive.queens], ["q1"])
            self.assertTrue(hive.queens[0].primary)
            with mock.patch("queenbee.mother.server._providers",
                            return_value=[{"id": "claude", "installed": True, "models": ["haiku", "opus"]}]):
                # The empty first queen is taken, not a second one added beside it.
                self.assertEqual(hive.add("Site", "claude", "opus"), {"ok": True, "queen": "q1"})
                self.assertEqual((hive.queens[0].name, hive.queens[0].agent, hive.queens[0].model),
                                 ("Site", "claude", "opus"))
                self.assertFalse(hive.add("x", "claude", "gpt")["ok"])
                self.assertFalse(hive.add("x", "codex")["ok"])
            hive.queens[0].plan = {"instruction": "build the site"}
            hive.rename("q1", "Queen 1")
            added = hive.add("Web app")
            self.assertEqual(added, {"ok": True, "queen": "q2"})
            self.assertFalse(hive.get("q2").primary)
            self.assertIs(hive.get("nope"), hive.queens[0])
            self.assertTrue(hive.rename("q2", "Game")["ok"])
            saved = json.loads((Path(self.tmp.name) / "queens.json").read_text(encoding="utf-8"))
            self.assertEqual(saved, [{"id": "q1", "name": "Queen 1", "agent": "claude", "model": "opus"},
                                     {"id": "q2", "name": "Game", "agent": "", "model": ""}])
            again = Hive(self.host)
            self.assertEqual([(q.queen, q.name) for q in again.queens], [("q1", "Queen 1"), ("q2", "Game")])
            self.assertTrue(again.remove("q2")["ok"])
            # A new queen taking a removed one's id starts empty.
            d = Dispatch([job(1, title="old work")], {"models": {}}, self.host)
            d.queen = "q2"
            d.save()
            again.queens[0].plan = {"instruction": "busy"}           # so a second queen is added
            self.assertEqual(again.add("Fresh")["queen"], "q2")
            self.assertIsNone(again.get("q2").dispatch)
            # Starting clean: the first queen is emptied, its plan kept aside.
            again.queens[0].plan = None
            q1 = Dispatch([job(1, title="REEM sweep")], {"models": {}}, self.host)
            q1.done.set()
            again.queens[0].dispatch = q1
            q1.save()
            self.assertTrue(again.reset("q1")["ok"])
            self.assertIsNone(again.get("q1").dispatch)
            self.assertFalse(Dispatch.path_for("q1").exists())
            self.assertTrue(list(Path(self.tmp.name).glob("mother_plan.removed-*.json")))

    def test_a_command_starts_workers_on_its_own_and_the_next_joins_them(self):
        from queenbee.mother.server import Mother

        with mock.patch("queenbee.mother.server.threading.Thread"):
            q = Mother(self.host, queen="q2", name="Shop", primary=False)
        planned = lambda title: {"jobs": [job(1, title=title, kind="task", group="frontend", work_order="do it")],
                                 "compare": None, "sweep": None, "notes": "", "questions": ["which port?"],
                                 "costs": {"planner": {"tokens": 10, "cost_usd": 0.01},
                                           "architect": {"tokens": 0, "cost_usd": 0}}}
        runs = []
        with mock.patch.object(Mother, "_plan_and_prepare", side_effect=lambda i, f, *rest: planned(i)), \
             mock.patch("queenbee.mother.server.threading.Thread",
                        side_effect=lambda target, **k: mock.Mock(start=lambda: runs.append(target))), \
             mock.patch("queenbee.app.server_up", return_value=True), \
             mock.patch("queenbee.mother.dispatch.Dispatch.start") as start:
            self.assertFalse(q.command("", self.tmp.name)["ok"])
            self.assertTrue(q.command("build the shop", self.tmp.name)["ok"])
            runs.pop()()
            self.assertEqual([j["title"] for j in q.dispatch.jobs], ["build the shop"])
            start.assert_called()
            self.assertIn("decided on its own: which port?", [c.get("queen") for c in q.commands])
            self.assertTrue(q.command("add a cart", self.tmp.name)["ok"])
            runs.pop()()
        self.assertEqual([(j["priority"], j["title"]) for j in q.dispatch.jobs],
                         [(1, "build the shop"), (2, "add a cart")])
        self.assertIn("+ add a cart", q.dispatch.instruction)
        self.assertIsNone(q.commanding)

    def test_each_terminal_says_whether_it_works_or_waits(self):
        from queenbee.mother.server import Mother

        with mock.patch("queenbee.mother.server.threading.Thread"):
            q = Mother(self.host, queen="q4", name="Web", primary=False)
        states = {"term-1": ("working", "Edit"), "term-2": ("blocked", "approve a command"),
                  "term-3": ("idle", "exited")}
        self.assertEqual(q._term_state({"terminal": "term-1"}, states), ("working", "Edit"))
        self.assertEqual(q._term_state({"terminal": "term-2"}, states)[0], "waiting")
        self.assertEqual(q._term_state({"terminal": "term-3"}, states)[0], "closed")
        self.assertEqual(q._term_state({"direct": True, "terminal": None, "workload": "term-1"}, states)[0], "working")
        self.assertEqual(q._term_state({"terminal": None}, states), ("", ""))
        q._states = states
        self.assertEqual(q._activity([{"terminal": "term-2", "status": "agent working"}]), "waiting")
        self.assertEqual(q._activity([{"terminal": "term-2", "status": "agent working"},
                                      {"terminal": "term-1", "status": "agent working"}]), "working")
        self.assertEqual(q._activity([{"terminal": "term-1", "status": "done"}]), "finished")
        self.assertEqual(q._activity([]), "new")

    def test_a_queen_holds_workers_at_its_limits_and_starts_them_when_there_is_room(self):
        from queenbee.mother.server import Mother, clean_limits

        self.assertEqual(clean_limits({"workers": "2", "budget_usd": 0, "ram_mb": -5, "cpu_pct": "x", "vram_mb": 4096}),
                         {"workers": 2, "vram_mb": 4096.0})
        with mock.patch("queenbee.mother.server.threading.Thread"):
            q = Mother(self.host, queen="q5", name="Shop", primary=False, limits={"workers": 2})
        q._agent_states = lambda: {}
        d = Dispatch([job(1, kind="task", work_order="a"), job(2, kind="task", work_order="b"),
                      job(3, kind="task", work_order="c")], {"models": {"claude": {}, "codex": {}}}, self.host)
        d._workloads = lambda: {}
        q._wire(d)
        d.step()
        self.assertEqual([j["status"] for j in d.jobs], ["agent working", "agent working", "queued"])
        self.assertEqual(d.jobs[2]["held"], "2/2 workers at work")
        self.assertIn("held by the queen's limit: 2/2 workers at work", d.jobs[2]["events"][-1][1])
        d.step()                                      # said once, not every pass
        self.assertEqual(sum("held by" in e[1] for e in d.jobs[2]["events"]), 1)
        d.jobs[0]["status"] = "done"
        d.step()
        self.assertEqual(d.jobs[2]["status"], "agent working")
        self.assertNotIn("held", d.jobs[2])
        self.assertIn("room again", d.jobs[2]["events"][-2][1])

    def test_a_spent_budget_holds_new_workers_and_new_commands(self):
        from queenbee.mother.server import Mother

        with mock.patch("queenbee.mother.server.threading.Thread"):
            q = Mother(self.host, queen="q6", name="Shop", primary=False, limits={"budget_usd": 1})
        q._agent_states = lambda: {}
        q._cost = {"tokens": 10, "cost_usd": 1.25}
        self.assertEqual(q._gate(job(1), []), "budget $1 reached ($1.25 spent)")
        r = q.command("add a cart", self.tmp.name)
        self.assertFalse(r["ok"])
        self.assertIn("spent its budget", r["error"])
        q.limits = {"vram_mb": 1024}
        q._usage = {"term-7": (5.0, 300, 2048)}
        gpu_job = job(2, kind="gpu", terminal="term-7", status="GPU job submitted")
        self.assertIn("VRAM", q._gate(job(3, kind="gpu"), [gpu_job]))
        self.assertIsNone(q._gate(job(3, kind="task", command=""), [gpu_job]))   # VRAM holds GPU work only

    def test_limits_are_kept_with_the_queen(self):
        from queenbee.mother.server import Hive

        with mock.patch("queenbee.mother.server.threading.Thread"):
            hive = Hive(self.host)
            self.assertTrue(hive.limit("q1", {"workers": 3, "budget_usd": 2.5})["ok"])
            self.assertFalse(hive.limit("q9", {"workers": 1})["ok"])
            again = Hive(self.host)
        self.assertEqual(again.queens[0].limits, {"workers": 3, "budget_usd": 2.5})

    def test_an_approval_that_did_not_land_is_given_again(self):
        from queenbee.mother.server import Mother

        with mock.patch("queenbee.mother.server.threading.Thread"):
            q = Mother(self.host, queen="q7", name="Shop", primary=False)
        q.dispatch = Dispatch([job(2, kind="task", work_order="cart", status="agent working", terminal="term-2",
                                   project_path=self.tmp.name)], {"models": {}}, self.host)
        q.dispatch.jobs[0].update(status="agent working", terminal="term-2")
        trust = {"text": "Quick safety check: Is this a project you created or one you trust?",
                 "options": [{"n": "1", "text": "No, exit"}, {"n": "2", "text": "Yes, I trust this folder"}],
                 "cursor": 1, "context": ["Accessing workspace:", self.tmp.name]}
        q._question = lambda term: trust
        q._agent_states = lambda: {"term-2": ("working", "terminal process active")}
        q._screen_tail = []
        writes = []
        self.host._call.side_effect = lambda m, *a, **k: writes.append(a) or {"ok": True}
        q._approve_once()
        self.assertEqual(len(writes), 1)
        q._approve_once()                            # at once: not again
        self.assertEqual(len(writes), 1)
        key = next(iter(q._answered))
        q._answered[key]["ts"] -= 9                 # still there 9 s later
        q._approve_once()
        self.assertEqual(len(writes), 2)
        self.assertEqual(q._answered[key]["retries"], 1)

    def test_a_question_left_to_the_user_is_the_queens_once_she_answers_again(self):
        # Seen live: "I answer" on, a worker asked, the user switched back to the queen; the question stayed on screen
        # for nine minutes because the queen had already marked it as the user's.
        from queenbee.mother.server import Mother

        with mock.patch("queenbee.mother.server.threading.Thread"):
            q = Mother(self.host, queen="q9", name="Shop", primary=False)
        q.dispatch = Dispatch([job(3, kind="task", work_order="export", project_path=self.tmp.name)],
                              {"models": {}}, self.host)
        q.dispatch.jobs[0].update(status="agent working", terminal="term-6")
        run = {"text": "Do you want to proceed?", "cursor": 1,
               "options": [{"n": "1", "text": "Yes"}, {"n": "2", "text": "No"}],
               "context": [f'cd "{self.tmp.name}"; python -m unittest tests.test_export', "Run the tests"]}
        q._question = lambda term: run
        q._agent_states = lambda: {"term-6": ("waiting", "")}
        q._screen_tail = []
        writes = []
        self.host._call.side_effect = lambda m, *a, **k: writes.append(a) or {"ok": True}
        q.set_answers("me")
        q._approve_once()
        self.assertEqual((writes, next(iter(q._answered.values()))["decision"]), ([], "left to you"))
        q._approve_once()                            # still the user's while they answer
        self.assertEqual(writes, [])
        q.set_answers("queen")
        q._approve_once()
        self.assertEqual(len(writes), 1)
        self.assertEqual(next(iter(q._answered.values()))["decision"], "approved")

    def test_the_queen_says_no_herself_to_what_a_worker_may_not_do(self):
        from queenbee.mother.server import Mother

        with mock.patch("queenbee.mother.server.threading.Thread"):
            q = Mother(self.host, queen="q8", name="Shop", primary=False)
        q.dispatch = Dispatch([job(1, kind="task", work_order="migrate", project_path=self.tmp.name)],
                              {"models": {}}, self.host)
        q.dispatch.jobs[0].update(status="agent working", terminal="term-4")
        install = {"text": "Do you want to proceed?", "cursor": 1,
                   "options": [{"n": "1", "text": "Yes"}, {"n": "2", "text": "No"}],
                   "context": [f'cd "{self.tmp.name}"; python -m pip install -e .', "Install package"]}
        q._question = lambda term: install
        q._agent_states = lambda: {"term-4": ("waiting", "")}
        q._screen_tail = []
        writes = []
        self.host._call.side_effect = lambda m, *a, **k: writes.append(a) or {"ok": True}
        with mock.patch("queenbee.mother.server.time.sleep"):
            q._approve_once()                        # first sight: it may still be the last answer's
            q._pending = {k: v - 10 for k, v in q._pending.items()}
            q._approve_once()
        entry = next(iter(q._answered.values()))
        self.assertEqual(entry["decision"], "declined by the queen")
        self.assertIn("Your queen here: no", writes[1][1])
        self.assertEqual(writes[-1][1], "\r")
        self.assertIn("the queen said no", q.dispatch.jobs[0]["events"][-1][1])

    def test_a_queen_summary_carries_its_tree(self):
        from queenbee.mother.server import Mother

        with mock.patch("queenbee.mother.server.threading.Thread"):
            q = Mother(self.host, queen="q3", name="Game", primary=False)
        q.dispatch = Dispatch([job(1), job(2, parent=1)], {"models": {}}, self.host)
        q.dispatch.instruction = "make a game"
        s = q.summary()
        self.assertEqual((s["id"], s["name"], s["instruction"]), ("q3", "Game", "make a game"))
        self.assertEqual([(j["priority"], j["parent"]) for j in s["jobs"]], [(1, None), (2, 1)])


if __name__ == "__main__":
    unittest.main()

