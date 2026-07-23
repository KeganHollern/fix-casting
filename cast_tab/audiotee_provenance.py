"""Verification for the installed native AudioTee generation."""

from __future__ import annotations

import hashlib
import hmac
import platform
from dataclasses import dataclass
from pathlib import Path

AUDIOTEE_PROTOCOL_VERSION = 2
AUDIOTEE_RECEIPT_FORMAT = "1"
# Generated from Package.swift, Package.resolved (when present), and Sources/**
# by scripts/audiotee_source_fingerprint.py. A regression test keeps this
# runtime contract synchronized with the vendored native inputs.
AUDIOTEE_SOURCE_FINGERPRINT = (
    "aef39ca0c9495b66495a7330b538e445671ea8141e0b8f98ec32895b4a928476"
)
_RECEIPT_FIELDS = {
    "format",
    "source_fingerprint",
    "binary_sha256",
    "architecture",
    "protocol_version",
}


@dataclass(frozen=True)
class VerifiedAudioTee:
    source_fingerprint: str
    binary_sha256: str
    architecture: str
    protocol_version: int


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def verify_installed_audiotee(
    binary_path: Path,
    *,
    architecture: str | None = None,
) -> tuple[VerifiedAudioTee | None, str]:
    """Verify the generation receipt adjacent to the resolved binary."""
    try:
        resolved = binary_path.resolve(strict=True)
        lines = (resolved.parent / "receipt").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None, "missing or unreadable generation receipt"

    fields: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or not key or key in fields:
            return None, "malformed generation receipt"
        fields[key] = value
    if set(fields) != _RECEIPT_FIELDS or fields.get("format") != AUDIOTEE_RECEIPT_FORMAT:
        return None, "malformed generation receipt"
    source_fingerprint = fields["source_fingerprint"]
    expected_binary_hash = fields["binary_sha256"]
    if not _is_sha256(source_fingerprint) or not _is_sha256(expected_binary_hash):
        return None, "malformed generation receipt"
    if source_fingerprint != AUDIOTEE_SOURCE_FINGERPRINT:
        return None, "helper source does not match this cast build"
    expected_architecture = architecture or platform.machine()
    if fields["architecture"] != expected_architecture:
        return None, f"helper architecture does not match {expected_architecture}"
    try:
        protocol_version = int(fields["protocol_version"])
    except ValueError:
        return None, "malformed generation receipt"
    if protocol_version != AUDIOTEE_PROTOCOL_VERSION:
        return None, f"helper protocol {protocol_version} is unsupported"
    try:
        actual_binary_hash = _file_sha256(resolved)
    except OSError:
        return None, "could not hash installed helper"
    if not hmac.compare_digest(actual_binary_hash, expected_binary_hash):
        return None, "installed helper does not match its receipt"
    return (
        VerifiedAudioTee(
            source_fingerprint=source_fingerprint,
            binary_sha256=actual_binary_hash,
            architecture=expected_architecture,
            protocol_version=protocol_version,
        ),
        "",
    )
