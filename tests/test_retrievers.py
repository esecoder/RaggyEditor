"""Retriever + metric tests. No network, no models — LSA only."""

import pathlib
import sys
import unittest

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from raggy.retrievers import (  # noqa: E402
    BM25, DenseIndex, hit_at_k, mrr, ndcg_at_k, recall_at_k, rrf, tokenize, top_indices)


class TestTokenizer(unittest.TestCase):
    def test_identifiers_survive_tokenisation(self):
        # The single most important lexical property: do not split error codes.
        self.assertIn("err_conn_4421", tokenize("Got ERR_CONN_4421 today"))
        self.assertIn("bge-small-en-v1.5", tokenize("use bge-small-en-v1.5"))

    def test_stopwords_dropped(self):
        self.assertNotIn("the", tokenize("the the the"))


class TestBM25(unittest.TestCase):
    def setUp(self):
        self.docs = [tokenize(t) for t in [
            "the connection handshake did not complete certificate expired",
            "replication lag is the pending change count",
            "queue high water mark backpressure",
        ]]
        self.bm25 = BM25(self.docs)

    def test_exact_term_wins(self):
        scores = self.bm25.scores("certificate expired")
        self.assertEqual(int(np.argmax(scores)), 0)

    def test_unseen_query_scores_all_zero(self):
        scores = self.bm25.scores("xylophone zebra")
        self.assertTrue(np.allclose(scores, 0.0))


class TestDense(unittest.TestCase):
    def test_builds_and_scores_in_range(self):
        docs = [tokenize(t) for t in [
            "connection handshake did not complete certificate expired",
            "workers cannot keep up with the write rate",
            "dead letter file for failed batches",
            "restore by replaying the stream",
        ]]
        idx = DenseIndex.build(docs)
        scores = idx.scores("failed batch wrote to dead letter")
        self.assertEqual(scores.shape[0], len(docs))
        self.assertLessEqual(float(np.max(scores)), 1.0 + 1e-6)


class TestFusion(unittest.TestCase):
    def test_rrf_rewards_agreement(self):
        a = [1, 2, 3]
        b = [2, 1, 3]
        fused = rrf([a, b])
        # Doc 1 is rank0 in a, rank1 in b; doc 2 is rank1 in a, rank0 in b.
        # They should be near-tied and both beat docs only ever at rank 2.
        self.assertGreater(fused[1], fused[3])
        self.assertGreater(fused[2], fused[3])

    def test_top_indices_order(self):
        s = np.array([0.1, 0.9, 0.5, 0.7])
        self.assertEqual(top_indices(s, 3), [1, 3, 2])


class TestMetrics(unittest.TestCase):
    def test_perfect_retrieval(self):
        self.assertEqual(recall_at_k(["a", "b"], ["a", "b"]), 1.0)
        self.assertEqual(hit_at_k(["a", "b"], ["a"]), 1.0)
        self.assertEqual(mrr(["x", "a"], ["a"]), 0.5)
        self.assertAlmostEqual(ndcg_at_k(["a", "b"], ["a", "b"]), 1.0)

    def test_nan_when_no_gold(self):
        self.assertTrue(np.isnan(recall_at_k(["a"], [])))
        self.assertTrue(np.isnan(hit_at_k(["a"], [])))
        self.assertTrue(np.isnan(mrr(["a"], [])))
        self.assertTrue(np.isnan(ndcg_at_k(["a"], [])))

    def test_ndcg_is_always_bounded(self):
        # The manual's eval once produced nDCG=1.445; the assertion in ndcg_at_k
        # exists to make that class of bug impossible.
        for rk in (["a"], ["b", "a"], [], ["a", "b", "c"], ["c", "b", "a"]):
            v = ndcg_at_k(rk, ["a", "b"], k=5)
            if not np.isnan(v):
                self.assertGreaterEqual(v, 0.0)
                self.assertLessEqual(v, 1.0)


if __name__ == "__main__":
    unittest.main()
