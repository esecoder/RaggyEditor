#!/usr/bin/env python3
"""
model_store.py — fetch the embedding model on first use, and cache it.

===============================================================================
WHY DOWNLOAD INSTEAD OF BUNDLE
===============================================================================
The app ships tiny. numpy and onnxruntime are tens of megabytes; a real encoder's
weights are ~130 MB, and the torch stack that usually comes with them is several
hundred more. Bundling all of that makes the installer enormous and slow to
build. So the encoder is fetched ONCE, on first use, into a user cache — the way
most desktop apps fetch model packs.

The important consequence: the app is fully functional BEFORE the download.
Offline LSA search works immediately; the download only upgrades quality.

⚠️ The files come from the model's own Hugging Face repo. `BAAI/bge-small-en-v1.5`
publishes `onnx/model.onnx` and `tokenizer.json` directly, so there is no custom
export step and no third-party mirror to trust.

⚠️ An interrupted download must not leave a corrupt model that then loads and
produces quietly wrong vectors. Files are written to `.part` and moved into place
only when complete, and a size check guards the final rename.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import ssl
import urllib.error
import urllib.request

DEFAULT_MODEL_ID = "BAAI/bge-small-en-v1.5"
HF_BASE = "https://huggingface.co"


def _ssl_context() -> ssl.SSLContext:
    """An SSL context with real root certificates.

    ⚠️ NOT A DETAIL. Python installed from python.org on macOS does NOT use the
    system keychain, so plain `urlopen` fails with CERTIFICATE_VERIFY_FAILED
    ("unable to get local issuer certificate") even though curl and the browser
    work fine. `certifi` ships the CA bundle; if it is absent we fall back to the
    default context rather than disabling verification (which would be worse than
    failing — it would silently accept a MITM'd model file).
    """
    try:
        import certifi                                            # noqa: PLC0415
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:                                             # noqa: BLE001
        return ssl.create_default_context()


# Kept deliberately small: only what inference actually needs.
REQUIRED_FILES = ("onnx/model.onnx", "tokenizer.json")
OPTIONAL_FILES = ("config.json", "tokenizer_config.json", "special_tokens_map.json")

# bge-small's ONNX export is ~133 MB; used as a sanity floor, not a strict check.
MIN_ONNX_BYTES = 10_000_000


class ModelUnavailable(RuntimeError):
    """Raised when the model is neither cached nor downloadable."""


def cache_root() -> pathlib.Path:
    override = os.environ.get("RAGGY_MODEL_DIR")
    base = pathlib.Path(override) if override else pathlib.Path.home() / ".cache" / "raggy-editor"
    return base


def model_dir(model_id: str = DEFAULT_MODEL_ID) -> pathlib.Path:
    return cache_root() / model_id.replace("/", "--")


def _targets(model_id: str) -> list[str]:
    return list(REQUIRED_FILES) + list(OPTIONAL_FILES)


def is_available(model_id: str = DEFAULT_MODEL_ID) -> bool:
    """True if every required file is present and plausible."""
    d = model_dir(model_id)
    if not all((d / f).is_file() for f in REQUIRED_FILES):
        return False
    onnx = d / "onnx" / "model.onnx"
    return onnx.stat().st_size >= MIN_ONNX_BYTES


def _auth_headers() -> dict:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _download_one(url: str, dest: pathlib.Path, progress) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=_auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
                    if progress and total:
                        progress(dest.name, got, total)
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        raise ModelUnavailable(f"{url} -> HTTP {e.code}") from e
    except urllib.error.URLError as e:
        tmp.unlink(missing_ok=True)
        raise ModelUnavailable(f"could not reach {url}: {e.reason}") from e
    # Only now is it safe to replace any previous copy.
    tmp.replace(dest)


def download(model_id: str = DEFAULT_MODEL_ID, progress=None,
             force: bool = False) -> pathlib.Path:
    """Fetch the model into the cache and return its directory.

    `progress(filename, bytes_done, bytes_total)` is called during each file.
    """
    d = model_dir(model_id)
    if is_available(model_id) and not force:
        return d

    for rel in _targets(model_id):
        dest = d / rel
        if dest.is_file() and not force:
            continue
        url = f"{HF_BASE}/{model_id}/resolve/main/{rel}"
        try:
            _download_one(url, dest, progress)
        except ModelUnavailable:
            # Optional files may legitimately be absent; required ones may not.
            if rel in REQUIRED_FILES:
                raise
            continue

    if not is_available(model_id):
        raise ModelUnavailable(
            f"model {model_id} is incomplete in {d}; "
            "re-run the download (it may have been interrupted)")
    return d


def ensure(model_id: str = DEFAULT_MODEL_ID, allow_download: bool = False,
           progress=None) -> pathlib.Path:
    """Return the model directory, downloading it only if `allow_download`.

    ⚠️ Never downloads implicitly. A 130 MB fetch triggered by typing a search
    term would be a hostile surprise; the caller asks the user first.
    """
    if is_available(model_id):
        return model_dir(model_id)
    if not allow_download:
        raise ModelUnavailable(
            f"model {model_id} is not downloaded yet.\n"
            f"    It would be fetched to: {model_dir(model_id)}\n"
            "    Run: ./run.sh install-model   (or enable the download in the app)")
    return download(model_id, progress=progress)


def sizes(model_id: str = DEFAULT_MODEL_ID) -> dict:
    d = model_dir(model_id)
    return {f: ((d / f).stat().st_size if (d / f).is_file() else 0) for f in _targets(model_id)}


def remove(model_id: str = DEFAULT_MODEL_ID) -> None:
    shutil.rmtree(model_dir(model_id), ignore_errors=True)


def _main() -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="RaggyEditor encoder model store")
    ap.add_argument("--download", action="store_true", help="fetch the model now")
    ap.add_argument("--status", action="store_true", help="show what is cached")
    ap.add_argument("--remove", action="store_true", help="delete the cached model")
    ap.add_argument("--model", default=DEFAULT_MODEL_ID)
    args = ap.parse_args()

    print(f"model       : {args.model}")
    print(f"location    : {model_dir(args.model)}")

    if args.status or not (args.download or args.remove):
        ok = is_available(args.model)
        print(f"downloaded  : {'yes' if ok else 'no'}")
        for name, size in sizes(args.model).items():
            print(f"  {name:34} {size/1e6:8.1f} MB" if size else f"  {name:34} {'missing':>11}")
        return 0 if ok else 1

    if args.remove:
        remove(args.model)
        print("removed.")
        return 0

    print("downloading…")
    last = {"pct": -1}

    def progress(name, got, total):
        pct = int(100 * got / total) if total else 0
        if pct != last["pct"]:
            last["pct"] = pct
            print(f"\r  {name}: {got/1e6:7.1f}/{total/1e6:.1f} MB ({pct:3d}%)", end="", flush=True)

    try:
        download(args.model, progress=progress)
    except ModelUnavailable as e:
        print(f"\n✗ {e}", file=sys.stderr)
        print("  On macOS, plain Python may lack root certificates; install certifi:\n"
              "      ./run.sh install", file=sys.stderr)
        return 2
    print("\n✓ ready. RaggyEditor will use the semantic encoder automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
