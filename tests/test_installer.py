"""AudioTee source identity and isolated native-installer tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _fingerprint(package: Path) -> str:
    return subprocess.check_output(
        [
            sys.executable,
            str(ROOT / "scripts" / "audiotee_source_fingerprint.py"),
            str(package),
        ],
        text=True,
    ).strip()


def test_audiotee_source_fingerprint_changes_with_native_input(tmp_path: Path):
    package = tmp_path / "audiotee"
    (package / "Sources" / "Core").mkdir(parents=True)
    (package / "Package.swift").write_text("// package\n", encoding="utf-8")
    source = package / "Sources" / "Core" / "Writer.swift"
    source.write_text("let version = 1\n", encoding="utf-8")

    before = _fingerprint(package)
    source.write_text("let version = 2\n", encoding="utf-8")

    assert _fingerprint(package) != before


def test_installer_ignores_stale_repo_binary_and_builds_current_source(tmp_path: Path):
    fake_root = tmp_path / "checkout"
    package = fake_root / "vendor" / "audiotee"
    (package / "Sources" / "AudioTeeCLI").mkdir(parents=True)
    (package / "Package.swift").write_text("// current package\n", encoding="utf-8")
    (package / "Sources" / "AudioTeeCLI" / "main.swift").write_text(
        "// current source\n",
        encoding="utf-8",
    )
    (package / "Tests" / "AudioTeeCoreTests").mkdir(parents=True)
    (package / "Tests" / "AudioTeeCoreTests" / "Smoke.swift").write_text(
        "// manifest-required test target\n",
        encoding="utf-8",
    )
    (fake_root / "bin").mkdir()
    _executable(fake_root / "bin" / "audiotee", "#!/bin/sh\necho stale\n")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    current_helper = tmp_path / "current-audiotee"
    _executable(
        current_helper,
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = --protocol-version ]; then echo 2; exit 0; fi\n"
        "echo current\n",
    )
    _executable(
        fake_bin / "swift",
        "#!/bin/sh\n"
        "package=\n"
        "scratch=\n"
        "while [ $# -gt 0 ]; do\n"
        "  case $1 in\n"
        "    --package-path) package=$2; shift 2 ;;\n"
        "    --scratch-path) scratch=$2; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "test -f \"$package/Tests/AudioTeeCoreTests/Smoke.swift\"\n"
        "mkdir -p \"$scratch/release\"\n"
        "cp \"$CURRENT_HELPER\" \"$scratch/release/audiotee\"\n"
        "chmod 755 \"$scratch/release/audiotee\"\n",
    )
    architecture = subprocess.check_output(["uname", "-m"], text=True).strip()
    _executable(
        fake_bin / "file",
        f"#!/bin/sh\necho \"$1: Mach-O 64-bit executable {architecture}\"\n",
    )

    # The candidate lives beneath this path; keep a space here to catch
    # accidental word-splitting when the installer executes the helper.
    data_dir = tmp_path / "install data"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    env["CURRENT_HELPER"] = str(current_helper)
    subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "install_audiotee.sh"),
            str(fake_root),
            str(data_dir),
            sys.executable,
        ],
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    installed = data_dir / "bin" / "audiotee"
    assert installed.is_symlink()
    assert installed.resolve().read_bytes() == current_helper.read_bytes()
    assert installed.resolve().read_bytes() != (fake_root / "bin" / "audiotee").read_bytes()
    receipt = (installed.resolve().parent / "receipt").read_text(encoding="utf-8")
    assert f"source_fingerprint={_fingerprint(package)}" in receipt


def test_mismatched_prebuilt_manifest_fails_closed_and_preserves_install(
    tmp_path: Path,
):
    fake_root = tmp_path / "checkout"
    package = fake_root / "vendor" / "audiotee"
    (package / "Sources").mkdir(parents=True)
    (package / "Package.swift").write_text("// current package\n", encoding="utf-8")
    (package / "Sources" / "Current.swift").write_text(
        "// current source\n",
        encoding="utf-8",
    )

    data_dir = tmp_path / "install-data"
    old_generation = data_dir / "audiotee-generations" / "old"
    old_generation.mkdir(parents=True)
    old_helper = old_generation / "audiotee"
    _executable(old_helper, "#!/bin/sh\necho old-valid-helper\n")
    (data_dir / "bin").mkdir()
    (data_dir / "bin" / "audiotee").symlink_to(
        Path("../audiotee-generations/old/audiotee")
    )

    release = tmp_path / "release"
    release.mkdir()
    prebuilt = release / "audiotee"
    _executable(
        prebuilt,
        "#!/bin/sh\nif [ \"${1:-}\" = --protocol-version ]; then echo 2; fi\n",
    )
    binary_hash = subprocess.check_output(
        ["shasum", "-a", "256", str(prebuilt)],
        text=True,
    ).split()[0]
    manifest = release / "manifest.txt"
    manifest.write_text(
        "format=1\n"
        f"source_fingerprint={'0' * 64}\n"
        f"binary_sha256={binary_hash}\n"
        f"architecture={subprocess.check_output(['uname', '-m'], text=True).strip()}\n"
        "protocol_version=2\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["AUDIOTEE_DISABLE_SOURCE_BUILD"] = "1"
    env["AUDIOTEE_RELEASE_URL"] = prebuilt.as_uri()
    env["AUDIOTEE_MANIFEST_URL"] = manifest.as_uri()
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "install_audiotee.sh"),
            str(fake_root),
            str(data_dir),
            sys.executable,
        ],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "does not match this checkout" in result.stderr
    assert (data_dir / "bin" / "audiotee").resolve() == old_helper
    assert (data_dir / "bin" / "audiotee").read_text() == "#!/bin/sh\necho old-valid-helper\n"


def test_matching_prebuilt_manifest_installs_without_swift(tmp_path: Path):
    fake_root = tmp_path / "checkout"
    package = fake_root / "vendor" / "audiotee"
    (package / "Sources").mkdir(parents=True)
    (package / "Package.swift").write_text("// release package\n", encoding="utf-8")
    (package / "Sources" / "Current.swift").write_text(
        "// released source\n",
        encoding="utf-8",
    )
    architecture = subprocess.check_output(["uname", "-m"], text=True).strip()
    release = tmp_path / "release"
    release.mkdir()
    prebuilt = release / "audiotee"
    _executable(
        prebuilt,
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = --protocol-version ]; then echo 2; exit 0; fi\n",
    )
    binary_hash = subprocess.check_output(
        ["shasum", "-a", "256", str(prebuilt)],
        text=True,
    ).split()[0]
    manifest = release / "manifest.txt"
    manifest.write_text(
        "format=1\n"
        f"source_fingerprint={_fingerprint(package)}\n"
        f"binary_sha256={binary_hash}\n"
        f"architecture={architecture}\n"
        "protocol_version=2\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _executable(
        fake_bin / "file",
        f"#!/bin/sh\necho \"$1: Mach-O 64-bit executable {architecture}\"\n",
    )

    data_dir = tmp_path / "install-data"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    env["AUDIOTEE_DISABLE_SOURCE_BUILD"] = "1"
    env["AUDIOTEE_RELEASE_URL"] = prebuilt.as_uri()
    env["AUDIOTEE_MANIFEST_URL"] = manifest.as_uri()
    subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "install_audiotee.sh"),
            str(fake_root),
            str(data_dir),
            sys.executable,
        ],
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    installed = data_dir / "bin" / "audiotee"
    assert installed.is_symlink()
    assert installed.resolve().read_bytes() == prebuilt.read_bytes()
    assert f"binary_sha256={binary_hash}" in (
        installed.resolve().parent / "receipt"
    ).read_text(encoding="utf-8")


def test_broken_swift_falls_back_to_matching_prebuilt(tmp_path: Path):
    fake_root = tmp_path / "checkout"
    package = fake_root / "vendor" / "audiotee"
    (package / "Sources").mkdir(parents=True)
    (package / "Package.swift").write_text("// release package\n", encoding="utf-8")
    (package / "Sources" / "Current.swift").write_text(
        "// released source\n",
        encoding="utf-8",
    )
    architecture = subprocess.check_output(["uname", "-m"], text=True).strip()
    release = tmp_path / "release"
    release.mkdir()
    prebuilt = release / "audiotee"
    _executable(
        prebuilt,
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = --protocol-version ]; then echo 2; exit 0; fi\n",
    )
    binary_hash = subprocess.check_output(
        ["shasum", "-a", "256", str(prebuilt)],
        text=True,
    ).split()[0]
    manifest = release / "manifest.txt"
    manifest.write_text(
        "format=1\n"
        f"source_fingerprint={_fingerprint(package)}\n"
        f"binary_sha256={binary_hash}\n"
        f"architecture={architecture}\n"
        "protocol_version=2\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _executable(fake_bin / "swift", "#!/bin/sh\nexit 1\n")
    _executable(
        fake_bin / "file",
        f"#!/bin/sh\necho \"$1: Mach-O 64-bit executable {architecture}\"\n",
    )

    data_dir = tmp_path / "install-data"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    env["AUDIOTEE_RELEASE_URL"] = prebuilt.as_uri()
    env["AUDIOTEE_MANIFEST_URL"] = manifest.as_uri()
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "install_audiotee.sh"),
            str(fake_root),
            str(data_dir),
            sys.executable,
        ],
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    installed = data_dir / "bin" / "audiotee"
    assert installed.resolve().read_bytes() == prebuilt.read_bytes()
    assert "local Swift could not produce" in result.stderr
    assert "Fetching source-matched AudioTee prebuilt" in result.stdout


def test_native_source_mutation_during_build_aborts_before_publish(tmp_path: Path):
    fake_root = tmp_path / "checkout"
    package = fake_root / "vendor" / "audiotee"
    (package / "Sources").mkdir(parents=True)
    (package / "Package.swift").write_text("// package\n", encoding="utf-8")
    source = package / "Sources" / "Current.swift"
    source.write_text("// original source\n", encoding="utf-8")

    data_dir = tmp_path / "install-data"
    old_generation = data_dir / "audiotee-generations" / "old"
    old_generation.mkdir(parents=True)
    old_helper = old_generation / "audiotee"
    _executable(old_helper, "#!/bin/sh\necho old-helper\n")
    (data_dir / "bin").mkdir()
    installed = data_dir / "bin" / "audiotee"
    installed.symlink_to(Path("../audiotee-generations/old/audiotee"))

    architecture = subprocess.check_output(["uname", "-m"], text=True).strip()
    current_helper = tmp_path / "current-audiotee"
    _executable(
        current_helper,
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = --protocol-version ]; then echo 2; exit 0; fi\n",
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _executable(
        fake_bin / "swift",
        "#!/bin/sh\n"
        "package=\n"
        "scratch=\n"
        "while [ $# -gt 0 ]; do\n"
        "  case $1 in\n"
        "    --package-path) package=$2; shift 2 ;;\n"
        "    --scratch-path) scratch=$2; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "grep -q 'original source' \"$package/Sources/Current.swift\"\n"
        "printf '%s\\n' '// changed during build' > \"$LIVE_SOURCE\"\n"
        "mkdir -p \"$scratch/release\"\n"
        "cp \"$CURRENT_HELPER\" \"$scratch/release/audiotee\"\n"
        "chmod 755 \"$scratch/release/audiotee\"\n",
    )
    _executable(
        fake_bin / "file",
        f"#!/bin/sh\necho \"$1: Mach-O 64-bit executable {architecture}\"\n",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    env["CURRENT_HELPER"] = str(current_helper)
    env["LIVE_SOURCE"] = str(source)

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "install_audiotee.sh"),
            str(fake_root),
            str(data_dir),
            sys.executable,
        ],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "sources changed during the build" in result.stderr
    assert installed.resolve() == old_helper
    assert list((data_dir / "audiotee-generations").iterdir()) == [old_generation]


def test_candidate_path_cannot_spoof_binary_architecture(tmp_path: Path):
    fake_root = tmp_path / "checkout"
    package = fake_root / "vendor" / "audiotee"
    (package / "Sources").mkdir(parents=True)
    (package / "Package.swift").write_text("// package\n", encoding="utf-8")
    (package / "Sources" / "Current.swift").write_text("// source\n", encoding="utf-8")
    architecture = subprocess.check_output(["uname", "-m"], text=True).strip()
    wrong_architecture = "x86_64" if architecture == "arm64" else "arm64"

    data_dir = tmp_path / "install-data"
    old_generation = data_dir / "audiotee-generations" / "old"
    old_generation.mkdir(parents=True)
    old_helper = old_generation / "audiotee"
    _executable(old_helper, "#!/bin/sh\necho old-helper\n")
    (data_dir / "bin").mkdir()
    installed = data_dir / "bin" / "audiotee"
    installed.symlink_to(Path("../audiotee-generations/old/audiotee"))

    current_helper = tmp_path / "current-audiotee"
    _executable(
        current_helper,
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = --protocol-version ]; then echo 2; exit 0; fi\n",
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _executable(
        fake_bin / "swift",
        "#!/bin/sh\n"
        "scratch=\n"
        "while [ $# -gt 0 ]; do\n"
        "  if [ \"$1\" = --scratch-path ]; then scratch=$2; shift 2; else shift; fi\n"
        "done\n"
        f"mkdir -p \"$scratch/{architecture}-apple-macosx/release\"\n"
        f"cp \"$CURRENT_HELPER\" \"$scratch/{architecture}-apple-macosx/release/audiotee\"\n"
        f"chmod 755 \"$scratch/{architecture}-apple-macosx/release/audiotee\"\n",
    )
    _executable(
        fake_bin / "file",
        f"#!/bin/sh\necho 'Mach-O 64-bit executable {wrong_architecture}'\n",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    env["CURRENT_HELPER"] = str(current_helper)
    env["AUDIOTEE_RELEASE_URL"] = (tmp_path / "missing-binary").as_uri()
    env["AUDIOTEE_MANIFEST_URL"] = (tmp_path / "missing-manifest").as_uri()

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "install_audiotee.sh"),
            str(fake_root),
            str(data_dir),
            sys.executable,
        ],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert f"/{architecture}-apple-macosx/release/audiotee" not in result.stdout
    assert "local Swift could not produce a compatible AudioTee" in result.stderr
    assert installed.resolve() == old_helper


def test_missing_vendored_audiotee_fails_without_cloning(tmp_path: Path):
    fake_root = tmp_path / "checkout"
    fake_root.mkdir()
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "install_audiotee.sh"),
            str(fake_root),
            str(tmp_path / "install-data"),
            sys.executable,
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "vendored AudioTee sources are missing" in result.stderr
    assert not (fake_root / "vendor" / "audiotee").exists()


def test_overall_install_receipt_is_published_after_native_install():
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    incomplete_marker = script.index("revision unknown (installation incomplete)")
    tool_install = script.index("\nuv tool install \\\n")
    native_install = script.index('bash "$ROOT/scripts/install_audiotee.sh"')
    final_receipt = script.rindex("printf 'format=1")

    assert incomplete_marker < tool_install < native_install < final_receipt


def test_whole_install_lock_precedes_every_shared_mutation():
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    trap = script.index("trap cleanup EXIT")
    lock = script.index("\nacquire_install_lock\n")
    tool_install = script.index("\nuv tool install \\\n")
    native_install = script.index('bash "$ROOT/scripts/install_audiotee.sh"')
    final_receipt = script.rindex("printf 'format=1")

    assert trap < lock < tool_install < native_install < final_receipt


def test_concurrent_whole_installs_are_serialized(tmp_path: Path):
    event_log = tmp_path / "events"
    data_home = tmp_path / "data-home"
    tool_base = tmp_path / "tools"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    package_fingerprints = {"A": "a" * 64, "B": "b" * 64}
    native_fingerprints = {"A": "c" * 64, "B": "d" * 64}

    fake_uv = fake_bin / "uv"
    _executable(
        fake_uv,
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "if [ \"${1:-}\" = export ]; then\n"
        "  output=\n"
        "  while [ $# -gt 0 ]; do\n"
        "    if [ \"$1\" = --output-file ]; then output=$2; shift 2; else shift; fi\n"
        "  done\n"
        "  printf '%s\\n' '# fake constraints' > \"$output\"\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"${1:-}\" = tool ] && [ \"${2:-}\" = install ]; then\n"
        "  checkout=${!#}\n"
        "  snapshot=$(sed -n '1p' \"$checkout/snapshot-id\")\n"
        "  package=$(sed -n '1p' \"$checkout/package-fingerprint\")\n"
        "  native=$(sed -n '1p' \"$checkout/native-fingerprint\")\n"
        "  printf '%s:uv-start\\n' \"$snapshot\" >> \"$EVENT_LOG\"\n"
        "  if [ \"$snapshot\" = A ]; then sleep 0.5; fi\n"
        "  mkdir -p \"$FAKE_TOOL_BASE/fix-casting/bin\"\n"
        "  python_tmp=\"$FAKE_TOOL_BASE/fix-casting/bin/python.$$\"\n"
        "  {\n"
        "    printf '%s\\n' '#!/bin/sh'\n"
        "    printf '%s\\n' 'case \"$*\" in'\n"
        "    printf \"  *AUDIOTEE_SOURCE_FINGERPRINT*) echo '%s' ;;\\n\" \"$native\"\n"
        "    printf \"  *) echo '%s' ;;\\n\" \"$package\"\n"
        "    printf '%s\\n' 'esac'\n"
        "  } > \"$python_tmp\"\n"
        "  chmod 755 \"$python_tmp\"\n"
        "  mv -f \"$python_tmp\" \"$FAKE_TOOL_BASE/fix-casting/bin/python\"\n"
        "  printf '%s\\n' '#!/bin/sh' 'exit 1' > \"$FAKE_TOOL_BASE/fix-casting/bin/playwright\"\n"
        "  chmod 755 \"$FAKE_TOOL_BASE/fix-casting/bin/playwright\"\n"
        "  printf '%s:uv-end\\n' \"$snapshot\" >> \"$EVENT_LOG\"\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"${1:-}\" = tool ] && [ \"${2:-}\" = dir ]; then\n"
        "  if [ \"${3:-}\" = --bin ]; then\n"
        "    printf '%s\\n' \"$FAKE_TOOL_BIN\"\n"
        "  else\n"
        "    printf '%s\\n' \"$FAKE_TOOL_BASE\"\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        "exit 2\n",
    )

    checkouts: dict[str, Path] = {}
    install_script = (ROOT / "install.sh").read_text(encoding="utf-8")
    for snapshot in ("A", "B"):
        checkout = tmp_path / f"checkout-{snapshot}"
        (checkout / "cast_tab").mkdir(parents=True)
        (checkout / "scripts").mkdir()
        (checkout / "install.sh").write_text(install_script, encoding="utf-8")
        (checkout / "install.sh").chmod(0o755)
        (checkout / "snapshot-id").write_text(f"{snapshot}\n", encoding="utf-8")
        (checkout / "package-fingerprint").write_text(
            f"{package_fingerprints[snapshot]}\n",
            encoding="utf-8",
        )
        (checkout / "native-fingerprint").write_text(
            f"{native_fingerprints[snapshot]}\n",
            encoding="utf-8",
        )
        _executable(
            checkout / "scripts" / "install_audiotee.sh",
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "snapshot=$(sed -n '1p' \"$1/snapshot-id\")\n"
            "test \"$4\" = \"$(sed -n '1p' \"$1/native-fingerprint\")\"\n"
            "printf '%s:native\\n' \"$snapshot\" >> \"$EVENT_LOG\"\n"
            "printf '%s\\n' \"$snapshot\" > \"$2/native-id\"\n",
        )
        checkouts[snapshot] = checkout

    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    environment["HOME"] = str(tmp_path / "home")
    environment["XDG_DATA_HOME"] = str(data_home)
    environment["TMPDIR"] = str(tmp_path)
    environment["EVENT_LOG"] = str(event_log)
    environment["FAKE_TOOL_BASE"] = str(tool_base)
    environment["FAKE_TOOL_BIN"] = str(tmp_path / "tool-bin")
    environment["CAST_INSTALL_LOCK_TIMEOUT_SECONDS"] = "5"

    first = subprocess.Popen(
        ["bash", str(checkouts["A"] / "install.sh")],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if event_log.exists() and "A:uv-start" in event_log.read_text(encoding="utf-8"):
            break
        time.sleep(0.01)
    else:
        first.kill()
        first.communicate()
        raise AssertionError("first installer did not reach the held tool-install phase")

    second = subprocess.Popen(
        ["bash", str(checkouts["B"] / "install.sh")],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    first_output = first.communicate(timeout=10)
    second_output = second.communicate(timeout=10)

    assert first.returncode == 0, first_output
    assert second.returncode == 0, second_output
    assert "fallback Chromium download failed" in first_output[1]
    assert "fallback Chromium download failed" in second_output[1]
    assert event_log.read_text(encoding="utf-8").splitlines() == [
        "A:uv-start",
        "A:uv-end",
        "A:native",
        "B:uv-start",
        "B:uv-end",
        "B:native",
    ]
    final_data = data_home / "fix-casting"
    assert (final_data / "native-id").read_text(encoding="utf-8") == "B\n"
    final_receipt = (final_data / "revision").read_text(encoding="utf-8")
    assert f"package_fingerprint={package_fingerprints['B']}" in final_receipt
    assert not (final_data / "install.lock").exists()


def test_ci_runs_native_audiotee_tests_on_macos():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "native-audiotee:" in workflow
    assert "runs-on: macos-latest" in workflow
    assert "swift test --package-path vendor/audiotee" in workflow


def test_release_tests_native_code_and_publishes_source_manifest():
    workflow = (ROOT / ".github" / "workflows" / "release-audiotee.yml").read_text(
        encoding="utf-8"
    )
    test_step = workflow.index("swift test --package-path vendor/audiotee")
    build_step = workflow.index("swift build --package-path vendor/audiotee -c release")
    fingerprint = workflow.index("scripts/audiotee_source_fingerprint.py")
    runtime_fingerprint = workflow.index("AUDIOTEE_SOURCE_FINGERPRINT")
    fingerprint_check = workflow.index(
        'test "$SOURCE_FINGERPRINT" = "$RUNTIME_FINGERPRINT"'
    )
    asset_creation = workflow.index('install -m 755 "$BIN" "$ASSET"')
    release = workflow.index("gh release create")

    assert (
        test_step
        < build_step
        < fingerprint
        < runtime_fingerprint
        < fingerprint_check
        < asset_creation
        < release
    )
    assert 'test "$("./$ASSET" --protocol-version)" = 2' in workflow
    assert "source_fingerprint=%s" in workflow
    assert "binary_sha256=%s" in workflow
    assert '"audiotee-manifest-$ARCH.txt"' in workflow[release:]


def test_concurrent_native_installs_publish_a_complete_generation(tmp_path: Path):
    fake_root = tmp_path / "checkout"
    package = fake_root / "vendor" / "audiotee"
    (package / "Sources").mkdir(parents=True)
    (package / "Package.swift").write_text("// package\n", encoding="utf-8")
    (package / "Sources" / "Current.swift").write_text("// source\n", encoding="utf-8")
    architecture = subprocess.check_output(["uname", "-m"], text=True).strip()

    current_helper = tmp_path / "current-audiotee"
    _executable(
        current_helper,
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = --protocol-version ]; then echo 2; exit 0; fi\n",
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _executable(
        fake_bin / "swift",
        "#!/bin/sh\n"
        "scratch=\n"
        "while [ $# -gt 0 ]; do\n"
        "  if [ \"$1\" = --scratch-path ]; then scratch=$2; shift 2; else shift; fi\n"
        "done\n"
        "mkdir -p \"$scratch/release\"\n"
        "cp \"$CURRENT_HELPER\" \"$scratch/release/audiotee\"\n"
        "chmod 755 \"$scratch/release/audiotee\"\n",
    )
    _executable(
        fake_bin / "file",
        f"#!/bin/sh\necho \"$1: Mach-O 64-bit executable {architecture}\"\n",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    env["CURRENT_HELPER"] = str(current_helper)
    data_dir = tmp_path / "install-data"
    command = [
        "bash",
        str(ROOT / "scripts" / "install_audiotee.sh"),
        str(fake_root),
        str(data_dir),
        sys.executable,
    ]

    installers = [
        subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(2)
    ]
    results = [installer.communicate(timeout=5) for installer in installers]

    assert [installer.returncode for installer in installers] == [0, 0], results
    installed = data_dir / "bin" / "audiotee"
    resolved = installed.resolve()
    assert installed.is_symlink() and resolved.is_file()
    receipt = (resolved.parent / "receipt").read_text(encoding="utf-8")
    actual_hash = subprocess.check_output(
        ["shasum", "-a", "256", str(resolved)],
        text=True,
    ).split()[0]
    assert f"binary_sha256={actual_hash}" in receipt
    assert f"source_fingerprint={_fingerprint(package)}" in receipt
