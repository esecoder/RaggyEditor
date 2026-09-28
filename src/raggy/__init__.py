"""
RaggyEditor — a plain-text editor whose Find is powered by RAG.

The idea
--------
Every text editor has Find. It matches *characters*. That is exactly what you
want for `ERR_CONN_4421` and exactly wrong for "how do I stop the service
falling over" — the second query shares no words with the passage that answers
it.

RaggyEditor keeps the ordinary Find and adds three things on top of it, over
the document you already have open:

    1. REGEX / literal Find   — unchanged. Still the right tool for identifiers.
    2. SEMANTIC search        — "find me the passage about X", no shared words.
    3. ASK                    — a grounded answer with citations back to the
                                exact passages, or an honest "I don't know".

It is the RAG pipeline from the AI-engineering manual, applied to one document
instead of a web-scale corpus: chunk -> index (BM25 + dense) -> fuse -> rerank
-> generate with citations.

Honesty rules this package follows
----------------------------------
* It works with NO API key and NO network. Only numpy is required.
* The real encoder (sentence-transformers) is opt-in; without it we use
  TF-IDF + SVD (LSA) and say so — we never pretend LSA is a transformer.
* With no LLM key, `ask` degrades to retrieval-only rather than inventing text.
* Every retrieved passage carries its source offsets, so the editor can
  highlight the exact span it is talking about.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
