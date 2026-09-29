#!/usr/bin/env python3
"""
app.py — RaggyEditor, the desktop application.

One document pane. No side panels, no tabs. `Cmd+F` slides a find bar in at the
top, exactly like TextEdit's — and that one search box answers with BOTH kinds of
match:

  * the word(s) you typed (exact, as any editor finds them), and
  * the passages that mean the same thing even though they use different words.

They arrive in one navigable list, so there is no separate "semantic find" UI to
learn. Enter / Shift+Enter (or the chevrons) walk through every match; the
document highlights them; Done closes the bar.

    Cmd+F   find (exact + related)      Cmd+G / Cmd+Shift+G  next / previous
    Cmd+E   use selection for find      Cmd+Shift+A          ask the document
    Esc     close the find bar

===============================================================================
WHAT CHANGED AND WHY
===============================================================================
The first version split the window into an editor and a tabbed panel with
permanent Find/Ask tabs. That is a code-editor idiom — it is not what a plain
text editor looks like, and it made the simple act of searching look like a
different application. The panel is gone; the search bar is transient.

⚠️ The engine is unchanged. This file is presentation: the same hybrid retrieval,
the same calibrated abstention, the same incremental index — just surfaced the
way a text editor's Find should be.

Run:
    ./run.sh app
    python -m raggy.app path/to/file.txt
"""

from __future__ import annotations

import os
import re
import sys

from raggy import model_store
from raggy.engine import RaggyEngine

try:
    from PyQt6 import Qsci
    from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
    from PyQt6.QtGui import QAction, QFontDatabase, QKeySequence
    from PyQt6.QtWidgets import (
        QApplication, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel,
        QLineEdit, QMainWindow, QMessageBox, QProgressDialog, QToolButton,
        QVBoxLayout, QWidget)
    HAS_QT = True
except Exception:                                                   # noqa: BLE001
    HAS_QT = False

APP_NAME = "RaggyEditor"

# Scintilla indicator slots used for the two kinds of highlight.
IND_EXACT = 8
IND_RELATED = 9
# Scintilla colours are BGR, not RGB.
COL_EXACT = 0x40E0FF        # amber  (R=255 G=224 B=64)
COL_RELATED = 0xFFBE8C      # blue   (R=140 G=190 B=255)
MAX_EXACT = 2000            # a search for "e" must not freeze the UI


# =============================================================================
# BACKGROUND WORK
# =============================================================================
if HAS_QT:

    class Worker(QThread):
        """Runs one callable off the UI thread and reports result or error."""

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
    # THE FIND BAR — the only search UI in the app
    # =========================================================================
    class FindBar(QFrame):
        """A slim bar that slides in above the document, TextEdit-style."""

        queryChanged = pyqtSignal(str)
        nextRequested = pyqtSignal()
        prevRequested = pyqtSignal()
        closed = pyqtSignal()

        def __init__(self):
            super().__init__()
            self.setFrameShape(QFrame.Shape.NoFrame)
            self.setStyleSheet(
                "FindBar { background: palette(window); border-bottom: 1px solid "
                "palette(mid); }")

            row = QHBoxLayout(self)
            row.setContentsMargins(10, 6, 10, 6)
            row.setSpacing(6)

            self.field = QLineEdit()
            self.field.setPlaceholderText("Find — words, or describe what you mean")
            self.field.setClearButtonEnabled(True)
            self.field.setMinimumWidth(320)
            self.field.textChanged.connect(self.queryChanged)
            self.field.returnPressed.connect(self._enter)
            row.addWidget(self.field, 1)

            self.status = QLabel("")
            self.status.setStyleSheet("color: palette(mid); font-size: 12px;")
            row.addWidget(self.status)

            self.regex_btn = self._toggle(".*", "Treat the query as a regular expression")
            self.case_btn = self._toggle("Aa", "Match case")
            row.addWidget(self.regex_btn)
            row.addWidget(self.case_btn)

            self.prev_btn = self._button("\u2039", "Previous match", self.prevRequested)
            self.next_btn = self._button("\u203a", "Next match", self.nextRequested)
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

        def _button(self, text, tip, signal):
            b = QToolButton()
            b.setText(text)
            b.setAutoRaise(True)
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.clicked.connect(signal)
            return b

        def _enter(self):
            # Shift+Enter walks backwards, like TextEdit.
            if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.prevRequested.emit()
            else:
                self.nextRequested.emit()

        def query(self) -> str:
            return self.field.text()

        def set_status(self, text: str):
            self.status.setText(text)

    # =========================================================================
    # THE WINDOW — one editor, nothing else
    # =========================================================================
    class MainWindow(QMainWindow):

        def __init__(self, path: str | None = None):
            super().__init__()
            self.setWindowTitle(APP_NAME)
            self.resize(980, 720)

            self.engine = RaggyEngine()
            self.path: str | None = None
            self._indexed_text = ""
            self._results: list[dict] = []
            self._current = 0
            self._semantic_low = False
            self._workers: list[QThread] = []

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

            if path:
                self.load_path(path)
            else:
                self.statusBar().showMessage(
                    "Open a file (Cmd+O) or paste text, then press Cmd+F.")

        # ------------------------------------------------------------------ UI
        def _build_ui(self):
            self.editor = Qsci.QsciScintilla()
            self.editor.setUtf8(True)
            self.editor.setWrapMode(Qsci.QsciScintilla.WrapMode.WrapWord)
            self.editor.setCaretLineVisible(False)          # TextEdit has no caret line
            self.editor.setMarginWidth(0, 0)                # no line numbers by default
            try:
                fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
                fixed.setPointSize(13)
                self.editor.setFont(fixed)
            except Exception:                                       # noqa: BLE001
                pass

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

        def _setup_indicators(self):
            """Two highlight styles: exact matches, and semantically related text."""
            S = Qsci.QsciScintilla
            for ind, colour in ((IND_EXACT, COL_EXACT), (IND_RELATED, COL_RELATED)):
                try:
                    self.editor.SendScintilla(S.SCI_INDICSETSTYLE, ind, S.INDIC_ROUNDBOX)
                    self.editor.SendScintilla(S.SCI_INDICSETFORE, ind, colour)
                    self.editor.SendScintilla(S.SCI_INDICSETALPHA, ind, 70)
                    self.editor.SendScintilla(S.SCI_INDICSETOUTLINEALPHA, ind, 130)
                    self.editor.SendScintilla(S.SCI_INDICSETUNDER, ind, 0)
                except Exception:                                   # noqa: BLE001
                    pass                                            # cosmetic only

        def _build_menus(self):
            m = self.menuBar()

            f = m.addMenu("&File")
            self._act(f, "&Open…", QKeySequence.StandardKey.Open, self.open_file)
            self._act(f, "&Save", QKeySequence.StandardKey.Save, self.save_file)
            self._act(f, "Save &As…", QKeySequence.StandardKey.SaveAs, self.save_as)

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
            self._act(d, "&Ask the Document…", "Ctrl+Shift+A", self.ask_document)

            v = m.addMenu("&View")
            self._line_act = self._act(v, "Show Line Numbers", None,
                                       self._toggle_line_numbers, checkable=True)
            self._wrap_act = self._act(v, "Wrap Lines", None, self._toggle_wrap,
                                       checkable=True)
            self._wrap_act.setChecked(True)

            mm = m.addMenu("&Model")
            self._act(mm, "&Download Semantic Model…", None, self.download_model)
            self._act(mm, "Use &Offline Encoder (LSA)", None, lambda: self._set_encoder("lsa"))
            self._act(mm, "Use &Semantic Encoder (ONNX)", None, lambda: self._set_encoder("onnx"))
            self._act(mm, "&Automatic (recommended)", None, lambda: self._set_encoder("auto"))
            mm.addSeparator()
            self._act(mm, "Model &Status", None, self.model_status)

            h = m.addMenu("&Help")
            self._act(h, "&About", None, self.about)

        def _act(self, menu, text, shortcut, slot, checkable=False):
            a = QAction(text, self)
            a.setCheckable(checkable)
            if shortcut:
                a.setShortcut(shortcut if isinstance(shortcut, str) else QKeySequence(shortcut))
            a.triggered.connect(slot)
            menu.addAction(a)
            return a

        # ------------------------------------------------------------- files
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
            self.setWindowTitle(f"{os.path.basename(path)} — {APP_NAME}")
            self._index_document()
            if self.find_bar.isVisible() and self.find_bar.query():
                self._run_search()

        def save_file(self):
            if not self.path:
                return self.save_as()
            try:
                with open(self.path, "w", encoding="utf-8") as fh:
                    fh.write(self.editor.text())
                self.statusBar().showMessage(f"Saved {self.path}", 4000)
            except Exception as e:                                  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Could not save:\n{e}")

        def save_as(self):
            p, _ = QFileDialog.getSaveFileName(
                self, "Save As", os.path.expanduser("~"), "Text files (*.txt);;All files (*)")
            if p:
                self.path = p
                self.save_file()
                self.setWindowTitle(f"{os.path.basename(p)} — {APP_NAME}")

        # ----------------------------------------------------------- indexing
        def _on_text_changed(self):
            self._index_timer.start()

        def _index_document(self):
            text = self.editor.text()
            if text == self._indexed_text:
                return
            doc = os.path.basename(self.path) if self.path else "untitled"
            self.statusBar().showMessage("Indexing…")
            self._run(self.engine.index, text, doc,
                      on_done=self._indexed, on_fail=lambda m: self.statusBar().showMessage(
                          "Index failed: " + m, 8000))

        def _indexed(self, info):
            self._indexed_text = self.editor.text()
            if info["reencoded_chunks"] == 0:
                note = f"reused all {info['reused_chunks']}"
            else:
                note = f"re-encoded {info['reencoded_chunks']}, reused {info['reused_chunks']}"
            self.statusBar().showMessage(
                f"{info['n_chunks']} passages · {note} · {info['index_ms']} ms", 5000)
            # Semantic results may now exist where there were none.
            if self.find_bar.isVisible() and self.find_bar.query():
                self._run_search()

        # ------------------------------------------------------------- find
        def _selected_text(self) -> str:
            """The current selection, read from Scintilla positions.

            ⚠️ Not `editor.selectedText()`: that returns an empty string in some
            QScintilla builds when the editor does not hold focus, which is
            exactly the case when the find bar has it.
            """
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
                # Bad regex. The message is already on the bar; do not overwrite
                # it with "No matches" — that would hide the actual problem.
                return
            related = self._related_passages(query)

            seen = {(r["start"], r["end"]) for r in exact}
            self._results = exact + [r for r in related if (r["start"], r["end"]) not in seen]

            self._highlight()
            self._update_status()
            if self._results:
                self._select_current()

        def _exact_matches(self, query: str) -> list[dict] | None:
            """The ordinary Find: the words you typed, matched literally (or as regex)."""
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
            """Passages that mean the same thing, via the RAG engine.

            ⚠️ These are what a word search cannot find, and they are returned in
            the SAME list as the word matches — that is the whole point of the
            redesign: one search box, two kinds of answer.
            """
            if not self._indexed_text or not self.engine.is_indexed:
                return []
            try:
                res = self.engine.semantic_search(query, k=15)
            except Exception:                                       # noqa: BLE001
                return []
            self._semantic_low = bool(res.get("low_confidence"))
            out = []
            for h in res.get("results", []):
                out.append({"kind": "related", "start": h["start"], "end": h["end"]})
            return out

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
                msg = "No matches"
                if self._semantic_low:
                    msg += " · nothing here looks related"
                elif not self._indexed_text:
                    msg = "Indexing…"
                self.find_bar.set_status(msg)
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

        # -------------------------------------------------------- navigation
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

        # -------------------------------------------------------------- ask
        def ask_document(self):
            question, ok = QInputDialog.getText(
                self, "Ask the Document",
                "Ask a question about this document:")
            if not ok or not question.strip():
                return
            if not self._indexed_text:
                self.statusBar().showMessage("Indexing — try again in a moment.", 6000)
                self._index_document()
                return
            self.statusBar().showMessage("Thinking…")
            self._run(self.engine.ask, question.strip(),
                      on_done=self._show_answer,
                      on_fail=lambda m: QMessageBox.warning(self, APP_NAME, m))

        def _show_answer(self, data):
            mode = data.get("mode")
            if mode == "abstained":
                body = (f"This document does not appear to contain the answer.\n\n"
                        f"({data.get('reason')})\n\n"
                        "RaggyEditor refuses rather than guessing.")
            elif mode == "retrieval_only":
                body = (f"{data.get('reason')}\n\n"
                        "Set OPENAI_API_KEY to enable generated answers. DeepSeek works "
                        "unchanged — it is OpenAI-compatible for chat.")
            else:
                body = data.get("answer") or ""
                cites = data.get("citations") or []
                used = data.get("cited") or []
                if used:
                    body += "\n\nSources:"
                    for i in used:
                        c = cites[i]
                        body += f"\n  [{i + 1}] line {c['line']}: " + " ".join(c["text"].split())[:70]
            self.statusBar().clearMessage()
            QMessageBox.information(self, "Ask the Document", body)

        # ------------------------------------------------------------ model
        def download_model(self):
            if model_store.is_available():
                self.model_status()
                return
            self.progress = QProgressDialog(
                "Downloading the semantic model (~133 MB)…", "Cancel", 0, 100, self)
            self.progress.setWindowTitle(APP_NAME)
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

        def _download_done(self, path: str):
            if getattr(self, "progress", None):
                self.progress.close()
            self._set_encoder("onnx")

        def _download_failed(self, msg: str):
            if getattr(self, "progress", None):
                self.progress.close()
            QMessageBox.warning(self, APP_NAME, f"Download failed:\n{msg}")

        def model_status(self):
            from raggy.encoder import onnx_importable
            active = self.engine.encoder.name if self.engine.encoder else "not indexed yet"
            QMessageBox.information(self, f"{APP_NAME} — Model", "\n".join([
                f"ONNX runtime available : {'yes' if onnx_importable() else 'no'}",
                f"Model downloaded       : {'yes' if model_store.is_available() else 'no'}",
                f"Active encoder         : {active}",
                f"Location               : {model_store.model_dir()}",
            ]))

        def _set_encoder(self, kind: str):
            self.engine._encoder_kind = kind
            self.engine._resolved = None
            self._indexed_text = ""
            self._index_document()

        # -------------------------------------------------------- view toggles
        def _toggle_line_numbers(self, on: bool):
            try:
                m = getattr(Qsci.QsciScintilla, "MarginType", None)
                kind = m.NumberMargin if m is not None else Qsci.QsciScintilla.NumberMargin
                self.editor.setMarginType(0, kind)
                self.editor.setMarginWidth(0, "0000" if on else 0)
            except Exception:                                       # noqa: BLE001
                pass

        def _toggle_wrap(self, on: bool):
            try:
                mode = (Qsci.QsciScintilla.WrapMode.WrapWord if on
                        else Qsci.QsciScintilla.WrapMode.WrapNone)
                self.editor.setWrapMode(mode)
            except Exception:                                       # noqa: BLE001
                pass

        # ------------------------------------------------------------ helpers
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

        def about(self):
            QMessageBox.about(self, f"About {APP_NAME}", (
                f"<b>{APP_NAME}</b> — a text editor whose Find works by meaning.<br><br>"
                "Editing engine: QScintilla (the text engine behind Notepad++ and SciTE).<br>"
                "One search box returns word matches and related passages together.<br><br>"
                "Runs fully offline. The semantic model is downloaded once, on request.<br>"
                "Licensed GPLv3 (QScintilla and PyQt6 are GPLv3)."))

        def closeEvent(self, event):
            if self.editor.isModified() and self.path:
                r = QMessageBox.question(self, APP_NAME, "Save changes before closing?")
                if r == QMessageBox.StandardButton.Yes:
                    self.save_file()
            event.accept()

else:  # pragma: no cover - only when Qt is missing

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
