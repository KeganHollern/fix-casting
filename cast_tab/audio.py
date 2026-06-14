"""Capture audio from specific Chrome processes on macOS via AudioTee."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIOTEE_CANDIDATES = (
    ROOT / "bin" / "audiotee",
    ROOT / "vendor" / "audiotee" / ".build" / "release" / "audiotee",
    ROOT / "vendor" / "audiotee" / ".build" / "arm64-apple-macosx" / "release" / "audiotee",
)


class AudioCaptureError(RuntimeError):
    """Raised when tab audio cannot be captured."""


@dataclass(frozen=True)
class AudioCapture:
    process: subprocess.Popen[bytes]
    read_fd: int
    pids: tuple[int, ...]


def audiotee_path() -> Path | None:
    for candidate in AUDIOTEE_CANDIDATES:
        if candidate.exists():
            return candidate
    return shutil.which("audiotee") and Path(shutil.which("audiotee"))  # type: ignore[arg-type]


def audiotee_available() -> bool:
    return audiotee_path() is not None


def _profile_process_lines(user_data_dir: Path) -> list[tuple[int, str]]:
    marker = f"--user-data-dir={user_data_dir}"
    result = subprocess.run(["ps", "ax", "-o", "pid=,command="], capture_output=True, text=True)
    matches: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        if "Google Chrome" not in line and "Chromium" not in line:
            continue
        if marker not in line:
            continue
        match = re.match(r"\s*(\d+)", line)
        if match:
            matches.append((int(match.group(1)), line))
    return matches


def chrome_pids_for_profile(user_data_dir: Path) -> list[int]:
    """Find Chrome processes launched for a dedicated cast profile."""
    return sorted({pid for pid, _ in _profile_process_lines(user_data_dir)})


def chrome_audio_pid_candidates(user_data_dir: Path) -> list[list[int]]:
    """Return PID sets to try, smallest/most likely first."""
    lines = _profile_process_lines(user_data_dir)
    candidates: list[list[int]] = []

    renderers = sorted(pid for pid, command in lines if "--type=renderer" in command)
    if renderers:
        candidates.append(renderers)
    for pid in renderers[:3]:
        candidates.append([pid])

    audio_service = sorted(
        pid for pid, command in lines if "audio.mojom.AudioService" in command
    )
    if audio_service:
        candidates.append(audio_service[:1])

    browser = sorted(pid for pid, command in lines if "--type=" not in command)
    if browser:
        candidates.append(browser[:1])

    all_pids = sorted({pid for pid, _ in lines})
    if all_pids:
        candidates.append(all_pids)

    deduped: list[list[int]] = []
    seen: set[tuple[int, ...]] = set()
    for candidate in candidates:
        key = tuple(candidate)
        if key not in seen:
            seen.add(key)
            deduped.append(candidate)
    return deduped


def chrome_audio_pids_for_profile(user_data_dir: Path) -> list[int]:
    candidates = chrome_audio_pid_candidates(user_data_dir)
    return candidates[0] if candidates else []


def try_start_chrome_audio_capture(
    user_data_dir: Path,
    *,
    timeout: float = 30.0,
    retry_interval: float = 2.0,
    on_retry: Callable[[], None] | None = None,
) -> AudioCapture:
    """Retry audio tap until Chrome is actively outputting audio."""
    deadline = time.monotonic() + timeout
    last_error = "unknown error"

    while time.monotonic() < deadline:
        if on_retry is not None:
            on_retry()

        for pids in chrome_audio_pid_candidates(user_data_dir):
            if not pids:
                continue
            try:
                return start_chrome_audio_capture(pids)
            except AudioCaptureError as exc:
                last_error = str(exc)
                if "Failed to translate" in last_error or "exited early" in last_error:
                    continue
                raise

        time.sleep(retry_interval)

    raise AudioCaptureError(
        "Could not attach to cast browser audio. "
        f"Make sure the page is playing sound. Last error: {last_error}"
    )


def start_chrome_audio_capture(
    pids: list[int],
    *,
    sample_rate: int = 44100,
    chunk_duration: float = 0.1,
    ready_timeout: float = 5.0,
) -> AudioCapture:
    """Capture audio from specific Chrome PIDs without touching other apps."""
    binary = audiotee_path()
    if binary is None:
        raise AudioCaptureError(
            "AudioTee is not installed. Build it with:\n"
            "  git clone https://github.com/makeusabrew/audiotee.git vendor/audiotee\n"
            "  cd vendor/audiotee && swift build -c release"
        )
    if not pids:
        raise AudioCaptureError("No Chrome process IDs found for tab audio capture.")

    tap_read, tap_write = os.pipe()
    relay_read, relay_write = os.pipe()
    command = [
        str(binary),
        "--include-processes",
        *[str(pid) for pid in pids],
        "--mute",
        "--stereo",
        "--sample-rate",
        str(sample_rate),
        "--chunk-duration",
        str(chunk_duration),
    ]
    process = subprocess.Popen(
        command,
        stdout=tap_write,
        stderr=subprocess.PIPE,
    )
    os.close(tap_write)

    if process.poll() is not None:
        stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
        os.close(tap_read)
        os.close(relay_read)
        os.close(relay_write)
        raise AudioCaptureError(f"AudioTee failed to start: {stderr.strip()}")

    ready = threading.Event()

    def relay_audio() -> None:
        try:
            while True:
                chunk = os.read(tap_read, 65_536)
                if not chunk:
                    break
                os.write(relay_write, chunk)
                ready.set()
        except OSError:
            pass
        finally:
            try:
                os.close(relay_write)
            except OSError:
                pass
            try:
                os.close(tap_read)
            except OSError:
                pass

    relay_thread = threading.Thread(target=relay_audio, name="audio-relay", daemon=True)
    relay_thread.start()

    deadline = time.monotonic() + ready_timeout
    while not ready.wait(timeout=0.2):
        if process.poll() is not None:
            stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
            os.close(relay_read)
            raise AudioCaptureError(f"AudioTee exited early: {stderr.strip()}")
        if time.monotonic() > deadline:
            os.close(relay_read)
            process.terminate()
            raise AudioCaptureError(
                "No audio data received from cast browser tap."
            )

    return AudioCapture(process=process, read_fd=relay_read, pids=tuple(pids))


def stop_audio_capture(capture: AudioCapture | None) -> None:
    if capture is None:
        return
    try:
        os.close(capture.read_fd)
    except OSError:
        pass
    if capture.process.poll() is None:
        capture.process.terminate()
        try:
            capture.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            capture.process.kill()
    if capture.process.stderr:
        capture.process.stderr.close()


def install_hint() -> str:
    return (
        "Tab audio uses AudioTee (macOS 14.2+) to capture only the cast browser.\n"
        "Other Mac audio is left untouched.\n"
        "Build with:\n"
        "  cd vendor/audiotee && swift build -c release"
    )