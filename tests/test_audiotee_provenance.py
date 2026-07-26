"""Runtime trust checks for the installed AudioTee generation."""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from pathlib import Path

from cast_tab import audio
from cast_tab.audiotee_provenance import (
    AUDIOTEE_PROTOCOL_VERSION,
    AUDIOTEE_SOURCE_FINGERPRINT,
    verify_installed_audiotee,
)

ROOT = Path(__file__).resolve().parents[1]


def _install_generation(
    root: Path,
    *,
    advertised_protocol: int = AUDIOTEE_PROTOCOL_VERSION,
    receipt_protocol: int = AUDIOTEE_PROTOCOL_VERSION,
    architecture: str | None = None,
) -> Path:
    generation = root / "generations" / "test"
    generation.mkdir(parents=True)
    binary = generation / "audiotee"
    binary.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = --protocol-version ]; then "
        f"echo {advertised_protocol}; exit 0; fi\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    (generation / "receipt").write_text(
        "format=1\n"
        f"source_fingerprint={AUDIOTEE_SOURCE_FINGERPRINT}\n"
        f"binary_sha256={binary_hash}\n"
        f"architecture={architecture or platform.machine()}\n"
        f"protocol_version={receipt_protocol}\n",
        encoding="utf-8",
    )
    installed = root / "bin" / "audiotee"
    installed.parent.mkdir(parents=True)
    installed.symlink_to(binary)
    return installed


def test_verified_installed_generation_is_accepted(tmp_path: Path) -> None:
    installed = _install_generation(tmp_path)

    verified, reason = verify_installed_audiotee(installed)

    assert reason == ""
    assert verified is not None
    assert verified.source_fingerprint == AUDIOTEE_SOURCE_FINGERPRINT
    assert verified.architecture == platform.machine()
    assert verified.protocol_version == AUDIOTEE_PROTOCOL_VERSION


def test_receipt_rejects_wrong_architecture(tmp_path: Path) -> None:
    installed = _install_generation(tmp_path, architecture="definitely-not-this-host")

    verified, reason = verify_installed_audiotee(installed)

    assert verified is None
    assert "architecture" in reason


def test_receipt_rejects_unsupported_protocol(tmp_path: Path) -> None:
    installed = _install_generation(tmp_path, receipt_protocol=1)

    verified, reason = verify_installed_audiotee(installed)

    assert verified is None
    assert "protocol 1" in reason


def test_receipt_rejects_other_protocol_compatible_source(tmp_path: Path) -> None:
    installed = _install_generation(tmp_path)
    receipt = installed.resolve().parent / "receipt"
    receipt.write_text(
        receipt.read_text(encoding="utf-8").replace(
            AUDIOTEE_SOURCE_FINGERPRINT,
            "b" * 64,
        ),
        encoding="utf-8",
    )

    verified, reason = verify_installed_audiotee(installed)

    assert verified is None
    assert "source does not match" in reason


def test_runtime_fingerprint_matches_vendored_native_sources() -> None:
    actual = subprocess.check_output(
        [
            sys.executable,
            str(ROOT / "scripts" / "audiotee_source_fingerprint.py"),
            str(ROOT / "vendor" / "audiotee"),
        ],
        text=True,
    ).strip()

    assert AUDIOTEE_SOURCE_FINGERPRINT == actual


def test_receipt_rejects_binary_replaced_after_install(tmp_path: Path) -> None:
    installed = _install_generation(tmp_path)
    installed.resolve().write_text("#!/bin/sh\necho replaced\n", encoding="utf-8")

    verified, reason = verify_installed_audiotee(installed)

    assert verified is None
    assert "does not match" in reason


def test_receipt_rejects_extra_or_duplicate_fields(tmp_path: Path) -> None:
    installed = _install_generation(tmp_path)
    receipt = installed.resolve().parent / "receipt"
    receipt.write_text(
        receipt.read_text(encoding="utf-8") + "protocol_version=2\nextra=value\n",
        encoding="utf-8",
    )

    verified, reason = verify_installed_audiotee(installed)

    assert verified is None
    assert reason == "malformed generation receipt"


def test_runtime_rejects_helper_whose_live_protocol_disagrees(
    monkeypatch,
    tmp_path: Path,
) -> None:
    installed = _install_generation(tmp_path, advertised_protocol=1)
    monkeypatch.setattr(audio, "AUDIOTEE_INSTALL_PATH", installed)

    assert audio.audiotee_path() is None


def test_runtime_accepts_only_receipt_verified_generation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    installed = _install_generation(tmp_path)
    monkeypatch.setattr(audio, "AUDIOTEE_INSTALL_PATH", installed)
    assert audio.audiotee_path() == installed.resolve()

    (installed.resolve().parent / "receipt").unlink()
    assert audio.audiotee_path() is None


def test_runtime_pins_verified_generation_across_atomic_install_swap(
    monkeypatch,
    tmp_path: Path,
) -> None:
    installed = _install_generation(tmp_path)
    verified_generation = installed.resolve()
    replacement_generation = tmp_path / "generations" / "replacement"
    replacement_generation.mkdir()
    replacement_binary = replacement_generation / "audiotee"
    replacement_binary.write_text(
        "#!/bin/sh\nif [ \"${1:-}\" = --protocol-version ]; then echo 2; fi\n",
        encoding="utf-8",
    )
    replacement_binary.chmod(0o755)
    replacement_hash = hashlib.sha256(replacement_binary.read_bytes()).hexdigest()
    (replacement_generation / "receipt").write_text(
        "format=1\n"
        f"source_fingerprint={'b' * 64}\n"
        f"binary_sha256={replacement_hash}\n"
        f"architecture={platform.machine()}\n"
        "protocol_version=2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(audio, "AUDIOTEE_INSTALL_PATH", installed)

    def swap_stable_symlink(candidate: Path) -> bool:
        assert candidate == verified_generation
        replacement_link = installed.parent / "replacement-link"
        replacement_link.symlink_to(replacement_binary)
        replacement_link.replace(installed)
        return True

    monkeypatch.setattr(audio, "_audiotee_protocol_matches", swap_stable_symlink)

    selected = audio.audiotee_path()

    assert selected == verified_generation
    assert installed.resolve() == replacement_binary
    replacement_verified, reason = verify_installed_audiotee(installed)
    assert replacement_verified is None
    assert "source does not match" in reason
