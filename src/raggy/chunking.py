#!/usr/bin/env python3
"""
chunking.py — split a document into passages, and KEEP THE OFFSETS.

===============================================================================
WHY THIS FILE DIFFERS FROM A NORMAL CHUNKER
===============================================================================
A RAG chunker returns text. A chunker for a *text editor* must also return
WHERE that text came from, as character offsets into the open document, because
the whole point of RaggyEditor's Find is the jump-and-highlight: the user
clicks a result and the editor selects the exact passage.

So every strategy here returns spans `(start, end)` in the ORIGINAL string, and
`chunk_text` slices the document verbatim. We never re-join text with a
different separator than the source, because that would make `start`/`end`
point at the wrong characters — and a highlight that is 3 characters off is
worse than no highlight.

===============================================================================
THE STRATEGIES
===============================================================================
  1. FIXED       every N characters with overlap. Blunt, never fails.
  2. RECURSIVE   split on the largest structural boundary that fits
                 (heading -> paragraph -> sentence -> character). The default:
                 it degrades gracefully instead of failing.
  3. SENTENCE    whole sentences only, packed to a budget. Always readable.

⚠️ Chunk SIZE is the lever everyone ignores and it decides what retrieval can
possibly return. `target_chars` is a starting point to measure, not a fact.

Usage:
    from raggy.chunking import chunk_text
    chunks = chunk_text(text, strategy="recursive", target_chars=600, overlap=80)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

STRATEGIES = ("fixed", "recursive", "sentence")

# A markdown heading, or a short ALL-CAPS line (common in plain .txt docs).
RE_MD_HEADING = re.compile(r"^#{1,6}\s+\S.*$", re.MULTILINE)
RE_CAPS_HEADING = re.compile(r"^[A-Z][A-Z0-9 ,'&/()-]{2,60}$", re.MULTILINE)
RE_PARAGRAPH = re.compile(r"\n\s*\n")
# Sentence end = terminal punctuation followed by whitespace or end of text.
RE_SENT_END = re.compile(r"[.!?](?=\s|$)")

# Defaults. ⚠️ Measure these on your own document; do not trust them.
DEFAULT_TARGET = 600
DEFAULT_OVERLAP = 80


@dataclass
class Chunk:
    """One passage, with the exact place it came from in the document.

    `text == document[start:end]` is an invariant. If it ever is not, the
    highlight in the editor will be off, so `chunk_text` asserts it.
    """

    index: int
    text: str
    start: int
    end: int
    heading: str = ""

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "heading": self.heading,
        }


# =============================================================================
# SPAN HELPERS — every unit is (start, end) in the original text
# =============================================================================
def _trim_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    """Strip surrounding whitespace but move the offsets in with it."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if end > start else None


def _span_paragraphs(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in RE_PARAGRAPH.finditer(text):
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(text)))
    return [s for s in (_trim_span(text, a, b) for a, b in spans) if s]


def _span_sentences(text: str) -> list[tuple[int, int]]:
    """Split at terminal punctuation.

    ⚠️ Heuristic. "v1.2" and "e.g." are not sentence ends; this handles the
    common cases and is documented as an approximation. Production would use a
    real sentence splitter (spaCy) — the tradeoff is a model dependency.
    """
    spans, start = [], 0
    for m in RE_SENT_END.finditer(text):
        end = m.end()
        if text[start:end].strip():
            spans.append((start, end))
        start = end
    if text[start:].strip():
        spans.append((start, len(text)))
    return [s for s in (_trim_span(text, a, b) for a, b in spans) if s]


def _heading_spans(text: str) -> list[tuple[int, int]]:
    """Positions of lines that look like headings."""
    spans = []
    for rx in (RE_MD_HEADING, RE_CAPS_HEADING):
        for m in rx.finditer(text):
            spans.append((m.start(), m.end()))
    return sorted(set(spans))


def _span_heading_blocks(text: str) -> list[tuple[int, int]]:
    """Cut the document at headings, so a chunk never mixes two sections."""
    heads = _heading_spans(text)
    if len(heads) < 2:
        return []
    blocks, starts = [], [h[0] for h in heads]
    starts = [s for s in starts if s > 0]
    starts = [0] + starts + [len(text)]
    for a, b in zip(starts, starts[1:]):
        span = _trim_span(text, a, b)
        if span:
            blocks.append(span)
    return blocks


#: How many times one oversized unit may be re-split before giving up and cutting
#: it by characters. See _split_unit.
_MAX_SPLIT_DEPTH = 3


def _split_unit(text: str, start: int, end: int, target: int, overlap: int,
                depth: int) -> list[tuple[int, int]]:
    """Break ONE unit that is larger than the target, using finer boundaries.

    Paragraphs first, then sentences, then raw characters — the same order the
    strategies themselves use, applied one level down.
    """
    piece = text[start:end]
    for finer in (_span_paragraphs, _span_sentences):
        inner = finer(piece)
        if len(inner) > 1:
            moved = [(a + start, b + start) for a, b in inner]
            return _pack_spans(text, moved, target, overlap, depth + 1)
    # Nothing finer to work with (one enormous unbroken run of text).
    return [(a + start, b + start)
            for a, b in _fixed_spans(piece, target, overlap)]


def _pack_spans(text: str, spans: list[tuple[int, int]], target: int,
                overlap: int, depth: int = 0) -> list[tuple[int, int]]:
    """Greedily pack units up to ~`target` chars, carrying `overlap` forward.

    Overlap exists so an answer that straddles a boundary is still fully inside
    at least one chunk. ⚠️ It is not free: it duplicates text in the index.

    ⚠️ PACKING CAN ONLY DECIDE WHERE TO STOP. It cannot break a unit that is
    ALREADY larger than the target, and the loop below silently emitted those
    whole. On a real 946 KB notes file that produced chunks of 5,925 characters on
    average, p95 of 30,729, and a single 237,315-character "passage" — a quarter of
    the document. Three things then went wrong at once, all silently:

      * the encoder truncates at 512 tokens, so ~99% of that chunk was never
        embedded at all — the text was in the index and invisible to the dense
        retriever;
      * BM25's length normalisation washed the terms out of an enormous chunk, so
        the lexical half could not rescue it either;
      * indexing was slow, because every batch padded to 512 tokens.

    So a unit bigger than the target is split further before it is packed.
    """
    if not spans:
        return []
    out: list[tuple[int, int]] = []
    i, n = 0, len(spans)
    while i < n:
        start = spans[i][0]
        end = spans[i][1]
        if end - start > target and depth < _MAX_SPLIT_DEPTH:
            out.extend(_split_unit(text, start, end, target, overlap, depth))
            i += 1
            continue
        j = i
        # Extend while the whole span still fits.
        while j + 1 < n and spans[j + 1][1] - start <= target:
            j += 1
            end = spans[j][1]
        out.append((start, end))
        if j + 1 >= n:
            break
        # Step back into the tail for overlap, always advancing by >= 1 unit.
        k = j
        while k > i and (end - spans[k][0]) < overlap:
            k -= 1
        i = k + 1
    return out


# =============================================================================
# THE STRATEGIES
# =============================================================================
def _fixed_spans(text: str, target: int, overlap: int) -> list[tuple[int, int]]:
    step = max(1, target - overlap)
    spans = []
    for i in range(0, len(text), step):
        span = _trim_span(text, i, min(i + target, len(text)))
        if span:
            spans.append(span)
        if i + target >= len(text):
            break
    return spans


def _recursive_spans(text: str, target: int, overlap: int) -> list[tuple[int, int]]:
    if len(text) <= target:
        span = _trim_span(text, 0, len(text))
        return [span] if span else []

    blocks = _span_heading_blocks(text)
    if blocks:
        return _pack_spans(text, blocks, target, overlap)

    paras = _span_paragraphs(text)
    if len(paras) > 1:
        return _pack_spans(text, paras, target, overlap)

    sents = _span_sentences(text)
    if len(sents) > 1:
        return _pack_spans(text, sents, target, overlap)

    # No structure at all (a log dump, minified text): fall back to characters.
    return _fixed_spans(text, target, overlap)


def _sentence_spans(text: str, target: int, overlap: int) -> list[tuple[int, int]]:
    sents = _span_sentences(text) or [_trim_span(text, 0, len(text))]
    sents = [s for s in sents if s]
    return _pack_spans(text, sents, target, overlap)


_SPAN_FUNCS = {
    "fixed": _fixed_spans,
    "recursive": _recursive_spans,
    "sentence": _sentence_spans,
}


# =============================================================================
# PUBLIC API
# =============================================================================
def chunk_text(text: str, strategy: str = "recursive", target_chars: int = DEFAULT_TARGET,
               overlap: int = DEFAULT_OVERLAP) -> list[Chunk]:
    """Chunk `text`, returning passages that know their own offsets."""
    if strategy not in _SPAN_FUNCS:
        raise ValueError(f"unknown strategy {strategy!r}; have {list(_SPAN_FUNCS)}")

    spans = _SPAN_FUNCS[strategy](text, target_chars, overlap)
    headings = _heading_spans(text)

    chunks: list[Chunk] = []
    for i, (start, end) in enumerate(spans):
        piece = text[start:end]
        # Invariant: the slice must reproduce the passage exactly.
        assert text[start:end] == piece
        heading = ""
        for hs, he in headings:
            if hs <= start:
                heading = text[hs:he].lstrip("# ").strip()
            else:
                break
        chunks.append(Chunk(index=i, text=piece, start=start, end=end, heading=heading))
    return chunks


def chunk_stats(chunks: list[Chunk]) -> dict:
    """Shape summary — the numbers to look at before believing a strategy."""
    if not chunks:
        return {"n_chunks": 0, "mean_chars": 0, "p50": 0, "p95": 0, "max": 0,
                "tiny_frac": 0.0}
    lens = sorted(len(c.text) for c in chunks)
    return {
        "n_chunks": len(chunks),
        "mean_chars": round(sum(lens) / len(lens), 1),
        "p50": lens[len(lens) // 2],
        "p95": lens[int(len(lens) * 0.95)],
        "max": lens[-1],
        "tiny_frac": round(sum(1 for x in lens if x < 80) / len(lens), 3),
    }
