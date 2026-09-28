#!/usr/bin/env python3
"""
eval.py — measure it, do not claim it.

This is the file that answers "does semantic search actually beat the Find I
already have?" with numbers instead of adjectives. It runs five retrieval
methods over a labelled set of queries on the sample handbook:

    literal    the naive Find: the query string must appear exactly
    regex_any  Find with every query word OR'd together (the generous version)
    bm25       lexical ranking (Okapi BM25)
    dense      semantic only (LSA or a real encoder)
    hybrid     BM25 + dense fused by RRF  <- what the app ships

and reports hit@k / recall@k / MRR / nDCG@k, split by query type, because an
aggregate hides the sub-population that is failing.

⚠️ TWO THINGS THIS EVAL DOES ON PURPOSE, BECAUSE MOST RAG EVALS OMIT THEM:

  1. It contains UNANSWERABLE queries, and it reports abstention on them. An eval
     set with only answerable questions makes a retriever that always returns
     top-5 look perfect. Ours also has to know when to say "not in this
     document", and the threshold that decides that is CALIBRATED here.

  2. It ASSERTS that ground truth is non-empty. A marker that matches no chunk
     means the eval is broken, not that the retriever failed — the same class of
     bug that once produced an nDCG of 1.445 in the manual. It fails loudly.

Run:
    ./run.sh eval
    ./run.sh eval --encoder both --k 5
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

from raggy.engine import RaggyEngine
from raggy.retrievers import (hit_at_k, mrr, ndcg_at_k, recall_at_k, tokenize)

SAMPLE = pathlib.Path(__file__).resolve().parents[2] / "samples" / "meridian-operations-handbook.txt"
K = 5

# -----------------------------------------------------------------------------
# THE LABELLED SET. `marker` is a distinctive phrase that MUST appear in the
# passage(s) that answer the query. Ground truth is the set of chunks that
# contain it. `None` marker => unanswerable (must not be in the document).
# -----------------------------------------------------------------------------
DATASET = [
    # -- paraphrase: the query shares little or no vocabulary with the answer
    {"type": "paraphrase", "q": "how do I fix an expired certificate on a replica",
     "marker": "handshake did not complete"},
    {"type": "paraphrase", "q": "the service is overwhelmed and cannot keep up with writes",
     "marker": "cannot keep up with the write rate"},
    {"type": "paraphrase", "q": "I restarted a replica but the coordinator still sent it work",
     "marker": "drain the worker first"},
    {"type": "paraphrase", "q": "how is replication lag defined",
     "marker": "difference between the newest change in the stream",
     "overlap_note": "the glossary wraps this phrase across a line break; matching is whitespace-normalised"},
    {"type": "paraphrase", "q": "what happens to a batch that never succeeds",
     "marker": "dead-letter file"},
    {"type": "paraphrase", "q": "why does adding workers not help when it is slow",
     "marker": "more workers make the contention worse"},
    {"type": "paraphrase", "q": "can I recover a replica from a disk snapshot",
     "marker": "Never restore a replica from a filesystem snapshot"},
    {"type": "paraphrase", "q": "when should I escalate to the platform team",
     "marker": "twenty minutes and is still growing"},
    {"type": "paraphrase", "q": "what does the queue being full tell me",
     "marker": "backpressure signal"},

    # -- identifier: exact tokens, where lexical search SHOULD win
    {"type": "identifier", "q": "ERR_CONN_4421",
     "marker": "cannot establish a connection to a replica"},
    {"type": "identifier", "q": "ERR_AUTH_1180",
     "marker": "credential problem"},
    {"type": "identifier", "q": "ERR_QUEUE_7003",
     "marker": "high-water mark"},
    {"type": "identifier", "q": "ERR_APPLY_5510",
     "marker": "replica rejected a write"},

    # -- unanswerable: not in the document. The retriever must not look confident.
    {"type": "unanswerable", "q": "what is the capital of France", "marker": None},
    {"type": "unanswerable", "q": "who is the CEO of the company", "marker": None},
    {"type": "unanswerable", "q": "what is the price of the enterprise plan", "marker": None},
    {"type": "unanswerable", "q": "how do I delete an S3 bucket", "marker": None},
    {"type": "unanswerable", "q": "what is the weather in Tokyo tomorrow", "marker": None},
    {"type": "unanswerable", "q": "write me a poem about the ocean", "marker": None},
]

METHODS = ("literal", "regex_any", "bm25", "dense", "hybrid")


def _norm(s: str) -> str:
    return " ".join(s.split())


def build_gold(chunks, marker: str) -> list[int]:
    """Chunk indices whose text contains `marker` (whitespace-insensitive)."""
    m = _norm(marker)
    return [c.index for c in chunks if m in _norm(c.text)]


# -----------------------------------------------------------------------------
# THE TWO BASELINES — what a text editor's Find would return
# -----------------------------------------------------------------------------
def ranked_literal(chunks, doc_text: str, query: str, k: int) -> list[int]:
    """Exact substring Find. What Ctrl+F does. Zero hits on a paraphrase.

    ⚠️ Searches the DOCUMENT, not the concatenated chunks: overlapping chunks
    would otherwise produce offsets that do not line up with `chunk.start`, and
    the whole baseline would be silently wrong.
    """
    needle = query.lower().strip()
    if not needle:
        return []
    offsets = [m.start() for m in re.finditer(re.escape(needle), doc_text.lower())]
    return _rank_chunks_by_offsets(chunks, offsets, k)


def _rank_chunks_by_offsets(chunks, offsets: list[int], k: int) -> list[int]:
    order = []
    for c in chunks:
        if any(c.start <= o < c.end for o in offsets):
            order.append(c.index)
    return order[:k]


def ranked_regex_any(chunks, query: str, k: int) -> list[int]:
    """Find with every query word OR'd — the most generous lexical baseline."""
    toks = [t for t in tokenize(query) if len(t) > 2]
    if not toks:
        return []
    rx = re.compile("|".join(re.escape(t) for t in toks), re.IGNORECASE)
    hits = [c.index for c in chunks if rx.search(c.text)]
    return hits[:k]


# -----------------------------------------------------------------------------
# ABSTENTION CALIBRATION
# -----------------------------------------------------------------------------
def calibrate(engine: RaggyEngine, rows: list[dict]) -> dict:
    """Pick the confidence threshold that best separates answerable/unanswerable.

    Sweeps thresholds and maximises balanced accuracy = mean(TNR, TPR), where:
      TPR = fraction of unanswerable queries correctly abstained on
      TNR = fraction of answerable queries correctly answered
    ⚠️ Calibrating on the queries you report is fine ONLY because this is a
    fixed, published sample set; on real data, split train/test first.
    """
    ans = [r for r in rows if r["type"] != "unanswerable"]
    unans = [r for r in rows if r["type"] == "unanswerable"]
    best = {"threshold": 0.5, "balanced_acc": -1.0, "abstain_unans": 0.0, "abstain_ans": 0.0}
    for i in range(5, 100, 5):
        t = i / 100
        abstain_unans = sum(1 for r in unans if r["confidence"] < t) / max(1, len(unans))
        abstain_ans = sum(1 for r in ans if r["confidence"] < t) / max(1, len(ans))
        bal = 0.5 * (abstain_unans + (1 - abstain_ans))
        if bal > best["balanced_acc"]:
            best = {"threshold": t, "balanced_acc": round(bal, 4),
                    "abstain_unans": round(abstain_unans, 3),
                    "abstain_ans": round(abstain_ans, 3)}
    return best


# -----------------------------------------------------------------------------
def run_encoder(kind: str, text: str, k: int) -> list[dict]:
    engine = RaggyEngine(encoder_kind=kind)
    info = engine.index(text, SAMPLE.name)
    chunks = engine.chunks

    # Validate ground truth before trusting any metric.
    for item in DATASET:
        if item["marker"] is None:
            continue
        gold = build_gold(chunks, item["marker"])
        assert gold, (f"eval bug: marker {item['marker']!r} matches no chunk "
                      f"(query: {item['q']!r}). Fix the marker, do not 'fix' the metric.")

    rows = []
    for item in DATASET:
        gold = [] if item["marker"] is None else build_gold(chunks, item["marker"])
        row = {"type": item["type"], "q": item["q"], "gold": gold, "rankings": {}}

        if item["marker"] is not None:
            row["rankings"]["literal"] = ranked_literal(chunks, text, item["q"], k)
            row["rankings"]["regex_any"] = ranked_regex_any(chunks, item["q"], k)
            for mode in ("bm25", "dense", "hybrid"):
                order, _ = engine.search_index.retrieve(item["q"], k=k, mode=mode)
                row["rankings"][mode] = order

        # Confidence is computed the same way `ask` does, for calibration.
        search = engine.semantic_search(item["q"], k=k, mode="hybrid")
        row["confidence"] = search.get("confidence", 0.0)
        rows.append(row)

    return {"info": info, "rows": rows, "calibration": calibrate(engine, rows)}


def metrics_block(rows: list[dict], enc_name: str, k: int) -> list[str]:
    """Render the per-method table, split by query type."""
    types = ["paraphrase", "identifier"]
    lines = [f"\n  encoder: {enc_name}   (k={k})"]
    header = f"  {'method':<11}" + "".join(
        f"| {'hit':>5} {'rec':>5} {'MRR':>5} {'nDCG':>5} " for _ in types)
    sub = f"  {'':<11}" + "".join(f"| {t[:5]:^23} " for t in types)
    lines += ["", sub, header, "  " + "-" * (len(header) - 2)]

    for method in METHODS:
        cells = []
        for t in types:
            sub_rows = [r for r in rows if r["type"] == t]
            if not sub_rows:
                cells.append(f"| {'-':>5} {'-':>5} {'-':>5} {'-':>5} ")
                continue
            hs, rs, ms, ns = [], [], [], []
            for r in sub_rows:
                rk = r["rankings"].get(method, [])
                hs.append(hit_at_k(rk, r["gold"]))
                rs.append(recall_at_k(rk, r["gold"]))
                ms.append(mrr(rk, r["gold"]))
                ns.append(ndcg_at_k(rk, r["gold"], k))
            cells.append(f"| {sum(hs)/len(hs):>5.3f} {sum(rs)/len(rs):>5.3f} "
                         f"{sum(ms)/len(ms):>5.3f} {sum(ns)/len(ns):>5.3f} ")
        lines.append(f"  {method:<11}" + "".join(cells))
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description="RaggyEditor retrieval evaluation")
    ap.add_argument("--encoder", default="both", choices=["lsa", "neural", "auto", "both"])
    ap.add_argument("--k", type=int, default=K)
    args = ap.parse_args()

    if not SAMPLE.is_file():
        sys.exit(f"sample document missing: {SAMPLE}")
    text = SAMPLE.read_text(encoding="utf-8")

    kinds = ["lsa", "neural"] if args.encoder == "both" else [args.encoder]

    print("=" * 78)
    print("RaggyEditor — does semantic search beat Find? (measured, not claimed)")
    print("=" * 78)
    print(f"  document: {SAMPLE.name}  ({len(text):,} chars)")
    print(f"  queries:  {len(DATASET)}  "
          f"({sum(1 for d in DATASET if d['type']=='paraphrase')} paraphrase, "
          f"{sum(1 for d in DATASET if d['type']=='identifier')} identifier, "
          f"{sum(1 for d in DATASET if d['type']=='unanswerable')} unanswerable)")

    results = {}
    for kind in kinds:
        try:
            results[kind] = run_encoder(kind, text, args.k)
        except Exception as e:                                   # noqa: BLE001
            print(f"\n  ⚠️ encoder {kind!r} unavailable: {type(e).__name__}: {e}")
            print("     (run ./run.sh install-neural to enable the real encoder)")

    for kind, res in results.items():
        info = res["info"]
        print(f"\n  {info['n_chunks']} passages · {info['strategy']} chunking · "
              f"{info['mean_chars']} chars avg · encoder={info['encoder']}")
        for line in metrics_block(res["rows"], info["encoder"], args.k):
            print(line)

        cal = res["calibration"]
        print(f"\n  abstention calibration (encoder={info['encoder']}):")
        print(f"    threshold              : {cal['threshold']:.2f}")
        print(f"    balanced accuracy      : {cal['balanced_acc']:.3f}")
        print(f"    abstains on UNanswerable (correct) : {cal['abstain_unans']*100:5.1f}%")
        print(f"    abstains on answerable   (harmful) : {cal['abstain_ans']*100:5.1f}%")

    # Save the calibrated threshold for the shipped default.
    if results:
        last = results[list(results)[-1]]
        print(f"\n  To ship this threshold, set DEFAULT_THRESHOLD in engine.py to "
              f"{last['calibration']['threshold']:.2f}")
    print("\n  Reading it: `literal` is a plain Ctrl+F. It scores near zero on "
          "paraphrase by construction.\n  `hybrid` is what RaggyEditor actually "
          "uses. If hybrid does not beat literal on paraphrase\n  and match it on "
          "identifiers, the system is not earning its complexity.\n")


if __name__ == "__main__":
    main()
