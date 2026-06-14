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


def default_fps_for_resolution(width: int, height: int) -> int:
    if width * height >= 1920 * 1080:
        return 23
    return 24


def default_jpeg_quality(width: int, height: int) -> int:
    if width * height >= 1920 * 1080:
        return 75
    return 80


def _target_bitrate(width: int, height: int) -> tuple[str, str, str]:
    """Pick a steady bitrate for the stream resolution."""
    pixels = width * height
    if pixels >= 1920 * 1080:
        return "4.5M", "5M", "10M"
    if pixels >= 1280 * 720:
        return "2.5M", "3M", "6M"
    return "1.5M", "2M", "4M"


def _video_encoder_args(fps: int, width: int, height: int) -> list[str]:
    """Prefer macOS hardware encoding for lower latency and less CPU load."""
    bitrate, maxrate, bufsize = _target_bitrate(width, height)
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
            str(fps),
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
        "veryfast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        bitrate,
        "-maxrate",
        maxrate,
        "-bufsize",
        bufsize,
        "-g",
        str(fps * 2),
        "-keyint_min",
        str(fps),
        "-sc_threshold",
        "0",
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
        port: int = 0,
        work_dir: Path | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
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

    def wait_until_ready(self, timeout: float = 30.0) -> None:
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
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter:v",
            f"scale={self.width}:{self.height}:force_original_aspect_ratio=decrease,"
            f"pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
            "-map",
            "0:v",
            "-map",
            "1:a",
            *_video_encoder_args(self.fps, self.width, self.height),
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "44100",
            "-ac",
            "2",
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
            "-hls_segment_filename",
            segment_pattern,
            str(playlist),
        ]

        self._ffmpeg = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

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