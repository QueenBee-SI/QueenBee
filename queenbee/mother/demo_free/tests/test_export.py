from notes import export, store
from tests._helpers import WithNotesFile


class ExportTests(WithNotesFile):
    def test_markdown_format(self):
        store.add("buy milk", ["home", "shop"])
        store.add("call Ann", [])
        self.assertEqual(export.to_markdown(store.all()),
                         "# Notes\n\n- buy milk (home, shop)\n- call Ann\n")

    def test_no_notes_is_just_the_title(self):
        self.assertEqual(export.to_markdown([]), "# Notes\n\n")
