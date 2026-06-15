"""HLS streaming server and ffmpeg encoder for tab screencast frames."""

from __future__ import annotations

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

# Never trust a measurement beyond this; a sane capture pre-roll is well under.
MAX_AUTO_AV_OFFSET_S = 1.5


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


def resolve_codec(requested: str) -> str:
    """Pick a Chromecast-compatible video codec."""
    if requested == "auto":
        # H.264 is universally supported on Chromecast; HEVC/AV1 need newer models.
        return "h264"

    if requested == "hevc":
        if not _ffmpeg_supports_encoder("hevc_videotoolbox"):
            raise RuntimeError("HEVC encoding is not available (hevc_videotoolbox).")
        return "hevc"

    if requested == "av1":
        if not _ffmpeg_supports_encoder("libsvtav1"):
            raise RuntimeError("AV1 encoding is not available (libsvtav1).")
        return "av1"

    if requested == "h264":
        return "h264"

    raise RuntimeError(f"Unknown codec: {requested}")


def codec_label(codec: str) -> str:
    labels = {
        "h264": "H.264 (VideoToolbox)" if _ffmpeg_supports_encoder("h264_videotoolbox") else "H.264",
        "hevc": "HEVC/H.265 (VideoToolbox)",
        "av1": "AV1 (SVT, software)",
    }
    return labels.get(codec, codec)


def default_jpeg_quality(width: int, height: int) -> int:
    if width * height >= 1920 * 1080:
        return 75
    return 80


def _target_bitrate(codec: str, width: int, height: int, *, buffered: bool) -> tuple[str, str, str]:
    """Pick bitrate targets. Efficient codecs use lower bitrate for similar quality."""
    pixels = width * height
    if pixels >= 1920 * 1080:
        if codec == "hevc":
            return ("3M", "3.5M", "12M") if buffered else ("2.5M", "3M", "6M")
        if codec == "av1":
            return ("2.5M", "3M", "12M") if buffered else ("2M", "2.5M", "6M")
        return ("5M", "6M", "12M") if buffered else ("4.5M", "5M", "5M")
    if pixels >= 1280 * 720:
        if codec in ("hevc", "av1"):
            return ("1.8M", "2.2M", "8M") if buffered else ("1.5M", "2M", "4M")
        return ("3M", "3.5M", "8M") if buffered else ("2.5M", "3M", "3M")
    if codec in ("hevc", "av1"):
        return ("1M", "1.2M", "4M") if buffered else ("900k", "1.1M", "2M")
    return ("1.5M", "2M", "4M") if buffered else ("1.5M", "2M", "2M")


def _video_encoder_args(
    codec: str,
    fps: int,
    width: int,
    height: int,
    *,
    buffered: bool,
) -> list[str]:
    bitrate, maxrate, bufsize = _target_bitrate(codec, width, height, buffered=buffered)
    gop = fps * (2 if buffered else 1)

    if codec == "hevc" and _ffmpeg_supports_encoder("hevc_videotoolbox"):
        return [
            "-c:v",
            "hevc_videotoolbox",
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

    if codec == "av1" and _ffmpeg_supports_encoder("libsvtav1"):
        return [
            "-c:v",
            "libsvtav1",
            "-preset",
            "6" if buffered else "10",
            "-crf",
            "32",
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
            "-pix_fmt",
            "yuv420p",
        ]

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
        codec: str = "hevc",
        buffered: bool = True,
        audio_fd: int | None = None,
        audio_sync: str = "off",
        audio_format: AudioFormat | None = None,
        port: int = 0,
        work_dir: Path | None = None,
        stats: PipelineStats | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.codec = codec
        self.buffered = buffered
        self.audio_fd = audio_fd
        self.audio_sync = audio_sync
        self.audio_format = audio_format or DEFAULT_AUDIO_FORMAT
        self.work_dir = work_dir or Path("/tmp/cast-tab-stream")
        self.work_dir.mkdir(parents=True, exist_ok=True)

        for old in self.work_dir.glob("seg*.ts"):
            old.unlink(missing_ok=True)
        playlist = self.work_dir / "stream.m3u8"
        playlist.unlink(missing_ok=True)

        self._latest = LatestFrame()
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
        self._frame_queue: deque[bytes] = deque()
        self._queue_cond = threading.Condition()
        self._queue_maxlen = max(1, self.fps * 3)

    @property
    def playlist_url(self) -> str:
        host = get_local_ip()
        port = self._port or (self._http_server.server_port if self._http_server else 0)
        return f"http://{host}:{port}/stream.m3u8"

    def start(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg is required but was not found in PATH.")

        self._start_ffmpeg()
        self._start_sampler_thread()
        self._start_writer_thread()
        self._start_http_server()

    def publish_frame(self, jpeg_data: bytes) -> None:
        if not self._stopped.is_set():
            self._latest.publish(jpeg_data)
            if self._stats is not None:
                self._stats.record_publish()

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
                *self._auto_av_offset_args(),
                "-thread_queue_size",
                "4096",
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

    def _auto_av_offset_args(self) -> list[str]:
        """Auto-correct the constant A/V skew from startup audio pre-roll.

        The tap fills the audio pipe before ffmpeg starts reading, so ffmpeg
        stamps that already-buffered audio with the same PTS 0 as the first
        (current) video frame, leaving audio lagging by the pre-roll duration.
        Measure exactly how much is queued right now and advance audio by that
        much with -itsoffset. Re-measured on every (re)start, no manual knob.
        """
        if self.audio_fd is None:
            return []
        try:
            buffered = _pipe_bytes_available(self.audio_fd)
        except OSError:
            return []
        offset_s = buffered / self.audio_format.bytes_per_second
        offset_s = max(0.0, min(offset_s, MAX_AUTO_AV_OFFSET_S))
        if offset_s < 0.005:
            return []
        print(
            f"Auto A/V sync: delaying audio {offset_s * 1000:.0f}ms "
            f"({buffered}-byte capture pre-roll).",
            flush=True,
        )
        # Audio runs ahead of video, so delay it (positive itsoffset).
        return ["-itsoffset", f"{offset_s:.3f}"]

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

    def _restart_ffmpeg(self) -> None:
        print("Restarting ffmpeg after sustained encoder backpressure...", flush=True)
        if self._stats is not None:
            self._stats.record_ffmpeg_restart()
        with self._ffmpeg_lock:
            self._kill_ffmpeg()
            self._start_ffmpeg()
        self._backpressure_started_at = None

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
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-framerate",
            str(self.fps),
            "-i",
            "pipe:0",
            *self._audio_input_args(),
            "-filter:v",
            f"scale={self.width}:{self.height}:force_original_aspect_ratio=increase,"
            f"crop={self.width}:{self.height},format=yuv420p",
            "-map",
            "0:v",
            "-map",
            "1:a",
            *_video_encoder_args(
                self.codec,
                self.fps,
                self.width,
                self.height,
                buffered=self.buffered,
            ),
            "-c:a",
            "aac",
            "-b:a",
            "160k" if self.buffered else "128k",
            # Keep the capture's native rate end to end so nothing resamples.
            "-ar",
            str(self.audio_format.sample_rate),
            "-ac",
            str(self.audio_format.channels),
            # "soft" corrects slow A/V clock drift over long sessions. async=1000
            # allows generous *gentle* stretching so it never resorts to hard
            # sample drops/inserts (which click). Default off keeps clean PCM.
            *(
                ["-af", "aresample=async=1000"]
                if self.audio_sync == "soft"
                else []
            ),
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
        self._ffmpeg = subprocess.Popen(cmd, **popen_kwargs)

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
                # If we fell far behind schedule (system hiccup), resync rather
                # than bursting a backlog of identical timestamps.
                if now - next_tick > 1.0:
                    next_tick = now
                    if self._stats is not None:
                        self._stats.record_encode_resync()
                next_tick += frame_period

                frame, published_at, generation = self._latest.peek()
                if frame is None:
                    continue

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