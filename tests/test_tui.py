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
