#!/usr/bin/env python3
"""
encoder.py — choose the dense encoder, and be honest about which one you got.

===============================================================================
THE ONE DECISION THIS FILE MAKES
===============================================================================
RaggyEditor's semantic search needs a dense representation of each passage. Two
options, and the difference is a real quality difference, not a preference:

  * NEURAL  a real sentence encoder (BAAI/bge-small-en-v1.5 by default). This is
            the production path. It matches paraphrase that shares no rare terms.
  * LSA     TF-IDF + truncated SVD. Zero dependencies, no download, and weaker
            at paraphrase. The honest offline stand-in.

`resolve()` implements the policy the app promises:

    RAGGY_ENCODER=auto    (default) use NEURAL if it is importable, else LSA.
    RAGGY_ENCODER=neural  require NEURAL; fail loudly if it is not available.
    RAGGY_ENCODER=lsa     always LSA. Hermetic; used by the test suite.

⚠️ WHERE THIS BECOMES A LIE. It is very easy to run a demo on LSA, screenshot
"semantic search", and let the viewer assume a transformer. So the active
encoder's name travels with every index and is printed everywhere results are.
If you see `lsa-tfidf-svd`, that is what produced the number.

⚠️ AND VERSION THE INDEX WITH THE MODEL. Query vectors from model A against
document vectors from model B produce plausible-looking garbage: retrieval still
returns results, they are just wrong, and nothing errors. `SearchIndex` carries
`encoder_name` for exactly this reason.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from raggy.retrievers import DEFAULT_ENCODER, NeuralIndex

VALID_KINDS = ("auto", "neural", "lsa")
LSA_NAME = "lsa-tfidf-svd"


@dataclass
class EncoderChoice:
    """What we ended up using, and why."""

    encoder: NeuralIndex | None      # None means LSA
    name: str                        # "lsa-tfidf-svd" or the model name
    kind: str                        # the resolved kind actually used
    note: str                        # one line for the UI / logs

    @property
    def is_neural(self) -> bool:
        return self.encoder is not None

    def describe(self) -> str:
        return f"{'NEURAL' if self.is_neural else 'LSA'} ({self.name}) — {self.note}"


def neural_importable() -> bool:
    """True if sentence-transformers can be imported. Does not load a model."""
    try:
        import sentence_transformers  # noqa: F401,PLC0415
        return True
    except Exception:
        return False


def resolve(kind: str | None = None, model_name: str | None = None,
            cache_folder: str | None = None) -> EncoderChoice:
    """Pick an encoder according to `kind` (default from RAGGY_ENCODER or auto)."""
    kind = (kind or os.environ.get("RAGGY_ENCODER") or "auto").lower()
    model_name = model_name or os.environ.get("RAGGY_ENCODER_MODEL") or DEFAULT_ENCODER
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown RAGGY_ENCODER={kind!r}; have {list(VALID_KINDS)}")

    if kind == "lsa":
        return EncoderChoice(None, LSA_NAME, "lsa",
                             "offline TF-IDF+SVD; no download, weaker on paraphrase")

    if kind == "neural" and not neural_importable():
        raise RuntimeError(
            "RAGGY_ENCODER=neural but sentence-transformers is not installed.\n"
            "    Install it:  ./run.sh install-neural\n"
            "    Or allow the fallback:  export RAGGY_ENCODER=auto")

    # auto, or an explicit neural request that is importable.
    try:
        enc = NeuralIndex(model_name, cache_folder=cache_folder)
        return EncoderChoice(enc, enc.model_name, "neural",
                             "real sentence encoder; semantic paraphrase works")
    except Exception as e:                                  # noqa: BLE001
        if kind == "neural":
            raise
        # auto + anything went wrong (usually offline, no weights). Degrade, and
        # SAY SO rather than silently returning weaker results.
        return EncoderChoice(None, LSA_NAME, "lsa",
                             f"neural unavailable ({type(e).__name__}); fell back to LSA")
