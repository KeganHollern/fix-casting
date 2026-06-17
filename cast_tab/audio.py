"""Capture audio from specific Chrome processes on macOS via AudioTee."""

from __future__ import annotations

import array
import fcntl
import json
import os
import re
import select
import shutil
import subprocess
import termios
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


def _pipe_bytes_available(fd: int) -> int:
    """Bytes queued on a pipe read end. 0 while readable means EOF, not data."""
    buf = array.array("i", [0])
    fcntl.ioctl(fd, termios.FIONREAD, buf, True)
    return buf[0]


def _parse_audio_format(line: str) -> AudioFormat | None:
    """Pull the real PCM format out of an AudioTee JSON metadata line."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    payload = data.get("data") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return None
    rate = payload.get("sample_rate")
    channels = payload.get("channels_per_frame")
    bits = payload.get("bits_per_channel")
    encoding = payload.get("encoding")  # e.g. "pcm_f32le"
    if rate is None or channels is None or bits is None or not encoding:
        return None
    try:
        rate = int(float(rate))
        channels = int(channels)
        bits = int(bits)
    except (TypeError, ValueError):
        return None
    ffmpeg_format = str(encoding)
    if ffmpeg_format.startswith("pcm_"):
        ffmpeg_format = ffmpeg_format[len("pcm_") :]
    return AudioFormat(
        sample_rate=rate,
        channels=channels,
        ffmpeg_format=ffmpeg_format,
        sample_bytes=max(1, bits // 8),
    )


@dataclass(frozen=True)
class AudioFormat:
    """The raw PCM format AudioTee is actually emitting (read from metadata)."""

    sample_rate: int
    channels: int
    ffmpeg_format: str  # raw demuxer name, e.g. "f32le" or "s16le"
    sample_bytes: int  # bytes per sample per channel

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * self.sample_bytes


# AudioTee's native macOS format when we don't force a conversion.
DEFAULT_AUDIO_FORMAT = AudioFormat(48_000, 2, "f32le", 4)


@dataclass(frozen=True)
class AudioCapture:
    process: subprocess.Popen[bytes]
    read_fd: int
    pids: tuple[int, ...]
    audio_format: AudioFormat


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


def try_start_chrome_audio_capture(
    user_data_dir: Path,
    *,
    timeout: float = 30.0,
    retry_interval: float = 2.0,
    on_retry: Callable[[], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
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
                return start_chrome_audio_capture(pids, on_stderr=on_stderr)
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
    sample_rate: int | None = None,
    chunk_duration: float = 0.1,
    ready_timeout: float = 5.0,
    on_stderr: Callable[[str], None] | None = None,
) -> AudioCapture:
    """Capture audio from specific Chrome PIDs without touching other apps.

    AudioTee's stdout fd is handed straight to ffmpeg (via pass_fds) — there
    is deliberately no Python relay thread between them. A relay would put a
    GIL-scheduled thread on the path of a real-time audio stream, and any
    scheduling delay stalls AudioTee's pipe and under-runs capture (clicks).

    sample_rate=None (default) lets AudioTee emit the device's native rate so
    it does no resampling; resampling per 100ms chunk leaves a discontinuity
    at every chunk boundary (audible as rapid clicking). The actual rate is
    read back from AudioTee's metadata and carried on the returned capture.
    """
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
    command = [
        str(binary),
        "--include-processes",
        *[str(pid) for pid in pids],
        "--mute",
        "--stereo",
        "--chunk-duration",
        str(chunk_duration),
    ]
    # Only pin a rate if explicitly asked; otherwise pass native through.
    if sample_rate is not None:
        command += ["--sample-rate", str(sample_rate)]
    process = subprocess.Popen(
        command,
        stdout=tap_write,
        stderr=subprocess.PIPE,
    )
    os.close(tap_write)

    # Continuously drain AudioTee stderr: it surfaces capture warnings
    # (under-runs/drops) and, left unread, its pipe fills and deadlocks
    # AudioTee. Lines are buffered so error paths can report them.
    stderr_lines: list[str] = []
    detected_format: dict[str, AudioFormat] = {}
    format_ready = threading.Event()

    def drain_stderr() -> None:
        try:
            if process.stderr is None:
                return
            for raw in process.stderr:
                line = raw.decode(errors="replace").rstrip()
                if not line:
                    continue
                if not format_ready.is_set():
                    fmt = _parse_audio_format(line)
                    if fmt is not None:
                        detected_format["v"] = fmt
                        format_ready.set()
                stderr_lines.append(line)
                if on_stderr is not None:
                    on_stderr(line)
        except (OSError, ValueError):
            pass

    stderr_thread = threading.Thread(
        target=drain_stderr, name="audiotee-stderr", daemon=True
    )
    stderr_thread.start()

    def _fail_exited_early() -> AudioCaptureError:
        stderr_thread.join(timeout=0.3)
        os.close(tap_read)
        return AudioCaptureError(
            f"AudioTee exited early: {' '.join(stderr_lines).strip()}"
        )

    # Wait until AudioTee actually has audio bytes ready, without consuming
    # them (ffmpeg reads the pipe from the first byte). A pipe read end goes
    # "readable" both when data arrives and when the writer dies (EOF), so we
    # confirm with FIONREAD that bytes are really queued before declaring
    # success — otherwise a crashed AudioTee looks like a working tap.
    deadline = time.monotonic() + ready_timeout
    while True:
        readable, _, _ = select.select([tap_read], [], [], 0.2)
        if readable:
            try:
                available = _pipe_bytes_available(tap_read)
            except OSError:
                available = 0
            if available > 0:
                format_ready.wait(timeout=0.5)
                return AudioCapture(
                    process=process,
                    read_fd=tap_read,
                    pids=tuple(pids),
                    audio_format=detected_format.get("v", DEFAULT_AUDIO_FORMAT),
                )
            # Readable with nothing queued == EOF: AudioTee closed stdout.
            raise _fail_exited_early()
        if process.poll() is not None:
            raise _fail_exited_early()
        if time.monotonic() > deadline:
            os.close(tap_read)
            process.terminate()
            raise AudioCaptureError("No audio data received from cast browser tap.")


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