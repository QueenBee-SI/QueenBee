"""The queen as an agent in a terminal: how she is started, what she may do, how her plan and the hive reach QueenBee."""
import os
import tempfile
import time
import unittest
from unittest import mock

from queenbee.mother import config, queen_term
from queenbee.mother.dispatch import Dispatch
from queenbee.mother.server import Mother


class Brief(unittest.TestCase):
    def test_she_is_told_to_read_the_rules_and_to_write_only_the_plan_and_the_context(self):
        text = queen_term.build_brief(r"C:\proj", ["Claude Code", "Codex"])
        for needle in ("QUEEN.md", "README.md", ".queenbee", "PLAN.md", "CONTEXT.md", "Claude Code, Codex", "Do not change any file"):
            self.assertIn(needle, text)
        self.assertIn("No worker has started yet", text)
        self.assertIn("HIVE.md", queen_term.build_brief(r"C:\proj", [], hive="- W1 ..."))

    def test_claude_starts_with_the_brief_first_and_may_only_read_by_itself(self):
        a = queen_term.argv("claude", "claude.exe", "sonnet", "the brief")
        self.assertEqual(a[:2], ["claude.exe", "the brief"])
        self.assertIn("--allowedTools", a)
        self.assertEqual(a[a.index("--allowedTools") + 1:], ["Read", "Glob", "Grep"])
        self.assertNotIn("Edit", a)
        self.assertNotIn("acceptEdits", a)

    def test_codex_starts_read_only(self):
        a = queen_term.argv("codex", "codex.exe", "gpt-x", "the brief")
        self.assertIn("read-only", a)
        self.assertEqual(a[-1], "the brief")
        self.assertIn("-c", a)               # without any -c, Codex 0.158 dies asking its background server


class Plan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_plan_until_she_writes_one_in_this_session(self):
        self.assertIsNone(queen_term.read_plan(self.folder, time.time()))
        os.makedirs(os.path.join(self.folder, ".queenbee"))
        path = queen_term.plan_file(self.folder)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("Build X.\nDone when tests pass.")
        self.assertEqual(queen_term.read_plan(self.folder, time.time() - 60)["text"], "Build X.\nDone when tests pass.")
        self.assertIsNone(queen_term.read_plan(self.folder, time.time() + 3600))          # an older session's file is not hers now

    def test_the_hive_file_is_written_for_her_to_read(self):
        queen_term.write_hive(self.folder, "- W1 parser: done")
        with open(os.path.join(self.folder, ".queenbee", "HIVE.md"), encoding="utf-8") as fh:
            self.assertIn("- W1 parser: done", fh.read())


class OpenIt(unittest.TestCase):
    def make(self):
        host = mock.MagicMock()
        with mock.patch("queenbee.mother.server.threading.Thread"):
            return Mother(host, queen="q9", name="T", primary=False), host

    def test_it_opens_a_terminal_in_the_project_folder_and_remembers_it(self):
        q, host = self.make()
        host._call.return_value = {"ok": True, "id": "term-7"}
        with tempfile.TemporaryDirectory() as folder, mock.patch("queenbee.agents.catalog.binary_for", return_value="claude.exe"):
            out = q.open_queen_terminal(folder)
            self.assertEqual(out, {"ok": True, "id": "term-7"})
            args = host._call.call_args
            self.assertEqual(args[0][0], "open_terminal")
            self.assertEqual(args[0][1], folder)
            self.assertEqual(q.queen_term["id"], "term-7")
            host._call.reset_mock()
            host._call.return_value = [{"id": "term-7", "running": True}]
            self.assertEqual(q.open_queen_terminal(folder), {"ok": True, "id": "term-7"})        # an open one is reused
            self.assertEqual(host._call.call_args[0][0], "terminals")

    def test_an_ended_terminal_is_replaced_not_reused(self):
        q, host = self.make()
        with tempfile.TemporaryDirectory() as folder, mock.patch("queenbee.agents.catalog.binary_for", return_value="claude.exe"):
            q.queen_term = {"id": "term-7", "folder": folder, "started": 0, "agent": "claude"}
            host._call.side_effect = lambda m, *a, **k: ([{"id": "term-7", "running": False}] if m == "terminals"
                                                         else {"ok": True, "id": "term-8"})
            self.assertEqual(q.open_queen_terminal(folder), {"ok": True, "id": "term-8"})

    def test_removing_the_queen_closes_her_terminal(self):
        from queenbee.mother.server import Hive

        from pathlib import Path

        # A Hive loads the saved queens of this computer: with a real run going on (a benchmark), the first queen would be
        # "still at work" and refuse to be removed. The test gets a state folder of its own.
        with tempfile.TemporaryDirectory() as state, mock.patch("queenbee.config._state_dir", lambda: Path(state)),                 mock.patch("queenbee.mother.server.threading.Thread"), mock.patch.object(Hive, "_save"):
            hive = Hive(mock.MagicMock())
            q = hive.queens[0]
            q.queen_term = {"id": "term-5", "folder": ".", "started": 1.0, "agent": "codex"}
            self.assertTrue(hive.remove(q.queen)["ok"])           # the last queen goes back to a blank one
            self.assertIsNone(q.queen_term)
            q.host._call.assert_any_call("terminal_close", "term-5")

    def test_a_missing_folder_or_agent_is_said_plainly(self):
        q, host = self.make()
        self.assertFalse(q.open_queen_terminal(r"C:\no\such\folder")["ok"])
        with tempfile.TemporaryDirectory() as folder, mock.patch("queenbee.agents.catalog.binary_for", return_value=None):
            self.assertIn("not installed", q.open_queen_terminal(folder)["error"])
        host._call.assert_not_called()

    def test_her_terminal_may_be_typed_into_from_the_window(self):
        from queenbee.mother import server

        q, host = self.make()
        q.queen_term = {"id": "term-3", "folder": ".", "started": 0, "agent": "claude"}
        handler = server.Handler.__new__(server.Handler)
        handler.hive = mock.Mock(queens=[q])
        self.assertTrue(server.Handler._workers_terminal(handler, "term-3"))
        self.assertFalse(server.Handler._workers_terminal(handler, "term-4"))


class WorkersReadHerBrief(unittest.TestCase):
    def test_a_worker_is_pointed_at_the_context_file_when_there_is_one(self):
        with tempfile.TemporaryDirectory() as folder:
            job = {"project_path": folder}
            self.assertEqual(Dispatch._context_note(job), "")
            os.makedirs(os.path.join(folder, ".queenbee"))
            with open(os.path.join(folder, ".queenbee", "CONTEXT.md"), "w", encoding="utf-8") as fh:
                fh.write("architecture ...")
            note = Dispatch._context_note(job)
            self.assertIn("CONTEXT.md", note)
            self.assertIn("do not explore the whole project again", note)


if __name__ == "__main__":
    unittest.main()


def test_other_agents_start_the_queen_terminal():
    from queenbee.mother import queen_term

    assert queen_term.argv("gemini", "g", "", "BRIEF") == ["g", "-i", "BRIEF"]
    assert queen_term.argv("cursor", "c", "default", "BRIEF") == ["c", "BRIEF"]
    assert queen_term.argv("grok", "x", "m1", "BRIEF") == ["x", "-m", "m1", "BRIEF"]
    assert queen_term.argv("kimi", "k", "", "BRIEF") == ["k"]          # first message is typed later


class SwitchingHerModel(unittest.TestCase):
    """The bug seen before filming: the chat was started on Claude, the queen was switched to Codex, and the old Claude
    terminal stayed in the card. Switching must close the old terminal and the next "Start chatting" must start the new agent."""

    def make(self):
        host = mock.MagicMock()
        with mock.patch("queenbee.mother.server.threading.Thread"):
            return Mother(host, queen="q9", name="T", primary=False), host

    def test_switching_to_codex_closes_the_claude_terminal_and_the_next_chat_is_codex(self):
        q, host = self.make()
        with tempfile.TemporaryDirectory() as folder, mock.patch("queenbee.agents.catalog.binary_for", side_effect=lambda a: f"{a}.exe"), \
                mock.patch.object(q, "ensure_director"):
            q.queen_term = {"id": "term-1", "folder": folder, "started": time.time(), "agent": "claude"}
            self.assertTrue(q.set_director("codex:gpt-5.6-sol")["ok"])
            host._call.assert_any_call("terminal_close", "term-1")
            self.assertIsNone(q.queen_term)
            host._call.reset_mock()
            host._call.side_effect = lambda m, *a, **k: ([] if m == "terminals" else {"ok": True, "id": "term-2"})
            self.assertEqual(q.open_queen_terminal(folder), {"ok": True, "id": "term-2"})
            argv = host._call.call_args[0][2]
            self.assertEqual(argv[0], "codex.exe")
            self.assertIn("gpt-5.6-sol", argv)
            self.assertEqual(q.queen_term["agent"], "codex")

    def test_switching_back_to_claude_closes_the_codex_terminal(self):
        q, host = self.make()
        with mock.patch("queenbee.agents.catalog.binary_for", side_effect=lambda a: f"{a}.exe"), mock.patch.object(q, "ensure_director"):
            q.queen_term = {"id": "term-9", "folder": ".", "started": time.time(), "agent": "codex"}
            self.assertTrue(q.set_director("opus")["ok"])
            host._call.assert_any_call("terminal_close", "term-9")
            self.assertIsNone(q.queen_term)
            self.assertEqual(q.roles["director_agent"], "claude")
