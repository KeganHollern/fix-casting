"""HLS streaming orchestrator: paced sampling → ffmpeg encode → HTTP serving.

The pieces live in sibling modules — command construction and the ffmpeg
process in `encoder`, the frame-pacing primitives in `pacing`, the HTTP
server in `server`. This module owns the threads and the A/V-sync-critical
ordering between them.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from cast_tab.audio import DEFAULT_AUDIO_FORMAT, AudioFormat, _pipe_bytes_available
from cast_tab.encoder import (  # re-exported for callers (cli, tools)
    DEFAULT_JPEG_QUALITY,
    FfmpegProcess,
    codec_label,
    default_fps_for_resolution,
    hls_args,
    video_encoder_args,
)
from cast_tab.pacing import BoundedFrameQueue, LatestFrame
from cast_tab.server import HLSHTTPServer, get_local_ip
from cast_tab.stats import PipelineStats

__all__ = [
    "DEFAULT_JPEG_QUALITY",
    "HLSStreamer",
    "MAX_AUTO_AV_OFFSET_MS",
    "codec_label",
    "default_fps_for_resolution",
    "get_local_ip",
]

FFMPEG_BACKPRESSURE_WRITE_S = 0.050
FFMPEG_BACKPRESSURE_DURATION_S = 60.0

# A broken ffmpeg pipe is usually transient (for example, a hardware encoder
# process dying), so retry it.  Persistent failures must not turn the writer
# thread into a hot respawn loop, though: cap consecutive failures and sleep
# interruptibly between attempts.  A successful frame write closes the
# circuit and resets the counter.
FFMPEG_RESTART_MAX_FAILURES = 5
FFMPEG_RESTART_BASE_DELAY_S = 0.25
FFMPEG_RESTART_MAX_DELAY_S = 2.0
# A replacement must run for this long before one successful write is enough
# to close the recovery circuit.  Otherwise an encoder which accepts a frame
# and immediately dies can restart forever without accumulating failures.
FFMPEG_RESTART_STABLE_S = 10.0
FFMPEG_WRITER_GRACEFUL_JOIN_S = 0.1
# A write which has not returned by this deadline is indistinguishable from a
# live-but-wedged encoder.  raise_if_failed() drives its bounded recovery.
FFMPEG_WRITE_STALL_S = 60.0
HEALTH_POLL_S = 0.1

# Keep filling video frames to catch up after a stall this long or shorter (so
# the encoded timeline stays locked to wall-clock and audio can't drift ahead);
# only abandon catch-up past it (machine slept, multi-second hang).
SAMPLER_MAX_CATCHUP_S = 5.0

# Clamp the manual --audio-offset-ms trim to a sane range; covers the audio
# pre-roll plus the video frame-queue latency we compensate for.
MAX_AUTO_AV_OFFSET_S = 3.0
MAX_AUTO_AV_OFFSET_MS = int(MAX_AUTO_AV_OFFSET_S * 1000)


class HLSStreamer:
    """Encode JPEG frames into an HLS stream served over HTTP."""

    def __init__(
        self,
        *,
        width: int = 1920,
        height: int = 1080,
        fps: int = 24,
        buffered: bool = True,
        audio_fd: int | None = None,
        audio_format: AudioFormat | None = None,
        audio_offset_ms: int = 0,
        audio_drift_ppm: float = 0.0,
        video_bitrate_mbps: float | None = None,
        port: int = 0,
        work_dir: Path | None = None,
        stats: PipelineStats | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.buffered = buffered
        self.audio_fd = audio_fd
        self.audio_format = audio_format or DEFAULT_AUDIO_FORMAT
        # Keep the public value identical to what the ffmpeg filter receives.
        # Previously values above the supported maximum were displayed and
        # returned unchanged even though the command silently applied 3000ms.
        self.audio_offset_ms = self._clamp_audio_offset_ms(audio_offset_ms)
        self.audio_drift_ppm = audio_drift_ppm
        # Test-only: sleep this many ms after each stdin write to simulate a slow
        # encoder, so the frame queue backs up (reproduces queue-delay lead).
        self._test_write_delay_s = (
            float(os.environ.get("CAST_TEST_WRITE_DELAY_MS", "0") or "0") / 1000.0
        )
        self.video_bitrate_mbps = video_bitrate_mbps
        # Unique per run: a fixed path means two simultaneous casts silently
        # serve each other's segments. We only remove dirs we created; an
        # explicit work_dir (the tools harnesses) is the caller's to manage.
        self._owns_work_dir = work_dir is None
        self.work_dir = work_dir or Path(tempfile.mkdtemp(prefix="cast-tab-stream-"))
        self.work_dir.mkdir(parents=True, exist_ok=True)

        for old in self.work_dir.glob("seg*.ts"):
            old.unlink(missing_ok=True)
        playlist = self.work_dir / "stream.m3u8"
        playlist.unlink(missing_ok=True)

        self._latest = LatestFrame()
        # Set the first time a captured frame is published. We hold ffmpeg's
        # spawn until this fires so the audio and video inputs anchor their
        # PTS=0 to the same moment (see start()).
        self._first_frame = threading.Event()
        self._ffmpeg: FfmpegProcess | None = None
        self._sampler_thread: threading.Thread | None = None
        self._writer_thread: threading.Thread | None = None
        self._http: HLSHTTPServer | None = None
        self._port = port
        self._stopped = threading.Event()
        self._stats = stats
        self._ffmpeg_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._lifecycle_state = "new"
        self._fatal_lock = threading.Lock()
        self._fatal_error: RuntimeError | None = None
        self._consecutive_ffmpeg_failures = 0
        self._ffmpeg_generation = 0
        self._ffmpeg_started_at: float | None = None
        self._write_state_lock = threading.Lock()
        self._write_started_at: float | None = None
        self._write_generation: int | None = None
        self._write_stall_recovery_started = False
        self._backpressure_started_at: float | None = None
        self._last_sampled_generation = -1
        self._known_hls_segments: set[str] = set()
        # Bounded at ~1s of frames: the queue's depth is the live audio lead
        # (see BoundedFrameQueue's docstring for the full mechanism).
        self._queue = BoundedFrameQueue(maxlen=max(1, self.fps))

    @property
    def playlist_url(self) -> str:
        host = get_local_ip()
        http = self._http
        port = self._port or (http.port if http is not None else 0)
        return f"http://{host}:{port}/stream.m3u8"

    @property
    def fatal_error(self) -> RuntimeError | None:
        """A terminal background-pipeline failure, if one has occurred.

        The writer runs outside the owner's thread, so an exception there
        cannot propagate normally.  Owners that remain active after startup
        can inspect this property or call :meth:`raise_if_failed` from their
        event loop instead of silently serving a frozen playlist.
        """
        with self._fatal_lock:
            return self._fatal_error

    def raise_if_failed(self) -> None:
        """Raise/recover failures recorded by any background component."""
        error = self.fatal_error
        if error is not None:
            raise error
        self._recover_stalled_write_if_needed()
        # Wedge recovery can open the circuit synchronously.
        error = self.fatal_error
        if error is not None:
            raise error
        http = self._http
        if http is not None:
            http.raise_if_failed()

    def _recover_stalled_write_if_needed(self) -> None:
        if self._stopped.is_set():
            return
        with self._write_state_lock:
            started_at = self._write_started_at
            generation = self._write_generation
            if (
                started_at is None
                or generation is None
                or self._write_stall_recovery_started
                or time.monotonic() - started_at < FFMPEG_WRITE_STALL_S
            ):
                return
            # Only one concurrent health checker may recover this write.
            self._write_stall_recovery_started = True
        with self._ffmpeg_lock:
            ffmpeg = (
                self._ffmpeg
                if self._ffmpeg_generation == generation
                else None
            )
        if ffmpeg is None:
            return
        stalled_for = time.monotonic() - started_at
        self._recover_ffmpeg(
            ffmpeg,
            generation,
            TimeoutError(f"ffmpeg stdin write blocked for {stalled_for:.1f}s"),
        )

    def _fail(self, message: str, cause: BaseException | None = None) -> None:
        """Record the first fatal error and stop all producer threads."""
        detail = message
        if cause is not None and str(cause):
            detail = f"{message}: {cause}"
        with self._fatal_lock:
            if self._fatal_error is not None:
                return
            self._fatal_error = RuntimeError(detail)
        self._stopped.set()
        self._first_frame.set()
        self._queue.wake_all()

    @staticmethod
    def _clamp_audio_offset_ms(offset_ms: int) -> int:
        return min(MAX_AUTO_AV_OFFSET_MS, max(0, int(offset_ms)))

    # How long to wait for Chrome's screencast to deliver its first frame
    # before spawning ffmpeg anyway. Chrome's screencast can take several
    # seconds to warm up; we'd rather wait than anchor audio without video.
    FIRST_FRAME_TIMEOUT_S = 30.0

    def _startup_step(self, action: Callable[[], None]) -> bool:
        """Run one resource-creation step atomically against stop()."""
        with self._lifecycle_lock:
            if self._lifecycle_state != "starting" or self._stopped.is_set():
                return False
            action()
            return True

    def _wait_for_first_frame(
        self,
        health_check: Callable[[], None] | None,
    ) -> bool:
        deadline = time.monotonic() + self.FIRST_FRAME_TIMEOUT_S
        while True:
            if health_check is not None:
                health_check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._first_frame.wait(min(HEALTH_POLL_S, remaining)):
                if health_check is not None:
                    health_check()
                return True

    def start(
        self,
        *,
        health_check: Callable[[], None] | None = None,
    ) -> None:
        with self._lifecycle_lock:
            if self._lifecycle_state != "new":
                raise RuntimeError("HLS streamer has already been started or stopped.")
            self._lifecycle_state = "starting"
        self.raise_if_failed()
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg is required but was not found in PATH.")

        # Hold ffmpeg until the first frame is captured. ffmpeg stamps both the
        # audio pipe and the video pipe from their first byte; audio is flowing
        # the instant ffmpeg opens it, but Chrome's screencast warms up several
        # seconds later. Spawning early anchors audio PTS=0 to "now" and video
        # PTS=0 to "now + warmup", baking that whole gap in as audio-ahead skew.
        # Waiting for the first frame anchors both inputs to the same moment.
        if not self._wait_for_first_frame(health_check):
            print(
                "Warning: no captured frame after "
                f"{self.FIRST_FRAME_TIMEOUT_S:.0f}s; starting ffmpeg anyway "
                "(audio may lead video).",
                flush=True,
            )
        if self._stopped.is_set():
            # Shut down before the first frame arrived; don't spawn anything.
            return

        def start_ffmpeg() -> None:
            with self._ffmpeg_lock:
                self._start_ffmpeg()

        if not self._startup_step(start_ffmpeg):
            return
        if not self._startup_step(self._start_sampler_thread):
            return
        if not self._startup_step(self._start_writer_thread):
            return

        def start_http() -> None:
            self._http = HLSHTTPServer(self.work_dir, self._port)
            self._http.start()

        if not self._startup_step(start_http):
            return
        with self._lifecycle_lock:
            if self._lifecycle_state == "starting" and not self._stopped.is_set():
                self._lifecycle_state = "running"

    def publish_frame(self, jpeg_data: bytes) -> None:
        if not self._stopped.is_set():
            self._latest.publish(jpeg_data)
            self._first_frame.set()
            if self._stats is not None:
                self._stats.trace("first frame published to streamer", once=True)

    def poll_audio_backlog(self) -> None:
        """Sample unread bytes in the audio pipe (ffmpeg's read backlog).

        FIONREAD is a non-destructive ioctl, so checking the backlog from here
        doesn't disturb the bytes ffmpeg reads off the same pipe. A backlog
        pinned near 0 means ffmpeg consumes audio as fast as AudioTee makes it;
        a growing backlog means audio is being buffered (delayed) before mux.
        """
        if self._stats is None or self.audio_fd is None:
            return
        try:
            backlog = _pipe_bytes_available(self.audio_fd)
        except OSError:
            return
        self._stats.record_audio_backlog(
            backlog / self.audio_format.bytes_per_second * 1000
        )

    def poll_hls_stats(self) -> list[str]:
        if self._stats is None:
            return []
        segments = sorted(self.work_dir.glob("seg*.ts"))
        newest_age: float | None = None
        if segments:
            newest_age = time.time() - segments[-1].stat().st_mtime

        current = {path.name for path in segments}
        had_segments = bool(self._known_hls_segments)
        deleted = sorted(self._known_hls_segments - current)
        events: list[str] = []
        if deleted and had_segments:
            events.append(f"hls deleted {', '.join(deleted)}")
        self._known_hls_segments = current

        self._stats.record_hls(
            segment_count=len(segments),
            newest_age_s=newest_age,
            segments_deleted=len(deleted) if had_segments else 0,
        )
        return events

    def wait_until_ready(
        self,
        timeout: float | None = None,
        *,
        health_check: Callable[[], None] | None = None,
    ) -> None:
        """Block until the HLS playlist and first segment exist."""
        if timeout is None:
            timeout = 60.0 if self.buffered else 30.0
        playlist = self.work_dir / "stream.m3u8"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if health_check is not None:
                health_check()
            self.raise_if_failed()
            if playlist.exists() and list(self.work_dir.glob("seg*.ts")):
                if health_check is not None:
                    health_check()
                return
            if self._stopped.wait(HEALTH_POLL_S):
                self.raise_if_failed()
                raise RuntimeError("HLS streamer stopped before becoming ready.")
        raise TimeoutError("Timed out waiting for the HLS stream to become ready.")

    def stop(self) -> None:
        # Serializing teardown makes repeated/concurrent stop calls wait for a
        # complete cleanup pass instead of racing component ownership.
        with self._stop_lock:
            with self._lifecycle_lock:
                self._lifecycle_state = "stopping"
                self._stopped.set()
                # Unblock start(), the sampler sleep, and an empty queue wait.
                self._first_frame.set()
                self._queue.wake_all()

            failures: list[tuple[str, BaseException]] = []

            def attempt(name: str, cleanup: Callable[[], None]) -> None:
                try:
                    cleanup()
                except BaseException as exc:
                    failures.append((name, exc))

            def join_sampler() -> None:
                thread = self._sampler_thread
                if (
                    thread is not None
                    and thread is not threading.current_thread()
                    and thread.is_alive()
                ):
                    thread.join(timeout=1)

            attempt("sampler thread", join_sampler)

            # Give an ordinary writer one scheduling turn to observe the stop
            # event. If it exits, graceful EOF lets ffmpeg flush diagnostics.
            writer = self._writer_thread
            if (
                writer is not None
                and writer is not threading.current_thread()
                and writer.is_alive()
            ):
                attempt(
                    "writer grace period",
                    lambda: writer.join(timeout=FFMPEG_WRITER_GRACEFUL_JOIN_S),
                )
            writer_done = not (writer is not None and writer.is_alive())

            def stop_ffmpeg() -> None:
                with self._ffmpeg_lock:
                    self._kill_ffmpeg(graceful=writer_done)

            # A wedged stdin.write() only unblocks when this closes ffmpeg's
            # read end, so kill before the writer's full join.
            attempt("ffmpeg", stop_ffmpeg)

            def join_writer() -> None:
                if (
                    writer is not None
                    and writer is not threading.current_thread()
                    and writer.is_alive()
                ):
                    writer.join(timeout=1)

            attempt("writer thread", join_writer)

            def stop_http() -> None:
                http = self._http
                if http is not None:
                    http.stop()
                    self._http = None

            attempt("HLS HTTP server", stop_http)

            if self._owns_work_dir:
                attempt(
                    "HLS work directory",
                    lambda: shutil.rmtree(self.work_dir, ignore_errors=True),
                )

            with self._lifecycle_lock:
                self._lifecycle_state = "stopped"

            if len(failures) == 1:
                name, failure = failures[0]
                failure.add_note(f"HLSStreamer failed while stopping {name}.")
                raise failure
            if failures:
                names = ", ".join(name for name, _failure in failures)
                raise BaseExceptionGroup(
                    f"HLSStreamer failed while stopping: {names}",
                    [failure for _name, failure in failures],
                )

    def _input_sample_rate(self) -> int:
        """Device's true PCM rate = nominal + measured drift. ppm>0 means the
        device clock runs fast (audio leads over time); declaring the higher
        input rate makes ffmpeg resample the surplus back down to the forced
        nominal output rate, locking audio to real time. ppm=0 is a no-op."""
        nominal = self.audio_format.sample_rate
        if not self.audio_drift_ppm:
            return nominal
        return max(1, round(nominal * (1.0 + self.audio_drift_ppm / 1_000_000.0)))

    def _audio_input_args(self) -> list[str]:
        if self.audio_fd is not None:
            return [
                "-thread_queue_size",
                "4096",
                # The PCM format is fully specified below, so ffmpeg needs no
                # stream analysis. Without these, ffmpeg waits to accumulate
                # ~analyzeduration (5s default) of audio before it starts — and
                # since audio arrives at real-time, that is a multi-second
                # startup stall that backs video up and desyncs A/V.
                "-probesize",
                "32",
                "-analyzeduration",
                "0",
                # Match AudioTee's actual native PCM format exactly so ffmpeg
                # never misreads the bytes (wrong format == white noise).
                "-f",
                self.audio_format.ffmpeg_format,
                # Declare the input at the device's TRUE rate (nominal + drift).
                # The output is forced to the nominal rate (below), so ffmpeg
                # does ONE constant resample of the drift ratio. This is the
                # clock-drift fix: AudioTee's device clock runs slightly fast
                # vs the system clock that paces 30fps video, so audio leads
                # over long runs; resampling the true rate down to nominal locks
                # audio back to real time. A constant ratio (unlike async) adds
                # no silence and no jitter — it is inaudible (~100ppm).
                "-ar",
                str(self._input_sample_rate()),
                "-ac",
                str(self.audio_format.channels),
                "-i",
                f"/dev/fd/{self.audio_fd}",
            ]
        return [
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
        ]

    def _audio_delay_filter_args(self) -> list[str]:
        """Delay audio to match the video pipeline's latency (lip-sync).

        Video runs through the capture + frame-queue + encoder path and reaches
        the muxer later than the near-direct audio, so audio plays ahead. We
        prepend silence with the adelay filter to push audio later by
        --audio-offset-ms.

        Input-side -itsoffset is silently ignored for a raw-PCM pipe (ffmpeg
        regenerates the timestamps from 0), so the delay must live in the audio
        filter graph instead. adelay only adds delay, which is all we need: the
        skew is always audio-ahead.
        """
        if self.audio_fd is None or self.audio_offset_ms == 0:
            return []
        print(f"A/V sync: delaying audio {self.audio_offset_ms}ms (adelay).", flush=True)
        return ["-af", f"adelay={self.audio_offset_ms}:all=1"]

    def _drain_audio_fd(self) -> None:
        """Discard PCM that buffered in the pipe before ffmpeg attaches.

        AudioTee streams into the pipe from the moment it starts, but ffmpeg
        only opens the fd when it (re)launches here. Whatever sat in the OS pipe
        buffer in the meantime (~64KB, ~170ms) would otherwise be read as the
        start of the stream and play ahead of video. Drop it so audio and video
        both effectively begin "now". Bounded so a live writer can't spin us.
        """
        if self.audio_fd is None:
            return
        max_drop = self.audio_format.bytes_per_second * 2
        dropped = 0
        try:
            while dropped < max_drop:
                available = _pipe_bytes_available(self.audio_fd)
                if available <= 0:
                    break
                chunk = os.read(self.audio_fd, min(available, 1 << 16))
                if not chunk:
                    break
                dropped += len(chunk)
        except OSError:
            return
        if dropped:
            ms = dropped / self.audio_format.bytes_per_second * 1000
            print(f"A/V sync: dropped {ms:.0f}ms of buffered pre-roll audio.", flush=True)
            if self._stats is not None:
                self._stats.trace(f"audio pre-roll drained ({ms:.0f}ms)")

    def _kill_ffmpeg(self, *, graceful: bool = False) -> None:
        if self._ffmpeg is None:
            return
        self._ffmpeg.kill(graceful=graceful)
        self._ffmpeg = None
        self._ffmpeg_started_at = None

    def set_audio_offset_ms(self, offset_ms: int) -> int:
        """Change the A/V audio delay live and apply it.

        The delay is an adelay filter baked into the ffmpeg command, so applying
        a new value means relaunching ffmpeg (a brief glitch + buffer refill).
        Returns the value actually applied (unchanged → no relaunch). Negative
        values are clamped to 0 (audio is only ever ahead, never behind).
        """
        offset_ms = self._clamp_audio_offset_ms(offset_ms)
        if offset_ms == self.audio_offset_ms:
            return offset_ms
        self.audio_offset_ms = offset_ms
        if not self._stopped.is_set():
            self._relaunch_ffmpeg(reset_failures=True)
        return offset_ms

    def _relaunch_ffmpeg(
        self,
        *,
        expected_generation: int | None = None,
        reset_failures: bool = False,
    ) -> bool:
        with self._ffmpeg_lock:
            if (
                expected_generation is not None
                and self._ffmpeg_generation != expected_generation
            ):
                return False
            self._kill_ffmpeg()
            # Drop the queued backlog: the sampler keeps producing during the
            # relaunch gap, and a fresh ffmpeg would otherwise inherit and
            # buffer seconds of stale video, inflating the A/V latency.
            self._queue.clear()
            # Re-check under the lock: stop() may have completed between our
            # caller's check and here (e.g. the TUI's debounced offset apply
            # racing a quit) — spawning now would orphan an ffmpeg pointed at
            # the already-removed work dir.
            if self._stopped.is_set():
                return False
            self._start_ffmpeg()
            if reset_failures:
                self._consecutive_ffmpeg_failures = 0
        self._backpressure_started_at = None
        return True

    def _restart_ffmpeg(
        self,
        reason: str,
        *,
        expected_generation: int,
    ) -> bool:
        restarted = self._relaunch_ffmpeg(
            expected_generation=expected_generation,
        )
        if restarted:
            print(f"Restarted ffmpeg after {reason}.", flush=True)
            if self._stats is not None:
                self._stats.record_ffmpeg_restart()
        return restarted

    @staticmethod
    def _ffmpeg_failure_detail(
        ffmpeg: FfmpegProcess | None,
        write_error: BaseException | None,
    ) -> str:
        details: list[str] = []
        if write_error is not None and str(write_error):
            details.append(str(write_error))
        if ffmpeg is not None:
            try:
                returncode = ffmpeg.poll()
                if returncode is not None:
                    details.append(f"exit status {returncode}")
                stderr = ffmpeg.stderr_text(join_timeout=0.1).strip()
                if stderr:
                    details.append(stderr)
            except Exception:
                # A process can disappear while shutdown/relaunch races this
                # diagnostic.  The failure count is still useful on its own.
                pass
        return "; ".join(details)

    def _recover_ffmpeg(
        self,
        failed_ffmpeg: FfmpegProcess | None,
        failed_generation: int,
        write_error: BaseException | None,
    ) -> bool:
        """Retry a broken encoder without allowing a respawn storm.

        Returns True once a replacement was spawned. The counter is reset only
        after that replacement remains healthy for the stability window; an
        encoder which accepts one frame and immediately dies therefore still
        opens the circuit and records a fatal error.
        """
        with self._ffmpeg_lock:
            if (
                self._ffmpeg_generation != failed_generation
                or self._ffmpeg is not failed_ffmpeg
            ):
                # A user-driven relaunch replaced the failed instance between
                # the writer's snapshot and recovery.  Leave that fresh
                # process alone and let its first write determine its health.
                return True
        detail = self._ffmpeg_failure_detail(failed_ffmpeg, write_error)
        while not self._stopped.is_set():
            with self._ffmpeg_lock:
                if self._ffmpeg_generation != failed_generation:
                    return True
            self._consecutive_ffmpeg_failures += 1
            failures = self._consecutive_ffmpeg_failures
            if failures >= FFMPEG_RESTART_MAX_FAILURES:
                message = (
                    "ffmpeg failed "
                    f"{failures} consecutive times; encoder recovery stopped"
                )
                if detail:
                    message = f"{message} ({detail})"
                # Usually the failed process has already exited, but a pipe
                # can also break while it is still alive.  Do not leave it
                # behind after opening the circuit. Keep the generation check
                # and terminal transition atomic against a user relaunch.
                with self._ffmpeg_lock:
                    if self._ffmpeg_generation != failed_generation:
                        return True
                    self._fail(message)
                    self._kill_ffmpeg()
                return False

            delay_s = min(
                FFMPEG_RESTART_BASE_DELAY_S * (2 ** (failures - 1)),
                FFMPEG_RESTART_MAX_DELAY_S,
            )
            print(
                "ffmpeg encoder failed"
                + (f" ({detail})" if detail else "")
                + f"; retrying in {delay_s:g}s "
                + f"({failures}/{FFMPEG_RESTART_MAX_FAILURES - 1}).",
                flush=True,
            )
            if self._stopped.wait(delay_s):
                return False
            try:
                restarted = self._restart_ffmpeg(
                    "encoder failure",
                    expected_generation=failed_generation,
                )
            except Exception as exc:
                detail = str(exc) or type(exc).__name__
                failed_ffmpeg = None
                continue
            # A user-driven relaunch changed the generation during our delay.
            # Its process is already the current recovery candidate; never kill
            # it merely because an older generation had failed.
            return restarted or not self._stopped.is_set()
        return False

    def _note_encode_backpressure(self, write_s: float, generation: int) -> None:
        now = time.monotonic()
        if write_s >= FFMPEG_BACKPRESSURE_WRITE_S:
            if self._backpressure_started_at is None:
                self._backpressure_started_at = now
            elif now - self._backpressure_started_at >= FFMPEG_BACKPRESSURE_DURATION_S:
                self._restart_ffmpeg(
                    "sustained encoder backpressure",
                    expected_generation=generation,
                )
        else:
            self._backpressure_started_at = None

    def _start_ffmpeg(self) -> None:
        playlist = self.work_dir / "stream.m3u8"
        segment_pattern = str(self.work_dir / "seg%03d.ts")

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            # THE A/V SYNC FIX: don't let ffmpeg spend its default ~5s
            # analyzeduration probing the MJPEG pipe before it starts. During
            # that window ffmpeg doesn't drain pipe:0, so our frame queue fills
            # and starts dropping frames; because video PTS is frame-count-based
            # (-framerate below), every dropped frame shifts the video timeline
            # earlier against the audio, producing a ~1s audio-ahead skew. The
            # MJPEG format is fully known, so skip the probe and start at once —
            # the queue then stays at depth ~1 and never drops. A buffered demux
            # thread on the pipe (matching the audio input) keeps video flowing
            # even if the transcode loop briefly waits on the real-time audio fd.
            "-thread_queue_size",
            "1024",
            "-probesize",
            "32",
            "-analyzeduration",
            "0",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-framerate",
            str(self.fps),
            "-i",
            "pipe:0",
            *self._audio_input_args(),
            # Capture is already viewport-sized (== output), so skip scale/crop
            # and only convert pixel format. Dropping the per-frame scale pass
            # gives ffmpeg throughput headroom to drain the frame queue, which
            # is what keeps video ~0.5s behind the audio.
            "-filter:v",
            "format=yuv420p",
            "-map",
            "0:v",
            "-map",
            "1:a",
            *video_encoder_args(
                self.fps,
                self.width,
                self.height,
                buffered=self.buffered,
                bitrate_mbps=self.video_bitrate_mbps,
            ),
            *self._audio_delay_filter_args(),
            "-c:a",
            "aac",
            "-b:a",
            "160k" if self.buffered else "128k",
            # Keep the capture's native rate end to end so nothing resamples.
            "-ar",
            str(self.audio_format.sample_rate),
            "-ac",
            str(self.audio_format.channels),
            *hls_args(buffered=self.buffered),
            "-hls_segment_filename",
            segment_pattern,
            "-max_muxing_queue_size",
            "1024",
            str(playlist),
        ]

        pass_fds: tuple[int, ...] = ()
        if self.audio_fd is not None:
            pass_fds = (self.audio_fd,)
            # Drain the pipe right before the child opens it so ffmpeg starts
            # reading at "now" instead of inheriting buffered pre-roll audio.
            self._drain_audio_fd()
        if self._stats is not None:
            self._stats.trace("ffmpeg spawn (audio+video PTS=0 anchor)")
        self._ffmpeg = FfmpegProcess(cmd, pass_fds=pass_fds, stats=self._stats)
        self._ffmpeg_generation += 1
        self._ffmpeg_started_at = time.monotonic()
        if self._stats is not None:
            self._stats.trace("ffmpeg spawned")

    def _enqueue_frame(self, frame: bytes) -> None:
        depth, dropped = self._queue.put(frame)
        if self._stats is not None:
            self._stats.record_queue(depth=depth, dropped=dropped)

    def _start_sampler_thread(self) -> None:
        """Sample the latest frame at an exactly even cadence and enqueue it.

        Keeping this loop free of the (variable-latency) ffmpeg write is what
        eliminates motion judder: every output frame represents an evenly
        spaced moment in real time, regardless of write stalls downstream.
        """

        def sample() -> None:
            frame_period = 1.0 / self.fps
            next_tick = time.monotonic()

            while not self._stopped.is_set():
                now = time.monotonic()
                sleep_for = next_tick - now
                if sleep_for > 0:
                    self._stopped.wait(sleep_for)
                    now = time.monotonic()
                # When we fall behind schedule, keep the loop running back-to-
                # back (no sleep) so it feeds one frame per missed tick — those
                # catch-up frames hold the encoded timeline level with wall-clock
                # so audio can't drift ahead of video. Only give up and resync
                # past a large gap (machine slept), where bursting the whole
                # backlog isn't worth it. The bounded queue caps the burst.
                if now - next_tick > SAMPLER_MAX_CATCHUP_S:
                    next_tick = now
                    if self._stats is not None:
                        self._stats.record_encode_resync()
                next_tick += frame_period

                frame, published_at, generation = self._latest.peek()
                if frame is None:
                    continue
                if self._stats is not None:
                    self._stats.trace("first sampler tick (video PTS=0 frame)", once=True)

                # One frame per tick holds a constant input rate. When capture
                # produced nothing new, re-enqueue the latest; skipping it would
                # make the encoded timeline lag wall-clock and drain the TV.
                if generation == self._last_sampled_generation:
                    if self._stats is not None:
                        self._stats.record_encode_repeat()
                elif self._stats is not None and published_at is not None:
                    self._stats.record_frame_age(time.monotonic() - published_at)
                self._last_sampled_generation = generation
                self._enqueue_frame(frame)

        def run() -> None:
            try:
                sample()
            except Exception as exc:
                self._fail("HLS sampler thread failed", exc)

        self._sampler_thread = threading.Thread(target=run, name="hls-sampler", daemon=True)
        self._sampler_thread.start()

    def _start_writer_thread(self) -> None:
        """Drain the frame queue into ffmpeg as fast as it will accept."""

        def write() -> None:
            while not self._stopped.is_set():
                frame = self._queue.get(self._stopped)
                if frame is None:
                    continue

                # Snapshot the current instance under the lock, but write
                # OUTSIDE it: stdin.write blocks indefinitely when ffmpeg
                # wedges with a full pipe, and holding the lock across that
                # would deadlock stop()/relaunch (which need the lock to kill
                # ffmpeg — the only thing that unblocks the write).
                with self._ffmpeg_lock:
                    ffmpeg = self._ffmpeg
                    generation = self._ffmpeg_generation

                write_s: float | None = None
                write_error: BaseException | None = None
                stdin = ffmpeg.stdin if ffmpeg is not None else None
                if ffmpeg is not None and ffmpeg.poll() is None and stdin is not None:
                    write_started = time.monotonic()
                    with self._write_state_lock:
                        self._write_started_at = write_started
                        self._write_generation = generation
                        self._write_stall_recovery_started = False
                    try:
                        stdin.write(frame)
                        stdin.flush()
                        write_s = time.monotonic() - write_started
                    except (BrokenPipeError, OSError, ValueError) as exc:
                        # ValueError: stdin closed under us by a kill/relaunch.
                        write_error = exc
                        write_s = None
                    finally:
                        with self._write_state_lock:
                            if self._write_generation == generation:
                                self._write_started_at = None
                                self._write_generation = None
                                self._write_stall_recovery_started = False

                if write_s is None:
                    if self._stopped.is_set():
                        break
                    with self._ffmpeg_lock:
                        replaced = self._ffmpeg is not ffmpeg
                    if replaced:
                        # A relaunch (offset change, restart) swapped instances
                        # mid-write; the new ffmpeg is healthy — don't kill it.
                        continue
                    # ffmpeg died or the pipe broke.  Recovery is delayed and
                    # bounded so a persistent failure cannot consume an
                    # unbounded number of frames/processes in a tight loop.
                    if not self._recover_ffmpeg(ffmpeg, generation, write_error):
                        break
                    continue

                # A single accepted frame is not proof of recovery: some
                # broken encoders accept one pipe write and then immediately
                # exit. Close the circuit only after this generation has been
                # continuously healthy for the stability window.
                with self._ffmpeg_lock:
                    current_generation = self._ffmpeg_generation
                    started_at = self._ffmpeg_started_at
                if (
                    current_generation == generation
                    and started_at is not None
                    and time.monotonic() - started_at >= FFMPEG_RESTART_STABLE_S
                ):
                    self._consecutive_ffmpeg_failures = 0

                if self._test_write_delay_s:
                    time.sleep(self._test_write_delay_s)

                if self._stats is not None:
                    self._stats.trace("first frame written to ffmpeg stdin", once=True)
                    self._stats.record_encode_write(write_s)
                self._note_encode_backpressure(write_s, generation)

        def run() -> None:
            try:
                write()
            except Exception as exc:
                self._fail("HLS writer thread failed", exc)

        self._writer_thread = threading.Thread(target=run, name="hls-writer", daemon=True)
        self._writer_thread.start()
