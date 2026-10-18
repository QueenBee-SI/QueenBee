import logging
import os
import tempfile
import unittest
from unittest import mock

from queenbee.mother import __main__ as entry
from queenbee.singleton import AlreadyRunning


class StaleInstance(unittest.TestCase):
    """The last QueenBee is still holding the lock but its server is gone."""

    def run_main(self, answers):
        said = []
        shown = []
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"QUEENBEE_STATE_DIR": d}), \
                mock.patch.object(entry.InstanceLock, "acquire", side_effect=AlreadyRunning), \
                mock.patch("queenbee.app.bring_forward", return_value=False), \
                mock.patch.object(entry, "_answers", side_effect=answers), \
                mock.patch.object(entry, "_tell", side_effect=said.append), \
                mock.patch.object(entry.time, "sleep"), \
                mock.patch("queenbee.shell.run", side_effect=lambda *a, **k: shown.append(a[0]) or 0):
            code = entry.main(["--no-window"])
            # main() opened mother.log in the temp folder through logging.basicConfig; Windows cannot remove a folder
            # holding an open file, so the handler is closed before the folder goes.
            for h in list(logging.getLogger().handlers):
                if getattr(h, "baseFilename", "").startswith(d):
                    h.close()
                    logging.getLogger().removeHandler(h)
        return code, said, shown

    def test_a_dead_server_never_gets_a_window(self):
        code, said, shown = self.run_main(lambda url: False)
        self.assertEqual((code, shown), (1, []))
        self.assertIn("still closing", said[0])

    def test_a_server_that_answers_gets_a_window(self):
        code, said, shown = self.run_main(lambda url: True)
        self.assertEqual((code, said), (0, []))
        self.assertTrue(shown[0].startswith("http://127.0.0.1:"))


class NoWindow(unittest.TestCase):
    """The window cannot open (Linux without GTK or Qt, a broken WebView2): the page goes to the browser, and why."""

    def tell(self, opened, platform="linux"):
        said = []
        with mock.patch("webbrowser.open", return_value=opened) as browser, \
                mock.patch.object(entry, "_tell", side_effect=said.append), \
                mock.patch.object(entry.sys, "platform", platform):
            got = entry._browser_instead("http://127.0.0.1:7880/?k=t", RuntimeError("no GTK or QT"), entry.Path("mother.log"))
        return got, said[0], browser

    def test_the_page_opens_in_the_browser_with_the_reason(self):
        got, said, browser = self.tell(True)
        browser.assert_called_once_with("http://127.0.0.1:7880/?k=t")
        self.assertTrue(got)
        self.assertIn("no GTK or QT", said)
        self.assertIn("opening in your browser", said)
        self.assertIn("http://127.0.0.1:7880/?k=t", said)          # the address too, in case no browser takes it
        self.assertIn("pywebview[qt]", said)                       # on Linux, how to get the window

    def test_no_browser_gives_the_address(self):
        got, said, _ = self.tell(False, platform="win32")
        self.assertFalse(got)
        self.assertIn("http://127.0.0.1:7880/?k=t", said)
        self.assertNotIn("pywebview[qt]", said)                    # the Linux hint only on Linux
