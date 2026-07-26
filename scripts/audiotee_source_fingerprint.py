#!/usr/bin/env python3
"""Print a deterministic fingerprint of the native AudioTee build inputs."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def source_fingerprint(package_dir: Path) -> str:
    package_dir = package_dir.resolve()
    inputs = [package_dir / "Package.swift"]
    resolved = package_dir / "Package.resolved"
    if resolved.is_file():
        inputs.append(resolved)
    sources = package_dir / "Sources"
    if sources.is_dir():
        inputs.extend(path for path in sources.rglob("*") if path.is_file())
    inputs = sorted(inputs, key=lambda path: path.relative_to(package_dir).as_posix())
    if not inputs or not all(path.is_file() for path in inputs):
        raise ValueError(f"AudioTee build inputs are incomplete under {package_dir}")

    digest = hashlib.sha256()
    for source_file in inputs:
        relative = source_file.relative_to(package_dir).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(hashlib.sha256(source_file.read_bytes()).digest())
    return digest.hexdigest()


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} AUDIO_TEE_PACKAGE", file=sys.stderr)
        return 2
    try:
        print(source_fingerprint(Path(sys.argv[1])))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
