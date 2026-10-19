"""The queen's completion report: written once from facts, when her team has stopped, and kept in the project."""
import os
import tempfile
import time
import unittest
from unittest import mock

from queenbee.mother import config
from queenbee.mother.dispatch import Dispatch
from queenbee.mother.server import Mother


class Completion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        host = mock.MagicMock()
        with mock.patch("queenbee.mother.server.threading.Thread"):
            self.q = Mother(host, queen="q9", name="T", primary=False)
        d = Dispatch([], config.load(), host, 7878)
        now = time.time()
        d.started_at = now - 600
        d.instruction = "build the thing"
        d.jobs.append({"priority": 1, "kind": "task", "title": "parser", "status": "done", "project_path": self.tmp.name,
                       "agent": "claude", "tier": "sonnet", "launched_at": now - 590,
                       "events": [[now - 100, "done"]], "report": {"status": "done", "notes": "wrote it", "changed_files": ["p.py"]}})
        d.jobs.append({"priority": 2, "kind": "task", "title": "storage", "status": "failed", "error": "needs you: x",
                       "project_path": self.tmp.name, "agent": "codex", "tier": "gpt", "launched_at": now - 590,
                       "events": [[now - 50, "failed"]]})
        d.approvals = [{"decision": "approved"}, {"decision": "approved"}, {"decision": "left to you"}]
        d.verify = {"command": "python check.py", "state": "failed", "log": "2 checks failed"}
        self.q.dispatch = d

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_facts_hold_what_each_worker_reported_and_what_the_check_showed(self):
        facts = self.q.completion_facts()
        self.assertIn("build the thing", facts)
        self.assertIn("W1 parser (claude sonnet): done; reported: wrote it; files: p.py", facts)
        self.assertIn("W2 storage (codex gpt): failed; error: needs you: x", facts)
        self.assertIn("2 answered by the queen, 1 left to the user", facts)
        self.assertIn("Delivery check (run by the queen) `python check.py`: failed; output: 2 checks failed", facts)

    def test_the_result_card_is_a_few_lines_of_facts(self):
        now = time.time()
        self.q._cost = {"tokens": 4_339_462}
        self.q.dispatch.verify = {"command": "python check.py", "state": "passed", "rounds": 1, "log": ""}
        self.q.dispatch.jobs.append({"priority": 3, "kind": "task", "title": "integration", "status": "done",
                                     "project_path": self.tmp.name, "agent": "claude", "tier": "sonnet",
                                     "launched_at": now - 40, "events": [[now - 10, "done"]]})
        card = self.q.result_card()
        self.assertIn("**QueenBee run: build the thing**", card)
        self.assertIn("- Check: `python check.py` passed", card)
        self.assertIn("- Prompts to a person: 1", card)
        self.assertIn("- Workers: Claude Code (sonnet), Codex (gpt)", card)       # the fix worker is counted as a fix
        self.assertIn("- Integration fixes: 1", card)
        self.assertIn("- Time: ", card)
        self.assertIn("- Tokens: 4.34 M", card)
        self.assertIn("github.com/QueenBee-SI/QueenBee", card)

    def test_the_result_card_is_empty_before_any_worker(self):
        self.q.dispatch.jobs.clear()
        self.assertEqual(self.q.result_card(), "")

    def test_no_delivery_check_is_said_plainly(self):
        self.q.dispatch.verify = {}
        self.assertIn("Delivery check: none was set", self.q.completion_facts())

    def test_the_report_is_written_once_when_the_team_has_stopped_and_saved_in_the_project(self):
        with mock.patch("queenbee.mother.chat.complete", return_value={"ok": True, "text": "All parts in.", "tokens": 9}) as complete, \
                mock.patch.object(Mother, "_finished_at", return_value=time.time()), \
                mock.patch("queenbee.mother.server.threading.Thread", side_effect=lambda target, **k: mock.Mock(start=target)):
            first = self.q._completion_view()
            second = self.q._completion_view()
        self.assertEqual(first["state"], "done")
        self.assertEqual(first["text"], "All parts in.")
        complete.assert_called_once()
        self.assertIs(first, second)
        with open(os.path.join(self.tmp.name, ".queenbee", "completion.md"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "All parts in.")

    def test_nothing_is_written_while_a_worker_is_still_at_work(self):
        with mock.patch.object(Mother, "_finished_at", return_value=0.0), \
                mock.patch("queenbee.mother.chat.complete") as complete:
            self.assertIsNone(self.q._completion_view())
        complete.assert_not_called()

    def test_a_saved_report_of_this_plan_is_read_back_not_written_again(self):
        path = os.path.join(self.tmp.name, ".queenbee", "completion.md")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("Saved earlier.")
        with mock.patch("queenbee.mother.chat.complete") as complete, mock.patch.object(Mother, "_finished_at", return_value=time.time()):
            out = self.q._completion_view()
        self.assertEqual(out["text"], "Saved earlier.")
        complete.assert_not_called()

    def test_a_report_belongs_to_its_plan(self):
        self.q.completion = {"state": "done", "text": "old plan's report", "plan": 1.0}
        with mock.patch.object(Mother, "_finished_at", return_value=0.0):
            self.assertIsNone(self.q._completion_view())            # this plan has not finished: the old report is gone
        self.assertIsNone(self.q.completion)

    def test_a_failed_write_is_reported_not_hidden(self):
        with mock.patch("queenbee.mother.chat.complete", return_value={"ok": False, "error": "no claude"}), \
                mock.patch.object(Mother, "_finished_at", return_value=time.time()), \
                mock.patch("queenbee.mother.server.threading.Thread", side_effect=lambda target, **k: mock.Mock(start=target)):
            out = self.q._completion_view()
        self.assertEqual((out["state"], out["error"]), ("failed", "no claude"))


if __name__ == "__main__":
    unittest.main()


    def test_a_lone_agents_task_is_its_own_goal(self):
        # A free worker: no instruction, no queen's description. The report had said "the task field was empty".
        d = self.q.dispatch
        d.instruction, self.q.description = "", ""
        d.jobs[0]["goal"] = "Implement the complete Friday release; done means 40/40."
        self.assertIn("Task: Implement the complete Friday release", self.q.completion_facts())


class TheBriefSwitch(unittest.TestCase):
    def test_the_planner_writes_no_brief_when_the_role_says_so(self):
        host = mock.MagicMock()
        with mock.patch("queenbee.mother.server.threading.Thread"):
            q = Mother(host, queen="q9", name="T", primary=False)
        jobs = [{"kind": "task"}, {"kind": "task"}]
        with tempfile.TemporaryDirectory() as folder:
            q.roles["write_context"] = "no"            # roles are strings; a False would be dropped as empty
            q._write_context(folder, "the brief", jobs)
            self.assertFalse(os.path.exists(os.path.join(folder, ".queenbee", "CONTEXT.md")))
            q.roles.pop("write_context")
            q._write_context(folder, "the brief", jobs)
            self.assertTrue(os.path.exists(os.path.join(folder, ".queenbee", "CONTEXT.md")))

