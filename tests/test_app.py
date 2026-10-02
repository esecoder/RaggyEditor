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

# ⚠️ Keep the app's settings out of the developer's real preferences.
# QSettings.setPath()/setDefaultFormat() DO NOT WORK for this on macOS: Qt still
# resolves QSettings("Org", "App") to NativeFormat and writes to
# ~/Library/Preferences. A test run once left `font_size = 72` there, which made
# the next print job come out 28 pages long. The app therefore honours an explicit
# path override, and that is what these tests set.
os.environ["RAGGY_SETTINGS_DIR"] = tempfile.mkdtemp(prefix="raggy-test-cfg-")
# Recovery snapshots go to a scratch directory too, for the same reason.
os.environ["RAGGY_RECOVERY_DIR"] = tempfile.mkdtemp(prefix="raggy-test-rec-")

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
        self.assertEqual(menus, ["File", "Edit", "Find", "View", "Format", "Help"])
        # ⚠️ "Model" was a menu of encoder choices (auto/onnx/neural/lsa). It must
        # never come back: those are words about the implementation, not the
        # document. "View" is fine — it holds only zoom.
        self.assertNotIn("Model", menus)
        view = next(a.menu() for a in w.menuBar().actions()
                    if a.text().replace("&", "") == "View")
        labels = [x.text().replace("&", "") for x in view.actions() if x.text()]
        self.assertEqual(labels, ["Zoom In", "Zoom Out", "Actual Size",
                                  "Show Formula Preview", "Appearance"])
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
        w._mark_clean()      # clears BOTH dirty flags
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
        w._mark_clean()      # clears BOTH dirty flags
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
        w._mark_clean()      # clears BOTH dirty flags
        w.close()

    def test_replace_one_replaces_only_the_current_match(self):
        w = self._window()
        self._search(w, "ERR_X")
        w._do_replace("ERR_Y", False)
        self.assertEqual(w.editor.text().count("ERR_Y"), 1)
        self.assertEqual(w.editor.text().count("ERR_X"), 1)
        w._mark_clean()      # clears BOTH dirty flags
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
        w._mark_clean()      # clears BOTH dirty flags
        w.close()

    def test_replace_respects_case_sensitivity(self):
        w = self._window("Err_x and ERR_X here\n")
        w.find_bar.case_btn.setChecked(True)
        self._search(w, "ERR_X")
        w._do_replace("Z", True)
        self.assertEqual(w.editor.text(), "Err_x and Z here\n")
        w._mark_clean()      # clears BOTH dirty flags
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
        w._mark_clean()      # clears BOTH dirty flags
        w.close()

    def test_replace_is_correct_after_multibyte_characters(self):
        # Same hazard on the writing path, not just the reading one.
        w = self._window("café ☕ ERR_X and ERR_X\n")
        self._search(w, "ERR_X")
        w._do_replace("OK", True)
        self.assertEqual(w.editor.text(), "café ☕ OK and OK\n")
        w._mark_clean()      # clears BOTH dirty flags
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
        w._mark_clean()      # clears BOTH dirty flags
        w.close()

    def test_zoom_is_clamped(self):
        w = self._window()
        for _ in range(200):
            w.zoom_in()
        self.assertLessEqual(w.editor.font().pointSize(), 72)
        w._mark_clean()      # clears BOTH dirty flags
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
        w._mark_clean()      # clears BOTH dirty flags
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
        w._mark_clean()      # clears BOTH dirty flags
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
        w._mark_clean()      # clears BOTH dirty flags
        w.close()

    def test_transformation_with_no_selection_applies_to_the_whole_document(self):
        from PyQt6 import Qsci
        w = self._window("hello world")
        w.editor.SendScintilla(Qsci.QsciScintilla.SCI_SETSEL, 0, 0)
        w.transform_case("upper")
        self.assertEqual(w.editor.text(), "HELLO WORLD")
        w._mark_clean()      # clears BOTH dirty flags
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
        w._mark_clean()      # clears BOTH dirty flags
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

    # ---- printing ---------------------------------------------------------
    def test_printed_row_matches_the_font_size_in_use(self):
        # ⚠️ REGRESSION GUARD: the printer must use the EDITOR's font. If it falls
        # back to a default while the editor has been zoomed, the printed page and
        # the window disagree — which is exactly how a huge font produced 28 pages
        # for a 3-page document and nobody noticed.
        from PyQt6.QtGui import QPageSize, QPdfWriter
        # Long enough to paginate, so the two font sizes give different counts.
        w = self._window("\n".join(f"line {i}" for i in range(1, 401)))
        for _ in range(8):
            w.zoom_in()
        big = w.editor.font().pointSize()
        self.assertGreater(big, w._base_point_size())

        import tempfile
        path = tempfile.mktemp(suffix=".pdf")
        writer = QPdfWriter(path)
        writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        pages_big = w._render_to_device(writer)

        w.zoom_reset()
        path2 = tempfile.mktemp(suffix=".pdf")
        writer2 = QPdfWriter(path2)
        writer2.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        pages_small = w._render_to_device(writer2)

        self.assertGreater(pages_big, pages_small,
                           "a bigger font must take more pages")
        w._mark_clean()      # clears BOTH dirty flags
        w.close()

    def test_export_writes_a_valid_pdf(self):
        from PyQt6.QtGui import QPageSize, QPdfWriter
        w = self._window("\n".join(f"line {i}" for i in range(1, 401)))
        import tempfile
        path = tempfile.mktemp(suffix=".pdf")
        writer = QPdfWriter(path)
        writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        pages = w._render_to_device(writer)
        self.assertGreaterEqual(pages, 1)
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertTrue(data.startswith(b"%PDF"), "not a PDF")
        self.assertGreater(len(data), 1000)
        w._mark_clean()      # clears BOTH dirty flags
        w.close()

    def test_wrapping_off_keeps_printed_line_numbers_identical(self):
        # A printed "line 61" must be line 61 in the window.
        from PyQt6 import Qsci
        from PyQt6.QtGui import QFontMetrics
        from raggy.printing import rows_for_document
        lines = [f"line {i}" for i in range(1, 201)]
        w = self._window("\n".join(lines))
        w.editor.setWrapMode(Qsci.QsciScintilla.WrapMode.WrapNone)
        self.assertFalse(w._editor_wraps())
        fm = QFontMetrics(w.editor.font())
        rows = rows_for_document(w.editor.text(), fm.horizontalAdvance,
                                 2000, wrap=w._editor_wraps())
        self.assertEqual(rows, lines)
        self.assertEqual(rows[60], "line 61")
        w._mark_clean()      # clears BOTH dirty flags
        w.close()


@unittest.skipUnless(HAS_QT, "PyQt6 + QScintilla not installed")
class TestSettingsIsolation(unittest.TestCase):
    """The suite must not touch the developer's real preferences.

    ⚠️ This is not hypothetical. An earlier version relied on
    `QSettings.setPath()`, which Qt IGNORES on macOS, so a test that zoomed wrote
    `font_size = 72` into ~/Library/Preferences. The next print job then came out
    28 pages long and the app opened with enormous text.
    """

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_settings_resolve_inside_the_override_directory(self):
        from raggy.app import SETTINGS_DIR_ENV, app_settings
        override = os.environ.get(SETTINGS_DIR_ENV)
        self.assertTrue(override, "the suite must set the override")
        self.assertTrue(app_settings().fileName().startswith(override),
                        f"settings leaked to {app_settings().fileName()}")

    def test_zoom_does_not_leak_out_of_the_override(self):
        from raggy.app import app_settings
        from raggy.app import MainWindow
        w = MainWindow()
        w._index_timer.stop()
        w._find_timer.stop()
        w.zoom_in()
        app_settings().sync()
        self.assertTrue(app_settings().fileName().startswith(
            os.environ["RAGGY_SETTINGS_DIR"]))
        w.zoom_reset()
        w._mark_clean()      # clears BOTH dirty flags
        w.close()




@unittest.skipUnless(HAS_QT, "PyQt6 + QScintilla not installed")
class TestDocumentIntegrity(unittest.TestCase):
    """Opening and saving must not change the file. See raggy.textfile."""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _window(self):
        from raggy.app import MainWindow
        w = MainWindow()
        w._index_timer.stop()
        w._find_timer.stop()
        w._save_timer.stop()
        w.editor.setModified(False)
        return w

    def _file(self, name, raw: bytes) -> str:
        path = pathlib.Path(self.tmp.name, name)
        path.write_bytes(raw)
        return str(path)

    # ---- the bug that started this ----
    def test_a_crlf_file_keeps_its_line_endings_after_saving(self):
        path = self._file("win.txt", b"one\r\ntwo\r\n")
        w = self._window()
        w.load_path(path, warn_encoding=False)
        w.editor.setText(w.editor.text() + "three\n")
        w.editor.setModified(True)
        self.assertTrue(w.save_file())
        self.assertEqual(pathlib.Path(path).read_bytes(), b"one\r\ntwo\r\nthree\r\n")
        w.close()

    def test_a_latin1_file_is_not_silently_converted_to_utf8(self):
        path = self._file("latin.txt", "caf\u00e9\n".encode("latin-1"))
        w = self._window()
        w.load_path(path, warn_encoding=False)
        self.assertEqual(w.doc_format.encoding, "latin-1")
        w.editor.setModified(True)
        w.save_file()
        self.assertIn(b"caf\xe9", pathlib.Path(path).read_bytes())
        self.assertNotIn(b"caf\xc3\xa9", pathlib.Path(path).read_bytes())
        w.close()

    def test_an_untouched_file_survives_open_and_save_byte_for_byte(self):
        for name, raw in {
                "unix.txt": b"a\nb\n",
                "win.txt": b"a\r\nb\r\n",
                "bom.txt": b"\xef\xbb\xbfa\nb\n",
                "latin.txt": "caf\u00e9\r\n".encode("latin-1"),
        }.items():
            with self.subTest(name=name):
                path = self._file(name, raw)
                w = self._window()
                w.load_path(path, warn_encoding=False)
                w.editor.setModified(True)      # a save the user asked for
                self.assertTrue(w.save_file())
                self.assertEqual(pathlib.Path(path).read_bytes(), raw)
                w.close()

    def test_the_editor_buffer_never_holds_a_carriage_return(self):
        path = self._file("win.txt", b"a\r\nb\r\n")
        w = self._window()
        w.load_path(path, warn_encoding=False)
        self.assertNotIn("\r", w.editor.text())
        w.close()

    # ---- the menu must tell the truth ----
    def test_the_line_ending_menu_follows_the_opened_file(self):
        path = self._file("win.txt", b"a\r\nb\r\n")
        w = self._window()
        w.load_path(path, warn_encoding=False)
        checked = [k for k, a in w._eol_actions.items() if a.isChecked()]
        self.assertEqual(checked, ["CRLF"], "the menu did not follow the document")
        w.close()

    def test_new_resets_the_format_instead_of_inheriting_it(self):
        # Otherwise "New" after opening a Windows file makes another Windows file.
        path = self._file("win.txt", b"a\r\nb\r\n")
        w = self._window()
        w.load_path(path, warn_encoding=False)
        w.editor.setModified(False)
        w.new_file()
        self.assertEqual(w.doc_format.newline_name, "LF")
        self.assertEqual([k for k, a in w._eol_actions.items() if a.isChecked()],
                         ["LF"])
        w.close()

    def test_converting_line_endings_is_a_deliberate_edit(self):
        path = self._file("unix.txt", b"a\nb\n")
        w = self._window()
        w.load_path(path, warn_encoding=False)
        self.assertFalse(w.editor.isModified())
        w.set_line_ending("\r\n")
        # ⚠️ `_is_dirty()`, not `editor.isModified()`: converting line endings does
        # not touch the buffer, and QScintilla's setModified(True) does nothing.
        self.assertTrue(w._is_dirty(), "a format change must look unsaved")
        self.assertTrue(w.windowTitle())     # and the title keeps its [*] marker
        w.save_file()
        self.assertEqual(pathlib.Path(path).read_bytes(), b"a\r\nb\r\n")
        w.close()

    def test_a_non_utf8_file_is_reported(self):
        path = self._file("latin.txt", "caf\u00e9\n".encode("latin-1"))
        w = self._window()
        with mock.patch("raggy.app.QMessageBox.information") as info:
            w.load_path(path)
        self.assertTrue(info.called, "the user was not told about the encoding")
        w.close()

    def test_a_utf8_file_is_not_reported(self):
        path = self._file("plain.txt", b"hello\n")
        w = self._window()
        with mock.patch("raggy.app.QMessageBox.information") as info:
            w.load_path(path)
        self.assertFalse(info.called, "an ordinary file should open silently")
        w.close()

    def test_revert_to_saved_restores_the_file_on_disk(self):
        path = self._file("doc.txt", b"original\n")
        w = self._window()
        w.load_path(path, warn_encoding=False)
        w.editor.setText("changed")
        w.editor.setModified(True)
        with mock.patch("raggy.app.QMessageBox.question",
                        return_value=__import__("PyQt6.QtWidgets",
                                                fromlist=["QMessageBox"])
                        .QMessageBox.StandardButton.Discard):
            w.revert_to_saved()
        self.assertEqual(w.editor.text(), "original\n")
        self.assertFalse(w.editor.isModified())
        w.close()


@unittest.skipUnless(HAS_QT, "PyQt6 + QScintilla not installed")
class TestAutosave(unittest.TestCase):
    """Unsaved work must survive a crash, and must not be offered when stale."""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from raggy import recovery
        recovery.clear_all()
        self.addCleanup(recovery.clear_all)

    def _window(self, text="some text"):
        from raggy.app import MainWindow
        w = MainWindow()
        w._index_timer.stop()
        w._find_timer.stop()
        w._save_timer.stop()
        w.editor.setText(text)
        w.editor.setModified(True)
        return w

    def test_a_snapshot_is_written_while_there_are_unsaved_changes(self):
        from raggy import recovery
        w = self._window("work in progress")
        w._snapshot()
        pending = recovery.list_all()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].text, "work in progress")
        w._mark_clean()      # clears BOTH dirty flags
        w.close()

    def test_nothing_is_written_when_the_document_is_saved(self):
        from raggy import recovery
        w = self._window("clean")
        w.editor.setModified(False)
        w._snapshot()
        self.assertEqual(recovery.list_all(), [])
        w.close()

    def test_an_empty_document_is_not_snapshotted(self):
        from raggy import recovery
        w = self._window("   \n  ")
        w._snapshot()
        self.assertEqual(recovery.list_all(), [])
        w._mark_clean()      # clears BOTH dirty flags
        w.close()

    def test_saving_forgets_the_snapshot(self):
        from raggy import recovery
        path = pathlib.Path(tempfile.mkdtemp(), "doc.txt")
        path.write_text("v1", encoding="utf-8")
        w = self._window("v2 unsaved")
        w.path = str(path)
        w._snapshot()
        self.assertEqual(len(recovery.list_all()), 1)
        w.save_file()
        self.assertEqual(recovery.list_all(), [])
        w.close()

    def test_editing_starts_the_autosave_timer(self):
        w = self._window("x")
        w._save_timer.stop()
        w.editor.setText("typed")
        self.assertTrue(w._save_timer.isActive())
        w._mark_clean()      # clears BOTH dirty flags
        w.close()

    def test_the_timer_stops_once_there_is_nothing_to_lose(self):
        w = self._window("x")
        w.editor.setModified(False)
        w._snapshot()
        self.assertFalse(w._save_timer.isActive())
        w.close()

    def test_a_snapshot_restores_the_text_and_the_format(self):
        w = self._window("recovered body")
        w.editor.setModified(False)       # new_file() would otherwise prompt to save
        w.new_file()                      # back to a clean LF document
        snap = __import__("raggy.recovery", fromlist=["x"]).Snapshot(
            slot="s", text="recovered body", path=None, newline="\r\n")
        w.restore_snapshot(snap)
        self.assertEqual(w.editor.text(), "recovered body")
        self.assertEqual(w.doc_format.newline_name, "CRLF")
        self.assertTrue(w.editor.isModified(), "a restored document is unsaved")
        w._mark_clean()      # clears BOTH dirty flags
        w.close()


@unittest.skipUnless(HAS_QT, "PyQt6 + QScintilla not installed")
class TestTierTwo(unittest.TestCase):
    """Whole-word, jump to selection, font, geometry, drag and drop."""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, text="cat category cat\n"):
        from raggy.app import MainWindow
        w = MainWindow()
        w.zoom_reset()
        w._index_timer.stop()
        w._find_timer.stop()
        w._save_timer.stop()
        w.editor.setText(text)
        w._offsets = None
        w.editor.setModified(False)
        return w

    def _matches(self, w, query):
        w.show_find()
        w.find_bar.field.setText(query)
        w._find_timer.stop()
        w._run_search()
        return [r for r in w._results if r["kind"] == "match"]

    def test_whole_word_excludes_substrings(self):
        w = self._window()
        loose = self._matches(w, "cat")
        w.find_bar.word_btn.setChecked(True)
        tight = self._matches(w, "cat")
        self.assertEqual(len(loose), 3)
        self.assertEqual(len(tight), 2)
        w.close()

    def test_whole_word_does_not_break_on_punctuation(self):
        # \b would fail here: there is no word boundary after ")".
        w = self._window("x (beta) y beta z\n")
        w.find_bar.word_btn.setChecked(True)
        self.assertEqual(len(self._matches(w, "(beta)")), 1)
        w.close()

    def test_jump_to_selection_scrolls_without_moving_the_selection(self):
        from PyQt6 import Qsci
        w = self._window("line\n" * 400)
        w.show()
        start, end = w._sel_chars()
        w._select_chars(2000, 2004)
        before = (w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETSELECTIONSTART),
                  w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETSELECTIONEND))
        w.jump_to_selection()
        after = (w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETSELECTIONSTART),
                 w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETSELECTIONEND))
        self.assertEqual(before, after, "jumping moved the selection")
        visible = w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETFIRSTVISIBLELINE)
        self.assertGreater(visible, 0, "it did not scroll")
        _ = (start, end)
        w.close()

    def test_jump_with_no_selection_does_nothing(self):
        w = self._window("a\nb\n")
        w._select_chars(0, 0)
        w.jump_to_selection()               # must not raise
        w.close()

    def test_the_font_menu_sets_the_family_and_leaves_zoom_alone(self):
        from PyQt6.QtGui import QFont
        w = self._window()
        w.zoom_in()
        size_before = w.editor.font().pointSize()
        with mock.patch("PyQt6.QtWidgets.QFontDialog.getFont",
                        return_value=(QFont("Courier New", 40), True)):
            w.choose_font()
        self.assertEqual(w.editor.font().family(), "Courier New")
        self.assertEqual(w.editor.font().pointSize(), size_before,
                         "the dialog's size leaked into zoom")
        self.assertIn("Courier New", w._font_action.text())
        w.close()

    def test_cancelling_the_font_dialog_changes_nothing(self):
        from PyQt6.QtGui import QFont
        w = self._window()
        before = w.editor.font().family()
        with mock.patch("PyQt6.QtWidgets.QFontDialog.getFont",
                        return_value=(QFont("Courier New", 40), False)):
            w.choose_font()
        self.assertEqual(w.editor.font().family(), before)
        w.close()

    def test_the_window_geometry_is_remembered(self):
        # ⚠️ Compare two RESTORED windows against each other, not a restored one
        # against the window that saved. A programmatic resize may exceed the
        # (offscreen) screen, while restoreGeometry clamps to it — comparing
        # across would fail on platform clamping rather than on the feature.
        from PyQt6.QtCore import QSize
        w = self._window()
        w.resize(820, 600)
        w._save_geometry()
        self.assertTrue(w._settings.value("geometry"), "nothing was stored")

        w2 = self._window()
        w2._restore_geometry()
        w3 = self._window()
        w3._restore_geometry()
        self.assertEqual(w2.size(), w3.size(), "geometry was not remembered")
        self.assertNotEqual(w2.size(), QSize(980, 720),
                            "fell back to the default size")
        w._mark_clean()
        w.close()
        w2.close()
        w3.close()

    def test_dropping_a_file_loads_it(self):
        from PyQt6.QtCore import QMimeData, QPointF, QUrl, Qt
        from PyQt6.QtGui import QDropEvent
        path = pathlib.Path(tempfile.mkdtemp(), "dropped.txt")
        path.write_text("dropped content\n", encoding="utf-8")
        w = self._window("original")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        event = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        w.dropEvent(event)
        self.assertEqual(w.editor.text(), "dropped content\n")
        w._mark_clean()      # clears BOTH dirty flags
        w.close()

    def test_the_window_accepts_drops(self):
        w = self._window()
        self.assertTrue(w.acceptDrops())
        w.close()


@unittest.skipUnless(HAS_QT, "PyQt6 + QScintilla not installed")
class TestRecoveryPrompt(unittest.TestCase):
    """The startup offer: only for work that is genuinely newer than disk."""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from raggy import recovery
        recovery.clear_all()
        self.addCleanup(recovery.clear_all)

    def _stale_free_snapshot(self, slot="s", text="recovered text"):
        from raggy import recovery
        snap = recovery.Snapshot(slot=slot, text=text, path=None)
        recovery.save(snap)
        return snap

    def test_nothing_pending_means_no_prompt(self):
        from raggy.app import MainWindow, _offer_recovery
        w = MainWindow()
        w._index_timer.stop()
        w._save_timer.stop()
        with mock.patch("raggy.app.QMessageBox.question") as q:
            _offer_recovery(w)
        self.assertFalse(q.called)
        w.close()

    def test_yes_restores_the_most_recent_snapshot(self):
        from PyQt6.QtWidgets import QMessageBox
        from raggy.app import MainWindow, _offer_recovery
        self._stale_free_snapshot(text="recovered text")
        w = MainWindow()
        w._index_timer.stop()
        w._save_timer.stop()
        w.editor.setModified(False)
        with mock.patch("raggy.app.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes):
            _offer_recovery(w)
        self.assertEqual(w.editor.text(), "recovered text")
        w._mark_clean()      # clears BOTH dirty flags
        w.close()

    def test_discard_clears_everything(self):
        from PyQt6.QtWidgets import QMessageBox
        from raggy import recovery
        from raggy.app import MainWindow, _offer_recovery
        self._stale_free_snapshot(text="recovered text")
        w = MainWindow()
        w._index_timer.stop()
        w._save_timer.stop()
        w.editor.setModified(False)
        with mock.patch("raggy.app.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Discard):
            _offer_recovery(w)
        self.assertEqual(recovery.list_all(), [])
        w.close()


class TestBundleDeclaresDocumentTypes(unittest.TestCase):
    """A downloadable editor must be able to open a .txt by double-clicking.

    Without CFBundleDocumentTypes macOS does not know the app handles text, so the
    file simply opens in something else and RaggyEditor is missing from Finder's
    "Open With". There is no runtime symptom to test, so the spec is inspected.
    """

    def test_the_spec_declares_plain_text(self):
        spec = (pathlib.Path(__file__).resolve().parents[1]
                / "packaging" / "raggyeditor.spec").read_text(encoding="utf-8")
        self.assertIn("CFBundleDocumentTypes", spec)
        self.assertIn("public.plain-text", spec)
        for ext in ("txt", "md", "log"):
            self.assertIn(ext, spec)




@unittest.skipUnless(HAS_QT, "PyQt6 + QScintilla not installed")
class TestWindows(unittest.TestCase):
    """One window per document, the way TextEdit works.

    ⚠️ `windows` is a process-wide registry, so every test here restores it.
    Leaking a window would change the behaviour of whatever runs next.
    """

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from raggy.app import windows
        self._saved = list(windows.windows)
        self._ever = windows.ever_opened
        windows.windows[:] = []
        windows.ever_opened = False
        self.addCleanup(self._restore, windows)

    def _restore(self, windows):
        for w in list(windows.windows):
            w._mark_clean()
            w.close()
        windows.windows[:] = self._saved
        windows.ever_opened = self._ever

    def _clean(self, w):
        w._index_timer.stop()
        w._find_timer.stop()
        w._save_timer.stop()
        return w

    # ---- the registry -----------------------------------------------------
    def test_open_registers_shows_and_marks_the_app_as_started(self):
        from raggy.app import windows
        self.assertEqual(windows.count(), 0)
        w = windows.open()
        self._clean(w)
        self.assertEqual(windows.count(), 1)
        self.assertTrue(windows.ever_opened)
        self.assertFalse(w.isHidden())

    def test_forget_removes_it(self):
        from raggy.app import windows
        w = self._clean(windows.open())
        windows.forget(w)
        self.assertEqual(windows.count(), 0)

    def test_forgetting_an_unknown_window_is_harmless(self):
        from raggy.app import MainWindow, windows
        w = self._clean(MainWindow())
        windows.forget(w)                       # never registered
        self.assertEqual(windows.count(), 0)
        w._mark_clean()
        w.close()

    def test_the_cascade_rule_counts_open_windows(self):
        # The rule itself, with no window manager involved: first window takes the
        # remembered geometry, each later one steps once. <br>The platform adjusts
        # positions when a window is shown, so the rule is pinned here rather than
        # by reading back screen coordinates.
        from raggy.app import WindowRegistry
        registry = WindowRegistry()
        steps = []
        for _ in range(3):
            steps.append(registry._cascade())
            registry.windows.append(object())          # stand-in for a window
        self.assertEqual(steps, [0, 1, 2])

    def test_the_cascade_wraps_instead_of_marching_off_screen(self):
        from raggy.app import WindowRegistry
        registry = WindowRegistry()
        registry.windows.extend(object() for _ in range(WindowRegistry.CASCADE_WRAP))
        self.assertEqual(registry._cascade(), 0)

    def test_new_windows_do_not_land_on_top_of_each_other(self):
        # ⚠️ The observable property. Every window restores the same remembered
        # rectangle, so without the cascade the user would see exactly one window.
        from raggy.app import windows
        for _ in range(3):
            self._clean(windows.open())
        xs = [w.pos().x() for w in windows.windows]
        self.assertEqual(len(set(xs)), 3, f"windows stacked on top of each other: {xs}")
        self.assertEqual(xs, sorted(xs), f"the cascade did not step down-right: {xs}")

    # ---- is_pristine ------------------------------------------------------
    def test_a_fresh_window_is_pristine(self):
        from raggy.app import MainWindow
        w = self._clean(MainWindow())
        self.assertTrue(w.is_pristine())
        w._mark_clean()
        w.close()

    def test_a_window_with_a_path_is_not_pristine(self):
        from raggy.app import MainWindow
        w = self._clean(MainWindow())
        w.path = "/tmp/whatever.txt"
        self.assertFalse(w.is_pristine())
        w._mark_clean()
        w.close()

    def test_an_emptied_window_is_not_pristine(self):
        # ⚠️ Typing and then deleting everything leaves an EMPTY window that is
        # still the user's document. Reusing it would silently discard it.
        from raggy.app import MainWindow
        w = self._clean(MainWindow())
        w.editor.setText("typed")
        w.editor.setText("")
        self.assertEqual(w.editor.text(), "")
        self.assertFalse(w.is_pristine(), "an emptied document was treated as unused")
        w._mark_clean()
        w.close()

    def test_a_window_with_only_a_format_change_is_not_pristine(self):
        # `_is_dirty()` catches changes QScintilla's own flag misses.
        from raggy.app import MainWindow
        w = self._clean(MainWindow())
        w.set_line_ending("\r\n")
        self.assertFalse(w.is_pristine())
        w._mark_clean()
        w.close()

    # ---- opening documents ------------------------------------------------
    def test_open_document_reuses_an_empty_window(self):
        from raggy.app import windows
        tmp = tempfile.mkdtemp()
        path = pathlib.Path(tmp, "a.txt")
        path.write_text("document A\n", encoding="utf-8")

        w = self._clean(windows.open())
        target = self._clean(windows.open_document(str(path)))
        self.assertIs(target, w, "it left a stranded empty window")
        self.assertEqual(w.editor.text(), "document A\n")
        self.assertEqual(windows.count(), 1)

    def test_open_document_does_not_hijack_a_document(self):
        from raggy.app import windows
        tmp = tempfile.mkdtemp()
        path = pathlib.Path(tmp, "b.txt")
        path.write_text("document B\n", encoding="utf-8")

        w = self._clean(windows.open())
        w.editor.setText("work in progress")
        target = self._clean(windows.open_document(str(path)))
        self.assertIsNot(target, w)
        self.assertEqual(w.editor.text(), "work in progress",
                         "opening a file disturbed another document")
        self.assertEqual(windows.count(), 2)

    # ---- File > New -------------------------------------------------------
    def test_new_window_leaves_the_current_document_alone(self):
        # ⚠️ THE behaviour change: New used to reset this window, which threw away
        # whatever was open.
        from raggy.app import windows
        w = self._clean(windows.open())
        w.editor.setText("precious unsaved text")

        w2 = self._clean(w.new_window())
        self.assertEqual(w.editor.text(), "precious unsaved text")
        self.assertEqual(w2.editor.text(), "")
        self.assertEqual(windows.count(), 2)
        self.assertIsNot(w2, w)

    def test_the_file_menu_new_opens_a_window_rather_than_resetting(self):
        from raggy.app import windows
        w = self._clean(windows.open())
        w.editor.setText("keep me")
        file_menu = next(a.menu() for a in w.menuBar().actions()
                         if a.text().replace("&", "") == "File")
        new_action = next(a for a in file_menu.actions()
                          if a.text().replace("&", "") == "New")
        new_action.trigger()
        self.assertEqual(windows.count(), 2)
        self.assertEqual(w.editor.text(), "keep me")

    # ---- lifecycle --------------------------------------------------------
    def test_reopen_only_when_the_app_has_had_windows_and_has_none(self):
        from raggy.app import should_reopen_window, windows
        self.assertFalse(should_reopen_window(), "startup would open two windows")
        w = self._clean(windows.open())
        self.assertFalse(should_reopen_window(), "a window is already open")
        windows.forget(w)
        self.assertTrue(should_reopen_window(), "clicking the Dock icon did nothing")
        self._clean(windows.open())
        self.assertFalse(should_reopen_window())

    def test_close_all_leaves_nothing_open(self):
        from raggy.app import windows
        self._clean(windows.open())
        self._clean(windows.open())
        windows.close_all()
        self.assertEqual(windows.count(), 0)


@unittest.skipUnless(HAS_QT, "PyQt6 + QScintilla not installed")
class TestPrintPreview(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from raggy.app import MainWindow
        w = MainWindow()
        w._index_timer.stop()
        w._find_timer.stop()
        w._save_timer.stop()
        w.editor.setModified(False)
        return w

    def test_the_file_menu_offers_print_preview(self):
        w = self._window()
        file_menu = next(a.menu() for a in w.menuBar().actions()
                         if a.text().replace("&", "") == "File")
        labels = [a.text().replace("&", "") for a in file_menu.actions() if a.text()]
        self.assertIn("Print Preview…", labels)
        self.assertIn("Print…", labels)
        w.close()

    def test_preview_is_wired_to_the_same_renderer_as_print(self):
        # ⚠️ The point of the preview is that it shows what Print will produce, so
        # it must call the SAME layout code, not a second implementation.
        from unittest import mock as _mock
        seen = {}

        class FakePreview:
            class _Signal:
                def __init__(self, sink):
                    self._sink = sink

                def connect(self, fn):
                    self._sink["handler"] = fn

            def __init__(self, printer, parent):
                seen["printer"] = printer
                self.paintRequested = self._Signal(seen)

            def setWindowTitle(self, title):
                seen["title"] = title

            def exec(self):
                seen["exec"] = True
                return 0

        w = self._window()
        with _mock.patch("PyQt6.QtPrintSupport.QPrintPreviewDialog", FakePreview):
            w.print_preview()
        self.assertTrue(seen.get("exec"))
        self.assertEqual(seen.get("title"), "Print Preview")
        self.assertEqual(seen.get("handler"), w._render_to_device)
        w.close()


@unittest.skipUnless(HAS_QT, "PyQt6 + QScintilla not installed")
class TestAppearance(unittest.TestCase):
    """Light / dark / follow-the-system, and keeping the pane and dialogs in step."""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from raggy.app import MainWindow
        w = MainWindow()
        w._index_timer.stop()
        w._find_timer.stop()
        w._save_timer.stop()
        return w

    def test_choosing_light_gives_a_white_page(self):
        # ⚠️ THE point of the setting: a white page on a dark desktop, which is
        # what TextEdit's light look is. Nothing about it depends on the platform.
        w = self._window()
        w.set_appearance("light")
        self.assertFalse(w._is_dark())
        self.assertEqual(w.editor.paper().name(), "#ffffff")
        w._mark_clean()
        w.close()

    def test_choosing_dark_gives_a_dark_page(self):
        w = self._window()
        w.set_appearance("dark")
        self.assertTrue(w._is_dark())
        self.assertEqual(w.editor.paper().name(), "#1e1e1e")
        self.assertEqual(w.editor.color().name(), "#e8e8e8")
        w._mark_clean()
        w.close()

    def test_a_dark_pane_has_no_light_seams(self):
        # ⚠️ QScintilla's widget palette is NOT its paper: it stayed light in dark
        # mode, so anything Qt painted around the text was a bright seam.
        from PyQt6.QtGui import QPalette
        w = self._window()
        w.set_appearance("dark")
        pal = w.editor.palette()
        self.assertEqual(pal.color(QPalette.ColorRole.Base).name(), "#1e1e1e")
        self.assertEqual(pal.color(QPalette.ColorRole.Window).name(), "#252526")
        self.assertEqual(
            w.centralWidget().palette().color(QPalette.ColorRole.Window).name(),
            "#252526")
        w._mark_clean()
        w.close()

    def test_the_choice_is_remembered(self):
        w = self._window()
        w.set_appearance("dark")
        w._mark_clean()
        w.close()

        again = self._window()
        self.assertTrue(again._is_dark(), "the appearance setting was not stored")
        again._mark_clean()
        again.close()

    def test_the_menu_shows_which_appearance_is_active(self):
        w = self._window()
        for choice in ("light", "dark", "system"):
            w.set_appearance(choice)
            ticked = [k for k, a in w._appearance_actions.items() if a.isChecked()]
            self.assertEqual(ticked, [choice])
        w._mark_clean()
        w.close()

    def test_bold_and_underline_survive_an_appearance_change(self):
        # ⚠️ STYLECLEARALL would wipe them; the guard is `_styled`.
        from raggy.app import STYLE_BOLD
        from PyQt6 import Qsci
        w = self._window()
        w.editor.setText("hello world\n")
        w.editor.SendScintilla(Qsci.QsciScintilla.SCI_SETSEL, 0, 5)
        w.toggle_bold()
        w.set_appearance("dark")
        styles = {w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETSTYLEAT, i)
                  for i in range(0, 5)}
        self.assertEqual(styles, {STYLE_BOLD})
        w._mark_clean()
        w.close()


@unittest.skipUnless(HAS_QT, "PyQt6 + QScintilla not installed")
class TestDialogTheming(unittest.TestCase):
    """The Ask dialogs must not be grey-on-grey in dark mode."""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_ask_dialog_follows_the_mode_it_is_given(self):
        from PyQt6.QtGui import QPalette
        from raggy.app import AskDialog
        d = AskDialog()
        d.apply_theme(True)
        pal = d.palette()
        self.assertEqual(pal.color(QPalette.ColorRole.Window).name(), "#252526")
        self.assertEqual(pal.color(QPalette.ColorRole.Base).name(), "#1e1e1e")
        self.assertEqual(pal.color(QPalette.ColorRole.Text).name(), "#e8e8e8")
        d.close()

    def test_the_setup_dialog_themes_the_same_way(self):
        from PyQt6.QtGui import QPalette
        from raggy.app import AISetupDialog
        d = AISetupDialog()
        d.apply_theme(True)
        self.assertEqual(
            d.palette().color(QPalette.ColorRole.Text).name(), "#e8e8e8")
        d.close()

    def test_muted_text_is_labelled_not_hard_coded(self):
        # ⚠️ The old `color: palette(mid)` was grey on grey in dark mode. Muted
        # labels now carry a role the theme styles.
        from raggy.app import THEME, AskDialog
        d = AskDialog()
        self.assertEqual(d.status.property("role"), "muted")
        d.apply_theme(True)
        self.assertIn(THEME[True]["muted"], d.styleSheet())
        d.apply_theme(False)
        self.assertIn(THEME[False]["muted"], d.styleSheet())
        d.close()

    def test_the_two_modes_really_are_different(self):
        from raggy.app import THEME
        self.assertNotEqual(THEME[True]["muted"], THEME[False]["muted"])
        self.assertNotEqual(THEME[True]["paper"], THEME[False]["paper"])

    def test_a_connection_result_is_coloured_by_outcome(self):
        from raggy.app import AISetupDialog
        d = AISetupDialog()
        d._set_result("Failed: nope", ok=False)
        self.assertEqual(d.result_label.property("role"), "bad")
        d._set_result("Working. The model said: \u201cOK\u201d", ok=True)
        self.assertEqual(d.result_label.property("role"), "ok")
        d._set_result("Trying\u2026")
        self.assertEqual(d.result_label.property("role"), "")
        d.close()

    def test_the_ask_dialog_offers_to_copy_a_real_answer_only(self):
        from raggy.app import AskDialog
        d = AskDialog()
        self.assertTrue(d.copy_btn.isHidden())
        d.show_answer("Restart the worker [1].")
        self.assertFalse(d.copy_btn.isHidden())
        d.close()

    def test_an_unconfigured_dialog_says_what_is_missing(self):
        from raggy.app import AskDialog
        d = AskDialog()
        d.set_ai_configured(False)
        self.assertFalse(d.setup_btn.isHidden())
        self.assertIn("No model connected", d.status.text())
        d.set_ai_configured(True, "llama3.2 at http://localhost:11434/v1")
        self.assertTrue(d.setup_btn.isHidden())
        self.assertIn("llama3.2", d.status.text())
        d.close()


@unittest.skipUnless(HAS_QT, "PyQt6 + QScintilla not installed")
class TestBoldUnderline(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from raggy.app import MainWindow
        self.w = MainWindow()
        self.w._index_timer.stop()
        self.w._find_timer.stop()
        self.w._save_timer.stop()
        self.addCleanup(self._dispose)

    def _dispose(self):
        self.w._mark_clean()
        self.w.close()

    def _select(self, start_char, end_char):
        """Select by CHARACTER offsets, converted to the bytes Scintilla wants.

        ⚠️ Deliberately done in the test rather than through the app, so a bug in
        the app's own conversion cannot hide itself.
        """
        from PyQt6 import Qsci
        text = self.w.editor.text()
        self.w.editor.SendScintilla(
            Qsci.QsciScintilla.SCI_SETSEL,
            len(text[:start_char].encode("utf-8")),
            len(text[:end_char].encode("utf-8")))

    def _styles(self, start_char, end_char):
        from PyQt6 import Qsci
        text = self.w.editor.text()
        first = len(text[:start_char].encode("utf-8"))
        last = len(text[:end_char].encode("utf-8"))
        return {self.w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETSTYLEAT, p)
                for p in range(first, last)}

    def test_bold_applies_and_toggles_off(self):
        from raggy.app import STYLE_BOLD
        from PyQt6 import Qsci
        self.w.editor.setText("hello world\n")
        self._select(0, 5)
        self.w.toggle_bold()
        self.assertEqual(self._styles(0, 5), {STYLE_BOLD})
        self.w.toggle_bold()
        self.assertEqual(self._styles(0, 5), {Qsci.QsciScintilla.STYLE_DEFAULT})

    def test_bold_and_underline_combine(self):
        from raggy.app import STYLE_BOLD_UNDERLINE
        self.w.editor.setText("hello world\n")
        self._select(0, 5)
        self.w.toggle_bold()
        self.w.toggle_underline()
        self.assertEqual(self._styles(0, 5), {STYLE_BOLD_UNDERLINE})
        self.w.toggle_bold()
        from raggy.app import STYLE_UNDERLINE
        self.assertEqual(self._styles(0, 5), {STYLE_UNDERLINE})

    def test_styling_does_not_spill_onto_the_neighbours(self):
        # ⚠️ The offsets are BYTES. With a multi-byte character inside the
        # selection, a mistake here styles the wrong text — the same class of bug
        # that once made the editor select the wrong passage for a search hit.
        from PyQt6 import Qsci
        from raggy.app import STYLE_BOLD
        text = "caf\u00e9 na\u00efve \u2014 the end\n"
        self.w.editor.setText(text)
        start = text.index("na\u00efve")
        end = start + len("na\u00efve")
        self._select(start, end)
        self.w.toggle_bold()
        self.assertEqual(self._styles(start, end), {STYLE_BOLD})
        plain = Qsci.QsciScintilla.STYLE_DEFAULT
        self.assertEqual(self._styles(0, start), {0}, "text before it was styled")
        self.assertEqual(self._styles(end, len(text) - 1), {0},
                         "text after it was styled")

    def test_nothing_selected_is_harmless(self):
        self.w.editor.setText("hello\n")
        from PyQt6 import Qsci
        self.w.editor.SendScintilla(Qsci.QsciScintilla.SCI_SETSEL, 2, 2)
        self.w.toggle_bold()          # must not raise, must not style anything

    def test_the_menu_entries_exist_and_are_checkable(self):
        edit = next(a.menu() for a in self.w.menuBar().actions()
                    if a.text().replace("&", "") == "Edit")
        labels = {a.text().replace("&", "") for a in edit.actions() if a.text()}
        self.assertIn("Bold", labels)
        self.assertIn("Underline", labels)
        self.assertTrue(self.w._bold_action.isCheckable())


@unittest.skipUnless(HAS_QT, "PyQt6 + QScintilla not installed")
class TestRename(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from raggy.app import MainWindow
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.w = MainWindow()
        self.w._index_timer.stop()
        self.w._find_timer.stop()
        self.w._save_timer.stop()
        self.addCleanup(self._dispose)

    def _dispose(self):
        self.w._mark_clean()
        self.w.close()

    def _loaded(self, name="notes.txt", body="hello\n"):
        path = self.dir / name
        path.write_text(body, encoding="utf-8")
        self.w.load_path(str(path), warn_encoding=False)
        return path

    def _rename_to(self, name):
        from unittest import mock
        from raggy import app as app_module
        with mock.patch.object(app_module.QInputDialog, "getText",
                               return_value=(name, True)):
            return self.w.rename_document()

    def test_it_renames_the_file_on_disk_and_follows_it(self):
        old = self._loaded()
        self.assertTrue(self._rename_to("renamed.txt"))
        self.assertFalse(old.exists())
        self.assertTrue((self.dir / "renamed.txt").exists())
        self.assertEqual(self.w.path, str(self.dir / "renamed.txt"))
        self.assertIn("renamed.txt", self.w.windowTitle())

    def test_a_cancelled_prompt_changes_nothing(self):
        from unittest import mock
        from raggy import app as app_module
        old = self._loaded()
        with mock.patch.object(app_module.QInputDialog, "getText",
                               return_value=("whatever.txt", False)):
            self.assertFalse(self.w.rename_document())
        self.assertTrue(old.exists())
        self.assertEqual(self.w.path, str(old))

    def test_a_separator_is_refused(self):
        # ⚠️ Otherwise "Rename" silently becomes "move the file elsewhere".
        from unittest import mock
        from raggy import app as app_module
        old = self._loaded()
        with mock.patch.object(app_module.QMessageBox, "warning") as warned:
            self.assertFalse(self._rename_to("sub/dir.txt"))
        self.assertTrue(old.exists(), "the file was moved anyway")
        self.assertTrue(warned.called)

    def test_an_existing_name_is_refused(self):
        from unittest import mock
        from raggy import app as app_module
        self._loaded("taken.txt", "other\n")
        old = self._loaded("notes.txt")
        with mock.patch.object(app_module.QMessageBox, "warning") as warned:
            self.assertFalse(self._rename_to("taken.txt"))
        self.assertTrue(old.exists())
        self.assertTrue(warned.called)

    def test_an_untitled_document_falls_back_to_save_as(self):
        called = []
        self.w.save_as = lambda: called.append(1) or False
        self.assertFalse(self.w.rename_document())
        self.assertEqual(called, [1])


@unittest.skipUnless(HAS_QT, "PyQt6 + QScintilla not installed")
class TestFormulaPreviewUI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from raggy.app import MainWindow
        self.w = MainWindow()
        self.w._index_timer.stop()
        self.w._find_timer.stop()
        self.w._save_timer.stop()
        self.addCleanup(self._dispose)

    def _dispose(self):
        self.w._mark_clean()
        self.w.close()

    def _caret_after(self, snippet):
        from PyQt6 import Qsci
        text = self.w.editor.text()
        char = text.index(snippet) + len(snippet)
        self.w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GOTOPOS,
                                    len(text[:char].encode("utf-8")))
        self.w._update_formula_preview()

    def test_the_bar_appears_on_a_formula_and_leaves_on_prose(self):
        self.w.editor.setText("notes\n\nE = mc^2\n\nplain words\n")
        self._caret_after("E = mc^2")
        self.assertFalse(self.w.formula_bar.isHidden())
        self.assertEqual(self.w.formula_bar.kind.text(), "equation")
        self.assertIn("<sup>2</sup>", self.w.formula_bar.render.text())

        self._caret_after("plain words")
        self.assertTrue(self.w.formula_bar.isHidden())

    def test_chemistry_renders_as_subscripts(self):
        self.w.editor.setText("2H2 + O2 -> 2H2O\n")
        self._caret_after("2H2 + O2 -> 2H2O")
        self.assertEqual(self.w.formula_bar.kind.text(), "chemical")
        self.assertIn("<sub>2</sub>", self.w.formula_bar.render.text())
        self.assertIn("\u2192", self.w.formula_bar.render.text())

    def test_the_caret_offset_is_characters_not_bytes(self):
        # ⚠️ Scintilla speaks bytes; the formula module speaks characters. A
        # document with multi-byte text before the formula would otherwise look up
        # the wrong place.
        self.w.editor.setText("caf\u00e9 \u2014 \n\nE = mc^2\n")
        self._caret_after("E = mc^2")
        self.assertEqual(self.w.formula_bar.kind.text(), "equation")

    def test_it_can_be_turned_off_and_the_menu_follows(self):
        self.w.editor.setText("E = mc^2\n")
        self._caret_after("E = mc^2")
        self.assertFalse(self.w.formula_bar.isHidden())
        self.w.set_formula_preview(False)
        self.assertTrue(self.w.formula_bar.isHidden())
        self.assertFalse(self.w._formula_action.isChecked())
        self.w.set_formula_preview(True)
        self.assertFalse(self.w.formula_bar.isHidden())
        self.assertTrue(self.w._formula_action.isChecked())

    def test_an_empty_document_is_harmless(self):
        self.w.editor.setText("")
        self.w._update_formula_preview()
        self.assertTrue(self.w.formula_bar.isHidden())

    def test_the_bar_is_not_a_splitter_or_a_tab(self):
        # The window is still one plain pane with a transient strip.
        from PyQt6.QtWidgets import QSplitter, QTabWidget
        self.assertEqual(self.w.findChildren(QSplitter), [])
        self.assertEqual(self.w.findChildren(QTabWidget), [])


if __name__ == "__main__":
    unittest.main()


