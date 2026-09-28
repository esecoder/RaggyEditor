"""Chunking tests — the offset invariant is the one that matters."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from raggy import chunking  # noqa: E402

DOC = """# Title

First paragraph about alpha and beta. It has two sentences.

Second paragraph about gamma.

## Section two

Third paragraph about delta. And another sentence here.

Fourth paragraph ends the document.
"""


class TestChunking(unittest.TestCase):

    def test_offsets_are_exact_for_every_strategy(self):
        # THE invariant. If this fails, highlight-in-editor is broken.
        for strategy in chunking.STRATEGIES:
            with self.subTest(strategy=strategy):
                for c in chunking.chunk_text(DOC, strategy=strategy, target_chars=80, overlap=20):
                    self.assertEqual(c.text, DOC[c.start:c.end])
                    self.assertGreater(c.end, c.start)

    def test_recursive_splits_a_structured_document(self):
        chunks = chunking.chunk_text(DOC, strategy="recursive", target_chars=80, overlap=20)
        self.assertGreater(len(chunks), 1)

    def test_fixed_splits_long_text(self):
        chunks = chunking.chunk_text("x" * 500, strategy="fixed", target_chars=100, overlap=10)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertEqual(c.text, ("x" * 500)[c.start:c.end])

    def test_sentence_never_returns_a_fragment(self):
        chunks = chunking.chunk_text(DOC, strategy="sentence", target_chars=90, overlap=0)
        for c in chunks:
            # A packed sentence chunk should end in terminal punctuation.
            self.assertIn(c.text.strip()[-1], ".!?\"'")

    def test_empty_and_whitespace_documents(self):
        self.assertEqual(chunking.chunk_text("", strategy="recursive"), [])
        self.assertEqual(chunking.chunk_text("   \n\n  ", strategy="recursive"), [])

    def test_short_document_is_one_chunk(self):
        chunks = chunking.chunk_text("just one line", strategy="recursive", target_chars=600)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text, "just one line")

    def test_unknown_strategy_rejected(self):
        with self.assertRaises(ValueError):
            chunking.chunk_text(DOC, strategy="magic")

    def test_stats_are_sane(self):
        chunks = chunking.chunk_text(DOC, strategy="fixed", target_chars=100, overlap=20)
        stats = chunking.chunk_stats(chunks)
        self.assertEqual(stats["n_chunks"], len(chunks))
        self.assertGreaterEqual(stats["max"], stats["p95"] // 1)   # max >= p95
        self.assertLessEqual(stats["tiny_frac"], 1.0)


if __name__ == "__main__":
    unittest.main()
