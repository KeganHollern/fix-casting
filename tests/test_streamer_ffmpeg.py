"""ffmpeg lifecycle tests, with one explicitly marked real-process check."""

import shutil
import threading
import time
from pathlib import Path

import pytest

import cast_tab.streamer as streamer_module
from cast_tab.stats import PipelineStats
from cast_tab.streamer import HLSStreamer

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


@pytest.mark.slow
@requires_ffmpeg
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


class _DeadFfmpeg:
    stdin = None

    def poll(self):
        return 1

    def stderr_text(self, *, join_timeout=1.0):
        del join_timeout
        return "simulated permanent encoder failure"

    def kill(self, *, graceful=False):
        del graceful


def test_persistent_ffmpeg_failure_opens_circuit_without_restart_storm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(streamer_module, "FFMPEG_RESTART_MAX_FAILURES", 4)
    monkeypatch.setattr(streamer_module, "FFMPEG_RESTART_BASE_DELAY_S", 0.0)
    monkeypatch.setattr(streamer_module, "FFMPEG_RESTART_MAX_DELAY_S", 0.0)

    streamer = HLSStreamer(width=320, height=240, fps=30, work_dir=tmp_path)
    spawned: list[_DeadFfmpeg] = []

    def spawn_dead_ffmpeg() -> None:
        process = _DeadFfmpeg()
        spawned.append(process)
        streamer._ffmpeg = process  # type: ignore[assignment]
        # _relaunch_ffmpeg clears the old queue. Feed one frame to exercise
        # each newly spawned process without involving the paced sampler.
        streamer._queue.put(b"frame")

    monkeypatch.setattr(streamer, "_start_ffmpeg", spawn_dead_ffmpeg)
    streamer._ffmpeg = _DeadFfmpeg()  # type: ignore[assignment]
    streamer._start_writer_thread()
    streamer._queue.put(b"frame")

    assert streamer._writer_thread is not None
    streamer._writer_thread.join(timeout=1)

    assert not streamer._writer_thread.is_alive()
    assert len(spawned) == streamer_module.FFMPEG_RESTART_MAX_FAILURES - 1
    assert streamer.fatal_error is not None
    assert "4 consecutive times" in str(streamer.fatal_error)
    with pytest.raises(RuntimeError, match="encoder recovery stopped"):
        streamer.raise_if_failed()

    # Cleanup remains safe after a background failure set the stop event.
    streamer.stop()


class _BlockingStdin:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.unblocked = threading.Event()

    def write(self, _frame: bytes) -> None:
        self.entered.set()
        self.unblocked.wait(timeout=10)
        raise BrokenPipeError("ffmpeg was killed")

    def flush(self) -> None:
        pass


class _WedgedFfmpeg:
    def __init__(self) -> None:
        self.stdin = _BlockingStdin()
        self.killed = False

    def poll(self):
        return None if not self.killed else -15

    def stderr_text(self, *, join_timeout=1.0):
        del join_timeout
        return ""

    def kill(self, *, graceful=False):
        del graceful
        self.killed = True
        self.stdin.unblocked.set()


def test_stop_kills_ffmpeg_before_joining_wedged_writer(tmp_path: Path):
    streamer = HLSStreamer(width=320, height=240, fps=30, work_dir=tmp_path)
    process = _WedgedFfmpeg()
    streamer._ffmpeg = process  # type: ignore[assignment]
    streamer._start_writer_thread()
    streamer._queue.put(b"frame")
    assert process.stdin.entered.wait(timeout=1)

    started = time.monotonic()
    streamer.stop()
    elapsed = time.monotonic() - started

    assert process.killed
    assert streamer._writer_thread is not None
    assert not streamer._writer_thread.is_alive()
    assert elapsed < 1.0


def test_audio_offset_is_clamped_for_storage_filter_and_return_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    streamer = HLSStreamer(
        width=320,
        height=240,
        audio_fd=123,
        audio_offset_ms=99_000,
        work_dir=tmp_path,
    )
    assert streamer.audio_offset_ms == streamer_module.MAX_AUTO_AV_OFFSET_MS
    assert streamer._audio_delay_filter_args() == [
        "-af",
        f"adelay={streamer_module.MAX_AUTO_AV_OFFSET_MS}:all=1",
    ]

    relaunched: list[bool] = []
    monkeypatch.setattr(
        streamer,
        "_relaunch_ffmpeg",
        lambda **_kwargs: relaunched.append(True),
    )
    assert streamer.set_audio_offset_ms(-100) == 0
    assert streamer.audio_offset_ms == 0
    assert relaunched == [True]

    assert streamer.set_audio_offset_ms(99_000) == streamer_module.MAX_AUTO_AV_OFFSET_MS
    assert streamer.audio_offset_ms == streamer_module.MAX_AUTO_AV_OFFSET_MS
    assert relaunched == [True, True]
    streamer.stop()


def test_start_polls_external_health_while_waiting_for_first_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    streamer = HLSStreamer(work_dir=tmp_path)
    failure = RuntimeError("browser died before its first frame")
    monkeypatch.setattr(streamer_module.shutil, "which", lambda _name: "/fake/ffmpeg")

    with pytest.raises(RuntimeError, match="browser died before its first frame") as caught:
        streamer.start(health_check=lambda: (_ for _ in ()).throw(failure))

    assert caught.value is failure
    assert streamer._ffmpeg is None
    streamer.stop()


def test_wait_until_ready_polls_external_health(tmp_path: Path):
    streamer = HLSStreamer(work_dir=tmp_path)
    failure = RuntimeError("browser died before the first HLS segment")

    with pytest.raises(RuntimeError, match="browser died before the first HLS segment"):
        streamer.wait_until_ready(
            timeout=10,
            health_check=lambda: (_ for _ in ()).throw(failure),
        )
    streamer.stop()


class _OneFrameFfmpeg:
    def __init__(self) -> None:
        self.stdin = self
        self.alive = True

    def poll(self):
        return None if self.alive else 1

    def write(self, _frame: bytes) -> None:
        pass

    def flush(self) -> None:
        self.alive = False

    def stderr_text(self, *, join_timeout=1.0):
        del join_timeout
        return "dies after one accepted frame"

    def kill(self, *, graceful=False):
        del graceful
        self.alive = False


def test_one_accepted_frame_does_not_reset_restart_circuit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(streamer_module, "FFMPEG_RESTART_MAX_FAILURES", 4)
    monkeypatch.setattr(streamer_module, "FFMPEG_RESTART_BASE_DELAY_S", 0.0)
    monkeypatch.setattr(streamer_module, "FFMPEG_RESTART_MAX_DELAY_S", 0.0)
    monkeypatch.setattr(streamer_module, "FFMPEG_RESTART_STABLE_S", 60.0)

    streamer = HLSStreamer(width=320, height=240, fps=30, work_dir=tmp_path)
    spawned: list[_OneFrameFfmpeg] = []

    def spawn_short_lived_ffmpeg() -> None:
        process = _OneFrameFfmpeg()
        spawned.append(process)
        streamer._ffmpeg = process  # type: ignore[assignment]
        streamer._ffmpeg_generation += 1
        streamer._ffmpeg_started_at = time.monotonic()
        # The first frame is accepted; the second observes process death.
        streamer._queue.put(b"first")
        streamer._queue.put(b"second")

    monkeypatch.setattr(streamer, "_start_ffmpeg", spawn_short_lived_ffmpeg)
    spawn_short_lived_ffmpeg()
    streamer._start_writer_thread()

    assert streamer._writer_thread is not None
    streamer._writer_thread.join(timeout=1)
    assert not streamer._writer_thread.is_alive()
    assert len(spawned) == streamer_module.FFMPEG_RESTART_MAX_FAILURES
    assert streamer.fatal_error is not None
    assert "encoder recovery stopped" in str(streamer.fatal_error)
    streamer.stop()


def test_health_check_recovers_wedged_writes_then_opens_circuit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(streamer_module, "FFMPEG_RESTART_MAX_FAILURES", 3)
    monkeypatch.setattr(streamer_module, "FFMPEG_RESTART_BASE_DELAY_S", 0.0)
    monkeypatch.setattr(streamer_module, "FFMPEG_RESTART_MAX_DELAY_S", 0.0)
    monkeypatch.setattr(streamer_module, "FFMPEG_WRITE_STALL_S", 0.0)

    streamer = HLSStreamer(width=320, height=240, fps=30, work_dir=tmp_path)
    spawned: list[_WedgedFfmpeg] = []

    def spawn_wedged_ffmpeg() -> None:
        process = _WedgedFfmpeg()
        spawned.append(process)
        streamer._ffmpeg = process  # type: ignore[assignment]
        streamer._ffmpeg_generation += 1
        streamer._ffmpeg_started_at = time.monotonic()
        streamer._queue.put(b"frame")

    monkeypatch.setattr(streamer, "_start_ffmpeg", spawn_wedged_ffmpeg)
    spawn_wedged_ffmpeg()
    streamer._start_writer_thread()

    for failure_number in range(1, streamer_module.FFMPEG_RESTART_MAX_FAILURES + 1):
        process = spawned[-1]
        assert process.stdin.entered.wait(timeout=1)
        if failure_number < streamer_module.FFMPEG_RESTART_MAX_FAILURES:
            streamer.raise_if_failed()
        else:
            with pytest.raises(RuntimeError, match="encoder recovery stopped"):
                streamer.raise_if_failed()

    assert len(spawned) == streamer_module.FFMPEG_RESTART_MAX_FAILURES
    assert streamer.fatal_error is not None
    streamer.stop()


def test_stop_cannot_return_before_concurrent_start_publishes_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    entered_spawn = threading.Event()
    release_spawn = threading.Event()
    stop_returned = threading.Event()
    events: list[str] = []

    class Process:
        stdin = None

        def kill(self, *, graceful=False):
            del graceful
            events.append("ffmpeg-stopped")

    class HTTP:
        port = 12345

        def __init__(self, *_args):
            events.append("http-created")

        def start(self):
            events.append("http-started")

        def stop(self):
            events.append("http-stopped")

        def raise_if_failed(self):
            pass

    monkeypatch.setattr(streamer_module.shutil, "which", lambda _name: "/fake/ffmpeg")
    monkeypatch.setattr(streamer_module, "HLSHTTPServer", HTTP)
    streamer = HLSStreamer(work_dir=tmp_path)
    streamer.publish_frame(b"frame")

    def spawn() -> None:
        entered_spawn.set()
        release_spawn.wait(timeout=2)
        streamer._ffmpeg = Process()  # type: ignore[assignment]
        streamer._ffmpeg_generation += 1
        streamer._ffmpeg_started_at = time.monotonic()
        events.append("ffmpeg-started")

    monkeypatch.setattr(streamer, "_start_ffmpeg", spawn)
    monkeypatch.setattr(
        streamer, "_start_sampler_thread", lambda: events.append("sampler-started")
    )
    monkeypatch.setattr(
        streamer, "_start_writer_thread", lambda: events.append("writer-started")
    )

    start_thread = threading.Thread(target=streamer.start)
    stop_thread = threading.Thread(
        target=lambda: (streamer.stop(), stop_returned.set())
    )
    start_thread.start()
    assert entered_spawn.wait(timeout=1)
    stop_thread.start()
    assert not stop_returned.wait(timeout=0.05)
    release_spawn.set()
    start_thread.join(timeout=1)
    stop_thread.join(timeout=1)

    assert not start_thread.is_alive()
    assert not stop_thread.is_alive()
    assert stop_returned.is_set()
    assert streamer._ffmpeg is None
    assert streamer._http is None
    assert "ffmpeg-started" in events
    assert "ffmpeg-stopped" in events
    with pytest.raises(RuntimeError, match="already been started or stopped"):
        streamer.start()


def test_stop_continues_http_and_workdir_cleanup_after_ffmpeg_failure():
    streamer = HLSStreamer(width=320, height=240)
    work_dir = streamer.work_dir
    events: list[str] = []

    class BadProcess:
        def kill(self, *, graceful=False):
            del graceful
            events.append("ffmpeg-stop")
            raise OSError("cannot kill ffmpeg")

    class HTTP:
        def stop(self):
            events.append("http-stop")

    streamer._ffmpeg = BadProcess()  # type: ignore[assignment]
    streamer._http = HTTP()  # type: ignore[assignment]

    with pytest.raises(OSError, match="cannot kill ffmpeg"):
        streamer.stop()

    assert events == ["ffmpeg-stop", "http-stop"]
    assert not work_dir.exists()
    assert streamer._http is None


def test_streamer_health_delegates_to_http_server(tmp_path: Path):
    streamer = HLSStreamer(work_dir=tmp_path)
    failure = OSError("HTTP worker died")

    class HTTP:
        def raise_if_failed(self):
            raise failure

        def stop(self):
            pass

    streamer._http = HTTP()  # type: ignore[assignment]
    with pytest.raises(OSError, match="HTTP worker died") as caught:
        streamer.raise_if_failed()
    assert caught.value is failure
    streamer.stop()


def test_recovery_does_not_replace_new_generation_during_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class GateEvent:
        def __init__(self) -> None:
            self.inner = threading.Event()
            self.waiting = threading.Event()
            self.release = threading.Event()

        def is_set(self):
            return self.inner.is_set()

        def set(self):
            self.inner.set()

        def wait(self, _timeout=None):
            self.waiting.set()
            self.release.wait(timeout=1)
            return self.inner.is_set()

    class Process(_DeadFfmpeg):
        def __init__(self) -> None:
            self.killed = False

        def kill(self, *, graceful=False):
            del graceful
            self.killed = True

    monkeypatch.setattr(streamer_module, "FFMPEG_RESTART_BASE_DELAY_S", 1.0)
    streamer = HLSStreamer(work_dir=tmp_path)
    gate = GateEvent()
    streamer._stopped = gate  # type: ignore[assignment]
    old = Process()
    new = Process()
    streamer._ffmpeg = old  # type: ignore[assignment]
    streamer._ffmpeg_generation = 7
    result: list[bool] = []

    recovery = threading.Thread(
        target=lambda: result.append(streamer._recover_ffmpeg(old, 7, None))
    )
    recovery.start()
    assert gate.waiting.wait(timeout=1)
    with streamer._ffmpeg_lock:
        old.kill()
        streamer._ffmpeg = new  # type: ignore[assignment]
        streamer._ffmpeg_generation = 8
        streamer._consecutive_ffmpeg_failures = 0
    gate.release.set()
    recovery.join(timeout=1)

    assert result == [True]
    assert not new.killed
    streamer.stop()
