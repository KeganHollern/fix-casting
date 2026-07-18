"""ffmpeg lifecycle tests, with one explicitly marked real-process check."""

import shutil
import threading
import time
from pathlib import Path

import pytest

import cast_tab.streamer as streamer_module
from cast_tab.stats import PipelineStats
from cast_tab.streamer import HLSStreamer

requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.mark.slow
@requires_ffmpeg
def test_stderr_drain_records_encoder_errors(tmp_path: Path):
    """Garbage MJPEG makes ffmpeg emit stderr; the drain must surface it in
    stats (and, structurally, keep the pipe from filling and stalling ffmpeg)."""
    stats = PipelineStats(target_fps=30.0)
    s = HLSStreamer(width=320, height=240, fps=30, buffered=False, work_dir=tmp_path, stats=stats)
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


def _publish_raw_hls_fixture(
    work_dir: Path,
    *segment_names: str,
    media_sequence: int = 0,
    discontinuity_before: set[int] | None = None,
) -> None:
    discontinuity_before = discontinuity_before or set()
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        "#EXT-X-TARGETDURATION:2",
        f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}",
    ]
    for index, name in enumerate(segment_names):
        if index in discontinuity_before:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.extend(["#EXTINF:2.0,", name])
        (work_dir / name).write_bytes(b"segment")
    (work_dir / "stream.m3u8").write_text("\n".join(lines) + "\n")


def test_hls_spawn_namespaces_are_unique_and_epochs_only_advance_after_publish(
    tmp_path: Path,
):
    streamer = HLSStreamer(work_dir=tmp_path)

    initial_pattern, append = streamer._prepare_hls_output()
    assert initial_pattern.endswith("seg-e000000-a000000-%09d.ts")
    assert not append

    first = "seg-e000000-a000000-000000000.ts"
    _publish_raw_hls_fixture(tmp_path, first)
    _records, next_sequence = streamer._read_hls_restart_state()
    assert next_sequence == 1
    next_pattern, append = streamer._prepare_hls_output()
    assert next_pattern.endswith("seg-e000001-a000001-%09d.ts")
    assert append

    # Attempt 1 published nothing. Its replacement gets a unique URI namespace
    # but reuses pending timeline epoch 1, avoiding a phantom discontinuity.
    retry_pattern, append = streamer._prepare_hls_output()
    assert retry_pattern.endswith("seg-e000001-a000002-%09d.ts")
    assert append
    streamer.stop()


def test_hls_restart_refuses_missing_or_corrupt_published_playlist(tmp_path: Path):
    streamer = HLSStreamer(work_dir=tmp_path)
    streamer._hls_ever_published = True

    with pytest.raises(RuntimeError, match="published playlist is missing"):
        streamer._prepare_hls_output()

    (tmp_path / "stream.m3u8").write_text("not hls\n")
    with pytest.raises(RuntimeError, match="malformed playlist header"):
        streamer._prepare_hls_output()
    streamer.stop()


def test_hls_restart_validates_epoch_boundary_and_continues_media_sequence(
    tmp_path: Path,
):
    streamer = HLSStreamer(work_dir=tmp_path)
    streamer._hls_current_attempt = 1
    streamer._hls_attempt = 1
    streamer._hls_timeline_epoch = 1
    first = "seg-e000000-a000000-000000008.ts"
    second = "seg-e000001-a000001-000000009.ts"
    _publish_raw_hls_fixture(
        tmp_path,
        first,
        second,
        media_sequence=8,
        discontinuity_before={1},
    )

    _records, next_sequence = streamer._read_hls_restart_state()
    assert next_sequence == 10
    pattern, append = streamer._prepare_hls_output()
    assert pattern.endswith("seg-e000002-a000002-%09d.ts")
    assert append
    streamer.stop()


def test_live_hls_check_rejects_media_sequence_renumbering(tmp_path: Path):
    streamer = HLSStreamer(work_dir=tmp_path)
    name = "seg-e000000-a000000-000000004.ts"
    _publish_raw_hls_fixture(tmp_path, name, media_sequence=4)
    streamer._check_hls_media_sequence_identity()

    # Reusing the URI at a different media sequence violates HLS identity and
    # was the subtle failure caused by combining append_list with start_number.
    _publish_raw_hls_fixture(tmp_path, name, media_sequence=9)
    streamer._hls_identity_mtime_ns = -1
    with pytest.raises(RuntimeError, match="identity changed"):
        streamer._check_hls_media_sequence_identity()

    assert streamer.fatal_error is not None
    streamer.stop()


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


class _RecordingFfmpeg:
    def __init__(self) -> None:
        self.stdin = self
        self.writes: list[bytes] = []
        self.killed = False

    def poll(self):
        return -15 if self.killed else None

    def write(self, frame: bytes) -> int:
        self.writes.append(frame)
        return len(frame)

    def flush(self) -> None:
        pass

    def stderr_text(self, *, join_timeout=1.0):
        del join_timeout
        return ""

    def kill(self, *, graceful=False):
        del graceful
        self.killed = True


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


def test_wait_until_ready_requires_three_segment_startup_runway(tmp_path: Path):
    streamer = HLSStreamer(work_dir=tmp_path)
    first = "seg-e000000-a000000-000000000.ts"
    _publish_raw_hls_fixture(tmp_path, first)

    with pytest.raises(TimeoutError, match="become ready"):
        streamer.wait_until_ready(timeout=0.05)

    second = "seg-e000000-a000000-000000001.ts"
    third = "seg-e000000-a000000-000000002.ts"
    _publish_raw_hls_fixture(tmp_path, first, second, third)
    streamer.wait_until_ready(timeout=0.2)
    streamer.stop()


class _OneFrameFfmpeg:
    def __init__(self) -> None:
        self.stdin = self
        self.alive = True

    def poll(self):
        return None if self.alive else 1

    def write(self, frame: bytes) -> int:
        return len(frame)

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


def test_queue_overflow_reanchors_before_post_gap_frame_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    stats = PipelineStats(target_fps=1.0)
    streamer = HLSStreamer(
        width=320,
        height=240,
        fps=1,
        work_dir=tmp_path,
        stats=stats,
    )
    old = _WedgedFfmpeg()
    replacements: list[_RecordingFfmpeg] = []
    streamer._ffmpeg = old  # type: ignore[assignment]
    streamer._ffmpeg_generation = 1
    streamer._ffmpeg_started_at = time.monotonic()

    def spawn_replacement() -> None:
        process = _RecordingFfmpeg()
        replacements.append(process)
        streamer._ffmpeg = process  # type: ignore[assignment]
        streamer._ffmpeg_generation += 1
        streamer._ffmpeg_started_at = time.monotonic()

    monkeypatch.setattr(streamer, "_start_ffmpeg", spawn_replacement)
    streamer._start_writer_thread()
    streamer._queue.put(b"write-blocker")
    assert old.stdin.entered.wait(timeout=1)

    streamer._enqueue_frame(b"queued-before-gap")
    streamer._enqueue_frame(b"post-gap-must-not-use-old-generation")
    assert streamer._pending_timeline_resync == (
        1,
        "video queue overflow dropped a sampled CFR frame",
    )

    # Health polling is the fallback while the writer is blocked. Killing the
    # old process unblocks it; the queued post-gap frame is discarded at the
    # boundary rather than entering either side with the wrong PTS anchor.
    streamer.raise_if_failed()
    assert old.killed
    assert len(replacements) == 1
    streamer._enqueue_frame(b"fresh-generation-frame")

    deadline = time.monotonic() + 1
    while not replacements[0].writes and time.monotonic() < deadline:
        time.sleep(0.01)

    assert replacements[0].writes == [b"fresh-generation-frame"]
    snap = stats.snapshot(1.0)
    assert snap.resyncs == 1
    assert snap.ffmpeg_restarts == 1
    assert snap.queue_dropped >= 2
    streamer.stop()


def test_writer_rejects_frame_popped_before_concurrent_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    streamer = HLSStreamer(width=320, height=240, fps=30, work_dir=tmp_path)
    old = _RecordingFfmpeg()
    new = _RecordingFfmpeg()
    streamer._ffmpeg = old  # type: ignore[assignment]
    streamer._ffmpeg_generation = 4
    streamer._ffmpeg_started_at = time.monotonic()
    streamer._queue.put(b"stale-popped-frame")

    popped = threading.Event()
    release = threading.Event()
    real_get = streamer._queue.get
    first_get = True

    def gated_get(stopped):
        nonlocal first_get
        frame = real_get(stopped)
        if first_get:
            first_get = False
            popped.set()
            assert release.wait(timeout=1)
        return frame

    monkeypatch.setattr(streamer._queue, "get", gated_get)

    def spawn_new() -> None:
        streamer._ffmpeg = new  # type: ignore[assignment]
        streamer._ffmpeg_generation += 1
        streamer._ffmpeg_started_at = time.monotonic()

    monkeypatch.setattr(streamer, "_start_ffmpeg", spawn_new)
    streamer._start_writer_thread()
    assert popped.wait(timeout=1)

    streamer._relaunch_ffmpeg(reset_failures=True)
    streamer._enqueue_frame(b"fresh-frame")
    release.set()

    deadline = time.monotonic() + 1
    while not new.writes and time.monotonic() < deadline:
        time.sleep(0.01)

    assert old.writes == []
    assert new.writes == [b"fresh-frame"]
    streamer.stop()


def test_sampler_rejects_local_frame_captured_before_relaunch(tmp_path: Path):
    stats = PipelineStats(target_fps=30.0)
    streamer = HLSStreamer(work_dir=tmp_path, stats=stats)
    streamer._ffmpeg = _RecordingFfmpeg()  # type: ignore[assignment]
    streamer._ffmpeg_generation = 12

    assert not streamer._enqueue_frame(
        b"stale-local-frame",
        sampled_for_generation=11,
    )
    stopped = threading.Event()
    stopped.set()
    assert streamer._queue.get(stopped) is None
    assert stats.snapshot(1.0).dropped_total == 1
    streamer.stop()


def test_sampler_clock_skip_requests_generation_boundary_and_counts_loss(
    tmp_path: Path,
):
    stats = PipelineStats(target_fps=30.0)
    streamer = HLSStreamer(work_dir=tmp_path, stats=stats)
    streamer._ffmpeg = _RecordingFfmpeg()  # type: ignore[assignment]
    streamer._ffmpeg_generation = 3

    assert not streamer._handle_sampler_clock_gap(1.0, 1 / 30)
    assert streamer._handle_sampler_clock_gap(6.0, 1 / 30)
    assert streamer._pending_timeline_resync == (
        3,
        "sampler skipped 180 elapsed CFR ticks",
    )

    snap = stats.snapshot(1.0)
    assert snap.resyncs == 1
    assert snap.queue_dropped == 180
    assert snap.resync_last_reason == "sampler skipped 180 elapsed CFR ticks"
    streamer.stop()


def test_stale_write_timing_cannot_reset_fresh_generation_backpressure(
    tmp_path: Path,
):
    streamer = HLSStreamer(work_dir=tmp_path)
    streamer._ffmpeg = _RecordingFfmpeg()  # type: ignore[assignment]
    streamer._ffmpeg_generation = 8
    streamer._backpressure_generation = 8
    streamer._backpressure_started_at = 123.0

    streamer._note_encode_backpressure(0.0, generation=7)

    assert streamer._backpressure_generation == 8
    assert streamer._backpressure_started_at == 123.0
    streamer.stop()


def test_stale_resync_request_cannot_kill_fresh_generation(tmp_path: Path):
    streamer = HLSStreamer(work_dir=tmp_path)
    old = _RecordingFfmpeg()
    fresh = _RecordingFfmpeg()
    streamer._ffmpeg = old  # type: ignore[assignment]
    streamer._ffmpeg_generation = 7

    assert streamer._request_timeline_resync(
        "old generation overflow",
        expected_generation=7,
    )

    # Model a concurrent manual relaunch after the request was queued but
    # before the health/writer path consumes it.  Its generation tag makes the
    # request already satisfied, rather than authority to kill the new process.
    with streamer._ffmpeg_lock:
        streamer._ffmpeg = fresh  # type: ignore[assignment]
        streamer._ffmpeg_generation = 8

    assert not streamer._process_pending_timeline_resync()
    assert streamer._pending_timeline_resync is None
    assert not fresh.killed
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

        def __init__(self, *_args, **_kwargs):
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
    monkeypatch.setattr(streamer, "_start_sampler_thread", lambda: events.append("sampler-started"))
    monkeypatch.setattr(streamer, "_start_writer_thread", lambda: events.append("writer-started"))

    start_thread = threading.Thread(target=streamer.start)
    stop_thread = threading.Thread(target=lambda: (streamer.stop(), stop_returned.set()))
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
