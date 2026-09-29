"""
aiconfig.py — how a user connects RaggyEditor to an LLM.

The Ask feature retrieves passages on its own, but writing a cited answer needs a
language model. This module is where the user tells RaggyEditor which one to use,
and where the credential is kept.

===============================================================================
WHERE THE KEY LIVES (read this before trusting it)
===============================================================================
The key is stored in a small JSON file with mode 0600 inside the user's config
directory:

    ~/.config/RaggyEditor/ai.json          (or $XDG_CONFIG_HOME/RaggyEditor)

⚠️ It is stored in **plain text and is not encrypted**. File permissions are the
only protection. That is a deliberate trade so the project needs no keychain
dependency; if that is not acceptable for a given key, the environment variables
below still work and take effect when no file is present.

Environment variables are also honoured, so a user who prefers not to write a key
to disk can export it instead:

    OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL

A local server (Ollama, LM Studio, llama.cpp) needs no key at all. Rather than
requiring a dummy one, `needs_key` is False whenever the base URL points at
localhost, and a placeholder is sent so the client is willing to make a request.
"""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass

from raggy.llm import LLMClient

APP_DIR_NAME = "RaggyEditor"
CONFIG_FILE_NAME = "ai.json"
LOCAL_PLACEHOLDER_KEY = "local"          # sent to servers that ignore the key

# Presets. `needs_key` is the starting point; a localhost URL always overrides it.
PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek (cloud, needs a key)",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "needs_key": True,
    },
    "openai": {
        "label": "OpenAI (cloud, needs a key)",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "needs_key": True,
    },
    "ollama": {
        "label": "Ollama (runs on this computer, no key)",
        "base_url": "http://localhost:11434/v1",
        "model": "llama3.2",
        "needs_key": False,
    },
    "lmstudio": {
        "label": "LM Studio (runs on this computer, no key)",
        "base_url": "http://localhost:1234/v1",
        "model": "local-model",
        "needs_key": False,
    },
    "custom": {
        "label": "Something else (any OpenAI-compatible server)",
        "base_url": "",
        "model": "",
        "needs_key": True,
    },
}
DEFAULT_PROVIDER = "deepseek"

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


def is_local_url(url: str) -> bool:
    """True when the endpoint is on this machine, so no key is expected."""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").strip().lower()
    except Exception:                                               # noqa: BLE001
        return False
    return host in _LOCAL_HOSTS or host.endswith(".local")


def config_dir() -> pathlib.Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return pathlib.Path(base) / APP_DIR_NAME


def config_file() -> pathlib.Path:
    return config_dir() / CONFIG_FILE_NAME


@dataclass
class AIConfig:
    """Which LLM to use for answers. Empty fields mean 'not set up'."""

    provider: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = ""

    # ------------------------------------------------------------- loading
    @classmethod
    def load(cls) -> "AIConfig":
        """File first; fall back to environment variables."""
        cfg = cls._from_file()
        if cfg and cfg.base_url and cfg.model:
            return cfg
        env = cls._from_env()
        return env if env.base_url or env.model or env.api_key else cls()

    @classmethod
    def _from_file(cls) -> "AIConfig | None":
        path = config_file()
        try:
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:                                           # noqa: BLE001
            return None
        if not isinstance(data, dict):
            return None
        return cls(
            provider=str(data.get("provider", "") or ""),
            base_url=str(data.get("base_url", "") or ""),
            model=str(data.get("model", "") or ""),
            api_key=str(data.get("api_key", "") or ""),
        )

    @classmethod
    def _from_env(cls) -> "AIConfig":
        return cls(
            provider="env" if os.environ.get("OPENAI_API_KEY") else "",
            base_url=os.environ.get("OPENAI_BASE_URL", "") or "",
            model=os.environ.get("OPENAI_MODEL", "") or "",
            api_key=os.environ.get("OPENAI_API_KEY", "") or "",
        )

    # ------------------------------------------------------------- saving
    def save(self):
        """Write with mode 0600. ⚠️ Plain text; see the module docstring."""
        d = config_dir()
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        path = config_file()
        payload = json.dumps({
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_key": self.api_key,
        }, indent=2)
        # Create with restrictive permissions from the start, so the key is
        # never briefly world-readable.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)

    def clear(self):
        try:
            config_file().unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------ derived
    @property
    def needs_key(self) -> bool:
        if is_local_url(self.base_url):
            return False
        preset = PROVIDERS.get(self.provider, {})
        if self.provider == "env":
            return True
        return bool(preset.get("needs_key", True))

    @property
    def configured(self) -> bool:
        if not (self.base_url and self.model):
            return False
        return bool(self.api_key) or not self.needs_key

    def describe(self) -> str:
        if not self.configured:
            return "not set up"
        where = "on this computer" if is_local_url(self.base_url) else self.base_url
        return f"{self.model} at {where}"

    def build_client(self) -> LLMClient:
        key = self.api_key
        if not key and not self.needs_key:
            key = LOCAL_PLACEHOLDER_KEY      # some servers require the field
        return LLMClient(api_key=key or None,
                         base_url=self.base_url or None,
                         model=self.model or None)
