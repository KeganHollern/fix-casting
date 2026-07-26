#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:?usage: install_audiotee.sh ROOT INSTALL_DATA_DIR PYTHON}"
INSTALL_DATA_DIR="${2:?usage: install_audiotee.sh ROOT INSTALL_DATA_DIR PYTHON}"
PYTHON="${3:?usage: install_audiotee.sh ROOT INSTALL_DATA_DIR PYTHON}"
EXPECTED_SOURCE_FINGERPRINT="${4:-}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIVE_PACKAGE_DIR="$ROOT/vendor/audiotee"
DEST="$INSTALL_DATA_DIR/bin/audiotee"
PROTOCOL_VERSION=2
ARCHITECTURE="$(uname -m)"

if [ ! -f "$LIVE_PACKAGE_DIR/Package.swift" ] \
  || [ ! -d "$LIVE_PACKAGE_DIR/Sources" ]; then
  echo "error: vendored AudioTee sources are missing from $LIVE_PACKAGE_DIR" >&2
  exit 1
fi

mkdir -p "$INSTALL_DATA_DIR/bin" "$INSTALL_DATA_DIR/audiotee-generations"
STAGE="$(mktemp -d "$INSTALL_DATA_DIR/.audiotee-install.XXXXXX")"
cleanup() {
  case "$STAGE" in
    "$INSTALL_DATA_DIR"/.audiotee-install.*) rm -rf -- "$STAGE" ;;
  esac
}
trap cleanup EXIT

LIVE_FINGERPRINT_BEFORE="$(
  "$PYTHON" "$SCRIPT_DIR/audiotee_source_fingerprint.py" "$LIVE_PACKAGE_DIR"
)"
PACKAGE_DIR="$STAGE/audiotee-source"
mkdir -p "$PACKAGE_DIR"
cp "$LIVE_PACKAGE_DIR/Package.swift" "$PACKAGE_DIR/Package.swift"
if [ -f "$LIVE_PACKAGE_DIR/Package.resolved" ]; then
  cp "$LIVE_PACKAGE_DIR/Package.resolved" "$PACKAGE_DIR/Package.resolved"
fi
cp -R "$LIVE_PACKAGE_DIR/Sources" "$PACKAGE_DIR/Sources"
if [ -d "$LIVE_PACKAGE_DIR/Tests" ]; then
  # SwiftPM validates custom test-target paths even for `swift build`, so the
  # immutable package snapshot must retain the manifest's test tree too.
  cp -R "$LIVE_PACKAGE_DIR/Tests" "$PACKAGE_DIR/Tests"
fi
SOURCE_FINGERPRINT="$(
  "$PYTHON" "$SCRIPT_DIR/audiotee_source_fingerprint.py" "$PACKAGE_DIR"
)"
LIVE_FINGERPRINT_AFTER_COPY="$(
  "$PYTHON" "$SCRIPT_DIR/audiotee_source_fingerprint.py" "$LIVE_PACKAGE_DIR"
)"
case "$SOURCE_FINGERPRINT" in
  ''|*[!0-9a-f]*)
    echo "error: AudioTee source fingerprint was malformed" >&2
    exit 1
    ;;
  *) ;;
esac
if [ "${#SOURCE_FINGERPRINT}" -ne 64 ]; then
  echo "error: AudioTee source fingerprint was malformed" >&2
  exit 1
fi
if [ "$LIVE_FINGERPRINT_BEFORE" != "$SOURCE_FINGERPRINT" ] \
  || [ "$LIVE_FINGERPRINT_AFTER_COPY" != "$SOURCE_FINGERPRINT" ]; then
  echo "error: vendored AudioTee sources changed while creating the build snapshot" >&2
  exit 1
fi
if [ -n "$EXPECTED_SOURCE_FINGERPRINT" ] \
  && [ "$EXPECTED_SOURCE_FINGERPRINT" != "$SOURCE_FINGERPRINT" ]; then
  echo "error: vendored AudioTee sources do not match the installed cast runtime" >&2
  exit 1
fi

candidate_is_compatible() {
  candidate_path="$1"
  [ -n "$candidate_path" ] && [ -x "$candidate_path" ] || return 1
  # `file` normally prefixes its output with the pathname. Swift's build path
  # itself contains the host architecture, which could otherwise make an
  # opposite-architecture binary look compatible.
  candidate_description="$(file -b "$candidate_path" 2>/dev/null)" || return 1
  [[ "$candidate_description" == *"Mach-O"* ]] || return 1
  [[ "$candidate_description" == *"$ARCHITECTURE"* ]] || return 1
  candidate_protocol="$("$candidate_path" --protocol-version 2>/dev/null)" || return 1
  [ "$candidate_protocol" = "$PROTOCOL_VERSION" ]
}

CANDIDATE=""
if [ "${AUDIOTEE_DISABLE_SOURCE_BUILD:-0}" != "1" ] \
  && command -v swift >/dev/null 2>&1; then
  echo "Building AudioTee from source fingerprint ${SOURCE_FINGERPRINT:0:16}..."
  if swift build \
    --package-path "$PACKAGE_DIR" \
    --scratch-path "$STAGE/swift-build" \
    -c release; then
    while IFS= read -r found; do
      CANDIDATE="$found"
      break
    done < <(find "$STAGE/swift-build" -type f -name audiotee -path '*release*' | LC_ALL=C sort)
  fi
  if ! candidate_is_compatible "$CANDIDATE"; then
    echo "warning: local Swift could not produce a compatible AudioTee; trying prebuilt." >&2
    CANDIDATE=""
  fi
fi

if [ -z "$CANDIDATE" ]; then
  RELEASE_BASE="${AUDIOTEE_RELEASE_BASE_URL:-https://github.com/KeganHollern/fix-casting/releases/latest/download}"
  RELEASE_URL="${AUDIOTEE_RELEASE_URL:-$RELEASE_BASE/audiotee-macos-$ARCHITECTURE}"
  MANIFEST_URL="${AUDIOTEE_MANIFEST_URL:-$RELEASE_BASE/audiotee-manifest-$ARCHITECTURE.txt}"
  MANIFEST="$STAGE/release-manifest"
  CANDIDATE="$STAGE/prebuilt-audiotee"
  echo "Fetching source-matched AudioTee prebuilt..."
  if ! curl -fsSL --retry 2 -o "$MANIFEST" "$MANIFEST_URL" \
    || ! curl -fsSL --retry 2 -o "$CANDIDATE" "$RELEASE_URL"; then
    echo "error: no source-matched AudioTee prebuilt could be downloaded" >&2
    exit 1
  fi

  manifest_field() {
    awk -F= -v wanted="$1" '$1 == wanted {value = substr($0, index($0, "=") + 1); count++} END {if (count == 1) print value}' "$MANIFEST"
  }
  MANIFEST_FORMAT="$(manifest_field format)"
  MANIFEST_SOURCE="$(manifest_field source_fingerprint)"
  MANIFEST_SHA256="$(manifest_field binary_sha256)"
  MANIFEST_ARCHITECTURE="$(manifest_field architecture)"
  MANIFEST_PROTOCOL="$(manifest_field protocol_version)"
  if [ "$MANIFEST_FORMAT" != "1" ] \
    || [ "$MANIFEST_SOURCE" != "$SOURCE_FINGERPRINT" ] \
    || [ "$MANIFEST_ARCHITECTURE" != "$ARCHITECTURE" ] \
    || [ "$MANIFEST_PROTOCOL" != "$PROTOCOL_VERSION" ] \
    || [ "${#MANIFEST_SHA256}" -ne 64 ]; then
    echo "error: AudioTee prebuilt manifest does not match this checkout" >&2
    exit 1
  fi
  case "$MANIFEST_SHA256" in
    ''|*[!0-9a-f]*)
      echo "error: AudioTee prebuilt manifest contains a malformed hash" >&2
      exit 1
      ;;
  esac
  ACTUAL_DOWNLOAD_SHA256="$(shasum -a 256 "$CANDIDATE" | awk '{print $1}')"
  if [ "$ACTUAL_DOWNLOAD_SHA256" != "$MANIFEST_SHA256" ]; then
    echo "error: AudioTee prebuilt hash does not match its source manifest" >&2
    exit 1
  fi
  chmod 755 "$CANDIDATE"
fi

if ! candidate_is_compatible "$CANDIDATE"; then
  echo "error: AudioTee installation did not produce a compatible Mach-O executable for $ARCHITECTURE and protocol $PROTOCOL_VERSION" >&2
  exit 1
fi

FINAL_LIVE_SOURCE_FINGERPRINT="$(
  "$PYTHON" "$SCRIPT_DIR/audiotee_source_fingerprint.py" "$LIVE_PACKAGE_DIR"
)"
if [ "$FINAL_LIVE_SOURCE_FINGERPRINT" != "$SOURCE_FINGERPRINT" ]; then
  echo "error: vendored AudioTee sources changed during the build; refusing publication" >&2
  exit 1
fi

PAYLOAD="$STAGE/payload"
mkdir -p "$PAYLOAD"
install -m 755 "$CANDIDATE" "$PAYLOAD/audiotee"
BINARY_SHA256="$(shasum -a 256 "$PAYLOAD/audiotee" | awk '{print $1}')"
{
  printf 'format=1\n'
  printf 'source_fingerprint=%s\n' "$SOURCE_FINGERPRINT"
  printf 'binary_sha256=%s\n' "$BINARY_SHA256"
  printf 'architecture=%s\n' "$ARCHITECTURE"
  printf 'protocol_version=%s\n' "$PROTOCOL_VERSION"
} > "$PAYLOAD/receipt"

GENERATION="source.${SOURCE_FINGERPRINT:0:16}-binary.${BINARY_SHA256:0:16}-$(basename "$STAGE")"
mv "$PAYLOAD" "$INSTALL_DATA_DIR/audiotee-generations/$GENERATION"
ln -s "../audiotee-generations/$GENERATION/audiotee" "$STAGE/audiotee-link"
mv -f "$STAGE/audiotee-link" "$DEST"

echo "Installed source-matched AudioTee -> $DEST"
echo "AudioTee source fingerprint: $SOURCE_FINGERPRINT"
