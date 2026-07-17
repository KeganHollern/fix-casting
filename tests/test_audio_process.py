"""AudioTee child-process lifecycle regression tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cast_tab import audio


def test_failed_audio_start_reaps_child(monkeypatch):
    real_popen = subprocess.Popen
    spawned = []

    def fake_popen(_command, **kwargs):
        process = real_popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            **kwargs,
        )
        spawned.append(process)
        return process

    monkeypatch.setattr(audio, "audiotee_path", lambda: Path(sys.executable))
    monkeypatch.setattr(audio.subprocess, "Popen", fake_popen)

    with pytest.raises(audio.AudioCaptureError, match="No audio data"):
        audio.start_chrome_audio_capture([1234], ready_timeout=0.01)

    assert len(spawned) == 1
    assert spawned[0].poll() is not None


def test_terminate_and_reap_kills_child_that_ignores_sigterm():
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import signal, sys, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(60)"
            ),
        ],
        stdout=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == b"ready\n"

        audio._terminate_and_reap(
            process,
            terminate_timeout=0.05,
            kill_timeout=1.0,
        )

        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1.0)
        if process.stdout is not None:
            process.stdout.close()
