"""Capture audio from specific Chrome processes on macOS via AudioTee."""

from __future__ import annotations

import array
import fcntl
import json
import os
import re
import select
import subprocess
import termios
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from cast_tab.audiotee_provenance import (
    AUDIOTEE_PROTOCOL_VERSION,
    verify_installed_audiotee,
)
from cast_tab.paths import AUDIOTEE_INSTALL_PATH


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


def _parse_audio_heartbeat(line: str) -> int | None:
    try:
        envelope = json.loads(line)
    except ValueError:
        return None
    if not isinstance(envelope, dict) or envelope.get("message_type") != "heartbeat":
        return None
    payload = envelope.get("data")
    sequence = payload.get("producer_sequence") if isinstance(payload, dict) else None
    if not isinstance(sequence, (int, str)):
        return None
    try:
        parsed = int(sequence)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _parse_audio_format(line: str) -> AudioFormat | None:
    """Pull the real PCM format out of an AudioTee JSON metadata line."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    payload = data.get("data") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return None
    if payload.get("protocol_version") != AUDIOTEE_PROTOCOL_VERSION:
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


class AudioCaptureHealth:
    """Tracks native producer progress reported independently on stderr."""

    def __init__(self, *, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._producer_sequence = 0
        self._heartbeat_seen = False
        self._last_progress_at = time.monotonic()
        self._last_source_check_at = 0.0

    def note_heartbeat(self, producer_sequence: int) -> None:
        with self._lock:
            self._heartbeat_seen = True
            if producer_sequence > self._producer_sequence:
                self._producer_sequence = producer_sequence
                self._last_progress_at = time.monotonic()

    def mark_ready(self) -> None:
        with self._lock:
            # Start the supervisor window at ownership handoff even if the
            # first once-per-second heartbeat is still in flight.
            self._last_progress_at = time.monotonic()

    def stalled_for(self) -> float | None:
        with self._lock:
            # Older helpers did not advertise producer heartbeats. Gate 14's
            # source-matched installer prevents those in production; do not
            # turn one into a false six-second restart loop during upgrades.
            if not self._heartbeat_seen:
                return None
            age = time.monotonic() - self._last_progress_at
            return age if age >= self.timeout_s else None

    def source_check_due(self, *, interval_s: float = 5.0) -> bool:
        now = time.monotonic()
        with self._lock:
            if now - self._last_source_check_at < interval_s:
                return False
            self._last_source_check_at = now
            return True


class AudioCaptureLifecycle:
    """Makes descriptor/process teardown exact-once across signal races."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.fd_closed = False
        self.process_reaped = False
        self.stderr_joined = False
        self.stderr_closed = False
        self.completed = False


@dataclass(frozen=True)
class AudioCapture:
    process: subprocess.Popen[bytes]
    read_fd: int
    pids: tuple[int, ...]
    audio_format: AudioFormat
    stderr_thread: threading.Thread | None = None
    stderr_tail: deque[str] | None = None
    health: AudioCaptureHealth | None = None
    profile_dir: Path | None = None
    lifecycle: AudioCaptureLifecycle = field(
        default_factory=AudioCaptureLifecycle,
        repr=False,
        compare=False,
    )

    def raise_if_failed(self) -> None:
        """Surface an AudioTee process which died after successful attach."""
        returncode = self.process.poll()
        if returncode is None:
            if self.profile_dir is not None and (
                self.health is None or self.health.source_check_due()
            ):
                profile_lines = _profile_process_lines(self.profile_dir)
                if profile_lines is not None:
                    profile_pids = {pid for pid, _command in profile_lines}
                    missing = [pid for pid in self.pids if pid not in profile_pids]
                    if missing:
                        raise AudioCaptureError(
                            "Chrome audio source left the isolated browser profile "
                            f"(PIDs: {', '.join(str(pid) for pid in missing)}); "
                            "AudioTee must reattach"
                        )
            elif self.profile_dir is None:
                for pid in self.pids:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError as exc:
                        raise AudioCaptureError(
                            f"Chrome audio source PID {pid} exited; AudioTee must reattach"
                        ) from exc
                    except PermissionError:
                        # The process exists but is not signalable by this user.
                        pass
            stalled_for = self.health.stalled_for() if self.health is not None else None
            if stalled_for is not None:
                raise AudioCaptureError(
                    "AudioTee producer heartbeat stalled for "
                    f"{stalled_for:.1f}s; capture must reattach"
                )
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


class AudioPrerollDrainer:
    """Temporarily discard uncommitted PCM while ffmpeg changes inputs.

    This is never part of the live media path. It only keeps a newly probed
    AudioTee flowing while the old video-only ffmpeg is torn down; all bytes
    consumed here predate the new joint PTS-zero boundary by definition.
    """

    def __init__(self, capture: AudioCapture) -> None:
        self._capture = capture
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Audio pre-roll drainer was already started.")
        self._thread = threading.Thread(
            target=self._run,
            name="audiotee-preroll-drain",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 1.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        if thread.is_alive():
            raise TimeoutError("Audio pre-roll drainer did not stop")
        if self._failure is not None:
            raise AudioCaptureError("Replacement audio pipe failed during handoff") from self._failure

    def _run(self) -> None:
        fd = self._capture.read_fd
        try:
            while not self._stop.is_set():
                readable, _, _ = select.select([fd], [], [], 0.05)
                if not readable:
                    continue
                available = _pipe_bytes_available(fd)
                if available <= 0:
                    if self._capture.process.poll() is not None:
                        raise AudioCaptureError("Replacement AudioTee closed its PCM pipe")
                    continue
                data = os.read(fd, min(available, 1 << 16))
                if not data:
                    raise AudioCaptureError("Replacement AudioTee closed its PCM pipe")
        except (OSError, ValueError, AudioCaptureError) as exc:
            if not self._stop.is_set():
                self._failure = exc


def audiotee_path() -> Path | None:
    try:
        # Pin the immutable generation before doing any validation. The stable
        # install symlink can legitimately change during a concurrent install;
        # returning it would create a verify-then-execute race.
        candidate = AUDIOTEE_INSTALL_PATH.resolve(strict=True)
    except OSError:
        return None
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        return None
    verified, _reason = verify_installed_audiotee(candidate)
    if verified is None or not _audiotee_protocol_matches(candidate):
        return None
    return candidate


def _audiotee_protocol_matches(candidate: Path) -> bool:
    try:
        result = subprocess.run(
            [str(candidate), "--protocol-version"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return (
        result.returncode == 0
        and result.stdout.strip() == str(AUDIOTEE_PROTOCOL_VERSION)
    )


def audiotee_available() -> bool:
    return audiotee_path() is not None


def _profile_process_lines(user_data_dir: Path) -> list[tuple[int, str]] | None:
    marker = f"--user-data-dir={user_data_dir}"
    try:
        result = subprocess.run(
            ["ps", "ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            # AudioTee has under a second of native+pipe buffering before its
            # real-time writer must fail closed. Source validation is advisory
            # on a transient timeout, so keep it below that runway while the
            # startup loop drains PCM immediately before and after this check.
            timeout=0.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
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
    lines = _profile_process_lines(user_data_dir) or []
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
    # The page renderer is not guaranteed to be one of the first three PIDs.
    # Try renderers individually: one surviving background renderer in a group
    # can keep a silent tap alive after the page renderer turns over, masking
    # the source loss from heartbeat supervision.
    for pid in renderers:
        candidates.append([pid])

    browser = sorted(pid for pid, command in lines if "--type=" not in command)
    if browser:
        candidates.append(browser[:1])

    all_pids = sorted({pid for pid, _ in lines})
    for pid in all_pids:
        candidates.append([pid])

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
                capture = start_chrome_audio_capture(
                    pids,
                    # A primed Audio Service produces its first 100ms chunk
                    # almost immediately.  Keep fallback candidates bounded
                    # so stale/silent renderers cannot consume the whole scan.
                    ready_timeout=min(2.0, remaining),
                    on_stderr=on_stderr,
                    cancelled=cancelled,
                )
                if isinstance(capture, AudioCapture):
                    capture = replace(capture, profile_dir=user_data_dir)
                return capture
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
    process: subprocess.Popen[bytes] | None = None
    tap_write_open = True
    try:
        process = subprocess.Popen(
            command,
            stdout=tap_write,
            stderr=subprocess.PIPE,
        )
        os.close(tap_write)
        tap_write_open = False
    except BaseException:
        _close_fd(tap_read)
        if tap_write_open:
            _close_fd(tap_write)
        if process is not None:
            _rollback_audio_start(process, tap_read=None, stderr_thread=None)
        raise

    # From this point until AudioCapture is returned, this scope owns every
    # child/fd/thread. Even allocation or argument-building failures after
    # Popen must pass through the same rollback path.
    stderr_thread_ref: list[threading.Thread | None] | None = None
    try:
        stderr_thread_ref = [None]
        return _await_chrome_audio_ready(
            process,
            tap_read,
            pids,
            chunk_duration=chunk_duration,
            ready_timeout=ready_timeout,
            on_stderr=on_stderr,
            cancelled=cancelled,
            stderr_thread_ref=stderr_thread_ref,
        )
    except BaseException:
        stderr_thread = (
            stderr_thread_ref[0] if stderr_thread_ref is not None else None
        )
        _rollback_audio_start(process, tap_read, stderr_thread)
        raise


def _await_chrome_audio_ready(
    process: subprocess.Popen[bytes],
    tap_read: int,
    pids: list[int],
    *,
    chunk_duration: float,
    ready_timeout: float,
    on_stderr: Callable[[str], None] | None,
    cancelled: Callable[[], bool] | None,
    stderr_thread_ref: list[threading.Thread | None],
) -> AudioCapture:
    # Continuously drain AudioTee stderr: it surfaces capture warnings
    # (under-runs/drops) and, left unread, its pipe fills and deadlocks
    # AudioTee. Lines are buffered so error paths can report them.
    # Only the tail is needed for startup errors. Keeping every diagnostic for
    # a multi-hour cast makes a noisy AudioTee process an unbounded memory leak.
    stderr_lines: deque[str] = deque(maxlen=100)
    detected_format: dict[str, AudioFormat] = {}
    metadata_failure: dict[str, str] = {}
    format_ready = threading.Event()
    health = AudioCaptureHealth(timeout_s=max(6.0, chunk_duration * 3.0 + 1.0))

    def drain_stderr() -> None:
        try:
            if process.stderr is None:
                return
            for raw in process.stderr:
                line = raw.decode(errors="replace").rstrip()
                if not line:
                    continue
                heartbeat = _parse_audio_heartbeat(line)
                if heartbeat is not None:
                    health.note_heartbeat(heartbeat)
                    # Heartbeats are supervision traffic, not diagnostics.
                    # Keeping them out of the tail also preserves the useful
                    # error context when a long-running helper eventually dies.
                    continue
                if not format_ready.is_set():
                    try:
                        envelope = json.loads(line)
                    except ValueError:
                        envelope = None
                    if (
                        isinstance(envelope, dict)
                        and envelope.get("message_type") == "metadata"
                    ):
                        payload = envelope.get("data")
                        version = (
                            payload.get("protocol_version")
                            if isinstance(payload, dict)
                            else None
                        )
                        if version != AUDIOTEE_PROTOCOL_VERSION:
                            metadata_failure["v"] = (
                                "AudioTee protocol metadata is missing or incompatible "
                                f"(expected {AUDIOTEE_PROTOCOL_VERSION}, got {version!r})"
                            )
                        else:
                            fmt = _parse_audio_format(line)
                            if fmt is None:
                                metadata_failure["v"] = (
                                    "AudioTee emitted invalid PCM metadata"
                                )
                            else:
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
    # Publish the object to the caller's rollback state before start(), which
    # itself may raise after the native AudioTee process already exists.
    stderr_thread_ref[0] = stderr_thread
    stderr_thread.start()

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
    while True:
        if cancelled is not None and cancelled():
            raise AudioCaptureCancelled("Audio capture startup was cancelled.")
        if "v" in metadata_failure:
            raise AudioCaptureCandidateError(metadata_failure["v"])
        readable, _, _ = select.select([tap_read], [], [], 0.2)
        if readable:
            try:
                available = _pipe_bytes_available(tap_read)
            except OSError:
                available = 0
            if available > 0:
                if not format_ready.wait(timeout=0.5):
                    raise AudioCaptureCandidateError(
                        "AudioTee emitted PCM without protocol metadata"
                    )
                if "v" in metadata_failure:
                    raise AudioCaptureCandidateError(metadata_failure["v"])
                audio_format = detected_format.get("v")
                if audio_format is None:
                    raise AudioCaptureCandidateError(
                        "AudioTee did not provide a valid PCM format"
                    )
                health.mark_ready()
                return AudioCapture(
                    process=process,
                    read_fd=tap_read,
                    pids=tuple(pids),
                    audio_format=audio_format,
                    stderr_thread=stderr_thread,
                    stderr_tail=stderr_lines,
                    health=health,
                )
            # Readable with nothing queued == EOF: AudioTee closed stdout.
            raise exited_early_error()
        if process.poll() is not None:
            raise exited_early_error()
        if time.monotonic() > deadline:
            raise AudioCaptureCandidateError(
                "No audio data received from cast browser tap."
            )


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _rollback_audio_start(
    process: subprocess.Popen[bytes],
    tap_read: int | None,
    stderr_thread: threading.Thread | None,
) -> None:
    """Best-effort rollback for every failure before AudioCapture handoff."""
    if tap_read is not None:
        _close_fd(tap_read)
    try:
        _terminate_and_reap(process)
    except BaseException:
        # Cleanup must not hide the startup exception; still close the pipe
        # and stderr resources below if a mocked or broken process API fails.
        pass
    if stderr_thread is not None and getattr(stderr_thread, "ident", None) is not None:
        try:
            stderr_thread.join(timeout=1.0)
        except (RuntimeError, OSError):
            pass
    if process.stderr:
        try:
            process.stderr.close()
        except (OSError, ValueError):
            pass


def _terminate_and_reap(
    process: subprocess.Popen[bytes],
    *,
    terminate_timeout: float = 3.0,
    kill_timeout: float = 3.0,
) -> bool:
    """Terminate a child and always wait for it so no live/zombie child leaks."""
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=terminate_timeout)
        return True
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
        return False
    return True


def stop_audio_capture(capture: AudioCapture | None) -> None:
    if capture is None:
        return
    lifecycle = capture.lifecycle
    with lifecycle.lock:
        if lifecycle.completed:
            return
        if not lifecycle.fd_closed:
            try:
                os.close(capture.read_fd)
            except OSError:
                pass
            lifecycle.fd_closed = True

        if not lifecycle.process_reaped:
            if not _terminate_and_reap(capture.process):
                raise TimeoutError("AudioTee could not be reaped after SIGKILL")
            lifecycle.process_reaped = True

        if not lifecycle.stderr_joined and capture.stderr_thread is not None:
            capture.stderr_thread.join(timeout=1.0)
            if capture.stderr_thread.is_alive():
                raise TimeoutError("AudioTee stderr drainer did not stop")
            lifecycle.stderr_joined = True

        if not lifecycle.stderr_closed and capture.process.stderr:
            try:
                capture.process.stderr.close()
            except (OSError, ValueError):
                pass
            lifecycle.stderr_closed = True
        lifecycle.completed = True


def install_hint() -> str:
    return (
        "Tab audio uses AudioTee (macOS 14.2+) to capture only the cast browser.\n"
        "Other Mac audio is left untouched.\n"
        "Re-run ./install.sh from the fix-casting checkout, or place an "
        f"executable at:\n  {AUDIOTEE_INSTALL_PATH}"
    )
