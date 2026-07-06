"""ffmpeg for the HLS encode: encoder selection, command args, process lifecycle."""

from __future__ import annotations

import subprocess
import threading
from collections import deque
from functools import lru_cache

from cast_tab.stats import PipelineStats

# 92 keeps the capture crisp so the (mostly local-CPU) JPEG stage isn't the
# quality bottleneck when there's H.264 bitrate to carry the detail.
DEFAULT_JPEG_QUALITY = 92

# HLS window per mode. Segment length × playlist length is the TV-side buffer,
# which is what the CLI advertises as the buffered delay.
HLS_TIME_S = {"buffered": 4, "low_latency": 1}
HLS_LIST_SIZE = {"buffered": 12, "low_latency": 4}


@lru_cache(maxsize=None)
def ffmpeg_supports_encoder(encoder: str) -> bool:
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
    return "H.264 (VideoToolbox)" if ffmpeg_supports_encoder("h264_videotoolbox") else "H.264"


def tv_delay_s(*, buffered: bool) -> int:
    """Approximate playback delay the TV buffers in each mode."""
    mode = "buffered" if buffered else "low_latency"
    return HLS_TIME_S[mode] * HLS_LIST_SIZE[mode]


def target_bitrate(
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


def video_encoder_args(
    fps: int,
    width: int,
    height: int,
    *,
    buffered: bool,
    bitrate_mbps: float | None = None,
) -> list[str]:
    bitrate, maxrate, bufsize = target_bitrate(
        width, height, buffered=buffered, override_mbps=bitrate_mbps
    )
    gop = fps * (2 if buffered else 1)

    if ffmpeg_supports_encoder("h264_videotoolbox"):
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


def hls_args(*, buffered: bool) -> list[str]:
    mode = "buffered" if buffered else "low_latency"
    return [
        "-f",
        "hls",
        "-hls_time",
        str(HLS_TIME_S[mode]),
        "-hls_list_size",
        str(HLS_LIST_SIZE[mode]),
        "-hls_flags",
        "delete_segments+append_list+omit_endlist+independent_segments",
        "-hls_segment_type",
        "mpegts",
    ]


class FfmpegProcess:
    """One spawned ffmpeg with its stderr continuously drained.

    ffmpeg writes errors to stderr for the life of the encode. Left unread,
    the ~64KB pipe buffer fills and ffmpeg blocks on the write — the encode
    stalls with no visible cause. The drain keeps ffmpeg running; the
    per-instance tail feeds the early-exit error report and each line is
    surfaced through stats so encoder errors show up in --stats/--tui
    instead of disappearing. The drain thread exits on EOF when this
    instance dies.
    """

    def __init__(
        self,
        cmd: list[str],
        *,
        pass_fds: tuple[int, ...] = (),
        stats: PipelineStats | None = None,
    ) -> None:
        popen_kwargs: dict = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
        }
        if pass_fds:
            # pass_fds keeps only these fds open in the child (close_fds stays
            # True by default); never inherit the rest of our fds, or ffmpeg
            # holds pipe write-ends open and never sees EOF on shutdown.
            popen_kwargs["pass_fds"] = pass_fds
        self._proc = subprocess.Popen(cmd, **popen_kwargs)
        self._stats = stats
        self.stderr_tail: deque[str] = deque(maxlen=50)
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name="ffmpeg-stderr", daemon=True
        )
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        proc = self._proc
        try:
            if proc.stderr is None:
                return
            for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if not line:
                    continue
                self.stderr_tail.append(line)
                if self._stats is not None:
                    self._stats.record_ffmpeg_stderr(line)
                else:
                    print(f"[ffmpeg] {line}", flush=True)
        except (OSError, ValueError):
            pass
        finally:
            if proc.stderr is not None:
                try:
                    proc.stderr.close()
                except OSError:
                    pass

    @property
    def stdin(self):
        return self._proc.stdin

    def poll(self) -> int | None:
        return self._proc.poll()

    def stderr_text(self, *, join_timeout: float = 1.0) -> str:
        """The drained stderr tail; waits briefly for the last lines to land."""
        self._stderr_thread.join(timeout=join_timeout)
        return "\n".join(self.stderr_tail)

    def kill(self) -> None:
        if self._proc.stdin:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
        if self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=3)
