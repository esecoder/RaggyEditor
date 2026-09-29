"""OffsetMap: character offsets (Python) <-> byte offsets (Scintilla, UTF-8).

⚠️ These are regression tests for a bug that shipped. The editor highlighted the
wrong text whenever a document contained any non-ASCII character before the
match — silent, no error. The earlier suite missed it because it compared the
NUMBERS the engine produced with the numbers Scintilla echoed back; both were
"25", so the assertions passed while the editor selected the wrong characters.

Every assertion here checks the TEXT, not the numbers.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from raggy.offsets import OffsetMap  # noqa: E402


class TestOffsetMap(unittest.TestCase):

    def test_ascii_is_the_identity(self):
        m = OffsetMap("hello world")
        self.assertTrue(m.is_ascii)
        for i in range(len(m.text) + 1):
            self.assertEqual(m.to_byte(i), i)
            self.assertEqual(m.to_char(i), i)

    def test_multibyte_characters_shift_byte_offsets(self):
        text = "a—b"                       # em dash is 3 bytes in UTF-8
        m = OffsetMap(text)
        self.assertFalse(m.is_ascii)
        self.assertEqual(m.byte_length, 5)
        self.assertEqual(m.to_byte(0), 0)
        self.assertEqual(m.to_byte(1), 1)   # after 'a'
        self.assertEqual(m.to_byte(2), 4)   # after the dash
        self.assertEqual(m.to_byte(3), 5)

    def test_round_trip_is_exact_for_every_character(self):
        for text in ("plain", "café", "a—b—c", "emoji 🎉 here", "日本語のテキスト",
                     "mixed — café 🎉 日本", ""):
            m = OffsetMap(text)
            for i in range(len(text) + 1):
                self.assertEqual(m.to_char(m.to_byte(i)), i,
                                 f"round trip broke at {i} in {text!r}")

    def test_the_shipped_bug_the_em_dash_case(self):
        # The exact case that was wrong: char offset 25 is not byte offset 25.
        text = "INTRO — with an em dash.\nERR_CONN_4421 is the code.\n"
        m = OffsetMap(text)
        start = text.index("ERR_CONN_4421")
        end = start + len("ERR_CONN_4421")
        self.assertNotEqual(m.to_byte(start), start, "the bug is back")
        raw = text.encode("utf-8")
        self.assertEqual(raw[m.to_byte(start):m.to_byte(end)].decode(),
                         "ERR_CONN_4421")
        # And the old, naive behaviour really did select the wrong text — this is
        # what a test comparing only numbers failed to notice.
        self.assertNotEqual(raw[start:end].decode(), "ERR_CONN_4421")

    def test_byte_to_char_on_a_continuation_byte_returns_the_character(self):
        m = OffsetMap("a—b")
        # Byte 2 and 3 are inside the dash; both belong to char 1.
        self.assertEqual(m.to_char(2), 1)
        self.assertEqual(m.to_char(3), 1)

    def test_offsets_are_clamped_rather_than_raising(self):
        m = OffsetMap("abc")
        self.assertEqual(m.to_byte(-5), 0)
        self.assertEqual(m.to_byte(99), 3)
        self.assertEqual(m.to_char(-5), 0)
        self.assertEqual(m.to_char(99), 3)

    def test_emoji_counts_as_one_character_and_four_bytes(self):
        m = OffsetMap("🎉")
        self.assertEqual(m.byte_length, 4)
        self.assertEqual(m.to_byte(1), 4)
        self.assertEqual(m.to_char(4), 1)


if __name__ == "__main__":
    unittest.main()
