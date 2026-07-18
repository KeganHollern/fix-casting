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
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cast_tab.paths import AUDIOTEE_INSTALL_PATH

ROOT = Path(__file__).resolve().parents[1]
AUDIOTEE_CANDIDATES = (
    ROOT / "bin" / "audiotee",
    ROOT / "vendor" / "audiotee" / ".build" / "release" / "audiotee",
    ROOT / "vendor" / "audiotee" / ".build" / "arm64-apple-macosx" / "release" / "audiotee",
    AUDIOTEE_INSTALL_PATH,
)


class AudioCaptureError(RuntimeError):
    """Raised when tab audio cannot be captured."""


class AudioCaptureCandidateError(AudioCaptureError):
    """A candidate PID could not provide audio; another candidate may work."""


class AudioCaptureCancelled(AudioCaptureError):
    """Audio attachment was cancelled because its owning session is stopping."""


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
    stderr_thread: threading.Thread | None = None
    stderr_tail: deque[str] | None = None

    def raise_if_failed(self) -> None:
        """Surface an AudioTee process which died after successful attach."""
        returncode = self.process.poll()
        if returncode is None:
            return
        details = ""
        if self.stderr_tail:
            try:
                details = " ".join(tuple(self.stderr_tail)).strip()
            except RuntimeError:
                # The stderr drainer may append its final line concurrently.
                pass
        suffix = f": {details}" if details else ""
        raise AudioCaptureError(
            f"AudioTee exited unexpectedly with status {returncode}{suffix}"
        )


def audiotee_path() -> Path | None:
    for candidate in AUDIOTEE_CANDIDATES:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    which = shutil.which("audiotee")
    return Path(which) if which else None


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

    # Chrome's dedicated Audio Service owns the final mixed stream for this
    # isolated browser profile.  Prefer it over renderers: a renderer may be
    # translatable yet yield no PCM, which needlessly burns the per-candidate
    # readiness timeout before we reach the reliable mixed-audio process.
    audio_service = sorted(
        pid for pid, command in lines if "audio.mojom.AudioService" in command
    )
    for pid in audio_service:
        candidates.append([pid])

    renderers = sorted(pid for pid, command in lines if "--type=renderer" in command)
    if renderers:
        candidates.append(renderers)
    # The page renderer is not guaranteed to be one of the first three PIDs.
    # Trying every renderer individually lets the silent-audio primer's process
    # attach even when extension/background renderers sort ahead of it.
    for pid in renderers:
        candidates.append([pid])

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
    cancelled: Callable[[], bool] | None = None,
) -> AudioCapture:
    """Retry candidate taps until Chrome's audio client is producing PCM."""
    deadline = time.monotonic() + timeout
    last_error = "unknown error"

    while time.monotonic() < deadline:
        if cancelled is not None and cancelled():
            raise AudioCaptureCancelled("Audio capture startup was cancelled.")
        if on_retry is not None:
            on_retry()

        for pids in chrome_audio_pid_candidates(user_data_dir):
            if not pids:
                continue
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                return start_chrome_audio_capture(
                    pids,
                    # A primed Audio Service produces its first 100ms chunk
                    # almost immediately.  Keep fallback candidates bounded
                    # so stale/silent renderers cannot consume the whole scan.
                    ready_timeout=min(2.0, remaining),
                    on_stderr=on_stderr,
                    cancelled=cancelled,
                )
            except AudioCaptureCancelled:
                raise
            except AudioCaptureCandidateError as exc:
                last_error = str(exc)
                # Candidate-specific failures include both PID translation and
                # a live tap which produced no bytes yet.  Continue trying the
                # other renderer/audio-service PIDs instead of permanently
                # degrading to video-only after the first silent candidate.
                continue

        if cancelled is not None and cancelled():
            raise AudioCaptureCancelled("Audio capture startup was cancelled.")
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(retry_interval, remaining))

    raise AudioCaptureError(
        "Could not initialize cast browser audio. "
        f"Last AudioTee error: {last_error}"
    )


def start_chrome_audio_capture(
    pids: list[int],
    *,
    sample_rate: int | None = None,
    chunk_duration: float = 0.1,
    ready_timeout: float = 5.0,
    on_stderr: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
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
        raise AudioCaptureError(f"AudioTee is not installed.\n{install_hint()}")
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
    try:
        process = subprocess.Popen(
            command,
            stdout=tap_write,
            stderr=subprocess.PIPE,
        )
    except BaseException:
        os.close(tap_read)
        os.close(tap_write)
        raise
    os.close(tap_write)

    # Continuously drain AudioTee stderr: it surfaces capture warnings
    # (under-runs/drops) and, left unread, its pipe fills and deadlocks
    # AudioTee. Lines are buffered so error paths can report them.
    # Only the tail is needed for startup errors. Keeping every diagnostic for
    # a multi-hour cast makes a noisy AudioTee process an unbounded memory leak.
    stderr_lines: deque[str] = deque(maxlen=100)
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
                    try:
                        on_stderr(line)
                    except Exception:
                        # A reporting callback must never stop the stderr drain;
                        # an undrained pipe eventually deadlocks AudioTee.
                        pass
        except (OSError, ValueError):
            pass

    stderr_thread = threading.Thread(
        target=drain_stderr, name="audiotee-stderr", daemon=True
    )
    stderr_thread.start()

    cleaned_up = False

    def cleanup_failed_start() -> None:
        """Close every resource and reap AudioTee on every non-success path."""
        nonlocal cleaned_up
        if cleaned_up:
            return
        cleaned_up = True
        try:
            os.close(tap_read)
        except OSError:
            pass
        _terminate_and_reap(process)
        stderr_thread.join(timeout=1.0)
        if process.stderr:
            try:
                process.stderr.close()
            except (OSError, ValueError):
                pass

    def exited_early_error() -> AudioCaptureCandidateError:
        if process.poll() is not None:
            stderr_thread.join(timeout=0.3)
        details = " ".join(list(stderr_lines)).strip()
        suffix = f": {details}" if details else ""
        return AudioCaptureCandidateError(f"AudioTee exited early{suffix}")

    # Wait until AudioTee actually has audio bytes ready, without consuming
    # them (ffmpeg reads the pipe from the first byte). A pipe read end goes
    # "readable" both when data arrives and when the writer dies (EOF), so we
    # confirm with FIONREAD that bytes are really queued before declaring
    # success — otherwise a crashed AudioTee looks like a working tap.
    deadline = time.monotonic() + ready_timeout
    try:
        while True:
            if cancelled is not None and cancelled():
                raise AudioCaptureCancelled("Audio capture startup was cancelled.")
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
                        stderr_thread=stderr_thread,
                        stderr_tail=stderr_lines,
                    )
                # Readable with nothing queued == EOF: AudioTee closed stdout.
                raise exited_early_error()
            if process.poll() is not None:
                raise exited_early_error()
            if time.monotonic() > deadline:
                raise AudioCaptureCandidateError(
                    "No audio data received from cast browser tap."
                )
    except BaseException:
        cleanup_failed_start()
        raise


def _terminate_and_reap(
    process: subprocess.Popen[bytes],
    *,
    terminate_timeout: float = 3.0,
    kill_timeout: float = 3.0,
) -> None:
    """Terminate a child and always wait for it so no live/zombie child leaks."""
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=terminate_timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=kill_timeout)
    except subprocess.TimeoutExpired:
        # An uninterruptible child cannot be reaped yet; cleanup should still
        # continue for the rest of the cast pipeline.
        pass


def stop_audio_capture(capture: AudioCapture | None) -> None:
    if capture is None:
        return
    try:
        os.close(capture.read_fd)
    except OSError:
        pass
    _terminate_and_reap(capture.process)
    if capture.stderr_thread is not None:
        capture.stderr_thread.join(timeout=1.0)
    if capture.process.stderr:
        try:
            capture.process.stderr.close()
        except (OSError, ValueError):
            pass


def install_hint() -> str:
    return (
        "Tab audio uses AudioTee (macOS 14.2+) to capture only the cast browser.\n"
        "Other Mac audio is left untouched.\n"
        "Re-run ./install.sh from the fix-casting checkout, or place an "
        f"executable at:\n  {AUDIOTEE_INSTALL_PATH}"
    )
