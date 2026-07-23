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

# Every remaining step mutates shared per-user state. Serialize the complete
# Python + browser + native-helper + final-receipt transaction so concurrent
# installs from different checkouts cannot publish a mixed installation.
INSTALL_LOCK_DIR="$INSTALL_DATA_DIR/install.lock"
INSTALL_LOCK_HELD=0
LOCK_CONSTRAINTS=""
PROVENANCE_TEMP=""
cleanup() {
  if [ -n "$LOCK_CONSTRAINTS" ]; then
    rm -f "$LOCK_CONSTRAINTS"
  fi
  if [ -n "$PROVENANCE_TEMP" ]; then
    rm -f "$PROVENANCE_TEMP"
  fi
  if [ "$INSTALL_LOCK_HELD" -eq 1 ]; then
    lock_owner="$(sed -n '1p' "$INSTALL_LOCK_DIR/owner" 2>/dev/null || true)"
    if [ -z "$lock_owner" ] || [ "$lock_owner" = "$$" ]; then
      rm -f "$INSTALL_LOCK_DIR/owner"
      rmdir "$INSTALL_LOCK_DIR" 2>/dev/null || true
    fi
  fi
}
trap cleanup EXIT

acquire_install_lock() {
  lock_timeout="${CAST_INSTALL_LOCK_TIMEOUT_SECONDS:-600}"
  case "$lock_timeout" in
    ''|*[!0-9]*)
      echo "error: CAST_INSTALL_LOCK_TIMEOUT_SECONDS must be a nonnegative integer" >&2
      return 1
      ;;
  esac
  lock_deadline=$((SECONDS + lock_timeout))
  lock_wait_reported=0
  ownerless_observations=0
  while ! mkdir "$INSTALL_LOCK_DIR" 2>/dev/null; do
    lock_owner="$(sed -n '1p' "$INSTALL_LOCK_DIR/owner" 2>/dev/null || true)"
    case "$lock_owner" in
      ''|*[!0-9]*)
        ownerless_observations=$((ownerless_observations + 1))
        if [ "$ownerless_observations" -ge 20 ]; then
          stale_lock="$INSTALL_LOCK_DIR.stale.$$.$RANDOM"
          if mv "$INSTALL_LOCK_DIR" "$stale_lock" 2>/dev/null; then
            rm -f "$stale_lock/owner"
            rmdir "$stale_lock" 2>/dev/null || true
          fi
          ownerless_observations=0
          continue
        fi
        ;;
      *)
        ownerless_observations=0
        if ! kill -0 "$lock_owner" 2>/dev/null; then
          stale_lock="$INSTALL_LOCK_DIR.stale.$$.$RANDOM"
          if mv "$INSTALL_LOCK_DIR" "$stale_lock" 2>/dev/null; then
            rm -f "$stale_lock/owner"
            rmdir "$stale_lock" 2>/dev/null || true
          fi
          continue
        fi
        ;;
    esac
    if [ "$SECONDS" -ge "$lock_deadline" ]; then
      echo "error: timed out waiting for another fix-casting install (owner PID: ${lock_owner:-unknown})" >&2
      return 1
    fi
    if [ "$lock_wait_reported" -eq 0 ]; then
      echo "Waiting for another fix-casting install to finish..."
      lock_wait_reported=1
    fi
    sleep 0.1
  done
  INSTALL_LOCK_HELD=1
  if ! printf '%s\n' "$$" > "$INSTALL_LOCK_DIR/owner"; then
    rmdir "$INSTALL_LOCK_DIR" 2>/dev/null || true
    INSTALL_LOCK_HELD=0
    return 1
  fi
}

acquire_install_lock

# --- install the `cast` CLI -------------------------------------------------
# `uv tool install` builds a snapshot in its own isolated environment and links
# the `cast` entry point into uv's bin dir (~/.local/bin). Keep it non-editable:
# changing branches or editing this checkout must not silently change the
# installed command. Export exact runtime constraints from the committed lock.
LOCK_CONSTRAINTS="$(mktemp "${TMPDIR:-/tmp}/fix-casting-constraints.XXXXXX")"
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

# Prepare the exact source identity now, but publish it only after browser and
# native-helper setup succeed. Until then the fail-closed marker written above
# truthfully reports that the multi-component installation is incomplete.
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

# --- Playwright fallback browser (Chromium) ---------------------------------
# Run the tool env's own playwright so the downloaded browser matches the
# pinned version. Browsers go to the shared ~/Library/Caches/ms-playwright.
TOOL_PLAYWRIGHT="$TOOL_ENV_DIR/bin/playwright"
if [ -x "$TOOL_PLAYWRIGHT" ]; then
  if ! "$TOOL_PLAYWRIGHT" install chromium; then
    echo "warning: fallback Chromium download failed; Google Chrome remains the primary browser." >&2
  fi
else
  echo "warning: could not locate the installed playwright; skipping Chromium download." >&2
fi

# --- AudioTee (per-tab audio capture, macOS) --------------------------------
# Build in an isolated scratch directory from this checkout's vendored inputs.
# Ignored repo/.build artifacts are never trusted: their existence says
# nothing about which source revision produced them. The helper installer
# publishes a versioned binary+receipt generation and atomically switches one
# symlink only after architecture/protocol/hash validation succeeds.
EXPECTED_AUDIOTEE_SOURCE_FINGERPRINT="$(
  "$TOOL_PYTHON" -I -c \
    'from cast_tab.audiotee_provenance import AUDIOTEE_SOURCE_FINGERPRINT; print(AUDIOTEE_SOURCE_FINGERPRINT)'
)"
bash "$ROOT/scripts/install_audiotee.sh" \
  "$ROOT" \
  "$INSTALL_DATA_DIR" \
  "$TOOL_PYTHON" \
  "$EXPECTED_AUDIOTEE_SOURCE_FINGERPRINT"

# Publish overall success last. The structured receipt lets the installed CLI
# recompute its package fingerprint before displaying the claimed revision.
# Build beside the destination and rename so readers never see a partial file.
PROVENANCE_TEMP="$(mktemp "$INSTALL_DATA_DIR/revision.XXXXXX")"
{
  printf 'format=1\n'
  printf 'revision=%s\n' "$INSTALL_REVISION"
  printf 'package_fingerprint=%s\n' "$INSTALLED_PACKAGE_FINGERPRINT"
} > "$PROVENANCE_TEMP"
mv -f "$PROVENANCE_TEMP" "$INSTALL_PROVENANCE_PATH"
PROVENANCE_TEMP=""

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
