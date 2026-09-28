"""Model-store tests: caching, and the deliberate refusal to auto-download.

Hermetic — nothing here touches the network. The download path itself is
exercised by `./run.sh install-model`, not by the test suite.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from raggy import model_store  # noqa: E402


class TestModelStore(unittest.TestCase):

    def test_cache_path_is_per_model_and_sanitised(self):
        p = model_store.model_dir("BAAI/bge-small-en-v1.5")
        self.assertIn("BAAI--bge-small-en-v1.5", str(p))
        # No raw slash that would silently nest an extra directory.
        self.assertEqual(p.name, "BAAI--bge-small-en-v1.5")
        self.assertEqual(p.parent, model_store.cache_root())

    def test_env_override_moves_the_cache(self):
        import os
        old = os.environ.get("RAGGY_MODEL_DIR")
        try:
            os.environ["RAGGY_MODEL_DIR"] = "/tmp/raggy-test-cache"
            self.assertEqual(model_store.cache_root(), pathlib.Path("/tmp/raggy-test-cache"))
        finally:
            if old is None:
                os.environ.pop("RAGGY_MODEL_DIR", None)
            else:
                os.environ["RAGGY_MODEL_DIR"] = old

    def test_ensure_refuses_to_download_implicitly(self):
        # A 133 MB fetch triggered by typing a search term would be a hostile
        # surprise. Without allow_download it must raise, whatever the cache
        # state, unless the model is genuinely already present.
        if model_store.is_available():
            self.skipTest("model is present in this environment")
        with self.assertRaises(model_store.ModelUnavailable):
            model_store.ensure(allow_download=False)

    def test_unavailable_model_is_not_reported_available(self):
        self.assertFalse(model_store.is_available("definitely/not-a-real-model-xyz"))

    def test_sizes_reports_missing_as_zero(self):
        s = model_store.sizes("definitely/not-a-real-model-xyz")
        self.assertTrue(all(v == 0 for v in s.values()))
        self.assertIn("onnx/model.onnx", s)

    def test_required_files_include_the_two_that_matter(self):
        self.assertIn("onnx/model.onnx", model_store.REQUIRED_FILES)
        self.assertIn("tokenizer.json", model_store.REQUIRED_FILES)


class TestEncoderPolicy(unittest.TestCase):

    def test_onnx_is_a_valid_kind(self):
        from raggy.encoder import VALID_KINDS
        self.assertIn("onnx", VALID_KINDS)
        self.assertIn("lsa", VALID_KINDS)

    def test_lsa_never_needs_a_model(self):
        from raggy.encoder import resolve
        c = resolve("lsa")
        self.assertFalse(c.is_neural)
        self.assertIsNone(c.encoder)

    def test_unknown_kind_rejected(self):
        from raggy.encoder import resolve
        with self.assertRaises(ValueError):
            resolve("telepathy")

    def test_auto_never_raises_even_with_nothing_installed(self):
        # `auto` is the default the app ships with; it must always return
        # something usable, falling back to LSA rather than dying.
        from raggy.encoder import resolve
        c = resolve("auto")
        self.assertIn(c.kind, ("onnx", "neural", "lsa"))
        self.assertTrue(c.name)


if __name__ == "__main__":
    unittest.main()
