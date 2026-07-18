"""AudioTee child-process lifecycle regression tests."""

from __future__ import annotations

import subprocess
import sys
from collections import deque
from pathlib import Path

import pytest

from cast_tab import audio


def test_pid_candidates_try_every_renderer_individually(monkeypatch, tmp_path) -> None:
    renderers = [101, 102, 103, 104, 105]
    lines = [
        (pid, f"Google Chrome --type=renderer --user-data-dir={tmp_path}")
        for pid in renderers
    ]
    monkeypatch.setattr(audio, "_profile_process_lines", lambda _profile: lines)

    candidates = audio.chrome_audio_pid_candidates(tmp_path)

    assert candidates[0] == renderers
    assert [[pid] for pid in renderers] == candidates[1:]


def test_pid_candidates_prefer_profile_audio_service(monkeypatch, tmp_path) -> None:
    lines = [
        (101, f"Google Chrome --type=renderer --user-data-dir={tmp_path}"),
        (
            202,
            "Google Chrome --type=utility "
            f"--utility-sub-type=audio.mojom.AudioService --user-data-dir={tmp_path}",
        ),
        (
            203,
            "Google Chrome --type=utility "
            f"--utility-sub-type=audio.mojom.AudioService --user-data-dir={tmp_path}",
        ),
    ]
    monkeypatch.setattr(audio, "_profile_process_lines", lambda _profile: lines)

    candidates = audio.chrome_audio_pid_candidates(tmp_path)

    assert candidates[:3] == [[202], [203], [101]]


def test_silent_candidate_does_not_abort_audio_search(monkeypatch, tmp_path) -> None:
    expected_capture = object()
    attempts: list[tuple[int, ...]] = []
    monkeypatch.setattr(
        audio,
        "chrome_audio_pid_candidates",
        lambda _profile: [[111], [222]],
    )

    def start(pids, *, ready_timeout, on_stderr, cancelled):
        del ready_timeout, on_stderr, cancelled
        attempts.append(tuple(pids))
        if pids == [111]:
            raise audio.AudioCaptureCandidateError(
                "No audio data received from cast browser tap."
            )
        return expected_capture

    monkeypatch.setattr(audio, "start_chrome_audio_capture", start)

    capture = audio.try_start_chrome_audio_capture(
        tmp_path,
        timeout=1,
        retry_interval=0,
    )

    assert capture is expected_capture
    assert attempts == [(111,), (222,)]


def test_fatal_audio_error_is_not_retried_for_other_candidates(
    monkeypatch, tmp_path
) -> None:
    attempts: list[tuple[int, ...]] = []
    monkeypatch.setattr(
        audio,
        "chrome_audio_pid_candidates",
        lambda _profile: [[111], [222]],
    )

    def fail(pids, **_kwargs):
        attempts.append(tuple(pids))
        raise audio.AudioCaptureError("AudioTee binary disappeared")

    monkeypatch.setattr(audio, "start_chrome_audio_capture", fail)

    with pytest.raises(audio.AudioCaptureError, match="binary disappeared"):
        audio.try_start_chrome_audio_capture(tmp_path, timeout=1)

    assert attempts == [(111,)]


def test_audio_capture_reports_helper_death_with_stderr_tail() -> None:
    class Process:
        def poll(self) -> int:
            return 9

    capture = audio.AudioCapture(
        process=Process(),  # type: ignore[arg-type]
        read_fd=123,
        pids=(456,),
        audio_format=audio.DEFAULT_AUDIO_FORMAT,
        stderr_tail=deque(["tap stopped unexpectedly"]),
    )

    with pytest.raises(
        audio.AudioCaptureError,
        match="status 9: tap stopped unexpectedly",
    ):
        capture.raise_if_failed()


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


def test_cancelled_audio_start_reaps_child(monkeypatch):
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

    with pytest.raises(audio.AudioCaptureCancelled, match="cancelled"):
        audio.start_chrome_audio_capture(
            [1234],
            cancelled=lambda: True,
        )

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
