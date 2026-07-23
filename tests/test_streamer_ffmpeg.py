"""ffmpeg lifecycle tests, with one explicitly marked real-process check."""

import os
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.parametrize(("width", "height"), [(319, 240), (320, 239)])
def test_streamer_rejects_odd_yuv420p_dimensions(
    tmp_path: Path,
    width: int,
    height: int,
):
    with pytest.raises(ValueError, match="positive even numbers"):
        HLSStreamer(width=width, height=height, work_dir=tmp_path)


def test_deferred_audio_is_accepted_only_after_streamer_is_running(tmp_path: Path):
    streamer = HLSStreamer(work_dir=tmp_path)
    assert not streamer.accepts_deferred_audio
    streamer._lifecycle_state = "running"
    assert streamer.accepts_deferred_audio
    streamer.stop()
    assert not streamer.accepts_deferred_audio


def test_explicit_work_dir_preserved(tmp_path: Path):
    s = HLSStreamer(width=320, height=240, fps=30, work_dir=tmp_path)
    s.stop()
    assert tmp_path.exists()


def test_receiver_delivery_observation_is_client_specific_and_time_bounded(
    tmp_path: Path, monkeypatch
):
    streamer = HLSStreamer(work_dir=tmp_path, buffered=True)
    calls = []
    snapshot = SimpleNamespace(
        segment_responses=4,
        active_segment_requests=0,
        oldest_active_segment_age_s=None,
        latest_segment_completed_at=98.0,
    )

    class HTTP:
        def client_delivery_snapshot(self, host):
            calls.append(host)
            return snapshot

    streamer._http = HTTP()
    monkeypatch.setattr(streamer_module.time, "time", lambda: 100.0)

    assert streamer.receiver_delivery_observation("192.0.2.10") == (4, True)
    assert calls == ["192.0.2.10"]

    snapshot.latest_segment_completed_at = 90.0
    assert streamer.receiver_delivery_observation("192.0.2.10") == (4, False)


def test_receiver_routed_playlist_url_is_frozen_once(tmp_path: Path, monkeypatch):
    streamer = HLSStreamer(work_dir=tmp_path)
    streamer._http = SimpleNamespace(port=4321)
    routed_hosts = []

    def routed_ip(host):
        routed_hosts.append(host)
        return SimpleNamespace(
            local_ip="192.168.50.12",
            peer_hosts=("192.168.50.80",),
        )

    monkeypatch.setattr(streamer_module, "get_receiver_route", routed_ip)

    url = streamer.configure_receiver("192.168.50.80")

    assert url == "http://192.168.50.12:4321/stream.m3u8"
    assert streamer.playlist_url == url
    assert streamer.configure_receiver("192.168.50.80") == url
    assert routed_hosts == ["192.168.50.80"]
    with pytest.raises(RuntimeError, match="already configured"):
        streamer.configure_receiver("192.168.50.81")


def test_hostname_receiver_uses_numeric_http_peers_for_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    streamer = HLSStreamer(work_dir=tmp_path, buffered=True)
    snapshots = {
        "192.0.2.10": SimpleNamespace(
            segment_responses=7,
            active_segment_requests=0,
            oldest_active_segment_age_s=None,
            latest_segment_completed_at=98.0,
        ),
        "192.0.2.11": SimpleNamespace(
            segment_responses=0,
            active_segment_requests=0,
            oldest_active_segment_age_s=None,
            latest_segment_completed_at=None,
        ),
    }
    calls: list[str] = []
    streamer._http = SimpleNamespace(  # type: ignore[assignment]
        port=4321,
        client_delivery_snapshot=lambda host: (
            calls.append(host) or snapshots[host]
        ),
    )
    monkeypatch.setattr(
        streamer_module,
        "get_receiver_route",
        lambda _host: SimpleNamespace(
            local_ip="192.0.2.50",
            peer_hosts=("192.0.2.10", "192.0.2.11"),
        ),
    )
    monkeypatch.setattr(streamer_module.time, "time", lambda: 100.0)

    assert streamer.configure_receiver("den-tv.local.") == (
        "http://192.0.2.50:4321/stream.m3u8"
    )
    assert streamer.receiver_delivery_observation() == (7, True)
    assert calls == ["192.0.2.10", "192.0.2.11"]


def test_hls_stats_count_playlist_and_measure_publish_and_tv_delivery(tmp_path: Path):
    stats = PipelineStats(target_fps=30.0)
    streamer = HLSStreamer(work_dir=tmp_path, buffered=True, stats=stats)
    grace = "seg-e000000-a000000-000000000.ts"
    active_one = "seg-e000000-a000000-000000001.ts"
    active_two = "seg-e000000-a000000-000000002.ts"
    _publish_raw_hls_fixture(tmp_path, active_one, active_two)
    (tmp_path / grace).write_bytes(b"grace")
    base = time.time() - 5.0
    for index, name in enumerate((grace, active_one, active_two)):
        os.utime(tmp_path / name, (base + index * 2, base + index * 2))
    # An orphan/deletion-grace file newer than the playlist must not make the
    # receiver-visible stream age or publish cadence look healthier.
    orphan_time = time.time()
    os.utime(tmp_path / grace, (orphan_time, orphan_time))

    delivery = SimpleNamespace(
        segment_requests=4,
        segment_responses=4,
        error_requests=1,
        active_requests=0,
        active_segment_requests=0,
        oldest_active_segment_age_s=None,
        latest_segment_bytes=1_000_000,
        latest_segment_duration_s=0.1,
        latest_segment_throughput_bps=10_000_000.0,
        latest_segment_completed_at=time.time() - 0.25,
        latest_request_kind="segment",
        latest_request_completed_at=time.time(),
        latest_request_started_at=time.time() - 0.1,
    )
    streamer._http = SimpleNamespace(  # type: ignore[assignment]
        delivery_snapshot=lambda: delivery,
    )

    assert streamer.poll_hls_stats() == []
    snap = stats.snapshot(1.0)
    assert snap.hls_count == 2  # deletion-grace file is not a playlist entry
    assert snap.hls_age is not None and 0.75 <= snap.hls_age <= 2.0
    assert snap.hls_publish_count == 1
    assert snap.hls_publish_ms == 2000.0
    assert snap.hls_segment_requests == 4
    assert snap.hls_delivery_ms == 100.0
    assert snap.hls_delivery_mbps == 80.0
    assert snap.hls_delivery_idle_ms == pytest.approx(250.0, abs=25.0)
    assert snap.hls_delivery_seen
    assert snap.hls_delivery_errors == 1
    streamer._http = None
    streamer.stop()


def test_hls_stats_keep_an_older_active_segment_visible_after_playlist_request(
    tmp_path: Path,
):
    stats = PipelineStats(target_fps=30.0)
    streamer = HLSStreamer(work_dir=tmp_path, buffered=True, stats=stats)
    delivery = SimpleNamespace(
        segment_requests=1,
        segment_responses=0,
        error_requests=0,
        active_segment_requests=1,
        oldest_active_segment_age_s=1.25,
        latest_segment_bytes=0,
        latest_segment_duration_s=None,
        latest_segment_throughput_bps=None,
        latest_segment_completed_at=None,
        # A newer playlist request is the generic latest request. Segment-
        # specific active telemetry must still drive the dashboard.
        latest_request_kind="playlist",
        latest_request_completed_at=time.time(),
        latest_request_started_at=time.time() - 0.01,
    )
    streamer._http = SimpleNamespace(  # type: ignore[assignment]
        delivery_snapshot=lambda: delivery,
    )

    streamer.poll_hls_stats()
    snap = stats.snapshot(1.0)

    assert snap.hls_delivery_active == 1
    assert snap.hls_delivery_active_ms == 1250.0
    assert snap.hls_delivery_seen
    streamer._http = None
    streamer.stop()


def test_hls_stats_expose_receiver_that_never_fetches_segments(tmp_path: Path):
    stats = PipelineStats(target_fps=30.0)
    streamer = HLSStreamer(work_dir=tmp_path, buffered=True, stats=stats)
    delivery = SimpleNamespace(
        segment_requests=0,
        segment_responses=0,
        error_requests=0,
        active_segment_requests=0,
        oldest_active_segment_age_s=None,
        latest_segment_bytes=0,
        latest_segment_duration_s=None,
        latest_segment_throughput_bps=None,
        latest_segment_completed_at=None,
        latest_request_kind="playlist",
        latest_request_completed_at=time.time(),
        latest_request_started_at=time.time() - 0.01,
    )
    streamer._http = SimpleNamespace(  # type: ignore[assignment]
        delivery_snapshot=lambda: delivery,
    )
    streamer._hls_delivery_observed_at = time.time() - 5.0

    streamer.poll_hls_stats()
    snap = stats.snapshot(1.0)

    assert not snap.hls_delivery_seen
    assert snap.hls_delivery_idle_ms == pytest.approx(5000.0, abs=25.0)
    streamer._http = None
    streamer.stop()


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
    monkeypatch.setattr(
        streamer_module,
        "preferred_video_encoder",
        lambda: streamer_module.SOFTWARE_VIDEO_ENCODER,
    )

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


def test_videotoolbox_runtime_failure_falls_back_to_x264_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    commands: list[list[str]] = []
    processes: list[_RecordingFfmpeg] = []

    def process_factory(cmd, **_kwargs):
        commands.append(cmd)
        process = _RecordingFfmpeg()
        processes.append(process)
        return process

    monkeypatch.setattr(
        streamer_module,
        "preferred_video_encoder",
        lambda: streamer_module.HARDWARE_VIDEO_ENCODER,
    )
    monkeypatch.setattr(streamer_module, "FfmpegProcess", process_factory)
    monkeypatch.setattr(streamer_module, "FFMPEG_RESTART_BASE_DELAY_S", 0.0)
    monkeypatch.setattr(streamer_module, "FFMPEG_RESTART_MAX_DELAY_S", 0.0)

    streamer = HLSStreamer(fps=30, work_dir=tmp_path)
    streamer._start_ffmpeg()
    first = streamer._ffmpeg
    assert first is processes[0]
    assert commands[0][commands[0].index("-c:v") + 1] == "h264_videotoolbox"

    assert streamer._recover_ffmpeg(
        first,
        streamer._ffmpeg_generation,
        BrokenPipeError("hardware encoder rejected the frame"),
    )
    assert streamer._video_encoder == streamer_module.SOFTWARE_VIDEO_ENCODER
    assert commands[1][commands[1].index("-c:v") + 1] == "libx264"
    assert commands[1][commands[1].index("-level") + 1] == "4.1"

    # Manual/audio-offset relaunches must never silently opt back into the
    # runtime-broken hardware encoder during this cast.
    assert streamer._relaunch_ffmpeg(reset_failures=True)
    assert commands[2][commands[2].index("-c:v") + 1] == "libx264"
    assert sum("h264_videotoolbox" in command for command in commands) == 1
    streamer.stop()


def test_stale_hardware_failure_cannot_downgrade_concurrent_relaunch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    diagnostic_started = threading.Event()
    release_diagnostic = threading.Event()

    class SlowDiagnosticFfmpeg(_RecordingFfmpeg):
        def stderr_text(self, *, join_timeout=1.0):
            del join_timeout
            diagnostic_started.set()
            assert release_diagnostic.wait(timeout=1)
            return "stale VideoToolbox failure"

    monkeypatch.setattr(
        streamer_module,
        "preferred_video_encoder",
        lambda: streamer_module.HARDWARE_VIDEO_ENCODER,
    )
    streamer = HLSStreamer(fps=30, work_dir=tmp_path)
    failed = SlowDiagnosticFfmpeg()
    healthy = _RecordingFfmpeg()
    streamer._ffmpeg = failed  # type: ignore[assignment]
    streamer._ffmpeg_generation = 1
    starts: list[bool] = []

    def start_healthy() -> None:
        starts.append(True)
        streamer._ffmpeg = healthy  # type: ignore[assignment]
        streamer._ffmpeg_generation += 1
        streamer._ffmpeg_started_at = time.monotonic()

    monkeypatch.setattr(streamer, "_start_ffmpeg", start_healthy)
    results: list[bool] = []
    failures: list[BaseException] = []

    def recover_stale_failure() -> None:
        try:
            results.append(
                streamer._recover_ffmpeg(
                    failed,  # type: ignore[arg-type]
                    1,
                    BrokenPipeError("old hardware pipe broke"),
                )
            )
        except BaseException as exc:
            failures.append(exc)

    recovery = threading.Thread(target=recover_stale_failure)
    recovery.start()
    assert diagnostic_started.wait(timeout=1)

    assert streamer._relaunch_ffmpeg(expected_generation=1, reset_failures=True)
    assert streamer._ffmpeg is healthy
    release_diagnostic.set()
    recovery.join(timeout=1)

    assert not recovery.is_alive()
    assert failures == []
    assert results == [True]
    assert starts == [True]
    assert streamer._video_encoder == streamer_module.HARDWARE_VIDEO_ENCODER
    assert streamer._ffmpeg is healthy
    streamer.stop()


def test_video_demux_queue_is_bounded_to_one_second(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    commands: list[list[str]] = []
    process = _RecordingFfmpeg()

    def process_factory(cmd, **_kwargs):
        commands.append(cmd)
        return process

    monkeypatch.setattr(streamer_module, "FfmpegProcess", process_factory)
    monkeypatch.setattr(streamer_module, "video_encoder_args", lambda *_args, **_kwargs: [])
    streamer = HLSStreamer(fps=30, work_dir=tmp_path)
    streamer._start_ffmpeg()

    command = commands[0]
    queue_index = command.index("-thread_queue_size")
    assert command[queue_index + 1] == "30"
    assert streamer._ffmpeg_video_boundary == (1, None)
    streamer.stop()


def test_replacement_spawn_records_generation_specific_video_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    processes: list[_RecordingFfmpeg] = []

    def process_factory(_cmd, **_kwargs):
        process = _RecordingFfmpeg()
        processes.append(process)
        return process

    monkeypatch.setattr(streamer_module, "FfmpegProcess", process_factory)
    monkeypatch.setattr(streamer_module, "video_encoder_args", lambda *_args, **_kwargs: [])
    streamer = HLSStreamer(fps=30, work_dir=tmp_path)
    streamer._latest.publish(b"static-page", captured_at=time.monotonic())
    streamer._start_ffmpeg()
    streamer._kill_ffmpeg()

    before = time.monotonic()
    streamer._start_ffmpeg()
    after = time.monotonic()

    boundary_generation, boundary_at = streamer._ffmpeg_video_boundary
    assert boundary_generation == streamer._ffmpeg_generation == 2
    assert boundary_at is not None
    assert before <= boundary_at <= after
    held = streamer._latest.select(boundary_at, not_before=boundary_at)
    assert held is not None
    assert held.frame == b"static-page"
    assert held.captured_at == held.published_at == boundary_at
    streamer.stop()


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


def test_streamer_retains_ffmpeg_until_retry_confirms_cleanup(tmp_path: Path):
    streamer = HLSStreamer(work_dir=tmp_path)

    class FlakyFfmpeg:
        def __init__(self):
            self.kill_calls = 0

        def kill(self, *, graceful=False):
            del graceful
            self.kill_calls += 1
            if self.kill_calls == 1:
                raise TimeoutError("ffmpeg still alive")

    process = FlakyFfmpeg()
    streamer._ffmpeg = process  # type: ignore[assignment]

    with pytest.raises(TimeoutError, match="still alive"):
        streamer.stop()

    assert streamer._ffmpeg is process
    assert streamer._lifecycle_state == "stopping"

    streamer.stop()
    assert process.kill_calls == 2
    assert streamer._ffmpeg is None
    assert streamer._lifecycle_state == "stopped"


def test_streamer_retains_ffmpeg_after_repeated_cleanup_failure(tmp_path: Path):
    streamer = HLSStreamer(work_dir=tmp_path)

    class UnreapableFfmpeg:
        kill_calls = 0

        def kill(self, *, graceful=False):
            del graceful
            self.kill_calls += 1
            raise TimeoutError(f"ffmpeg cleanup attempt {self.kill_calls} failed")

    process = UnreapableFfmpeg()
    streamer._ffmpeg = process  # type: ignore[assignment]

    for attempt in (1, 2):
        with pytest.raises(TimeoutError, match=f"attempt {attempt} failed"):
            streamer.stop()

    assert process.kill_calls == 2
    assert streamer._ffmpeg is process
    assert streamer._lifecycle_state == "stopping"


@pytest.mark.parametrize(
    ("thread_attr", "stops_after", "message"),
    [
        ("_sampler_thread", 2, "sampler thread did not stop"),
        # Writer gets a short grace join and a final post-kill completion join
        # on its first pass, then exits during the second pass's grace join.
        ("_writer_thread", 3, "writer thread did not stop"),
    ],
)
def test_streamer_retries_live_worker_thread_join(
    tmp_path: Path,
    thread_attr: str,
    stops_after: int,
    message: str,
) -> None:
    streamer = HLSStreamer(work_dir=tmp_path)

    class SlowThread:
        def __init__(self):
            self.join_calls = 0

        def is_alive(self):
            return self.join_calls < stops_after

        def join(self, *, timeout):
            del timeout
            self.join_calls += 1

    thread = SlowThread()
    setattr(streamer, thread_attr, thread)

    with pytest.raises(TimeoutError, match=message):
        streamer.stop()

    assert getattr(streamer, thread_attr) is thread
    assert streamer._lifecycle_state == "stopping"

    streamer.stop()
    assert not thread.is_alive()
    assert streamer._lifecycle_state == "stopped"


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


def test_replace_audio_source_swaps_pipe_at_joint_timeline_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_format = streamer_module.AudioFormat(48_000, 2, "f32le", 4)
    new_format = streamer_module.AudioFormat(44_100, 2, "s16le", 2)
    streamer = HLSStreamer(
        width=320,
        height=240,
        audio_fd=11,
        audio_format=old_format,
        work_dir=tmp_path,
    )
    streamer._lifecycle_state = "running"
    streamer._ffmpeg = object()  # type: ignore[assignment]
    streamer._pending_timeline_resync = (0, "old boundary")
    events: list[object] = []

    def kill_ffmpeg(*, graceful=False):
        events.append(("kill", graceful))
        streamer._ffmpeg = None

    def clear_queue():
        events.append("clear")
        return 3

    def start_ffmpeg():
        events.append(("start", streamer.audio_fd, streamer.audio_format))
        streamer._ffmpeg = object()  # type: ignore[assignment]

    monkeypatch.setattr(streamer, "_kill_ffmpeg", kill_ffmpeg)
    monkeypatch.setattr(streamer._queue, "clear", clear_queue)
    monkeypatch.setattr(streamer, "_start_ffmpeg", start_ffmpeg)

    assert streamer.begin_audio_source_replacement()
    assert streamer.complete_audio_source_replacement(22, new_format)

    assert events == [
        ("kill", False),
        "clear",
        ("start", 22, new_format),
    ]
    assert streamer.audio_fd == 22
    assert streamer.audio_format == new_format
    assert streamer._pending_timeline_resync is None
    streamer._ffmpeg = None
    streamer.stop()


def test_replace_audio_source_during_startup_preserves_first_frame_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    new_format = streamer_module.AudioFormat(44_100, 2, "s16le", 2)
    streamer = HLSStreamer(audio_fd=11, work_dir=tmp_path)
    streamer._lifecycle_state = "starting"
    monkeypatch.setattr(
        streamer,
        "_start_ffmpeg",
        lambda: (_ for _ in ()).throw(AssertionError("spawned before first frame")),
    )

    assert streamer.begin_audio_source_replacement()
    assert streamer.complete_audio_source_replacement(22, new_format)
    assert streamer.audio_fd == 22
    assert streamer.audio_format == new_format
    streamer.stop()


def test_replace_audio_source_rejects_shutdown_without_mutating_input(
    tmp_path: Path,
) -> None:
    original_format = streamer_module.AudioFormat(48_000, 2, "f32le", 4)
    streamer = HLSStreamer(
        audio_fd=11,
        audio_format=original_format,
        work_dir=tmp_path,
    )
    streamer.stop()

    assert not streamer.begin_audio_source_replacement()
    assert streamer.audio_fd == 11
    assert streamer.audio_format == original_format


def test_audio_replacement_blocks_every_other_ffmpeg_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    streamer = HLSStreamer(audio_fd=11, work_dir=tmp_path)
    streamer._lifecycle_state = "running"
    streamer._ffmpeg = object()  # type: ignore[assignment]
    starts: list[int | None] = []

    def kill_ffmpeg(*, graceful=False):
        del graceful
        streamer._ffmpeg = None

    def start_ffmpeg():
        starts.append(streamer.audio_fd)
        streamer._ffmpeg = object()  # type: ignore[assignment]

    monkeypatch.setattr(streamer, "_kill_ffmpeg", kill_ffmpeg)
    monkeypatch.setattr(streamer, "_start_ffmpeg", start_ffmpeg)

    generation = streamer._ffmpeg_generation
    assert streamer.begin_audio_source_replacement()
    assert streamer.set_audio_offset_ms(25) == 25
    assert not streamer._relaunch_ffmpeg(expected_generation=generation)
    assert starts == []

    assert streamer.complete_audio_source_replacement(22)
    assert starts == [22]
    streamer._ffmpeg = None
    streamer.stop()


def test_audio_replacement_spawn_failure_is_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    streamer = HLSStreamer(audio_fd=11, work_dir=tmp_path)
    streamer._lifecycle_state = "running"
    streamer._ffmpeg = object()  # type: ignore[assignment]

    def kill_ffmpeg(*, graceful=False):
        del graceful
        streamer._ffmpeg = None

    monkeypatch.setattr(streamer, "_kill_ffmpeg", kill_ffmpeg)
    monkeypatch.setattr(
        streamer,
        "_start_ffmpeg",
        lambda: (_ for _ in ()).throw(OSError("spawn broke")),
    )

    assert streamer.begin_audio_source_replacement()
    with pytest.raises(OSError, match="spawn broke"):
        streamer.complete_audio_source_replacement(22)

    assert streamer._stopped.is_set()
    assert streamer.fatal_error is not None
    assert "committing replacement audio" in str(streamer.fatal_error)
    assert not streamer._audio_replacement_in_progress
    streamer.stop()


def test_reentrant_stop_during_audio_teardown_does_not_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    streamer = HLSStreamer(audio_fd=11, work_dir=tmp_path)
    streamer._lifecycle_state = "running"
    streamer._ffmpeg = object()  # type: ignore[assignment]
    kill_calls = 0

    def reentrant_kill(*, graceful=False):
        nonlocal kill_calls
        del graceful
        kill_calls += 1
        streamer._ffmpeg = None
        if kill_calls == 1:
            streamer.stop()

    monkeypatch.setattr(streamer, "_kill_ffmpeg", reentrant_kill)

    started = time.monotonic()
    assert not streamer.begin_audio_source_replacement()

    assert time.monotonic() - started < 1.0
    assert kill_calls == 2
    assert not streamer._audio_replacement_in_progress


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


def test_first_frame_wait_drains_more_than_audio_pipe_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    streamer = HLSStreamer(audio_fd=read_fd, work_dir=tmp_path)
    streamer.FIRST_FRAME_TIMEOUT_S = 2.0
    monkeypatch.setattr(streamer_module, "HEALTH_POLL_S", 0.0001)
    calls = 0

    def produce_audio() -> None:
        nonlocal calls
        calls += 1
        # 1024 writes are 64x this Mac's 64KiB pipe capacity. Without a
        # startup consumer, the 17th nonblocking write deterministically fails.
        os.write(write_fd, b"\0" * 4096)
        if calls >= 1024:
            streamer._first_frame.set()

    try:
        assert streamer._wait_for_first_frame(produce_audio)
        assert calls >= 1024
        assert streamer_module._pipe_bytes_available(read_fd) == 0
    finally:
        streamer.stop()
        os.close(read_fd)
        os.close(write_fd)


def test_first_frame_wait_drains_audio_fd_replaced_by_health_check(
    tmp_path: Path,
) -> None:
    old_read, old_write = os.pipe()
    new_read, new_write = os.pipe()
    streamer = HLSStreamer(audio_fd=old_read, work_dir=tmp_path)
    os.write(old_write, b"old pre-roll")
    health_calls = 0

    def replace_audio() -> None:
        nonlocal health_calls
        health_calls += 1
        if health_calls == 1:
            streamer.audio_fd = new_read
            os.write(new_write, b"new pre-roll")
            streamer._first_frame.set()

    try:
        assert streamer._wait_for_first_frame(replace_audio)
        assert streamer_module._pipe_bytes_available(old_read) == 0
        assert streamer_module._pipe_bytes_available(new_read) == 0
    finally:
        streamer.stop()
        for fd in (old_read, old_write, new_read, new_write):
            os.close(fd)


def test_final_audio_drain_precedes_ffmpeg_process_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"buffered-before-joint-boundary")
    observed: list[tuple[int, tuple[int, ...]]] = []
    process = _RecordingFfmpeg()

    def process_factory(_cmd, *, pass_fds=(), **_kwargs):
        observed.append(
            (streamer_module._pipe_bytes_available(read_fd), pass_fds)
        )
        return process

    monkeypatch.setattr(streamer_module, "FfmpegProcess", process_factory)
    monkeypatch.setattr(
        streamer_module, "video_encoder_args", lambda *_args, **_kwargs: []
    )
    streamer = HLSStreamer(audio_fd=read_fd, work_dir=tmp_path)
    try:
        streamer._start_ffmpeg()
        assert observed == [(0, (read_fd,))]
    finally:
        streamer.stop()
        os.close(read_fd)
        os.close(write_fd)


def test_audio_drain_budget_covers_native_relaunch_runway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    streamer = HLSStreamer(audio_fd=123, work_dir=tmp_path)
    remaining = streamer.audio_format.bytes_per_second * 5

    def available(_fd):
        return min(remaining, 1 << 16)

    def read(_fd, count):
        nonlocal remaining
        consumed = min(count, remaining)
        remaining -= consumed
        return b"\0" * consumed

    monkeypatch.setattr(streamer_module, "_pipe_bytes_available", available)
    monkeypatch.setattr(streamer_module.os, "read", read)

    dropped = streamer._drain_audio_fd(report=False)

    assert remaining == 0
    assert dropped == streamer.audio_format.bytes_per_second * 5
    streamer.stop()


def test_ffmpeg_teardown_drains_audio_concurrently_with_slow_reap(
    tmp_path: Path,
) -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    streamer = HLSStreamer(audio_fd=read_fd, work_dir=tmp_path)
    kill_started = threading.Event()
    producer_done = threading.Event()

    class SlowFfmpeg:
        def kill(self, *, graceful=False):
            del graceful
            kill_started.set()
            assert producer_done.wait(timeout=1)
            time.sleep(0.05)

    streamer._ffmpeg = SlowFfmpeg()  # type: ignore[assignment]
    producer_failures: list[BaseException] = []

    def produce() -> None:
        try:
            assert kill_started.wait(timeout=1)
            for _ in range(128):
                os.write(write_fd, b"\0" * 4096)
                time.sleep(0.001)
        except BaseException as exc:
            producer_failures.append(exc)
        finally:
            producer_done.set()

    producer = threading.Thread(target=produce)
    producer.start()
    try:
        streamer._kill_ffmpeg()
        producer.join(timeout=1)

        assert not producer.is_alive()
        assert producer_failures == []
        assert streamer_module._pipe_bytes_available(read_fd) == 0
        assert streamer._ffmpeg is None
    finally:
        if producer.is_alive():
            producer.join(timeout=1)
        streamer.stop()
        os.close(read_fd)
        os.close(write_fd)


def test_start_without_first_video_frame_never_spawns_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    streamer = HLSStreamer(audio_fd=11, work_dir=tmp_path)
    streamer.FIRST_FRAME_TIMEOUT_S = 0.01
    monkeypatch.setattr(streamer_module.shutil, "which", lambda _name: "/fake/ffmpeg")
    monkeypatch.setattr(
        streamer,
        "_start_ffmpeg",
        lambda: (_ for _ in ()).throw(AssertionError("ffmpeg must not spawn")),
    )

    with pytest.raises(TimeoutError, match="first captured video frame"):
        streamer.start()

    assert streamer._ffmpeg is None
    assert streamer._sampler_thread is None
    assert streamer._writer_thread is None
    assert streamer._http is None
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
    monkeypatch.setattr(
        streamer_module,
        "preferred_video_encoder",
        lambda: streamer_module.SOFTWARE_VIDEO_ENCODER,
    )

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
    monkeypatch.setattr(
        streamer_module,
        "preferred_video_encoder",
        lambda: streamer_module.HARDWARE_VIDEO_ENCODER,
    )

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

    total_failures = streamer_module.FFMPEG_RESTART_MAX_FAILURES * 2
    for failure_number in range(1, total_failures + 1):
        process = spawned[-1]
        assert process.stdin.entered.wait(timeout=1)
        if failure_number < total_failures:
            streamer.raise_if_failed()
        else:
            with pytest.raises(RuntimeError, match="encoder recovery stopped"):
                streamer.raise_if_failed()

    # Hardware gets its bounded recovery budget, then x264 gets an independent
    # budget before the stream becomes terminal.
    assert len(spawned) == total_failures
    assert streamer._video_encoder == streamer_module.SOFTWARE_VIDEO_ENCODER
    assert streamer.fatal_error is not None
    streamer.stop()


def test_pipe_failure_after_hardware_resync_budget_still_attempts_x264(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(streamer_module, "FFMPEG_RESTART_MAX_FAILURES", 3)
    monkeypatch.setattr(streamer_module, "FFMPEG_RESTART_BASE_DELAY_S", 0.0)
    monkeypatch.setattr(streamer_module, "FFMPEG_RESTART_MAX_DELAY_S", 0.0)
    monkeypatch.setattr(
        streamer_module,
        "preferred_video_encoder",
        lambda: streamer_module.HARDWARE_VIDEO_ENCODER,
    )
    streamer = HLSStreamer(work_dir=tmp_path)
    old = _DeadFfmpeg()
    replacement = _RecordingFfmpeg()
    starts: list[str] = []
    streamer._ffmpeg = old  # type: ignore[assignment]
    streamer._ffmpeg_generation = 7
    streamer._consecutive_ffmpeg_failures = (
        streamer_module.FFMPEG_RESTART_MAX_FAILURES - 1
    )

    def start_replacement() -> None:
        starts.append(streamer._video_encoder)
        streamer._ffmpeg = replacement  # type: ignore[assignment]
        streamer._ffmpeg_generation += 1
        streamer._ffmpeg_started_at = time.monotonic()

    monkeypatch.setattr(streamer, "_start_ffmpeg", start_replacement)

    assert streamer._recover_ffmpeg(old, 7, BrokenPipeError("VT pipe failed"))

    assert starts == [streamer_module.SOFTWARE_VIDEO_ENCODER]
    assert streamer._video_encoder == streamer_module.SOFTWARE_VIDEO_ENCODER
    assert streamer._consecutive_ffmpeg_failures == 0
    assert streamer.fatal_error is None
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


def test_sampler_new_generation_holds_static_page_and_rejects_delayed_old_frame(
    tmp_path: Path,
):
    streamer = HLSStreamer(fps=20, work_dir=tmp_path)
    streamer._ffmpeg = _RecordingFfmpeg()  # type: ignore[assignment]
    streamer._ffmpeg_generation = 1
    streamer._ffmpeg_video_boundary = (1, None)
    initial_capture_at = time.monotonic() - 0.01
    streamer._latest.publish(
        b"initial",
        captured_at=initial_capture_at,
    )

    stopped = threading.Event()
    stopped.set()

    def pop_until(deadline: float) -> bytes | None:
        while time.monotonic() < deadline:
            frame = streamer._queue.get(stopped)
            if frame is not None:
                return frame
            time.sleep(0.005)
        return None

    streamer._start_sampler_thread()
    assert pop_until(time.monotonic() + 1.0) == b"initial"

    # Model a completed re-anchor while the sampler is between ticks. Clearing
    # the encode queue is part of the production relaunch transaction.
    with streamer._ffmpeg_lock:
        streamer._queue.clear()
        streamer._ffmpeg_generation = 2
        boundary_at = time.monotonic()
        streamer._ffmpeg_video_boundary = (2, boundary_at)
        held = streamer._latest.reanchor_latest(boundary_at)
        assert held is not None

    # Delivery happened after the re-anchor, but Chrome says capture happened
    # before it. Publication order must not let this frame seed generation 2.
    streamer._latest.publish(
        b"delayed-pre-boundary",
        captured_at=boundary_at - 0.001,
    )
    # With no new page capture, the held visual keeps a static page alive. The
    # delayed old-timeline capture must never displace it.
    assert pop_until(time.monotonic() + 1.0) == b"initial"
    assert pop_until(time.monotonic() + 1.0) == b"initial"

    streamer._latest.publish(
        b"fresh-generation-frame",
        captured_at=boundary_at + 0.001,
    )
    deadline = time.monotonic() + 1.0
    while True:
        frame = pop_until(deadline)
        assert frame != b"delayed-pre-boundary"
        if frame == b"fresh-generation-frame":
            break
        assert frame == b"initial"
    streamer.stop()


def test_sampler_refreshes_clock_after_waiting_for_generation_reanchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(streamer_module, "SAMPLER_MAX_CATCHUP_S", 0.02)
    stats = PipelineStats(target_fps=100.0)
    streamer = HLSStreamer(fps=100, work_dir=tmp_path, stats=stats)
    streamer._ffmpeg = _RecordingFfmpeg()  # type: ignore[assignment]
    streamer._ffmpeg_generation = 1
    streamer._ffmpeg_video_boundary = (1, None)
    streamer._latest.publish(b"initial")

    real_lock = streamer._ffmpeg_lock
    sampler_blocked = threading.Event()
    release_sampler = threading.Event()

    class ThirdEntryGate:
        def __init__(self) -> None:
            self.entries = 0

        def __enter__(self):
            self.entries += 1
            # First sampler tick enters once to snapshot the generation and
            # once to enqueue. Block its next generation snapshot after that
            # tick's monotonic timestamp has already been captured.
            if self.entries == 3:
                sampler_blocked.set()
                assert release_sampler.wait(timeout=1.0)
            real_lock.acquire()
            return self

        def __exit__(self, *_exc_info):
            real_lock.release()

    streamer._ffmpeg_lock = ThirdEntryGate()  # type: ignore[assignment]
    streamer._start_sampler_thread()
    assert sampler_blocked.wait(timeout=1.0)

    # Exceed both a frame period and the reduced catch-up budget while the
    # sampler is waiting to observe a completed generation change.
    time.sleep(0.08)
    with real_lock:
        streamer._ffmpeg_generation = 2
        boundary_at = time.monotonic()
        streamer._ffmpeg_video_boundary = (2, boundary_at)
        assert streamer._latest.reanchor_latest(boundary_at) is not None
    release_sampler.set()
    time.sleep(0.08)

    assert streamer._pending_timeline_resync is None
    assert stats.snapshot(0.2).resyncs == 0
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


def test_same_thread_stop_reenters_guarded_streamer_startup_without_deadlock(
    tmp_path: Path,
):
    streamer = HLSStreamer(work_dir=tmp_path)
    streamer._lifecycle_state = "starting"
    results: list[bool] = []

    thread = threading.Thread(
        target=lambda: results.append(streamer._startup_step(streamer.stop))
    )
    thread.start()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert results == [False]
    assert streamer._lifecycle_state == "stopped"


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
    monkeypatch.setattr(
        streamer_module,
        "preferred_video_encoder",
        lambda: streamer_module.SOFTWARE_VIDEO_ENCODER,
    )
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
