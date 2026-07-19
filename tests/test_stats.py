"""Unit tests for PipelineStats snapshot/reset semantics."""

from cast_tab.stats import PipelineStats


def test_snapshot_resets_interval_windows():
    st = PipelineStats(target_fps=30.0)
    st.record_capture(0.010)
    st.record_encode_write(0.002)
    st.record_queue(depth=2, dropped=1)
    st.record_ffmpeg_stderr("boom")
    st.record_audio_warning("underrun")

    snap = st.snapshot(10.0)
    assert snap.capture_fps > 0
    assert snap.queue_peak == 2
    assert snap.queue_dropped == 1
    assert snap.ffmpeg_errors == 1
    assert snap.ffmpeg_last_error == "boom"
    assert snap.audio_warnings == 1

    # Interval windows reset; cumulative totals survive.
    snap2 = st.snapshot(10.0)
    assert snap2.capture_fps == 0
    assert snap2.queue_peak == 0
    assert snap2.queue_dropped == 0
    assert snap2.ffmpeg_errors == 0
    assert snap2.audio_warnings == 0
    assert snap2.dropped_total == 1  # cumulative


def test_queue_depth_is_residence_not_av_drift():
    st = PipelineStats(target_fps=30.0)
    for _ in range(10):
        st.record_queue(depth=30)  # a full second of queued frames
    snap = st.snapshot(10.0)
    assert abs(snap.queue_residence_ms - 1000.0) < 1e-6


def test_sampler_cadence_exposes_held_frame_and_history_recovery():
    st = PipelineStats(target_fps=30.0)
    st.record_sampler_cadence(
        source_interval_s=0.0,
        tick_late_s=0.040,
        recovered_from_history=True,
    )

    snap = st.snapshot(1.0)
    assert snap.history_recoveries == 1
    assert 33.3 < snap.cadence_error_ms < 33.4
    assert snap.cadence_error_peak_ms == snap.cadence_error_ms
    assert snap.sampler_late_peak_ms == 40.0

    reset = st.snapshot(1.0)
    assert reset.history_recoveries == 0
    assert reset.cadence_error_peak_ms == 0.0


def test_hls_snapshot_distinguishes_playlist_delivery_and_publish_cadence():
    st = PipelineStats(target_fps=30.0)
    st.record_hls(
        segment_count=6,
        newest_age_s=0.5,
        segments_deleted=1,
        target_duration_s=2.0,
        publish_intervals_s=(2.0, 2.1),
        segment_requests=1,
        delivery_s=0.125,
        delivery_mbps=80.0,
        delivery_active=1,
        delivery_active_s=0.75,
        delivery_idle_s=1.5,
        delivery_seen=True,
        delivery_errors=1,
    )

    snap = st.snapshot(1.0)
    assert snap.hls_count == 6
    assert snap.hls_target_s == 2.0
    assert snap.hls_publish_count == 2
    assert snap.hls_publish_ms == 2050.0
    assert snap.hls_publish_peak_ms == 2100.0
    assert snap.hls_segment_requests == 1
    assert snap.hls_delivery_ms == 125.0
    assert snap.hls_delivery_mbps == 80.0
    assert snap.hls_delivery_active == 1
    assert snap.hls_delivery_active_ms == 750.0
    assert snap.hls_delivery_idle_ms == 1500.0
    assert snap.hls_delivery_seen
    assert snap.hls_delivery_errors == 1

    reset = st.snapshot(1.0)
    assert reset.hls_publish_count == 0
    assert reset.hls_segment_requests == 0
    assert reset.hls_delivery_errors == 0
    # Latest delivery health remains visible between the TV's segment GETs.
    assert reset.hls_delivery_ms == 125.0
    assert reset.hls_delivery_active_ms == 750.0
    assert reset.hls_delivery_idle_ms == 1500.0
    assert reset.hls_delivery_seen


def test_timeline_reanchor_counts_reason_and_discarded_frames():
    st = PipelineStats(target_fps=30.0)
    st.record_encode_resync("video queue overflow", lost_frames=4)
    st.record_timeline_loss(6)

    snap = st.snapshot(10.0)
    assert snap.resyncs == 1
    assert snap.resyncs_total == 1
    assert snap.resync_last_reason == "video queue overflow"
    assert snap.queue_dropped == 10
    assert snap.dropped_total == 10

    snap2 = st.snapshot(10.0)
    assert snap2.resyncs == 0
    assert snap2.resyncs_total == 1
    assert snap2.resync_last_reason is None
    assert snap2.dropped_total == 10


def test_tv_poll_stall_accumulation():
    st = PipelineStats(target_fps=30.0)
    events = st.record_tv_poll(state="PLAYING", position_s=10.0, idle_reason=None)
    assert events == []
    events = st.record_tv_poll(state="BUFFERING", position_s=10.0, idle_reason=None)
    assert events == ["tv event BUFFERING"]
    snap = st.snapshot(10.0)
    assert snap.tv_non_playing == 1
    assert snap.tv_non_playing_states == {"BUFFERING": 1}


def test_format_report_renders_all_lines():
    st = PipelineStats(target_fps=30.0)
    st.record_capture(0.010)
    st.record_ffmpeg_stderr("some encoder error")
    report = st.format_report(10.0)
    assert "capture" in report
    assert "encode" in report
    assert "ffmpeg  1 stderr lines" in report
    assert "sync" in report
    assert "tv" in report
