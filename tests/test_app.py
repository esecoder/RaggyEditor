"""Desktop-app tests (headless).

These construct the real Qt window offscreen and drive the real Find/jump path.
They are skipped when PyQt6/QScintilla are absent, so the core suite stays
runnable on a machine with only numpy.

⚠️ Without this test the GUI is the one part of the app that is only ever
eyeballed — and "it looked fine when I clicked it" is not evidence.
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
        w.editor.setText(self.text)
        # Index synchronously with the offline encoder: no thread, no model.
        info = w.engine.index(self.text, SAMPLE.name)
        w._indexed_text = self.text
        return w, info

    def test_window_constructs_with_expected_menus(self):
        w, _ = self._window()
        menus = [a.text().replace("&", "") for a in w.menuBar().actions()]
        for expected in ("File", "Edit", "Find", "Model", "Help"):
            self.assertIn(expected, menus)
        w.close()

    def test_index_then_semantic_find_populates_results(self):
        w, info = self._window()
        self.assertGreater(info["n_chunks"], 1)
        w.mode.setCurrentIndex(0)                     # semantic
        w.query.setText("how do I fix an expired certificate on a replica")
        w.do_find()
        self.assertGreater(w.results.count(), 0)
        self.assertIn("confidence", w.find_meta.text())
        w.close()

    def test_result_click_selects_the_exact_passage(self):
        from PyQt6 import Qsci
        w, _ = self._window()
        w.mode.setCurrentIndex(0)
        w.query.setText("replication lag")
        w.do_find()
        w._activate_result(w.results.item(0))
        start = w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETSELECTIONSTART)
        end = w.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETSELECTIONEND)
        self.assertLess(start, end)
        # The selected text must be exactly what the result said it was.
        self.assertEqual(self.text[start:end], w._result_data[0]["text"])
        w.close()

    def test_regex_find_uses_the_engine(self):
        w, _ = self._window()
        w.mode.setCurrentIndex(1)                     # regex
        w.query.setText(r"ERR_[A-Z]+_\d+")
        w.do_find()
        self.assertGreaterEqual(w.results.count(), 4)
        w.close()

    def test_out_of_scope_query_is_flagged_low_confidence(self):
        w, _ = self._window()
        w.mode.setCurrentIndex(0)
        w.query.setText("what is the capital of France")
        w.do_find()
        self.assertIn("below threshold", w.find_meta.text())
        w.close()

    def test_editing_schedules_a_reindex(self):
        from raggy.app import _hook_editor_edits
        w, _ = self._window()
        _hook_editor_edits(w)
        self.assertFalse(w._index_timer.isActive())
        w.editor.setText(self.text + "\nA new final line.\n")
        self.assertTrue(w._index_timer.isActive())
        w.close()


if __name__ == "__main__":
    unittest.main()
