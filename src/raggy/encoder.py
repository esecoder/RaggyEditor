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

VALID_KINDS = ("auto", "onnx", "neural", "lsa")
LSA_NAME = "lsa-tfidf-svd"


@dataclass
class EncoderChoice:
    """What we ended up using, and why."""

    encoder: object | None          # None means LSA; otherwise a neural encoder
    name: str                        # "lsa-tfidf-svd" or the model name
    kind: str                        # the resolved kind actually used
    note: str                        # one line for the UI / logs

    @property
    def is_neural(self) -> bool:
        return self.encoder is not None

    def describe(self) -> str:
        return f"{'NEURAL' if self.is_neural else 'LSA'} ({self.name}) — {self.note}"


def neural_importable() -> bool:
    """True if the torch-backed sentence-transformers can be imported."""
    try:
        import sentence_transformers  # noqa: F401,PLC0415
        return True
    except Exception:
        return False


def onnx_importable() -> bool:
    """True if onnxruntime + tokenizers can be imported (the shipping path)."""
    try:
        from raggy import onnx_encoder  # noqa: PLC0415
        return onnx_encoder.available()
    except Exception:
        return False


def resolve(kind: str | None = None, model_name: str | None = None,
            cache_folder: str | None = None, allow_download: bool = False,
            progress=None) -> EncoderChoice:
    """Pick an encoder according to `kind` (default from RAGGY_ENCODER or auto).

    Priority for `auto`: ONNX (small runtime, the shipping path) -> torch-backed
    sentence-transformers (development) -> LSA (offline fallback).

    ⚠️ `auto` never downloads a model. A 130 MB fetch triggered by a search
    keystroke would be a hostile surprise; the caller opts in via
    `allow_download=True` (or the `onnx` kind).
    """
    kind = (kind or os.environ.get("RAGGY_ENCODER") or "auto").lower()
    model_name = model_name or os.environ.get("RAGGY_ENCODER_MODEL") or DEFAULT_ENCODER
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown RAGGY_ENCODER={kind!r}; have {list(VALID_KINDS)}")

    if kind == "lsa":
        return EncoderChoice(None, LSA_NAME, "lsa",
                             "offline TF-IDF+SVD; no download, weaker on paraphrase")

    # ---- ONNX (preferred) ------------------------------------------------
    if kind in ("auto", "onnx"):
        if kind == "onnx" and not onnx_importable():
            raise RuntimeError(
                "RAGGY_ENCODER=onnx but onnxruntime/tokenizers are not installed.\n"
                "    Install them:  ./run.sh install-model")
        if onnx_importable():
            from raggy import model_store                          # noqa: PLC0415
            from raggy.onnx_encoder import OnnxEncoder              # noqa: PLC0415
            ready = model_store.is_available(model_name)
            if ready or allow_download or kind == "onnx":
                try:
                    enc = OnnxEncoder(model_name, allow_download=allow_download or kind == "onnx",
                                      progress=progress)
                    note = ("real sentence encoder via ONNX; semantic paraphrase works"
                            if ready else "model downloaded to the local cache")
                    return EncoderChoice(enc, enc.model_name, "onnx", note)
                except Exception as e:                              # noqa: BLE001
                    if kind == "onnx":
                        raise
                    # auto + download/model problem: degrade, and say so.
                    return EncoderChoice(None, LSA_NAME, "lsa",
                                         f"onnx encoder unavailable ({type(e).__name__}); "
                                         "fell back to LSA")
            # auto, onnxruntime present but the model was never fetched.
            if kind == "auto":
                pass    # fall through to the torch path, then LSA

    # ---- torch-backed sentence-transformers (development) -----------------
    if kind in ("auto", "neural"):
        if kind == "neural" and not neural_importable():
            raise RuntimeError(
                "RAGGY_ENCODER=neural but sentence-transformers is not installed.\n"
                "    Install it:  ./run.sh install-neural\n"
                "    Or allow the fallback:  export RAGGY_ENCODER=auto")
        if neural_importable():
            try:
                enc = NeuralIndex(model_name, cache_folder=cache_folder)
                return EncoderChoice(enc, enc.model_name, "neural",
                                     "real sentence encoder (torch); semantic paraphrase works")
            except Exception as e:                                  # noqa: BLE001
                if kind == "neural":
                    raise
                return EncoderChoice(None, LSA_NAME, "lsa",
                                     f"neural unavailable ({type(e).__name__}); fell back to LSA")

    # ---- offline ----------------------------------------------------------
    note = "offline TF-IDF+SVD (real encoder not downloaded); weaker on paraphrase"
    return EncoderChoice(None, LSA_NAME, "lsa", note)
