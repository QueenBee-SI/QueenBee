"""macOS and Linux: the agents' install lines and places, the install window, and the double-clickable launcher."""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class CatalogOnPosix(unittest.TestCase):
    def test_posix_install_lines_and_places(self):
        from queenbee.agents import catalog
        with mock.patch.object(catalog, "WINDOWS", False):
            self.assertEqual(catalog._install("irm x | iex", "curl -fsSL x | bash"), "curl -fsSL x | bash")
        with mock.patch.object(catalog, "WINDOWS", True):
            self.assertEqual(catalog._install("irm x | iex", "curl -fsSL x | bash"), "irm x | iex")
        places = catalog._posix_bins("claude")
        self.assertIn("/opt/homebrew/bin/claude", places)
        self.assertTrue(any(p.endswith(os.path.join(".local", "bin", "claude")) for p in places))
        claude = next(s for s in catalog.CATALOG if s.id == "claude")
        self.assertTrue(any("homebrew" in g for g in claude.extra_globs))

    def test_the_install_line_matches_the_platform_the_module_loaded_on(self):
        from queenbee.agents import catalog
        claude = next(s for s in catalog.CATALOG if s.id == "claude")
        if os.name == "nt":
            self.assertTrue(claude.install_command.startswith("irm "))
        else:
            self.assertTrue(claude.install_command.startswith("curl "))


class InstallWindow(unittest.TestCase):
    def test_macos_opens_terminal_app_with_the_command(self):
        from queenbee.mother import server
        with mock.patch.object(server.os, "name", "posix"), mock.patch.object(server.sys, "platform", "darwin"), \
                mock.patch("subprocess.Popen") as popen, mock.patch("shutil.which", return_value="/usr/bin/npm"):
            out = server.agent_setup("codex", "install")
        self.assertTrue(out["ok"], out)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], "osascript")
        self.assertIn("npm install -g @openai/codex", " ".join(argv))

    def test_linux_without_a_terminal_says_what_to_run(self):
        from queenbee.mother import server
        with mock.patch.object(server.os, "name", "posix"), mock.patch.object(server.sys, "platform", "linux"), \
                mock.patch("subprocess.Popen") as popen, mock.patch("shutil.which", return_value=None):
            out = server.agent_setup("claude", "login")
        self.assertFalse(out["ok"])
        self.assertIn("claude", out["error"])
        popen.assert_not_called()


class Launchers(unittest.TestCase):
    def test_macos_gets_a_command_file_on_the_desktop(self):
        from queenbee.mother import __main__ as main
        with tempfile.TemporaryDirectory() as home, mock.patch.object(Path, "home", return_value=Path(home)), \
                mock.patch.object(main.sys, "platform", "darwin"):
            made = main._posix_launchers()
        self.assertEqual(len(made), 1)
        self.assertTrue(made[0].endswith("QueenBee.command"))

    def test_linux_gets_a_desktop_entry(self):
        from queenbee.mother import __main__ as main
        with tempfile.TemporaryDirectory() as home, mock.patch.object(Path, "home", return_value=Path(home)), \
                mock.patch.object(main.sys, "platform", "linux"):
            made = main._posix_launchers()
            text = Path(made[0]).read_text(encoding="utf-8")
        self.assertEqual(len(made), 2)
        self.assertIn("[Desktop Entry]", text)
        self.assertIn("queenbee.mother", text)
