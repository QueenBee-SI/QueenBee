import subprocess
import sys

from notes import store
from tests._helpers import WithNotesFile


class StoreTests(WithNotesFile):
    def test_ids_start_at_one_and_grow_and_all_keeps_the_order(self):
        self.assertEqual(store.add("buy milk", ["home"]), 1)
        self.assertEqual(store.add("call Ann", []), 2)
        self.assertEqual(store.add("pay rent", ["home", "money"]), 3)
        self.assertEqual(len(store.all()), 3)

    def test_the_line_format(self):
        store.add("buy milk", ["home", "shop"])
        store.add("call Ann", [])
        a, b = store.all()
        self.assertEqual(store.line(a), "1  buy milk  [home, shop]")
        self.assertEqual(store.line(b), "2  call Ann")

    def test_notes_are_kept_in_the_file_between_runs(self):
        store.add("one", ["x"])
        out = subprocess.run([sys.executable, "-m", "notes", "list"], capture_output=True, text=True,
                             env={**__import__("os").environ})
        self.assertEqual(out.stdout.strip(), "1  one  [x]")
        subprocess.run([sys.executable, "-m", "notes", "add", "two"], capture_output=True, text=True)
        self.assertEqual(store.add("three", []), 3)
