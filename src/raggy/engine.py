#!/usr/bin/env python3
"""
engine.py — the piece the editor talks to.

One `RaggyEngine` holds one open document and answers three kinds of question
about it:

    regex_search(pattern)     the ordinary Find, but returning offsets
    semantic_search(query)    "find the passage about X", no shared words needed
    ask(question)             a grounded, cited answer — or an honest abstention

===============================================================================
THE PART THAT MAKES IT A RAG SYSTEM AND NOT A DEMO
===============================================================================
1. PROVENANCE. Every result carries `start`/`end`/`line`. The editor highlights
   the exact span; nothing is re-derived from a re-joined string.

2. ABSTENTION. `ask` computes a confidence and, below threshold, REFUSES to
   generate. ⚠️ The most-skipped part of RAG: a retriever that always returns
   its top-k looks perfect on an eval set that contains only answerable
   questions, and feeds an LLM irrelevant context on every question the document
   cannot answer. The threshold is calibrated in `eval.py`, not guessed.

3. HONEST DEGRADATION. No API key -> retrieval-only results, not a fake answer.
   No sentence-transformers -> LSA, and the encoder name says so.

⚠️ A CONFIDENCE IS NOT A PROBABILITY. The formula below is a documented
heuristic (semantic margin + rare-term coverage), not a calibrated score. Treat
it as a ranking signal until you have labels; `eval.py` is where it earns the
right to be called a threshold.
"""

from __future__ import annotations

import math
import re
import time
from bisect import bisect_right

from raggy import chunking
from raggy.encoder import EncoderChoice, resolve as resolve_encoder
from raggy.llm import GROUNDED_SYSTEM, LLMClient, build_user_prompt
from raggy.retrievers import IndexCache, SearchIndex, tokenize

# Below this confidence we abstain rather than generate. CALIBRATED, not guessed:
# `./run.sh eval` sweeps the threshold against labelled answerable/unanswerable
# queries and reports the balanced accuracy. At 0.45 it abstains on 100% of the
# out-of-scope questions at the cost of 15% of answerable ones on the sample
# corpus — a deliberate trade, and one you should re-run on YOUR document.
DEFAULT_THRESHOLD = 0.45

NOT_IN_DOCUMENT = "NOT_IN_DOCUMENT"


class RaggyEngine:
    """Index one document; search it; ask about it."""

    def __init__(self, strategy: str = "recursive", target_chars: int = 600,
                 overlap: int = 80, encoder_kind: str | None = None,
                 encoder_model: str | None = None, threshold: float = DEFAULT_THRESHOLD,
                 llm: LLMClient | None = None):
        self.strategy = strategy
        self.target_chars = target_chars
        self.overlap = overlap
        self.threshold = threshold
        self.llm = llm if llm is not None else LLMClient()

        self.text: str = ""
        self.doc_name: str = ""
        self.chunks: list = []
        self.search_index: SearchIndex | None = None
        self.encoder: EncoderChoice | None = None
        self._encoder_kind = encoder_kind
        self._encoder_model = encoder_model
        self._line_starts: list[int] = [0]

        # Incremental-indexing state.
        # ⚠️ The encoder is resolved ONCE and reused: on the neural path,
        # `resolve_encoder` constructs a SentenceTransformer, and doing that on
        # every keystroke-triggered re-index reloads the model each time.
        self._resolved: EncoderChoice | None = None
        self._resolved_key: tuple | None = None
        self._cache = IndexCache()
        self._cache_doc: str | None = None
        self._cache_encoder: str | None = None

    # --------------------------------------------------------------- encoder
    def _encoder_choice(self) -> EncoderChoice:
        key = (self._encoder_kind, self._encoder_model)
        if self._resolved is None or self._resolved_key != key:
            self._resolved = resolve_encoder(self._encoder_kind, self._encoder_model)
            self._resolved_key = key
        return self._resolved

    # ------------------------------------------------------------------ index
    def index(self, text: str, doc_name: str = "untitled") -> dict:
        """(Re)index the document, reusing everything that did not change.

        Safe — and cheap — to call on every edit. Passages whose text is
        unchanged keep their tokens, and (with a real encoder) their embedding
        vectors, via the content-addressed `IndexCache`. The returned
        `reused_chunks` / `reencoded_chunks` say exactly what was recomputed.
        """
        started = time.perf_counter()
        changed = self._changed_chars(self.text, text)
        self.text = text
        self._line_starts = [0] + [m.end() for m in re.finditer(r"\n", text)]

        self.encoder = self._encoder_choice()

        # ⚠️ INVALIDATE ON CONTEXT CHANGE. The cache maps text -> vector, and a
        # vector is only meaningful for the model that produced it. A different
        # document or a different encoder must not reuse the old entries, or we
        # would compare vectors from two models — which returns results that
        # look fine and are wrong.
        if doc_name != self._cache_doc or self.encoder.name != self._cache_encoder:
            self._cache = IndexCache()
            self._cache_doc = doc_name
            self._cache_encoder = self.encoder.name

        self.doc_name = doc_name
        self.chunks = chunking.chunk_text(
            text, strategy=self.strategy, target_chars=self.target_chars,
            overlap=self.overlap)
        self.search_index = SearchIndex.build(
            self.chunks, encoder=self.encoder.encoder, cache=self._cache)

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return {
            "doc_name": doc_name,
            "chars": len(text),
            "lines": len(self._line_starts),
            "strategy": self.strategy,
            "encoder": self.encoder.name,
            "encoder_note": self.encoder.note,
            "is_neural": self.encoder.is_neural,
            # incremental bookkeeping
            "changed_chars": changed,
            "reused_chunks": self.search_index.reused_chunks,
            "reencoded_chunks": self.search_index.reencoded_chunks,
            "index_ms": round(elapsed_ms, 2),
            **chunking.chunk_stats(self.chunks),
        }

    def update(self, text: str) -> dict:
        """Incremental re-index after an edit of the SAME document.

        Thin alias for `index`; separate name only so callers can say what they
        mean — and so this is where the "same document" assumption lives.
        """
        return self.index(text, self.doc_name or "untitled")

    @staticmethod
    def _changed_chars(old: str, new: str) -> int:
        """Size of the changed region, by common prefix/suffix.

        O(n) and enough for the editor case: typing, deleting, or editing a
        paragraph changes one contiguous region. (A move/paste elsewhere shows up
        as a larger region — which is correct, it IS a bigger change.)
        """
        if old == new:
            return 0
        n = min(len(old), len(new))
        p = 0
        while p < n and old[p] == new[p]:
            p += 1
        s = 0
        while s < (n - p) and old[len(old) - 1 - s] == new[len(new) - 1 - s]:
            s += 1
        return max(len(old), len(new)) - p - s

    @property
    def is_indexed(self) -> bool:
        return self.search_index is not None and bool(self.chunks)

    # ------------------------------------------------------- location helpers
    def locate(self, offset: int) -> tuple[int, int]:
        """Character offset -> (line, column), both 1-based."""
        i = bisect_right(self._line_starts, offset) - 1
        i = max(0, min(i, len(self._line_starts) - 1))
        return i + 1, offset - self._line_starts[i] + 1

    def _decorate(self, hits: list[dict]) -> list[dict]:
        for h in hits:
            line, col = self.locate(h["start"])
            h["line"] = line
            h["col"] = col
            h["end_line"] = self.locate(max(h["start"], h["end"] - 1))[0]
        return hits

    # --------------------------------------------------------------- searching
    def semantic_search(self, query: str, k: int = 5, mode: str = "hybrid") -> dict:
        """Rank passages by meaning. Returns results with offsets + scores."""
        if not self.is_indexed:
            return {"error": "document is not indexed yet", "query": query, "results": []}
        hits = self.search_index.search(query, k=k, mode=mode)
        hits = self._decorate(hits)
        conf = self.confidence(query, hits, mode)
        return {
            "query": query,
            "kind": "semantic",
            "mode": mode,
            "encoder": self.encoder.name if self.encoder else "?",
            "confidence": round(conf, 4),
            "low_confidence": conf < self.threshold,
            "threshold": self.threshold,
            "results": hits,
        }

    def regex_search(self, pattern: str, case_sensitive: bool = False, regex: bool = True,
                     max_hits: int = 500) -> dict:
        """The classic Find, returning the same shape as semantic_search.

        Giving both the same result shape is deliberate: the editor renders one
        list, and the whole point of RaggyEditor is that you can compare the two
        side by side on the same query.
        """
        flags = 0 if case_sensitive else re.IGNORECASE
        expr = pattern if regex else re.escape(pattern)
        try:
            rx = re.compile(expr, flags)
        except re.error as e:
            return {"error": f"bad pattern: {e}", "query": pattern, "results": []}
        if not expr:
            return {"query": pattern, "kind": "regex", "mode": "regex",
                    "count": 0, "results": []}

        hits = []
        for i, m in enumerate(rx.finditer(self.text)):
            if i >= max_hits:
                break
            hits.append({
                "rank": i + 1,
                "text": m.group(0),
                "start": m.start(),
                "end": m.end(),
                "heading": "",
                "context": self._context(m.start(), m.end()),
            })
        self._decorate(hits)
        return {"query": pattern, "kind": "regex", "mode": "regex",
                "count": len(hits), "results": hits}

    def _context(self, start: int, end: int, window: int = 70) -> str:
        a = max(0, start - window)
        b = min(len(self.text), end + window)
        return ("…" if a > 0 else "") + self.text[a:b].replace("\n", " ") + ("…" if b < len(self.text) else "")

    # ------------------------------------------------------------- confidence
    def confidence(self, query: str, hits: list[dict], mode: str = "hybrid") -> float:
        """Heuristic 0..1: does this query look answerable from this document?

        MEASURED, not assumed. On the sample handbook the two candidate signals
        behave very differently:

          signal                     answerable   unanswerable
          ------------------------   ----------   ------------
          lexical coverage (top)        0.69         0.17
          neural top cosine             0.61-0.77    0.42-0.68   <- overlaps
          LSA top cosine                0.50-1.00    0.00-0.95   <- overlaps

        ⚠️ So embedding similarity is NOT the signal here: a top-1 passage always
        looks "similar" to something, including an out-of-scope question. The
        signal that separates is whether the document actually uses the query's
        informative vocabulary. We therefore weight coverage heavily and use the
        semantic margin only as a tiebreaker.

        ⚠️ Still a heuristic, not a probability. The threshold is calibrated in
        eval.py against labelled answerable/unanswerable queries.
        """
        if not hits or self.search_index is None:
            return 0.0
        top = hits[0]
        idx = top.get("chunk_index", 0)

        q_set = set(tokenize(query))
        c_set = set(tokenize(top["text"] + " " + (top.get("heading") or "")))
        coverage = len(q_set & c_set) / max(1, len(q_set))

        # Semantic margin: how far the top passage stands out from the rest.
        # z-scored so it means the same thing for LSA and for a transformer.
        sem = 0.0
        order, scores = self.search_index.retrieve(query, k=len(self.chunks), mode=mode,
                                                   pool=max(1, len(self.chunks)))
        dn = scores.get("dense")
        if dn is not None and getattr(dn, "size", 0) > 1 and idx < dn.size:
            z = (dn[idx] - dn.mean()) / (dn.std() + 1e-9)
            sem = 1.0 / (1.0 + math.exp(-z))     # squash the z-score to 0..1

        return round(0.75 * coverage + 0.25 * sem, 6)

    # -------------------------------------------------------------------- ask
    def ask(self, question: str, k: int = 5, mode: str = "hybrid") -> dict:
        """Retrieve, then generate a cited answer — or abstain, honestly."""
        search = self.semantic_search(question, k=k, mode=mode)
        results = search.get("results", [])
        conf = search.get("confidence", 0.0)

        base = {
            "question": question,
            "encoder": search.get("encoder"),
            "confidence": conf,
            "threshold": self.threshold,
            "citations": results,
        }

        # 1. ABSTAIN before spending a token if the document does not look relevant.
        if not results or conf < self.threshold:
            return {**base, "mode": "abstained", "abstained": True, "answer": None,
                    "reason": ("nothing in this document scores above the confidence "
                               f"threshold ({conf:.2f} < {self.threshold:.2f})"),
                    "llm_available": self.llm.available}

        # 2. No key -> retrieval only. We do not fabricate prose.
        if not self.llm.available:
            return {**base, "mode": "retrieval_only", "abstained": False, "answer": None,
                    "reason": "no API key set; showing the passages instead of a generated answer",
                    "llm_available": False}

        # 3. Generate, grounded, with citations.
        raw = self.llm.chat(GROUNDED_SYSTEM, build_user_prompt(question, results)).strip()
        if raw.upper().startswith(NOT_IN_DOCUMENT) or raw == NOT_IN_DOCUMENT:
            return {**base, "mode": "abstained", "abstained": True, "answer": None,
                    "reason": "the model judged the answer is not in the document",
                    "llm_available": True}
        return {**base, "mode": "generated", "abstained": False, "answer": raw,
                "cited": self._cited_indices(raw, len(results)),
                "model": self.llm.model, "llm_available": True}

    @staticmethod
    def _cited_indices(answer: str, n_context: int) -> list[int]:
        """Citation numbers actually used, filtered to valid 1..n (0-based out)."""
        used = {int(m) for m in re.findall(r"\[(\d+)\]", answer)}
        return sorted(i - 1 for i in used if 1 <= i <= n_context)

    # ----------------------------------------------------------------- status
    def status(self) -> dict:
        return {
            "indexed": self.is_indexed,
            "doc_name": self.doc_name,
            "chars": len(self.text),
            "n_chunks": len(self.chunks),
            "strategy": self.strategy,
            "encoder": self.encoder.name if self.encoder else None,
            "encoder_note": self.encoder.note if self.encoder else None,
            "is_neural": self.encoder.is_neural if self.encoder else False,
            "threshold": self.threshold,
            "llm_available": self.llm.available,
            "llm_model": self.llm.model if self.llm.available else None,
            "cache_entries": len(self._cache),
            "cache_hits": self._cache.hits,
        }
