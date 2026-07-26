"""TUI health-monitoring and control-boundary regressions."""

from __future__ import annotations

from types import SimpleNamespace

from cast_tab.stats import PipelineStats
from cast_tab.streamer import MAX_AUTO_AV_OFFSET_MS
from cast_tab.tui import CastTUI


def test_initial_audio_offset_is_clamped_to_streamer_limit():
    app = CastTUI(
        stats=PipelineStats(),
        streamer=SimpleNamespace(),
        caster=SimpleNamespace(),
        initial_offset_ms=MAX_AUTO_AV_OFFSET_MS + 500,
    )

    assert app._offset_applied == MAX_AUTO_AV_OFFSET_MS
    assert app._offset_pending == MAX_AUTO_AV_OFFSET_MS


def test_pipeline_failure_exits_poll_loop(monkeypatch):
    exit_requested = []

    def fail_health_check():
        raise RuntimeError("browser worker died")

    monkeypatch.setattr(
        CastTUI,
        "call_from_thread",
        lambda self, callback, *args, **kwargs: exit_requested.append(callback),
    )
    app = CastTUI(
        stats=PipelineStats(),
        streamer=SimpleNamespace(),
        caster=SimpleNamespace(),
        refresh_s=0.001,
        health_check=fail_health_check,
    )

    app._poll_loop()

    assert isinstance(app._pipeline_failure, RuntimeError)
    assert "browser worker died" in str(app._pipeline_failure)
    assert exit_requested == [app.exit]


def test_hls_delivery_health_flags_a_stalled_active_segment():
    stats = PipelineStats()
    stats.record_hls(
        segment_count=6,
        newest_age_s=0.5,
        target_duration_s=2.0,
        delivery_active=1,
        delivery_active_s=2.5,
        delivery_seen=True,
    )

    warn, bad = CastTUI._hls_delivery_health(stats.snapshot(1.0))

    assert warn
    assert bad


def test_hls_delivery_staleness_only_warns_when_tv_is_playing():
    stats = PipelineStats()
    stats.record_hls(
        segment_count=6,
        newest_age_s=0.5,
        target_duration_s=2.0,
        delivery_idle_s=4.0,
        delivery_seen=True,
    )
    not_playing = stats.snapshot(1.0)
    assert CastTUI._hls_delivery_health(not_playing) == (False, False)

    stats.record_tv_poll(state="PLAYING", position_s=10.0, idle_reason=None)
    playing = stats.snapshot(1.0)
    assert CastTUI._hls_delivery_health(playing) == (True, False)
