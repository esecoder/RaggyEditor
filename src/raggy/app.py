#!/usr/bin/env python3
"""
app.py — RaggyEditor, the desktop application.

A plain text editor. One document, one window, a find bar that slides in when
you ask for it — the way TextEdit works.

    Cmd+N    new document
    Cmd+O    open           Cmd+S    save          Cmd+Shift+S  save as
    Cmd+F    find           Cmd+G / Cmd+Shift+G    next / previous
    Cmd+E    use selection for find        Esc      close the find bar
    Cmd+Shift+A   ask a question about this document

===============================================================================
ONE SEARCH BOX, TWO KINDS OF ANSWER
===============================================================================
The find field returns what every editor returns — the words you typed — AND the
passages that mean the same thing in different words. They arrive in one
navigable list, so there is no "semantic mode" to switch to and no second panel.

===============================================================================
WHAT IS DELIBERATELY NOT HERE
===============================================================================
No "Model" menu, no encoder picker, no "LSA / ONNX / neural" jargon, no status
bar reporting cache statistics, no line-number gutter. Those are facts about the
implementation, not about the user's document.

⚠️ The one user-facing need behind them — "can this find things by meaning?" — is
met by a single offer, made once, at the moment it matters: when a search finds
nothing by exact match. Also reachable as Help ▸ Enable Search by Meaning…

Internal names remain for developers: RAGGY_ENCODER=lsa|onnx|neural,
./run.sh install-model.
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
    from PyQt6.QtGui import QAction, QColor, QKeySequence, QPalette
    from PyQt6.QtWidgets import (
        QApplication, QDialog, QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout,
        QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
        QProgressDialog, QPushButton, QToolButton, QVBoxLayout, QWidget)
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

FIND_PLACEHOLDER = "Find — words, or describe what you mean"


def _bgr(colour) -> int:
    """QColor -> the 0xBBGGRR integer Scintilla expects.

    ⚠️ Scintilla packs colours as BGR, not RGB. Passing an RGB value produces a
    plausible-looking wrong colour (red and blue swapped), with no error.
    """
    return (colour.blue() << 16) | (colour.green() << 8) | colour.red()


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
    # ASK DIALOG
    # =========================================================================
    class AskDialog(QDialog):
        """A question box and a large, resizable answer area.

        ⚠️ A QMessageBox is the obvious choice and the wrong one: it sizes itself
        to its content, so a long cited answer arrives in a cramped box. This is
        a real window the user can resize and read.
        """

        asked = pyqtSignal(str)

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Ask About This Document")
            self.setMinimumSize(680, 480)
            self.resize(760, 560)

            lay = QVBoxLayout(self)
            lay.setSpacing(8)

            row = QHBoxLayout()
            self.question = QLineEdit()
            self.question.setPlaceholderText("Your question")
            self.question.returnPressed.connect(self._emit)
            self.ask_btn = QPushButton("Ask")
            self.ask_btn.setDefault(True)
            self.ask_btn.clicked.connect(self._emit)
            row.addWidget(self.question, 1)
            row.addWidget(self.ask_btn)
            lay.addLayout(row)

            self.answer = QPlainTextEdit()
            self.answer.setReadOnly(True)
            self.answer.setPlaceholderText(
                "The answer, with the lines it came from. If the document does not "
                "contain the answer, it will say so instead of guessing.")
            lay.addWidget(self.answer, 1)

            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(self.reject)
            lay.addWidget(buttons)

        def _emit(self):
            q = self.question.text().strip()
            if q:
                self.asked.emit(q)

        def start(self, question: str):
            self.question.setText(question)
            self.set_busy(True)

        def set_busy(self, busy: bool):
            self.ask_btn.setEnabled(not busy)
            self.ask_btn.setText("Asking…" if busy else "Ask")
            if busy:
                self.answer.setPlainText("Looking through the document…")

        def show_answer(self, text: str):
            self.set_busy(False)
            self.answer.setPlainText(text)

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
            self.field.setPlaceholderText(FIND_PLACEHOLDER)
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
            self._ask_dialog: AskDialog | None = None
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
            self._apply_theme()
            self._watch_theme()
            self._update_title()

            if path:
                self.load_path(path)

        def _watch_theme(self):
            """Re-theme live when the user flips the system appearance."""
            try:
                QApplication.styleHints().colorSchemeChanged.connect(
                    lambda _scheme: self._apply_theme())
            except Exception:                                       # noqa: BLE001
                pass

        # ------------------------------------------------------------------ UI
        def _build_ui(self):
            self.editor = Qsci.QsciScintilla()
            self.editor.setUtf8(True)
            self.editor.setWrapMode(Qsci.QsciScintilla.WrapMode.WrapWord)
            self.editor.setCaretLineVisible(False)

            # ⚠️ Zero ALL THREE margins. Setting margin 0 to zero is not enough:
            # QScintilla gives margin 1 (the symbol/bookmark margin) a default
            # width of 16px, which shows up as a grey gutter down the left edge.
            # It is width 0, not "not drawn", so it has to be set explicitly.
            for m in (0, 1, 2):
                self.editor.setMarginWidth(m, 0)
            try:
                # Breathing room around the text, as TextEdit has. Left and right
                # are set together so the text block is not visibly off-centre.
                self.editor.SendScintilla(Qsci.QsciScintilla.SCI_SETMARGINLEFT, 0, 8)
                self.editor.SendScintilla(Qsci.QsciScintilla.SCI_SETMARGINRIGHT, 0, 8)
            except Exception:                                       # noqa: BLE001
                pass

            # The system font, at the system size — whatever the platform calls
            # "default". On macOS this is the proportional UI font TextEdit uses.
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

        # ------------------------------------------------------------- theming
        def _is_dark(self) -> bool:
            """Is the app currently in dark mode?

            Qt 6.5+ reports the platform colour scheme directly. `Unknown` is
            returned when the platform has no opinion (or in a headless/offscreen
            run), so fall back to reading the palette the app was given.
            """
            try:
                scheme = QApplication.styleHints().colorScheme()
                if scheme == Qt.ColorScheme.Dark:
                    return True
                if scheme == Qt.ColorScheme.Light:
                    return False
            except Exception:                                       # noqa: BLE001
                pass
            window = self.palette().color(QPalette.ColorRole.Window)
            return window.lightness() < 128

        def _apply_theme(self):
            """Colour the editor to match the platform, including the caret.

            ⚠️ QScintilla does NOT follow the application palette: it keeps a
            white background and a BLACK CARET whatever the system theme is. In
            dark mode that leaves a black caret on a white pane, and a white pane
            inside dark window chrome. Every colour below has to be set by hand.
            """
            dark = self._is_dark()
            if dark:
                paper = QColor("#1e1e1e")   # background
                ink = QColor("#e8e8e8")     # text
                caret = QColor("#ffffff")   # ⚠️ bright: a dark caret is invisible here
                sel_bg, sel_fg = QColor("#2f5fa8"), QColor("#ffffff")
                exact, related = 0x66D1FF, 0xFFA96A      # brighter, for a dark pane
            else:
                paper = QColor("#ffffff")
                ink = QColor("#000000")
                caret = QColor("#000000")
                sel_bg, sel_fg = QColor("#b3d7ff"), QColor("#000000")
                exact, related = COL_EXACT, COL_RELATED

            e = self.editor
            for setter, value in (
                    (e.setPaper, paper), (e.setColor, ink),
                    (e.setCaretForegroundColor, caret),
                    (e.setSelectionBackgroundColor, sel_bg),
                    (e.setSelectionForegroundColor, sel_fg),
                    (e.setMarginsBackgroundColor, paper),
                    (e.setMarginsForegroundColor, ink),
                    (e.setCaretLineBackgroundColor, paper)):
                try:
                    setter(value)
                except Exception:                                   # noqa: BLE001
                    pass

            # Default style + STYLECLEARALL, so any style QScintilla defaults to
            # (rather than one we set) also takes the theme.
            S = Qsci.QsciScintilla
            try:
                e.SendScintilla(S.SCI_STYLESETFORE, S.STYLE_DEFAULT, _bgr(ink))
                e.SendScintilla(S.SCI_STYLESETBACK, S.STYLE_DEFAULT, _bgr(paper))
                e.SendScintilla(S.SCI_STYLECLEARALL)
                e.SendScintilla(S.SCI_SETCARETFORE, _bgr(caret))
            except Exception:                                       # noqa: BLE001
                pass

            for ind, colour in ((IND_EXACT, exact), (IND_RELATED, related)):
                try:
                    e.SendScintilla(S.SCI_INDICSETSTYLE, ind, S.INDIC_ROUNDBOX)
                    e.SendScintilla(S.SCI_INDICSETFORE, ind, colour)
                    e.SendScintilla(S.SCI_INDICSETALPHA, ind, 70)
                    e.SendScintilla(S.SCI_INDICSETOUTLINEALPHA, ind, 130)
                    e.SendScintilla(S.SCI_INDICSETUNDER, ind, 0)
                except Exception:                                   # noqa: BLE001
                    pass

            # Indicators are re-painted from the current colours.
            self._highlight()

        def _build_menus(self):
            m = self.menuBar()

            f = m.addMenu("&File")
            self._act(f, "&New", QKeySequence.StandardKey.New, self.new_file)
            self._act(f, "&Open…", QKeySequence.StandardKey.Open, self.open_file)
            f.addSeparator()
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
            """TextEdit shows the document NAME, not the application name.

            ⚠️ The `[*]` placeholder is required, not cosmetic: Qt substitutes it
            with the modified marker (the dot in the close button on macOS), and
            `setWindowModified()` silently does nothing without it — Qt warns
            "The window title does not contain a '[*]' placeholder" and the user
            gets no unsaved-changes indicator.
            """
            name = os.path.basename(self.path) if self.path else UNTITLED
            self.setWindowTitle(name + "[*]")
            try:
                self.setWindowFilePath(self.path or "")
            except Exception:                                       # noqa: BLE001
                pass

        # --------------------------------------------------------------- files
        def _confirm_discard(self) -> bool:
            """Ask about unsaved changes. True means 'go ahead'."""
            if not self.editor.isModified():
                return True
            r = QMessageBox.question(
                self, APP_NAME, "Save changes before continuing?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Save:
                return self.save_file()          # may have been cancelled in Save As
            return r == QMessageBox.StandardButton.Discard

        def new_file(self):
            if not self._confirm_discard():
                return
            self.path = None
            self._indexed_text = ""
            self.editor.setText("")
            self.editor.setModified(False)
            self._update_title()
            self._close_find()
            self.editor.setFocus()

        def open_file(self):
            if not self._confirm_discard():
                return
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

        def save_file(self) -> bool:
            if not self.path:
                return self.save_as()
            try:
                with open(self.path, "w", encoding="utf-8") as fh:
                    fh.write(self.editor.text())
            except Exception as e:                                  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Could not save:\n{e}")
                return False
            self.editor.setModified(False)
            return True

        def save_as(self) -> bool:
            p, _ = QFileDialog.getSaveFileName(
                self, "Save As", os.path.expanduser("~"), "Text files (*.txt);;All files (*)")
            if not p:
                return False
            self.path = p
            self.editor.setModified(True)
            ok = self.save_file()
            if ok:
                self._update_title()
            return ok

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

        def _indexed(self, _info):
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
                self.find_bar.set_status(
                    "Indexing…" if not self._indexed_text else "No matches")
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
            if self._ask_dialog is None:
                self._ask_dialog = AskDialog(self)
                self._ask_dialog.asked.connect(self._ask)
            self._ask_dialog.show()
            self._ask_dialog.raise_()
            self._ask_dialog.activateWindow()
            self._ask_dialog.question.setFocus()

        def _ask(self, question: str):
            dlg = self._ask_dialog
            if dlg:
                dlg.start(question)
            self._run(self.engine.ask, question,
                      on_done=self._answered,
                      on_fail=self._ask_failed)

        def _answered(self, data):
            if self._ask_dialog:
                self._ask_dialog.show_answer(self._format_answer(data))

        def _ask_failed(self, msg: str):
            if self._ask_dialog:
                self._ask_dialog.show_answer(f"Something went wrong:\n\n{msg}")

        @staticmethod
        def _format_answer(data) -> str:
            mode = data.get("mode")
            if mode == "abstained":
                return ("This document does not appear to contain the answer.\n\n"
                        "RaggyEditor says so rather than guessing.")
            if mode == "retrieval_only":
                return ("No answer can be written without an API key, so here are the "
                        "relevant passages — press Cmd+F and search for the topic to "
                        "see them highlighted.\n\n"
                        "To enable written answers, add your key to the project's .env "
                        "file. DeepSeek works unchanged.")
            body = data.get("answer") or ""
            cites = data.get("citations") or []
            used = data.get("cited") or []
            if used:
                body += "\n\nFrom the document:"
                for i in used:
                    c = cites[i]
                    body += ("\n  line " + str(c["line"]) + ": "
                             + " ".join(c["text"].split())[:72])
            return body

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
            if self._confirm_discard():
                event.accept()
            else:
                event.ignore()

else:  # pragma: no cover

    class MainWindow:  # type: ignore
        pass

    class FindBar:  # type: ignore
        pass

    class AskDialog:  # type: ignore
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
