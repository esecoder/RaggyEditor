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


class TestNoChunkExceedsTheTarget(unittest.TestCase):
    """⚠️ THE BUG THIS CLASS EXISTS FOR.

    `_pack_spans` decides where to STOP packing; it could not SPLIT a unit that
    was already larger than the target, and it silently emitted those whole.

    Found on a real 946 KB notes file: 160 chunks averaging 5,925 characters, a
    p95 of 30,729 and one of 237,315 — a quarter of the document in a single
    "passage". Everything downstream then failed quietly:

      * the ONNX encoder truncates at 512 tokens, so ~99% of an oversized chunk
        was never embedded: the text was indexed and invisible to search;
      * BM25's length normalisation buried the terms in a chunk that size;
      * the user's query "Which activation function is suitable for multi-class
        classification" returned nothing relevant, although the answer is in the
        file verbatim — the passage sat inside a chunk that was mostly ignored.

    Every other fixture in this suite was too well behaved to catch it: the sample
    handbook is 9 KB with NO headings, so `_span_heading_blocks` returns [] and the
    oversized path is never reached. These tests deliberately build documents that
    look like real notes.
    """

    TARGET = 600

    def _assert_all_fit(self, text, strategy="recursive"):
        chunks = chunking.chunk_text(text, strategy, self.TARGET, 80)
        self.assertTrue(chunks, "no chunks produced")
        sizes = [len(c.text) for c in chunks]
        self.assertLessEqual(
            max(sizes), self.TARGET,
            f"a chunk of {max(sizes)} chars exceeds the {self.TARGET} target")
        # The invariant search correctness rests on.
        for c in chunks:
            self.assertEqual(text[c.start:c.end], c.text)
        return chunks

    def _long_body(self, sentences=400):
        return " ".join(
            f"Sentence number {i} discusses the behaviour of the system in detail "
            f"and carries enough words to matter." for i in range(sentences))

    def test_one_heading_followed_by_a_very_long_section(self):
        # A heading and then a huge run of text: exactly the shape of a notes file,
        # and exactly what produced a 237,315-character chunk.
        text = "# Activation Functions\n\n" + self._long_body()
        chunks = self._assert_all_fit(text)
        self.assertGreater(len(chunks), 5, "the long section was not split")

    def test_several_headings_with_one_enormous_section(self):
        text = ("# First\n\nshort bit\n\n"
                "# Second\n\n" + self._long_body(600) + "\n\n"
                "# Third\n\ntail\n")
        self._assert_all_fit(text)

    def test_a_single_unbroken_run_of_text(self):
        # No headings, no paragraphs, no sentence stops: the last resort is
        # characters, and it still has to respect the target.
        text = "x" * 50000
        chunks = self._assert_all_fit(text)
        self.assertGreater(len(chunks), 50)

    def test_one_paragraph_of_many_sentences(self):
        text = self._long_body(300)          # no blank lines anywhere
        self._assert_all_fit(text)

    def test_a_run_of_text_with_no_sentence_punctuation(self):
        text = ("# Heading\n\n"
                + " ".join("word" * 1 for _ in range(9000)))
        self._assert_all_fit(text)

    def test_the_split_applies_to_every_strategy(self):
        text = "# Heading\n\n" + self._long_body(300)
        for strategy in ("recursive", "sentence", "fixed"):
            chunks = chunking.chunk_text(text, strategy, self.TARGET, 80)
            self.assertLessEqual(max(len(c.text) for c in chunks), self.TARGET,
                                 f"{strategy} produced an oversized chunk")

    def test_a_heading_only_block_still_makes_a_chunk(self):
        # Tiny blocks are allowed — they are just not allowed to be enormous.
        text = "# A\n\n# B\n\n# C\n\nsome text here\n"
        chunks = chunking.chunk_text(text, "recursive", self.TARGET, 80)
        self.assertTrue(chunks)

    def test_the_stats_agree(self):
        text = "# Heading\n\n" + self._long_body(400)
        stats = chunking.chunk_stats(chunking.chunk_text(text, "recursive", self.TARGET, 80))
        self.assertLessEqual(stats["max"], self.TARGET)
        self.assertLessEqual(stats["p95"], self.TARGET)
        # ⚠️ Not 0.0: a heading on its own is legitimately a tiny chunk (see
        # test_a_heading_only_block_still_makes_a_chunk). What matters is that tiny
        # chunks are a small minority rather than an accident of packing.
        self.assertLess(stats["tiny_frac"], 0.05)


if __name__ == "__main__":
    unittest.main()
