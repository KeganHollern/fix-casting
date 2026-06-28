#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

# --- preflight checks -------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv not found. Install it:  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "warning: ffmpeg not found on PATH. Install it before casting:  brew install ffmpeg" >&2
fi

# --- install the `cast` CLI -------------------------------------------------
# `uv tool install` builds the package in its own isolated env and links the
# `cast` entry point into uv's bin dir (~/.local/bin). --editable keeps it
# pointing at this checkout so code edits take effect without reinstalling.
uv tool install --force --editable "$ROOT"

# --- Playwright fallback browser (Chromium) ---------------------------------
# Run the tool env's own playwright so the downloaded browser matches the
# pinned version. Browsers go to the shared ~/Library/Caches/ms-playwright.
TOOL_PLAYWRIGHT="$(uv tool dir)/fix-casting/bin/playwright"
if [ -x "$TOOL_PLAYWRIGHT" ]; then
  "$TOOL_PLAYWRIGHT" install chromium
else
  echo "warning: could not locate the installed playwright; skipping Chromium download." >&2
fi

# --- AudioTee (per-tab audio capture, macOS) --------------------------------
if command -v swift >/dev/null 2>&1; then
  if [ ! -x "$ROOT/vendor/audiotee/.build/arm64-apple-macosx/release/audiotee" ] \
    && [ ! -x "$ROOT/vendor/audiotee/.build/release/audiotee" ]; then
    echo "Building AudioTee for per-tab audio capture..."
    if [ ! -d "$ROOT/vendor/audiotee" ]; then
      git clone --depth 1 https://github.com/makeusabrew/audiotee.git "$ROOT/vendor/audiotee"
    fi
    (cd "$ROOT/vendor/audiotee" && swift build -c release)
  fi
else
  echo "warning: swift not found — skipping AudioTee build (per-tab audio capture won't work)." >&2
fi

# --- done -------------------------------------------------------------------
BIN_DIR="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
echo
echo "Installed cast -> $BIN_DIR/cast"
case ":$PATH:" in
  *":$BIN_DIR:"*)
    echo "Ready to go:  cast \"https://example.com/watch\""
    ;;
  *)
    echo "$BIN_DIR is NOT on your PATH. Add it with:  uv tool update-shell"
    ;;
esac
