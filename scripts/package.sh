#!/usr/bin/env bash
# =============================================================================
# package.sh — freeze RaggyEditor into a standalone executable.
#
#   macOS   -> dist/RaggyEditor.app   (double-clickable)
#   Windows -> dist/RaggyEditor/RaggyEditor.exe
#   Linux   -> dist/RaggyEditor/RaggyEditor
#
# The model is NOT bundled: it is downloaded on first use, which is what keeps
# the artifact small. The app works offline before that (LSA fallback).
#
# Usage:
#   ./run.sh package            # build for the current OS
#   ./run.sh package --dmg      # macOS: also produce a .dmg
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "✗ no .venv. Run: ./run.sh install"; exit 1; }

MAKE_DMG=0
for a in "$@"; do [ "$a" = "--dmg" ] && MAKE_DMG=1; done

echo "== PACKAGE — freezing RaggyEditor =="

if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
  echo "  installing PyInstaller…"
  uv pip install --python "$PY" pyinstaller >/dev/null
fi

echo "  cleaning previous build…"
rm -rf "$ROOT/build" "$ROOT/dist"

echo "  running PyInstaller (this takes a few minutes)…"
"$PY" -m PyInstaller --noconfirm --clean \
  --distpath "$ROOT/dist" --workpath "$ROOT/build" \
  "$ROOT/packaging/raggyeditor.spec"

# ---------------------------------------------------------------------------
# Verify the bundle actually RUNS. A build that produces a file is not the same
# as a build that produces a working app; the selftest constructs the window
# and exits, which catches missing Qt plugins and absent compiled modules.
# ---------------------------------------------------------------------------
case "$(uname -s)" in
  Darwin) BIN="$ROOT/dist/RaggyEditor.app/Contents/MacOS/RaggyEditor" ;;
  *)      BIN="$ROOT/dist/RaggyEditor/RaggyEditor" ;;
esac

if [ -x "$BIN" ]; then
  echo "  verifying the bundle runs…"
  if QT_QPA_PLATFORM=offscreen "$BIN" --selftest >/dev/null 2>&1; then
    echo "  ✓ selftest passed"
  else
    echo "  ⚠️ selftest FAILED — the bundle was produced but does not start."
    echo "     Re-run without --clean to read the error, or run it directly:"
    echo "       $BIN"
    exit 1
  fi
else
  echo "  ⚠️ could not find the built binary at $BIN"
  exit 1
fi

SIZE="$(du -sh "$ROOT/dist" | cut -f1)"
echo "  ✓ built: $ROOT/dist  ($SIZE)"

# ---------------------------------------------------------------------------
# macOS: a .dmg is the thing a user actually downloads.
# ---------------------------------------------------------------------------
if [ "$(uname -s)" = "Darwin" ] && [ "$MAKE_DMG" = "1" ]; then
  echo "  creating DMG…"
  DMG="$ROOT/dist/RaggyEditor.dmg"
  rm -f "$DMG"
  hdiutil create -volname "RaggyEditor" -srcfolder "$ROOT/dist/RaggyEditor.app" \
    -ov -format UDZO "$DMG" >/dev/null
  echo "  ✓ $DMG ($(du -sh "$DMG" | cut -f1))"
  echo "  ⚠️ unsigned: macOS Gatekeeper will warn until you codesign + notarise"
  echo "     (see README → Shipping). testers can right-click → Open once."
fi

echo
echo "  Note: the semantic model is downloaded on first use (Menu ▸ Model ▸"
echo "  Download semantic model…). Without it the app still searches, using the"
echo "  offline LSA encoder."
