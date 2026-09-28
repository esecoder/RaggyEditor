#!/usr/bin/env bash
# =============================================================================
# RaggyEditor — one entry point.
#
#   ./run.sh install         create .venv and install the ONE dependency
#   ./run.sh serve           start the sidecar + open the editor web UI
#   ./run.sh demo            offline demo: index a sample doc, run real searches
#   ./run.sh search "..."    semantic search from the terminal
#   ./run.sh ask "..."       cited answer from the terminal (needs a key)
#   ./run.sh eval            measure semantic vs regex on ground-truth queries
#   ./run.sh test            run the test suite
#   ./run.sh install-neural  opt in to the real local encoder (bge-small)
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
    banner "INSTALL — numpy is the only required dependency"
    command -v uv >/dev/null 2>&1 || { echo "✗ uv not found. Install: https://docs.astral.sh/uv/"; exit 1; }
    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python numpy
    echo "✓ Done. Try: ./run.sh demo"
    ;;

  install-neural)
    banner "INSTALL NEURAL — the real semantic encoder (one-time ~130MB download)"
    need_py
    uv pip install --python "$PY" sentence-transformers
    echo "✓ Done. RaggyEditor will now use BAAI/bge-small-en-v1.5 automatically."
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
