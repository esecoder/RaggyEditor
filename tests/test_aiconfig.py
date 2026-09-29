"""ai config: where the LLM settings live, and what counts as "set up".

The important properties are that a local model needs no key, that cloud models
do, and that the credential file is not world-readable.
"""

import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from raggy.aiconfig import AIConfig, is_local_url  # noqa: E402

CLEAN_ENV = {"OPENAI_API_KEY": None, "OPENAI_BASE_URL": None, "OPENAI_MODEL": None}


class TestAIConfig(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        for k, v in CLEAN_ENV.items():
            if v is None:
                os.environ.pop(k, None)

    # ---- local vs remote ------------------------------------------------
    def test_local_addresses_are_recognised(self):
        for url in ("http://localhost:11434/v1", "http://127.0.0.1:1234/v1",
                    "http://my-box.local:8000/v1"):
            self.assertTrue(is_local_url(url), url)
        for url in ("https://api.deepseek.com/v1", "https://api.openai.com/v1"):
            self.assertFalse(is_local_url(url), url)

    def test_a_local_model_needs_no_key(self):
        cfg = AIConfig(provider="openai", base_url="http://localhost:11434/v1",
                       model="llama3.2", api_key="")
        self.assertFalse(cfg.needs_key)
        self.assertTrue(cfg.configured)

    def test_a_cloud_model_requires_a_key(self):
        cfg = AIConfig(provider="deepseek", base_url="https://api.deepseek.com/v1",
                       model="deepseek-chat", api_key="")
        self.assertTrue(cfg.needs_key)
        self.assertFalse(cfg.configured)
        cfg.api_key = "sk-test"
        self.assertTrue(cfg.configured)

    def test_an_address_and_a_model_are_both_required(self):
        self.assertFalse(AIConfig(provider="deepseek", base_url="", model="m").configured)
        self.assertFalse(AIConfig(provider="deepseek", base_url="http://x/v1").configured)

    def test_a_local_client_gets_a_placeholder_key(self):
        # Some servers reject a missing Authorization header outright.
        cfg = AIConfig(base_url="http://localhost:11434/v1", model="llama3.2")
        self.assertTrue(cfg.build_client().available)

    # ---- persistence -----------------------------------------------------
    def test_save_then_load_round_trips(self):
        AIConfig(provider="deepseek", base_url="https://api.deepseek.com/v1",
                 model="deepseek-chat", api_key="sk-secret").save()
        back = AIConfig.load()
        self.assertEqual(back.model, "deepseek-chat")
        self.assertEqual(back.api_key, "sk-secret")
        self.assertTrue(back.configured)

    def test_the_credential_file_is_not_world_readable(self):
        AIConfig(provider="x", base_url="https://api.example/v1",
                 model="m", api_key="sk-secret").save()
        from raggy.aiconfig import config_file
        mode = stat.S_IMODE(config_file().stat().st_mode)
        self.assertEqual(mode, 0o600, f"key file is {oct(mode)}, should be 0600")

    def test_clear_removes_it(self):
        AIConfig(base_url="https://api.example/v1", model="m", api_key="k").save()
        AIConfig().clear()
        self.assertFalse(AIConfig.load().configured)

    def test_a_corrupt_file_is_ignored_rather_than_crashing(self):
        from raggy.aiconfig import config_file
        config_file().parent.mkdir(parents=True, exist_ok=True)
        config_file().write_text("{not json", encoding="utf-8")
        self.assertFalse(AIConfig.load().configured)

    # ---- environment fallback -------------------------------------------
    def test_environment_variables_are_honoured_with_no_file(self):
        with mock.patch.dict(os.environ, {
                "OPENAI_API_KEY": "sk-env",
                "OPENAI_BASE_URL": "https://api.openai.com/v1",
                "OPENAI_MODEL": "gpt-4o-mini"}):
            cfg = AIConfig.load()
        self.assertTrue(cfg.configured)
        self.assertEqual(cfg.api_key, "sk-env")
        self.assertTrue(cfg.build_client().available)

    def test_the_file_wins_over_the_environment(self):
        AIConfig(provider="deepseek", base_url="https://api.deepseek.com/v1",
                 model="deepseek-chat", api_key="sk-file").save()
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-env"}):
            self.assertEqual(AIConfig.load().api_key, "sk-file")

    def test_describe_says_something_useful_in_both_states(self):
        self.assertEqual(AIConfig().describe(), "not set up")
        local = AIConfig(base_url="http://localhost:11434/v1", model="llama3.2")
        self.assertIn("on this computer", local.describe())


if __name__ == "__main__":
    unittest.main()
