"""HLS streaming server and ffmpeg encoder for tab screencast frames."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cast_tab.audio import DEFAULT_AUDIO_FORMAT, AudioFormat, _pipe_bytes_available
from cast_tab.stats import PipelineStats

FFMPEG_BACKPRESSURE_WRITE_S = 0.050
FFMPEG_BACKPRESSURE_DURATION_S = 60.0

# Keep filling video frames to catch up after a stall this long or shorter (so
# the encoded timeline stays locked to wall-clock and audio can't drift ahead);
# only abandon catch-up past it (machine slept, multi-second hang).
SAMPLER_MAX_CATCHUP_S = 5.0

# Clamp the manual --audio-offset-ms trim to a sane range; covers the audio
# pre-roll plus the video frame-queue latency we compensate for.
MAX_AUTO_AV_OFFSET_S = 3.0


def _ffmpeg_supports_encoder(encoder: str) -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return encoder in result.stdout


def default_fps_for_resolution(width: int, height: int, *, buffered: bool = False) -> int:
    if buffered:
        return 30
    if width * height >= 1920 * 1080:
        return 23
    return 24


def codec_label() -> str:
    return "H.264 (VideoToolbox)" if _ffmpeg_supports_encoder("h264_videotoolbox") else "H.264"


def default_jpeg_quality(width: int, height: int) -> int:
    # 92 keeps the capture crisp so the (mostly local-CPU) JPEG stage isn't the
    # quality bottleneck when there's H.264 bitrate to carry the detail.
    return 92


def _target_bitrate(
    width: int,
    height: int,
    *,
    buffered: bool,
    override_mbps: float | None = None,
) -> tuple[str, str, str]:
    """Pick H.264 bitrate targets (bitrate, maxrate, bufsize).

    override_mbps forces the average bitrate (in Mbps) and derives maxrate /
    bufsize from it using the same ratios as the resolution presets — a roomy
    VBV (2.4x) when buffered, a tight one (1.1x) when not. Used to sweep
    bitrate against a Chromecast's network headroom.
    """
    if override_mbps is not None:
        v = override_mbps
        if buffered:
            return (f"{v:g}M", f"{v * 1.2:g}M", f"{v * 2.4:g}M")
        return (f"{v:g}M", f"{v * 1.1:g}M", f"{v * 1.1:g}M")
    pixels = width * height
    if pixels >= 1920 * 1080:
        return ("15M", "18M", "36M") if buffered else ("15M", "16.5M", "16.5M")
    if pixels >= 1280 * 720:
        return ("3M", "3.5M", "8M") if buffered else ("2.5M", "3M", "3M")
    return ("1.5M", "2M", "4M") if buffered else ("1.5M", "2M", "2M")


def _video_encoder_args(
    fps: int,
    width: int,
    height: int,
    *,
    buffered: bool,
    bitrate_mbps: float | None = None,
) -> list[str]:
    bitrate, maxrate, bufsize = _target_bitrate(
        width, height, buffered=buffered, override_mbps=bitrate_mbps
    )
    gop = fps * (2 if buffered else 1)

    if _ffmpeg_supports_encoder("h264_videotoolbox"):
        return [
            "-c:v",
            "h264_videotoolbox",
            "-profile:v",
            "main",
            "-b:v",
            bitrate,
            "-maxrate",
            maxrate,
            "-bufsize",
            bufsize,
            "-g",
            str(gop),
            "-keyint_min",
            str(fps),
        ]

    return [
        "-c:v",
        "libx264",
        "-profile:v",
        "main",
        "-level",
        "3.1",
        "-preset",
        "medium" if buffered else "veryfast",
        "-tune",
        "film" if buffered else "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        bitrate,
        "-maxrate",
        maxrate,
        "-bufsize",
        bufsize,
        "-g",
        str(gop),
        "-keyint_min",
        str(fps),
        "-sc_threshold",
        "0",
        # No B-frames: avoid frame-reorder latency (video lagging audio).
        "-bf",
        "0",
    ]


def _hls_args(*, buffered: bool) -> list[str]:
    if buffered:
        return [
            "-f",
            "hls",
            "-hls_time",
            "4",
            "-hls_list_size",
            "12",
            "-hls_flags",
            "delete_segments+append_list+omit_endlist+independent_segments",
            "-hls_segment_type",
            "mpegts",
        ]
    return [
        "-f",
        "hls",
        "-hls_time",
        "1",
        "-hls_list_size",
        "4",
        "-hls_flags",
        "delete_segments+append_list+omit_endlist+independent_segments",
        "-hls_segment_type",
        "mpegts",
    ]


class LatestFrame:
    """Thread-safe holder for the most recent captured frame."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: bytes | None = None
        self._published_at: float | None = None
        self._generation = 0

    def publish(self, frame: bytes) -> None:
        with self._lock:
            self._frame = frame
            self._published_at = time.monotonic()
            self._generation += 1

    def peek(self) -> tuple[bytes | None, float | None, int]:
        with self._lock:
            return self._frame, self._published_at, self._generation


def get_local_ip() -> str:
    """Return the LAN IP address used for outbound traffic."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]


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
        self.audio_offset_ms = audio_offset_ms
        self.video_bitrate_mbps = video_bitrate_mbps
        self.work_dir = work_dir or Path("/tmp/cast-tab-stream")
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
        self._ffmpeg: subprocess.Popen[bytes] | None = None
        self._sampler_thread: threading.Thread | None = None
        self._writer_thread: threading.Thread | None = None
        self._http_server: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._port = port
        self._stopped = threading.Event()
        self._stats = stats
        self._ffmpeg_lock = threading.Lock()
        self._backpressure_started_at: float | None = None
        self._last_sampled_generation = -1
        self._known_hls_segments: set[str] = set()
        # Frames sampled at an even cadence wait here for the writer thread to
        # push them into ffmpeg. Decoupling the two keeps sampling perfectly
        # paced even when an ffmpeg write stalls (HLS segment flush, keyframe),
        # which is what otherwise distorts motion into judder.
        #
        # Depth matters for A/V sync: when the queue overflows it drops the
        # oldest frame, and under frame-count-based PTS every dropped frame
        # pulls video permanently behind audio (the long-run "audio leads"
        # drift). In normal operation the queue sits at depth ~1 (ffmpeg drains
        # immediately, see the analyzeduration note in _start_ffmpeg); the depth
        # only matters during an occasional multi-second encoder stall. Sizing
        # it to ~8s lets those stalls be absorbed without dropping — the encoder
        # then drains the backlog (capture is rate-capped at fps, so it can't
        # outrun it) and sync is preserved. Verified offline: a 3s stall fills
        # the queue to ~84 then drains back with 0 drops and 0 residual skew,
        # while a stall past the bound drops frames and disturbs sync — so the
        # depth directly sets how long a stall we tolerate before audio leads.
        # In normal operation depth sits at ~1, so the deeper bound costs memory
        # only transiently during an actual stall, not steady-state latency. The
        # bound still caps growth for a pathological stall; drops show in --stats.
        self._frame_queue: deque[bytes] = deque()
        self._queue_cond = threading.Condition()
        self._queue_maxlen = max(1, self.fps * 8)

    @property
    def playlist_url(self) -> str:
        host = get_local_ip()
        port = self._port or (self._http_server.server_port if self._http_server else 0)
        return f"http://{host}:{port}/stream.m3u8"

    # How long to wait for Chrome's screencast to deliver its first frame
    # before spawning ffmpeg anyway. Chrome's screencast can take several
    # seconds to warm up; we'd rather wait than anchor audio without video.
    FIRST_FRAME_TIMEOUT_S = 30.0

    def start(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg is required but was not found in PATH.")

        # Hold ffmpeg until the first frame is captured. ffmpeg stamps both the
        # audio pipe and the video pipe from their first byte; audio is flowing
        # the instant ffmpeg opens it, but Chrome's screencast warms up several
        # seconds later. Spawning early anchors audio PTS=0 to "now" and video
        # PTS=0 to "now + warmup", baking that whole gap in as audio-ahead skew.
        # Waiting for the first frame anchors both inputs to the same moment.
        if not self._first_frame.wait(timeout=self.FIRST_FRAME_TIMEOUT_S):
            print(
                "Warning: no captured frame after "
                f"{self.FIRST_FRAME_TIMEOUT_S:.0f}s; starting ffmpeg anyway "
                "(audio may lead video).",
                flush=True,
            )
        if self._stopped.is_set():
            # Shut down before the first frame arrived; don't spawn anything.
            return

        self._start_ffmpeg()
        self._start_sampler_thread()
        self._start_writer_thread()
        self._start_http_server()

    def publish_frame(self, jpeg_data: bytes) -> None:
        if not self._stopped.is_set():
            self._latest.publish(jpeg_data)
            self._first_frame.set()
            if self._stats is not None:
                self._stats.trace("first frame published to streamer", once=True)
                self._stats.record_publish()

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

    def wait_until_ready(self, timeout: float | None = None) -> None:
        if timeout is None:
            timeout = 60.0 if self.buffered else 30.0
        """Block until the HLS playlist and first segment exist."""
        playlist = self.work_dir / "stream.m3u8"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if playlist.exists() and list(self.work_dir.glob("seg*.ts")):
                return
            if self._ffmpeg and self._ffmpeg.poll() is not None:
                stderr = ""
                if self._ffmpeg.stderr:
                    stderr = self._ffmpeg.stderr.read().decode(errors="replace")
                raise RuntimeError(f"ffmpeg exited early: {stderr.strip()}")
            time.sleep(0.5)
        raise TimeoutError("Timed out waiting for the HLS stream to become ready.")

    def stop(self) -> None:
        self._stopped.set()
        # Unblock start() if it's still waiting for the first frame.
        self._first_frame.set()
        with self._queue_cond:
            self._queue_cond.notify_all()

        for thread in (self._sampler_thread, self._writer_thread):
            if thread and thread.is_alive():
                thread.join(timeout=5)

        with self._ffmpeg_lock:
            self._kill_ffmpeg()

        if self._http_server:
            self._http_server.shutdown()
        if self._http_thread and self._http_thread.is_alive():
            self._http_thread.join(timeout=3)

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
                # never misreads the bytes (wrong format == white noise) and
                # nothing resamples.
                "-f",
                self.audio_format.ffmpeg_format,
                "-ar",
                str(self.audio_format.sample_rate),
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
        if self.audio_offset_ms < 0:
            print(
                "A/V sync: negative --audio-offset-ms is not supported "
                "(audio is structurally ahead, never behind); ignoring.",
                flush=True,
            )
            return []
        delay_ms = min(self.audio_offset_ms, int(MAX_AUTO_AV_OFFSET_S * 1000))
        print(f"A/V sync: delaying audio {delay_ms}ms (adelay).", flush=True)
        return ["-af", f"adelay={delay_ms}:all=1"]

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

    def _kill_ffmpeg(self) -> None:
        if self._ffmpeg is None:
            return
        if self._ffmpeg.stdin:
            try:
                self._ffmpeg.stdin.close()
            except OSError:
                pass
        if self._ffmpeg.poll() is None:
            try:
                self._ffmpeg.terminate()
                self._ffmpeg.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._ffmpeg.kill()
                self._ffmpeg.wait(timeout=3)
        self._ffmpeg = None

    def set_audio_offset_ms(self, offset_ms: int) -> int:
        """Change the A/V audio delay live and apply it.

        The delay is an adelay filter baked into the ffmpeg command, so applying
        a new value means relaunching ffmpeg (a brief glitch + buffer refill).
        Returns the value actually applied (unchanged → no relaunch). Negative
        values are clamped to 0 (audio is only ever ahead, never behind).
        """
        offset_ms = max(0, int(offset_ms))
        if offset_ms == self.audio_offset_ms:
            return offset_ms
        self.audio_offset_ms = offset_ms
        if not self._stopped.is_set():
            self._relaunch_ffmpeg()
        return offset_ms

    def _relaunch_ffmpeg(self) -> None:
        with self._ffmpeg_lock:
            self._kill_ffmpeg()
            # Drop the queued backlog: the sampler keeps producing during the
            # relaunch gap, and a fresh ffmpeg would otherwise inherit and
            # buffer seconds of stale video, inflating the A/V latency.
            with self._queue_cond:
                self._frame_queue.clear()
            self._start_ffmpeg()
        self._backpressure_started_at = None

    def _restart_ffmpeg(self) -> None:
        print("Restarting ffmpeg after sustained encoder backpressure...", flush=True)
        if self._stats is not None:
            self._stats.record_ffmpeg_restart()
        self._relaunch_ffmpeg()

    def _note_encode_backpressure(self, write_s: float) -> None:
        now = time.monotonic()
        if write_s >= FFMPEG_BACKPRESSURE_WRITE_S:
            if self._backpressure_started_at is None:
                self._backpressure_started_at = now
            elif now - self._backpressure_started_at >= FFMPEG_BACKPRESSURE_DURATION_S:
                self._restart_ffmpeg()
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
            *_video_encoder_args(
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
            *_hls_args(buffered=self.buffered),
            "-hls_segment_filename",
            segment_pattern,
            "-max_muxing_queue_size",
            "1024",
            str(playlist),
        ]

        popen_kwargs: dict = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
        }
        if self.audio_fd is not None:
            # pass_fds keeps only this fd open in the child (close_fds stays
            # True by default); never inherit the rest of our fds, or ffmpeg
            # holds pipe write-ends open and never sees EOF on shutdown.
            popen_kwargs["pass_fds"] = (self.audio_fd,)
            # Drain the pipe right before the child opens it so ffmpeg starts
            # reading at "now" instead of inheriting buffered pre-roll audio.
            self._drain_audio_fd()
        if self._stats is not None:
            self._stats.trace("ffmpeg spawn (audio+video PTS=0 anchor)")
        self._ffmpeg = subprocess.Popen(cmd, **popen_kwargs)
        if self._stats is not None:
            self._stats.trace("ffmpeg spawned")

    def _enqueue_frame(self, frame: bytes) -> None:
        with self._queue_cond:
            dropped = 0
            if len(self._frame_queue) >= self._queue_maxlen:
                # ffmpeg is sustainably behind; drop the oldest frame so latency
                # cannot grow without bound. Even sampling is preserved.
                self._frame_queue.popleft()
                dropped = 1
            self._frame_queue.append(frame)
            depth = len(self._frame_queue)
            self._queue_cond.notify()
        if self._stats is not None:
            self._stats.record_queue(depth=depth, dropped=dropped)

    def _start_sampler_thread(self) -> None:
        """Sample the latest frame at an exactly even cadence and enqueue it.

        Keeping this loop free of the (variable-latency) ffmpeg write is what
        eliminates motion judder: every output frame represents an evenly
        spaced moment in real time, regardless of write stalls downstream.
        """

        def run() -> None:
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

        self._sampler_thread = threading.Thread(target=run, name="hls-sampler", daemon=True)
        self._sampler_thread.start()

    def _start_writer_thread(self) -> None:
        """Drain the frame queue into ffmpeg as fast as it will accept."""

        def run() -> None:
            while not self._stopped.is_set():
                with self._queue_cond:
                    while not self._frame_queue and not self._stopped.is_set():
                        self._queue_cond.wait(timeout=0.5)
                    if not self._frame_queue:
                        continue
                    frame = self._frame_queue.popleft()

                write_s: float | None = None
                with self._ffmpeg_lock:
                    ffmpeg = self._ffmpeg
                    stdin = ffmpeg.stdin if ffmpeg is not None else None
                    if ffmpeg is not None and ffmpeg.poll() is None and stdin is not None:
                        try:
                            write_started = time.monotonic()
                            stdin.write(frame)
                            stdin.flush()
                            write_s = time.monotonic() - write_started
                        except (BrokenPipeError, OSError):
                            write_s = None

                if write_s is None:
                    if self._stopped.is_set():
                        break
                    # ffmpeg died or the pipe broke; respawn it (outside the
                    # lock) and keep streaming from the next queued frame.
                    self._restart_ffmpeg()
                    continue

                if self._stats is not None:
                    self._stats.trace("first frame written to ffmpeg stdin", once=True)
                    self._stats.record_encode_write(write_s)
                self._note_encode_backpressure(write_s)

        self._writer_thread = threading.Thread(target=run, name="hls-writer", daemon=True)
        self._writer_thread.start()

    def _start_http_server(self) -> None:
        serve_dir = str(self.work_dir)

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=serve_dir, **kwargs)

            def log_message(self, _format: str, *_args) -> None:
                pass

            def end_headers(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                super().end_headers()

        self._http_server = ThreadingHTTPServer(("0.0.0.0", self._port), Handler)
        self._http_thread = threading.Thread(
            target=self._http_server.serve_forever,
            name="hls-http",
            daemon=True,
        )
        self._http_thread.start()