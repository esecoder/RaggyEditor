"""Desktop-app tests (headless).

These construct the real Qt window offscreen and drive the real Find path. They
are skipped when PyQt6/QScintilla are absent, so the core suite still runs on a
machine with only numpy.

⚠️ One test here exists purely to prevent a regression in the UI SHAPE: the
window must be a single document pane with a transient find bar — no tab widget
and no splitter. An earlier version shipped an editor + tabbed side panel, which
is a code-editor idiom and looked nothing like a text editor.
"""

import os
import pathlib
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")     # must precede Qt import
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from raggy.app import HAS_QT  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
SAMPLE = REPO / "samples" / "meridian-operations-handbook.txt"


@unittest.skipUnless(HAS_QT, "PyQt6 + QScintilla not installed")
class TestDesktopApp(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.text = SAMPLE.read_text(encoding="utf-8")

    def _window(self):
        from raggy.app import MainWindow
        w = MainWindow()
        w.editor.setText(self.text)                 # fires textChanged -> starts timer
        w.engine.index(self.text, SAMPLE.name)      # synchronous, offline encoder
        w._indexed_text = self.text
        w._index_timer.stop()                        # stop AFTER setup, not before
        w._find_timer.stop()
        return w

    def _search(self, w, query):
        w.show_find()
        w.find_bar.field.setText(query)
        w._find_timer.stop()
        w._run_search()
        return w._results

    # ---- shape (regression guard) ----------------------------------------
    def test_window_is_a_single_pane_no_tabs_no_splitter(self):
        from PyQt6.QtWidgets import QSplitter, QTabWidget
        w = self._window()
        central = w.centralWidget()
        self.assertIsNone(central.findChild(QTabWidget), "tabbed panel came back")
        self.assertIsNone(central.findChild(QSplitter), "split layout came back")
        w.close()

    def test_menu_bar_is_text_editor_shaped(self):
        w = self._window()
        menus = [a.text().replace("&", "") for a in w.menuBar().actions()]
        for expected in ("File", "Edit", "Find", "View", "Model", "Help"):
            self.assertIn(expected, menus)
        w.close()

    def test_find_bar_is_hidden_until_find_is_used(self):
        w = self._window()
        self.assertTrue(w.find_bar.isHidden())
        w.show_find()
        self.assertFalse(w.find_bar.isHidden())
        w._close_find()
        self.assertTrue(w.find_bar.isHidden())
        w.close()

    # ---- the unified search ----------------------------------------------
    def test_exact_matches_are_found(self):
        w = self._window()
        res = self._search(w, "ERR_CONN_4421")
        self.assertGreaterEqual(len(res), 1)
        # Exact hits are present, and — by design — related passages may be too:
        # it is one search box, not a word box plus a separate meaning box.
        self.assertTrue(any(r["kind"] == "match" for r in res))
        w.close()

    def test_paraphrase_returns_related_passages(self):
        # The words do not appear in the document; only meaning connects them.
        w = self._window()
        res = self._search(w, "how do I fix an expired certificate on a replica")
        self.assertTrue(res)
        self.assertTrue(any(r["kind"] == "related" for r in res))
        w.close()

    def test_one_search_box_can_return_both_kinds(self):
        # A query with a literal phrase AND semantic content yields both in one
        # list — this is the point of the redesign.
        w = self._window()
        res = self._search(w, "replication lag")
        kinds = {r["kind"] for r in res}
        self.assertIn("related", kinds)
        self.assertIn("match", kinds)
        w.close()

    def test_regex_toggle_changes_matching(self):
        w = self._window()
        w.find_bar.regex_btn.setChecked(True)
        res = self._search(w, r"ERR_[A-Z]+_\d+")
        self.assertGreaterEqual(len(res), 4)
        w.close()

    def test_empty_query_clears_results(self):
        w = self._window()
        self._search(w, "replication")
        res = self._search(w, "")
        self.assertEqual(res, [])
        self.assertEqual(w.find_bar.status.text(), "")
        w.close()

    def test_bad_regex_reports_instead_of_crashing(self):
        w = self._window()
        w.find_bar.regex_btn.setChecked(True)
        res = self._search(w, "([unclosed")
        self.assertEqual(res, [])
        self.assertIn("bad expression", w.find_bar.status.text().lower())
        w.close()

    # ---- navigation + jump ------------------------------------------------
    def test_result_selection_covers_the_exact_text(self):
        from PyQt6 import Qsci
        w = self._window()
        res = self._search(w, "how do I fix an expired certificate on a replica")
        r = res[0]
        start = w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETSELECTIONSTART)
        end = w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETSELECTIONEND)
        self.assertEqual((start, end), (r["start"], r["end"]))
        self.assertEqual(self.text[start:end], self.text[r["start"]:r["end"]])
        w.close()

    def test_next_wraps_around(self):
        w = self._window()
        res = self._search(w, "replication")
        n = len(res)
        self.assertGreater(n, 1)
        for _ in range(n):
            w._next()
        self.assertEqual(w._current, 0)                 # wrapped back to the start
        w._prev()
        self.assertEqual(w._current, n - 1)
        w.close()

    def test_status_reports_both_counts(self):
        w = self._window()
        self._search(w, "replication lag")
        text = w.find_bar.status.text()
        self.assertIn("exact", text)
        self.assertIn("related", text)
        w.close()

    def test_out_of_scope_query_is_flagged(self):
        w = self._window()
        self._search(w, "what is the capital of France")
        self.assertIn("related", w.find_bar.status.text().lower())   # may still return passages
        w._run_search()
        # Either nothing was found, or it is explicitly marked low-confidence.
        self.assertTrue(w._semantic_low or not w._results)
        w.close()

    def test_use_selection_for_find(self):
        from PyQt6 import Qsci
        w = self._window()
        i = self.text.index("ERR_AUTH_1180")
        w.editor.SendScintilla(Qsci.QsciScintilla.SCI_SETSEL, i, i + len("ERR_AUTH_1180"))
        w._use_selection()
        self.assertEqual(w.find_bar.query(), "ERR_AUTH_1180")
        self.assertTrue(w._results)
        w.close()

    # ---- indexing behaviour ----------------------------------------------
    def test_editing_schedules_a_reindex(self):
        w = self._window()
        self.assertFalse(w._index_timer.isActive())
        w.editor.setText(self.text + "\nA new final line.\n")
        self.assertTrue(w._index_timer.isActive())
        w.close()

    def test_view_toggles_do_not_raise(self):
        w = self._window()
        w._toggle_line_numbers(True)
        w._toggle_line_numbers(False)
        w._toggle_wrap(False)
        w._toggle_wrap(True)
        w.close()


if __name__ == "__main__":
    unittest.main()
