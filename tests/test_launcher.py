"""The packaged app's entry point: -m runs a module, --stop-all ends the host, anything else opens the window."""
import unittest
from unittest import mock

from queenbee import launcher


class Launcher(unittest.TestCase):
    def test_stop_all_ends_the_host(self):
        with mock.patch("queenbee.host.main", return_value=0) as host:
            self.assertEqual(launcher.main(["--stop-all"]), 0)
        host.assert_called_once_with(["--stop"])

    def test_other_arguments_go_to_the_window(self):
        with mock.patch("queenbee.mother.__main__.main", return_value=0) as queen:
            self.assertEqual(launcher.main(["--no-window"]), 0)
        queen.assert_called_once_with(["--no-window"])
