import os
import tempfile
import unittest


class WithNotesFile(unittest.TestCase):
    """Every test gets its own empty notes file (NOTES_FILE)."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "notes.json")
        self._old = os.environ.get("NOTES_FILE")
        os.environ["NOTES_FILE"] = self.path
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("NOTES_FILE", None)
        else:
            os.environ["NOTES_FILE"] = self._old
        self._dir.cleanup()
