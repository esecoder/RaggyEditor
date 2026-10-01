"""Print/export layout: the wrapping and paging rules, with no Qt involved.

⚠️ `measure` is injected, so these run on a machine with only numpy. The fake used
here is 1 unit per character, which makes the assertions readable: a max width of
10 means "ten characters", exactly like a monospace document.

The promise being tested is the one that matters to a reader: **with wrapping off,
printed row N is document line N.** Otherwise a printed "line 61" disagrees with
line 61 in the window.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from raggy.printing import (expand_tabs, footer_text, paginate,  # noqa: E402
                            rows_for_document, rows_per_page, wrap_line)

mono = len                      # one unit per character


class TestTabs(unittest.TestCase):

    def test_tabs_expand_to_the_next_stop(self):
        self.assertEqual(expand_tabs("a\tb", 8), "a" + " " * 7 + "b")
        self.assertEqual(expand_tabs("\t", 4), " " * 4)
        self.assertEqual(expand_tabs("abcd\tx", 4), "abcd" + " " * 4 + "x")

    def test_a_tab_at_an_exact_stop_advances_a_full_stop(self):
        self.assertEqual(expand_tabs("abcd\t", 4), "abcd" + " " * 4)

    def test_leading_tabs_become_leading_spaces(self):
        # An indented line must stay indented on paper.
        self.assertTrue(expand_tabs("\t\tcode", 4).startswith(" " * 8))

    def test_lines_without_tabs_are_untouched(self):
        self.assertEqual(expand_tabs("plain text", 8), "plain text")

    def test_tab_width_is_clamped_to_at_least_one(self):
        self.assertEqual(expand_tabs("\t", 0), " ")


class TestWrapping(unittest.TestCase):

    def test_a_short_line_is_one_row(self):
        self.assertEqual(wrap_line("short", mono, 40, True), ["short"])

    def test_a_blank_line_stays_a_line(self):
        self.assertEqual(wrap_line("", mono, 40, True), [""])

    def test_wrapping_is_off_by_request(self):
        long = "x" * 200
        self.assertEqual(wrap_line(long, mono, 10, wrap=False), [long])

    def test_long_lines_split_on_spaces(self):
        rows = wrap_line("aaa bbb ccc ddd eee", mono, 11, True)
        self.assertGreater(len(rows), 1)
        for row in rows:
            self.assertLessEqual(mono(row), 11, f"{row!r} is too wide")
        self.assertEqual(" ".join(rows), "aaa bbb ccc ddd eee")

    def test_an_unbreakable_run_is_cut_not_overflowed(self):
        # A long path or a base64 blob has no spaces to break at.
        rows = wrap_line("y" * 25, mono, 10, True)
        self.assertEqual(rows, ["y" * 10, "y" * 10, "y" * 5])
        for row in rows:
            self.assertLessEqual(mono(row), 10)

    def test_no_row_is_ever_wider_than_the_page(self):
        text = ("supercalifragilistic " * 3) + ("z" * 97)
        for row in wrap_line(text, mono, 13, True):
            self.assertLessEqual(mono(row), 13)

    def test_tabs_are_expanded_before_measuring(self):
        # Six tabs at width 4 put 'x' at column 25. If a tab were measured as one
        # character, the whole thing would fit on a single row.
        rows = wrap_line("\t\t\t\t\t\tx", mono, 10, True, tab_width=4)
        self.assertEqual("".join(rows), " " * 24 + "x")
        self.assertEqual(len(rows), 3)                  # 25 columns, 10 per row
        self.assertTrue(rows[-1].endswith("x"))

    def test_wrapping_keeps_leading_indentation(self):
        # ⚠️ REGRESSION GUARD: `"    indented".split(" ")` throws the leading
        # whitespace away, so an indented listing came out flat on paper.
        rows = wrap_line("        deeply indented code that runs on", mono, 20, True)
        self.assertTrue(rows[0].startswith(" " * 8), f"indent lost: {rows[0]!r}")
        for row in rows:
            self.assertLessEqual(mono(row), 20)


class TestDocumentRows(unittest.TestCase):

    def test_wrapping_off_preserves_line_numbers(self):
        # ⚠️ THE promise: printed row N is document line N.
        lines = [f"line {i}" for i in range(1, 201)]
        text = "\n".join(lines)
        rows = rows_for_document(text, mono, 40, wrap=False)
        self.assertEqual(len(rows), len(lines))
        self.assertEqual(rows[60], "line 61")
        self.assertEqual(rows, lines)

    def test_blank_lines_are_preserved(self):
        self.assertEqual(rows_for_document("a\n\nb", mono, 40, False), ["a", "", "b"])

    def test_wrapping_on_produces_at_least_as_many_rows(self):
        text = "\n".join(["a" * 50, "short"])
        self.assertGreater(len(rows_for_document(text, mono, 10, True)),
                           len(rows_for_document(text, mono, 10, False)))

    def test_a_trailing_newline_makes_a_final_empty_row(self):
        # "a\n" is two lines in an editor, and must be two rows on paper.
        self.assertEqual(rows_for_document("a\n", mono, 40, False), ["a", ""])

    def test_empty_document_is_not_an_error(self):
        self.assertEqual(rows_for_document("", mono, 40, True), [""])


class TestPaging(unittest.TestCase):

    def test_paginate_splits_evenly_and_keeps_the_tail(self):
        pages = paginate([str(i) for i in range(10)], 3)
        self.assertEqual([len(p) for p in pages], [3, 3, 3, 1])
        self.assertEqual(sum(pages, []), [str(i) for i in range(10)])

    def test_paginate_never_returns_zero_pages(self):
        self.assertEqual(paginate([], 10), [[]])

    def test_paginate_rejects_a_zero_or_negative_rows_per_page(self):
        self.assertEqual(paginate(["a", "b"], 0), [["a"], ["b"]])
        self.assertEqual(paginate(["a", "b"], -5), [["a"], ["b"]])

    def test_rows_per_page_reserves_room_for_the_footer(self):
        self.assertEqual(rows_per_page(100, 10, 20), 8)
        self.assertEqual(rows_per_page(100, 10, 0), 10)

    def test_rows_per_page_is_never_zero(self):
        self.assertEqual(rows_per_page(5, 10, 0), 1)
        self.assertEqual(rows_per_page(100, 0, 0), 1)     # no divide by zero

    def test_footer_names_the_document_and_the_page(self):
        self.assertEqual(footer_text("notes.txt", 2, 7),
                         "notes.txt   ·   page 2 of 7")


if __name__ == "__main__":
    unittest.main()
