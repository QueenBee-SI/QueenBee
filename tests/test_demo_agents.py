"""The first demo runs with whichever agent the README says is enough: Claude Code, or Codex alone."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _provider(pid, installed):
    return {"id": pid, "installed": installed, "models": ["haiku", "sonnet"] if pid == "claude" else ["default"]}


class DemoTeamTests(unittest.TestCase):
    def run_demo(self, claude, codex):
        from queenbee.mother import server

        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        with mock.patch("queenbee.mother.server.threading.Thread"), mock.patch.object(server.Hive, "_save"), \
                mock.patch("queenbee.mother.server._providers",
                           return_value=[_provider("claude", claude), _provider("codex", codex)]), \
                mock.patch("pathlib.Path.home", return_value=Path(home.name)):
            hive = server.Hive(mock.MagicMock())
            out = hive.demo("notes")
            return out, (hive.get(out["queen"]) if out.get("ok") else None)

    def test_with_claude_code_alone_the_workers_are_haiku_and_the_queen_is_claude(self):
        out, queen = self.run_demo(claude=True, codex=False)
        self.assertTrue(out["ok"], out)
        self.assertEqual({(j["agent"], j.get("tier")) for j in queen.dispatch.jobs}, {("claude", "haiku")})
        self.assertNotEqual(queen.roles.get("director_agent"), "codex")

    def test_with_both_it_is_one_team_of_both(self):
        out, queen = self.run_demo(claude=True, codex=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual([j["agent"] for j in sorted(queen.dispatch.jobs, key=lambda j: j["priority"])],
                         ["codex", "claude", "claude"])                  # the store part, which decides the note, on Codex
        self.assertNotEqual(queen.roles.get("director_agent"), "codex")

    def test_with_codex_alone_the_workers_and_the_queen_are_codex(self):
        out, queen = self.run_demo(claude=False, codex=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(queen.dispatch.jobs), 3)
        self.assertEqual({j["agent"] for j in queen.dispatch.jobs}, {"codex"})
        self.assertEqual(queen.roles.get("director_agent"), "codex")

    def test_with_no_agent_it_says_to_install_one(self):
        out, _ = self.run_demo(claude=False, codex=False)
        self.assertFalse(out["ok"])
        self.assertIn("Claude Code or Codex", out["error"])


class ReportAgentTests(unittest.TestCase):
    def test_the_report_is_written_by_codex_when_the_queen_is_codex_or_codex_is_all_there_is(self):
        from queenbee.mother import chat

        answer = {"ok": True, "text": "report", "tokens": 1}
        with mock.patch.object(chat, "_ask_codex", return_value=answer) as codex:
            with mock.patch.object(chat, "binary_for", side_effect=lambda a: "codex.exe" if a == "codex" else None):
                self.assertEqual(chat.complete("facts", "sonnet"), answer)           # no Claude on this computer
            with mock.patch.object(chat, "binary_for", side_effect=lambda a: a + ".exe"):
                self.assertEqual(chat.complete("facts", "sonnet", agent="codex", codex_model="m"), answer)
            self.assertEqual(codex.call_count, 2)
            self.assertEqual(codex.call_args.args[1], "m")

    def test_with_neither_agent_the_report_says_so(self):
        from queenbee.mother import chat

        with mock.patch.object(chat, "binary_for", return_value=None):
            out = chat.complete("facts", "sonnet")
        self.assertFalse(out["ok"])
        self.assertIn("Claude Code nor Codex", out["error"])


class ChatAgentTests(unittest.TestCase):
    """Talking the job through before it starts: a Codex queen, or a computer with Codex alone, talks through Codex."""

    def test_with_codex_alone_the_queen_talks_through_codex(self):
        from queenbee.mother import chat

        answer = {"ok": True, "text": "What does done mean?\nREADY: build it", "tokens": 7}
        with mock.patch.object(chat, "_ask_codex", return_value=answer) as codex, \
                mock.patch.object(chat, "binary_for", side_effect=lambda a: "codex.exe" if a == "codex" else None):
            out = chat.reply([{"role": "user", "text": "add a notes command"}], "", "sonnet")
        self.assertTrue(out["ok"], out)
        self.assertEqual((out["reply"], out["ready"], out["tokens"]), ("What does done mean?", "build it", 7))
        self.assertIn("add a notes command", codex.call_args.args[0])

    def test_a_codex_queen_talks_through_codex_even_with_claude_here(self):
        from queenbee.mother import chat

        answer = {"ok": True, "text": "hi", "tokens": 1}
        with mock.patch.object(chat, "_ask_codex", return_value=answer) as codex, \
                mock.patch.object(chat, "binary_for", side_effect=lambda a: a + ".exe"):
            self.assertTrue(chat.reply([{"role": "user", "text": "hi"}], "", "sonnet", agent="codex", codex_model="m")["ok"])
        self.assertEqual(codex.call_args.args[1], "m")

    def test_with_no_agent_the_talk_says_so(self):
        from queenbee.mother import chat

        with mock.patch.object(chat, "binary_for", return_value=None):
            out = chat.reply([{"role": "user", "text": "hi"}], "", "sonnet")
        self.assertFalse(out["ok"])
        self.assertIn("Claude Code nor Codex", out["error"])


if __name__ == "__main__":
    unittest.main()
