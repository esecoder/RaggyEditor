#!/usr/bin/env python3
"""
app.py — RaggyEditor, the desktop application.

This is the shippable product: one window, one program, no sidecar. The editor is
QScintilla (the same editing engine behind Notepad++ and SciTE), so the text
editing surface is not something we wrote or maintain; the RaggyEditor part is
the Find that works by meaning.

    Find (Ctrl+F)           characters — the ordinary search
    Semantic Find (Ctrl+Shift+F)   meaning
    Ask (Ctrl+Alt+A)        a grounded answer with citations, or an honest refusal

===============================================================================
WHY THE ENGINE RUNS IN-PROCESS HERE
===============================================================================
The CudaText plugin had to POST to a local server, because CudaText's embedded
Python cannot import numpy. This app IS Python, so the engine is imported
directly: no ports, no serialisation, no second process to keep alive. The
sidecar (`server.py`) is kept for the browser UI and any future integration.

⚠️ The encoder is loaded lazily and indexing runs off the UI thread. A full index
with the real encoder takes seconds on a large document, and a frozen window is
the difference between "slow" and "broken" to a user.

Run:
    ./run.sh app
    python -m raggy.app path/to/file.txt
"""

from __future__ import annotations

import os
import sys

from raggy import model_store
from raggy.engine import RaggyEngine

try:
    from PyQt6 import Qsci
    from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
    from PyQt6.QtGui import QAction, QFont, QKeySequence
    from PyQt6.QtWidgets import (
        QApplication, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
        QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QProgressDialog,
        QPushButton, QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget)
    HAS_QT = True
except Exception:                                                   # noqa: BLE001
    HAS_QT = False

APP_NAME = "RaggyEditor"


# =============================================================================
# BACKGROUND WORK — never block the UI thread
# =============================================================================
if HAS_QT:

    class Worker(QThread):
        """Runs one callable off the UI thread and reports the result or the error."""

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
        """Downloads the encoder model, reporting progress for a dialog."""

        progress = pyqtSignal(int, int)          # bytes done, bytes total
        done = pyqtSignal(str)
        failed = pyqtSignal(str)

        def run(self):
            try:
                def cb(_name, got, total):
                    self.progress.emit(got, total)
                path = model_store.download(progress=cb)
                self.done.emit(str(path))
            except Exception as e:                                  # noqa: BLE001
                self.failed.emit(f"{type(e).__name__}: {e}")


    # =========================================================================
    # THE WINDOW
    # =========================================================================
    class MainWindow(QMainWindow):

        def __init__(self, path: str | None = None):
            super().__init__()
            self.setWindowTitle(APP_NAME)
            self.resize(1200, 760)

            self.engine = RaggyEngine()
            self.path: str | None = None
            self._indexed_text = ""
            self._workers: list[QThread] = []
            self._index_timer = QTimer(self)
            self._index_timer.setSingleShot(True)
            self._index_timer.setInterval(1200)
            self._index_timer.timeout.connect(self.index_document)

            self._build_ui()
            self._build_menus()
            self._refresh_status()

            if path:
                self.load_path(path)
            else:
                self.hint("Open a file (Ctrl+O) or paste text, then search.")

        # ------------------------------------------------------------ UI
        def _build_ui(self):
            self.editor = Qsci.QsciScintilla()
            self.editor.setUtf8(True)
            try:
                self.editor.setMarginType(0, self._margin_number())
                self.editor.setMarginWidth(0, "0000")
                self.editor.setCaretLineVisible(True)
                self.editor.setWrapMode(Qsci.QsciScintilla.WrapMode.WrapWord)
            except Exception:                                       # noqa: BLE001
                pass                                                # cosmetic only

            split = QSplitter(Qt.Orientation.Horizontal)
            split.addWidget(self.editor)
            split.addWidget(self._build_panel())
            split.setSizes([760, 440])
            self.setCentralWidget(split)
            self.statusBar().showMessage("ready")

        def _margin_number(self):
            m = getattr(Qsci.QsciScintilla, "MarginType", None)
            return m.NumberMargin if m is not None else Qsci.QsciScintilla.NumberMargin

        def _build_panel(self) -> QWidget:
            panel = QTabWidget()

            # ---- Find tab ----
            find = QWidget()
            fv = QVBoxLayout(find)

            row = QHBoxLayout()
            self.mode = QComboBox()
            self.mode.addItems(["Semantic (by meaning)", "Regex / literal"])
            self.query = QLineEdit()
            self.query.setPlaceholderText("how do I stop it falling over?")
            self.query.returnPressed.connect(self.do_find)
            btn = QPushButton("Find")
            btn.clicked.connect(self.do_find)
            row.addWidget(self.mode)
            row.addWidget(self.query, 1)
            row.addWidget(btn)
            fv.addLayout(row)

            self.find_meta = QLabel(" ")
            self.find_meta.setWordWrap(True)
            fv.addWidget(self.find_meta)

            self.results = QListWidget()
            self.results.itemActivated.connect(self._activate_result)
            self.results.itemClicked.connect(self._activate_result)
            fv.addWidget(self.results, 1)
            self._result_data: list[dict] = []
            panel.addTab(find, "Find")

            # ---- Ask tab ----
            ask = QWidget()
            av = QVBoxLayout(ask)
            arow = QHBoxLayout()
            self.question = QLineEdit()
            self.question.setPlaceholderText("why does adding workers not help?")
            self.question.returnPressed.connect(self.do_ask)
            abtn = QPushButton("Ask")
            abtn.clicked.connect(self.do_ask)
            arow.addWidget(self.question, 1)
            arow.addWidget(abtn)
            av.addLayout(arow)

            self.ask_meta = QLabel(" ")
            self.ask_meta.setWordWrap(True)
            av.addWidget(self.ask_meta)

            self.answer = QTextEdit()
            self.answer.setReadOnly(True)
            self.answer.setFont(QFont("Menlo", 12))
            av.addWidget(self.answer, 1)
            panel.addTab(ask, "Ask")
            return panel

        def _build_menus(self):
            m = self.menuBar()

            f = m.addMenu("&File")
            self._act(f, "&Open…", QKeySequence.StandardKey.Open, self.open_file)
            self._act(f, "&Save", QKeySequence.StandardKey.Save, self.save_file)
            self._act(f, "Save &As…", QKeySequence.StandardKey.SaveAs, self.save_as)
            f.addSeparator()
            self._act(f, "&Quit", QKeySequence.StandardKey.Quit, self.close)

            e = m.addMenu("&Edit")
            self._act(e, "&Undo", QKeySequence.StandardKey.Undo, self.editor.undo)
            self._act(e, "&Redo", QKeySequence.StandardKey.Redo, self.editor.redo)
            e.addSeparator()
            self._act(e, "Cu&t", QKeySequence.StandardKey.Cut, self.editor.cut)
            self._act(e, "&Copy", QKeySequence.StandardKey.Copy, self.editor.copy)
            self._act(e, "&Paste", QKeySequence.StandardKey.Paste, self.editor.paste)
            e.addSeparator()
            self._act(e, "&Index document now", "Ctrl+I", self.index_document)

            d = m.addMenu("&Find")
            self._act(d, "&Find…", "Ctrl+F", lambda: self._focus_find(0))
            self._act(d, "&Semantic Find…", "Ctrl+Shift+F", lambda: self._focus_find(1))
            d.addSeparator()
            self._act(d, "&Ask the document…", "Ctrl+Alt+A", self._focus_ask)

            mm = m.addMenu("&Model")
            self._act(mm, "&Download semantic model…", None, self.download_model)
            self._act(mm, "Use &offline encoder (LSA)", None, lambda: self.set_encoder("lsa"))
            self._act(mm, "Use &semantic encoder (ONNX)", None, lambda: self.set_encoder("onnx"))
            self._act(mm, "&Automatic (recommended)", None, lambda: self.set_encoder("auto"))
            mm.addSeparator()
            self._act(mm, "Model &status", None, self.model_status)

            h = m.addMenu("&Help")
            self._act(h, "&About", None, self.about)

        def _act(self, menu, text, shortcut, slot):
            a = QAction(text, self)
            if shortcut:
                a.setShortcut(shortcut if isinstance(shortcut, str) else QKeySequence(shortcut))
            a.triggered.connect(slot)
            menu.addAction(a)

        # ------------------------------------------------------- file handling
        def open_file(self):
            p, _ = QFileDialog.getOpenFileName(self, "Open text file",
                                               os.path.expanduser("~"),
                                               "Text files (*.txt *.md *.log *.csv *.json);;All files (*)")
            if p:
                self.load_path(p)

        def load_path(self, path: str):
            try:
                self.editor.setText(open(path, "r", encoding="utf-8", errors="replace").read())
            except Exception as e:                                  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Could not open:\n{e}")
                return
            self.path = path
            self._indexed_text = ""
            self.setWindowTitle(f"{os.path.basename(path)} — {APP_NAME}")
            self.index_document()

        def save_file(self):
            if not self.path:
                return self.save_as()
            try:
                open(self.path, "w", encoding="utf-8").write(self.editor.text())
                self.statusBar().showMessage(f"saved {self.path}", 4000)
            except Exception as e:                                  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Could not save:\n{e}")

        def save_as(self):
            p, _ = QFileDialog.getSaveFileName(self, "Save as", os.path.expanduser("~"),
                                               "Text files (*.txt);;All files (*)")
            if p:
                self.path = p
                self.save_file()
                self.setWindowTitle(f"{os.path.basename(p)} — {APP_NAME}")

        # ---------------------------------------------------------- indexing
        def index_document(self):
            text = self.editor.text()
            if text == self._indexed_text:
                return
            self.statusBar().showMessage("indexing…")
            doc = os.path.basename(self.path) if self.path else "untitled"
            self._run(self.engine.index, text, doc, on_done=self._indexed, on_fail=self._index_failed)

        def _indexed(self, info):
            self._indexed_text = self.editor.text()
            reuse = ("reused all passages" if info["reencoded_chunks"] == 0
                     else f"re-encoded {info['reencoded_chunks']}, reused {info['reused_chunks']}")
            self.statusBar().showMessage(
                f"{info['n_chunks']} passages · {reuse} · {info['index_ms']} ms", 6000)
            self._refresh_status()

        def _index_failed(self, msg):
            self.statusBar().showMessage("index failed: " + msg, 8000)

        # ---------------------------------------------------------- searching
        def _focus_find(self, semantic: bool):
            self.mode.setCurrentIndex(1 if semantic else 0)
            self.query.setFocus()
            self.query.selectAll()

        def _focus_ask(self):
            tabs = self.centralWidget().findChild(QTabWidget)
            if tabs:
                tabs.setCurrentIndex(1)
            self.question.setFocus()

        def do_find(self):
            q = self.query.text().strip()
            if not q:
                return
            if not self._indexed_text:
                self.index_document()
                self.hint("Indexing the document — press Find again in a moment.")
                return
            semantic = self.mode.currentIndex() == 0
            try:
                data = (self.engine.semantic_search(q, k=20) if semantic
                        else self.engine.regex_search(q, regex=True))
            except Exception as e:                                  # noqa: BLE001
                self.hint(f"Find failed: {e}")
                return
            self._show_results(data, semantic)

        def _show_results(self, data, semantic: bool):
            self.results.clear()
            self._result_data = data.get("results", [])
            if data.get("error"):
                self.find_meta.setText(f"⚠️ {data['error']}")
                return
            for h in self._result_data:
                snippet = " ".join(h["text"].split())[:110]
                item = QListWidgetItem(f"#{h['rank']}  line {h['line']}   {snippet}")
                self.results.addItem(item)
            if semantic:
                conf = data.get("confidence", 0.0)
                warn = "  ⚠️ below threshold — maybe not in this document" if data.get("low_confidence") else ""
                self.find_meta.setText(
                    f"{len(self._result_data)} passages · confidence {conf:.2f}{warn}")
            else:
                self.find_meta.setText(f"{len(self._result_data)} matches for /{data.get('query','')}/")

        def _activate_result(self, item: QListWidgetItem):
            i = self.results.row(item)
            if 0 <= i < len(self._result_data):
                h = self._result_data[i]
                self._jump(h["start"], h["end"])

        def _jump(self, start: int, end: int):
            try:
                self.editor.SendScintilla(Qsci.QsciScintilla.SCI_SETSEL, start, end)
                line = self.editor.SendScintilla(Qsci.QsciScintilla.SCI_LINEFROMPOSITION, start)
                self.editor.ensureLineVisible(line)
                self.editor.setFocus()
            except Exception:                                       # noqa: BLE001
                pass

        # --------------------------------------------------------------- ask
        def do_ask(self):
            q = self.question.text().strip()
            if not q:
                return
            if not self._indexed_text:
                self.index_document()
                self.hint("Indexing the document — press Ask again in a moment.")
                return
            self.ask_meta.setText("thinking…")
            self._run(self.engine.ask, q, on_done=self._answered, on_fail=self._ask_failed)

        def _answered(self, data):
            mode = data.get("mode")
            if mode == "abstained":
                self.ask_meta.setText("not answered")
                self.answer.setPlainText(
                    f"This document does not appear to contain the answer.\n\n"
                    f"Reason: {data.get('reason')}\n\n"
                    "RaggyEditor refuses rather than guessing. Use Semantic Find to see "
                    "the closest passages.")
                return
            if mode == "retrieval_only":
                self.ask_meta.setText("no API key — showing passages instead")
                self.answer.setPlainText(
                    f"{data.get('reason')}\n\n"
                    "Set OPENAI_API_KEY to enable generated answers. DeepSeek works: it is "
                    "OpenAI-compatible for chat.")
                self._show_results({"results": data.get("citations", []),
                                    "confidence": data.get("confidence", 0.0),
                                    "low_confidence": False}, semantic=True)
                return
            self.ask_meta.setText(f"answered · confidence {data.get('confidence', 0):.2f}")
            self.answer.setPlainText(data.get("answer") or "")

        def _ask_failed(self, msg):
            self.ask_meta.setText("ask failed")
            self.answer.setPlainText(msg)

        # ------------------------------------------------------------- model
        def download_model(self):
            if model_store.is_available():
                self.model_status()
                return
            self.progress = QProgressDialog("Downloading the semantic model…", "Cancel", 0, 100, self)
            self.progress.setWindowTitle(APP_NAME)
            self.progress.setMinimumDuration(0)
            w = DownloadWorker()
            w.progress.connect(self._download_progress)
            w.done.connect(self._download_done)
            w.failed.connect(self._download_failed)
            self._keep(w)
            w.start()

        def _download_progress(self, got: int, total: int):
            if total and self.progress:
                self.progress.setValue(int(100 * got / total))

        def _download_done(self, path: str):
            if self.progress:
                self.progress.setValue(100)
                self.progress.close()
            self.statusBar().showMessage(f"model ready: {path}", 6000)
            self.set_encoder("onnx")

        def _download_failed(self, msg: str):
            if self.progress:
                self.progress.close()
            QMessageBox.warning(self, APP_NAME, f"Download failed:\n{msg}")

        def model_status(self):
            ready = model_store.is_available()
            from raggy.encoder import onnx_importable
            rows = [
                f"ONNX runtime available : {'yes' if onnx_importable() else 'no'}",
                f"Model downloaded       : {'yes' if ready else 'no'}",
                f"Model location         : {model_store.model_dir()}",
            ]
            try:
                rows.append(f"Active encoder         : {self.engine.encoder.name if self.engine.encoder else 'not indexed'}")
            except Exception:                                       # noqa: BLE001
                pass
            QMessageBox.information(self, f"{APP_NAME} — model status", "\n".join(rows))

        def set_encoder(self, kind: str):
            self.engine._encoder_kind = kind
            self.engine._resolved = None
            self._indexed_text = ""
            self.index_document()

        # ------------------------------------------------------------ helpers
        def _run(self, fn, *args, on_done, on_fail):
            w = Worker(fn, *args)
            w.done.connect(on_done)
            w.failed.connect(on_fail)
            self._keep(w)
            w.start()

        def _keep(self, w: QThread):
            # Hold a reference or Qt may garbage-collect the running thread.
            self._workers.append(w)
            w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)

        def hint(self, text: str):
            self.statusBar().showMessage(text, 8000)

        def _refresh_status(self):
            s = self.engine.status()
            enc = s.get("encoder") or "idle (resolves on index)"
            self.setStatusTip(f"encoder: {enc}")

        def about(self):
            QMessageBox.about(self, f"About {APP_NAME}", (
                f"<b>{APP_NAME}</b> — a text editor whose Find works by meaning.<br><br>"
                "Editing engine: QScintilla (the text engine behind Notepad++ and SciTE).<br>"
                "Retrieval: BM25 + dense embeddings fused with RRF, with cited answers.<br><br>"
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


def _hook_editor_edits(win: "MainWindow"):
    """Start the debounce timer whenever the document changes."""
    if not HAS_QT:
        return
    win.editor.textChanged.connect(lambda: win._index_timer.start())


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
    _hook_editor_edits(win)
    win.show()
    if selftest:
        # Verify a packaged build can construct its window, then exit. Used by
        # scripts/package.sh to prove the frozen bundle actually runs.
        print("selftest: window constructed OK")
        QTimer.singleShot(0, app.quit)
        return app.exec()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
