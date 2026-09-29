"""Desktop-app tests (headless).

These construct the real Qt window offscreen and drive the real Find path. They
are skipped when PyQt6/QScintilla are absent, so the core suite still runs on a
machine with only numpy.

Two of them exist purely to stop the UI drifting back into a code editor:
  * the window must be a single pane — no tab widget, no splitter;
  * the menu bar must carry no implementation jargon — no Model menu, no
    encoder choices.
"""

import os
import pathlib
import sys
import unittest
from unittest import mock

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
        w.editor.setModified(False)                  # so close() cannot prompt
        return w

    def _search(self, w, query):
        w.show_find()
        w.find_bar.field.setText(query)
        w._find_timer.stop()
        w._run_search()
        return w._results

    # ---- shape (regression guards) ---------------------------------------
    def test_window_is_a_single_pane_no_tabs_no_splitter(self):
        from PyQt6.QtWidgets import QSplitter, QTabWidget
        w = self._window()
        central = w.centralWidget()
        self.assertIsNone(central.findChild(QTabWidget), "tabbed panel came back")
        self.assertIsNone(central.findChild(QSplitter), "split layout came back")
        w.close()

    def test_menu_bar_carries_no_implementation_jargon(self):
        w = self._window()
        menus = [a.text().replace("&", "") for a in w.menuBar().actions()]
        self.assertEqual(menus, ["File", "Edit", "Find", "Help"])
        for jargon in ("Model", "View"):
            self.assertNotIn(jargon, menus)
        w.close()

    def test_there_is_no_status_bar(self):
        from PyQt6.QtWidgets import QStatusBar
        w = self._window()
        self.assertIsNone(w.findChild(QStatusBar), "status bar came back")
        w.close()

    def test_ask_is_reachable_from_the_find_menu(self):
        w = self._window()
        find_menu = next(a.menu() for a in w.menuBar().actions()
                         if a.text().replace("&", "") == "Find")
        labels = [a.text().replace("&", "") for a in find_menu.actions() if a.text()]
        self.assertTrue(any("Ask" in x for x in labels))
        w.close()

    # ---- title ------------------------------------------------------------
    def test_title_is_the_document_name_not_the_app_name(self):
        from raggy.app import APP_NAME
        w = self._window()
        w.path = None
        w._update_title()
        self.assertEqual(w.windowTitle(), "Untitled")
        w.path = "/tmp/notes.txt"
        w._update_title()
        self.assertEqual(w.windowTitle(), "notes.txt")
        self.assertNotIn(APP_NAME, w.windowTitle())
        w.path = None                                # do not let close() prompt to save
        w.close()

    # ---- find bar ---------------------------------------------------------
    def test_find_bar_is_hidden_until_find_is_used(self):
        w = self._window()
        self.assertTrue(w.find_bar.isHidden())
        w.show_find()
        self.assertFalse(w.find_bar.isHidden())
        w.close()

    def test_escape_closes_the_find_bar(self):
        w = self._window()
        w.show_find()
        self.assertFalse(w.find_bar.isHidden())
        w.find_bar.field.escapePressed.emit()       # what Esc does in the field
        self.assertTrue(w.find_bar.isHidden())
        w.close()

    # ---- the unified search ----------------------------------------------
    def test_exact_matches_are_found(self):
        w = self._window()
        res = self._search(w, "ERR_CONN_4421")
        self.assertGreaterEqual(len(res), 1)
        self.assertTrue(any(r["kind"] == "match" for r in res))
        w.close()

    def test_paraphrase_returns_related_passages(self):
        w = self._window()
        res = self._search(w, "how do I fix an expired certificate on a replica")
        self.assertTrue(res)
        self.assertTrue(any(r["kind"] == "related" for r in res))
        w.close()

    def test_one_search_box_can_return_both_kinds(self):
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
        w.close()

    def test_next_wraps_around(self):
        w = self._window()
        res = self._search(w, "replication")
        n = len(res)
        self.assertGreater(n, 1)
        for _ in range(n):
            w._next()
        self.assertEqual(w._current, 0)
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

    def test_use_selection_for_find(self):
        from PyQt6 import Qsci
        w = self._window()
        i = self.text.index("ERR_AUTH_1180")
        w.editor.SendScintilla(Qsci.QsciScintilla.SCI_SETSEL, i, i + len("ERR_AUTH_1180"))
        w._use_selection()
        self.assertEqual(w.find_bar.query(), "ERR_AUTH_1180")
        self.assertTrue(w._results)
        w.close()

    # ---- indexing ---------------------------------------------------------
    def test_editing_schedules_a_reindex(self):
        w = self._window()
        self.assertFalse(w._index_timer.isActive())
        w.editor.setText(self.text + "\nA new final line.\n")
        self.assertTrue(w._index_timer.isActive())
        w.close()

    # ---- the meaning-search offer ----------------------------------------
    def test_meaning_is_offered_once_and_only_when_it_can_help(self):
        w = self._window()
        with mock.patch("raggy.encoder.onnx_importable", return_value=True), \
             mock.patch("raggy.app.model_store.is_available", return_value=False):
            w._meaning_offer_made = False
            self.assertTrue(w._should_offer_meaning())
            w._meaning_offer_made = True               # already asked once
            self.assertFalse(w._should_offer_meaning())
        w.close()

    def test_meaning_is_not_offered_when_the_model_is_already_there(self):
        w = self._window()
        with mock.patch("raggy.app.model_store.is_available", return_value=True):
            w._meaning_offer_made = False
            self.assertFalse(w._should_offer_meaning())
        w.close()


if __name__ == "__main__":
    unittest.main()
