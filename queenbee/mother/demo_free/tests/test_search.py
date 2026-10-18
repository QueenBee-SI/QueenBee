from notes import search, store
from tests._helpers import WithNotesFile


class SearchTests(WithNotesFile):
    def setUp(self):
        super().setUp()
        store.add("Buy milk", ["home", "shop"])
        store.add("call Ann", ["work"])
        store.add("milk the cow", [])
        self.notes = store.all()

    def texts(self, found):
        return [store.line(n).split("  ")[1] for n in found]

    def test_text_is_found_in_any_case_and_keeps_the_order(self):
        self.assertEqual(self.texts(search.find(self.notes, "MILK")), ["Buy milk", "milk the cow"])

    def test_a_tag_must_match_exactly(self):
        self.assertEqual(self.texts(search.find(self.notes, tag="work")), ["call Ann"])
        self.assertEqual(search.find(self.notes, tag="wor"), [])

    def test_text_and_tag_together_need_both(self):
        self.assertEqual(self.texts(search.find(self.notes, "milk", "shop")), ["Buy milk"])
        self.assertEqual(search.find(self.notes, "ann", "home"), [])

    def test_nothing_asked_gives_everything(self):
        self.assertEqual(len(search.find(self.notes)), 3)
