# RaggyEditor

**A plain-text editor whose Find works by meaning.**

Every editor's Find matches *characters*. That is exactly right for
`ERR_CONN_4421` and exactly wrong for *"how do I stop the service falling over"* —
a question that shares no words with the passage that answers it.

RaggyEditor is a desktop text editor that keeps the ordinary Find and adds two
things on top of it:

| | What it matches | Example |
|---|---|---|
| **Find** `Ctrl+F` | characters (regex) | `ERR_CONN_4421` |
| **Semantic Find** `Ctrl+Shift+F` | meaning | *how do I stop it falling over?* |
| **Ask** `Ctrl+Alt+A` | a grounded answer with citations — or an honest refusal | *why is adding workers not helping?* |

It runs **fully offline** with no API key, ships as a **single downloadable
app**, and fetches its embedding model once, on request.

```
Ctrl+Shift+F  "how do I fix an expired certificate on a replica"
  → #1  line 61  conf 0.83   ERR_CONN_4422  The connection was established but the
                             handshake did not complete…
     (plain Find for "expired certificate": 0 matches)
```

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

Double-click, then **Model ▸ Download semantic model…** once. Semantic search
and Ask are ready.

> ⚠️ The build is **unsigned**, so macOS Gatekeeper and Windows SmartScreen will
> warn on first launch (right-click ▸ Open on macOS). See **Shipping** below.

### From source (developer)

```bash
./run.sh install      # .venv with the full app stack (engine + GUI + ONNX)
./run.sh app          # launch the editor
./run.sh demo         # offline engine tour — no GUI, no model, no key
./run.sh test         # 64 tests
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

✅ **64 tests pass** (`./run.sh test`), hermetic — no network, no model download.
✅ The offset invariant (`chunk.text == document[start:end]`) is asserted in the
chunker **and** tested for every strategy, so highlight-jump cannot drift.
✅ **The GUI is tested headlessly** (`tests/test_app.py`): window construction,
semantic Find, regex Find, click-to-select-the-exact-passage, low-confidence
flagging, and the edit-debounce timer.
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
