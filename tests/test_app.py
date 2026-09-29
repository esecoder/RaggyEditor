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
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")     # must precede Qt import
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

# Hermetic: nothing here should read (or write) the developer's real AI settings
# or pick up a key from their environment.
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="raggy-test-cfg-")
for _k in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
    os.environ.pop(_k, None)

from raggy.app import HAS_QT  # noqa: E402

if HAS_QT:
    # ⚠️ Keep QSettings out of the developer's real preferences: route the default
    # format into the same scratch directory used for XDG_CONFIG_HOME.
    from PyQt6.QtCore import QSettings as _QSettings
    _QSettings.setDefaultFormat(_QSettings.Format.IniFormat)
    _QSettings.setPath(_QSettings.Format.IniFormat, _QSettings.Scope.UserScope,
                       os.environ["XDG_CONFIG_HOME"])

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
        self.assertEqual(menus, ["File", "Edit", "Find", "View", "Help"])
        # ⚠️ "Model" was a menu of encoder choices (auto/onnx/neural/lsa). It must
        # never come back: those are words about the implementation, not the
        # document. "View" is fine — it holds only zoom.
        self.assertNotIn("Model", menus)
        view = next(a.menu() for a in w.menuBar().actions()
                    if a.text().replace("&", "") == "View")
        labels = [x.text().replace("&", "") for x in view.actions() if x.text()]
        self.assertEqual(labels, ["Zoom In", "Zoom Out", "Actual Size"])
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
        # Qt's [*] placeholder must be present for the unsaved-changes marker.
        self.assertTrue(w.windowTitle().endswith("[*]"))
        self.assertEqual(w.windowTitle().replace("[*]", ""), "Untitled")
        w.path = "/tmp/notes.txt"
        w._update_title()
        self.assertEqual(w.windowTitle().replace("[*]", ""), "notes.txt")
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
        # ⚠️ Compare the TEXT, not the offsets. The engine reports characters
        # and Scintilla reports UTF-8 bytes, so the numbers legitimately differ on
        # any document with a non-ASCII character (this sample has several).
        from PyQt6 import Qsci
        w = self._window()
        res = self._search(w, "how do I fix an expired certificate on a replica")
        r = res[0]
        start = w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETSELECTIONSTART)
        end = w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETSELECTIONEND)
        selected = self.text.encode("utf-8")[start:end].decode("utf-8")
        self.assertEqual(selected, self.text[r["start"]:r["end"]])
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
        w._select_chars(i, i + len("ERR_AUTH_1180"))   # char offsets -> bytes
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
        w.editor.setModified(False)      # close() would otherwise prompt to save
        w.close()

    def test_closing_an_edited_document_asks_about_unsaved_changes(self):
        # TextEdit prompts even for an untitled document, so we must too.
        from PyQt6.QtWidgets import QMessageBox
        w = self._window()
        w.path = None
        w.editor.setText("edited")
        self.assertTrue(w.editor.isModified())
        with mock.patch("raggy.app.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Discard):
            self.assertTrue(w._confirm_discard())
        w.editor.setModified(False)
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


@unittest.skipUnless(HAS_QT, "PyQt6 + QScintilla not installed")
class TestTextEditDetails(unittest.TestCase):
    """The small things that make it look like a macOS text editor."""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from raggy.app import MainWindow
        w = MainWindow()
        w._index_timer.stop()
        w._find_timer.stop()
        w.editor.setModified(False)
        return w

    def test_no_left_gutter_at_all(self):
        # ⚠️ Margin 0 is not the only one: margin 1 (the symbol margin) defaults
        # to 16px, which is the grey strip that appears down the left edge.
        w = self._window()
        for m in (0, 1, 2):
            self.assertEqual(w.editor.marginWidth(m), 0, f"margin {m} is visible")
        w.close()

    def test_find_field_placeholder_describes_what_find_can_do(self):
        from raggy.app import FIND_PLACEHOLDER
        w = self._window()
        self.assertEqual(w.find_bar.field.placeholderText(), FIND_PLACEHOLDER)
        self.assertIn("words", FIND_PLACEHOLDER.lower())
        self.assertIn("mean", FIND_PLACEHOLDER.lower())
        w.close()

    def test_file_menu_has_new(self):
        w = self._window()
        file_menu = next(a.menu() for a in w.menuBar().actions()
                         if a.text().replace("&", "") == "File")
        labels = [a.text().replace("&", "") for a in file_menu.actions() if a.text()]
        self.assertIn("New", labels)
        self.assertIn("Open…", labels)
        self.assertIn("Save", labels)
        w.close()

    def test_new_clears_the_document(self):
        w = self._window()
        w.editor.setText("some text")
        w.path = "/tmp/x.txt"
        w.editor.setModified(False)                  # so no save prompt
        w.new_file()
        self.assertEqual(w.editor.text(), "")
        self.assertIsNone(w.path)
        self.assertEqual(w.windowTitle().replace("[*]", ""), "Untitled")
        self.assertFalse(w.editor.isModified())
        w.close()


@unittest.skipUnless(HAS_QT, "PyQt6 + QScintilla not installed")
class TestAskDialog(unittest.TestCase):
    """Ask answers arrive in a real, roomy window — not a cramped message box."""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_dialog_is_large_and_resizable(self):
        from raggy.app import AskDialog
        d = AskDialog()
        self.assertGreaterEqual(d.minimumWidth(), 640)
        self.assertGreaterEqual(d.minimumHeight(), 460)
        d.close()

    def test_answer_text_is_shown_and_busy_state_toggles(self):
        from raggy.app import AskDialog
        d = AskDialog()
        d.start("why is it slow?")
        self.assertFalse(d.ask_btn.isEnabled())
        d.show_answer("Because the queue is full.")
        self.assertTrue(d.ask_btn.isEnabled())
        self.assertIn("queue is full", d.answer.toPlainText())
        d.close()

    def test_question_signal_fires(self):
        from raggy.app import AskDialog
        seen = []
        d = AskDialog()
        d.asked.connect(seen.append)
        d.question.setText("what breaks?")
        d.ask_btn.click()
        self.assertEqual(seen, ["what breaks?"])
        d.close()

    def test_ask_menu_opens_the_dialog(self):
        from raggy.app import MainWindow
        w = MainWindow()
        w.ask_document()
        self.assertIsNotNone(w._ask_dialog)
        self.assertFalse(w._ask_dialog.isHidden())
        w._ask_dialog.close()
        w.close()

    def test_the_three_answer_modes_are_formatted_honestly(self):
        from raggy.app import MainWindow
        gen = MainWindow._format_answer({
            "mode": "generated", "answer": "Restart the worker.",
            "citations": [{"line": 61, "text": "drain the worker first"}], "cited": [0]})
        self.assertIn("Restart the worker.", gen)
        self.assertIn("line 61", gen)

        abst = MainWindow._format_answer({"mode": "abstained"})
        self.assertIn("not appear to contain the answer", abst)

        ro = MainWindow._format_answer({"mode": "retrieval_only"})
        # The message must name the way to fix it, not just state the problem.
        self.assertIn("Set Up AI Answers", ro)


@unittest.skipUnless(HAS_QT, "PyQt6 + QScintilla not installed")
class TestTheme(unittest.TestCase):
    """QScintilla ignores the app palette, so every colour is set by hand."""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from raggy.app import MainWindow
        w = MainWindow()
        w._index_timer.stop()
        w._find_timer.stop()
        w.editor.setModified(False)
        return w

    def test_bgr_packing_is_blue_green_red(self):
        # Scintilla wants 0xBBGGRR. Getting this wrong swaps red and blue with
        # no error, so it is worth pinning.
        from PyQt6.QtGui import QColor
        from raggy.app import _bgr
        self.assertEqual(_bgr(QColor("#FF0000")), 0x0000FF)   # red   -> low byte
        self.assertEqual(_bgr(QColor("#0000FF")), 0xFF0000)   # blue  -> high byte
        self.assertEqual(_bgr(QColor("#FFFFFF")), 0xFFFFFF)

    def test_dark_mode_gives_a_bright_caret_on_a_dark_pane(self):
        from PyQt6 import Qsci
        w = self._window()
        w._is_dark = lambda: True
        w._apply_theme()
        caret = w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETCARETFORE)
        self.assertEqual(caret, 0xFFFFFF, "caret must be bright against a dark pane")
        self.assertLess(w.editor.paper().lightness(), 128, "pane should be dark")
        self.assertGreater(w.editor.color().lightness(), 128, "text should be light")
        w.close()

    def test_light_mode_gives_a_dark_caret_on_a_light_pane(self):
        from PyQt6 import Qsci
        w = self._window()
        w._is_dark = lambda: False
        w._apply_theme()
        caret = w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETCARETFORE)
        self.assertEqual(caret, 0x000000)
        self.assertGreater(w.editor.paper().lightness(), 128)
        w.close()

    def test_theme_change_does_not_break_existing_results(self):
        # Re-theming repaints indicators; it must not lose the search results.
        w = self._window()
        w.editor.setText("alpha beta gamma")
        w.engine.index("alpha beta gamma", "t.txt")
        w._indexed_text = "alpha beta gamma"
        w.show_find()
        w.find_bar.field.setText("beta")
        w._find_timer.stop()
        w._run_search()
        before = len(w._results)
        w._is_dark = lambda: True
        w._apply_theme()
        self.assertEqual(len(w._results), before)
        w.editor.setModified(False)
        w.close()


@unittest.skipUnless(HAS_QT, "PyQt6 + QScintilla not installed")
class TestEditorFeatures(unittest.TestCase):
    """Replace, zoom, go-to-line, transformations, recent files, AI setup."""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, text="alpha ERR_X beta ERR_X gamma\n"):
        from raggy.app import MainWindow
        w = MainWindow()
        # ⚠️ Zoom is persisted between launches on purpose, so a test that zooms
        # leaves the size behind for the next one. Start every test at the default.
        w.zoom_reset()
        w.editor.setText(text)
        w._offsets = None
        w.engine.index(text, "t")
        w._indexed_text = text
        w._index_timer.stop()
        w._find_timer.stop()
        w.editor.setModified(False)
        return w

    def _search(self, w, query):
        w.show_find()
        w.find_bar.field.setText(query)
        w._find_timer.stop()
        w._run_search()
        return w._results

    # ---- replace ----------------------------------------------------------
    def test_replace_all_replaces_every_exact_match(self):
        w = self._window()
        self._search(w, "ERR_X")
        w._do_replace("ERR_Y", True)
        self.assertEqual(w.editor.text(), "alpha ERR_Y beta ERR_Y gamma\n")
        w.editor.setModified(False)
        w.close()

    def test_replace_one_replaces_only_the_current_match(self):
        w = self._window()
        self._search(w, "ERR_X")
        w._do_replace("ERR_Y", False)
        self.assertEqual(w.editor.text().count("ERR_Y"), 1)
        self.assertEqual(w.editor.text().count("ERR_X"), 1)
        w.editor.setModified(False)
        w.close()

    def test_replace_is_refused_when_only_meaning_matches_exist(self):
        # ⚠️ A "related" passage is about the same subject, not an occurrence of
        # the query. Replacing it would rewrite text the user never searched for.
        w = self._window("the queue backed up and the worker stalled\n")
        res = self._search(w, "service fell over")
        self.assertTrue(res)
        self.assertTrue(all(r["kind"] == "related" for r in res))
        self.assertFalse(w.find_bar.replace_btn.isEnabled())
        before = w.editor.text()
        w._do_replace("XXX", True)
        self.assertEqual(w.editor.text(), before, "it replaced a meaning match!")
        w.editor.setModified(False)
        w.close()

    def test_replace_respects_case_sensitivity(self):
        w = self._window("Err_x and ERR_X here\n")
        w.find_bar.case_btn.setChecked(True)
        self._search(w, "ERR_X")
        w._do_replace("Z", True)
        self.assertEqual(w.editor.text(), "Err_x and Z here\n")
        w.editor.setModified(False)
        w.close()

    # ---- the offset regression -------------------------------------------
    def test_highlight_lands_on_the_right_text_with_multibyte_characters(self):
        # ⚠️ THE regression test. Char offsets != byte offsets once the document
        # has any non-ASCII character. Assert on the TEXT, never on the numbers.
        from PyQt6 import Qsci
        text = "INTRO — with an em dash.\nERR_CONN_4421 is the code.\n"
        w = self._window(text)
        res = self._search(w, "ERR_CONN_4421")
        self.assertTrue(res)
        S = Qsci.QsciScintilla
        b0 = w.editor.SendScintilla(S.SCI_GETSELECTIONSTART)
        b1 = w.editor.SendScintilla(S.SCI_GETSELECTIONEND)
        selected = text.encode("utf-8")[b0:b1].decode("utf-8")
        self.assertEqual(selected, "ERR_CONN_4421",
                         f"editor selected {selected!r} instead of the match")
        w.editor.setModified(False)
        w.close()

    def test_replace_is_correct_after_multibyte_characters(self):
        # Same hazard on the writing path, not just the reading one.
        w = self._window("café ☕ ERR_X and ERR_X\n")
        self._search(w, "ERR_X")
        w._do_replace("OK", True)
        self.assertEqual(w.editor.text(), "café ☕ OK and OK\n")
        w.editor.setModified(False)
        w.close()

    # ---- zoom -------------------------------------------------------------
    def test_zoom_in_out_and_reset(self):
        w = self._window()
        base = w.editor.font().pointSize()
        w.zoom_in()
        self.assertEqual(w.editor.font().pointSize(), base + 1)
        w.zoom_out()
        w.zoom_out()
        self.assertEqual(w.editor.font().pointSize(), base - 1)
        w.zoom_reset()
        self.assertEqual(w.editor.font().pointSize(), w._base_point_size())
        w.editor.setModified(False)
        w.close()

    def test_zoom_is_clamped(self):
        w = self._window()
        for _ in range(200):
            w.zoom_in()
        self.assertLessEqual(w.editor.font().pointSize(), 72)
        w.editor.setModified(False)
        w.close()

    # ---- go to line -------------------------------------------------------
    def test_go_to_line_moves_the_caret(self):
        from PyQt6 import Qsci
        w = self._window("one\ntwo\nthree\nfour\n")
        with mock.patch("raggy.app.QInputDialog.getInt", return_value=(3, True)):
            w.go_to_line()
        pos = w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETCURRENTPOS)
        line = w.editor.SendScintilla(Qsci.QsciScintilla.SCI_LINEFROMPOSITION, pos)
        self.assertEqual(line, 2)              # 0-based: the third line
        w.editor.setModified(False)
        w.close()

    def test_go_to_line_cancelled_changes_nothing(self):
        from PyQt6 import Qsci
        w = self._window("one\ntwo\n")
        w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GOTOLINE, 0)
        before = w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETCURRENTPOS)
        with mock.patch("raggy.app.QInputDialog.getInt", return_value=(2, False)):
            w.go_to_line()
        self.assertEqual(w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETCURRENTPOS),
                         before)
        w.editor.setModified(False)
        w.close()

    # ---- transformations --------------------------------------------------
    def test_transformations_on_a_selection(self):
        from PyQt6 import Qsci
        w = self._window("hello world")
        w.editor.SendScintilla(Qsci.QsciScintilla.SCI_SETSEL, 0, 5)
        w.transform_case("upper")
        self.assertEqual(w.editor.text(), "HELLO world")
        w.editor.SendScintilla(Qsci.QsciScintilla.SCI_SETSEL, 0, 5)
        w.transform_case("lower")
        self.assertEqual(w.editor.text(), "hello world")
        w.editor.setModified(False)
        w.close()

    def test_transformation_with_no_selection_applies_to_the_whole_document(self):
        from PyQt6 import Qsci
        w = self._window("hello world")
        w.editor.SendScintilla(Qsci.QsciScintilla.SCI_SETSEL, 0, 0)
        w.transform_case("upper")
        self.assertEqual(w.editor.text(), "HELLO WORLD")
        w.editor.setModified(False)
        w.close()

    # ---- recent files -----------------------------------------------------
    def test_recent_menu_remembers_dedupes_and_clears(self):
        w = self._window()
        w._settings.setValue("recent", [])
        w._remember_recent("/tmp/a.txt")
        w._remember_recent("/tmp/b.txt")
        w._remember_recent("/tmp/a.txt")            # deduped, moved to the front
        self.assertEqual(w._recent_paths(), ["/tmp/a.txt", "/tmp/b.txt"])
        w._fill_recent_menu()
        labels = [a.text() for a in w.recent_menu.actions() if a.text()]
        self.assertTrue(any("a.txt" in x for x in labels))
        self.assertIn("Clear Menu", labels)
        w.editor.setModified(False)
        w.close()

    # ---- AI setup ---------------------------------------------------------
    def test_ask_offers_setup_only_when_no_model_is_connected(self):
        from raggy.aiconfig import AIConfig
        from raggy.app import AskDialog
        d = AskDialog()
        d.set_ai_configured(False)
        self.assertFalse(d.setup_btn.isHidden(), "no discovery path for setup")
        d.set_ai_configured(True, "llama3.2 at http://localhost:11434/v1")
        self.assertTrue(d.setup_btn.isHidden())
        _ = AIConfig
        d.close()

    def test_ask_dialog_explains_the_limit_when_unconfigured(self):
        from raggy.app import AskDialog
        d = AskDialog()
        d.set_ai_configured(False)
        self.assertIn("no model", d.status.text().lower())
        d.close()

    def test_ai_setup_dialog_presets_fill_in_and_know_about_local_models(self):
        from raggy.aiconfig import AIConfig
        from raggy.app import AISetupDialog
        d = AISetupDialog(AIConfig(provider="deepseek"))
        self.assertEqual(d.provider.count(), 5)
        idx = d.provider.findData("ollama")
        d.provider.setCurrentIndex(idx)
        self.assertIn("localhost", d.base_url.text())
        self.assertIn("no key is needed", d.key_note.text().lower())
        self.assertFalse(d._current().needs_key)
        d.close()

    def test_ai_setup_dialog_refuses_an_incomplete_cloud_setup(self):
        from raggy.aiconfig import AIConfig, config_file
        from raggy.app import AISetupDialog
        AIConfig().clear()
        d = AISetupDialog(AIConfig(provider="deepseek"))
        d.provider.setCurrentIndex(d.provider.findData("custom"))
        d.base_url.setText("")
        d.model.setText("")
        d._save()
        self.assertFalse(config_file().exists(), "it saved an unusable config")
        # `result()` must still be QDialog's method, not a shadowed attribute.
        self.assertEqual(d.result(), 0)
        d.base_url.setText("https://api.example.com/v1")
        d.model.setText("m")
        d.api_key.setText("")
        d._save()
        self.assertFalse(config_file().exists(), "it saved without the required key")
        d.close()

    def test_ai_setup_dialog_saves_a_local_model_without_a_key(self):
        from raggy.aiconfig import AIConfig, config_file
        from raggy.app import AISetupDialog
        AIConfig().clear()
        d = AISetupDialog(AIConfig(provider="ollama"))
        saved = []
        d.saved.connect(lambda: saved.append(True))
        d._save()
        self.assertTrue(saved, "a local model should save with no key")
        self.assertTrue(config_file().exists())
        self.assertEqual(AIConfig.load().model, "llama3.2")
        self.assertTrue(AIConfig.load().configured)
        AIConfig().clear()
        d.close()

    def test_retrieval_only_message_points_at_the_setup(self):
        from raggy.app import MainWindow
        text = MainWindow._format_answer({"mode": "retrieval_only"})
        self.assertIn("Set Up AI Answers", text)


if __name__ == "__main__":
    unittest.main()
