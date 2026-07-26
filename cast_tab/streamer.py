"""HLS streaming orchestrator: paced sampling → ffmpeg encode → HTTP serving.

The pieces live in sibling modules — command construction and the ffmpeg
process in `encoder`, the frame-pacing primitives in `pacing`, the HTTP
server in `server`. This module owns the threads and the A/V-sync-critical
ordering between them.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from cast_tab.audio import DEFAULT_AUDIO_FORMAT, AudioFormat, _pipe_bytes_available
from cast_tab.encoder import (  # re-exported for callers (cli, tools)
    DEFAULT_JPEG_QUALITY,
    HARDWARE_VIDEO_ENCODER,
    SOFTWARE_VIDEO_ENCODER,
    FfmpegProcess,
    codec_label,
    default_fps_for_resolution,
    hls_args,
    hls_segment_duration_s,
    preferred_video_encoder,
    video_encoder_args,
)
from cast_tab.pacing import BoundedFrameQueue, LatestFrame
from cast_tab.server import (
    HLSDiscontinuitySequenceNormalizer,
    HLSHTTPServer,
    get_local_ip,
    get_receiver_route,
)
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
# Do not tolerate slow writes for a minute: the Python queue only holds about
# one second of CFR ticks.  Queue overflow is itself an immediate re-anchor
# trigger; this shorter window catches sustained pressure before that point.
FFMPEG_BACKPRESSURE_DURATION_S = 2.0

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
# live-but-wedged encoder.  This must be no longer than the frame queue's
# capacity in seconds: once a sampled frame is lost, continuing the same
# image2pipe frame-count timeline permanently desynchronizes it from raw PCM.
FFMPEG_WRITE_STALL_S = 1.0
HEALTH_POLL_S = 0.1
# Give a live receiver the conventional three target durations it expects
# before advertising the stream as ready. This avoids asking the Chromecast to
# bootstrap from a single segment without increasing steady-state holdback.
HLS_STARTUP_SEGMENTS = 3

# Timestamp history and the application queue each cover roughly a second. Catch
# up within that window using the actual historical captures; beyond it, create
# a clean joint A/V boundary instead of bursting repeated or pruned frames.
SAMPLER_MAX_CATCHUP_S = 1.0

# Clamp the manual --audio-offset-ms trim to a sane range; covers ordinary
# fixed capture/encode baseline offsets without masking a broken timeline.
MAX_AUTO_AV_OFFSET_S = 3.0
MAX_AUTO_AV_OFFSET_MS = int(MAX_AUTO_AV_OFFSET_S * 1000)

_HLS_SEGMENT_NAME_RE = re.compile(
    r"^seg-e(?P<epoch>[0-9]{6})-a(?P<attempt>[0-9]{6})-"
    r"(?P<sequence>[0-9]{9})\.ts$"
)


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
        if width <= 0 or height <= 0 or width % 2 or height % 2:
            raise ValueError(
                "HLS video dimensions must be positive even numbers for yuv420p"
            )
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
        # Test-only: sleep after each stdin write to simulate an encoder whose
        # throughput is too low, exercising queue overflow and guarded recovery.
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
        self._receiver_host: str | None = None
        self._receiver_client_hosts: tuple[str, ...] = ()
        self._playlist_url: str | None = None
        self._stopped = threading.Event()
        self._stats = stats
        # Signal handlers can re-enter stop() on the main thread during an
        # audio-source/ffmpeg transaction. Reentrancy avoids self-deadlock;
        # generation checks still protect ordinary cross-thread races.
        self._ffmpeg_lock = threading.RLock()
        self._stop_lock = threading.Lock()
        # A POSIX signal handler runs on the interrupted main thread and may
        # re-enter stop() while start() is inside a guarded resource factory.
        self._lifecycle_lock = threading.RLock()
        self._lifecycle_state = "new"
        self._fatal_lock = threading.Lock()
        self._fatal_error: RuntimeError | None = None
        # Encoder availability only proves ffmpeg was built with VideoToolbox;
        # the hardware session can still fail at runtime. Once that happens,
        # every later generation in this stream stays on libx264.
        self._video_encoder = preferred_video_encoder()
        self._consecutive_ffmpeg_failures = 0
        self._ffmpeg_generation = 0
        self._ffmpeg_started_at: float | None = None
        # (ffmpeg generation, earliest capture eligible to seed its video PTS
        # zero). The initial process may use the frame which triggered startup;
        # every replacement requires capture from its new raw-input boundary.
        self._ffmpeg_video_boundary: tuple[int, float | None] = (0, None)
        self._write_state_lock = threading.Lock()
        self._write_started_at: float | None = None
        self._write_generation: int | None = None
        self._write_stall_recovery_started = False
        self._backpressure_started_at: float | None = None
        self._backpressure_generation: int | None = None
        self._last_sampled_generation = -1
        self._known_hls_segments: set[str] = set()
        self._known_hls_playlist_segments: set[str] = set()
        self._last_hls_publish_mtime: float | None = None
        self._hls_delivery_segment_requests = 0
        self._hls_delivery_errors = 0
        self._hls_last_delivery_s: float | None = None
        self._hls_last_delivery_mbps: float | None = None
        self._hls_delivery_observed_at: float | None = None
        # Every ffmpeg spawn gets a unique URI namespace. ``timeline_epoch``
        # advances only after the prior attempt actually published media, so it
        # is also the exact HLS discontinuity sequence used by the HTTP
        # playlist normalizer. A failed empty attempt gets a new attempt id but
        # reuses the pending epoch.
        self._hls_timeline_epoch = 0
        self._hls_attempt = -1
        self._hls_current_attempt: int | None = None
        self._hls_ever_published = False
        self._hls_identity_lock = threading.Lock()
        self._hls_uri_sequences: dict[str, int] = {}
        self._hls_identity_mtime_ns = -1
        # Bounded at ~1s of frames.  The bound is an early-warning threshold,
        # not an A/V-offset cap: overflow breaks image2pipe's CFR timeline and
        # must force a new ffmpeg generation (see BoundedFrameQueue).
        self._queue = BoundedFrameQueue(maxlen=max(1, self.fps))
        # (ffmpeg generation, reason). Accessed only under _ffmpeg_lock.  A
        # generation tag prevents an old overflow/stall request from killing a
        # fresh process installed concurrently by a manual offset relaunch.
        self._pending_timeline_resync: tuple[int, str] | None = None
        self._audio_replacement_in_progress = False
        self._audio_replacement_ready = threading.Event()
        self._audio_replacement_ready.set()

    @property
    def playlist_url(self) -> str:
        playlist_url = self._playlist_url
        if playlist_url is None:
            raise RuntimeError("Chromecast destination has not been configured.")
        return playlist_url

    @property
    def accepts_deferred_audio(self) -> bool:
        """Whether ffmpeg exists to consume a newly proven PCM source promptly."""
        with self._lifecycle_lock:
            return self._lifecycle_state == "running" and not self._stopped.is_set()

    def configure_receiver(self, receiver_host: str) -> str:
        """Freeze the receiver-routed public URL used for this whole cast."""
        if not receiver_host:
            raise ValueError("receiver_host is required")
        existing = self._playlist_url
        if existing is not None:
            if receiver_host != self._receiver_host:
                raise RuntimeError("HLS receiver destination is already configured.")
            return existing
        http = self._http
        port = self._port or (http.port if http is not None else 0)
        if port <= 0:
            raise RuntimeError("HLS server is not running.")
        route = get_receiver_route(receiver_host)
        playlist_url = f"http://{route.local_ip}:{port}/stream.m3u8"
        self._receiver_host = receiver_host
        self._receiver_client_hosts = route.peer_hosts
        self._playlist_url = playlist_url
        return playlist_url

    def receiver_delivery_observation(
        self,
        client_host: str | None = None,
    ) -> tuple[int, bool] | None:
        """Return (successful segment count, currently fresh) for one receiver.

        A newly started segment request is temporarily healthy, but only a
        completed non-empty response advances the count used to validate LOAD.
        This prevents a browser probe or a stale receiver from proving that the
        selected Chromecast consumed the new media session.
        """
        http = self._http
        if http is None:
            return None
        client_hosts = self._receiver_client_hosts
        if not client_hosts:
            if not client_host:
                return None
            client_hosts = (client_host,)
        stale_after_s = float(hls_segment_duration_s(buffered=self.buffered) * 3)
        segment_responses = 0
        fresh = False
        for host in client_hosts:
            snapshot = http.client_delivery_snapshot(host)
            segment_responses += snapshot.segment_responses
            active_fresh = (
                snapshot.active_segment_requests > 0
                and snapshot.oldest_active_segment_age_s is not None
                and snapshot.oldest_active_segment_age_s <= stale_after_s
            )
            completed_fresh = (
                snapshot.latest_segment_completed_at is not None
                and max(0.0, time.time() - snapshot.latest_segment_completed_at)
                <= stale_after_s
            )
            fresh = fresh or active_fresh or completed_fresh
        return segment_responses, fresh

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
        self._check_hls_media_sequence_identity()
        # Queue overflow and sampler clock gaps are CFR timeline boundaries,
        # not ordinary encoder errors.  Re-anchor both raw inputs before any
        # post-gap frame is allowed into the old ffmpeg generation.
        self._process_pending_timeline_resync()
        self._recover_stalled_write_if_needed()
        # Timeline/wedge recovery can open the circuit synchronously.
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
            ffmpeg = self._ffmpeg if self._ffmpeg_generation == generation else None
        if ffmpeg is None:
            return
        stalled_for = time.monotonic() - started_at
        requested = self._request_timeline_resync(
            f"ffmpeg stdin write blocked for {stalled_for:.1f}s",
            expected_generation=generation,
        )
        if requested:
            self._process_pending_timeline_resync()

    def _request_timeline_resync_locked(
        self,
        reason: str,
        *,
        expected_generation: int | None = None,
        lost_frames: int = 0,
    ) -> bool:
        """Request a raw-input re-anchor while holding ``_ffmpeg_lock``.

        The generation tag is the key race invariant: a request caused by an
        old queue/write stall can never tear down a process installed by a
        concurrent manual relaunch.  Repeated requests for the same generation
        coalesce, while any additional known frame loss is still counted.
        """
        generation = self._ffmpeg_generation
        if (
            self._stopped.is_set()
            or self._audio_replacement_in_progress
            or self._ffmpeg is None
            or (expected_generation is not None and expected_generation != generation)
        ):
            return False

        pending = self._pending_timeline_resync
        if pending is not None and pending[0] == generation:
            if lost_frames and self._stats is not None:
                self._stats.record_timeline_loss(lost_frames)
            return False

        self._pending_timeline_resync = (generation, reason)
        if self._stats is not None:
            self._stats.record_encode_resync(reason, lost_frames=lost_frames)
            self._stats.trace(f"timeline re-anchor requested: {reason}")
        return True

    def _request_timeline_resync(
        self,
        reason: str,
        *,
        expected_generation: int | None = None,
        lost_frames: int = 0,
    ) -> bool:
        with self._ffmpeg_lock:
            return self._request_timeline_resync_locked(
                reason,
                expected_generation=expected_generation,
                lost_frames=lost_frames,
            )

    def _process_pending_timeline_resync_locked(self) -> bool:
        """Apply a pending re-anchor while holding ``_ffmpeg_lock``.

        Returns True when a request was consumed (including a terminal circuit
        open).  Killing the old process unblocks a writer stuck outside the
        lock; clearing video and draining audio in ``_start_ffmpeg`` then gives
        the replacement two fresh frame/sample-count timelines at PTS zero.
        """
        pending = self._pending_timeline_resync
        if pending is None:
            return False
        requested_generation, reason = pending
        if requested_generation != self._ffmpeg_generation:
            # Another relaunch already supplied the requested boundary.
            self._pending_timeline_resync = None
            return False

        self._pending_timeline_resync = None
        self._consecutive_ffmpeg_failures += 1
        failures = self._consecutive_ffmpeg_failures
        if failures >= FFMPEG_RESTART_MAX_FAILURES:
            if not self._fallback_to_software_encoder_locked(
                f"{failures} consecutive CFR timeline recoveries ({reason})"
            ):
                self._fail(
                    "ffmpeg encoder recovery stopped after "
                    f"{failures} consecutive CFR timeline recovery attempts "
                    f"({reason})"
                )
                self._kill_ffmpeg()
                return True

        self._kill_ffmpeg()
        discarded = self._queue.clear()
        if discarded and self._stats is not None:
            self._stats.record_timeline_loss(discarded)
        self._backpressure_started_at = None
        self._backpressure_generation = None
        if self._stopped.is_set():
            return True
        try:
            self._start_ffmpeg()
        except Exception as exc:
            self._fail("ffmpeg timeline re-anchor failed", exc)
            return True

        if self._stats is not None:
            self._stats.record_ffmpeg_restart()
        print(f"Re-anchored A/V after {reason}.", flush=True)
        return True

    def _process_pending_timeline_resync(self) -> bool:
        with self._ffmpeg_lock:
            return self._process_pending_timeline_resync_locked()

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

    # How long to wait for Chrome's screencast to deliver its first frame.
    # No valid HLS stream can be produced without video, and opening only the
    # real-time PCM input destroys the joint PTS-zero boundary.
    FIRST_FRAME_TIMEOUT_S = 30.0

    def _startup_step(self, action: Callable[[], None]) -> bool:
        """Run one resource-creation step atomically against stop()."""
        with self._lifecycle_lock:
            if self._lifecycle_state != "starting" or self._stopped.is_set():
                return False
            action()
            return self._lifecycle_state == "starting" and not self._stopped.is_set()

    def _wait_for_first_frame(
        self,
        health_check: Callable[[], None] | None,
    ) -> bool:
        deadline = time.monotonic() + self.FIRST_FRAME_TIMEOUT_S
        while True:
            # AudioTee begins producing as soon as it attaches, while the first
            # paint-driven Chrome frame can legitimately take many seconds.
            # Its native queue plus the OS pipe holds under a second of PCM;
            # discard uncommitted samples on every health tick so that queue
            # never overflows before ffmpeg exists. Draining both sides of the
            # health callback also follows a replacement audio fd immediately.
            self._drain_audio_fd(report=False)
            if health_check is not None:
                health_check()
            self._drain_audio_fd(report=False)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._first_frame.wait(min(HEALTH_POLL_S, remaining)):
                self._drain_audio_fd(report=False)
                if health_check is not None:
                    health_check()
                self._drain_audio_fd(report=False)
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
            raise TimeoutError(
                "Timed out after "
                f"{self.FIRST_FRAME_TIMEOUT_S:.0f}s waiting for the first captured "
                "video frame; refusing to start an unsynchronized A/V stream."
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
            self._http = HLSHTTPServer(
                self.work_dir,
                self._port,
                playlist_transform=HLSDiscontinuitySequenceNormalizer(),
            )
            self._http.start()
            self._hls_delivery_observed_at = time.time()

        if not self._startup_step(start_http):
            return
        with self._lifecycle_lock:
            if self._lifecycle_state == "starting" and not self._stopped.is_set():
                self._lifecycle_state = "running"

    def publish_frame(
        self,
        jpeg_data: bytes,
        captured_at: float | None = None,
    ) -> None:
        if not self._stopped.is_set():
            self._latest.publish(jpeg_data, captured_at=captured_at)
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
        self._stats.record_audio_backlog(backlog / self.audio_format.bytes_per_second * 1000)

    def poll_hls_stats(self) -> list[str]:
        if self._stats is None:
            return []

        segment_mtimes: dict[str, float] = {}
        for path in self.work_dir.glob("seg*.ts"):
            try:
                segment_mtimes[path.name] = path.stat().st_mtime
            except OSError:
                continue

        current = set(segment_mtimes)
        had_segments = bool(self._known_hls_segments)
        deleted = sorted(self._known_hls_segments - current)

        events: list[str] = []
        if deleted and had_segments:
            events.append(f"hls deleted {', '.join(deleted)}")
        self._known_hls_segments = current

        # Count actual playlist entries rather than deletion-grace files left on
        # disk by FFmpeg.  The old disk count made a 12-entry window appear as
        # thirteen segments and was easy to misread as receiver buffer depth.
        playlist_segments = set(self._known_hls_playlist_segments)
        playlist_read = False
        try:
            playlist_text = (self.work_dir / "stream.m3u8").read_text(encoding="utf-8")
            playlist_segments = {
                line.split("?", 1)[0]
                for line in playlist_text.splitlines()
                if line and not line.startswith("#") and line.split("?", 1)[0].endswith(".ts")
            }
            playlist_read = True
        except (OSError, UnicodeError):
            pass

        # Production cadence and age must follow media the receiver can
        # actually discover. A completed but not-yet-published (or orphaned)
        # .ts file must not make a stalled playlist look healthy.
        active_mtimes = {
            name: segment_mtimes[name]
            for name in playlist_segments
            if name in segment_mtimes
        }
        newest_age: float | None = None
        if active_mtimes:
            newest_age = max(0.0, time.time() - max(active_mtimes.values()))

        new_mtimes = sorted(
            active_mtimes[name]
            for name in (
                playlist_segments - self._known_hls_playlist_segments
                if playlist_read
                else set()
            )
            if name in active_mtimes
        )
        publish_intervals: list[float] = []
        for published_at in new_mtimes:
            previous = self._last_hls_publish_mtime
            if previous is not None and published_at >= previous:
                publish_intervals.append(published_at - previous)
            if previous is None or published_at > previous:
                self._last_hls_publish_mtime = published_at
        if playlist_read:
            self._known_hls_playlist_segments = playlist_segments

        segment_requests = 0
        delivery_errors = 0
        delivery_active = 0
        delivery_active_s: float | None = None
        delivery_idle_s: float | None = None
        delivery_seen = False
        http = self._http
        if http is not None and hasattr(http, "delivery_snapshot"):
            delivery = http.delivery_snapshot()
            segment_requests = max(
                0,
                delivery.segment_requests - self._hls_delivery_segment_requests,
            )
            delivery_errors = max(
                0,
                delivery.error_requests - self._hls_delivery_errors,
            )
            self._hls_delivery_segment_requests = delivery.segment_requests
            self._hls_delivery_errors = delivery.error_requests
            delivery_seen = (
                delivery.segment_responses > 0
                or delivery.active_segment_requests > 0
            )
            delivery_active = delivery.active_segment_requests
            delivery_active_s = delivery.oldest_active_segment_age_s

            now_wall = time.time()
            if delivery.latest_segment_completed_at is not None:
                delivery_idle_s = max(
                    0.0,
                    now_wall - delivery.latest_segment_completed_at,
                )
            elif self._hls_delivery_observed_at is not None:
                delivery_idle_s = max(
                    0.0,
                    now_wall - self._hls_delivery_observed_at,
                )

            if delivery.latest_segment_bytes > 0 and delivery.latest_segment_duration_s is not None:
                self._hls_last_delivery_s = delivery.latest_segment_duration_s
                throughput = delivery.latest_segment_throughput_bps
                self._hls_last_delivery_mbps = (
                    throughput * 8 / 1_000_000 if throughput is not None else None
                )
        self._stats.record_hls(
            segment_count=len(playlist_segments),
            newest_age_s=newest_age,
            segments_deleted=len(deleted) if had_segments else 0,
            target_duration_s=float(hls_segment_duration_s(buffered=self.buffered)),
            publish_intervals_s=tuple(publish_intervals),
            segment_requests=segment_requests,
            delivery_s=self._hls_last_delivery_s,
            delivery_mbps=self._hls_last_delivery_mbps,
            delivery_active=delivery_active,
            delivery_active_s=delivery_active_s,
            delivery_idle_s=delivery_idle_s,
            delivery_seen=delivery_seen,
            delivery_errors=delivery_errors,
        )
        return events

    def _check_hls_media_sequence_identity(self) -> None:
        """Ensure a published segment URI never changes media-sequence number."""
        playlist = self.work_dir / "stream.m3u8"
        try:
            modified_ns = playlist.stat().st_mtime_ns
        except OSError:
            return
        with self._hls_identity_lock:
            if modified_ns == self._hls_identity_mtime_ns:
                return
        try:
            lines = playlist.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return
        media_prefix = "#EXT-X-MEDIA-SEQUENCE:"
        media_values = [
            line[len(media_prefix) :] for line in lines if line.startswith(media_prefix)
        ]
        uris = [line for line in lines if line and not line.startswith("#")]
        if len(media_values) != 1 or not uris:
            return
        try:
            media_sequence = int(media_values[0])
        except ValueError:
            return

        with self._hls_identity_lock:
            current_sequences: dict[str, int] = {}
            for index, uri in enumerate(uris):
                if _HLS_SEGMENT_NAME_RE.fullmatch(uri) is None:
                    message = f"HLS published an unexpected segment URI {uri!r}"
                    self._fail(message)
                    raise self.fatal_error or RuntimeError(message)
                sequence = media_sequence + index
                previous = self._hls_uri_sequences.get(uri)
                if previous is not None and previous != sequence:
                    message = (
                        f"HLS media-sequence identity changed for {uri!r}: {previous} -> {sequence}"
                    )
                    self._fail(message)
                    raise self.fatal_error or RuntimeError(message)
                current_sequences[uri] = sequence
            self._hls_uri_sequences = current_sequences
            self._hls_identity_mtime_ns = modified_ns

    def wait_until_ready(
        self,
        timeout: float | None = None,
        *,
        health_check: Callable[[], None] | None = None,
    ) -> None:
        """Block until the live playlist has a stable three-segment runway."""
        if timeout is None:
            timeout = 60.0 if self.buffered else 30.0
        playlist = self.work_dir / "stream.m3u8"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if health_check is not None:
                health_check()
            self.raise_if_failed()
            try:
                playlist_lines = playlist.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                playlist_lines = []
            playlist_segments = [
                line for line in playlist_lines if line and not line.startswith("#")
            ]
            if len(playlist_segments) >= HLS_STARTUP_SEGMENTS and all(
                (self.work_dir / name).is_file() for name in playlist_segments
            ):
                self._hls_ever_published = True
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
                self._audio_replacement_ready.set()

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
                    if thread.is_alive():
                        raise TimeoutError("HLS sampler thread did not stop")

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
                    if writer.is_alive():
                        raise TimeoutError("HLS writer thread did not stop")

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

            if not failures:
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
        """Apply the user's fixed audio-ahead lip-sync trim.

        A source/device path can have a stable baseline offset even while its
        clocks remain locked. We prepend silence with ``adelay`` to move audio
        later by ``--audio-offset-ms`` without changing its long-run rate.

        Input-side -itsoffset is silently ignored for a raw-PCM pipe (ffmpeg
        regenerates the timestamps from 0), so the delay must live in the audio
        filter graph instead. adelay only adds delay, which is all we need: the
        the supported manual trim direction is audio-ahead.
        """
        if self.audio_fd is None or self.audio_offset_ms == 0:
            return []
        print(f"A/V sync: delaying audio {self.audio_offset_ms}ms (adelay).", flush=True)
        return ["-af", f"adelay={self.audio_offset_ms}:all=1"]

    def _drain_audio_fd(self, *, report: bool = True) -> int:
        """Discard PCM that buffered in the pipe before ffmpeg attaches.

        AudioTee streams into the pipe from the moment it starts, but ffmpeg
        only opens the fd when it (re)launches here. Whatever sat in the OS pipe
        buffer in the meantime (~64KB, ~170ms) would otherwise be read as the
        start of the stream and play ahead of video. Drop it so audio and video
        both effectively begin "now". Bounded so a live writer can't spin us.
        """
        if self.audio_fd is None:
            return 0
        # AudioTee intentionally retains ten seconds across a slow ffmpeg
        # teardown. Bound above that native runway so a concurrently flushing
        # writer cannot leave stale pre-boundary PCM for the next process.
        max_drop = self.audio_format.bytes_per_second * 12
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
            return dropped
        if dropped and report:
            ms = dropped / self.audio_format.bytes_per_second * 1000
            print(f"A/V sync: dropped {ms:.0f}ms of buffered pre-roll audio.", flush=True)
            if self._stats is not None:
                self._stats.trace(f"audio pre-roll drained ({ms:.0f}ms)")
        return dropped

    def _kill_ffmpeg(self, *, graceful: bool = False) -> None:
        ffmpeg = self._ffmpeg
        if ffmpeg is None:
            return

        if self.audio_fd is None:
            ffmpeg.kill(graceful=graceful)
        else:
            # Once a relaunch boundary is requested, samples consumed by the
            # old generation are no longer committed output. Drain AudioTee in
            # parallel with the bounded TERM/KILL wait so its real-time queue
            # never fills while ffmpeg's inherited reader is disappearing.
            # The final synchronous drain below removes the last uncommitted
            # bytes before a replacement process establishes fresh PTS zero.
            finished = threading.Event()
            failures: list[BaseException] = []

            def stop_process() -> None:
                try:
                    ffmpeg.kill(graceful=graceful)
                except BaseException as exc:
                    failures.append(exc)
                finally:
                    finished.set()

            self._drain_audio_fd(report=False)
            killer = threading.Thread(
                target=stop_process,
                name="ffmpeg-stop",
                daemon=True,
            )
            try:
                killer.start()
            except BaseException:
                # The ten-second native queue exceeds kill()'s full bounded
                # wait, so synchronous fallback remains safe. A helper-thread
                # failure is immaterial if process cleanup itself succeeds.
                ffmpeg.kill(graceful=graceful)
            else:
                while not finished.wait(0.01):
                    self._drain_audio_fd(report=False)
                killer.join()
                self._drain_audio_fd(report=False)
                if failures:
                    raise failures[0]

        # A failed kill above deliberately leaves ownership for stop()'s next
        # bounded pass. Only discard the handle after confirmed child reap.
        if self._ffmpeg is ffmpeg:
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
            relaunched = self._relaunch_ffmpeg(reset_failures=True)
            if relaunched and self._stats is not None:
                self._stats.record_encode_resync("manual audio-offset change")
                self._stats.record_ffmpeg_restart()
        return offset_ms

    def begin_audio_source_replacement(self) -> bool:
        """Quiesce ffmpeg before a replacement AudioTee starts producing.

        Starting the helper first is unsafe: killing a wedged ffmpeg can take
        seconds, while the native lossless PCM queue is intentionally bounded
        to less than a second. Quiescing first prevents the fresh capture from
        overflowing before its descriptor is committed.
        """
        with self._ffmpeg_lock:
            if self._stopped.is_set():
                return False
            if self._audio_replacement_in_progress:
                raise RuntimeError("Audio source replacement is already in progress.")
            self._audio_replacement_in_progress = True
            self._audio_replacement_ready.clear()
            try:
                self._kill_ffmpeg()
                if self._stopped.is_set():
                    self._audio_replacement_in_progress = False
                    self._audio_replacement_ready.set()
                    return False
                discarded = self._queue.clear()
                if discarded and self._stats is not None:
                    self._stats.record_timeline_loss(discarded)
                self._pending_timeline_resync = None
                self._backpressure_started_at = None
                self._backpressure_generation = None
            except BaseException:
                self._audio_replacement_in_progress = False
                self._audio_replacement_ready.set()
                raise
        return True

    def complete_audio_source_replacement(
        self,
        audio_fd: int | None,
        audio_format: AudioFormat | None = None,
    ) -> bool:
        """Commit a fresh PCM pipe and create a joint A/V boundary.

        AudioTee recovery must never reuse the old pipe: ffmpeg can retain
        demuxer buffers from that descriptor even after its producer stalls.
        begin_audio_source_replacement() has already removed the old encoder;
        this method swaps the descriptor and starts both raw timelines at zero.
        """
        if audio_fd is not None and audio_fd < 0:
            raise ValueError("audio_fd must be a valid descriptor or None")
        replacement_format = audio_format or DEFAULT_AUDIO_FORMAT

        with self._ffmpeg_lock:
            if not self._audio_replacement_in_progress:
                raise RuntimeError("Audio source replacement was not started.")
            if self._stopped.is_set():
                self._audio_replacement_in_progress = False
                self._audio_replacement_ready.set()
                return False

            self.audio_fd = audio_fd
            self.audio_format = replacement_format
            self._consecutive_ffmpeg_failures = 0

            # start() still owns the initial PTS-zero transaction. Spawning
            # here before its first captured frame would recreate the startup
            # skew this class deliberately avoids.
            if self._lifecycle_state in ("new", "starting"):
                self._audio_replacement_in_progress = False
                self._audio_replacement_ready.set()
                return True

            try:
                self._start_ffmpeg()
            except Exception as exc:
                self._audio_replacement_in_progress = False
                self._audio_replacement_ready.set()
                self._fail("ffmpeg failed while committing replacement audio", exc)
                raise
            self._audio_replacement_in_progress = False
            self._audio_replacement_ready.set()

        if self._stats is not None:
            self._stats.record_encode_resync("audio capture reattached")
            self._stats.record_ffmpeg_restart()
        print(
            "Re-anchored A/V with a fresh audio capture."
            if audio_fd is not None
            else "Re-anchored video with silence after audio capture failed.",
            flush=True,
        )
        return True

    def fail_audio_source_replacement(self, failure: BaseException) -> None:
        """Make an uncommitted replacement terminal instead of auto-reusing stale input."""
        with self._ffmpeg_lock:
            if not self._audio_replacement_in_progress:
                return
            self._audio_replacement_in_progress = False
            self._audio_replacement_ready.set()
            if not self._stopped.is_set():
                self._fail("Audio capture recovery failed", failure)

    def _relaunch_ffmpeg(
        self,
        *,
        expected_generation: int | None = None,
        reset_failures: bool = False,
    ) -> bool:
        with self._ffmpeg_lock:
            if self._audio_replacement_in_progress:
                return False
            if expected_generation is not None and self._ffmpeg_generation != expected_generation:
                return False
            self._kill_ffmpeg()
            # Drop the queued backlog: the sampler keeps producing during the
            # relaunch gap, and a fresh ffmpeg would otherwise inherit and
            # buffer seconds of stale video, inflating the A/V latency.
            discarded = self._queue.clear()
            if discarded and self._stats is not None:
                self._stats.record_timeline_loss(discarded)
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
            self._backpressure_generation = None
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
                self._stats.record_encode_resync(f"ffmpeg restart after {reason}")
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

    def _fallback_to_software_encoder_locked(self, reason: str) -> bool:
        """Give libx264 a fresh bounded recovery budget after VT fails."""
        if self._video_encoder != HARDWARE_VIDEO_ENCODER:
            return False
        self._video_encoder = SOFTWARE_VIDEO_ENCODER
        self._consecutive_ffmpeg_failures = 0
        print(
            f"VideoToolbox failed at runtime; falling back to libx264 ({reason}).",
            flush=True,
        )
        if self._stats is not None:
            self._stats.trace("encoder fallback VideoToolbox -> libx264")
        return True

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
            if self._ffmpeg_generation != failed_generation or self._ffmpeg is not failed_ffmpeg:
                # A user-driven relaunch replaced the failed instance between
                # the writer's snapshot and recovery.  Leave that fresh
                # process alone and let its first write determine its health.
                return True
        detail = self._ffmpeg_failure_detail(failed_ffmpeg, write_error)
        with self._ffmpeg_lock:
            if (
                self._ffmpeg_generation != failed_generation
                or self._ffmpeg is not failed_ffmpeg
            ):
                # stderr collection deliberately happens outside the lock and
                # may block briefly. A manual/audio relaunch that won during
                # that window owns the encoder choice for its fresh process;
                # never let this stale failure downgrade later generations.
                return True
            fell_back_to_software = self._fallback_to_software_encoder_locked(
                detail or "encoder process or input pipe failed"
            )
        if fell_back_to_software:
            # This launch is x264's first chance to run, not an x264 failure.
            # Give the software backend its complete independent circuit
            # budget, even if VideoToolbox had already consumed all of its own.
            try:
                restarted = self._restart_ffmpeg(
                    "VideoToolbox runtime failure",
                    expected_generation=failed_generation,
                )
            except Exception as exc:
                # A failed x264 spawn is its first real failure and enters the
                # ordinary bounded loop below.
                detail = str(exc) or type(exc).__name__
                failed_ffmpeg = None
            else:
                return restarted or not self._stopped.is_set()
        while not self._stopped.is_set():
            with self._ffmpeg_lock:
                if self._ffmpeg_generation != failed_generation:
                    return True
            self._consecutive_ffmpeg_failures += 1
            failures = self._consecutive_ffmpeg_failures
            if failures >= FFMPEG_RESTART_MAX_FAILURES:
                message = f"ffmpeg failed {failures} consecutive times; encoder recovery stopped"
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
        with self._ffmpeg_lock:
            # A write can return after another thread has already relaunched
            # ffmpeg.  Never let its stale timing arm/reset the fresh
            # generation's pressure state.
            if self._ffmpeg_generation != generation:
                return
            if write_s >= FFMPEG_BACKPRESSURE_WRITE_S:
                if self._backpressure_generation != generation:
                    self._backpressure_generation = generation
                    self._backpressure_started_at = now
                elif (
                    self._backpressure_started_at is not None
                    and now - self._backpressure_started_at >= FFMPEG_BACKPRESSURE_DURATION_S
                ):
                    self._request_timeline_resync_locked(
                        "sustained encoder backpressure",
                        expected_generation=generation,
                    )
            else:
                self._backpressure_started_at = None
                self._backpressure_generation = None

    def _read_hls_restart_state(self) -> tuple[list[tuple[int, int, str]], int]:
        """Validate the static raw playlist before an append-list relaunch.

        The caller has already killed the old ffmpeg while holding
        ``_ffmpeg_lock``, so the playlist cannot change underneath this read.
        Refusing a missing or malformed previously-published playlist is
        intentional: FFmpeg otherwise silently falls back to media sequence
        zero, which can overwrite segment identities and wedge a live receiver.

        Returns ``(segments, next_media_sequence)``. An empty result is valid
        only before any generation has published media.
        """
        playlist = self.work_dir / "stream.m3u8"
        if not playlist.exists():
            if self._hls_ever_published:
                raise RuntimeError("cannot safely restart HLS: the published playlist is missing")
            return [], 0

        try:
            text = playlist.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("cannot safely restart HLS: playlist is unreadable") from exc

        lines = text.splitlines()
        if not lines or lines[0] != "#EXTM3U":
            raise RuntimeError("cannot safely restart HLS: malformed playlist header")

        media_prefix = "#EXT-X-MEDIA-SEQUENCE:"
        media_values = [
            line[len(media_prefix) :] for line in lines if line.startswith(media_prefix)
        ]
        if len(media_values) != 1:
            raise RuntimeError("cannot safely restart HLS: expected one media sequence")
        try:
            media_sequence = int(media_values[0])
        except ValueError as exc:
            raise RuntimeError("cannot safely restart HLS: invalid media sequence") from exc
        if media_sequence < 0:
            raise RuntimeError("cannot safely restart HLS: negative media sequence")

        records: list[tuple[int, int, str]] = []
        boundaries: list[bool] = []
        pending_extinf = False
        pending_boundary = False
        for line in lines:
            if line == "#EXT-X-DISCONTINUITY":
                if pending_boundary:
                    raise RuntimeError("cannot safely restart HLS: duplicate discontinuity")
                pending_boundary = True
                continue
            if line.startswith("#EXTINF:"):
                if pending_extinf:
                    raise RuntimeError("cannot safely restart HLS: segment URI is missing")
                pending_extinf = True
                continue
            if not line or line.startswith("#"):
                continue
            if not pending_extinf:
                raise RuntimeError("cannot safely restart HLS: segment has no EXTINF")
            match = _HLS_SEGMENT_NAME_RE.fullmatch(line)
            if match is None:
                raise RuntimeError(f"cannot safely restart HLS: unexpected segment URI {line!r}")
            if not (self.work_dir / line).is_file():
                raise RuntimeError(
                    f"cannot safely restart HLS: referenced segment {line!r} is missing"
                )
            records.append(
                (
                    int(match.group("epoch")),
                    int(match.group("attempt")),
                    line,
                )
            )
            boundaries.append(pending_boundary)
            pending_extinf = False
            pending_boundary = False

        if pending_extinf:
            raise RuntimeError("cannot safely restart HLS: final segment URI is missing")
        if not records:
            if self._hls_ever_published:
                raise RuntimeError("cannot safely restart HLS: published playlist has no segments")
            return [], 0
        if len({record[2] for record in records}) != len(records):
            raise RuntimeError("cannot safely restart HLS: duplicate segment URI")

        previous_epoch, previous_attempt, _name = records[0]
        for index, (epoch, attempt, _name) in enumerate(records[1:], start=1):
            boundary = boundaries[index]
            if epoch == previous_epoch:
                if boundary or attempt != previous_attempt:
                    raise RuntimeError("cannot safely restart HLS: inconsistent attempt boundary")
            elif epoch == previous_epoch + 1:
                if not boundary:
                    raise RuntimeError("cannot safely restart HLS: timeline boundary is missing")
            else:
                raise RuntimeError("cannot safely restart HLS: timeline epochs are not consecutive")
            previous_epoch, previous_attempt = epoch, attempt

        with self._hls_identity_lock:
            current_sequences = {
                name: media_sequence + index
                for index, (_epoch, _attempt, name) in enumerate(records)
            }
            for name, sequence in current_sequences.items():
                previous = self._hls_uri_sequences.get(name)
                if previous is not None and previous != sequence:
                    raise RuntimeError(
                        "cannot safely restart HLS: media-sequence identity "
                        f"changed for {name!r}: {previous} -> {sequence}"
                    )
            self._hls_uri_sequences = current_sequences

        self._hls_ever_published = True
        return records, media_sequence + len(records)

    def _prepare_hls_output(self) -> tuple[str, bool]:
        """Allocate a never-reused segment namespace for the next spawn."""
        records, _next_media_sequence = self._read_hls_restart_state()
        append = bool(records)
        current_attempt_published = self._hls_current_attempt is not None and any(
            attempt == self._hls_current_attempt for _epoch, attempt, _name in records
        )
        if current_attempt_published:
            self._hls_timeline_epoch += 1

        self._hls_attempt += 1
        self._hls_current_attempt = self._hls_attempt
        segment_pattern = str(
            self.work_dir
            / (f"seg-e{self._hls_timeline_epoch:06d}-a{self._hls_attempt:06d}-%09d.ts")
        )
        return segment_pattern, append

    def _start_ffmpeg(self) -> None:
        playlist = self.work_dir / "stream.m3u8"
        segment_pattern, append_hls = self._prepare_hls_output()

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
            # Bound that hidden queue to one second: the previous 1024 packets
            # could conceal roughly 34 seconds of transcode lag behind fast app
            # writes, making the dashboard green while HLS fell behind.
            "-thread_queue_size",
            str(max(8, self.fps)),
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
            # gives ffmpeg enough throughput to preserve every CFR tick during
            # normal operation, keeping the video and PCM timelines equal.
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
                encoder=self._video_encoder,
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
            *hls_args(buffered=self.buffered, append=append_hls),
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
        self._ffmpeg_video_boundary = (
            self._ffmpeg_generation,
            None if self._ffmpeg_generation == 1 else self._ffmpeg_started_at,
        )
        video_boundary_at = self._ffmpeg_video_boundary[1]
        if video_boundary_at is not None:
            # A static page may not emit another screencast frame. Represent
            # its current visual state at the fresh PTS-zero boundary while the
            # sampler's cutoff still rejects delayed captures from before it.
            self._latest.reanchor_latest(video_boundary_at)
        if self._stats is not None:
            self._stats.trace("ffmpeg spawned")

    def _enqueue_frame(
        self,
        frame: bytes,
        *,
        sampled_for_generation: int | None = None,
    ) -> bool:
        # Serialize the drop decision with writer generation snapshots.  Once
        # put() reports a lost CFR tick, no writer can obtain the old ffmpeg
        # under this lock without first observing the pending re-anchor.
        with self._ffmpeg_lock:
            if self._audio_replacement_in_progress:
                # Deliberately do not build a hidden video backlog while no
                # matching audio producer exists. The latest-frame holder will
                # seed the fresh joint boundary when replacement commits.
                return False
            if (
                sampled_for_generation is not None
                and sampled_for_generation != self._ffmpeg_generation
            ):
                # The sampler may have been holding a local frame while a
                # relaunch killed a wedged process. Never let that pre-boundary
                # sample become the first frame of the fresh generation.
                if self._stats is not None:
                    self._stats.record_timeline_loss(1)
                return False
            depth, dropped = self._queue.put(frame)
            if self._stats is not None:
                self._stats.record_queue(depth=depth, dropped=dropped)
            if dropped:
                self._request_timeline_resync_locked(
                    "video queue overflow dropped a sampled CFR frame",
                    expected_generation=self._ffmpeg_generation,
                )
            return True

    def _handle_sampler_clock_gap(self, lag_s: float, frame_period: float) -> bool:
        """Request a boundary when catch-up would skip CFR ticks."""
        if lag_s <= SAMPLER_MAX_CATCHUP_S:
            return False
        skipped_ticks = max(1, int(lag_s / frame_period))
        self._request_timeline_resync(
            f"sampler skipped {skipped_ticks} elapsed CFR ticks",
            lost_frames=skipped_ticks,
        )
        return True

    def _start_sampler_thread(self) -> None:
        """Sample the latest frame at an exactly even cadence and enqueue it.

        Keeping this loop free of the (variable-latency) ffmpeg write is what
        eliminates motion judder: every output frame represents an evenly
        spaced moment in real time, regardless of write stalls downstream.
        """

        def sample() -> None:
            frame_period = 1.0 / self.fps
            next_tick = time.monotonic()
            sampled_ffmpeg_generation: int | None = None
            previous_capture_at: float | None = None

            while not self._stopped.is_set():
                now = time.monotonic()
                sleep_for = next_tick - now
                if sleep_for > 0:
                    self._stopped.wait(sleep_for)
                    now = time.monotonic()
                with self._ffmpeg_lock:
                    current_ffmpeg_generation = self._ffmpeg_generation
                    boundary_generation, video_boundary_at = self._ffmpeg_video_boundary
                # Recovery can hold _ffmpeg_lock while terminating a wedged
                # process. Refresh the cadence clock after that wait: using
                # the pre-lock timestamp would make a newly observed generation
                # appear seconds late on the next loop and trigger a redundant
                # second re-anchor for the same stall.
                now = time.monotonic()
                if boundary_generation != current_ffmpeg_generation:
                    # Tests and failed partial starts can install a process
                    # without the production boundary transaction. Never apply
                    # another generation's cutoff to this one.
                    video_boundary_at = None
                generation_changed = (
                    sampled_ffmpeg_generation is not None
                    and current_ffmpeg_generation != sampled_ffmpeg_generation
                )
                if generation_changed:
                    # Do this before selecting: an old scheduled tick can name
                    # a perfectly valid historical frame which must nevertheless
                    # never seed the replacement's video PTS-zero timeline.
                    next_tick = now
                    self._last_sampled_generation = -1
                    previous_capture_at = None
                # When we fall behind schedule, keep the loop running back-to-
                # back (no sleep) so it feeds one frame per missed tick — those
                # catch-up frames hold the encoded timeline level with wall-clock
                # so audio can't drift ahead of video. Only give up and resync
                # past a large gap (machine slept), where bursting the whole
                # backlog isn't worth it. The bounded queue caps the burst.
                if self._handle_sampler_clock_gap(now - next_tick, frame_period):
                    next_tick = now
                scheduled_tick = next_tick
                tick_late_s = max(0.0, now - scheduled_tick)
                next_tick = scheduled_tick + frame_period

                previous_generation = (
                    self._last_sampled_generation if self._last_sampled_generation >= 0 else None
                )
                selection = self._latest.select(
                    scheduled_tick,
                    previous_generation=previous_generation,
                    not_before=video_boundary_at,
                )
                if selection is None:
                    sampled_ffmpeg_generation = current_ffmpeg_generation
                    continue
                if self._stats is not None:
                    self._stats.trace("first sampler tick (video PTS=0 frame)", once=True)

                # Select by the tick's original time, not "whatever is latest"
                # after a late wake. The short history lets catch-up ticks use
                # the distinct captures that actually belonged to them instead
                # of duplicating one current frame and visibly jumping forward.
                _latest_frame, _latest_published_at, newest_generation = self._latest.peek()
                enqueued = self._enqueue_frame(
                    selection.frame,
                    sampled_for_generation=current_ffmpeg_generation,
                )
                if enqueued:
                    if self._stats is not None:
                        if selection.repeated:
                            self._stats.record_encode_repeat()
                        self._stats.record_frame_age(
                            max(0.0, time.monotonic() - selection.published_at)
                        )
                        source_interval = (
                            None
                            if previous_capture_at is None
                            else selection.captured_at - previous_capture_at
                        )
                        self._stats.record_sampler_cadence(
                            source_interval_s=source_interval,
                            tick_late_s=tick_late_s,
                            recovered_from_history=(
                                not selection.used_future_fallback
                                and selection.generation != newest_generation
                            ),
                        )
                    self._last_sampled_generation = selection.generation
                    previous_capture_at = selection.captured_at
                if not enqueued:
                    # A relaunch already supplied the required timeline
                    # boundary. Reset the sampler cadence to the fresh
                    # generation instead of bursting missed old-generation
                    # ticks into it and causing a redundant overflow/restart.
                    next_tick = time.monotonic() + frame_period
                sampled_ffmpeg_generation = current_ffmpeg_generation

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
                # Process a boundary before taking another queue item, and
                # remember which generation that item belongs to.  queue.get()
                # deliberately happens without _ffmpeg_lock, so the second
                # check below rejects a frame popped concurrently with a
                # relaunch instead of seeding the new generation with stale
                # pre-boundary video.
                with self._ffmpeg_lock:
                    audio_replacement = self._audio_replacement_in_progress
                    if (
                        not audio_replacement
                        and self._process_pending_timeline_resync_locked()
                    ):
                        continue
                    queued_for_generation = self._ffmpeg_generation

                if audio_replacement:
                    self._audio_replacement_ready.wait(HEALTH_POLL_S)
                    continue

                frame = self._queue.get(self._stopped)
                if frame is None:
                    continue

                # Snapshot the current instance under the lock, but write
                # OUTSIDE it: stdin.write blocks indefinitely when ffmpeg
                # wedges with a full pipe, and holding the lock across that
                # would deadlock stop()/relaunch (which need the lock to kill
                # ffmpeg — the only thing that unblocks the write).
                with self._ffmpeg_lock:
                    reanchored = self._process_pending_timeline_resync_locked()
                    generation = self._ffmpeg_generation
                    stale_frame = (
                        reanchored
                        or self._audio_replacement_in_progress
                        or generation != queued_for_generation
                    )
                    ffmpeg = None if stale_frame else self._ffmpeg

                if stale_frame:
                    continue

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
                        written = stdin.write(frame)
                        if written != len(frame):
                            raise OSError(f"short ffmpeg stdin write: {written}/{len(frame)} bytes")
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
                        reanchored = self._process_pending_timeline_resync_locked()
                        replaced = self._ffmpeg is not ffmpeg
                    if reanchored or replaced:
                        # A relaunch (offset change, restart) swapped instances
                        # mid-write; the new ffmpeg is healthy — don't kill it.
                        continue
                    # ffmpeg died or the pipe broke.  Recovery is delayed and
                    # bounded so a persistent failure cannot consume an
                    # unbounded number of frames/processes in a tight loop.
                    if not self._recover_ffmpeg(ffmpeg, generation, write_error):
                        break
                    continue

                # A queue overflow may have been requested while this write was
                # blocked.  Apply it immediately on return, before another
                # queue item can enter the shortened old CFR timeline.
                with self._ffmpeg_lock:
                    reanchored = self._process_pending_timeline_resync_locked()
                    started_at = self._ffmpeg_started_at
                    # A single accepted frame is not proof of recovery: some
                    # broken encoders accept one pipe write and immediately
                    # exit.  The generation check and counter mutation must be
                    # atomic so an old writer cannot bless a fresh process.
                    if (
                        not reanchored
                        and self._ffmpeg_generation == generation
                        and started_at is not None
                        and time.monotonic() - started_at >= FFMPEG_RESTART_STABLE_S
                    ):
                        self._consecutive_ffmpeg_failures = 0

                if self._stats is not None:
                    self._stats.trace("first frame written to ffmpeg stdin", once=True)
                    self._stats.record_encode_write(write_s)
                if reanchored:
                    continue
                self._note_encode_backpressure(write_s, generation)

                if self._test_write_delay_s:
                    time.sleep(self._test_write_delay_s)

        def run() -> None:
            try:
                write()
            except Exception as exc:
                self._fail("HLS writer thread failed", exc)

        self._writer_thread = threading.Thread(target=run, name="hls-writer", daemon=True)
        self._writer_thread.start()
