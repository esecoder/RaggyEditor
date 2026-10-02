# RaggyEditor

**A plain-text editor whose Find works by meaning.**

Every editor's Find matches *characters*. That is exactly right for
`ERR_CONN_4421` and exactly wrong for *"how do I stop the service falling over"* —
a question that shares no words with the passage that answers it.

RaggyEditor is a desktop text editor with **two search commands**, because one
command cannot honestly do both jobs:

| | | |
|---|---|---|
| `Cmd+F` | **Find…** | matches characters, as any editor does |
| `Cmd+Shift+F` | **Semantic Find…** | matches meaning |

Both use the same TextEdit-style bar above the document, and there is still no
side panel. `Enter` / `Shift+Enter` (or the chevrons) walk the results; **Done**
closes the bar.

⚠️ **THE TWO ARE SEPARATE, AND THEIR RESULTS ARE NEVER MIXED.** An earlier version
fused them into one box, and in use that was nonsense: the count read
`3 of 12 · 4 exact · 8 related`, one number adding up two unrelated things;
next/previous stepped between a literal hit and a passage from the other end of
the document; and searching for a string you could see on screen could land you
somewhere else entirely. So:

- **Find…** returns literal matches only — never a "close enough" passage. If the
  words are not there, it says `No matches`, which is the truth.
- **Semantic Find…** returns passages that resemble the query, and nothing that
  merely contains the string. It never replaces text: a passage that is *about*
  the subject is not an occurrence of it, and replacing one would rewrite
  something nobody searched for.
- The literal-only controls (regex, match case, whole word, Replace) are hidden in
  Semantic Find, because they mean nothing there.

```
Cmd+Shift+F  "how do I fix an expired certificate on a replica"
  Semantic  ·  1 of 21 related passages
  → jumps to line 61:  "ERR_CONN_4422  The connection was established but the
                        handshake did not complete…"
     (Find… for "expired certificate": No matches)
```

### Keyboard

| | |
|---|---|
| `Cmd+N` / `Cmd+O` / `Cmd+S` / `Cmd+Shift+S` | New window · Open · Save · Save As |
| `Cmd+Shift+R` | Rename the file on disk |
| `Cmd+F` | Find (literal / regex) |
| `Cmd+Shift+F` | Semantic Find (by meaning) |
| `Cmd+Alt+F` | Find and Replace |
| `Cmd+G` / `Cmd+Shift+G` | Find next / previous |
| `Cmd+E` | Use selection for find |
| `Cmd+L` | Go to line |
| `Cmd+J` | Jump to selection |
| `Cmd+B` / `Cmd+U` | Bold · Underline (see the caveat below) |
| `Cmd+=` / `Cmd+-` / `Cmd+0` | Zoom in · out · actual size |
| `Cmd+Shift+P` / `Cmd+P` | Print preview · Print (also File ▸ Export as PDF…) |
| `Esc` | Close the find bar |
| `Cmd+Shift+A` | Ask a question about this document |

**One document per window, the way TextEdit works.** `Cmd+N` opens a new window
rather than resetting the one you are in; **Open** and **Open Recent** hand the
file to an empty Untitled window if there is one and otherwise open a new window,
so an open document is never disturbed. New windows cascade instead of landing on
top of each other. Closing the last window leaves the app running — click its
Dock icon to get a window back (and `Cmd+Q` to quit).

⚠️ Every window shares one process-wide **neural encoder**, so ten open documents
do not load ten copies of the model. Only the neural encoder is shared: LSA is
fitted per document, and sharing it would leak one document's vocabulary into
another's results.

The window title is the **document name** — `Untitled` until you save — with the
macOS proxy icon and the unsaved-changes dot, exactly as TextEdit shows them.

The app follows the **system appearance**. Dark mode switches the pane, text,
caret and search highlights — QScintilla does not follow the application palette
on its own, so a black caret on a dark pane is the default failure mode and is
set explicitly.

### Replace, and why it only touches exact matches

The find bar has a **Replace** disclosure that reveals a replace row with
**Replace** and **Replace All**. A whole replace run is a single undo step.

⚠️ Replace operates on **literal matches only**. A "related" passage came back
because it is *about* the same subject, not because it contains what you typed —
rewriting it would edit text you never searched for. When a search returns only
meaning matches, the replace controls are disabled and say why.

The replacement text is inserted literally: `\1` is not expanded, even in regular
expression mode.

### Printing mirrors the editor

`Cmd+P` and **File ▸ Export as PDF…** render the document with the editor's own
**font, wrap setting and tab width** — the three things that decide where a line
breaks. The promise that matters: **with wrapping off, printed row N is document
line N**, so a printed "line 61" matches line 61 in the window. There is a small
page footer with the file name and `page n of m`.

⚠️ The first version handed the text to Qt's `QTextDocument`, which prints *the
text content* — it re-wraps at its own width, ignores the editor's wrap setting
and indentation, and gives no way to keep the two in step. `raggy/printing.py`
computes the layout instead, and takes the width measurement as an argument, so
the wrapping and paging rules are unit-tested without a window or a printer.

### Appearance: light, dark, or whatever the system says

**View ▸ Appearance** offers *Match the System*, *Light (white page)* and *Dark*,
and the choice is remembered. Light is a pure `#ffffff` page — verified by
sampling the rendered pixels, not by reading a colour constant.

⚠️ The pane and the dialogs are themed from **one** palette (`THEME` in
`app.py`), because QScintilla ignores the application palette entirely and is
coloured by hand. Before that, the two drifted apart: a dark pane next to dialogs
whose secondary text was `palette(mid)` — grey on grey. Two more traps live in
`_apply_theme` and are commented where they bite:

- QScintilla re-derives its widget **palette** while processing Scintilla
  messages, so the palette has to be set *last*, or the editor keeps a light
  `Base` inside a dark window: a bright seam around the text.
- Giving that widget a **style sheet** instead makes Qt re-resolve `palette()`
  from the sheet and silently discards the colours, so the editor gets a palette
  and no sheet.

### Bold and underline — and the honest caveat

`Cmd+B` and `Cmd+U` style the selection, and they combine. **A `.txt` file has
nowhere to record which characters are bold**, so this is on-screen emphasis: it
is not saved, and it does not survive reopening the document or printing it. That
is a property of the plain-text format, not a bug — TextEdit only persists
formatting because it can save Rich Text. Making it stick means saving as
RTF/HTML, which is a different feature; it is not implemented.

⚠️ The styles are numbers 40-42. Scintilla reserves 0-39 (`STYLE_DEFAULT` is 32,
line numbers 33), and a collision there shows up as unexplained underlining
somewhere else in the document. `STYLECLEARALL` is also skipped once the document
carries styles, because it resets every character to the default and would wipe
them on a theme change.

### Formulas are recognised, not typeset

Paste `E = mc^2`, `2H2 + O2 -> 2H2O`, `$\alpha + \beta$` or `\sqrt{x+1}` and the
notation under the caret is rendered in a strip below the editor — superscripts,
subscripts, Greek letters, operators, arrows and chemical formulae. **View ▸
Show Formula Preview** turns it off.

⚠️ **This is not a LaTeX engine.** A real one means matplotlib's mathtext (~40 MB,
deliberately excluded from the bundle), KaTeX or MathJax (QtWebEngine, larger
still). `raggy/formulas.py` is a recogniser and a presentational converter with no
dependencies at all, and it passes anything it does not understand through
**unchanged** rather than guessing.

The design rule is **under-match**. A missed formula is a cosmetic loss; a
sentence rewritten into symbols is a corrupted document. So `pi` becomes π only
inside recognised notation, `render()` defaults to a kind that changes nothing,
and `"The answer = what you get when you add them all up"` is deliberately not
a formula. Two regression tests in `tests/test_formulas.py` exist purely because
loose patterns swallowed the line *above* a formula (via `\s` matching a newline)
and merged two formulas in one paragraph.

**The text is never modified.** Detection returns character offsets; rendering
happens in the strip. The file keeps exactly what you typed.

### Renaming the document

`Cmd+Shift+R` renames the file on disk and everything follows it: the window
title, the recent-files list and the recovery slot. It takes a **name**, not a
path — a folder separator is refused rather than quietly turning "Rename" into
"move the file somewhere else". An untitled document falls back to Save As.

⚠️ **What this is not.** TextEdit lets you rename from the document chip in its
title bar. Qt gives a macOS window a document **proxy icon** (that is
`setWindowFilePath`, applied in `_update_title`) whose click popup macOS builds
itself — and Qt does not expose it, so a "Rename" entry cannot be added to it.
Making the title text itself an editable field would mean a frameless window with
a hand-built title bar, which would cost the native window chrome. So the command
lives in the File menu, and the proxy icon is what signals that the title is about
this file.

### Your file is not modified behind your back

Opening and saving a document reproduces its **exact format** — encoding, BOM and
line endings. `File ▸ Line Endings` converts deliberately (LF / CRLF / CR), and
that conversion counts as an unsaved change.

⚠️ Earlier versions read every file as UTF-8 and wrote it back the same way, so a
Windows file came back as LF and a Latin-1 file came back as UTF-8, with no
warning. A file that is not valid UTF-8 is now opened as Latin-1 and the app
**says so** rather than pretending it understood. See `raggy/textfile.py`; the
tests assert on **bytes**, because a str-level round trip hides both bugs.

### Autosave and crash recovery

While a document has unsaved changes the app writes a snapshot every few seconds
to `~/.config/RaggyEditor/recovery/` (mode `0600`, directory `0700` — it holds
your text in plain form). On the next launch it offers to restore.

⚠️ A snapshot is only offered when it is **newer than the file on disk**. If you
saved and *then* the app died, restoring would silently roll you backwards — a
data-loss bug wearing a safety net's clothes, so `recovery.pending()` enforces the
rule and a test pins it. An **untitled** document is always offered: it exists
nowhere else.

### Answers need a model — and the app says so

**Ask a Question About This Document…** (`Cmd+Shift+A`, under the Find menu) opens
a dialog, not a permanent pane. Retrieval is local and always works; **writing a
cited answer needs a language model.**

⚠️ Retrieval and generation are different things, and the 133 MB download is only
the first one:

| | Needs | Works offline |
|---|---|---|
| **Find** (words + meaning) | the embedding model (~133 MB) | ✅ |
| **Ask** (a written, cited answer) | an LLM — cloud key, or a model on this computer | depends |

So `Help ▸ Set Up AI Answers…` asks one question — *where should the model come
from?* — and offers:

| | |
|---|---|
| **DeepSeek** / **OpenAI** | paste a key; nothing else to install |
| **Ollama** / **LM Studio** | a model already running on this computer — no key, nothing sent anywhere |
| **Something else** | any OpenAI-compatible server |

There is a **Test Connection** button, so you find out immediately whether it
works rather than when you next need an answer. If no model is connected, the Ask
dialog shows a **Set Up AI Answers…** button instead of leaving you to guess.

The key is stored in `~/.config/RaggyEditor/ai.json` with mode `0600`. ⚠️ It is
**plain text and not encrypted** — file permissions are the only protection. Set
`OPENAI_API_KEY` in the environment instead if you would rather not write it to
disk.

Without a model, Ask returns the ranked passages and says plainly that no answer
can be written. It never invents prose.

### What is deliberately *not* in the UI

A text editor should not ask you to make decisions about its implementation. So:

| Not there | Why |
|---|---|
| Model / encoder menu (`ONNX`, `LSA`, `neural`) | Implementation words. The app picks the encoder itself. |
| "Download model" menu item | Replaced by a single offer, made once, at the moment a search finds nothing exactly. |
| Status bar ("re-encoded 1, reused 19 · 12 ms") | Cache statistics are not facts about your document. |
| Line-number gutter, wrap toggle | TextEdit has neither; wrap is always on. |

The View menu holds only zoom. The escape hatches for developers remain:
`RAGGY_ENCODER=lsa|onnx|neural` and `./run.sh install-model`.

---

## Built on a real editor

The editing surface is **QScintilla** — the Scintilla text engine behind
**Notepad++**, **SciTE** and **Geany**. We did not write a text editor; we
extended one, which is what "don't reinvent the wheel" should mean:

| Layer | What it is | Licence |
|---|---|---|
| Editing engine | **Scintilla / QScintilla** (unchanged, used as a library) | Scintilla: permissive; QScintilla: GPLv3 |
| GUI toolkit | **PyQt6** | GPLv3 |
| Retrieval + app | **RaggyEditor** (`src/raggy/`) — this project | GPLv3 |
| Encoder runtime | **ONNX Runtime** (no torch) | MIT |

---

## Why ONNX and not sentence-transformers

`sentence-transformers` is the easy way to run a real encoder and the wrong way
to *ship* one: it pulls in PyTorch, hundreds of megabytes. RaggyEditor serves the
same weights through **ONNX Runtime**, and the embeddings are identical — verified:

```
ONNX vs torch, cosine on the same texts:   per-doc [1.0, 1.0, 1.0]   query 1.0
```

Measured on this machine (bge-small-en-v1.5, 400 passages):

| | torch / sentence-transformers | **ONNX Runtime** |
|---|---:|---:|
| model load (one-time) | 11,004 ms | **135 ms** |
| runtime footprint | hundreds of MB | tens of MB |

The model is **not bundled**. It is downloaded once (~133 MB) into
`~/.cache/raggy-editor/` the first time you ask for it — and the app is already
fully usable before that, running the offline LSA encoder. So the download is an
upgrade, never a prerequisite.

---

## Install and run

### As a user (packaged app)

```bash
./run.sh package --dmg      # macOS -> dist/RaggyEditor.dmg  (~57 MB)
./run.sh package            # Windows -> dist/RaggyEditor/RaggyEditor.exe
                            # Linux   -> dist/RaggyEditor/RaggyEditor
```

Double-click and start typing. Find works immediately. The first time a search
matches nothing exactly, the app offers — once — to download the language model
(~133 MB) that lets it also find passages by meaning. You can also trigger it
from **Help ▸ Enable Search by Meaning…**.

> ⚠️ The build is **unsigned**, so macOS Gatekeeper and Windows SmartScreen will
> warn on first launch (right-click ▸ Open on macOS). See **Shipping** below.

### From source (developer)

```bash
./run.sh install      # .venv with the full app stack (engine + GUI + ONNX)
./run.sh app          # launch the editor
./run.sh demo         # offline engine tour — no GUI, no model, no key
./run.sh test         # 323 tests
```

### Just the engine, no Qt

```bash
./run.sh install-core     # numpy only
./run.sh search "how do I recover a replica from a disk snapshot"
./run.sh ask    "why does adding workers not help?"     # needs a key to generate
./run.sh eval             # the measured comparison below
./run.sh bench            # the incremental-index cost below
```

---

## Measured: does semantic search actually beat Find?

`./run.sh eval` runs five methods over 19 labelled queries (9 paraphrase, 4
identifier, 6 unanswerable) on the sample handbook. **hit@5 / MRR, paraphrase
subset** — the number the project exists to move:

| method | paraphrase hit@5 | paraphrase MRR | identifier MRR |
|---|---:|---:|---:|
| `literal` (plain Find) | **0.000** | 0.000 | 1.000 |
| `regex_any` (every word OR'd) | 0.556 | 0.213 | 1.000 |
| `bm25` (lexical) | 1.000 | 0.806 | 1.000 |
| `dense` (meaning only) | 0.889 – 1.000 | 0.648 – 0.796 | **0.708** |
| **`hybrid` (what ships)** | **0.889 – 1.000** | **0.750 – 0.833** | **1.000** |

Read it honestly:

- **Plain Find scores 0.000 on paraphrase.** It cannot do better; the words are
  not there. That is the entire motivation, in one number.
- **Dense-only loses on identifiers** — identifier MRR drops to 0.708 because a
  transformer blurs `ERR_AUTH_1180` toward `ERR_CONN_4422`. **Fusion repairs it
  to 1.000**, which is why the app ships hybrid rather than "just embeddings".
- The ranges are the offline LSA fallback vs the real ONNX encoder; the eval
  prints both and names which produced each number.

### Abstention — the part most RAG demos skip

An eval set with only answerable questions makes a retriever that always returns
top-5 look perfect. Ours has 6 out-of-scope queries and the confidence threshold
is **calibrated**, not guessed:

```
  threshold : 0.45          balanced accuracy : 0.923
  abstains on UNanswerable (correct) : 100.0%
  abstains on answerable   (harmful) :  15.4%
```

⚠️ A measured surprise that shaped the design: **embedding similarity is not what
separates answerable from unanswerable.** Neural top-cosine runs 0.61–0.77 for
answerable and 0.42–0.68 for *out-of-scope* — they overlap. Something always
looks "similar". The signal that separates is whether the document actually uses
the query's vocabulary (0.69 vs 0.17), so confidence is coverage-led with
similarity only as a tiebreak. The measurement sits next to the code.

---

## Measured: editing a large document

Re-indexing the whole document on every save is the obvious implementation and
the wrong one. The chunk cache is keyed by each passage's **text, not its
position**, so an edit that shifts every later passage still reuses them.

`./run.sh bench` (400 passages, one paragraph edited, warm model):

| encoder | cold index | edit → incremental | speedup | re-encoded |
|---|---:|---:|---:|---:|
| **ONNX (real)** | 4,599 ms | **35 ms** | **133×** | 1 of 400 |
| LSA (offline fallback) | 554 ms | 589 ms | 0.9× | 400 of 400 |

- A re-index with **no change** is **7 ms**, because nothing is encoded.
- **LSA gets no speedup, and the table says so**: TF-IDF and the SVD basis are
  fit corpus-wide, so an edit changes every vector. A property of LSA, not a bug.
- ⚠️ **The correctness guarantee is tested**: an incremental re-index must be
  *indistinguishable* from a cold rebuild — same passages, offsets and ranking.
  A cache's failure mode is not a crash; it is results that look fine and are
  wrong.

---

## Architecture

```
        RaggyEditor.app  (PyQt6 + QScintilla, one process)
                            │
        open document ──────┴──────────────────────────────
                            ▼
                ┌───────────────────────────────┐
                │  engine  (src/raggy/engine.py) │
                │                                │
                │  CHUNK  (offset-preserving)    │
                │    ├─► BM25      ┐             │
                │    └─► dense     ┘─► RRF       │
                │            (ONNX or LSA)       │
                │              ▼                 │
                │      rerank / confidence       │
                │        ├──► ranked passages    │  → exact offsets → editor highlight
                │        └──► cited answer, or   │  → OpenAI-compatible LLM (optional)
                │             abstain            │
                └───────────────────────────────┘
```

Everything except `app.py` runs on **numpy alone**, so the retrieval core is
testable without Qt and without a model.

The engine also runs as:
- a **CLI** (`./run.sh search / ask / demo / eval / bench`),
- a **stdlib HTTP sidecar + browser UI** (`./run.sh serve`),
- and historically a **CudaText plugin** (`cudatext_plugin/`, kept as an optional
  integration for people who already use CudaText).

They are all thin clients over the same engine.

---

## Packaging and shipping

`./run.sh package` freezes the app with PyInstaller using
`packaging/raggyeditor.spec`, which **explicitly excludes torch, transformers and
sentence-transformers** — if they happen to be installed in the build
environment, PyInstaller would otherwise bundle hundreds of megabytes of them.

The build then runs the frozen binary with `--selftest`, which constructs the
window and exits. *A build that produces a file is not the same as a build that
produces a working app*, and the difference is usually a missing Qt plugin.

| Output | Size |
|---|---|
| `dist/RaggyEditor.app` (macOS, uncompressed) | 289 MB |
| `dist/RaggyEditor.dmg` (**the download**) | **57 MB** |

⚠️ **Unsigned builds warn.** To ship without warnings you need, per platform:

- **macOS**: an Apple Developer ID, `codesign --deep --options runtime`, then
  `notarytool submit` + `stapler staple`. Without it, macOS refuses to open the
  app on first run (right-click ▸ Open bypasses it for testers).
- **Windows**: an Authenticode certificate, or SmartScreen warns.
- **Linux**: ship the AppImage or tarball; no signing authority.

⚠️ **Each OS builds its own artifact.** PyInstaller does not cross-compile — a
Windows `.exe` must be built on Windows, a Linux binary on Linux. A CI matrix
(GitHub Actions `macos-latest` / `windows-latest` / `ubuntu-latest`) is the usual
answer.

---

## Platform notes (learned the hard way)

- **onnxruntime is pinned `<1.20`.** Version 1.20+ wheels are built for macOS
  13.4+, and on macOS 13.0 they fail to *load*:
  `Symbol not found: __ZNSt3__18to_charsEPcS0_d`. 1.19.2 works with numpy 2.x.
- **`certifi` is required for the model download.** Python installed from
  python.org on macOS does not use the system keychain, so a plain
  `urlopen` fails with `CERTIFICATE_VERIFY_FAILED` even though `curl` works.
  We pass certifi's CA bundle explicitly rather than disabling verification —
  accepting an unverified model file would be worse than failing.
- **The encoder is loaded lazily** and indexing runs off the UI thread. A frozen
  window is the difference between "slow" and "broken" to a user.

---

## Verified / not verified

✅ **323 tests pass** (`./run.sh test`), hermetic — no network, no model download,
and the app's settings redirected into a scratch directory via
`RAGGY_SETTINGS_DIR` so nothing touches your real preferences. ⚠️
`QSettings.setPath()` is NOT enough on macOS: Qt ignores it and writes to
`~/Library/Preferences` anyway, which is how a test once left `font_size = 72`
behind.
✅ The offset invariant (`chunk.text == document[start:end]`) is asserted in the
chunker **and** tested for every strategy, so highlight-jump cannot drift.
✅ ⚠️ **Character offsets are converted to UTF-8 byte offsets** at the Scintilla
boundary (`raggy/offsets.py`). This was a real bug: the editor highlighted the
wrong text in any document containing a non-ASCII character before the match.
The old tests missed it because they compared the *numbers* the engine produced
with the numbers Scintilla echoed back — both read "25" while the editor selected
different characters. The regression test asserts on the **selected text**.
✅ **The GUI is tested headlessly** (`tests/test_app.py`): the single-pane layout
is guarded (a test fails if a tab widget or splitter returns), the absence of a
Model menu and a status bar, bar visibility, exact+related results in one list,
regex toggle, bad-regex reporting, next/previous wrapping, use-selection-for-find,
jump-to-exact-span, replace (all/one, disabled for meaning-only results, correct
after multibyte text), zoom, go-to-line, transformations, recent files, the AI
setup dialog, and the edit-debounce timer.
✅ Incremental re-index is asserted **identical to a cold rebuild**, and both
cache-invalidation rules (different document, different encoder) are tested.
✅ **ONNX embeddings equal torch embeddings** (cosine 1.00000).
✅ The **packaged `.app` builds and its frozen selftest passes**; `.dmg` produced.
✅ Retrieval and cost numbers above are produced by `./run.sh eval` and
`./run.sh bench` on this machine.

⚠️ **Not verified:** Windows and Linux packaging (only macOS was built here);
code signing / notarisation; and the legacy CudaText plugin, which is not
executed by the test suite.

⚠️ The eval set is 19 queries on one document. Enough to show the shape of the
result, not to publish a benchmark. **Re-run `./run.sh eval` on your document** —
chunk size and threshold are corpus-specific.

---

## Licensing and credits

RaggyEditor is **GPL-3.0-or-later** (see [LICENSE](LICENSE)). That is a
consequence of the stack, not a preference: **PyQt6 and QScintilla are GPLv3**,
and linking them makes the combined work GPLv3. The permissive parts:

- **Scintilla** (the text engine) is under a permissive licence.
- **ONNX Runtime**, **numpy**, **tokenizers** are MIT/Apache-2.0.
- **CudaText**, whose plugin is kept in this repo, is BSD-3-Clause.

⚠️ If you need RaggyEditor under a **permissive** licence, the swap is
`PyQt6 → PySide6` (LGPLv3) plus Qt's built-in `QPlainTextEdit` instead of
QScintilla — at the cost of the Scintilla editing surface. Everything in
`src/raggy/` except `app.py` has no Qt dependency at all and can be MIT on its
own.

The retrieval stack is adapted for a single open document from the
**AI-engineering manual** (`ai-engineer-learning/11-rag/`), by the same author —
same algorithms, same warnings.

---

## Repository layout

| Path | What it is |
|---|---|
| [`src/raggy/app.py`](src/raggy/app.py) | **The desktop app** (PyQt6 + QScintilla) |
| [`src/raggy/chunking.py`](src/raggy/chunking.py) | Offset-preserving chunking (fixed / recursive / sentence) |
| [`src/raggy/retrievers.py`](src/raggy/retrievers.py) | BM25, dense, RRF, reranker, metrics, **IndexCache** |
| [`src/raggy/onnx_encoder.py`](src/raggy/onnx_encoder.py) | The ONNX sentence encoder (CLS pooling, query prefix) |
| [`src/raggy/model_store.py`](src/raggy/model_store.py) | Download-on-first-use cache for the model |
| [`src/raggy/encoder.py`](src/raggy/encoder.py) | Encoder policy: `auto` / `onnx` / `neural` / `lsa` |
| [`src/raggy/engine.py`](src/raggy/engine.py) | Index, search, ask, **calibrated abstention** |
| [`src/raggy/llm.py`](src/raggy/llm.py) | stdlib OpenAI-compatible client + grounding prompt |
| [`src/raggy/eval.py`](src/raggy/eval.py) · [`bench.py`](src/raggy/bench.py) | The measurements |
| [`src/raggy/server.py`](src/raggy/server.py) · [`webui/`](webui/) | Alternative browser UI (no Qt needed) |
| [`packaging/`](packaging/) · [`scripts/package.sh`](scripts/package.sh) | PyInstaller build |
| [`cudatext_plugin/`](cudatext_plugin/) | Optional CudaText integration |
| [`samples/`](samples/) | The handbook the demo and eval use |
