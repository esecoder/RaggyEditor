"""Incremental re-index tests.

The headline guarantee: an incremental re-index must be INDISTINGUISHABLE from a
cold full rebuild of the same text. If that ever stops holding, the cache has
started serving stale or mismatched data — the worst kind of bug, because
retrieval still returns results, they are just wrong.

Hermetic: LSA encoder, so no model load and no network.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from raggy.engine import RaggyEngine  # noqa: E402
from raggy.llm import LLMClient  # noqa: E402
from raggy.retrievers import IndexCache  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
SAMPLE = REPO / "samples" / "meridian-operations-handbook.txt"


def make_engine() -> RaggyEngine:
    return RaggyEngine(encoder_kind="lsa", llm=LLMClient(api_key=""))


def _doc(n_paragraphs: int) -> str:
    return "".join(
        f"Section {i} covers subsystem {i} under load. Node {i} reports status {i}.\n\n"
        for i in range(n_paragraphs))


class TestCacheMechanics(unittest.TestCase):
    def test_text_keyed_not_position_keyed(self):
        c = IndexCache()
        c.put_tokens("alpha", ["alpha"])
        self.assertEqual(c.get_tokens("alpha"), ["alpha"])
        self.assertIsNone(c.get_tokens("beta"))

    def test_clear(self):
        c = IndexCache()
        c.put_tokens("alpha", ["alpha"])
        c.clear()
        self.assertIsNone(c.get_tokens("alpha"))
        self.assertEqual(len(c), 0)


class TestIncrementalEquivalence(unittest.TestCase):
    """The correctness guard: incremental result == cold rebuild."""

    DOC_A = _doc(40)
    DOC_B = DOC_A.replace("subsystem 20", "subsystem 21")   # same length, one para edited

    def _assert_equivalent(self, incremental_engine, text):
        cold = make_engine()
        cold.index(text, "doc.txt")
        self.assertEqual([c.text for c in incremental_engine.chunks],
                         [c.text for c in cold.chunks])
        self.assertEqual([(c.start, c.end) for c in incremental_engine.chunks],
                         [(c.start, c.end) for c in cold.chunks])
        for q in ("subsystem 20", "node reports status", "under load"):
            inc_hits, _ = incremental_engine.search_index.retrieve(q, k=5, mode="hybrid")
            cold_hits, _ = cold.search_index.retrieve(q, k=5, mode="hybrid")
            self.assertEqual(inc_hits, cold_hits, f"ranking differs for {q!r}")

    def test_incremental_equals_cold_after_edit(self):
        eng = make_engine()
        eng.index(self.DOC_A, "doc.txt")
        eng.update(self.DOC_B)
        self._assert_equivalent(eng, self.DOC_B)

    def test_incremental_equals_cold_for_sample_document(self):
        text = SAMPLE.read_text(encoding="utf-8")
        edited = text.replace("handshake did not complete", "handshake did not finalize")
        eng = make_engine()
        eng.index(text, SAMPLE.name)
        eng.update(edited)
        self._assert_equivalent(eng, edited)


class TestReuseReporting(unittest.TestCase):
    DOC = _doc(40)

    def test_cold_index_reuses_nothing(self):
        eng = make_engine()
        info = eng.index(self.DOC, "doc.txt")
        self.assertEqual(info["reused_chunks"], 0)
        self.assertEqual(info["changed_chars"], len(self.DOC))

    def test_noop_reindex_reuses_everything(self):
        eng = make_engine()
        eng.index(self.DOC, "doc.txt")
        info = eng.index(self.DOC, "doc.txt")
        self.assertEqual(info["changed_chars"], 0)
        self.assertEqual(info["reused_chunks"], info["n_chunks"])

    def test_one_paragraph_edit_reuses_the_rest(self):
        eng = make_engine()
        eng.index(self.DOC, "doc.txt")
        edited = self.DOC.replace("subsystem 20", "subsystem 21")
        info = eng.update(edited)
        # "subsystem 20" -> "subsystem 21" differs in exactly one character, and
        # the prefix/suffix diff reports exactly that. It is char-level, not
        # paragraph-level — so an edit that changes one digit re-encodes only the
        # one passage containing it, nothing more.
        self.assertEqual(info["changed_chars"], 1)
        # Every passage except the one containing the edit is reused.
        self.assertEqual(info["reused_chunks"], info["n_chunks"] - 1)
        self.assertGreater(info["reused_chunks"], 0)

    def test_insertion_shifts_offsets_but_reuses_text(self):
        eng = make_engine()
        eng.index(self.DOC, "doc.txt")
        # Inserting text in the middle moves every later passage's offset.
        edited = self.DOC.replace("Section 20 covers", "Section 20 (revised) covers")
        info = eng.update(edited)
        for c in eng.chunks:                       # offsets must still be exact
            self.assertEqual(c.text, edited[c.start:c.end])
        self.assertGreater(info["reused_chunks"], info["n_chunks"] - 3)

    def test_offsets_stay_exact_after_incremental_update(self):
        eng = make_engine()
        eng.index(self.DOC, "doc.txt")
        # An edit of a DIFFERENT length must shift every later passage's offsets.
        edited = self.DOC.replace("subsystem 20", "subsystem 2020 and more")
        eng.update(edited)
        for c in eng.chunks:
            self.assertEqual(c.text, edited[c.start:c.end])

    def test_new_document_does_not_reuse_the_old_cache(self):
        eng = make_engine()
        eng.index(self.DOC, "doc.txt")
        info = eng.index(_doc(15), "other.txt")     # a different document
        # Nothing from the previous document should be reusable.
        self.assertLess(info["reused_chunks"], info["n_chunks"])

    def test_encoder_change_invalidates_the_cache(self):
        # ⚠️ A vector is only meaningful for the model that produced it. If the
        # selected encoder changes, every cached vector must be dropped — reusing
        # them across models returns plausible-looking garbage.
        from raggy.encoder import EncoderChoice
        eng = make_engine()
        eng.index(self.DOC, "doc.txt")
        self.assertGreater(len(eng._cache), 0)
        eng._encoder_choice = lambda: EncoderChoice(None, "some-other-model", "lsa", "test")
        info = eng.index(self.DOC, "doc.txt")
        self.assertEqual(info["reused_chunks"], 0)

    def test_prepend_reuses_all_later_chunks(self):
        # Inserting at the very top shifts every later chunk's POSITION. Because
        # the cache is keyed by text, not position, they are all still reusable —
        # which is the whole reason for the design.
        eng = make_engine()
        eng.index(self.DOC, "doc.txt")
        prepended = "A NEW FIRST LINE.\n\n" + self.DOC
        info = eng.update(prepended)
        self.assertGreaterEqual(info["reused_chunks"], info["n_chunks"] - 1)


if __name__ == "__main__":
    unittest.main()
