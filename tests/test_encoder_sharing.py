"""One neural encoder per process, and none of the dangerous sharing.

⚠️ WHY THIS EXISTS. Each editor window owns its own RaggyEngine, and an engine
resolves its own encoder, so without sharing, opening ten documents would build
ten ONNX sessions — a model load and tens of megabytes each.

⚠️ AND WHY ONLY THE NEURAL ONE IS SHARED. LSA is not an object: `resolve` returns
`encoder=None` for it and the index fits its own TF-IDF+SVD on the document it is
given. Sharing that would leak one document's vocabulary and singular vectors into
another document's results — a wrong answer with no error, which is the failure
mode this whole codebase keeps trying to avoid.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from raggy import encoder  # noqa: E402


class TestSharedEncoder(unittest.TestCase):

    def setUp(self):
        encoder.forget_shared_encoders()
        self.addCleanup(encoder.forget_shared_encoders)

    def test_the_factory_runs_once_and_the_object_is_reused(self):
        calls = []

        def factory():
            calls.append(1)
            return {"encoder": len(calls)}

        first = encoder.shared_encoder("onnx", "model-x", factory)
        second = encoder.shared_encoder("onnx", "model-x", factory)
        self.assertIs(first, second)
        self.assertEqual(len(calls), 1, "the model was built more than once")
        self.assertEqual(encoder.shared_encoder_count(), 1)

    def test_different_models_get_different_encoders(self):
        a = encoder.shared_encoder("onnx", "model-a", lambda: object())
        b = encoder.shared_encoder("onnx", "model-b", lambda: object())
        self.assertIsNot(a, b)
        self.assertEqual(encoder.shared_encoder_count(), 2)

    def test_different_kinds_get_different_encoders(self):
        a = encoder.shared_encoder("onnx", "m", lambda: object())
        b = encoder.shared_encoder("neural", "m", lambda: object())
        self.assertIsNot(a, b)

    def test_a_failing_factory_is_not_cached(self):
        # A half-built encoder must not be handed to every later caller.
        def boom():
            raise RuntimeError("model missing")

        with self.assertRaises(RuntimeError):
            encoder.shared_encoder("onnx", "model-x", boom)
        self.assertEqual(encoder.shared_encoder_count(), 0)

    def test_forget_clears_the_cache(self):
        encoder.shared_encoder("onnx", "m", lambda: object())
        encoder.forget_shared_encoders()
        self.assertEqual(encoder.shared_encoder_count(), 0)

    def test_lsa_is_never_shared(self):
        # ⚠️ The dangerous case. `encoder=None` means the index builds its own
        # TF-IDF+SVD per document; nothing may be cached here.
        choice_a = encoder.resolve("lsa")
        choice_b = encoder.resolve("lsa")
        self.assertIsNone(choice_a.encoder)
        self.assertIsNone(choice_b.encoder)
        self.assertEqual(encoder.shared_encoder_count(), 0,
                         "LSA was cached, so two documents could share a fit")
        self.assertFalse(choice_a.is_neural)


class TestEncoderChoice(unittest.TestCase):

    def test_lsa_describes_itself_as_offline(self):
        choice = encoder.resolve("lsa")
        self.assertEqual(choice.kind, "lsa")
        self.assertIn("lsa", choice.describe().lower())

    def test_an_unknown_kind_is_rejected_loudly(self):
        with self.assertRaises(ValueError):
            encoder.resolve("telepathy")


if __name__ == "__main__":
    unittest.main()
