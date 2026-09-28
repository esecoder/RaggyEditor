#!/usr/bin/env python3
"""
onnx_encoder.py — the real sentence encoder, served by ONNX Runtime.

===============================================================================
WHY ONNX AND NOT sentence-transformers
===============================================================================
`sentence-transformers` is the easy way to run a real encoder, and it is the
wrong way to SHIP one: it pulls in PyTorch, which is hundreds of megabytes, and
it is the single biggest thing in the installer. ONNX Runtime runs the same
weights with a runtime measured in tens of megabytes.

The embeddings are equivalent — same weights, same pooling, same normalisation.
Only the runtime differs. So the shipping path is ONNX and the torch path is
kept as an optional development fallback.

⚠️ THE THREE DETAILS THAT SILENTLY BREAK THIS, all handled below:

  1. POOLING. BGE uses the **CLS token** (`last_hidden_state[:, 0, :]`), NOT mean
     pooling. Mean pooling over a BGE model returns a vector that is close enough
     to look plausible and far enough to lose recall. No error is raised.
  2. NORMALISATION. The output is L2-normalised, so cosine similarity is a plain
     dot product. Skip it and "cosine" silently becomes a length-weighted score.
  3. QUERY PREFIX, ON QUERIES ONLY. BGE is trained with an instruction prepended
     to the query and NOT to the passage. Applying it to both, or to neither,
     costs recall and raises nothing.

It also uses `tokenizers` directly rather than `transformers`, because
`transformers` drags in torch as a dependency here. `tokenizers` reads the same
`tokenizer.json` with a fraction of the footprint.
"""

from __future__ import annotations

import numpy as np

from raggy import model_store

DEFAULT_MODEL_ID = model_store.DEFAULT_MODEL_ID
MAX_LENGTH = 512


class OnnxEncoder:
    """A real sentence encoder over ONNX Runtime.

    Same duck-typed interface as the torch-backed encoder, so it drops straight
    into `NeuralDenseIndex`: `.model_name`, `.dim`, `.encode_docs()`,
    `.encode_query()`.
    """

    QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

    def __init__(self, model_id: str = DEFAULT_MODEL_ID, model_dir=None,
                 allow_download: bool = False, progress=None, threads: int | None = None):
        import onnxruntime as ort                                   # noqa: PLC0415
        from tokenizers import Tokenizer                            # noqa: PLC0415

        self.model_id = model_id
        self.dir = model_store.ensure(model_id, allow_download=allow_download,
                                      progress=progress)
        self.model_name = f"{model_id} [onnx]"

        self.tokenizer = Tokenizer.from_file(str(self.dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=MAX_LENGTH)
        # BERT-family pad id/token; right padding keeps CLS at index 0.
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")

        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if threads:
            opts.intra_op_num_threads = threads
        self.session = ort.InferenceSession(
            str(self.dir / "onnx" / "model.onnx"),
            sess_options=opts,
            providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self.session.get_inputs()}
        self._outputs = [o.name for o in self.session.get_outputs()]

        # Probe the dimensionality rather than hard-coding 384: a different model
        # in the same slot must not silently reshape the index.
        self.dim = int(self._forward([""]).shape[1])

    # ------------------------------------------------------------- inference
    def _forward(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim if hasattr(self, "dim") else 1), dtype=np.float32)
        encs = self.tokenizer.encode_batch(texts)
        feed = {
            "input_ids": np.array([e.ids for e in encs], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encs], dtype=np.int64),
        }
        # BERT-style graphs usually declare token_type_ids; it is all zeros here.
        if "token_type_ids" in self._inputs and "token_type_ids" not in feed:
            feed["token_type_ids"] = np.zeros_like(feed["input_ids"])
        feed = {k: v for k, v in feed.items() if k in self._inputs}

        out = self.session.run(None, feed)
        hidden = out[self._outputs.index("last_hidden_state")] if "last_hidden_state" in self._outputs else out[0]

        # ⚠️ CLS pooling, not mean pooling (see the module docstring).
        vecs = np.asarray(hidden)[:, 0, :].astype(np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return vecs / np.maximum(norms, 1e-12)

    # ------------------------------------------------------- public interface
    def encode_docs(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        chunks = [self._forward(texts[i:i + batch_size])
                  for i in range(0, len(texts), batch_size)]
        return np.vstack(chunks)

    def encode_query(self, query: str) -> np.ndarray:
        # ⚠️ Prefix the QUERY only.
        return self._forward([self.QUERY_PREFIX + query])[0]


def available() -> bool:
    """True if onnxruntime + tokenizers import. Does not load or download a model."""
    try:
        import onnxruntime        # noqa: F401,PLC0415
        import tokenizers         # noqa: F401,PLC0415
        return True
    except Exception:
        return False
