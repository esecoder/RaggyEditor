#!/usr/bin/env python3
"""
cli.py — RaggyEditor from the terminal.

The editor plugin and the web UI are two faces on the same engine; this is the
third, and the one to reach for when you want to see exactly what is happening
without a GUI in the way.

    ./run.sh demo                          # offline tour over the sample handbook
    ./run.sh search "how do I stop it falling over"
    ./run.sh search "ERR_CONN_4421" --mode bm25
    ./run.sh ask "why is adding workers not helping?"      # needs a key for an answer
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

from raggy.engine import RaggyEngine

REPO = pathlib.Path(__file__).resolve().parents[2]
SAMPLE = REPO / "samples" / "meridian-operations-handbook.txt"


def _load(args) -> tuple[str, str]:
    if args.file:
        p = pathlib.Path(args.file)
        if not p.is_file():
            sys.exit(f"no such file: {p}")
        return p.read_text(encoding="utf-8"), p.name
    if not SAMPLE.is_file():
        sys.exit(f"sample document missing: {SAMPLE}")
    return SAMPLE.read_text(encoding="utf-8"), SAMPLE.name


def _make_engine(args) -> RaggyEngine:
    kind = args.encoder or os.environ.get("RAGGY_ENCODER")
    return RaggyEngine(encoder_kind=kind, strategy=args.strategy,
                       target_chars=args.target_chars)


def _show_hits(hits: list[dict], limit: int) -> None:
    for h in hits[:limit]:
        snippet = " ".join(h["text"].split())
        print(f"    #{h['rank']}  line {h['line']:<4} [{h['start']}:{h['end']}]  {snippet[:88]}")


def cmd_search(args) -> None:
    text, name = _load(args)
    eng = _make_engine(args)
    info = eng.index(text, name)
    print(f"  indexed {info['n_chunks']} passages from {name} "
          f"({info['strategy']}, encoder={info['encoder']})\n")
    res = eng.semantic_search(args.query, k=args.k, mode=args.mode)
    conf = res.get("confidence", 0.0)
    flag = "  ⚠ LOW — may not be in this document" if res.get("low_confidence") else ""
    print(f"  query: {args.query!r}   mode={args.mode}   confidence={conf:.3f}{flag}\n")
    _show_hits(res["results"], args.k)
    print()


def cmd_ask(args) -> None:
    text, name = _load(args)
    eng = _make_engine(args)
    eng.index(text, name)
    try:
        res = eng.ask(args.query, k=args.k)
    except Exception as e:                                       # noqa: BLE001
        sys.exit(f"ask failed: {e}")
    print(f"  question: {args.query}\n")
    if res["mode"] == "abstained":
        print(f"  ⛔ NOT ANSWERED — {res['reason']}\n")
    elif res["mode"] == "retrieval_only":
        print(f"  ℹ️  {res['reason']}\n")
    else:
        print(f"  answer ({res.get('model')}):\n")
        for line in (res["answer"] or "").splitlines():
            print(f"    {line}")
        print()
    print(f"  sources (encoder={res.get('encoder')}, confidence={res.get('confidence'):.3f}):")
    _show_hits(res.get("citations", []), args.k)
    print()


def cmd_demo(args) -> None:
    text, name = _load(args)
    eng = RaggyEngine(encoder_kind=args.encoder or "lsa")   # offline by default
    info = eng.index(text, name)

    bar = "=" * 78
    print(bar)
    print("RaggyEditor demo — the same document, three kinds of Find")
    print(bar)
    print(f"  {name}: {info['chars']:,} chars, {info['n_chunks']} passages "
          f"({info['strategy']} chunking)")
    print(f"  encoder: {info['encoder']}  —  {info['encoder_note']}")
    print(f"  answers: {'enabled' if eng.llm.available else 'OFF (no OPENAI_API_KEY) — search still works'}")
    print("\n  Nothing here needs the network. This is the offline path.\n")

    # 1. The classic Find, on a paraphrase — and why it fails.
    q1 = "how do I fix an expired certificate on a replica"
    lit = eng.regex_search("expired certificate", regex=False)["count"]
    print(bar)
    print("1. ORDINARY FIND (literal) vs SEMANTIC on a paraphrase")
    print(bar)
    print(f"  query:                      {q1!r}")
    print(f"  literal 'expired certificate' -> {lit} match(es)   <- Ctrl+F finds nothing useful")
    res = eng.semantic_search(q1, k=3)
    print(f"  semantic (confidence {res['confidence']:.2f}) -> the passage about the failed handshake:")
    _show_hits(res["results"], 2)

    # 2. The identifier case — where lexical is RIGHT and dense is not.
    q2 = "ERR_AUTH_1180"
    print("\n" + bar)
    print("2. IDENTIFIERS — where the classic Find is the correct tool")
    print(bar)
    lit2 = eng.regex_search(q2, regex=False)
    print(f"  query: {q2!r}  -> literal finds {lit2['count']} exact match(es); "
          f"semantic can blur it with ERR_CONN_4422.")
    print("  (RaggyEditor keeps both, fused — that is the point.)")

    # 3. Ask, with honest degradation.
    q3 = "why does adding workers not help when the service is slow?"
    print("\n" + bar)
    print("3. ASK — grounded answer, or an honest refusal")
    print(bar)
    a = eng.ask(q3, k=3)
    print(f"  question: {q3}")
    print(f"  mode: {a['mode']}   ({a.get('reason', 'generated with citations')})")
    if a.get("answer"):
        print(f"  answer: {a['answer']}")
    print("  sources:")
    _show_hits(a.get("citations", []), 2)

    # 4. Out of scope — the part most demos skip.
    print("\n" + bar)
    print("4. OUT OF SCOPE — the failure mode almost every RAG demo hides")
    print(bar)
    oos = eng.ask("what is the capital of France?", k=3)
    print(f"  question: 'what is the capital of France?'")
    print(f"  mode: {oos['mode']}   confidence: {oos.get('confidence'):.3f} "
          f"(threshold {oos.get('threshold')})")
    print(f"  reason: {oos.get('reason')}")

    print("\n" + bar)
    print("  Try:  ./run.sh search \"how do I stop it falling over\"")
    print("        ./run.sh eval")
    print("        ./run.sh serve      # then open the web editor UI")
    print(bar)


def main() -> None:
    ap = argparse.ArgumentParser(prog="raggy", description="RaggyEditor CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--file", help="document to open (default: the sample handbook)")
        p.add_argument("--encoder", choices=["auto", "neural", "lsa"], default=None)
        p.add_argument("--strategy", default="recursive",
                       choices=["fixed", "recursive", "sentence"])
        p.add_argument("--target-chars", type=int, default=600)
        p.add_argument("-k", type=int, default=5)

    d = sub.add_parser("demo", help="offline tour of the sample document")
    common(d)
    d.set_defaults(func=cmd_demo)

    s = sub.add_parser("search", help="semantic find")
    s.add_argument("query")
    s.add_argument("--mode", default="hybrid", choices=["hybrid", "bm25", "dense"])
    common(s)
    s.set_defaults(func=cmd_search)

    a = sub.add_parser("ask", help="grounded answer with citations")
    a.add_argument("query")
    common(a)
    a.set_defaults(func=cmd_ask)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
