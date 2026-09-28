#!/usr/bin/env python3
"""
retrievers.py — the retrieval stack, implemented on numpy.

Adapted for RaggyEditor from the AI-engineering manual
(`ai-engineer-learning/11-rag/src/retrievers.py`, same author) and retargeted
from a document corpus to the passages of a single open document. The
algorithms are unchanged; the provenance and the honest notes are kept.

===============================================================================
THE RETRIEVERS, AND WHAT EACH ONE IS GOOD AT
===============================================================================
  1. BM25        lexical. Exact terms, error codes, identifiers, product names.
                 ⚠️ Zero score when the query shares no vocabulary with the
                 passage — which is the whole reason semantic search exists.
  2. DENSE       semantic. Paraphrase, synonyms, intent.
                 ⚠️ Blurs rare tokens: "ERR_CONN_4421" is close to
                 "ERR_AUTH_1180" to an encoder, because the difference is noise
                 to it. Identifiers are exactly where dense fails.
  3. HYBRID      run both, fuse the RANKINGS with RRF. The production default,
                 and it beats either alone on almost every real corpus.

⚠️ ON THE DENSE ENCODER. A real system uses a transformer sentence encoder
(bge, e5, gte, or an API). This file ships TF-IDF + truncated SVD (Latent
Semantic Analysis) as the dependency-free default, and a `NeuralIndex` that
wraps a real sentence-transformer when one is installed. The swap point is one
class; `encoder.py` chooses. We label which one is active everywhere it is
shown, because an LSA index is not a transformer and pretending otherwise is
how a demo becomes a lie.

⚠️ WHY RRF AND NOT A WEIGHTED SUM. BM25 is unbounded (0 to 50+); cosine is in
[-1, 1]. Adding them needs a normalisation that silently decays as the corpus
changes. Ranks have no units, so there is nothing to normalise.

        RRF(d) = sum over retrievers of 1 / (k + rank_r(d)),  k = 60

Usage:
    from raggy.retrievers import SearchIndex
    idx = SearchIndex.build(chunks)                  # chunks: list[Chunk]
    hits = idx.search("how do I stop it falling over", k=5, mode="hybrid")
"""

from __future__ import annotations

import math
import re
from collections import Counter

import numpy as np

# =============================================================================
# TOKENISATION
# =============================================================================
# ⚠️ Keep identifiers INTACT. A tokenizer that splits "ERR_CONN_4421" into
# "err","conn","4421" destroys the most discriminative token in the query — and
# those are exactly the tokens lexical search exists for.
RE_TOKEN = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")

STOPWORDS = set("""a an the and or but if then than that this these those is are was were
be been being do does did doing have has had having i you he she it we they me him her us
them my your his its our their of in on at to for with from by as into about over after
before between under above not no nor so such can could will would should may might must
what which who whom when where why how all any both each few more most other some only
own same too very s t just don now""".split())


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, identifiers kept whole, stopwords dropped.

    ⚠️ No stemming. Stemming merges "recovering"/"recovered" (nice) but also
    mangles product names, and on identifier-heavy text it usually hurts.
    """
    toks = RE_TOKEN.findall(text.lower())
    return [t for t in toks if t not in STOPWORDS and len(t) > 1]


# =============================================================================
# BM25
# =============================================================================
class BM25:
    """Okapi BM25 — the lexical baseline that is still hard to beat.

        score(q, d) = sum_t IDF(t) * f(t,d)(k1+1) / (f(t,d) + k1*(1 - b + b*|d|/avgdl))

    k1 saturates term frequency; b normalises for document length.
    """

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.doc_len = np.array([len(d) for d in docs], dtype=np.float64)
        self.avgdl = self.doc_len.mean() if self.n else 1.0
        self.tfs: list[Counter] = [Counter(d) for d in docs]

        df = Counter()
        for d in docs:
            df.update(set(d))
        self.df = df
        # ⚠️ The +0.5 smoothing matters: without it a term in >half the chunks
        # gets NEGATIVE idf and actively penalises a match.
        self.idf = {
            t: math.log(1.0 + (self.n - n_t + 0.5) / (n_t + 0.5))
            for t, n_t in df.items()
        }
        self.postings: dict[str, list[int]] = {}
        for i, d in enumerate(docs):
            for t in set(d):
                self.postings.setdefault(t, []).append(i)

    def scores(self, query: str) -> np.ndarray:
        out = np.zeros(self.n, dtype=np.float64)
        q_toks = tokenize(query)
        for t in q_toks:
            if t not in self.idf:
                continue
            idf = self.idf[t]
            for i in self.postings.get(t, ()):      # inverted index = the speed
                f = self.tfs[i][t]
                denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avgdl)
                out[i] += idf * (f * (self.k1 + 1.0)) / denom
        return out


# =============================================================================
# DENSE — TF-IDF + truncated SVD (LSA), the dependency-free stand-in
# =============================================================================
def _l2(x: np.ndarray, eps: float = 1e-12) -> np.ndarray:
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    return x / np.maximum(n, eps)


class DenseIndex:
    """LSA: TF-IDF term vectors projected onto the top singular directions.

    ⚠️ WHY SVD AT ALL. Raw TF-IDF is a sparse lexical representation and has
    BM25's paraphrase blindness. The SVD finds latent directions on which
    "stop the service falling over" and "overload protection" land near each
    other, because they co-occur with similar vocabulary. Same idea as a neural
    encoder, done with linear algebra — and weaker at it.
    """

    def __init__(self, doc_vecs: np.ndarray, vocab: dict[str, int], idf: np.ndarray,
                 vt: np.ndarray):
        self.doc_vecs = doc_vecs          # [n, dim], L2-normalised
        self.vocab = vocab
        self.idf = idf
        self.vt = vt                      # [dim, vocab] projection

    @staticmethod
    def build(docs: list[list[str]], n_components: int = 128, ngram: int = 2) -> "DenseIndex":
        # ngram=2 adds bigrams: without them bag-of-words cannot tell
        # "connection pool exhausted" from "pool connection exhausted".
        def feats(toks: list[str]) -> list[str]:
            bi = [f"{a}_{b}" for a, b in zip(toks, toks[1:])] if ngram >= 2 else []
            return toks + bi

        feats_docs = [feats(d) for d in docs]
        vocab: dict[str, int] = {}
        for fd in feats_docs:
            for t in fd:
                if t not in vocab:
                    vocab[t] = len(vocab)
        V = len(vocab)
        if V == 0:
            return DenseIndex(np.zeros((len(docs), 1)), {}, np.zeros(0), np.zeros((1, 0)))

        df = np.zeros(V, dtype=np.float64)
        for fd in feats_docs:
            for t in set(fd):
                df[vocab[t]] += 1
        n = len(feats_docs)
        idf = np.log(1.0 + (n - df + 0.5) / (df + 0.5))

        X = np.zeros((n, V), dtype=np.float64)
        for i, fd in enumerate(feats_docs):
            for t, c in Counter(fd).items():
                X[i, vocab[t]] = (1.0 + math.log(c)) * idf[vocab[t]]   # sublinear tf

        k = max(1, min(n_components, min(X.shape) - 1))
        # Truncated SVD without centring — centring would destroy the sparsity
        # that makes these directions interpretable as topics.
        U, S, Vt = np.linalg.svd(X, full_matrices=False)
        doc_vecs = _l2(U[:, :k] * S[:k])
        return DenseIndex(doc_vecs, vocab, idf, Vt[:k, :])

    def _vec(self, toks: list[str], ngram: int = 2) -> np.ndarray:
        bi = [f"{a}_{b}" for a, b in zip(toks, toks[1:])] if ngram >= 2 else []
        v = np.zeros(len(self.vocab), dtype=np.float64)
        for t, c in Counter(toks + bi).items():
            idx = self.vocab.get(t)
            if idx is not None:
                v[idx] = (1.0 + math.log(c)) * self.idf[idx]
        if v.shape[0] == 0 or self.vt.shape[0] == 0:
            return np.zeros(self.doc_vecs.shape[1] if self.doc_vecs.size else 1)
        proj = v @ self.vt.T
        return _l2(proj)

    def scores(self, query: str) -> np.ndarray:
        return self.doc_vecs @ self._vec(tokenize(query))


# =============================================================================
# NEURAL — the real encoder, opt-in
# =============================================================================
DEFAULT_ENCODER = "BAAI/bge-small-en-v1.5"


class NeuralIndex:
    """A real sentence encoder. Drop-in replacement for `DenseIndex`.

    ⚠️ bge models are trained with a QUERY PREFIX: retrieval queries get the
    instruction, documents do not. Getting this wrong costs recall and produces
    no error — a silent failure.
    """

    QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

    def __init__(self, model_name: str = DEFAULT_ENCODER, cache_folder: str | None = None):
        import sentence_transformers                                # noqa: PLC0415
        self.model_name = model_name
        self.model = sentence_transformers.SentenceTransformer(
            model_name, cache_folder=cache_folder, device="cpu")
        # ⚠️ The method was renamed in sentence-transformers 5.x. Read the modern
        # name first and fall back, so neither version emits a deprecation or dies.
        get_dim = (getattr(self.model, "get_embedding_dimension", None)
                   or self.model.get_sentence_embedding_dimension)
        self.dim = get_dim()

    def encode_docs(self, texts: list[str]) -> np.ndarray:
        return self.model.encode(texts, normalize_embeddings=True, batch_size=32,
                                 show_progress_bar=False)

    def encode_query(self, query: str) -> np.ndarray:
        return self.model.encode([self.QUERY_PREFIX + query],
                                 normalize_embeddings=True, show_progress_bar=False)[0]


class NeuralDenseIndex:
    """Same `.scores(query)` interface as DenseIndex, backed by a real encoder."""

    def __init__(self, doc_vecs: np.ndarray, encoder: NeuralIndex):
        self.doc_vecs = doc_vecs
        self.encoder = encoder

    @staticmethod
    def build(texts: list[str], encoder: NeuralIndex) -> "NeuralDenseIndex":
        return NeuralDenseIndex(encoder.encode_docs(texts), encoder)

    def scores(self, query: str) -> np.ndarray:
        return self.doc_vecs @ self.encoder.encode_query(query)


# =============================================================================
# FUSION
# =============================================================================
def rrf(rank_lists: list[list[int]], k: int = 60,
        weights: list[float] | None = None) -> dict[int, float]:
    """Reciprocal Rank Fusion over RANKED LISTS of indices -> {index: score}.

    k=60 is from the original paper and is remarkably insensitive — exactly
    what you want in production.
    """
    weights = weights or [1.0] * len(rank_lists)
    fused: dict[int, float] = {}
    for w, ranks in zip(weights, rank_lists):
        for rank, doc in enumerate(ranks):
            fused[doc] = fused.get(doc, 0.0) + w / (k + rank + 1)
    return fused


def top_indices(scores: np.ndarray, n: int) -> list[int]:
    if n <= 0 or scores.size == 0:
        return []
    n = min(n, len(scores))
    # argpartition is O(n) vs O(n log n) for a sort — matters as the doc grows.
    part = np.argpartition(-scores, n - 1)[:n]
    return sorted(part.tolist(), key=lambda i: -scores[i])


# =============================================================================
# RERANKER — a local feature-based stand-in for a cross-encoder
# =============================================================================
RERANK_FEATURES = [
    "bm25_norm", "dense_cos", "term_coverage", "rare_term_coverage",
    "phrase_match", "heading_overlap", "log_len",
]


def rerank_features(query: str, chunk, bm25_score: float, dense_score: float,
                    bm25: BM25) -> np.ndarray:
    """Features for one (query, passage) pair.

    ⚠️ A real reranker is a CROSS-ENCODER: query and passage go through one
    transformer together, so it models their interaction. These hand-designed
    features are the local stand-in; they capture the same signals without a
    model and let you measure whether reranking helps at all.
    """
    q_toks = tokenize(query)
    c_toks = tokenize(chunk.text + " " + chunk.heading)
    c_set, q_set = set(c_toks), set(q_toks)

    coverage = len(q_set & c_set) / max(1, len(q_set))
    # Rare-term coverage, IDF weighted: getting "the" right means nothing;
    # getting "ERR_CONN_4421" right means everything.
    if q_set:
        w = sum(bm25.idf.get(t, 1.0) for t in q_set & c_set)
        wt = sum(bm25.idf.get(t, 1.0) for t in q_set)
        rare_cov = w / wt if wt else 0.0
    else:
        rare_cov = 0.0

    phrase = 1.0 if query.lower().strip() and query.lower().strip() in chunk.text.lower() else 0.0
    head_toks = set(tokenize(chunk.heading))
    head_ov = len(q_set & head_toks) / max(1, len(q_set))

    return np.array([
        math.tanh(bm25_score / 10.0),       # bounded, keeps the linear model stable
        dense_score,
        coverage,
        rare_cov,
        phrase,
        head_ov,
        math.log1p(len(chunk.text)) / 10.0,
    ], dtype=np.float64)


class Reranker:
    """Logistic regression over the features above, trained on labelled pairs.

    ⚠️ Fit on TRAIN queries only — fitting on the queries you then report is
    how an evaluation becomes fiction.
    """

    def __init__(self, dim: int):
        self.w = np.zeros(dim)
        self.b = 0.0
        self.fitted = False

    def fit(self, X: np.ndarray, y: np.ndarray, epochs: int = 800, lr: float = 0.5,
            l2: float = 1e-3) -> "Reranker":
        n, d = X.shape
        self.w, self.b = np.zeros(d), 0.0
        for _ in range(epochs):
            p = 1.0 / (1.0 + np.exp(-np.clip(X @ self.w + self.b, -30, 30)))
            g = (p - y) / n
            self.w -= lr * (X.T @ g + l2 * self.w)
            self.b -= lr * g.sum()
        self.w = self.w / (np.linalg.norm(self.w) + 1e-9)
        self.fitted = True
        return self

    def score(self, feats: np.ndarray) -> np.ndarray:
        return feats @ self.w + self.b


# =============================================================================
# METRICS
# =============================================================================
def recall_at_k(retrieved: list, gold: list) -> float:
    """Fraction of gold passages found. ⚠️ Report with hit@k — different question."""
    if not gold:
        return float("nan")
    return len(set(retrieved) & set(gold)) / len(set(gold))


def hit_at_k(retrieved: list, gold: list) -> float:
    if not gold:
        return float("nan")
    return 1.0 if set(retrieved) & set(gold) else 0.0


def mrr(retrieved: list, gold: list) -> float:
    """Reciprocal rank of the FIRST gold passage — rewards putting it first."""
    if not gold:
        return float("nan")
    g = set(gold)
    for i, d in enumerate(retrieved):
        if d in g:
            return 1.0 / (i + 1)
    return 0.0


def ndcg_at_k(retrieved: list, gold: list, k: int | None = None) -> float:
    """nDCG, binary relevance. Use it when ORDER matters.

    ⚠️ Lives in [0, 1]. A value outside that range is an evaluation bug.
    """
    if not gold:
        return float("nan")
    g = set(gold)
    rel = [1.0 if d in g else 0.0 for d in (retrieved[:k] if k else retrieved)]
    dcg = sum(r / math.log2(i + 2) for i, r in enumerate(rel))
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(g), len(rel) or len(g))))
    val = dcg / ideal if ideal > 0 else 0.0
    assert -1e-9 <= val <= 1.0 + 1e-9, f"nDCG out of range: {val}"
    return min(1.0, max(0.0, val))


# =============================================================================
# THE SEARCH INDEX
# =============================================================================
class SearchIndex:
    """Holds the passages plus every retriever, and answers queries.

    `chunks` are `raggy.chunking.Chunk` objects (they carry `.text`, `.heading`,
    `.start`, `.end`). The index reads only `.text`/`.heading`, so it is
    agnostic to how they were produced.
    """

    def __init__(self, chunks: list, encoder=None):
        self.chunks = chunks
        self.tokens = [tokenize(c.text + " " + c.heading) for c in chunks]
        self.bm25 = BM25(self.tokens)
        self.encoder = encoder
        self.encoder_name = getattr(encoder, "model_name", None) or "lsa-tfidf-svd"
        if len(chunks) < 3:
            self.dense = None
        elif encoder is not None:
            # Feed the encoder the same text the LSA path sees, so the two are
            # compared on identical input.
            self.dense = NeuralDenseIndex.build(
                [c.text + " " + c.heading for c in chunks], encoder)
        else:
            self.dense = DenseIndex.build(self.tokens)
        self.reranker: Reranker | None = None

    @classmethod
    def build(cls, chunks: list, encoder=None) -> "SearchIndex":
        return cls(chunks, encoder)

    def retrieve(self, query: str, k: int = 10, mode: str = "hybrid",
                 pool: int = 50) -> tuple[list[int], dict]:
        """Return (ranked chunk indices, score dict)."""
        bm_scores = self.bm25.scores(query)
        dn_scores = (self.dense.scores(query) if self.dense is not None
                     else np.zeros(len(self.chunks)))

        bm_rank = top_indices(bm_scores, pool)
        dn_rank = top_indices(dn_scores, pool)

        if mode == "bm25":
            return bm_rank[:k], {"bm25": bm_scores}
        if mode == "dense":
            return dn_rank[:k], {"dense": dn_scores}
        if mode in ("hybrid", "hybrid_rerank"):
            fused = rrf([bm_rank, dn_rank], k=60)
            cand = [i for i, _ in sorted(fused.items(), key=lambda kv: -kv[1])][:pool]
            if mode == "hybrid_rerank" and self.reranker is not None and self.reranker.fitted:
                F = np.stack([rerank_features(query, self.chunks[i], bm_scores[i],
                                              dn_scores[i], self.bm25) for i in cand])
                rr = self.reranker.score(F)
                order = [cand[j] for j in np.argsort(-rr)][:k]
                return order, {"bm25": bm_scores, "dense": dn_scores, "rerank": rr,
                               "candidates": np.array(cand)}
            return cand[:k], {"bm25": bm_scores, "dense": dn_scores}
        raise ValueError(f"unknown mode {mode!r}")

    def search(self, query: str, k: int = 5, mode: str = "hybrid",
               pool: int = 50) -> list[dict]:
        """Convenience wrapper: ranked results as dicts ready for JSON/highlight."""
        order, scores = self.retrieve(query, k=k, mode=mode, pool=pool)
        out = []
        for rank, i in enumerate(order):
            c = self.chunks[i]
            out.append({
                "chunk_id": getattr(c, "index", i),
                "chunk_index": getattr(c, "index", i),
                "rank": rank + 1,
                "text": c.text,
                "start": c.start,
                "end": c.end,
                "heading": c.heading,
                "bm25": float(scores["bm25"][i]) if "bm25" in scores else None,
                "dense": float(scores["dense"][i]) if "dense" in scores else None,
            })
        return out

    def rerank_train_matrix(self, queries: list[dict], pool: int = 50):
        """(features, labels) for fitting the reranker. ⚠️ TRAIN queries only."""
        X, y = [], []
        for q in queries:
            gold = set(q.get("gold_chunk_ids") or [])
            if not gold:
                continue
            cand, scores = self.retrieve(q["query"], k=pool, mode="hybrid", pool=pool)
            for i in cand:
                X.append(rerank_features(q["query"], self.chunks[i], scores["bm25"][i],
                                         scores["dense"][i], self.bm25))
                y.append(1.0 if getattr(self.chunks[i], "index", i) in gold else 0.0)
        if not X:
            return np.zeros((0, len(RERANK_FEATURES))), np.zeros(0)
        return np.stack(X), np.array(y)
