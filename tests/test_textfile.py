"""textfile: a document must come back byte-identical after open -> save.

⚠️ These are regression tests for a shipped bug. The editor read every file as
UTF-8 with Python's default newline translation and wrote it back the same way, so

    a Windows file   "a\\r\\nb\\r\\n"      ->  "a\\nb\\n"
    a Latin-1 file   b"caf\\xe9"          ->  b"caf\\xc3\\xa9"

with no warning. The assertions below are on BYTES, because that is what the user
cares about and because a str-level round trip hides both bugs completely.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from raggy.textfile import (LATIN1, UTF8, UTF8_BOM, UTF16_LE, TextFormat,  # noqa: E402
                            decode, detect_format, detect_newline, encode)


class TestNewlineDetection(unittest.TestCase):

    def test_lf(self):
        self.assertEqual(detect_newline(b"a\nb\nc\n"), "\n")

    def test_crlf(self):
        self.assertEqual(detect_newline(b"a\r\nb\r\nc\r\n"), "\r\n")

    def test_cr(self):
        self.assertEqual(detect_newline(b"a\rb\rc\r"), "\r")

    def test_one_stray_cr_does_not_decide_a_mostly_lf_file(self):
        # ⚠️ "first one wins" would call this CRLF-ish or CR; counting gets it right.
        self.assertEqual(detect_newline(b"a\nb\nc\nd\nstray\re\n"), "\n")

    def test_crlf_wins_over_bare_lf_in_a_mixed_file(self):
        self.assertEqual(detect_newline(b"a\r\nb\r\nc\n"), "\r\n")

    def test_no_newline_at_all_defaults_to_lf(self):
        self.assertEqual(detect_newline(b"one line"), "\n")


class TestEncodingDetection(unittest.TestCase):

    def test_plain_ascii_is_utf8_and_not_guessed(self):
        fmt = detect_format(b"hello")
        self.assertEqual(fmt.encoding, UTF8)

    def test_utf8_bom_is_recognised_and_remembered(self):
        fmt = detect_format(b"\xef\xbb\xbffirst")
        self.assertEqual(fmt.encoding, UTF8_BOM)
        self.assertEqual(fmt.bom, b"\xef\xbb\xbf")

    def test_utf16_boms_are_recognised(self):
        self.assertEqual(detect_format(b"\xff\xfea\x00").encoding, UTF16_LE)

    def test_latin1_is_the_floor_when_utf8_fails(self):
        fmt = detect_format(b"caf\xe9")
        self.assertEqual(fmt.encoding, LATIN1)

    def test_a_bom_is_not_left_in_the_text(self):
        text, fmt, _ = decode(b"\xef\xbb\xbfhello")
        self.assertEqual(text, "hello")
        self.assertFalse(text.startswith("\ufeff"))

    def test_utf16_decodes_without_a_stray_bom_character(self):
        text, _, _ = decode("caf\u00e9".encode("utf-16"))
        self.assertEqual(text, "caf\u00e9")


class TestRoundTrip(unittest.TestCase):
    """Open then save must not change one byte."""

    CASES = {
        "unix": b"alpha\nbeta\n",
        "windows": b"alpha\r\nbeta\r\n",
        "old mac": b"alpha\rbeta\r",
        "utf8 bom": b"\xef\xbb\xbfalpha\nbeta\n",
        "latin1 crlf": "caf\u00e9\r\nna\u00efve\r\n".encode(LATIN1),
        "empty": b"",
        "no trailing newline": b"one line only",
        "utf8 multibyte": "dash \u2014 and \u00e9\n".encode(UTF8),
        "utf16le": "hello\n".encode("utf-16-le"),
    }

    def test_every_case_round_trips_exactly(self):
        for name, raw in self.CASES.items():
            with self.subTest(case=name):
                text, fmt, _ = decode(raw)
                self.assertEqual(encode(text, fmt), raw, f"{name} did not round trip")

    def test_crlf_survives_an_edit(self):
        raw = b"one\r\ntwo\r\n"
        text, fmt, _ = decode(raw)
        out = encode(text + "three\n", fmt)
        self.assertEqual(out, b"one\r\ntwo\r\nthree\r\n")

    def test_latin1_is_not_silently_upgraded_to_utf8(self):
        raw = "caf\u00e9\n".encode(LATIN1)
        text, fmt, _ = decode(raw)
        self.assertEqual(encode(text, fmt), raw)
        self.assertNotIn(b"\xc3\xa9", encode(text, fmt))

    def test_utf8_bom_is_written_exactly_once(self):
        # ⚠️ Encoding with "utf-8-sig" AND prepending BOM_UTF8 writes two marks.
        text, fmt, _ = decode(b"\xef\xbb\xbfhello\n")
        out = encode(text, fmt)
        self.assertEqual(out.count(b"\xef\xbb\xbf"), 1)
        self.assertEqual(out, b"\xef\xbb\xbfhello\n")

    def test_the_buffer_never_contains_a_carriage_return(self):
        text, _, _ = decode(b"a\r\nb\rc\n")
        self.assertNotIn("\r", text)
        self.assertEqual(text, "a\nb\nc\n")


class TestFormatDescription(unittest.TestCase):

    def test_names_are_human_readable(self):
        self.assertEqual(TextFormat(newline="\n").newline_name, "LF")
        self.assertEqual(TextFormat(newline="\r\n").newline_name, "CRLF")
        self.assertEqual(TextFormat(newline="\r").newline_name, "CR")
        self.assertIn("latin-1", TextFormat(encoding=LATIN1).describe())

    def test_utf8_flag(self):
        self.assertTrue(TextFormat(encoding=UTF8).is_utf8)
        self.assertTrue(TextFormat(encoding=UTF8_BOM).is_utf8)
        self.assertFalse(TextFormat(encoding=LATIN1).is_utf8)


if __name__ == "__main__":
    unittest.main()
