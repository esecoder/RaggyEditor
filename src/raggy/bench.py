#!/usr/bin/env python3
"""
bench.py — what incremental re-indexing actually saves.

`eval.py` measures quality. This measures cost, because "incremental" is a
performance claim and a performance claim without a number is marketing.

The trap this benchmark avoids: the real encoder loads a model the first time
you touch it, and that one-time cost (~10s here) dwarfs everything else. If you
time "cold index" naively you are mostly timing the model load. So we:

  1. Warm the encoder once, and report that separately as a one-time cost.
  2. Build a document large enough that ENCODING dominates.
  3. Compare, with a warm model:
        cold        index everything (what a save does today)
        edit        re-index after changing one paragraph (incremental)
        no-op       re-index with no change at all

⚠️ Read the LSA row too. LSA has no per-passage vectors to reuse — TF-IDF and
the SVD basis are fit over the whole corpus, so an edit changes every vector.
Incremental indexing is a large win on the neural path and a small one on LSA,
and the honest table says so.

Run:
    ./run.sh bench
    ./run.sh bench --paragraphs 800
"""

from __future__ import annotations

import argparse
import sys
import time

from raggy.engine import RaggyEngine


def synthetic_document(n_paragraphs: int) -> str:
    """A large document of UNIQUE paragraphs.

    ⚠️ Unique on purpose. If paragraphs repeated, the content-addressed cache
    would hit on identical text even during a cold index, and the benchmark
    would flatter itself.
    """
    parts = []
    for i in range(n_paragraphs):
        parts.append(
            f"Section {i} covers subsystem {i} and its behaviour under load. "
            f"Operators of node {i} should verify checkpoint {i} before proceeding. "
            f"When component {i} reports a fault, inspect shard {i} and the "
            f"associated journal entry {i}. The recommended action for incident {i} "
            f"is to drain traffic, rebalance shard {i}, and confirm that the "
            f"replication position for node {i} has advanced.\n\n"
        )
    return "".join(parts)


def bench_encoder(kind: str, n_paragraphs: int) -> dict | None:
    doc = synthetic_document(n_paragraphs)
    mid = n_paragraphs // 2
    edited = doc.replace(f"node {mid} ", f"node {mid + 1} ")   # same length: 1 passage changes

    try:
        eng = RaggyEngine(encoder_kind=kind)
    except Exception as e:                                       # noqa: BLE001
        print(f"  ⚠️ encoder {kind!r} unavailable: {type(e).__name__}: {e}")
        return None

    # 1. One-time model load, measured and excluded from the comparison.
    t0 = time.perf_counter()
    eng._encoder_choice()
    load_ms = (time.perf_counter() - t0) * 1000.0

    # 2. Cold index: clear the cache so every passage must be tokenised/encoded.
    eng._cache.clear()
    cold = eng.index(doc, "synthetic.txt")

    # 3. Warm no-op re-index (nothing changed).
    noop = eng.index(doc, "synthetic.txt")

    # 4. Incremental re-index after a one-paragraph edit.
    inc = eng.update(edited)

    return {"encoder": cold["encoder"], "chunks": cold["n_chunks"], "chars": cold["chars"],
            "load_ms": load_ms, "cold_ms": cold["index_ms"], "noop_ms": noop["index_ms"],
            "inc_ms": inc["index_ms"], "reused": inc["reused_chunks"],
            "noop_reused": noop["reused_chunks"],
            "reencoded": inc["reencoded_chunks"], "changed_chars": inc["changed_chars"]}


def main() -> None:
    ap = argparse.ArgumentParser(description="RaggyEditor incremental-index benchmark")
    ap.add_argument("--paragraphs", type=int, default=600,
                    help="document size (each paragraph ~300 chars)")
    ap.add_argument("--encoder", default="both", choices=["lsa", "neural", "auto", "both"])
    args = ap.parse_args()

    kinds = ["lsa", "neural"] if args.encoder == "both" else [args.encoder]

    print("=" * 78)
    print("RaggyEditor — cost of re-indexing after a one-paragraph edit")
    print("=" * 78)
    print(f"  synthetic document: {args.paragraphs} paragraphs "
          f"(~{args.paragraphs * 300:,} chars)\n")

    rows = []
    for kind in kinds:
        r = bench_encoder(kind, args.paragraphs)
        if r:
            rows.append(r)

    if not rows:
        sys.exit("no encoder available")

    for r in rows:
        speed = r["cold_ms"] / max(r["inc_ms"], 1e-6)
        print(f"  encoder: {r['encoder']}   ({r['chunks']} passages, {r['chars']:,} chars)")
        print(f"    one-time model load      : {r['load_ms']:>9.1f} ms   (paid once, excluded below)")
        print(f"    cold index (all passages): {r['cold_ms']:>9.1f} ms")
        print(f"    no-op re-index           : {r['noop_ms']:>9.1f} ms   "
              f"(reused {r['noop_reused']}/{r['chunks']})")
        print(f"    edit -> incremental      : {r['inc_ms']:>9.1f} ms   "
              f"(reused {r['reused']}, re-encoded {r['reencoded']}, "
              f"{r['changed_chars']} chars changed)")
        print(f"    speedup vs cold          : {speed:>9.1f}x")
        if r["reencoded"] >= r["chunks"]:
            print("    ⚠️ every passage was recomputed — this encoder has no reusable "
                  "per-passage vectors (LSA refits corpus-wide).")
        print()

    print("  How to read it: 'cold' is what a naive save does every time. 'edit' is")
    print("  what RaggyEditor does now. The gap is why the incremental cache exists.")
    print()


if __name__ == "__main__":
    main()
