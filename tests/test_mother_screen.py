"""Reading an agent's screen the way a person sees it."""

import unittest

from queenbee.mother.screen import render


def at(row, col, text):
    return f"\x1b[{row};{col}H{text}"


class ScreenTests(unittest.TestCase):
    def test_words_placed_by_cursor_moves_form_lines(self):
        # Codex draws each word of a wrapped command with its own cursor move.
        data = ("\x1b]0;title\x07" + at(1, 3, "Would") + at(1, 9, "you") + at(1, 13, "like?")
                + at(2, 3, r"$ python 'C:\a") + at(3, 3, r"b\train.py'")
                + at(5, 1, "› 1. Yes") + at(6, 3, "2. No"))
        rows = render(data, 40, 8)
        self.assertEqual(rows[0], "  Would you like?")
        self.assertEqual(rows[1:3], [r"  $ python 'C:\a", r"  b\train.py'"])
        self.assertEqual(rows[4:6], ["› 1. Yes", "  2. No"])

    def test_erase_scroll_and_alternate_screen(self):
        rows = render("old\r\x1b[Knew\r\n\x1b[?1049hTUI\x1b[?1049l", 20, 5)
        self.assertEqual(rows[0], "new")
        rows = render("\r\n".join("abcdef"), 20, 5)
        self.assertEqual(rows, ["b", "c", "d", "e", "f"])

    def test_long_line_wraps_at_the_width(self):
        self.assertEqual(render("x" * 25, 20, 5)[:2], ["x" * 20, "x" * 5])


if __name__ == "__main__":
    unittest.main()
