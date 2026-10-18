import os
import tempfile
import unittest
from unittest import mock

from queenbee.mother import beekeeper


class BeekeeperTests(unittest.TestCase):
    def test_reads_rule_files_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "QUEEN.md"), "w", encoding="utf-8") as fh:
                fh.write("Never install packages.")
            self.assertIn("Never install packages.", beekeeper.read_rules(tmp))
            answer = ('{"score": 140, "summary": "ok", "findings": [{"rule": "QUEEN.md: no installs", "status": "followed",'
                      ' "evidence": "W1 asked, queen refused"}, {"rule": "x", "status": "bogus", "evidence": "y"}], "advice": ["a"]}')
            with mock.patch.object(beekeeper, "_run_codex", return_value=answer):
                r = beekeeper.judge(tmp, "state", "", "", agent="codex")
            self.assertEqual(r["score"], 100)
            self.assertEqual([f["status"] for f in r["findings"]], ["followed", "unclear"])
            self.assertIn("✔", beekeeper.to_markdown(r))

    def test_no_verdict_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(beekeeper, "_run_codex", return_value="nothing"):
            with self.assertRaises(RuntimeError):
                beekeeper.judge(tmp, "s", "", "", agent="codex")


if __name__ == "__main__":
    unittest.main()


class PickPortTests(unittest.TestCase):
    def test_skips_a_port_held_by_another_program(self):
        import socket

        from queenbee.agents.host_client import pick_port

        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            held = busy.getsockname()[1]
            self.assertNotEqual(pick_port(held), held)


class TerminalBeekeeperTests(unittest.TestCase):
    def test_another_agent_writes_its_verdict_to_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            made = {}

            def open_terminal(agent, model, folder, brief):
                made["brief"] = brief
                out = os.path.join(folder, ".queenbee", "beekeeper.verdict.json")      # what the agent does in its terminal
                with open(out, "w", encoding="utf-8") as fh:
                    fh.write('{"score": 71, "summary": "mostly", "findings": [], "advice": []}')
                return "term-9"

            closed = []
            bk = beekeeper.Beekeeper(lambda: None, lambda: tmp, lambda: "", open_terminal=open_terminal)
            bk.agent, bk.close_terminal = "kimi", closed.append
            with mock.patch.object(beekeeper.time, "sleep"):
                r = bk._judge_in_terminal(tmp, "state", "", "")
            self.assertEqual((r["score"], r["agent"]), (71, "kimi"))
            self.assertIn("beekeeper.verdict.json", made["brief"])
            self.assertEqual(closed, ["term-9"])


class CodexPlannerTests(unittest.TestCase):
    def test_a_codex_queen_plans_without_claude(self):
        from queenbee.mother import planner

        answer = {"result": '{"jobs":[{"priority":1,"title":"Store","goal":"write store","kind":"task","agent":"codex","effort":"medium"}]}',
                  "usage": {}, "total_cost_usd": 0.0, "duration_ms": 1200}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(planner, "_ask_codex", return_value=answer) as ask, \
                mock.patch.object(planner, "binary_for", return_value=None):          # no Claude at all
            out = planner.plan("Build the store, the search and the export of the notes program, each part with its own tests",
                               "gpt-5.6-terra", folder=folder, agents=("codex",), strategy="multi", planner_agent="codex")
        ask.assert_called_once()
        self.assertEqual(out["jobs"][0]["agent"], "codex")


class SeniorChecksTests(unittest.TestCase):
    def test_the_senior_is_not_held_to_tests_that_wait_for_the_juniors(self):
        from queenbee.mother import planner

        suite = {"type": "command", "run": "python -m unittest discover -s tests -t .", "expect_exit": 0}
        exists = {"type": "file_contains", "path": "notes/__init__.py", "text": "Note"}
        jobs = planner.normalise({"jobs": [
            {"priority": 1, "title": "Ground", "goal": "g", "senior": True, "acceptance": [suite, exists]},
            {"priority": 2, "title": "Store", "goal": "s", "after": [1], "acceptance": [suite]}]}, ("codex",))
        self.assertEqual([c["type"] for c in jobs[0]["acceptance"]], ["file_contains"])      # no whole-suite run for her
        self.assertEqual(len(jobs[1]["acceptance"]), 1)                                      # the junior keeps her check


class UnrunnableChecksTests(unittest.TestCase):
    def test_a_models_check_the_queen_may_not_run_is_dropped(self):
        from queenbee.mother import verifier

        out = verifier.normalise([{"type": "command", "run": 'python -c "import notes.store"'},
                                  {"type": "command", "run": "python -m unittest tests.test_store -t ."},
                                  {"type": "file_exists", "path": "tests/test_store.py"}])
        self.assertEqual([c["type"] for c in out], ["command", "file_exists"])
        self.assertIn("unittest", out[0]["run"])
        self.assertEqual(len(verifier.normalise([{"type": "command", "run": 'python -c "print(1)"'}], trusted=True)), 1)


class SharedBriefTests(unittest.TestCase):
    def test_the_plans_brief_becomes_context_md_once(self):
        from queenbee.mother import server

        q = server.Mother(mock.MagicMock(), queen="q8", name="T", primary=False)
        jobs = [{"kind": "task"}, {"kind": "task"}]
        with tempfile.TemporaryDirectory() as folder:
            q._write_context(folder, "Parts: store, search. Run: python -m unittest", jobs)
            path = os.path.join(folder, ".queenbee", "CONTEXT.md")
            self.assertIn("Parts: store", open(path, encoding="utf-8").read())
            q._write_context(folder, "SOMETHING ELSE", jobs)                       # the first brief stays
            self.assertIn("Parts: store", open(path, encoding="utf-8").read())
        with tempfile.TemporaryDirectory() as folder:
            q._write_context(folder, "x", [{"kind": "task"}])                      # a single job needs no team brief
            self.assertFalse(os.path.exists(os.path.join(folder, ".queenbee", "CONTEXT.md")))

    def test_worker_models_are_chosen_per_agent(self):
        from queenbee.mother import server

        q = server.Mother(mock.MagicMock(), queen="q8", name="T", primary=False)
        self.assertTrue(q.set_worker_models("haiku", "gpt-5.6-terra")["ok"])
        self.assertEqual((q.cfg["worker_model"], q.roles["codex_worker_model"]), ("haiku", "gpt-5.6-terra"))
        self.assertFalse(q.set_worker_models("bad model!", "")["ok"])


class PeersFromQueenMdTests(unittest.TestCase):
    def test_queen_md_can_ask_for_a_team_with_no_senior(self):
        from queenbee.mother import hints

        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(hints.wants_peers(folder))
            with open(os.path.join(folder, "QUEEN.md"), "w", encoding="utf-8") as fh:
                fh.write("- All workers start at once. Do not plan a senior, a skeleton or a foundation worker.")
            self.assertTrue(hints.wants_peers(folder))


class FreeWorkerTests(unittest.TestCase):
    def test_a_free_worker_is_a_container_that_is_marked_solo_and_kept(self):
        from queenbee.mother import server

        with tempfile.TemporaryDirectory() as folder, mock.patch("queenbee.mother.server.threading.Thread"), \
                mock.patch.object(server.Hive, "_save"), \
                mock.patch("queenbee.mother.server._providers", return_value=[{"id": "codex", "installed": True, "models": ["default"]}]):
            hive = server.Hive(mock.MagicMock())
            out = hive.add_free_worker("Lone", "codex", "default", folder, "write a hello script")
            self.assertTrue(out["ok"], out)
            q = hive.get(out["queen"])
            self.assertTrue(q.solo and q.manual_approvals)
            self.assertEqual(len(q.dispatch.jobs), 1)
            self.assertTrue(q.summary()["solo"])
            self.assertFalse(hive.add_free_worker("x", "gemini", "", folder, "y")["ok"])        # not installed

    def test_free_workers_in_one_folder_get_their_own_numbers(self):
        from queenbee.mother import server

        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as other, \
                mock.patch("queenbee.mother.server.threading.Thread"), mock.patch.object(server.Hive, "_save"), \
                mock.patch("queenbee.mother.server._providers", return_value=[{"id": "codex", "installed": True, "models": ["default"]}]):
            hive = server.Hive(mock.MagicMock())
            nums = []
            for f in (folder, folder, other, folder):
                out = hive.add_free_worker("Peer", "codex", "default", f, "write a hello script")
                self.assertTrue(out["ok"], out)
                nums.append(hive.get(out["queen"]).dispatch.jobs[0]["priority"])
            self.assertEqual(nums, [1, 2, 1, 3])          # W1, W2, W3 in the shared folder; the other folder starts at W1


class CodexTokensTests(unittest.TestCase):
    def test_the_total_a_codex_exec_prints_is_read(self):
        from queenbee.mother.director import codex_tokens

        self.assertEqual(codex_tokens("work\ntokens used\n16.496\nOK"), 16496)
        self.assertEqual(codex_tokens("", "tokens used: 1,234,567"), 1234567)
        self.assertEqual(codex_tokens("nothing here"), 0)


class DoneMeansTests(unittest.TestCase):
    def test_a_command_in_ticks_with_a_description_after_it_is_the_check(self):
        from queenbee.mother import hints

        text = ("Read TASK.md. Done means `python check_release.py` prints 40/40 checks pass. Start the workers when your plan is ready.")
        self.assertEqual(hints.parse(text)["check"], "python check_release.py")
        self.assertEqual(hints.parse("Done means: python check_minidb.py passes all checks.")["check"], "python check_minidb.py")
