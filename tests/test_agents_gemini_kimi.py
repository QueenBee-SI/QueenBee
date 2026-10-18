"""Gemini and Kimi as workers (experimental), the catalog's install and sign-in details, and the setup window's safety."""
import unittest
from unittest import mock

from queenbee.agents import catalog
from queenbee.mother import config, server
from queenbee.mother.dispatch import Dispatch


class Argv(unittest.TestCase):
    def make(self):
        return Dispatch([], config.load(), mock.MagicMock(), 7878)

    def test_gemini_takes_the_prompt_with_i_and_stays_interactive(self):
        d = self.make()
        with mock.patch("queenbee.mother.dispatch.binary_for", return_value="gemini.cmd"):
            argv = d._argv({"agent": "gemini", "tier": "default", "effort": "medium"}, "do the thing")
            named = d._argv({"agent": "gemini", "tier": "gemini-2.5-pro", "effort": "medium"}, "x")
        self.assertEqual(argv, ["gemini.cmd", "-i", "do the thing"])
        self.assertEqual(named[:3], ["gemini.cmd", "-m", "gemini-2.5-pro"])

    def test_kimi_starts_bare_because_it_has_no_first_message_flag(self):
        d = self.make()
        with mock.patch("queenbee.mother.dispatch.binary_for", return_value="kimi.exe"):
            self.assertEqual(d._argv({"agent": "kimi", "tier": "default", "effort": "medium"}, "brief"), ["kimi.exe"])

    def test_opencode_and_mistral_are_started_bare_and_their_first_message_is_typed(self):
        d = self.make()
        for agent in ("opencode", "mistral"):
            with mock.patch("queenbee.mother.dispatch.binary_for", return_value=f"{agent}.exe"):
                self.assertEqual(d._argv({"agent": agent, "tier": "default", "effort": "medium"}, "brief"), [f"{agent}.exe"])
            self.assertIn(agent, d.TYPED_FIRST)

    def test_cursor_and_grok_take_the_task_as_the_first_turn(self):
        d = self.make()
        with mock.patch("queenbee.mother.dispatch.binary_for", return_value="x.exe"):
            self.assertEqual(d._argv({"agent": "cursor", "tier": "default", "effort": "medium"}, "do it"), ["x.exe", "do it"])
            self.assertEqual(d._argv({"agent": "grok", "tier": "grok-4", "effort": "medium"}, "do it"), ["x.exe", "-m", "grok-4", "do it"])
            self.assertEqual(d._argv({"agent": "cursor", "tier": "gpt-5", "effort": "medium"}, "do it"),
                             ["x.exe", "--model", "gpt-5", "do it"])

    def test_the_agents_are_known_to_the_planner_config_and_the_model_lists(self):
        self.assertIn("gemini", config.AGENTS)
        self.assertIn("kimi", config.AGENTS)
        self.assertIn("gemini", Dispatch.MODELS)
        self.assertIn("kimi", Dispatch.MODELS)

    def test_providers_list_all_four_and_mark_the_new_ones_experimental(self):
        rows = {p["id"]: p for p in server._providers()}
        self.assertEqual(set(rows), {"claude", "codex", "kimi", "gemini", "opencode", "cursor", "mistral", "grok"})
        self.assertTrue(all(rows[a]["experimental"] for a in ("kimi", "gemini", "opencode", "cursor", "mistral", "grok")))
        self.assertFalse(rows["claude"]["experimental"])


class Setup(unittest.TestCase):
    def test_every_agent_has_a_way_to_sign_in_and_the_catalog_gives_the_install_command(self):
        by = {s.id: s for s in catalog.CATALOG}
        for agent in ("claude", "codex", "gemini", "kimi", "opencode", "cursor", "mistral", "grok"):
            self.assertTrue(by[agent].login_command, agent)
            self.assertTrue(by[agent].install_command, agent)
        self.assertEqual(by["gemini"].needs, "npm")

    def test_the_setup_command_comes_from_the_catalog_never_from_the_window(self):
        with mock.patch("subprocess.Popen") as popen, mock.patch("shutil.which", return_value="npm"):
            out = server.agent_setup("gemini", "install")
        self.assertTrue(out["ok"])
        self.assertEqual(out["command"], "npm install -g @google/gemini-cli")
        self.assertIn("npm install -g @google/gemini-cli", " ".join(popen.call_args[0][0]))     # inside a bash -lc on Linux

    def test_unknown_agents_and_actions_are_refused(self):
        with mock.patch("subprocess.Popen") as popen:
            self.assertFalse(server.agent_setup("rm -rf", "install")["ok"])
            self.assertFalse(server.agent_setup("claude", "format")["ok"])
        popen.assert_not_called()

    def test_an_npm_agent_without_npm_says_what_is_missing(self):
        with mock.patch("subprocess.Popen") as popen, mock.patch("shutil.which", return_value=None):
            out = server.agent_setup("codex", "install")
        self.assertFalse(out["ok"])
        self.assertIn("Node.js", out["error"])
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class NewModelsAppear(unittest.TestCase):
    def test_a_claude_model_that_has_run_here_joins_the_list_without_editing_it(self):
        with mock.patch("queenbee.mother.ledger.models_seen", return_value=["claude-haiku-5-5", "claude-opus-5-5"]):
            models = {p["id"]: p for p in server._providers()}["claude"]["models"]
        self.assertIn("claude-haiku-5-5", models)
        self.assertEqual(models.count("claude-opus-5-5"), 1)
        self.assertEqual(models[0], "haiku")                          # the aliases stay first: they follow the newest


class NewestClaude(unittest.TestCase):
    """Claude Code can be on PATH and inside the Claude desktop app, updated separately: the newest one runs."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.old = Path(self.tmp.name, "bin", "claude.exe")
        self.new = Path(self.tmp.name, "app", "claude-code", "2.1.293", "abc123", "claude.exe")
        for p in (self.old, self.new):
            p.parent.mkdir(parents=True)
            p.write_text("x")
        self.spec = catalog.AgentSpec(id="claude", label="Claude Code", binaries=("claude",),
                                      extra_globs=(str(Path(self.tmp.name, "app", "claude-code", "*", "*", "claude.exe")),))

    def tearDown(self):
        self.tmp.cleanup()

    def versions(self, path):
        return (2, 1, 293) if "abc123" in str(path) else (2, 1, 288)

    def test_the_newer_install_wins_over_the_one_on_path(self):
        with mock.patch("shutil.which", return_value=str(self.old)), mock.patch.object(catalog, "_claude_version", self.versions):
            self.assertEqual(catalog._newest_claude(self.spec), str(self.new))

    def test_on_a_tie_the_one_on_path_stays(self):
        with mock.patch("shutil.which", return_value=str(self.old)), mock.patch.object(catalog, "_claude_version", lambda p: (2, 1, 1)):
            self.assertEqual(catalog._newest_claude(self.spec), str(self.old))

    def test_a_single_install_is_used_without_asking_its_version(self):
        with mock.patch("shutil.which", return_value=str(self.old)), mock.patch.object(catalog, "_claude_version") as asked:
            spec = catalog.AgentSpec(id="claude", label="Claude Code", binaries=("claude",), extra_globs=())
            self.assertEqual(catalog._newest_claude(spec), str(self.old))
        asked.assert_not_called()

    def test_none_installed_is_none(self):
        with mock.patch("shutil.which", return_value=None):
            spec = catalog.AgentSpec(id="claude", label="Claude Code", binaries=("claude",), extra_globs=())
            self.assertIsNone(catalog._newest_claude(spec))
