#!/usr/bin/env python3
"""
app.py — RaggyEditor, the desktop application.

A plain text editor. One document, one window, a find bar that slides in when
you ask for it — the way TextEdit works.

    Cmd+N    new document                     Cmd+O    open
    Cmd+S    save                             Cmd+Shift+S  save as
    Cmd+F    find                             Cmd+Alt+F    find and replace
    Cmd+G / Cmd+Shift+G   next / previous     Cmd+E    use selection for find
    Cmd+L    go to line                       Cmd+P    print
    Cmd+= / Cmd+- / Cmd+0   zoom in / out / actual size
    Cmd+Shift+A   ask a question about this document
    Esc      close the find bar

===============================================================================
ONE SEARCH BOX, TWO KINDS OF ANSWER
===============================================================================
The find field returns what every editor returns — the words you typed — AND the
passages that mean the same thing in different words. They arrive in one
navigable list, so there is no "semantic mode" to switch to and no second panel.

⚠️ ONLY THE EXACT MATCHES ARE REPLACEABLE. A "related" passage is a passage about
the same subject, not an occurrence of what you typed, so replacing it would
rewrite text you never searched for. Replace therefore operates on literal
matches only and is disabled (with the reason shown) when there are none.

===============================================================================
WHAT IS DELIBERATELY NOT HERE
===============================================================================
No "Model" menu, no encoder picker, no "LSA / ONNX / neural" jargon, no status
bar reporting cache statistics, no line-number gutter.

⚠️ The one user-facing need behind them — "can this find things by meaning?" — is
met by a single offer, made once, at the moment it matters: when a search finds
nothing by exact match. Also reachable as Help ▸ Enable Search by Meaning…

===============================================================================
OFFSETS
===============================================================================
The engine reports CHARACTER offsets; Scintilla stores BYTES (it runs UTF-8).
Every crossing of that boundary goes through raggy.offsets.OffsetMap — see that
module for what goes wrong when it does not.
"""

from __future__ import annotations

import os
import re
import sys
import time
import uuid

from raggy import model_store, recovery, textfile
from raggy.aiconfig import PROVIDERS, AIConfig
from raggy.engine import RaggyEngine
from raggy.offsets import OffsetMap
from raggy.printing import footer_text, paginate, rows_for_document, rows_per_page

try:
    from PyQt6 import Qsci
    from PyQt6.QtCore import QEvent, QSettings, Qt, QThread, QTimer, pyqtSignal
    from PyQt6.QtGui import QAction, QColor, QKeySequence, QPageSize, QPalette
    from PyQt6.QtWidgets import (
        QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
        QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow,
        QMessageBox, QPlainTextEdit, QProgressDialog, QPushButton, QToolButton,
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
MAX_RECENT = 10

FIND_PLACEHOLDER = "Find — words, or describe what you mean"
REPLACE_PLACEHOLDER = "Replace with…"


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

    #: Set RAGGY_SETTINGS_DIR to keep window/zoom/recents state out of the user's
    #: real preferences. The test suite uses it.
    SETTINGS_DIR_ENV = "RAGGY_SETTINGS_DIR"

    def app_settings() -> "QSettings":
        """The application's settings store.

        ⚠️ The environment override exists because `QSettings.setPath()` and
        `setDefaultFormat()` are NOT enough on macOS: Qt resolves
        `QSettings("Org", "App")` to NativeFormat there whatever the defaults say,
        so a test run writes zoom size and recent-file lists into the developer's
        real ~/Library/Preferences. That is how a test left `font_size = 72`
        behind and made the next print job come out 28 pages long.

        Passing an explicit INI path is the only reliable redirect.
        """
        override = os.environ.get(SETTINGS_DIR_ENV)
        if override:
            return QSettings(os.path.join(override, "raggyeditor.ini"),
                             QSettings.Format.IniFormat)
        return QSettings("RaggyEditor", "RaggyEditor")

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

    class TestAIConnection(QThread):
        """One tiny round-trip, to prove the settings work before saving them."""

        done = pyqtSignal(str)
        failed = pyqtSignal(str)

        def __init__(self, client):
            super().__init__()
            self.client = client

        def run(self):
            try:
                reply = self.client.chat(
                    "You are a connectivity check. Reply with the single word: OK",
                    "Reply with the single word: OK").strip()
                self.done.emit(reply[:80] or "(empty reply)")
            except Exception as e:                                  # noqa: BLE001
                self.failed.emit(f"{type(e).__name__}: {e}")

    # =========================================================================
    # SET UP AI ANSWERS
    # =========================================================================
    class AISetupDialog(QDialog):
        """Connect an LLM, so Ask can write answers instead of only retrieving.

        ⚠️ This dialog exists because the previous version told users "no API key
        is set" and left them to discover, on their own, that a key was even
        possible or how to supply one. Saying what is missing is not the same as
        offering to fix it.
        """

        saved = pyqtSignal()

        def __init__(self, config: AIConfig | None = None, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Set Up AI Answers")
            self.setMinimumWidth(620)
            self._config = config or AIConfig.load()
            self._test: TestAIConnection | None = None

            outer = QVBoxLayout(self)
            intro = QLabel(
                "Finding passages works on its own and always will.\n\n"
                "Writing a <b>cited answer</b> to a question needs a language model. "
                "Pick one below. A model running on this computer needs no key and "
                "nothing is sent anywhere.")
            intro.setWordWrap(True)
            outer.addWidget(intro)

            form = QFormLayout()
            self.provider = QComboBox()
            for key, meta in PROVIDERS.items():
                self.provider.addItem(meta["label"], key)
            idx = self.provider.findData(self._config.provider or "deepseek")
            self.provider.setCurrentIndex(max(0, idx))
            form.addRow("Where", self.provider)

            self.base_url = QLineEdit(self._config.base_url)
            self.base_url.setPlaceholderText("https://api.deepseek.com/v1")
            form.addRow("Address", self.base_url)

            self.model = QLineEdit(self._config.model)
            self.model.setPlaceholderText("deepseek-chat")
            form.addRow("Model", self.model)

            self.api_key = QLineEdit(self._config.api_key)
            self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
            self.api_key.setPlaceholderText("not needed for a local model")
            form.addRow("API key", self.api_key)
            outer.addLayout(form)

            self.key_note = QLabel("")
            self.key_note.setWordWrap(True)
            self.key_note.setStyleSheet("color: palette(mid); font-size: 12px;")
            outer.addWidget(self.key_note)

            row = QHBoxLayout()
            self.test_btn = QPushButton("Test Connection")
            self.test_btn.clicked.connect(self._test_connection)
            row.addWidget(self.test_btn)
            self.result_label = QLabel("")
            self.result_label.setWordWrap(True)
            row.addWidget(self.result_label, 1)
            outer.addLayout(row)

            self.buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Save
                | QDialogButtonBox.StandardButton.Cancel)
            self.buttons.accepted.connect(self._save)
            self.buttons.rejected.connect(self.reject)
            outer.addWidget(self.buttons)

            self.provider.currentIndexChanged.connect(self._provider_changed)
            self.base_url.textChanged.connect(self._update_note)
            # ⚠️ NOTE: the feedback label above is `result_label`, NOT `result`.
            # `QDialog.result()` is a real method that exec()/accept() rely on;
            # assigning an attribute called `result` silently replaces it and the
            # dialog's return value stops working.
            self._prefill_empty_fields()
            self._update_note()

        # ---------------------------------------------------------- internals
        def _prefill_empty_fields(self):
            """Fill blanks from the preset, without overwriting a saved config.

            The provider combo is set before the change signal is connected, so
            `_provider_changed` never runs for the initial selection. Without
            this, opening the dialog on a config that records only a provider
            (or a first run on a preset) leaves the fields empty.
            """
            meta = PROVIDERS.get(self.provider.currentData(), {})
            if not self.base_url.text() and meta.get("base_url"):
                self.base_url.setText(meta["base_url"])
            if not self.model.text() and meta.get("model"):
                self.model.setText(meta["model"])

        def _provider_changed(self):
            meta = PROVIDERS.get(self.provider.currentData(), {})
            if meta.get("base_url"):
                self.base_url.setText(meta["base_url"])
            if meta.get("model"):
                self.model.setText(meta["model"])
            self._update_note()

        def _current(self) -> AIConfig:
            return AIConfig(provider=self.provider.currentData() or "",
                            base_url=self.base_url.text().strip(),
                            model=self.model.text().strip(),
                            api_key=self.api_key.text().strip())

        def _update_note(self):
            cfg = self._current()
            if cfg.needs_key:
                self.key_note.setText(
                    "⚠️ The key is saved as plain text in "
                    "~/.config/RaggyEditor/ai.json, readable only by you. "
                    "Leave it blank here and set OPENAI_API_KEY in the environment "
                    "instead if you would rather not write it to disk.")
            else:
                self.key_note.setText(
                    "This address is on this computer, so no key is needed and "
                    "nothing leaves the machine.")

        def _test_connection(self):
            cfg = self._current()
            if not cfg.base_url or not cfg.model:
                self.result_label.setText("Fill in the address and the model first.")
                return
            self.test_btn.setEnabled(False)
            self.result_label.setText("Trying…")
            self._test = TestAIConnection(cfg.build_client())
            self._test.done.connect(lambda r: self._tested(f"Working. The model said: “{r}”"))
            self._test.failed.connect(lambda m: self._tested(f"Failed: {m}"))
            self._test.start()

        def _tested(self, message: str):
            self.test_btn.setEnabled(True)
            self.result_label.setText(message)

        def _save(self):
            cfg = self._current()
            if not cfg.base_url or not cfg.model:
                self.result_label.setText("An address and a model are required.")
                return
            if cfg.needs_key and not cfg.api_key:
                self.result_label.setText("An API key is required for this provider.")
                return
            cfg.save()
            self.saved.emit()
            self.accept()

    # =========================================================================
    # ASK DIALOG
    # =========================================================================
    class AskDialog(QDialog):
        """A question box and a large, resizable answer area.

        ⚠️ A QMessageBox is the obvious choice and the wrong one: it sizes itself
        to its content, so a long cited answer arrives in a cramped box.
        """

        asked = pyqtSignal(str)
        setupRequested = pyqtSignal()

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

            bottom = QHBoxLayout()
            # ⚠️ Shown only when no model is connected: this is the discovery path.
            self.setup_btn = QPushButton("Set Up AI Answers…")
            self.setup_btn.clicked.connect(self.setupRequested)
            self.setup_btn.hide()
            bottom.addWidget(self.setup_btn)
            bottom.addStretch(1)
            self.status = QLabel("")
            self.status.setStyleSheet("color: palette(mid); font-size: 12px;")
            bottom.addWidget(self.status)
            lay.addLayout(bottom)

            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(self.reject)
            lay.addWidget(buttons)

        def _emit(self):
            q = self.question.text().strip()
            if q:
                self.asked.emit(q)

        def set_ai_configured(self, configured: bool, description: str = ""):
            self.setup_btn.setVisible(not configured)
            self.status.setText("" if configured else
                                "No model connected — answers are limited to passages.")
            self.setWindowTitle("Ask About This Document"
                                + ("" if configured else " — not set up"))

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
        """The only search UI: a slim bar above the document, with an optional
        replace row that stays hidden until the user asks for it."""

        queryChanged = pyqtSignal(str)
        nextRequested = pyqtSignal()
        prevRequested = pyqtSignal()
        closed = pyqtSignal()
        replaceRequested = pyqtSignal(str, bool)      # (replacement, replace_all)

        def __init__(self):
            super().__init__()
            self.setFrameShape(QFrame.Shape.NoFrame)
            self.setStyleSheet(
                "FindBar { background: palette(window);"
                " border-bottom: 1px solid palette(mid); }")

            outer = QVBoxLayout(self)
            outer.setContentsMargins(10, 6, 10, 6)
            outer.setSpacing(6)

            row = QHBoxLayout()
            row.setSpacing(6)
            self.field = FindField()
            self.field.setPlaceholderText(FIND_PLACEHOLDER)
            self.field.setClearButtonEnabled(True)
            self.field.setMinimumWidth(280)
            self.field.textChanged.connect(self.queryChanged)
            self.field.returnPressed.connect(self._enter)
            self.field.escapePressed.connect(self.closed)
            row.addWidget(self.field, 1)

            self.status = QLabel("")
            self.status.setStyleSheet("color: palette(mid); font-size: 12px;")
            row.addWidget(self.status)

            self.case_btn = self._toggle("Aa", "Match case")
            self.word_btn = self._toggle("ab", "Whole words only")
            self.regex_btn = self._toggle(".*", "Regular expression")
            row.addWidget(self.case_btn)
            row.addWidget(self.word_btn)
            row.addWidget(self.regex_btn)

            self.prev_btn = self._chevron("\u2039", "Previous match", self.prevRequested)
            self.next_btn = self._chevron("\u203a", "Next match", self.nextRequested)
            row.addWidget(self.prev_btn)
            row.addWidget(self.next_btn)

            self.disclosure = QToolButton()
            self.disclosure.setText("Replace")
            self.disclosure.setCheckable(True)
            self.disclosure.setAutoRaise(True)
            self.disclosure.setToolTip("Show the replace field")
            self.disclosure.toggled.connect(self._toggle_replace)
            row.addWidget(self.disclosure)

            done = QToolButton()
            done.setText("Done")
            done.setAutoRaise(True)
            done.clicked.connect(self.closed)
            row.addWidget(done)

            outer.addLayout(row)

            # ---- replace row (hidden until asked for) ----
            self.replace_row = QWidget()
            rrow = QHBoxLayout(self.replace_row)
            rrow.setContentsMargins(0, 0, 0, 0)
            rrow.setSpacing(6)
            spacer = QWidget()
            spacer.setFixedWidth(2)
            rrow.addWidget(spacer)
            self.replace_field = QLineEdit()
            self.replace_field.setPlaceholderText(REPLACE_PLACEHOLDER)
            self.replace_field.setClearButtonEnabled(True)
            rrow.addWidget(self.replace_field, 1)
            self.replace_btn = QPushButton("Replace")
            self.replace_all_btn = QPushButton("Replace All")
            self.replace_btn.clicked.connect(lambda: self.replaceRequested.emit(
                self.replace_field.text(), False))
            self.replace_all_btn.clicked.connect(lambda: self.replaceRequested.emit(
                self.replace_field.text(), True))
            rrow.addWidget(self.replace_btn)
            rrow.addWidget(self.replace_all_btn)
            self.replace_row.hide()
            outer.addWidget(self.replace_row)

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

        def _toggle_replace(self, on: bool):
            self.replace_row.setVisible(on)
            if on:
                self.replace_field.setFocus()

        def show_replace(self, on: bool = True):
            self.disclosure.setChecked(on)
            self._toggle_replace(on)

        def set_replace_enabled(self, enabled: bool, why: str = ""):
            for b in (self.replace_btn, self.replace_all_btn):
                b.setEnabled(enabled)
            tip = "" if enabled else why
            self.replace_btn.setToolTip(tip)
            self.replace_all_btn.setToolTip(tip)
            self.replace_field.setToolTip(tip)

        def _enter(self):
            if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.prevRequested.emit()
            else:
                self.nextRequested.emit()

        def query(self) -> str:
            return self.field.text()

        def replacement(self) -> str:
            return self.replace_field.text()

        def set_status(self, text: str):
            self.status.setText(text)

    # =========================================================================
    # WINDOW
    # =========================================================================
    # =========================================================================
    # WINDOWS — one per document, the way TextEdit works
    # =========================================================================
    class WindowRegistry:
        """Holds the open editor windows.

        ⚠️ Something has to hold a Python reference to each window. Qt keeps a C++
        pointer for a shown widget, but not a Python reference to the wrapper: with
        nothing in a list here, a window can be collected while it is still on
        screen.

        ⚠️ ONE `geometry` KEY IS SHARED BY EVERY WINDOW. New windows therefore
        restore the remembered size and then step down and right, or every window
        would open in exactly the same place and the user would see only one.
        """

        CASCADE_STEP = 26
        #: After this many steps the cascade wraps, so windows do not march off the
        #: bottom-right of the screen on the tenth document.
        CASCADE_WRAP = 8

        def __init__(self):
            self.windows: list = []
            #: True once a window has existed. Closing the last one leaves the app
            #: running (macOS behaviour), and this is what distinguishes "the user
            #: closed everything" from "the app just started".
            self.ever_opened = False

        # ------------------------------------------------------------ querying
        def count(self) -> int:
            return len(self.windows)

        def pristine(self):
            """The first empty, untitled, unmodified window, or None.

            ⚠️ Searches ALL windows, not just "when there is exactly one".
            Requiring a single window meant that opening a second file while an
            empty Untitled window was already open created yet another window, and
            empty Untitled windows then pile up one per Open. Handing the file to
            the empty window is what TextEdit does, and it is the whole point of
            the check.
            """
            for win in self.windows:
                if win.is_pristine():
                    return win
            return None

        # ------------------------------------------------------------- opening
        def _cascade(self) -> int:
            return 0 if not self.windows else len(self.windows) % self.CASCADE_WRAP

        def open(self, path: str | None = None):
            """Create, show and keep a new window. Returns it."""
            win = MainWindow(path, cascade=self._cascade())
            self.windows.append(win)
            self.ever_opened = True
            win.show()
            return win

        def open_document(self, path: str):
            """Open a file, reusing an untouched window if there is one."""
            reuse = self.pristine()
            if reuse is not None:
                reuse.load_path(path)
                reuse.raise_()
                reuse.activateWindow()
                return reuse
            return self.open(path)

        # ------------------------------------------------------------- closing
        def forget(self, win) -> None:
            if win in self.windows:
                self.windows.remove(win)

        def close_all(self) -> None:
            """Close every window without tripping the unsaved-changes prompt."""
            for win in list(self.windows):
                win._mark_clean()
                win.close()

    class MainWindow(QMainWindow):

        def __init__(self, path: str | None = None, cascade: int = 0):
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
            self._offsets: OffsetMap | None = None
            self._settings = app_settings()
            self._meaning_offer_made = bool(
                self._settings.value("meaning_offer_made", False, type=bool))
            self.ai = AIConfig.load()
            self._apply_ai()

            # The document's on-disk format, preserved across a save. See
            # raggy.textfile for what goes wrong when it is not.
            self.doc_format = textfile.DEFAULT_FORMAT
            self._on_disk_format = textfile.DEFAULT_FORMAT
            # ⚠️ `editor.setModified(True)` IS A SILENT NO-OP in this binding — it
            # does not set QScintilla's modified flag (which is not SCI_GETMODIFY
            # either). Only a real edit marks the document dirty. So a change that
            # does not touch the buffer — converting line endings, restoring a
            # snapshot — needs to be recorded here, or the app will happily close
            # without saving it.
            self._extra_dirty = False
            self._untitled_key = uuid.uuid4().hex[:8]

            self._index_timer = QTimer(self)
            self._index_timer.setSingleShot(True)
            self._index_timer.setInterval(1200)
            self._index_timer.timeout.connect(self._index_document)

            self._find_timer = QTimer(self)
            self._find_timer.setSingleShot(True)
            self._find_timer.setInterval(180)
            self._find_timer.timeout.connect(self._run_search)

            # Autosave. Runs only while there are unsaved changes (see _snapshot).
            self._save_timer = QTimer(self)
            self._save_timer.setInterval(3000)
            self._save_timer.timeout.connect(self._snapshot)

            self._build_ui()
            self._build_menus()
            self._apply_theme()
            self._apply_font()
            self._watch_theme()
            self._restore_geometry(cascade)
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
            self.editor.setUtf8(True)          # ⚠️ byte offsets: see raggy.offsets
            self.editor.setWrapMode(Qsci.QsciScintilla.WrapMode.WrapWord)
            self.editor.setCaretLineVisible(False)

            # ⚠️ Zero ALL THREE margins. Setting margin 0 to zero is not enough:
            # QScintilla gives margin 1 (the symbol/bookmark margin) a default
            # width of 16px, which shows up as a grey gutter down the left edge.
            for m in (0, 1, 2):
                self.editor.setMarginWidth(m, 0)
            try:
                self.editor.SendScintilla(Qsci.QsciScintilla.SCI_SETMARGINLEFT, 0, 8)
                self.editor.SendScintilla(Qsci.QsciScintilla.SCI_SETMARGINRIGHT, 0, 8)
            except Exception:                                       # noqa: BLE001
                pass

            self.find_bar = FindBar()
            self.find_bar.hide()
            self.find_bar.queryChanged.connect(self._on_query_changed)
            self.find_bar.nextRequested.connect(self._next)
            self.find_bar.prevRequested.connect(self._prev)
            self.find_bar.closed.connect(self._close_find)
            self.find_bar.replaceRequested.connect(self._do_replace)
            self.find_bar.set_replace_enabled(False, "Search for something first")

            central = QWidget()
            col = QVBoxLayout(central)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(0)
            col.addWidget(self.find_bar)
            col.addWidget(self.editor, 1)
            self.setCentralWidget(central)
            self.setAcceptDrops(True)        # drop a file on the window to open it

            self.editor.textChanged.connect(self._on_text_changed)
            try:
                self.editor.modificationChanged.connect(self.setWindowModified)
            except Exception:                                       # noqa: BLE001
                pass

        def _build_menus(self):
            m = self.menuBar()

            f = m.addMenu("&File")
            self._act(f, "&New", QKeySequence.StandardKey.New, self.new_window)
            self._act(f, "&Open…", QKeySequence.StandardKey.Open, self.open_window)
            self.recent_menu = f.addMenu("Open &Recent")
            self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
            f.addSeparator()
            self._act(f, "&Save", QKeySequence.StandardKey.Save, self.save_file)
            self._act(f, "Save &As…", QKeySequence.StandardKey.SaveAs, self.save_as)
            self._act(f, "Re&vert to Saved", None, self.revert_to_saved)
            f.addSeparator()
            self._act(f, "&Export as PDF…", None, self.export_pdf)
            self._act(f, "Print Pre&view…", "Ctrl+Shift+P", self.print_preview)
            self._act(f, "&Print…", QKeySequence.StandardKey.Print, self.print_document)
            f.addSeparator()
            eol = f.addMenu("Line &Endings")
            self._eol_actions = {}
            _platform = {"LF": "Unix", "CRLF": "Windows", "CR": "old Mac"}
            for name, newline in (("LF", "\n"), ("CRLF", "\r\n"), ("CR", "\r")):
                action = QAction(name + "  (" + _platform[name] + ")", self)
                action.setCheckable(True)
                action.triggered.connect(lambda _=False, nl=newline: self.set_line_ending(nl))
                eol.addAction(action)
                self._eol_actions[name] = action
            self._update_line_ending_menu()
            f.addSeparator()
            self._act(f, "&Close", QKeySequence.StandardKey.Close, self.close)

            e = m.addMenu("&Edit")
            self._act(e, "&Undo", QKeySequence.StandardKey.Undo, self.editor.undo)
            self._act(e, "&Redo", QKeySequence.StandardKey.Redo, self.editor.redo)
            e.addSeparator()
            self._act(e, "Cu&t", QKeySequence.StandardKey.Cut, self.editor.cut)
            self._act(e, "&Copy", QKeySequence.StandardKey.Copy, self.editor.copy)
            self._act(e, "&Paste", QKeySequence.StandardKey.Paste, self.editor.paste)
            e.addSeparator()
            self._act(e, "Select &All", QKeySequence.StandardKey.SelectAll,
                      self.editor.selectAll)
            t = e.addMenu("&Transformations")
            self._act(t, "Make &Upper Case", "Ctrl+Shift+U",
                      lambda: self.transform_case("upper"))
            self._act(t, "Make &Lower Case", "Ctrl+Shift+L",
                      lambda: self.transform_case("lower"))
            self._act(t, "&Capitalize", None, lambda: self.transform_case("capitalize"))

            d = m.addMenu("&Find")
            self._act(d, "&Find…", QKeySequence.StandardKey.Find, self.show_find)
            self._act(d, "Find and &Replace…", "Ctrl+Alt+F", self.show_replace)
            self._act(d, "Find &Next", QKeySequence.StandardKey.FindNext, self._next)
            self._act(d, "Find &Previous", QKeySequence.StandardKey.FindPrevious, self._prev)
            self._act(d, "Use Selection for Find", "Ctrl+E", self._use_selection)
            self._act(d, "&Jump to Selection", "Ctrl+J", self.jump_to_selection)
            d.addSeparator()
            self._act(d, "&Go to Line…", "Ctrl+L", self.go_to_line)
            d.addSeparator()
            self._act(d, "&Ask a Question About This Document…", "Ctrl+Shift+A",
                      self.ask_document)

            v = m.addMenu("&View")
            self._act(v, "Zoom &In", "Ctrl+=", self.zoom_in)
            self._act(v, "Zoom &Out", "Ctrl+-", self.zoom_out)
            self._act(v, "&Actual Size", "Ctrl+0", self.zoom_reset)

            fmt = m.addMenu("F&ormat")
            self._font_action = self._act(fmt, "&Font…", None, self.choose_font)
            self._update_font_menu_label()

            h = m.addMenu("&Help")
            self._act(h, "Set Up AI Answers…", None, self.setup_ai)
            self._act(h, "Enable Search by Meaning…", None, self.enable_meaning_search)
            h.addSeparator()
            self._act(h, "&About " + APP_NAME, None, self.about)

        def _act(self, menu, text, shortcut, slot, checkable=False):
            a = QAction(text, self)
            a.setCheckable(checkable)
            if shortcut:
                a.setShortcut(shortcut if isinstance(shortcut, str)
                              else QKeySequence(shortcut))
            a.triggered.connect(slot)
            menu.addAction(a)
            return a

        # --------------------------------------------------------------- title
        def _update_title(self):
            """TextEdit shows the document NAME, not the application name.

            ⚠️ The `[*]` placeholder is required, not cosmetic: Qt substitutes it
            with the modified marker (the dot in the close button on macOS), and
            `setWindowModified()` silently does nothing without it.
            """
            name = os.path.basename(self.path) if self.path else UNTITLED
            self.setWindowTitle(name + "[*]")
            try:
                self.setWindowFilePath(self.path or "")
            except Exception:                                       # noqa: BLE001
                pass

        # ------------------------------------------------------------- theming
        def _is_dark(self) -> bool:
            """Qt 6.5+ reports the scheme directly; Unknown means ask the palette."""
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
                paper = QColor("#1e1e1e")
                ink = QColor("#e8e8e8")
                caret = QColor("#ffffff")   # ⚠️ bright: a dark caret is invisible
                sel_bg, sel_fg = QColor("#2f5fa8"), QColor("#ffffff")
                exact, related = 0x66D1FF, 0xFFA96A
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
                except Exception:                                       # noqa: BLE001
                    pass

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

            self._highlight()

        # --------------------------------------------------------------- font
        def _base_point_size(self) -> int:
            size = QApplication.font().pointSize()
            return size if size > 0 else 13

        def _apply_font(self):
            """The editor font = chosen family (or the system font) at the zoom size.

            ⚠️ Family and size come from DIFFERENT places on purpose. The Format
            menu sets the family; View ▸ Zoom sets the size. Reading the size back
            from the dialog as well is how the two end up disagreeing.
            """
            size = self._settings.value("font_size", 0, type=int) or self._base_point_size()
            family = self._settings.value("font_family", "", type=str)
            font = QApplication.font()
            if family:
                font.setFamily(family)
            if size > 0:
                font.setPointSize(size)
            self.editor.setFont(font)
            self._apply_theme()          # metrics changed; re-paint indicators

        def _set_font_size(self, size: int, remember: bool):
            size = max(8, min(72, size))
            self._settings.setValue("font_size", size if remember else 0)
            self._apply_font()

        def zoom_in(self):
            self._set_font_size(self.editor.font().pointSize() + 1, True)

        def zoom_out(self):
            self._set_font_size(self.editor.font().pointSize() - 1, True)

        def zoom_reset(self):
            self._settings.setValue("font_size", 0)
            self._apply_font()

        # ------------------------------------------------------------ offsets
        def _om(self) -> OffsetMap:
            """Char↔byte map for the current buffer, rebuilt when it changes."""
            if self._offsets is None or self._offsets.text != self.editor.text():
                self._offsets = OffsetMap(self.editor.text())
            return self._offsets

        def _select_chars(self, start: int, end: int):
            """Select a CHARACTER range (Scintilla wants bytes)."""
            om = self._om()
            S = Qsci.QsciScintilla
            self.editor.SendScintilla(S.SCI_SETSEL, om.to_byte(start), om.to_byte(end))

        def _sel_chars(self) -> tuple[int, int]:
            """The current selection as CHARACTER offsets."""
            om = self._om()
            S = Qsci.QsciScintilla
            a = self.editor.SendScintilla(S.SCI_GETSELECTIONSTART)
            b = self.editor.SendScintilla(S.SCI_GETSELECTIONEND)
            return om.to_char(a), om.to_char(b)

        def _replace_chars(self, start: int, end: int, text: str) -> int:
            """Replace a CHARACTER range with `text`, as one undoable edit."""
            om = self._om()
            S = Qsci.QsciScintilla
            blen = len(text.encode("utf-8"))
            self.editor.SendScintilla(S.SCI_SETTARGETSTART, om.to_byte(start))
            self.editor.SendScintilla(S.SCI_SETTARGETEND, om.to_byte(end))
            self.editor.SendScintilla(S.SCI_REPLACETARGET, blen, text.encode("utf-8"))
            self._offsets = None            # the buffer moved; the map is stale
            return blen

        # --------------------------------------------------------------- files
        def _confirm_discard(self) -> bool:
            """Ask about unsaved changes. True means 'go ahead'."""
            if not self._is_dirty():
                return True
            r = QMessageBox.question(
                self, APP_NAME, "Save changes before continuing?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Save:
                return self.save_file()          # may have been cancelled in Save As
            return r == QMessageBox.StandardButton.Discard

        def is_pristine(self) -> bool:
            """Untitled, unmodified and empty — safe to reuse for another document.

            ⚠️ Deliberately checks all three. An empty window the user has *typed
            into and then deleted* is still their document, and so is one they have
            converted to CRLF; `_is_dirty()` covers the cases QScintilla's own flag
            misses.
            """
            return (self.path is None and not self._is_dirty()
                    and not self.editor.text())

        # ------------------------------------------------------- new windows
        def new_window(self):
            """File ▸ New: a NEW window, leaving this document alone.

            ⚠️ Not `new_file()`, which resets THIS window. TextEdit gives each
            document its own window, and replacing the current one would throw away
            whatever the user had open — silently, if they had saved it.
            """
            win = windows.open()
            win.editor.setFocus()
            return win

        def open_window(self):
            """File ▸ Open: open the file in its own window.
            
            No unsaved-changes prompt is needed: nothing in this window is touched.
            """
            p, _ = QFileDialog.getOpenFileName(
                self, "Open", os.path.expanduser("~"),
                "Text files (*.txt *.md *.log *.csv *.json);;All files (*)")
            if p:
                target = windows.open_document(p)
                target.editor.setFocus()

        def new_file(self):
            if not self._confirm_discard():
                return
            self.path = None
            self._indexed_text = ""
            self._offsets = None
            # ⚠️ Reset the format too. Inheriting the previous document's encoding
            # and line endings means "New" after opening a Windows file silently
            # produces another Windows file.
            self.doc_format = textfile.DEFAULT_FORMAT
            self.editor.setText("")
            self.editor.setModified(False)
            self._mark_clean()
            self._update_title()
            self._apply_eol_mode()
            self._update_line_ending_menu()
            self._close_find()
            self.editor.setFocus()

        def load_path(self, path: str, warn_encoding: bool = True):
            """Open a file, remembering its exact on-disk format."""
            try:
                text, fmt, guessed = textfile.read(path)
            except Exception as e:                                  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Could not open:\n{e}")
                return
            self.editor.setText(text)
            self.path = path
            self.doc_format = fmt
            self._apply_eol_mode(fmt)
            self._update_line_ending_menu()   # or the menu keeps the previous file's
            self._indexed_text = ""
            self._offsets = None
            self.editor.setModified(False)
            self._mark_clean()
            self._update_title()
            self._remember_recent(path)
            self._index_document()
            if self.find_bar.isVisible() and self.find_bar.query():
                self._run_search()

            # ⚠️ Say so rather than pretending we understood the file. A wrong guess
            # means saving would write a different encoding back than we read.
            if guessed and warn_encoding:
                QMessageBox.information(
                    self, "Opened as " + fmt.encoding,
                    f"This file is not valid UTF-8, so it has been opened as "
                    f"{fmt.encoding}.\n\nIt will be saved back as {fmt.encoding} "
                    f"with {fmt.newline_name} line endings, unchanged.")

        def _apply_eol_mode(self, fmt=None):
            """Tell Scintilla which line ending this document uses, for display."""
            fmt = fmt or self.doc_format
            try:
                self.editor.setEolMode(textfile.EOL_MODE.get(fmt.newline, 2))
            except Exception:                                       # noqa: BLE001
                pass

        def save_file(self) -> bool:
            if not self.path:
                return self.save_as()
            try:
                # ⚠️ Writes the document's OWN encoding and newline style. A
                # plain open(..., "w") here is what silently turned Windows files
                # into LF and Latin-1 files into UTF-8.
                textfile.write(self.path, self.editor.text(), self.doc_format)
            except Exception as e:                                  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Could not save:\n{e}")
                return False
            self.editor.setModified(False)
            self._mark_clean()               # format changes count as saved too
            self._remember_recent(self.path)
            self._forget_snapshot()          # safely on disk; the net is not needed
            return True

        def save_as(self) -> bool:
            p, _ = QFileDialog.getSaveFileName(
                self, "Save As", os.path.expanduser("~"),
                "Text files (*.txt);;All files (*)")
            if not p:
                return False
            self.path = p
            self.editor.setModified(True)
            ok = self.save_file()
            if ok:
                self._update_title()
            return ok

        # ------------------------------------------------------- doc format
        def set_line_ending(self, newline: str):
            """Convert the document to a different newline style, deliberately."""
            if newline == self.doc_format.newline:
                return
            self.doc_format = textfile.TextFormat(self.doc_format.encoding, newline,
                                                  self.doc_format.bom)
            self._apply_eol_mode()
            self._mark_dirty()                # the file will differ from disk
            self._update_line_ending_menu()

        def _is_dirty(self) -> bool:
            """Has anything changed since the last save?

            ⚠️ Not just `editor.isModified()`: a format change or a restored
            snapshot never touches the buffer, and `setModified(True)` cannot mark
            it. See the note in __init__.
            """
            return bool(self.editor.isModified()) or self._extra_dirty

        def _mark_dirty(self):
            self._extra_dirty = True
            self.setWindowModified(True)

        def _mark_clean(self):
            """The document now matches what is on disk. Clears BOTH dirty flags.

            ⚠️ Clearing only `_extra_dirty` leaves QScintilla's own flag set, so the
            document still looks unsaved and the app prompts on close.
            """
            self.editor.setModified(False)
            self._extra_dirty = False
            self._on_disk_format = self.doc_format
            self.setWindowModified(False)

        def _update_line_ending_menu(self):
            for name, action in getattr(self, "_eol_actions", {}).items():
                action.setChecked(name == self.doc_format.newline_name)

        def revert_to_saved(self):
            """Throw away changes and reload the file from disk."""
            if not self.path:
                return
            if self._is_dirty():
                r = QMessageBox.question(
                    self, APP_NAME,
                    "Discard all changes and reload from disk?",
                    QMessageBox.StandardButton.Discard
                    | QMessageBox.StandardButton.Cancel)
                if r != QMessageBox.StandardButton.Discard:
                    return
            self.load_path(self.path, warn_encoding=False)
            self._forget_snapshot()

        # ------------------------------------------------------- autosave
        def _snapshot_slot(self) -> str:
            return recovery.slot_id(self.path, self._untitled_key)

        def _snapshot(self):
            """Write a recovery snapshot if — and only if — there is work to lose."""
            if not self._is_dirty():
                self._save_timer.stop()       # nothing to lose; save the wake-ups
                return
            text = self.editor.text()
            if not text.strip():
                return
            try:
                recovery.save(recovery.Snapshot(
                    slot=self._snapshot_slot(),
                    text=text,
                    path=self.path,
                    encoding=self.doc_format.encoding,
                    newline=self.doc_format.newline,
                    bom=self.doc_format.bom))
            except Exception:                                       # noqa: BLE001
                pass          # a failed safety net must never interrupt typing

        def _forget_snapshot(self):
            try:
                recovery.clear(self._snapshot_slot())
            except Exception:                                       # noqa: BLE001
                pass

        def restore_snapshot(self, snap):
            """Put a recovered document back in the editor."""
            self.editor.setText(snap.text)
            self.path = snap.path
            self.doc_format = snap.text_format()
            self._apply_eol_mode()
            self._indexed_text = ""
            self._offsets = None
            self._mark_dirty()                   # it is NOT what is on disk
            self._update_title()
            self._update_line_ending_menu()
            self._index_document()

        # ------------------------------------------------------- geometry
        def _restore_geometry(self, cascade: int = 0):
            saved = self._settings.value("geometry")
            if saved:
                try:
                    self.restoreGeometry(saved)
                except Exception:                                   # noqa: BLE001
                    pass
            if cascade:
                # ⚠️ Every window restores the SAME remembered rectangle, so without
                # this they would all land on top of one another and look like one
                # window. Step down and right instead, which is what TextEdit does.
                step = WindowRegistry.CASCADE_STEP * cascade
                self.move(self.x() + step, self.y() + step)

        def _save_geometry(self):
            try:
                self._settings.setValue("geometry", self.saveGeometry())
            except Exception:                                       # noqa: BLE001
                pass


        # ----------------------------------------------------------- printing
        def _editor_wraps(self) -> bool:
            try:
                return self.editor.wrapMode() != Qsci.QsciScintilla.WrapMode.WrapNone
            except Exception:                                       # noqa: BLE001
                return True

        def _document_name(self) -> str:
            return os.path.basename(self.path) if self.path else UNTITLED

        def _render_to_device(self, device) -> int:
            """Draw the document onto a printer/PDF device, editor layout intact.

            Uses the editor's font, wrap setting and tab width — the same three
            things that decide where a line breaks on screen — so a printed line
            number matches the line number in the window. See raggy.printing for
            why QTextDocument was the wrong tool here.

            Returns the number of pages written.
            """
            from PyQt6.QtGui import QFont, QPainter

            painter = QPainter(device)
            try:
                painter.setFont(QFont(self.editor.font()))
                fm = painter.fontMetrics()
                page = device.pageLayout().paintRectPixels(device.resolution())
                pad = max(8, page.width() // 40)          # a small, honest margin
                left = page.left() + pad
                max_width = page.width() - 2 * pad
                line_h = max(1, fm.lineSpacing())
                footer_h = int(fm.lineSpacing() * 2)
                top = page.top() + fm.ascent() + pad // 2

                rows = rows_for_document(
                    self.editor.text(), fm.horizontalAdvance, max_width,
                    wrap=self._editor_wraps(),
                    tab_width=max(1, self.editor.tabWidth()))
                per_page = rows_per_page(page.height(), line_h, footer_h)
                pages = paginate(rows, per_page)

                total = len(pages)
                name = self._document_name()
                for index, page_rows in enumerate(pages):
                    if index:
                        device.newPage()             # ⚠️ after the first page only
                    y = top
                    for row in page_rows:
                        if row:
                            painter.drawText(left, y, row)
                        y += line_h

                    painter.setFont(self._footer_font())
                    painter.setPen(self._footer_colour())
                    painter.drawText(
                        left, page.bottom() - fm.descent(),
                        footer_text(name, index + 1, total))
                    painter.setFont(QFont(self.editor.font()))
                return total
            finally:
                painter.end()

        @staticmethod
        def _footer_font():
            from PyQt6.QtGui import QFont
            font = QFont()
            font.setPointSize(max(7, font.pointSize() - 3))
            return font

        @staticmethod
        def _footer_colour():
            from PyQt6.QtGui import QColor
            return QColor("#666666")

        def print_document(self):
            from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
            printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
            dlg = QPrintDialog(printer, self)
            dlg.setWindowTitle("Print")
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._render_to_device(printer)

        def print_preview(self):
            """Show the pages before committing paper to them.

            ⚠️ This is nearly free because `_render_to_device` draws onto whatever
            paged device it is handed, and the preview dialog hands us a QPrinter
            through `paintRequested`. The preview therefore shows exactly what
            Print will produce — same layout code, not a second implementation.
            """
            from PyQt6.QtPrintSupport import QPrintPreviewDialog, QPrinter
            printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
            dlg = QPrintPreviewDialog(printer, self)
            dlg.setWindowTitle("Print Preview")
            dlg.paintRequested.connect(self._render_to_device)
            dlg.exec()

        def export_pdf(self):
            from PyQt6.QtGui import QPdfWriter
            suggested = os.path.expanduser("~")
            if self.path:
                suggested = os.path.splitext(self.path)[0] + ".pdf"
            p, _ = QFileDialog.getSaveFileName(self, "Export as PDF", suggested,
                                               "PDF files (*.pdf)")
            if not p:
                return
            if not p.lower().endswith(".pdf"):
                p += ".pdf"
            writer = QPdfWriter(p)
            writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
            try:
                self._render_to_device(writer)
            except Exception as e:                                  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Could not export:\n{e}")
                return
            self._remember_recent(p)

        # --------------------------------------------------------- recent files
        def _recent_paths(self) -> list[str]:
            raw = self._settings.value("recent", [])
            if isinstance(raw, str):
                raw = [raw]
            return [p for p in (raw or []) if isinstance(p, str)]

        def _remember_recent(self, path: str):
            items = [p for p in self._recent_paths() if p != path]
            items.insert(0, path)
            self._settings.setValue("recent", items[:MAX_RECENT])

        def _fill_recent_menu(self):
            self.recent_menu.clear()
            items = self._recent_paths()
            if not items:
                a = QAction("(nothing yet)", self)
                a.setEnabled(False)
                self.recent_menu.addAction(a)
                return
            for p in items:
                a = QAction(os.path.basename(p) + "  —  " + p, self)
                a.triggered.connect(lambda _=False, path=p: self._open_recent(path))
                self.recent_menu.addAction(a)
            self.recent_menu.addSeparator()
            clear = QAction("Clear Menu", self)
            clear.triggered.connect(lambda: self._settings.setValue("recent", []))
            self.recent_menu.addAction(clear)

        def _open_recent(self, path: str):
            if not os.path.exists(path):
                QMessageBox.information(self, APP_NAME,
                                        f"That file is no longer there:\n{path}")
                self._settings.setValue(
                    "recent", [p for p in self._recent_paths() if p != path])
                return
            # ⚠️ A new window, not this one: opening a file must not disturb a
            # document the user already has open, and so must not prompt either.
            windows.open_document(path)

        # --------------------------------------------------- transformations
        def transform_case(self, mode: str):
            start, end = self._sel_chars()
            if start == end:                      # no selection: whole document
                start, end = 0, len(self.editor.text())
            piece = self.editor.text()[start:end]
            if mode == "upper":
                new = piece.upper()
            elif mode == "lower":
                new = piece.lower()
            else:
                new = " ".join(w.capitalize() for w in re.split(r"(\s+)", piece))
            if new != piece:
                self._replace_chars(start, end, new)

        # ------------------------------------------------------------- indexing
        def _on_text_changed(self):
            self._offsets = None
            self._index_timer.start()
            if not self._save_timer.isActive():
                self._save_timer.start()      # autosave, while there are changes

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
            """Read via Scintilla positions then map bytes back to characters.

            ⚠️ Not `editor.selectedText()`: that returns an empty string in some
            QScintilla builds when the editor does not hold focus — which is
            exactly when the find bar has it.
            """
            try:
                a, b = self._sel_chars()
            except Exception:                                       # noqa: BLE001
                return ""
            text = self.editor.text()
            return text[a:b] if 0 <= a < b <= len(text) else ""

        def show_find(self, with_replace: bool = False):
            self.find_bar.show()
            if with_replace:
                self.find_bar.show_replace(True)
            sel = self._selected_text()
            if sel and "\n" not in sel and not self.find_bar.query():
                self.find_bar.field.setText(sel)
            self.find_bar.field.setFocus()
            self.find_bar.field.selectAll()
            if self.find_bar.query():
                self._run_search()

        def show_replace(self):
            self.show_find(with_replace=True)

        def _close_find(self):
            self.find_bar.hide()
            self.find_bar.show_replace(False)
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
                self.find_bar.set_replace_enabled(False, "Search for something first")
                return

            exact = self._exact_matches(query)
            if exact is None:
                self.find_bar.set_replace_enabled(False, "Fix the expression first")
                return                          # bad regex; message already shown
            related = self._related_passages(query)
            seen = {(r["start"], r["end"]) for r in exact}
            self._results = exact + [r for r in related
                                     if (r["start"], r["end"]) not in seen]

            # ⚠️ Only literal matches can be replaced. "Related" passages are
            # about the same subject, not occurrences of the query string, so
            # replacing them would rewrite text the user never searched for.
            self.find_bar.set_replace_enabled(
                bool(exact),
                "Nothing to replace — no exact matches for this search")

            self._highlight()
            self._update_status()
            if self._results:
                self._select_current()

            if not exact and self._should_offer_meaning():
                self._offer_meaning_search()

        def _exact_matches(self, query: str) -> list[dict] | None:
            flags = 0 if self.find_bar.case_btn.isChecked() else re.IGNORECASE
            pattern = query if self.find_bar.regex_btn.isChecked() else re.escape(query)
            if self.find_bar.word_btn.isChecked():
                # ⚠️ Lookarounds, not \b: \b is defined against word characters, so
                # searching for "(beta)" as a whole word with \b...(\)\b can never
                # match — the closing ")" is not a word character, so there is no
                # boundary after it. "Not preceded/followed by a word character" is
                # the rule people actually mean.
                pattern = r"(?<!\w)" + pattern + r"(?!\w)"
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

        # ------------------------------------------------------------- replace
        def _do_replace(self, replacement: str, replace_all: bool):
            exact = [r for r in self._results if r["kind"] == "match"]
            if not exact:
                self.find_bar.set_status("Nothing to replace — no exact matches")
                return
            targets = exact if replace_all else [self._results[self._current]]
            targets = [r for r in targets if r["kind"] == "match"]
            if not targets:
                self.find_bar.set_status("The current result is a related passage, "
                                         "not an exact match — nothing to replace")
                return

            omitted = len(self._results) - len(exact)
            S = Qsci.QsciScintilla
            self.editor.SendScintilla(S.SCI_BEGINUNDOACTION)
            try:
                # ⚠️ Back to front: replacing text shifts every offset after it.
                for r in sorted(targets, key=lambda r: r["start"], reverse=True):
                    self._replace_chars(r["start"], r["end"], replacement)
            finally:
                self.editor.SendScintilla(S.SCI_ENDUNDOACTION)

            n = len(targets)
            word = "replacement" if n == 1 else "replacements"
            self.find_bar.set_status(f"Replaced {n} {word}")
            self._index_timer.start()
            self._run_search()                 # offsets all moved; search afresh
            if omitted and not replace_all:
                pass                           # related passages are never replaced

        # -------------------------------------------------------- highlighting
        def _clear_indicators(self):
            S = Qsci.QsciScintilla
            length = self._om().byte_length
            try:
                for ind in (IND_EXACT, IND_RELATED):
                    self.editor.SendScintilla(S.SCI_SETINDICATORCURRENT, ind)
                    self.editor.SendScintilla(S.SCI_INDICATORCLEARRANGE, 0, length)
            except Exception:                                       # noqa: BLE001
                pass

        def _highlight(self):
            S = Qsci.QsciScintilla
            om = self._om()
            try:
                for r in self._results:
                    ind = IND_EXACT if r["kind"] == "match" else IND_RELATED
                    self.editor.SendScintilla(S.SCI_SETINDICATORCURRENT, ind)
                    start = om.to_byte(r["start"])
                    self.editor.SendScintilla(S.SCI_INDICATORFILLRANGE,
                                              start, om.to_byte(r["end"]) - start)
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
                self._select_chars(r["start"], r["end"])
                om = self._om()
                line = self.editor.SendScintilla(
                    S.SCI_LINEFROMPOSITION, om.to_byte(r["start"]))
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

        def go_to_line(self):
            total = self.editor.SendScintilla(Qsci.QsciScintilla.SCI_GETLINECOUNT)
            n, ok = QInputDialog.getInt(self, "Go to Line",
                                        f"Line number (1–{max(1, total)}):",
                                        1, 1, max(1, total))
            if ok:
                self.editor.SendScintilla(Qsci.QsciScintilla.SCI_GOTOLINE, n - 1)
                self.editor.setFocus()

        def jump_to_selection(self):
            """Scroll so the current selection is visible, without moving it.

            ⚠️ ensureLineVisible only ever scrolls the MINIMUM amount, so on a long
            jump it can leave the caret at the very edge of the viewport. Scrolling
            to the line first and then revealing the caret gives a little context.
            """
            S = Qsci.QsciScintilla
            try:
                start, end = self._sel_chars()
                if start == end:
                    return
                line = self.editor.SendScintilla(
                    S.SCI_LINEFROMPOSITION, self._om().to_byte(start))
                # A few lines of context above, then make sure the caret is shown.
                self.editor.SendScintilla(S.SCI_SETFIRSTVISIBLELINE, max(0, line - 3))
                self.editor.ensureLineVisible(line)
                self.editor.setFocus()
            except Exception:                                       # noqa: BLE001
                pass

        def choose_font(self):
            """Pick the document font. Size stays with the View menu's zoom."""
            from PyQt6.QtWidgets import QFontDialog
            font, ok = QFontDialog.getFont(self.editor.font(), self, "Document Font")
            if not ok:
                return
            # ⚠️ QFontDialog returns a size too. Zoom and the font size are the same
            # number, so taking the dialog's size as well would make the two
            # disagree the moment either changed. The dialog sets the family; zoom
            # owns the size.
            family = font.family()
            self._settings.setValue("font_family", family)
            self._apply_font()
            self._update_font_menu_label()

        def _update_font_menu_label(self):
            action = getattr(self, "_font_action", None)
            if action:
                action.setText("&Font…   (" + self._font_family() + ")")

        def _font_family(self) -> str:
            return self._settings.value("font_family", "", type=str) or "System"

        # ------------------------------------------------- meaning search offer
        def _should_offer_meaning(self) -> bool:
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

        # ------------------------------------------------------------ AI setup
        def _apply_ai(self):
            """Point the engine at the configured model.

            With nothing configured, `build_client()` still falls back to the
            OPENAI_* environment variables, so a user who prefers not to write a
            key to disk is not locked out.
            """
            self.engine.llm = self.ai.build_client()

        def setup_ai(self):
            dlg = AISetupDialog(self.ai, self)
            dlg.saved.connect(self._ai_saved)
            dlg.exec()

        def _ai_saved(self):
            self.ai = AIConfig.load()
            self._apply_ai()
            if self._ask_dialog:
                self._ask_dialog.set_ai_configured(self.ai.configured,
                                                   self.ai.describe())

        # ----------------------------------------------------------------- ask
        def ask_document(self):
            if self._ask_dialog is None:
                self._ask_dialog = AskDialog(self)
                self._ask_dialog.asked.connect(self._ask)
                self._ask_dialog.setupRequested.connect(self.setup_ai)
            self._ask_dialog.set_ai_configured(self.ai.configured, self.ai.describe())
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
                return ("No model is connected, so no answer can be written — here is "
                        "what the document says. The passages are ranked by relevance; "
                        "press Cmd+F and search for the topic to see them "
                        "highlighted.\n\n"
                        "To get written answers, choose Help ▸ Set Up AI Answers… — "
                        "a model on this computer needs no key and no account.")
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
            answers = self.ai.describe()
            QMessageBox.about(self, f"About {APP_NAME}", (
                f"<b>{APP_NAME}</b><br><br>"
                "A plain text editor that can find passages by meaning, as well as "
                "by the exact words you type.<br><br>"
                f"Answers: {answers}<br>"
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

        # ------------------------------------------------------------ dragging
        def dragEnterEvent(self, event):
            if event.mimeData().hasUrls() and any(
                    u.isLocalFile() for u in event.mimeData().urls()):
                event.acceptProposedAction()

        def dropEvent(self, event):
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    if self._confirm_discard():
                        self.load_path(url.toLocalFile())
                    event.acceptProposedAction()
                    return

        def closeEvent(self, event):
            if not self._confirm_discard():
                event.ignore()
                return
            # Saved, or the user chose to discard: the snapshot is no longer needed.
            self._forget_snapshot()
            self._save_timer.stop()
            self._save_geometry()
            windows.forget(self)
            event.accept()

    #: The open windows. Created after MainWindow so the two can refer to each
    #: other without a forward reference.
    windows = WindowRegistry()

else:  # pragma: no cover

    class MainWindow:  # type: ignore
        pass

    class FindBar:  # type: ignore
        pass

    class AskDialog:  # type: ignore
        pass

    class AISetupDialog:  # type: ignore
        pass


def _offer_recovery(win=None) -> None:
    """Offer unsaved work from a previous session, if any survived.

    ⚠️ Runs AFTER the window is shown, and only offers snapshots that are newer
    than what is on disk. Restoring a stale snapshot would silently roll the user
    backwards — worse than not offering at all.
    """
    try:
        pending = recovery.pending()
    except Exception:                                               # noqa: BLE001
        return
    if not pending:
        return

    lines = []
    for snap in pending[:8]:
        when = time.strftime("%H:%M", time.localtime(snap.taken_at))
        lines.append(f"  • {snap.name}   (unsaved at {when})")
    extra = len(pending) - len(lines)
    if extra > 0:
        lines.append(f"  …and {extra} more")

    r = QMessageBox.question(
        win, "Recover Unsaved Work",
        "RaggyEditor closed unexpectedly and has unsaved changes:\n\n"
        + "\n".join(lines)
        + "\n\nRecover the most recent one?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        | QMessageBox.StandardButton.Discard)
    if r == QMessageBox.StandardButton.Yes:
        # Restore into the empty window the app started with when there is one,
        # rather than leaving a stranded empty window next to the recovered one.
        target = win if (win is not None and win.is_pristine()) else windows.open()
        target.restore_snapshot(pending[0])
    elif r == QMessageBox.StandardButton.Discard:
        try:
            recovery.clear_all()
        except Exception:                                           # noqa: BLE001
            pass


class RaggyApplication(QApplication):
    """QApplication that reopens a window when the user has closed them all.

    ⚠️ The default, `quitOnLastWindowClosed=True`, quits the process as soon as the
    last document closes. On macOS that is wrong for a document application: the
    app should stay running with no windows, and clicking its Dock icon should
    bring a new document window back. It also means the recovery check is not run
    again on every Dock click, which is what would happen if the process restarted.
    """

    def __init__(self, argv):
        super().__init__(argv)
        self.setQuitOnLastWindowClosed(False)

    def event(self, event):                                     # noqa: D102
        if event.type() == QEvent.Type.ApplicationActivate and should_reopen_window():
            windows.open()
        return super().event(event)


def should_reopen_window() -> bool:
    """Should activating the application bring a window back?

    ⚠️ Pulled out of `RaggyApplication.event` so it can be tested: a second
    QApplication cannot be constructed in the test process, so the rule would
    otherwise have no coverage at all.

    ⚠️ `ever_opened` matters. Without it, the activation during startup would
    create a window in addition to the one main() is about to open.
    """
    return windows.ever_opened and windows.count() == 0


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
    app = RaggyApplication(argv)
    app.setApplicationName(APP_NAME)
    win = windows.open(path)
    if selftest:
        print("selftest: window constructed OK")
        QTimer.singleShot(0, app.quit)
        return app.exec()
    if not path:
        QTimer.singleShot(0, lambda: _offer_recovery(win))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
