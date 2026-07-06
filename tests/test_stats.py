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


def test_queue_depth_is_audio_lead():
    st = PipelineStats(target_fps=30.0)
    for _ in range(10):
        st.record_queue(depth=30)  # a full second of queued frames
    snap = st.snapshot(10.0)
    assert abs(snap.drift_ms - 1000.0) < 1e-6


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
