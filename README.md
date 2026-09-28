# RaggyEditor

**A plain-text editor whose Find works by meaning.**

Every text editor has Find. It matches *characters* — which is exactly right for
`ERR_CONN_4421` and exactly wrong for *"how do I stop the service falling over"*,
a question that shares no words with the passage that answers it.

RaggyEditor keeps the ordinary Find and adds two things over the document you
already have open:

| | What it matches | Example query |
|---|---|---|
| **Regex / literal** | characters | `ERR_CONN_4421` |
| **Semantic** | meaning | *how do I stop it falling over?* |
| **Ask** | a grounded, cited answer — or an honest refusal | *why is adding workers not helping?* |

It is the RAG pipeline (chunk → index → fuse → rerank → generate) applied to
**one document** instead of a web-scale corpus, and wired into a real editor's
Find. It runs offline with **one dependency (`numpy`)** and **no API key**.

```
$ ./run.sh demo
  query: 'how do I fix an expired certificate on a replica'
  literal 'expired certificate' -> 0 match(es)   <- Ctrl+F finds nothing useful
  semantic (confidence 0.81) -> the passage about the failed handshake:
    #1  line 61   ERR_CONN_4422 The connection was established but the handshake did not complete…
```

---

## The base editor: CudaText

RaggyEditor does not reinvent a text editor. It extends
**[CudaText](https://cudatext.github.io/)** — the leading open-source,
cross-platform (Windows / Linux / macOS), Notepad++-class plain-text editor:

- **BSD-3-Clause** — permissive, so it imposes nothing on this repo's licence.
- **A real Notepad-class editor**, not an IDE: tabs, encodings, macros, regex
  Find, sessions — the things a text editor is for.
- **An official Python 3 plugin API**, which is the reason it was chosen: our
  engine is Python, so the integration is a plugin, not a fork.

⚠️ We extend CudaText through its plugin API. We do **not** patch its Free Pascal
core — rebuilding that would bury the retrieval work (the actual point of the
project) under a compiler toolchain.

There is also a built-in **web editor UI** (`./run.sh serve`) that reuses the
same engine, so the project runs and demos even if CudaText is not installed.

---

## Measured: does semantic search actually beat Find?

`./run.sh eval` runs five methods over 19 labelled queries on the sample
handbook (9 paraphrase, 4 identifier, 6 unanswerable). **hit@5 / MRR, paraphrase
subset** — this is the number the project exists to move:

| method | paraphrase hit@5 | paraphrase MRR | identifier MRR |
|---|---:|---:|---:|
| `literal` (plain Ctrl+F) | **0.000** | 0.000 | 1.000 |
| `regex_any` (every word OR'd) | 0.556 | 0.213 | 1.000 |
| `bm25` (lexical) | 1.000 | 0.806 | 1.000 |
| `dense` (meaning only) | 0.889 – 1.000 | 0.648 – 0.796 | **0.708** |
| **`hybrid` (what ships)** | **0.889 – 1.000** | **0.750 – 0.833** | **1.000** |

Read it honestly:

- **A plain Find scores 0.000 on paraphrase.** It cannot do better — the words
  are not there. This is the entire motivation, in one number.
- **Dense-only retrieval loses on identifiers** (identifier MRR drops to 0.708
  with the real encoder): a transformer blurs `ERR_AUTH_1180` toward
  `ERR_CONN_4422`, exactly as predicted. **Fusion repairs it to 1.000.** That is
  why the app ships hybrid rather than "just embeddings".
- The ranges show the **real encoder vs the offline LSA stand-in**; the eval
  prints both, labelled. We never report a number without saying which encoder
  produced it.

### Abstention, the part most RAG demos skip

An eval set with only answerable questions makes a retriever that always returns
top-5 look perfect. Ours contains 6 out-of-scope questions, and the confidence
threshold is **calibrated**, not guessed (`eval.py` sweeps it):

```
  threshold : 0.45         balanced accuracy : 0.923
  abstains on UNanswerable (correct) : 100.0%
  abstains on answerable   (harmful) :  15.4%
```

⚠️ A measured surprise worth keeping: **embedding similarity is not the signal
that separates answerable from unanswerable.** On this corpus the neural top
cosine runs 0.61–0.77 for answerable questions and 0.42–0.68 for *out-of-scope*
ones — they overlap. Something always looks "similar". The signal that separates
is whether the document actually uses the query's vocabulary ("rare-term
coverage", 0.69 vs 0.17). So confidence is coverage-led, with similarity only as
a tiebreak. That is in `engine.py`, with the measurement next to it.

---

## Editing a large document: incremental re-index

You edit a 200-page document and press save. Re-chunking and re-embedding the
whole thing on every save is the obvious implementation and the wrong one. So the
cache is keyed by each passage's **text, not its position** — when an edit shifts
every later passage down, their text is unchanged and they are reused. Only the
passages whose text actually changed are recomputed.

`./run.sh bench` measures it (warm model, 400 passages, one paragraph edited):

| encoder | cold index (every save, today) | edit → incremental | speedup | re-encoded |
|---|---:|---:|---:|---:|
| **bge-small (real)** | 5,301 ms | **104 ms** | **51×** | 1 of 400 |
| LSA (offline fallback) | 7,144 ms | 7,870 ms | 0.9× | 400 of 400 |

Two things to read honestly:

- **The neural path is the win.** 400 passages re-encoded becomes 1; and a
  re-index with no change at all is **~9 ms** because nothing is encoded. Both
  `demo` and the editor UI now re-index automatically while you type.
- **LSA gets no speedup, and the table says so.** TF-IDF and the SVD basis are
  fit over the *whole corpus*, so adding one passage changes every vector — there
  is no per-passage vector to reuse. That is a property of LSA, not a bug, and it
  is a second reason the real encoder is the production path. (Its tokenisation
  is still cached; the SVD refit dominates.)

⚠️ **The correctness guarantee is the point, and it is tested.** An incremental
re-index must be *indistinguishable* from a cold rebuild of the same text — same
passages, same offsets, same ranking. `tests/test_incremental.py` asserts exactly
that, because the failure mode of a cache is not a crash: it is retrieval
returning results that look fine and are wrong.

⚠️ **Two things invalidate the cache**, both tested: switching to a **different
document**, and any change to the **encoder**. A vector is only meaningful for
the model that produced it; reusing vectors across two models is silently wrong.

The same work also stopped a real waste elsewhere: the encoder is now resolved
**once** and reused, instead of constructing a `SentenceTransformer` on every
re-index call.

---

## Architecture

```
   CudaText (editor)                 browser UI
        │  Plugins ▸ RaggyEditor         │
        └──────────────┬─────────────────┘
                       ▼   HTTP/JSON on localhost
        ┌──────────────────────────────────────────┐
        │  sidecar   src/raggy/server.py            │   stdlib http.server
        └──────────────────────┬───────────────────┘
                               ▼
        ┌──────────────────────────────────────────┐
        │  engine   src/raggy/engine.py             │
        │                                           │
        │   document ─► CHUNK (offset-preserving)   │
        │                 │                         │
        │                 ├─► BM25  ┐               │
        │                 └─► dense ┘─► RRF fusion  │
        │                              │            │
        │                              ▼            │
        │                    rerank / confidence    │
        │                              │            │
        │              ┌───────────────┴──────────┐ │
        │              ▼                          ▼ │
        │      ranked passages            grounded answer
        │      (+ exact offsets)          (+ citations, or abstain)
        └──────────────────────────────────────────┘

  dense = the real encoder (bge-small) when installed, else TF-IDF+SVD (LSA)
  answer = OpenAI-compatible LLM when a key is set, else retrieval-only
```

The editor surface is deliberately dumb (read buffer → POST → render JSON). All
the retrieval lives in `src/raggy/` where it can be tested without a GUI.

---

## Quickstart

```bash
./run.sh install     # creates .venv, installs numpy (the only hard dependency)
./run.sh demo        # offline tour over the sample document — no key needed
./run.sh eval        # the measured comparison above
./run.sh bench       # what incremental re-indexing saves
./run.sh test        # 48 tests, no network

# optional: the real local encoder (uses the cached bge-small model, ~130MB once)
./run.sh install-neural
```

### Use it in CudaText

1. Copy `cudatext_plugin/raggy_editor/` into your CudaText `py/` folder
   (see [`cudatext_plugin/README.md`](cudatext_plugin/README.md) for paths).
2. Restart CudaText → **Plugins ▸ RaggyEditor ▸ Start sidecar** (once).
3. Open any text file. **Ctrl+Alt+F** semantic find, **Ctrl+Alt+A** ask.

### Or in the browser

```bash
./run.sh serve       # http://127.0.0.1:8791
```

Click **Load sample**, ask a paraphrase, click a result to jump to the exact
passage. Same engine, no CudaText required.

### Or from the terminal

```bash
./run.sh search "how do I recover a replica from a disk snapshot"
./run.sh search "ERR_CONN_4421" --mode bm25
./run.sh ask "why does adding workers not help?"      # needs a key to generate
```

---

## Answers, keys, and honest degradation

Generate answers only needs an OpenAI-compatible endpoint. **DeepSeek works
unmodified** — it is OpenAI-compatible for `/chat/completions` (it has no
`/embeddings`, which is why embeddings always stay local).

```bash
cp .env.example .env      # then add your key
./run.sh serve
```

| Situation | What happens |
|---|---|
| API key set | Grounded answer with `[n]` citations back to passages the model actually used |
| No key | **Retrieval-only** — the ranked passages, and a message saying so. Never fabricated prose. |
| Question not in the document | **Abstains**: *"nothing in this document scores above the confidence threshold"* |
| `sentence-transformers` missing | Falls back to LSA and **labels it `lsa-tfidf-svd` everywhere** it shows results |

The grounding prompt (`llm.py`) forbids outside knowledge, requires `[n]`
citations, and gives the model an explicit `NOT_IN_DOCUMENT` exit — because
"the document does not say" must be an allowed answer.

---

## What is in this repo

| Path | What it is |
|---|---|
| [`src/raggy/chunking.py`](src/raggy/chunking.py) | Fixed / recursive / sentence chunking **that keeps document offsets**, so the editor can highlight the exact span |
| [`src/raggy/retrievers.py`](src/raggy/retrievers.py) | BM25, dense (LSA + neural), RRF fusion, a feature reranker, the metrics, and the **content-addressed chunk cache** |
| [`src/raggy/encoder.py`](src/raggy/encoder.py) | The encoder policy: `auto` / `neural` / `lsa`, and it names which one you got |
| [`src/raggy/engine.py`](src/raggy/engine.py) | Index a document (incrementally); semantic search; ask with citations; **calibrated abstention** |
| [`src/raggy/llm.py`](src/raggy/llm.py) | stdlib OpenAI-compatible client + the grounding prompt |
| [`src/raggy/server.py`](src/raggy/server.py) | The stdlib HTTP sidecar (`/api/index`, `/update`, `/search`, `/regex`, `/ask`, `/health`) |
| [`src/raggy/cli.py`](src/raggy/cli.py) | `demo` / `search` / `ask` from the terminal |
| [`src/raggy/eval.py`](src/raggy/eval.py) | The labelled evaluation + threshold calibration |
| [`src/raggy/bench.py`](src/raggy/bench.py) | The incremental-index cost benchmark |
| [`cudatext_plugin/`](cudatext_plugin/) | The CudaText plugin (thin HTTP client) |
| [`webui/index.html`](webui/index.html) | The browser editor, no build step |
| [`samples/`](samples/) | The operations handbook the demo and eval use |

---

## Verified / not verified

✅ Runs offline on macOS with `numpy` alone (48 tests, no network).
✅ The offset invariant (`chunk.text == document[start:end]`) is asserted in the
chunker **and** tested for every strategy — a highlight that is off by a few
characters is worse than none.
✅ Retrieval numbers above are produced by `./run.sh eval` on this machine.
✅ Abstention threshold calibrated, with the over-refusal rate reported.
✅ Incremental re-index is asserted **identical to a cold rebuild** (texts,
offsets, rankings), and both invalidation rules are tested.
✅ Traversal-protected static serving; the sidecar binds `127.0.0.1` only.

⚠️ **The CudaText plugin itself is not executed by the test suite** — CudaText is
not installed here. The engine, API, and web UI are all exercised; the plugin
follows the documented API (`Command` class, `method=` manifest, `ed.set_caret`)
and is the one part to verify by hand the first time you install it.

⚠️ The eval set is 19 queries on one document. It is enough to show the shape of
the result, not to publish a benchmark. **Re-run `./run.sh eval` on your own
document** — chunk size and threshold are corpus-specific, which is the whole
lesson of the RAG track.

---

## Production swaps

Everything here is built to be replaced one piece at a time:

| Local (this repo) | Production | Why |
|---|---|---|
| `http.server` sidecar | FastAPI / uvicorn | Async, workers, OpenAPI |
| BM25 from scratch | Elasticsearch / Tantivy / OpenSearch | Sharding, incremental updates |
| numpy flat index | **Qdrant / pgvector / Milvus** | HNSW/IVF, filtering, persistence |
| LSA stand-in | **bge-m3 / e5 / gte / Qwen3-Embedding** | The single biggest quality gain |
| Feature reranker | **bge-reranker-v2 / Cohere Rerank** | Real cross-encoders |
| Re-index the whole doc on save | Incremental, content-addressed updates | Long documents |

⚠️ **Re-index after changing the encoder, and version the index with the model
name.** Query vectors from model A against document vectors from model B produce
plausible-looking garbage: retrieval still returns results, they are just wrong,
and nothing errors. `SearchIndex.encoder_name` carries it for exactly this.

---

## Credits

The retrieval stack is adapted, for a single open document, from the
**AI-engineering manual** (`ai-engineer-learning/11-rag/`), by the same author —
same algorithms, same warnings. The editor it extends is
**[CudaText](https://github.com/Alexey-T/CudaText)** (BSD-3-Clause), used as a
host and not redistributed here.

MIT licensed — see [LICENSE](LICENSE).
