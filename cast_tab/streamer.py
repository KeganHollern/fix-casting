"""HLS streaming server and ffmpeg encoder for tab screencast frames."""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


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

    def publish(self, frame: bytes) -> None:
        with self._lock:
            self._frame = frame

    def peek(self) -> bytes | None:
        with self._lock:
            return self._frame


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
        port: int = 0,
        work_dir: Path | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.codec = codec
        self.buffered = buffered
        self.audio_fd = audio_fd
        self.work_dir = work_dir or Path("/tmp/cast-tab-stream")
        self.work_dir.mkdir(parents=True, exist_ok=True)

        for old in self.work_dir.glob("seg*.ts"):
            old.unlink(missing_ok=True)
        playlist = self.work_dir / "stream.m3u8"
        playlist.unlink(missing_ok=True)

        self._latest = LatestFrame()
        self._ffmpeg: subprocess.Popen[bytes] | None = None
        self._encoder_thread: threading.Thread | None = None
        self._http_server: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._port = port
        self._stopped = threading.Event()

    @property
    def playlist_url(self) -> str:
        host = get_local_ip()
        port = self._port or (self._http_server.server_port if self._http_server else 0)
        return f"http://{host}:{port}/stream.m3u8"

    def start(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg is required but was not found in PATH.")

        self._start_ffmpeg()
        self._start_encoder_thread()
        self._start_http_server()

    def publish_frame(self, jpeg_data: bytes) -> None:
        if not self._stopped.is_set():
            self._latest.publish(jpeg_data)

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

        if self._encoder_thread and self._encoder_thread.is_alive():
            self._encoder_thread.join(timeout=5)

        if self._ffmpeg and self._ffmpeg.stdin:
            try:
                self._ffmpeg.stdin.close()
            except OSError:
                pass
        if self._ffmpeg:
            try:
                self._ffmpeg.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._ffmpeg.kill()

        if self._http_server:
            self._http_server.shutdown()
        if self._http_thread and self._http_thread.is_alive():
            self._http_thread.join(timeout=3)

    def _audio_input_args(self) -> list[str]:
        if self.audio_fd is not None:
            return [
                "-thread_queue_size",
                "512",
                "-f",
                "s16le",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-i",
                f"/dev/fd/{self.audio_fd}",
            ]
        return [
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
        ]

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
            "-ar",
            "44100",
            "-ac",
            "2",
            *([] if self.buffered else ["-async", "1"]),
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
            popen_kwargs["pass_fds"] = (self.audio_fd,)
            popen_kwargs["close_fds"] = False
        self._ffmpeg = subprocess.Popen(cmd, **popen_kwargs)

    def _start_encoder_thread(self) -> None:
        def run() -> None:
            assert self._ffmpeg is not None
            stdin = self._ffmpeg.stdin
            assert stdin is not None
            last_frame: bytes | None = None
            frame_period = 1.0 / self.fps
            next_tick = time.monotonic()

            while not self._stopped.is_set():
                now = time.monotonic()
                sleep_for = next_tick - now
                if sleep_for > 0:
                    time.sleep(sleep_for)
                next_tick += frame_period

                frame = self._latest.peek() or last_frame
                if frame is None:
                    continue
                last_frame = frame
                try:
                    stdin.write(frame)
                    stdin.flush()
                except (BrokenPipeError, OSError):
                    break

        self._encoder_thread = threading.Thread(target=run, name="hls-encoder", daemon=True)
        self._encoder_thread.start()

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