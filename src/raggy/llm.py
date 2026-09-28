#!/usr/bin/env python3
"""
llm.py — a minimal OpenAI-compatible chat client, on the standard library.

⚠️ WHY THERE IS NO `requests` OR `openai` HERE. RaggyEditor's promise is that it
runs with numpy and nothing else. `urllib.request` is enough for one POST, and
it removes a dependency from the one path a user is most likely to run first.

⚠️ DEEPSEEK WORKS UNMODIFIED. DeepSeek is OpenAI-compatible for
/chat/completions; it just has no /embeddings endpoint (which is why our
embeddings are always local). Point OPENAI_BASE_URL at it and this class talks
to it.

⚠️ AND IT IS OPTIONAL. With no key, `available` is False and the engine returns
retrieval-only results. We do not fabricate an answer — a search that shows you
the passages is useful; an invented answer is worse than nothing.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

# =============================================================================
# THE GROUNDING PROMPT — where honesty is enforced
# =============================================================================
# ⚠️ The rules here are not decoration. Each one corresponds to a failure mode:
#   * "answer ONLY from the context"  -> prevents the model's world knowledge
#                                        from quietly replacing the document.
#   * "cite [n]"                      -> makes every claim checkable.
#   * "say exactly NOT_IN_DOCUMENT"   -> gives out-of-scope questions an honest
#                                        exit instead of a confident invention.
#   * "do not merge [n]s"             -> a citation that points at the wrong
#                                        passage is worse than no citation.
GROUNDED_SYSTEM = """You answer questions about a single open document.

Rules, in order of importance:
1. Answer ONLY from the numbered context passages. Do not use outside knowledge.
2. Cite the passages you used inline, like [1] or [2][3]. Cite ONLY passages you actually used.
3. If the context does not contain the answer, reply with exactly:
   NOT_IN_DOCUMENT
   and nothing else. Do not guess. Do not use outside knowledge.
4. Be concise. Quote the document's own words where it matters.
5. Never invent a citation number that is not in the context."""


def build_context(passages: list[dict]) -> str:
    """Render retrieved passages as a numbered context block.

    Numbers here are the citation numbers the model must use. They are assigned
    at prompt time, which is why the same list order must be preserved back to
    the UI (see `engine.ask`).
    """
    blocks = []
    for i, p in enumerate(passages, start=1):
        label = p.get("heading") or f"lines around {p.get('line', '?')}"
        blocks.append(f"[{i}] ({label})\n{p['text']}")
    return "\n\n".join(blocks)


def build_user_prompt(question: str, passages: list[dict]) -> str:
    return (
        f"Document context:\n\n{build_context(passages)}\n\n"
        f"---\nQuestion: {question}\n\n"
        "Answer following the rules. Cite passages as [n]. If the answer is not "
        "in the context above, reply with exactly NOT_IN_DOCUMENT."
    )


class LLMClient:
    """OpenAI-compatible /chat/completions over urllib."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, temperature: float = 0.0, timeout: int = 90):
        self.key = (api_key if api_key is not None else os.environ.get("OPENAI_API_KEY") or "").strip()
        self.base = (base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE).rstrip("/")
        self.model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
        self.temperature = temperature
        self.timeout = timeout
        self.calls = 0

    @property
    def available(self) -> bool:
        return bool(self.key)

    def chat(self, system: str, user: str) -> str:
        if not self.available:
            raise RuntimeError(
                "no API key, so no generated answer is possible.\n"
                "    export OPENAI_API_KEY=sk-...\n"
                "    export OPENAI_BASE_URL=https://api.deepseek.com/v1   # optional\n"
                "    export OPENAI_MODEL=deepseek-chat                     # optional\n"
                "Search and highlighting still work without one: `./run.sh demo`.")
        payload = json.dumps({
            "model": self.model,
            "temperature": self.temperature,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"API {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"could not reach {self.base}: {e.reason}") from e
        self.calls += 1
        return body["choices"][0]["message"]["content"]
