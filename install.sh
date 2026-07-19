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
INSTALL_PROVENANCE_PATH="$INSTALL_DATA_DIR/revision"
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
PROVENANCE_TEMP=""
cleanup() {
  rm -f "$LOCK_CONSTRAINTS"
  if [ -n "$PROVENANCE_TEMP" ]; then
    rm -f "$PROVENANCE_TEMP"
  fi
}
trap cleanup EXIT
uv export \
  --project "$ROOT" \
  --locked \
  --no-dev \
  --no-emit-project \
  --no-hashes \
  --output-file "$LOCK_CONSTRAINTS"

# Once replacement starts, an old receipt can no longer safely describe the
# command on disk. Publish an atomic fail-closed marker first; a failed build or
# verification will therefore never leave `cast --version` claiming a snapshot.
PROVENANCE_TEMP="$(mktemp "$INSTALL_DATA_DIR/revision.XXXXXX")"
printf '%s\n' "revision unknown (installation incomplete)" > "$PROVENANCE_TEMP"
mv -f "$PROVENANCE_TEMP" "$INSTALL_PROVENANCE_PATH"
PROVENANCE_TEMP=""

# --force recreates the tool environment, but does not by itself invalidate a
# wheel cached for this local directory. --reinstall-package implies a scoped
# cache refresh and forces fix-casting itself to be rebuilt from this checkout.
uv tool install \
  --force \
  --reinstall-package fix-casting \
  --constraints "$LOCK_CONSTRAINTS" \
  "$ROOT"

TOOL_ENV_DIR="$(uv tool dir)/fix-casting"
TOOL_PYTHON="$TOOL_ENV_DIR/bin/python"
if [ ! -x "$TOOL_PYTHON" ]; then
  echo "error: installed fix-casting Python was not found at $TOOL_PYTHON" >&2
  exit 1
fi

# Run the helper from the isolated tool environment (`-I` excludes this
# checkout from Python's import path). A stale wheel that predates the helper
# fails here; one with different sources produces a different fingerprint.
SOURCE_PACKAGE_FINGERPRINT="$(
  "$TOOL_PYTHON" -I -c \
    'import sys; from pathlib import Path; from cast_tab.cli import _package_fingerprint; print(_package_fingerprint(Path(sys.argv[1])))' \
    "$ROOT/cast_tab"
)"
INSTALLED_PACKAGE_FINGERPRINT="$(
  "$TOOL_PYTHON" -I -c \
    'from pathlib import Path; import cast_tab.cli as cli; print(cli._package_fingerprint(Path(cli.__file__).resolve().parent))'
)"
for package_fingerprint in \
  "$SOURCE_PACKAGE_FINGERPRINT" \
  "$INSTALLED_PACKAGE_FINGERPRINT"
do
  if [ "${#package_fingerprint}" -ne 64 ]; then
    echo "error: fix-casting package fingerprint was malformed" >&2
    exit 1
  fi
  case "$package_fingerprint" in
    *[!0-9a-f]*)
      echo "error: fix-casting package fingerprint was malformed" >&2
      exit 1
      ;;
  esac
done
if [ "$SOURCE_PACKAGE_FINGERPRINT" != "$INSTALLED_PACKAGE_FINGERPRINT" ]; then
  echo "error: installed fix-casting sources do not match $ROOT/cast_tab" >&2
  echo "       Refusing to publish an installation revision receipt." >&2
  exit 1
fi

# Record exactly which verified source snapshot produced the installed command
# before optional browser/native-helper setup. If one of those later steps
# fails, the already-updated `cast` command still retains an accurate receipt.
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
SOURCE_FINGERPRINT="${SOURCE_PACKAGE_FINGERPRINT:0:16}"
INSTALL_REVISION="$BRANCH@$REVISION$DIRTY+source.$SOURCE_FINGERPRINT"

# The structured receipt lets the installed CLI recompute its own package
# fingerprint before displaying the claimed revision. Build it beside the
# destination and rename it into place so readers never observe a partial file.
PROVENANCE_TEMP="$(mktemp "$INSTALL_DATA_DIR/revision.XXXXXX")"
{
  printf 'format=1\n'
  printf 'revision=%s\n' "$INSTALL_REVISION"
  printf 'package_fingerprint=%s\n' "$INSTALLED_PACKAGE_FINGERPRINT"
} > "$PROVENANCE_TEMP"
mv -f "$PROVENANCE_TEMP" "$INSTALL_PROVENANCE_PATH"
PROVENANCE_TEMP=""

# --- Playwright fallback browser (Chromium) ---------------------------------
# Run the tool env's own playwright so the downloaded browser matches the
# pinned version. Browsers go to the shared ~/Library/Caches/ms-playwright.
TOOL_PLAYWRIGHT="$TOOL_ENV_DIR/bin/playwright"
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
