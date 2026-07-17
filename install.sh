#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
case "$DATA_HOME" in
  /*) ;;
  *)
    echo "error: XDG_DATA_HOME must be an absolute path (got: $DATA_HOME)" >&2
    exit 1
    ;;
esac
INSTALL_DATA_DIR="$DATA_HOME/fix-casting"
AUDIOTEE_DEST="$INSTALL_DATA_DIR/bin/audiotee"
mkdir -p "$INSTALL_DATA_DIR/bin"

# --- preflight checks -------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv not found. Install it:  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "warning: ffmpeg not found on PATH. Install it before casting:  brew install ffmpeg" >&2
fi

# --- install the `cast` CLI -------------------------------------------------
# `uv tool install` builds a snapshot in its own isolated environment and links
# the `cast` entry point into uv's bin dir (~/.local/bin). Keep it non-editable:
# changing branches or editing this checkout must not silently change the
# installed command. Export exact runtime constraints from the committed lock.
LOCK_CONSTRAINTS="$(mktemp "${TMPDIR:-/tmp}/fix-casting-constraints.XXXXXX")"
cleanup() {
  rm -f "$LOCK_CONSTRAINTS"
}
trap cleanup EXIT
uv export \
  --project "$ROOT" \
  --locked \
  --no-dev \
  --no-emit-project \
  --no-hashes \
  --output-file "$LOCK_CONSTRAINTS"
uv tool install --force --constraints "$LOCK_CONSTRAINTS" "$ROOT"

# Record exactly which source snapshot produced the installed command before
# optional browser/native-helper setup. If one of those later steps fails, the
# already-updated `cast` command must not retain a stale receipt.
REVISION="unknown"
BRANCH="unknown"
DIRTY=""
if REVISION="$(git -C "$ROOT" rev-parse --verify HEAD 2>/dev/null)"; then
  BRANCH="$(git -C "$ROOT" branch --show-current 2>/dev/null || true)"
  [ -n "$BRANCH" ] || BRANCH="detached"
  if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=normal 2>/dev/null)" ]; then
    DIRTY="-dirty"
  fi
fi
SOURCE_FINGERPRINT="$(
  {
    find "$ROOT/cast_tab" -type f -name '*.py' -print
    printf '%s\n' "$ROOT/pyproject.toml" "$ROOT/uv.lock"
  } | LC_ALL=C sort | while IFS= read -r source_file; do
    shasum -a 256 "$source_file" | awk '{print $1}'
  done | shasum -a 256 | awk '{print $1}'
)"
SOURCE_FINGERPRINT="${SOURCE_FINGERPRINT:0:16}"
INSTALL_REVISION="$BRANCH@$REVISION$DIRTY+source.$SOURCE_FINGERPRINT"
printf '%s\n' "$INSTALL_REVISION" > "$INSTALL_DATA_DIR/revision"

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
# Resolution order: existing repo build → prebuilt download (no Swift needed)
# → swift build from vendor/. Copy the result outside the checkout so the
# non-editable tool can find it after the source tree moves or changes branches.
# The prebuilt is published by the
# release-audiotee.yml workflow when an `audiotee-v*` tag is pushed.
AUDIOTEE_RELEASE_URL="${AUDIOTEE_RELEASE_URL:-https://github.com/KeganHollern/fix-casting/releases/latest/download/audiotee-macos-$(uname -m)}"
AUDIOTEE_CHECKSUM_URL="${AUDIOTEE_CHECKSUM_URL:-${AUDIOTEE_RELEASE_URL%/*}/checksums.txt}"
AUDIOTEE_SHA256="${AUDIOTEE_SHA256:-}"

have_audiotee() {
  [ -x "$AUDIOTEE_DEST" ] && file "$AUDIOTEE_DEST" | grep -q "Mach-O"
}

repo_audiotee() {
  for candidate in \
    "$ROOT/bin/audiotee" \
    "$ROOT/vendor/audiotee/.build/arm64-apple-macosx/release/audiotee" \
    "$ROOT/vendor/audiotee/.build/release/audiotee"
  do
    if [ -x "$candidate" ] && file "$candidate" | grep -q "Mach-O"; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

record_audiotee_hash() {
  shasum -a 256 "$AUDIOTEE_DEST" | awk '{print $1}' \
    > "$INSTALL_DATA_DIR/audiotee.sha256"
}

if EXISTING_AUDIOTEE="$(repo_audiotee)"; then
  install -m 755 "$EXISTING_AUDIOTEE" "$AUDIOTEE_DEST"
  record_audiotee_hash
  echo "Installed existing AudioTee -> $AUDIOTEE_DEST"
else
  echo "Fetching prebuilt AudioTee..."
  AUDIOTEE_TEMP="$INSTALL_DATA_DIR/bin/audiotee.tmp"
  AUDIOTEE_CHECKSUM_TEMP="$INSTALL_DATA_DIR/bin/checksums.tmp"
  EXPECTED_AUDIOTEE_SHA256="$AUDIOTEE_SHA256"
  if curl -fsSL --retry 2 -o "$AUDIOTEE_TEMP" "$AUDIOTEE_RELEASE_URL" 2>/dev/null; then
    if [ -z "$EXPECTED_AUDIOTEE_SHA256" ] \
      && curl -fsSL --retry 2 -o "$AUDIOTEE_CHECKSUM_TEMP" "$AUDIOTEE_CHECKSUM_URL" 2>/dev/null; then
      AUDIOTEE_ASSET_NAME="$(basename "$AUDIOTEE_RELEASE_URL")"
      EXPECTED_AUDIOTEE_SHA256="$(
        awk -v asset="$AUDIOTEE_ASSET_NAME" '$2 == asset {print $1; exit}' \
          "$AUDIOTEE_CHECKSUM_TEMP"
      )"
    fi
    ACTUAL_AUDIOTEE_SHA256="$(shasum -a 256 "$AUDIOTEE_TEMP" | awk '{print $1}')"
  else
    ACTUAL_AUDIOTEE_SHA256=""
  fi
  if [ "${#EXPECTED_AUDIOTEE_SHA256}" -eq 64 ] \
    && [ "$ACTUAL_AUDIOTEE_SHA256" = "$EXPECTED_AUDIOTEE_SHA256" ] \
    && file "$AUDIOTEE_TEMP" | grep -q "Mach-O"; then
    mv "$AUDIOTEE_TEMP" "$AUDIOTEE_DEST"
    chmod +x "$AUDIOTEE_DEST"
    record_audiotee_hash
    echo "Installed prebuilt AudioTee -> $AUDIOTEE_DEST"
  else
    rm -f "$AUDIOTEE_TEMP"
    if have_audiotee; then
      echo "Could not verify refreshed AudioTee; keeping the installed binary."
      record_audiotee_hash
    else
      echo "No verified prebuilt AudioTee available; falling back to a source build."
    fi
  fi
  rm -f "$AUDIOTEE_CHECKSUM_TEMP"
fi

if ! have_audiotee; then
  if command -v swift >/dev/null 2>&1; then
    echo "Building AudioTee for per-tab audio capture..."
    if [ ! -d "$ROOT/vendor/audiotee" ]; then
      git clone --depth 1 https://github.com/makeusabrew/audiotee.git "$ROOT/vendor/audiotee"
    fi
    (cd "$ROOT/vendor/audiotee" && swift build -c release)
    if BUILT_AUDIOTEE="$(repo_audiotee)"; then
      install -m 755 "$BUILT_AUDIOTEE" "$AUDIOTEE_DEST"
      record_audiotee_hash
      echo "Installed built AudioTee -> $AUDIOTEE_DEST"
    else
      echo "warning: AudioTee build completed but its binary was not found." >&2
    fi
  else
    echo "warning: no prebuilt AudioTee and swift not found — per-tab audio capture won't work." >&2
    echo "         Install Xcode command line tools (xcode-select --install) and re-run." >&2
  fi
fi

# --- done -------------------------------------------------------------------
BIN_DIR="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
echo
echo "Installed cast -> $BIN_DIR/cast"
echo "Installed snapshot: $INSTALL_REVISION (check with: cast --version)"
case ":$PATH:" in
  *":$BIN_DIR:"*)
    echo "Ready to go:  cast \"https://example.com/watch\""
    ;;
  *)
    echo "$BIN_DIR is NOT on your PATH. Add it with:  uv tool update-shell"
    ;;
esac
