"""ffmpeg for the HLS encode: encoder selection, command args, process lifecycle."""

from __future__ import annotations

import math
import subprocess
import threading
from collections import deque

from cast_tab.stats import PipelineStats

# 92 keeps the capture crisp so the (mostly local-CPU) JPEG stage isn't the
# quality bottleneck when there's H.264 bitrate to carry the detail.
DEFAULT_JPEG_QUALITY = 92

# HLS window per mode.  The segment target multiplied by the list size is the
# amount of media retained in the rolling playlist; it is not a promise about
# how far a receiver plays behind the live edge.  Without an explicit HLS
# hold-back tag, roughly three target durations is a useful player estimate,
# but the Chromecast remains free to choose a different live position.
#
# ``buffered=True`` is the compatibility name for the production profile.  Its
# encode quality stays unchanged while the old 4s x 12 deep window is replaced
# by a conservative 2s x 6 window.  Six target durations leaves headroom above
# the conventional three-duration holdback without retaining 48 seconds.
HLS_TIME_S = {"production": 2, "low_latency": 1}
HLS_LIST_SIZE = {"production": 6, "low_latency": 4}


# Memoized by hand instead of lru_cache: only a probe that actually ran gets
# cached. Caching a transient failure (timeout under startup load) as False
# would silently downgrade every (re)launch of the whole cast to libx264.
_encoder_support: dict[str, bool] = {}

HARDWARE_VIDEO_ENCODER = "h264_videotoolbox"
SOFTWARE_VIDEO_ENCODER = "libx264"

# Annex A limits for Main-profile streams. We intentionally start at 3.0:
# every resolution this application exposes fits there or above, and avoiding
# obsolete lower levels keeps receiver compatibility simple. The bitrate limit
# and CPB limits are for Main profile, not the higher High-profile multipliers.
_H264_MAIN_LEVEL_LIMITS = (
    # level, macroblocks/s, macroblocks/frame, max bitrate, max CPB (Mbit)
    ("3.0", 40_500, 1_620, 10.0, 10.0),
    ("3.1", 108_000, 3_600, 14.0, 14.0),
    ("3.2", 216_000, 5_120, 20.0, 20.0),
    ("4.0", 245_760, 8_192, 20.0, 25.0),
    ("4.1", 245_760, 8_192, 50.0, 62.5),
    ("4.2", 522_240, 8_704, 50.0, 62.5),
    ("5.0", 589_824, 22_080, 135.0, 135.0),
    ("5.1", 983_040, 36_864, 240.0, 240.0),
    ("5.2", 2_073_600, 36_864, 240.0, 240.0),
)


def ffmpeg_supports_encoder(encoder: str) -> bool:
    if encoder in _encoder_support:
        return _encoder_support[encoder]
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return False  # transient; re-probe next call
    supported = encoder in result.stdout
    _encoder_support[encoder] = supported
    return supported


def default_fps_for_resolution(width: int, height: int, *, buffered: bool = False) -> int:
    if buffered:
        return 30
    if width * height >= 1920 * 1080:
        return 23
    return 24


def codec_label() -> str:
    return (
        "H.264 (VideoToolbox)"
        if preferred_video_encoder() == HARDWARE_VIDEO_ENCODER
        else "H.264"
    )


def preferred_video_encoder() -> str:
    """Choose the first encoder for a session; runtime fallback is per-stream."""
    if ffmpeg_supports_encoder(HARDWARE_VIDEO_ENCODER):
        return HARDWARE_VIDEO_ENCODER
    return SOFTWARE_VIDEO_ENCODER


def h264_main_level(
    width: int,
    height: int,
    fps: int,
    *,
    maxrate_mbps: float,
    bufsize_mbits: float | None = None,
) -> str:
    """Return the lowest valid H.264 Main-profile level for the encode."""
    if bufsize_mbits is None:
        bufsize_mbits = maxrate_mbps
    if (
        width <= 0
        or height <= 0
        or fps <= 0
        or maxrate_mbps <= 0
        or bufsize_mbits <= 0
    ):
        raise ValueError(
            "H.264 dimensions, fps, maxrate, and bufsize must be positive"
        )
    width_macroblocks = math.ceil(width / 16)
    height_macroblocks = math.ceil(height / 16)
    frame_macroblocks = width_macroblocks * height_macroblocks
    macroblocks_per_second = frame_macroblocks * fps
    for (
        level,
        max_mbps,
        max_frame_mbs,
        max_bitrate_mbps,
        max_cpb_mbits,
    ) in _H264_MAIN_LEVEL_LIMITS:
        max_dimension_macroblocks = math.floor(math.sqrt(max_frame_mbs * 8))
        if (
            macroblocks_per_second <= max_mbps
            and frame_macroblocks <= max_frame_mbs
            and width_macroblocks <= max_dimension_macroblocks
            and height_macroblocks <= max_dimension_macroblocks
            and maxrate_mbps <= max_bitrate_mbps
            and bufsize_mbits <= max_cpb_mbits
        ):
            return level
    raise ValueError(
        "Stream exceeds supported H.264 Main-profile level 5.2 limits: "
        f"{width}x{height}@{fps}, maxrate={maxrate_mbps:g}Mbps, "
        f"bufsize={bufsize_mbits:g}Mbit"
    )


def _rate_mbps(rate: str) -> float:
    if not rate.endswith("M"):
        raise ValueError(f"Expected an Mbps ffmpeg rate, got {rate!r}")
    return float(rate[:-1])


def _hls_mode(*, buffered: bool) -> str:
    return "production" if buffered else "low_latency"


def hls_playlist_retention_s(*, buffered: bool) -> int:
    """Media duration retained in the rolling playlist."""
    mode = _hls_mode(buffered=buffered)
    return HLS_TIME_S[mode] * HLS_LIST_SIZE[mode]


def hls_segment_duration_s(*, buffered: bool) -> int:
    """Target duration of one HLS media segment."""
    return HLS_TIME_S[_hls_mode(buffered=buffered)]


def estimated_hls_holdback_s(*, buffered: bool) -> int:
    """Conventional live holdback estimate, not a receiver latency guarantee."""
    mode = _hls_mode(buffered=buffered)
    return HLS_TIME_S[mode] * 3


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
    encoder: str | None = None,
) -> list[str]:
    bitrate, maxrate, bufsize = target_bitrate(
        width, height, buffered=buffered, override_mbps=bitrate_mbps
    )
    gop = fps * (2 if buffered else 1)
    encoder = encoder or preferred_video_encoder()

    if encoder == HARDWARE_VIDEO_ENCODER:
        return [
            "-c:v",
            HARDWARE_VIDEO_ENCODER,
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

    if encoder != SOFTWARE_VIDEO_ENCODER:
        raise ValueError(f"Unsupported H.264 encoder: {encoder}")

    level = h264_main_level(
        width,
        height,
        fps,
        maxrate_mbps=_rate_mbps(maxrate),
        bufsize_mbits=_rate_mbps(bufsize),
    )
    return [
        "-c:v",
        SOFTWARE_VIDEO_ENCODER,
        "-profile:v",
        "main",
        "-level",
        level,
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


def hls_args(*, buffered: bool, append: bool = False) -> list[str]:
    mode = _hls_mode(buffered=buffered)
    flags = ["delete_segments"]
    if append:
        # On relaunch FFmpeg parses the existing media sequence, appends the
        # new generation, and inserts its boundary discontinuity. Enabling
        # append_list on the initial generation would add a bogus initial tag.
        flags.append("append_list")
    flags.extend(["omit_endlist", "independent_segments"])
    return [
        "-f",
        "hls",
        "-hls_time",
        str(HLS_TIME_S[mode]),
        "-hls_list_size",
        str(HLS_LIST_SIZE[mode]),
        "-hls_flags",
        "+".join(flags),
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
        proc = subprocess.Popen(cmd, **popen_kwargs)
        try:
            # Publish the child on the instance before constructing any other
            # fallible state.  In particular, Thread.start() can fail after
            # ffmpeg has inherited its input fds; abandoning the constructor
            # at that point would otherwise orphan the encoder and keep those
            # fds alive indefinitely.
            self._proc = proc
            self._stats = stats
            self.stderr_tail: deque[str] = deque(maxlen=50)
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr, name="ffmpeg-stderr", daemon=True
            )
            self._stderr_thread.start()
        except BaseException:
            self._reap_failed_initialization(proc)
            raise

    @staticmethod
    def _reap_failed_initialization(proc: subprocess.Popen) -> None:
        """Best-effort rollback when ownership cannot leave the constructor."""
        try:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is None:
                    continue
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

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
                # Quiet/default mode still carries cumulative stats for the
                # exit summary. Preserve its immediate ffmpeg diagnostics;
                # --stats and --tui surface the recorded errors themselves.
                if self._stats is None or not self._stats.trace_enabled:
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

    @property
    def pid(self) -> int:
        return self._proc.pid

    def poll(self) -> int | None:
        return self._proc.poll()

    def stderr_text(self, *, join_timeout: float = 1.0) -> str:
        """The drained stderr tail; waits briefly for the last lines to land."""
        self._stderr_thread.join(timeout=join_timeout)
        return "\n".join(self.stderr_tail)

    def kill(self, *, graceful: bool = False) -> None:
        """Stop this ffmpeg. graceful=True closes stdin first (EOF) so ffmpeg
        flushes pending error output and exits on its own — only safe when no
        thread can be blocked writing to stdin, because a blocked writer holds
        the buffered file's internal lock (close() would block on it) and only
        killing the reader unblocks that writer. Default is terminate-first,
        which is always deadlock-safe.
        """
        try:
            if graceful and self._proc.stdin and self._proc.poll() is None:
                try:
                    self._proc.stdin.close()
                except (OSError, ValueError):
                    pass
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass  # didn't exit on EOF; fall through to terminate
            if self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    try:
                        self._proc.wait(timeout=3)
                    except subprocess.TimeoutExpired as exc:
                        # Ownership cannot be discarded while a live/unreaped
                        # child may still hold inherited audio/video fds.
                        raise TimeoutError(
                            "ffmpeg could not be reaped after SIGKILL"
                        ) from exc
            if self._proc.poll() is None:
                raise TimeoutError("ffmpeg stop returned before the child was reaped")
        finally:
            # A live child may still have a writer blocked in BufferedWriter.
            # That writer owns stdin's internal lock, so close() can block
            # forever and defeat the bounded reap contract. Keep stdin with
            # the still-owned process; a later retry closes it after reap.
            if self._proc.poll() is not None and self._proc.stdin:
                try:
                    self._proc.stdin.close()
                except (OSError, ValueError):
                    pass
