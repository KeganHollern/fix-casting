"""Unit tests for encoder arg construction (pure, no ffmpeg needed)."""

import io
import subprocess
import threading
from collections import deque
from types import SimpleNamespace

import pytest

import cast_tab.encoder as encoder
from cast_tab.encoder import (
    FfmpegProcess,
    default_fps_for_resolution,
    estimated_hls_holdback_s,
    h264_main_level,
    hls_args,
    hls_playlist_retention_s,
    hls_segment_duration_s,
    target_bitrate,
    video_encoder_args,
)
from cast_tab.stats import PipelineStats


def test_ffmpeg_constructor_reaps_child_when_stderr_thread_cannot_start(
    monkeypatch,
):
    class FakeProcess:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = None
            self.stderr = io.BytesIO()
            self.returncode = None
            self.terminated = False
            self.killed = False
            self.wait_timeouts = []

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, *, timeout):
            self.wait_timeouts.append(timeout)
            self.returncode = -15
            return self.returncode

    class BrokenThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread creation failed")

    proc = FakeProcess()
    monkeypatch.setattr(encoder.subprocess, "Popen", lambda *_a, **_kw: proc)
    monkeypatch.setattr(encoder.threading, "Thread", BrokenThread)

    with pytest.raises(RuntimeError, match="thread creation failed"):
        FfmpegProcess(["ffmpeg"])

    assert proc.terminated is True
    assert proc.killed is False
    assert proc.wait_timeouts == [1]
    assert proc.stdin.closed
    assert proc.stderr.closed


def test_ffmpeg_kill_surfaces_unreaped_child_for_owner_retry():
    class Process:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.returncode = None
            self.wait_calls = 0

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, *, timeout):
            del timeout
            self.wait_calls += 1
            if self.wait_calls <= 2:
                raise subprocess.TimeoutExpired("ffmpeg", 3)
            self.returncode = -9
            return self.returncode

    process = FfmpegProcess.__new__(FfmpegProcess)
    process._proc = Process()

    with pytest.raises(TimeoutError, match="reaped after SIGKILL"):
        process.kill()

    assert process.poll() is None
    process.kill()
    assert process.poll() == -9
    assert process.stdin.closed


def test_ffmpeg_kill_does_not_close_locked_stdin_until_child_is_reaped():
    class LockedStdin:
        def __init__(self, owner):
            self.owner = owner
            self.close_calls = 0
            self.release = threading.Event()

        def close(self):
            self.close_calls += 1
            if self.owner.returncode is None:
                # Model BufferedWriter.close() waiting on the lock held by a
                # writer whose pipe write cannot finish while ffmpeg is live.
                self.release.wait(timeout=10)

    class Process:
        def __init__(self):
            self.returncode = None
            self.wait_calls = 0
            self.stdin = LockedStdin(self)

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, *, timeout):
            del timeout
            self.wait_calls += 1
            if self.wait_calls <= 2:
                raise subprocess.TimeoutExpired("ffmpeg", 3)
            self.returncode = -9
            return self.returncode

    child = Process()
    process = FfmpegProcess.__new__(FfmpegProcess)
    process._proc = child
    failures = []
    completed = threading.Event()

    def first_cleanup_attempt():
        try:
            process.kill()
        except BaseException as exc:
            failures.append(exc)
        finally:
            completed.set()

    worker = threading.Thread(target=first_cleanup_attempt)
    worker.start()
    try:
        assert completed.wait(timeout=0.5), "kill blocked trying to close live stdin"
    finally:
        child.stdin.release.set()
        worker.join(timeout=1)

    assert len(failures) == 1
    assert isinstance(failures[0], TimeoutError)
    assert child.returncode is None
    assert child.stdin.close_calls == 0

    process.kill()
    assert child.returncode == -9
    assert child.stdin.close_calls == 1


def test_quiet_summary_stats_do_not_suppress_ffmpeg_diagnostics(capsys):
    stats = PipelineStats(trace_enabled=False)
    process = object.__new__(FfmpegProcess)
    process._proc = SimpleNamespace(stderr=io.BytesIO(b"encoder failed\n"))
    process._stats = stats
    process.stderr_tail = deque(maxlen=50)

    process._drain_stderr()

    assert "[ffmpeg] encoder failed" in capsys.readouterr().out
    assert stats.snapshot(1.0).ffmpeg_errors == 1


def test_encoder_probe_failure_is_not_cached(monkeypatch):
    """A transient probe failure must not pin False for the whole run."""
    monkeypatch.setattr(encoder, "_encoder_support", {})
    calls = {"n": 0}

    def flaky_run(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5)

        class R:
            stdout = "... h264_videotoolbox ..."

        return R()

    monkeypatch.setattr(encoder.subprocess, "run", flaky_run)
    assert encoder.ffmpeg_supports_encoder("h264_videotoolbox") is False
    assert encoder.ffmpeg_supports_encoder("h264_videotoolbox") is True  # re-probed
    assert encoder.ffmpeg_supports_encoder("h264_videotoolbox") is True  # cached
    assert calls["n"] == 2


def test_target_bitrate_resolution_tiers():
    assert target_bitrate(1920, 1080, buffered=True) == ("15M", "18M", "36M")
    assert target_bitrate(1920, 1080, buffered=False) == ("15M", "16.5M", "16.5M")
    assert target_bitrate(1280, 720, buffered=True) == ("3M", "3.5M", "8M")
    assert target_bitrate(640, 480, buffered=False) == ("1.5M", "2M", "2M")


def test_target_bitrate_override_ratios():
    # Buffered: roomy VBV (1.2x maxrate, 2.4x bufsize).
    assert target_bitrate(1920, 1080, buffered=True, override_mbps=10) == (
        "10M",
        "12M",
        "24M",
    )
    # Unbuffered: tight VBV (1.1x both).
    assert target_bitrate(1920, 1080, buffered=False, override_mbps=10) == (
        "10M",
        "11M",
        "11M",
    )


def test_default_fps():
    assert default_fps_for_resolution(1920, 1080, buffered=True) == 30
    assert default_fps_for_resolution(1920, 1080, buffered=False) == 23
    assert default_fps_for_resolution(1280, 720, buffered=False) == 24


def test_video_encoder_args_gop():
    args = video_encoder_args(30, 1920, 1080, buffered=True)
    gop = args[args.index("-g") + 1]
    keyint = args[args.index("-keyint_min") + 1]
    assert gop == "60"  # 2s GOP when buffered
    assert keyint == "30"
    args = video_encoder_args(30, 1920, 1080, buffered=False)
    assert args[args.index("-g") + 1] == "30"  # 1s GOP unbuffered


def test_software_encoder_advertises_a_valid_level_for_each_video_mode():
    cases = (
        (640, 480, 30, "3.0"),
        (1280, 720, 30, "3.1"),
        (1280, 720, 60, "3.2"),
        # The production profile's 36M VBV requires Level 4.1 even though
        # 1080p30's macroblock rate alone fits Level 4.0.
        (1920, 1080, 30, "4.1"),
        (1920, 1080, 60, "4.2"),
        (3840, 2160, 60, "5.2"),
    )
    for width, height, fps, expected_level in cases:
        args = video_encoder_args(
            fps,
            width,
            height,
            buffered=True,
            encoder=encoder.SOFTWARE_VIDEO_ENCODER,
        )
        assert args[args.index("-level") + 1] == expected_level


def test_h264_level_accounts_for_main_profile_bitrate_limit():
    assert h264_main_level(1280, 720, 30, maxrate_mbps=14) == "3.1"
    assert h264_main_level(1280, 720, 30, maxrate_mbps=20) == "3.2"


def test_h264_level_accounts_for_coded_picture_buffer_limit():
    assert (
        h264_main_level(
            1920,
            1080,
            30,
            maxrate_mbps=18,
            bufsize_mbits=25,
        )
        == "4.0"
    )
    assert (
        h264_main_level(
            1920,
            1080,
            30,
            maxrate_mbps=18,
            bufsize_mbits=36,
        )
        == "4.1"
    )


def test_h264_level_accounts_for_per_dimension_macroblock_limit():
    # Product-only MaxFS checks would incorrectly choose 3.0 for this shape;
    # Annex A also caps either picture dimension to sqrt(MaxFS * 8).
    assert h264_main_level(3000, 16, 30, maxrate_mbps=1) == "3.2"


def test_video_encoder_args_rejects_unknown_encoder():
    try:
        video_encoder_args(
            30,
            1920,
            1080,
            buffered=True,
            encoder="mystery_h264",
        )
    except ValueError as exc:
        assert "Unsupported H.264 encoder" in str(exc)
    else:
        raise AssertionError("unknown encoder was accepted")


def test_hls_args_retention_and_estimated_holdback_are_consistent():
    for buffered in (True, False):
        args = hls_args(buffered=buffered)
        hls_time = int(args[args.index("-hls_time") + 1])
        list_size = int(args[args.index("-hls_list_size") + 1])
        assert hls_segment_duration_s(buffered=buffered) == hls_time
        assert hls_playlist_retention_s(buffered=buffered) == hls_time * list_size
        assert estimated_hls_holdback_s(buffered=buffered) == hls_time * 3
        assert list_size >= 3

    assert hls_playlist_retention_s(buffered=True) == 12
    assert estimated_hls_holdback_s(buffered=True) == 6
    assert hls_playlist_retention_s(buffered=False) == 4
    assert estimated_hls_holdback_s(buffered=False) == 3


def test_hls_restarts_append_to_the_existing_playlist():
    initial = hls_args(buffered=True)
    restarted = hls_args(buffered=True, append=True)
    initial_flags = initial[initial.index("-hls_flags") + 1].split("+")
    restarted_flags = restarted[restarted.index("-hls_flags") + 1].split("+")

    # FFmpeg's append_list mode parses the existing playlist, continues its
    # media sequence, and inserts a discontinuity before the first newly
    # appended segment. discont_start must not be combined with it: that also
    # inserts a spurious discontinuity at the head of the retained playlist.
    assert "append_list" not in initial_flags
    assert "append_list" in restarted_flags
    assert "discont_start" not in restarted_flags
