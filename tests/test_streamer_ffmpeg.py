"""Slow tests exercising the real ffmpeg process management."""

import shutil
import threading
import time
from pathlib import Path

import pytest

from cast_tab.stats import PipelineStats
from cast_tab.streamer import HLSStreamer

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]


def test_stderr_drain_records_encoder_errors(tmp_path: Path):
    """Garbage MJPEG makes ffmpeg emit stderr; the drain must surface it in
    stats (and, structurally, keep the pipe from filling and stalling ffmpeg)."""
    stats = PipelineStats(target_fps=30.0)
    s = HLSStreamer(width=320, height=240, fps=30, buffered=False,
                    work_dir=tmp_path, stats=stats)
    stop = threading.Event()

    def pump():
        while not stop.is_set():
            s.publish_frame(b"\xff\xd8 not a jpeg \xff\xd9" * 200)
            time.sleep(1 / 30)

    threading.Thread(target=pump, daemon=True).start()
    s.start()
    time.sleep(4)
    stop.set()
    s.stop()  # EOF flushes ffmpeg's buffered error lines
    time.sleep(1.0)

    snap = stats.snapshot(5.0)
    assert snap.ffmpeg_errors > 0
    assert snap.ffmpeg_last_error


def test_owned_work_dir_lifecycle():
    s = HLSStreamer(width=320, height=240, fps=30)
    wd = s.work_dir
    assert wd.exists() and wd.name.startswith("cast-tab-stream-")
    s.stop()
    assert not wd.exists()


def test_explicit_work_dir_preserved(tmp_path: Path):
    s = HLSStreamer(width=320, height=240, fps=30, work_dir=tmp_path)
    s.stop()
    assert tmp_path.exists()
