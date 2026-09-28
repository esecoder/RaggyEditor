#!/usr/bin/env bash
# =============================================================================
# RaggyEditor — one entry point.
#
#   ./run.sh install         create .venv with the full app stack (engine + GUI)
#   ./run.sh install-core    engine only (numpy); CLI/eval, no GUI
#   ./run.sh install-model   download the ONNX encoder (~133 MB, once)
#   ./run.sh app             launch the RaggyEditor desktop app
#   ./run.sh package         freeze into a standalone .app/.exe/AppImage
#   ./run.sh serve           sidecar + browser editor UI (no Qt needed)
#   ./run.sh demo            offline demo: index a sample doc, run real searches
#   ./run.sh search "..."    semantic search from the terminal
#   ./run.sh ask "..."       cited answer from the terminal (needs a key)
#   ./run.sh eval            measure semantic vs regex on ground-truth queries
#   ./run.sh bench           measure what incremental re-indexing saves
#   ./run.sh test            run the test suite
#   ./run.sh install-neural  dev only: torch encoder for comparison (large)
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# Make `python -m raggy.<module>` work without an editable install.
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# Prefer the project venv; fall back to whatever python3 is on PATH.
if [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="${PYTHON:-python3}"
fi

banner() {
  printf '\n\033[1m== %s ==\033[0m\n' "$1"
}

need_py() {
  if ! "$PY" -c "import numpy" 2>/dev/null; then
    echo "✗ numpy is not importable by $PY"
    echo "  Run: ./run.sh install"
    exit 1
  fi
}

# Load .env if present (export every non-comment KEY=VALUE line).
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

cmd="${1:-serve}"
shift || true

case "$cmd" in
  install)
    banner "INSTALL — RaggyEditor app stack"
    command -v uv >/dev/null 2>&1 || { echo "✗ uv not found. Install: https://docs.astral.sh/uv/"; exit 1; }
    uv venv --python 3.12 .venv
    # onnxruntime is pinned <1.20: 1.20+ wheels require macOS 13.4+ and fail to
    # load on macOS 13.0. See README "Platform notes".
    uv pip install --python .venv/bin/python \
      "numpy>=2.1" "onnxruntime>=1.19.2,<1.20" "tokenizers>=0.19" "certifi" \
      "PyQt6>=6.6" "PyQt6-QScintilla>=2.14"
    echo "✓ Done. Try: ./run.sh demo   (then ./run.sh app for the editor)"
    ;;

  install-core)
    banner "INSTALL CORE — engine only (no GUI, no neural runtime)"
    command -v uv >/dev/null 2>&1 || { echo "✗ uv not found."; exit 1; }
    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python "numpy>=2.1"
    echo "✓ Done. ./run.sh demo / search / eval work; ./run.sh app needs install."
    ;;

  install-model)
    banner "INSTALL MODEL — download the ONNX encoder (~133 MB, once)"
    need_py
    exec "$PY" -m raggy.model_store --download
    ;;

  install-neural)
    banner "INSTALL NEURAL (dev) — torch-backed sentence-transformers"
    echo "  ⚠️  This is the DEVELOPMENT comparison path. The shipping path is ONNX,"
    echo "      which is ~10x smaller. Use it only to compare embeddings."
    need_py
    uv pip install --python "$PY" sentence-transformers
    ;;

  app)
    need_py
    banner "RAGGYEDITOR — the editor"
    exec "$PY" -m raggy.app "$@"
    ;;

  package)
    banner "PACKAGE — freeze RaggyEditor into a standalone executable"
    need_py
    exec bash "$ROOT/scripts/package.sh" "$@"
    ;;

  serve)
    need_py
    banner "RAGGYEDITOR — serving on http://127.0.0.1:${RAGGY_PORT:-8791}"
    exec "$PY" -m raggy.server "$@"
    ;;

  demo)
    need_py
    banner "DEMO — semantic search over a sample document (offline, no key)"
    exec "$PY" -m raggy.cli demo "$@"
    ;;

  search)
    need_py
    banner "SEARCH"
    exec "$PY" -m raggy.cli search "$@"
    ;;

  ask)
    need_py
    banner "ASK"
    exec "$PY" -m raggy.cli ask "$@"
    ;;

  eval)
    need_py
    banner "EVAL — does semantic search beat regex? measured."
    exec "$PY" -m raggy.eval "$@"
    ;;

  bench)
    need_py
    banner "BENCH — what incremental re-indexing saves"
    exec "$PY" -m raggy.bench "$@"
    ;;

  test)
    need_py
    banner "TEST"
    exec "$PY" -m unittest discover -s "$ROOT/tests" -v
    ;;

  -h|--help|help)
    grep '^#' "$ROOT/run.sh" | sed 's/^# \{0,1\}//' | head -15
    ;;

  *)
    echo "unknown command: $cmd"; echo "run: ./run.sh --help"; exit 2
    ;;
esac
