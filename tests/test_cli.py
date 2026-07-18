"""CLI validation, health monitoring, and signal-shutdown regressions."""

from __future__ import annotations

import signal
from types import SimpleNamespace

import pytest

from cast_tab import cli


def test_version_identifies_source_checkout(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli._parse_args(["--version"])
    assert exc_info.value.code == 0
    assert "development checkout" in capsys.readouterr().out


def test_hls_profile_cli_defaults_and_compatibility_flag():
    assert cli._parse_args(["https://example.test"]).buffered is True
    assert cli._parse_args(["https://example.test", "--buffered"]).buffered is True
    assert cli._parse_args(["https://example.test", "--no-buffered"]).buffered is False


def test_hls_profile_help_uses_playlist_not_tv_buffer_terminology(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli._parse_args(["--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "2s HLS segments" in help_text
    assert "12s rolling playlist" in help_text
    assert "actual TV delay is receiver-controlled" in help_text
    assert "Buffer ~48s on the TV" not in help_text


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--width", "0"),
        ("--height", "-1"),
        ("--fps", "0"),
        ("--jpeg-quality", "0"),
        ("--jpeg-quality", "101"),
        ("--video-bitrate", "0"),
        ("--video-bitrate", "nan"),
        ("--video-bitrate", "inf"),
        ("--video-bitrate", "1e308"),
        ("--discovery-timeout", "-1"),
        ("--discovery-timeout", "nan"),
        ("--stats-interval", "0"),
        ("--stats-interval", "inf"),
        ("--tv-poll-interval", "-1"),
        ("--tv-poll-interval", "nan"),
        ("--audio-offset-ms", "-1"),
        ("--audio-offset-ms", "3001"),
        ("--audio-drift-ppm", "nan"),
        ("--audio-drift-ppm", "inf"),
        ("--audio-drift-ppm", "-1000000"),
        ("--audio-drift-ppm", "1e308"),
    ],
)
def test_invalid_numeric_options_are_rejected(option, value):
    with pytest.raises(SystemExit) as exc_info:
        cli._parse_args(["https://example.test", option, value])
    assert exc_info.value.code == 2


class _FakeStreamer:
    audio_offset_ms = 0


class _FakeCaster:
    last_instance = None

    def __init__(self, _device):
        type(self).last_instance = self
        self.reconnects = 0
        self.stopped = False

    def connect(self):
        pass

    def play_hls(self, _url):
        pass

    def start_watchdog(self, **_kwargs):
        pass

    def stop(self):
        self.stopped = True


def _patch_cast_runtime(monkeypatch, session_class, signal_handler):
    device = SimpleNamespace(name="Test TV")
    monkeypatch.setattr(cli, "discover_devices", lambda timeout: [device])
    monkeypatch.setattr(cli, "find_device", lambda devices, query: device)
    monkeypatch.setattr(cli, "CastSession", session_class)
    monkeypatch.setattr(cli, "TabCaster", _FakeCaster)
    monkeypatch.setattr(cli.signal, "signal", signal_handler)


def test_default_mode_collects_summary_counters_without_trace_noise(
    monkeypatch, capsys
):
    class Session:
        def __init__(self, _config, *, stats):
            self.stats = stats
            self.streamer = _FakeStreamer()
            self.audio_active = False
            self.playlist_url = "http://example.test/live.m3u8"

        def start(self):
            self.stats.record_queue(depth=1, dropped=4)
            self.stats.record_ffmpeg_restart()

        def raise_if_failed(self):
            raise RuntimeError("background encoder failed")

        def stop(self):
            pass

    _patch_cast_runtime(monkeypatch, Session, lambda _signum, _handler: None)

    result = cli.main(
        [
            "https://example.test",
            "--device",
            "Test TV",
            "--no-adblock",
            "--no-audio",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "4 video timeline ticks discarded/skipped during A/V re-anchors" in captured.out
    assert "1 ffmpeg restarts" in captured.out
    assert "[trace]" not in captured.out
    assert "background encoder failed" in captured.err


def test_second_signal_does_not_interrupt_first_shutdown(monkeypatch):
    handlers = {}

    class Session:
        last_instance = None

        def __init__(self, _config, *, stats):
            type(self).last_instance = self
            self.streamer = _FakeStreamer()
            self.audio_active = False
            self.playlist_url = "http://example.test/live.m3u8"
            self.stop_completed = False

        def start(self):
            pass

        def raise_if_failed(self):
            handlers[signal.SIGINT]()

        def stop(self):
            # Simulate an impatient second Ctrl+C while the original handler
            # is still tearing down the session.
            handlers[signal.SIGINT]()
            self.stop_completed = True

    def save_handler(signum, handler):
        handlers[signum] = handler

    _patch_cast_runtime(monkeypatch, Session, save_handler)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "https://example.test",
                "--device",
                "Test TV",
                "--no-adblock",
                "--no-audio",
            ]
        )

    assert exc_info.value.code == 0
    assert Session.last_instance.stop_completed
    assert _FakeCaster.last_instance.stopped
