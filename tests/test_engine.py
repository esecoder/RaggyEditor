"""Engine tests: provenance, search, abstention, honest degradation.

Hermetic: LSA encoder, and an LLM client with no key, so nothing touches the
network or downloads a model.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from raggy.engine import RaggyEngine  # noqa: E402
from raggy.llm import LLMClient  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
SAMPLE = REPO / "samples" / "meridian-operations-handbook.txt"


def make_engine() -> RaggyEngine:
    # No key -> the answer path must degrade, not invent.
    return RaggyEngine(encoder_kind="lsa", llm=LLMClient(api_key=""))


class TestEngine(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = SAMPLE.read_text(encoding="utf-8")
        cls.eng = make_engine()
        cls.info = cls.eng.index(cls.text, SAMPLE.name)

    # ---- indexing / provenance -------------------------------------------
    def test_index_reports_shape(self):
        self.assertGreater(self.info["n_chunks"], 1)
        self.assertEqual(self.info["chars"], len(self.text))
        self.assertEqual(self.info["encoder"], "lsa-tfidf-svd")
        self.assertFalse(self.info["is_neural"])

    def test_every_chunk_offset_is_exact(self):
        for c in self.eng.chunks:
            self.assertEqual(c.text, self.text[c.start:c.end])

    def test_locate_offset_to_line(self):
        # First character is line 1, col 1.
        self.assertEqual(self.eng.locate(0), (1, 1))
        nl = self.text.index("\n")
        self.assertEqual(self.eng.locate(nl + 1), (2, 1))

    # ---- regex (the classic Find) ----------------------------------------
    def test_literal_find(self):
        res = self.eng.regex_search("ERR_CONN_4421", regex=False)
        self.assertGreaterEqual(res["count"], 1)
        for h in res["results"]:
            self.assertEqual(h["text"], "ERR_CONN_4421")
            self.assertEqual(self.text[h["start"]:h["end"]], "ERR_CONN_4421")
            self.assertGreaterEqual(h["line"], 1)

    def test_regex_find(self):
        res = self.eng.regex_search(r"ERR_[A-Z]+_\d+", regex=True)
        self.assertGreaterEqual(res["count"], 4)

    def test_invalid_regex_is_a_message_not_a_crash(self):
        res = self.eng.regex_search("([unclosed", regex=True)
        self.assertIn("error", res)
        self.assertEqual(res["results"], [])

    # ---- semantic --------------------------------------------------------
    def test_paraphrase_finds_the_right_passage(self):
        res = self.eng.semantic_search("how do I fix an expired certificate on a replica", k=3)
        self.assertTrue(res["results"])
        self.assertFalse(res["low_confidence"])
        self.assertGreater(res["confidence"], self.eng.threshold)
        joined = " ".join(h["text"] for h in res["results"])
        self.assertIn("handshake", joined)

    def test_semantic_offsets_map_back_to_document(self):
        res = self.eng.semantic_search("replication lag", k=3)
        for h in res["results"]:
            self.assertEqual(self.text[h["start"]:h["end"]], h["text"])

    def test_out_of_scope_is_low_confidence(self):
        res = self.eng.semantic_search("what is the capital of France", k=3)
        self.assertTrue(res["low_confidence"])
        self.assertLess(res["confidence"], self.eng.threshold)

    def test_search_before_index(self):
        fresh = make_engine()
        res = fresh.semantic_search("anything")
        self.assertEqual(res["results"], [])
        self.assertIn("error", res)

    # ---- ask / abstention ------------------------------------------------
    def test_ask_without_key_degrades_to_retrieval(self):
        res = self.eng.ask("how do I fix an expired certificate on a replica")
        self.assertEqual(res["mode"], "retrieval_only")
        self.assertFalse(res["abstained"])
        self.assertIsNone(res["answer"])          # never fabricate prose
        self.assertTrue(res["citations"])

    def test_ask_out_of_scope_abstains(self):
        res = self.eng.ask("what is the capital of France")
        self.assertEqual(res["mode"], "abstained")
        self.assertTrue(res["abstained"])
        self.assertIsNone(res["answer"])

    def test_cited_indices_are_parsed_and_bounded(self):
        self.assertEqual(RaggyEngine._cited_indices("see [1] and [2][3]", 3), [0, 1, 2])
        self.assertEqual(RaggyEngine._cited_indices("see [9] and [1]", 3), [0])   # [9] dropped
        self.assertEqual(RaggyEngine._cited_indices("no cites", 3), [])


if __name__ == "__main__":
    unittest.main()
