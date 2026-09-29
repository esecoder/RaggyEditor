#!/usr/bin/env python3
"""
app.py — RaggyEditor, the desktop application.

A plain text editor. One document, one window, a find bar that slides in when
you ask for it — the way TextEdit works.

    Cmd+F    find        Cmd+G / Cmd+Shift+G   next / previous
    Cmd+E    use selection for find
    Cmd+Shift+A   ask a question about this document
    Esc      close the find bar

===============================================================================
ONE SEARCH BOX, TWO KINDS OF ANSWER
===============================================================================
The find field returns what every editor returns — the words you typed — AND the
passages that mean the same thing in different words. They arrive in one
navigable list, so there is no "semantic mode" to switch to and no second panel.

===============================================================================
WHAT IS DELIBERATELY NOT HERE
===============================================================================
There is no "Model" menu, no encoder picker, no "LSA / ONNX / neural" jargon,
and no status bar reporting cache statistics. Those are facts about the
implementation, not about the user's document, and a text editor should not ask
anyone to choose them.

⚠️ The one genuinely user-facing need behind all of it — "can this find things by
meaning?" — is met by a single offer, made exactly once, at the moment it
matters: when a search finds nothing by exact match. It is also reachable by
hand from the Help menu as "Enable Search by Meaning…".

The internal names still exist for developers: RAGGY_ENCODER=lsa|onnx|neural,
and `./run.sh install-model`.
"""

from __future__ import annotations

import os
import re
import sys

from raggy import model_store
from raggy.engine import RaggyEngine

try:
    from PyQt6 import Qsci
    from PyQt6.QtCore import QSettings, Qt, QThread, QTimer, pyqtSignal
    from PyQt6.QtGui import QAction, QKeySequence
    from PyQt6.QtWidgets import (
        QApplication, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel,
        QLineEdit, QMainWindow, QMessageBox, QProgressDialog, QToolButton,
        QVBoxLayout, QWidget)
    HAS_QT = True
except Exception:                                                   # noqa: BLE001
    HAS_QT = False

APP_NAME = "RaggyEditor"

IND_EXACT = 8
IND_RELATED = 9
COL_EXACT = 0x40E0FF        # Scintilla colours are BGR: amber
COL_RELATED = 0xFFBE8C      # blue
MAX_EXACT = 2000
UNTITLED = "Untitled"


# =============================================================================
# BACKGROUND WORK
# =============================================================================
if HAS_QT:

    class Worker(QThread):
        done = pyqtSignal(object)
        failed = pyqtSignal(str)

        def __init__(self, fn, *args, **kwargs):
            super().__init__()
            self._fn, self._args, self._kwargs = fn, args, kwargs

        def run(self):
            try:
                self.done.emit(self._fn(*self._args, **self._kwargs))
            except Exception as e:                                  # noqa: BLE001
                self.failed.emit(f"{type(e).__name__}: {e}")

    class DownloadWorker(QThread):
        progress = pyqtSignal(int, int)
        done = pyqtSignal(str)
        failed = pyqtSignal(str)

        def run(self):
            try:
                self.done.emit(str(model_store.download(
                    progress=lambda _n, got, total: self.progress.emit(got, total))))
            except Exception as e:                                  # noqa: BLE001
                self.failed.emit(f"{type(e).__name__}: {e}")

    # =========================================================================
    # FIND BAR
    # =========================================================================
    class FindField(QLineEdit):
        """A search field that reports Esc, so the bar can close on it."""

        escapePressed = pyqtSignal()

        def keyPressEvent(self, event):
            if event.key() == Qt.Key.Key_Escape:
                self.escapePressed.emit()
                return
            super().keyPressEvent(event)

    class FindBar(QFrame):
        """The only search UI: a slim bar above the document."""

        queryChanged = pyqtSignal(str)
        nextRequested = pyqtSignal()
        prevRequested = pyqtSignal()
        closed = pyqtSignal()

        def __init__(self):
            super().__init__()
            self.setFrameShape(QFrame.Shape.NoFrame)
            self.setStyleSheet(
                "FindBar { background: palette(window);"
                " border-bottom: 1px solid palette(mid); }")

            row = QHBoxLayout(self)
            row.setContentsMargins(10, 6, 10, 6)
            row.setSpacing(6)

            self.field = FindField()
            self.field.setPlaceholderText("Find")
            self.field.setClearButtonEnabled(True)
            self.field.setMinimumWidth(300)
            self.field.textChanged.connect(self.queryChanged)
            self.field.returnPressed.connect(self._enter)
            self.field.escapePressed.connect(self.closed)
            row.addWidget(self.field, 1)

            self.status = QLabel("")
            self.status.setStyleSheet("color: palette(mid); font-size: 12px;")
            row.addWidget(self.status)

            self.case_btn = self._toggle("Aa", "Match case")
            self.regex_btn = self._toggle(".*", "Regular expression")
            row.addWidget(self.case_btn)
            row.addWidget(self.regex_btn)

            self.prev_btn = self._chevron("\u2039", "Previous match", self.prevRequested)
            self.next_btn = self._chevron("\u203a", "Next match", self.nextRequested)
            row.addWidget(self.prev_btn)
            row.addWidget(self.next_btn)

            done = QToolButton()
            done.setText("Done")
            done.setAutoRaise(True)
            done.clicked.connect(self.closed)
            row.addWidget(done)

        def _toggle(self, text, tip):
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setAutoRaise(True)
            b.setToolTip(tip)
            b.toggled.connect(lambda _: self.queryChanged.emit(self.field.text()))
            return b

        def _chevron(self, text, tip, signal):
            b = QToolButton()
            b.setText(text)
            b.setAutoRaise(True)
            b.setToolTip(tip)
            b.clicked.connect(signal)
            return b

        def _enter(self):
            if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.prevRequested.emit()
            else:
                self.nextRequested.emit()

        def query(self) -> str:
            return self.field.text()

        def set_status(self, text: str):
            self.status.setText(text)

    # =========================================================================
    # WINDOW
    # =========================================================================
    class MainWindow(QMainWindow):

        def __init__(self, path: str | None = None):
            super().__init__()
            self.resize(980, 720)

            self.engine = RaggyEngine()
            self.path: str | None = None
            self._indexed_text = ""
            self._results: list[dict] = []
            self._current = 0
            self._semantic_low = False
            self._workers: list[QThread] = []
            self._settings = QSettings("RaggyEditor", "RaggyEditor")
            self._meaning_offer_made = bool(
                self._settings.value("meaning_offer_made", False, type=bool))

            self._index_timer = QTimer(self)
            self._index_timer.setSingleShot(True)
            self._index_timer.setInterval(1200)
            self._index_timer.timeout.connect(self._index_document)

            self._find_timer = QTimer(self)
            self._find_timer.setSingleShot(True)
            self._find_timer.setInterval(180)
            self._find_timer.timeout.connect(self._run_search)

            self._build_ui()
            self._build_menus()
            self._setup_indicators()
            self._update_title()

            if path:
                self.load_path(path)

        # ------------------------------------------------------------------ UI
        def _build_ui(self):
            self.editor = Qsci.QsciScintilla()
            self.editor.setUtf8(True)
            self.editor.setWrapMode(Qsci.QsciScintilla.WrapMode.WrapWord)
            self.editor.setCaretLineVisible(False)
            self.editor.setMarginWidth(0, 0)
            # ⚠️ TextEdit's plain-text default is the PROPORTIONAL system font,
            # not a fixed-width one. Looking like TextEdit was the explicit ask.
            self.editor.setFont(QApplication.font())

            self.find_bar = FindBar()
            self.find_bar.hide()
            self.find_bar.queryChanged.connect(self._on_query_changed)
            self.find_bar.nextRequested.connect(self._next)
            self.find_bar.prevRequested.connect(self._prev)
            self.find_bar.closed.connect(self._close_find)

            central = QWidget()
            col = QVBoxLayout(central)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(0)
            col.addWidget(self.find_bar)
            col.addWidget(self.editor, 1)
            self.setCentralWidget(central)

            self.editor.textChanged.connect(self._on_text_changed)
            try:
                self.editor.modificationChanged.connect(self.setWindowModified)
            except Exception:                                       # noqa: BLE001
                pass

        def _setup_indicators(self):
            S = Qsci.QsciScintilla
            for ind, colour in ((IND_EXACT, COL_EXACT), (IND_RELATED, COL_RELATED)):
                try:
                    self.editor.SendScintilla(S.SCI_INDICSETSTYLE, ind, S.INDIC_ROUNDBOX)
                    self.editor.SendScintilla(S.SCI_INDICSETFORE, ind, colour)
                    self.editor.SendScintilla(S.SCI_INDICSETALPHA, ind, 70)
                    self.editor.SendScintilla(S.SCI_INDICSETOUTLINEALPHA, ind, 130)
                    self.editor.SendScintilla(S.SCI_INDICSETUNDER, ind, 0)
                except Exception:                                   # noqa: BLE001
                    pass

        def _build_menus(self):
            m = self.menuBar()

            f = m.addMenu("&File")
            self._act(f, "&Open…", QKeySequence.StandardKey.Open, self.open_file)
            self._act(f, "&Save", QKeySequence.StandardKey.Save, self.save_file)
            self._act(f, "Save &As…", QKeySequence.StandardKey.SaveAs, self.save_as)
            f.addSeparator()
            self._act(f, "&Close", QKeySequence.StandardKey.Close, self.close)

            e = m.addMenu("&Edit")
            self._act(e, "&Undo", QKeySequence.StandardKey.Undo, self.editor.undo)
            self._act(e, "&Redo", QKeySequence.StandardKey.Redo, self.editor.redo)
            e.addSeparator()
            self._act(e, "Cu&t", QKeySequence.StandardKey.Cut, self.editor.cut)
            self._act(e, "&Copy", QKeySequence.StandardKey.Copy, self.editor.copy)
            self._act(e, "&Paste", QKeySequence.StandardKey.Paste, self.editor.paste)
            self._act(e, "Select &All", QKeySequence.StandardKey.SelectAll,
                      self.editor.selectAll)

            d = m.addMenu("&Find")
            self._act(d, "&Find…", QKeySequence.StandardKey.Find, self.show_find)
            self._act(d, "Find &Next", QKeySequence.StandardKey.FindNext, self._next)
            self._act(d, "Find &Previous", QKeySequence.StandardKey.FindPrevious, self._prev)
            self._act(d, "Use Selection for Find", "Ctrl+E", self._use_selection)
            d.addSeparator()
            self._act(d, "&Ask a Question About This Document…", "Ctrl+Shift+A",
                      self.ask_document)

            h = m.addMenu("&Help")
            self._act(h, "Enable Search by Meaning…", None, self.enable_meaning_search)
            h.addSeparator()
            self._act(h, "&About " + APP_NAME, None, self.about)

        def _act(self, menu, text, shortcut, slot, checkable=False):
            a = QAction(text, self)
            a.setCheckable(checkable)
            if shortcut:
                a.setShortcut(shortcut if isinstance(shortcut, str) else QKeySequence(shortcut))
            a.triggered.connect(slot)
            menu.addAction(a)
            return a

        # --------------------------------------------------------------- title
        def _update_title(self):
            """TextEdit shows the document NAME, not the application name."""
            name = os.path.basename(self.path) if self.path else UNTITLED
            self.setWindowTitle(name)
            try:
                self.setWindowFilePath(self.path or "")
            except Exception:                                       # noqa: BLE001
                pass

        # --------------------------------------------------------------- files
        def open_file(self):
            p, _ = QFileDialog.getOpenFileName(
                self, "Open", os.path.expanduser("~"),
                "Text files (*.txt *.md *.log *.csv *.json);;All files (*)")
            if p:
                self.load_path(p)

        def load_path(self, path: str):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    self.editor.setText(fh.read())
            except Exception as e:                                  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Could not open:\n{e}")
                return
            self.path = path
            self._indexed_text = ""
            self.editor.setModified(False)
            self._update_title()
            self._index_document()
            if self.find_bar.isVisible() and self.find_bar.query():
                self._run_search()

        def save_file(self):
            if not self.path:
                return self.save_as()
            try:
                with open(self.path, "w", encoding="utf-8") as fh:
                    fh.write(self.editor.text())
                self.editor.setModified(False)
            except Exception as e:                                  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Could not save:\n{e}")

        def save_as(self):
            p, _ = QFileDialog.getSaveFileName(
                self, "Save As", os.path.expanduser("~"), "Text files (*.txt);;All files (*)")
            if p:
                self.path = p
                self.editor.setModified(True)
                self.save_file()
                self._update_title()

        # ------------------------------------------------------------- indexing
        def _on_text_changed(self):
            self._index_timer.start()

        def _index_document(self):
            text = self.editor.text()
            if text == self._indexed_text:
                return
            doc = os.path.basename(self.path) if self.path else UNTITLED
            self._run(self.engine.index, text, doc,
                      on_done=self._indexed,
                      on_fail=lambda _m: None)     # indexing failure is not fatal
        # NOTE: no status bar. Indexing is silent unless a search is waiting on
        # it, in which case the find bar says "Indexing…" (see _update_status).

        def _indexed(self, info):
            self._indexed_text = self.editor.text()
            if self.find_bar.isVisible() and self.find_bar.query():
                self._run_search()

        # ---------------------------------------------------------------- find
        def _selected_text(self) -> str:
            """Read from Scintilla positions: `selectedText()` is empty when the
            editor does not hold focus, which is exactly when the bar has it."""
            S = Qsci.QsciScintilla
            try:
                a = self.editor.SendScintilla(S.SCI_GETSELECTIONSTART)
                b = self.editor.SendScintilla(S.SCI_GETSELECTIONEND)
            except Exception:                                       # noqa: BLE001
                return ""
            text = self.editor.text()
            return text[a:b] if 0 <= a < b <= len(text) else ""

        def show_find(self):
            self.find_bar.show()
            sel = self._selected_text()
            if sel and "\n" not in sel and not self.find_bar.query():
                self.find_bar.field.setText(sel)
            self.find_bar.field.setFocus()
            self.find_bar.field.selectAll()
            if self.find_bar.query():
                self._run_search()

        def _close_find(self):
            self.find_bar.hide()
            self._clear_indicators()
            self.editor.setFocus()

        def _use_selection(self):
            sel = self._selected_text()
            if sel and "\n" not in sel:
                self.find_bar.show()
                self.find_bar.field.setText(sel)
                self.find_bar.field.setFocus()
                self._run_search()

        def _on_query_changed(self, _text):
            self._find_timer.start()

        def _run_search(self):
            query = self.find_bar.query()
            self._clear_indicators()
            self._results = []
            self._current = 0
            self._semantic_low = False

            if not query:
                self.find_bar.set_status("")
                return

            exact = self._exact_matches(query)
            if exact is None:
                return                          # bad regex; message already shown
            related = self._related_passages(query)
            seen = {(r["start"], r["end"]) for r in exact}
            self._results = exact + [r for r in related
                                     if (r["start"], r["end"]) not in seen]

            self._highlight()
            self._update_status()
            if self._results:
                self._select_current()

            # The one moment worth mentioning meaning-search: you asked for
            # something, and by exact match alone, this document has nothing.
            if not exact and self._should_offer_meaning():
                self._offer_meaning_search()

        def _exact_matches(self, query: str) -> list[dict] | None:
            flags = 0 if self.find_bar.case_btn.isChecked() else re.IGNORECASE
            pattern = query if self.find_bar.regex_btn.isChecked() else re.escape(query)
            try:
                rx = re.compile(pattern, flags)
            except re.error as e:
                self.find_bar.set_status(f"Bad expression: {e}")
                return None
            out = []
            for i, m in enumerate(rx.finditer(self.editor.text())):
                if i >= MAX_EXACT:
                    break
                if m.end() > m.start():
                    out.append({"kind": "match", "start": m.start(), "end": m.end()})
            return out

        def _related_passages(self, query: str) -> list[dict]:
            if not self._indexed_text or not self.engine.is_indexed:
                return []
            try:
                res = self.engine.semantic_search(query, k=15)
            except Exception:                                       # noqa: BLE001
                return []
            self._semantic_low = bool(res.get("low_confidence"))
            return [{"kind": "related", "start": h["start"], "end": h["end"]}
                    for h in res.get("results", [])]

        # -------------------------------------------------------- highlighting
        def _clear_indicators(self):
            S = Qsci.QsciScintilla
            length = len(self.editor.text())
            try:
                for ind in (IND_EXACT, IND_RELATED):
                    self.editor.SendScintilla(S.SCI_SETINDICATORCURRENT, ind)
                    self.editor.SendScintilla(S.SCI_INDICATORCLEARRANGE, 0, length)
            except Exception:                                       # noqa: BLE001
                pass

        def _highlight(self):
            S = Qsci.QsciScintilla
            try:
                for r in self._results:
                    ind = IND_EXACT if r["kind"] == "match" else IND_RELATED
                    self.editor.SendScintilla(S.SCI_SETINDICATORCURRENT, ind)
                    self.editor.SendScintilla(S.SCI_INDICATORFILLRANGE,
                                              r["start"], r["end"] - r["start"])
            except Exception:                                       # noqa: BLE001
                pass

        def _update_status(self):
            n = len(self._results)
            if not n:
                if not self._indexed_text:
                    self.find_bar.set_status("Indexing…")
                elif self._semantic_low:
                    self.find_bar.set_status("No matches")
                else:
                    self.find_bar.set_status("No matches")
                return
            exact = sum(1 for r in self._results if r["kind"] == "match")
            related = n - exact
            parts = [f"{self._current + 1} of {n}"]
            if exact and related:
                parts.append(f"{exact} exact · {related} related")
            elif related:
                parts.append(f"{related} related")
            else:
                parts.append(f"{exact} matches")
            self.find_bar.set_status("  ·  ".join(parts))

        # ---------------------------------------------------------- navigation
        def _select_current(self):
            if not self._results:
                return
            r = self._results[self._current]
            S = Qsci.QsciScintilla
            try:
                self.editor.SendScintilla(S.SCI_SETSEL, r["start"], r["end"])
                line = self.editor.SendScintilla(S.SCI_LINEFROMPOSITION, r["start"])
                self.editor.ensureLineVisible(line)
            except Exception:                                       # noqa: BLE001
                pass
            self._update_status()

        def _next(self):
            if not self._results:
                if self.find_bar.isVisible():
                    self._run_search()
                return
            self._current = (self._current + 1) % len(self._results)
            self._select_current()

        def _prev(self):
            if not self._results:
                return
            self._current = (self._current - 1) % len(self._results)
            self._select_current()

        # ------------------------------------------------- meaning search offer
        def _should_offer_meaning(self) -> bool:
            """Offer the download once, only when it would actually help."""
            if self._meaning_offer_made or model_store.is_available():
                return False
            from raggy.encoder import onnx_importable
            return bool(onnx_importable())

        def _offer_meaning_search(self):
            self._meaning_offer_made = True
            self._settings.setValue("meaning_offer_made", True)
            r = QMessageBox.question(
                self, "Search by Meaning",
                "No exact matches.\n\n"
                "RaggyEditor can also find passages that mean the same thing in "
                "different words. That needs a one-time download (about 133 MB).\n\n"
                "Download it now?")
            if r == QMessageBox.StandardButton.Yes:
                self.enable_meaning_search()

        def enable_meaning_search(self):
            if model_store.is_available():
                self._set_encoder("onnx")
                return
            self.progress = QProgressDialog(
                "Downloading (about 133 MB)…", "Cancel", 0, 100, self)
            self.progress.setWindowTitle("Search by Meaning")
            self.progress.setMinimumDuration(0)
            w = DownloadWorker()
            w.progress.connect(self._download_progress)
            w.done.connect(self._download_done)
            w.failed.connect(self._download_failed)
            self._keep(w)
            w.start()

        def _download_progress(self, got: int, total: int):
            if total and getattr(self, "progress", None):
                self.progress.setValue(int(100 * got / total))

        def _download_done(self, _path: str):
            if getattr(self, "progress", None):
                self.progress.close()
            self._set_encoder("onnx")

        def _download_failed(self, msg: str):
            if getattr(self, "progress", None):
                self.progress.close()
            QMessageBox.warning(self, APP_NAME,
                                f"Could not download:\n{msg}\n\n"
                                "The editor still works — exact search is unaffected.")

        def _set_encoder(self, kind: str):
            self.engine._encoder_kind = kind
            self.engine._resolved = None
            self._indexed_text = ""
            self._index_document()

        # ----------------------------------------------------------------- ask
        def ask_document(self):
            question, ok = QInputDialog.getText(
                self, "Ask About This Document", "Your question:")
            if not ok or not question.strip():
                return
            self._run(self.engine.ask, question.strip(),
                      on_done=self._show_answer,
                      on_fail=lambda m: QMessageBox.warning(self, APP_NAME, m))

        def _show_answer(self, data):
            mode = data.get("mode")
            if mode == "abstained":
                body = ("This document does not appear to contain the answer.\n\n"
                        "RaggyEditor says so rather than guessing.")
            elif mode == "retrieval_only":
                body = ("No answer can be generated without an API key, so here is what "
                        "the document does say — use Find to see the passages.\n\n"
                        "To enable written answers, add your key to the project's .env "
                        "file (DeepSeek works unchanged).")
            else:
                body = data.get("answer") or ""
                cites = data.get("citations") or []
                used = data.get("cited") or []
                if used:
                    body += "\n\nFrom:"
                    for i in used:
                        c = cites[i]
                        body += ("\n  line " + str(c["line"]) + ": "
                                 + " ".join(c["text"].split())[:70])
            QMessageBox.information(self, "Answer", body)

        # --------------------------------------------------------------- about
        def about(self):
            QMessageBox.about(self, f"About {APP_NAME}", (
                f"<b>{APP_NAME}</b><br><br>"
                "A plain text editor that can find passages by meaning, as well as "
                "by the exact words you type.<br><br>"
                "Free software under the GNU General Public License v3.<br>"
                "Editing engine: Scintilla."))

        # ------------------------------------------------------------- helpers
        def _run(self, fn, *args, on_done, on_fail):
            w = Worker(fn, *args)
            w.done.connect(on_done)
            w.failed.connect(on_fail)
            self._keep(w)
            w.start()

        def _keep(self, w: QThread):
            self._workers.append(w)
            w.finished.connect(
                lambda: self._workers.remove(w) if w in self._workers else None)

        def closeEvent(self, event):
            if self.editor.isModified() and self.path:
                r = QMessageBox.question(self, APP_NAME, "Save changes before closing?",
                                         QMessageBox.StandardButton.Save
                                         | QMessageBox.StandardButton.Discard
                                         | QMessageBox.StandardButton.Cancel)
                if r == QMessageBox.StandardButton.Save:
                    self.save_file()
                elif r == QMessageBox.StandardButton.Cancel:
                    event.ignore()
                    return
            event.accept()

else:  # pragma: no cover

    class MainWindow:  # type: ignore
        pass

    class FindBar:  # type: ignore
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    selftest = "--selftest" in argv
    if selftest:
        argv = [a for a in argv if a != "--selftest"]
    if not HAS_QT:
        print("RaggyEditor needs PyQt6 and QScintilla:\n"
              "    ./run.sh install\n"
              "(the engine and CLI still work without them: ./run.sh demo)")
        return 1
    path = argv[1] if len(argv) > 1 else None
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    win = MainWindow(path)
    win.show()
    if selftest:
        print("selftest: window constructed OK")
        QTimer.singleShot(0, app.quit)
        return app.exec()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
