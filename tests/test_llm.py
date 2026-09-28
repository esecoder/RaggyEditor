"""LLM prompt-shape and encoder-policy tests. No network calls are made."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from raggy.encoder import LSA_NAME, resolve  # noqa: E402
from raggy.llm import LLMClient, build_context, build_user_prompt  # noqa: E402

PASSAGES = [
    {"text": "the handshake did not complete", "heading": "Common errors", "line": 63},
    {"text": "workers cannot keep up with the write rate", "heading": "Capacity", "line": 41},
]


class TestLLMClient(unittest.TestCase):
    def test_unavailable_without_key(self):
        c = LLMClient(api_key="")
        self.assertFalse(c.available)
        with self.assertRaises(RuntimeError):
            c.chat("sys", "user")

    def test_context_is_numbered_for_citation(self):
        ctx = build_context(PASSAGES)
        self.assertIn("[1]", ctx)
        self.assertIn("[2]", ctx)

    def test_prompt_carries_question_and_grounding_rule(self):
        p = build_user_prompt("why is it slow?", PASSAGES)
        self.assertIn("why is it slow?", p)
        self.assertIn("NOT_IN_DOCUMENT", p)


class TestEncoderPolicy(unittest.TestCase):
    def test_lsa_is_always_available_offline(self):
        c = resolve("lsa")
        self.assertFalse(c.is_neural)
        self.assertEqual(c.name, LSA_NAME)
        self.assertIsNone(c.encoder)

    def test_invalid_kind_rejected(self):
        with self.assertRaises(ValueError):
            resolve("telepathy")


if __name__ == "__main__":
    unittest.main()
