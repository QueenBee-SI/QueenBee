"""A PTY hands over whatever bytes the OS flushed, with no regard for UTF-8
character boundaries. A multi-byte character (Turkish diacritics, emoji, ...)
split across two reads used to come back as two mangled replacement
characters instead of the one it was.
"""

import collections
import threading
import unittest

from queenbee.agents.pty_runner import PtyRunner, _utf8_safe_len


def make_runner():
    r = object.__new__(PtyRunner)
    r._lock = threading.Lock()
    r._chunks = collections.deque()
    r._size = 0
    r._offset = 0
    r._exited = False
    return r


def feed(runner, data: bytes) -> None:
    with runner._lock:
        runner._chunks.append(data)
        runner._size += len(data)
        runner._offset += len(data)


class Utf8SafeLenTests(unittest.TestCase):
    def test_ascii_is_never_trimmed(self):
        self.assertEqual(_utf8_safe_len(b"hello"), 5)

    def test_empty_is_zero(self):
        self.assertEqual(_utf8_safe_len(b""), 0)

    def test_complete_two_byte_sequence_is_kept(self):
        data = "hello é".encode("utf-8")  # trailing e-acute
        self.assertEqual(_utf8_safe_len(data), len(data))

    def test_dangling_lead_byte_is_trimmed(self):
        data = "hello é".encode("utf-8")[:-1]  # drop the continuation byte
        self.assertEqual(_utf8_safe_len(data), len(data) - 1)

    def test_four_byte_sequence_trimmed_at_every_cut_point(self):
        emoji = "\U0001F44D".encode("utf-8")  # thumbs up, 4 bytes
        for cut in range(1, 4):
            self.assertEqual(_utf8_safe_len(emoji[:cut]), 0)


class ReadSinceSplitCharacterTests(unittest.TestCase):
    def test_split_multibyte_character_reassembles_across_reads(self):
        runner = make_runner()
        full = "hello é".encode("utf-8")
        feed(runner, full[:-1])
        end1, text1 = runner.read_since(0)
        feed(runner, full[-1:])
        end2, text2 = runner.read_since(end1)
        self.assertEqual(text1 + text2, full.decode("utf-8"))
        self.assertEqual(text2, "é")
        self.assertEqual(end2, len(full))

    def test_exited_process_flushes_a_still_pending_tail(self):
        runner = make_runner()
        full = "hello é".encode("utf-8")
        feed(runner, full[:-1])  # dangling lead byte, no continuation byte ever comes
        runner._exited = True
        # No more bytes are coming, so holding the incomplete tail back would
        # lose it forever -- decode it (as a replacement character) instead.
        end, text = runner.read_since(0)
        self.assertEqual(end, len(full) - 1)
        self.assertEqual(text, full[:-1].decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
