"""AudioTee child-process lifecycle regression tests."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from collections import deque
from pathlib import Path

import pytest

from cast_tab import audio


def test_parse_audio_heartbeat_requires_valid_nonnegative_sequence() -> None:
    assert (
        audio._parse_audio_heartbeat(
            json.dumps(
                {
                    "message_type": "heartbeat",
                    "data": {"producer_sequence": 42},
                }
            )
        )
        == 42
    )
    assert audio._parse_audio_heartbeat("not json") is None
    assert audio._parse_audio_heartbeat('{"message_type":"info"}') is None
    assert (
        audio._parse_audio_heartbeat(
            '{"message_type":"heartbeat","data":{"producer_sequence":-1}}'
        )
        is None
    )


def test_audio_capture_health_requires_producer_sequence_progress(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr(audio.time, "monotonic", lambda: now[0])
    health = audio.AudioCaptureHealth(timeout_s=6.0)
    health.mark_ready()
    health.note_heartbeat(1)

    now[0] = 105.0
    health.note_heartbeat(1)
    assert health.stalled_for() is None

    now[0] = 106.1
    assert health.stalled_for() == pytest.approx(6.1)

    health.note_heartbeat(2)
    assert health.stalled_for() is None


def test_live_helper_with_stalled_heartbeat_requests_reattach(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr(audio.time, "monotonic", lambda: now[0])
    health = audio.AudioCaptureHealth(timeout_s=5.0)
    health.mark_ready()
    health.note_heartbeat(1)

    class Process:
        def poll(self):
            return None

    capture = audio.AudioCapture(
        process=Process(),  # type: ignore[arg-type]
        read_fd=123,
        pids=(),
        audio_format=audio.DEFAULT_AUDIO_FORMAT,
        health=health,
    )
    now[0] = 105.1

    with pytest.raises(audio.AudioCaptureError, match="heartbeat stalled"):
        capture.raise_if_failed()


def test_exited_chrome_source_pid_requests_reattach(monkeypatch) -> None:
    class Process:
        def poll(self):
            return None

    def missing_pid(_pid, _signal):
        raise ProcessLookupError

    monkeypatch.setattr(audio.os, "kill", missing_pid)
    capture = audio.AudioCapture(
        process=Process(),  # type: ignore[arg-type]
        read_fd=123,
        pids=(456,),
        audio_format=audio.DEFAULT_AUDIO_FORMAT,
    )

    with pytest.raises(audio.AudioCaptureError, match="source PID 456 exited"):
        capture.raise_if_failed()


def test_transient_profile_process_scan_failure_is_inconclusive(
    monkeypatch, tmp_path
) -> None:
    class Process:
        def poll(self):
            return None

    monkeypatch.setattr(audio, "_profile_process_lines", lambda _profile: None)
    capture = audio.AudioCapture(
        process=Process(),  # type: ignore[arg-type]
        read_fd=123,
        pids=(456,),
        audio_format=audio.DEFAULT_AUDIO_FORMAT,
        profile_dir=tmp_path,
    )

    capture.raise_if_failed()


def test_pid_candidates_try_every_renderer_individually(monkeypatch, tmp_path) -> None:
    renderers = [101, 102, 103, 104, 105]
    lines = [
        (pid, f"Google Chrome --type=renderer --user-data-dir={tmp_path}")
        for pid in renderers
    ]
    monkeypatch.setattr(audio, "_profile_process_lines", lambda _profile: lines)

    candidates = audio.chrome_audio_pid_candidates(tmp_path)

    assert [[pid] for pid in renderers] == candidates


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


def test_audio_capture_stop_is_exact_once(monkeypatch) -> None:
    closed: list[int] = []

    class Process:
        stderr = None
        waits = 0

        def poll(self):
            return 0

        def wait(self, *, timeout):
            del timeout
            self.waits += 1
            return 0

    process = Process()
    capture = audio.AudioCapture(
        process=process,  # type: ignore[arg-type]
        read_fd=123,
        pids=(),
        audio_format=audio.DEFAULT_AUDIO_FORMAT,
    )
    monkeypatch.setattr(audio.os, "close", closed.append)

    audio.stop_audio_capture(capture)
    audio.stop_audio_capture(capture)

    assert closed == [123]
    assert process.waits == 1
    assert capture.lifecycle.completed


def test_audio_capture_stop_retries_reap_without_reclosing_fd(monkeypatch) -> None:
    closed: list[int] = []

    class Process:
        stderr = None
        waits = 0

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, *, timeout):
            del timeout
            self.waits += 1
            if self.waits <= 2:
                raise subprocess.TimeoutExpired("fake-audiotee", 0)
            return 0

    process = Process()
    capture = audio.AudioCapture(
        process=process,  # type: ignore[arg-type]
        read_fd=123,
        pids=(),
        audio_format=audio.DEFAULT_AUDIO_FORMAT,
    )
    monkeypatch.setattr(audio.os, "close", closed.append)

    with pytest.raises(TimeoutError, match="reaped"):
        audio.stop_audio_capture(capture)
    audio.stop_audio_capture(capture)

    assert closed == [123]
    assert process.waits == 3
    assert capture.lifecycle.completed


def test_preroll_drainer_discards_only_before_streamer_handoff() -> None:
    read_fd, write_fd = os.pipe()

    class Process:
        def poll(self):
            return None

    capture = audio.AudioCapture(
        process=Process(),  # type: ignore[arg-type]
        read_fd=read_fd,
        pids=(),
        audio_format=audio.DEFAULT_AUDIO_FORMAT,
    )
    drainer = audio.AudioPrerollDrainer(capture)
    try:
        os.write(write_fd, b"pre-boundary-pcm" * 100)
        drainer.start()
        deadline = audio.time.monotonic() + 1.0
        while audio._pipe_bytes_available(read_fd) and audio.time.monotonic() < deadline:
            audio.time.sleep(0.01)
        drainer.stop()

        assert audio._pipe_bytes_available(read_fd) == 0
    finally:
        os.close(read_fd)
        os.close(write_fd)


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


def test_audio_start_rolls_back_when_stderr_thread_cannot_start(monkeypatch):
    closed_fds: list[int] = []

    class FakeProcess:
        def __init__(self):
            self.stderr = io.BytesIO()
            self.returncode = None
            self.terminated = False
            self.waits: list[float] = []

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def kill(self):
            raise AssertionError("cooperative child should not need SIGKILL")

        def wait(self, *, timeout):
            self.waits.append(timeout)
            self.returncode = -15
            return self.returncode

    class BrokenThread:
        ident = None

        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread creation failed")

    process = FakeProcess()
    monkeypatch.setattr(audio, "audiotee_path", lambda: Path("/fake/audiotee"))
    monkeypatch.setattr(audio.os, "pipe", lambda: (101, 102))
    monkeypatch.setattr(audio.os, "close", closed_fds.append)
    monkeypatch.setattr(audio.subprocess, "Popen", lambda *_a, **_kw: process)
    monkeypatch.setattr(audio.threading, "Thread", BrokenThread)

    with pytest.raises(RuntimeError, match="thread creation failed"):
        audio.start_chrome_audio_capture([1234])

    assert closed_fds == [102, 101]
    assert process.terminated is True
    assert process.waits == [3.0]
    assert process.stderr.closed


def test_native_heartbeats_are_supervision_only_not_user_diagnostics(monkeypatch):
    real_popen = subprocess.Popen
    reported: list[str] = []
    script = (
        "import json, sys, time; "
        "print(json.dumps({'message_type':'metadata','data':"
        "{'sample_rate':48000,'channels_per_frame':2,'bits_per_channel':32,"
        "'encoding':'pcm_f32le','protocol_version':2}}), file=sys.stderr, flush=True); "
        "print(json.dumps({'message_type':'heartbeat','data':"
        "{'producer_sequence':7}}), file=sys.stderr, flush=True); "
        "sys.stdout.buffer.write(b'\\x00' * 4096); sys.stdout.buffer.flush(); "
        "time.sleep(60)"
    )

    def fake_popen(_command, **kwargs):
        return real_popen([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(audio, "audiotee_path", lambda: Path(sys.executable))
    monkeypatch.setattr(audio.subprocess, "Popen", fake_popen)

    capture = audio.start_chrome_audio_capture(
        [1234],
        ready_timeout=1.0,
        on_stderr=reported.append,
    )
    try:
        tail = list(capture.stderr_tail or ())
        assert any('"message_type": "metadata"' in line for line in tail)
        assert all('"message_type": "heartbeat"' not in line for line in tail)
        assert all('"message_type": "heartbeat"' not in line for line in reported)
    finally:
        audio.stop_audio_capture(capture)


def test_incompatible_live_protocol_reaps_helper_before_handoff(monkeypatch):
    real_popen = subprocess.Popen
    spawned = []
    script = (
        "import json, sys, time; "
        "print(json.dumps({'message_type':'metadata','data':"
        "{'sample_rate':48000,'channels_per_frame':2,'bits_per_channel':32,"
        "'encoding':'pcm_f32le','protocol_version':1}}), file=sys.stderr, flush=True); "
        "sys.stdout.buffer.write(b'\\x00' * 4096); sys.stdout.buffer.flush(); "
        "time.sleep(60)"
    )

    def fake_popen(_command, **kwargs):
        process = real_popen([sys.executable, "-c", script], **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(audio, "audiotee_path", lambda: Path(sys.executable))
    monkeypatch.setattr(audio.subprocess, "Popen", fake_popen)

    with pytest.raises(audio.AudioCaptureCandidateError, match="protocol metadata"):
        audio.start_chrome_audio_capture([1234], ready_timeout=1.0)

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
