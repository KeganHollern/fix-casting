"""Unit tests for the TV re-cast watchdog (fake chromecast, no network)."""

from cast_tab.caster import IDLE_POLLS_BEFORE_RECAST, TabCaster


class FakeStatus:
    def __init__(self, state, idle_reason=None):
        self.player_state = state
        self.idle_reason = idle_reason
        self.current_time = 0.0


class FakeMediaController:
    """Serves a scripted sequence of states; PLAYING once playback restarts."""

    def __init__(self, states):
        self._states = list(states)
        self.status = None
        self.play_media_calls = []
        self.pause_calls = 0
        self.play_calls = 0

    def update_status(self):
        if self._states:
            self.status = self._states.pop(0)

    def play_media(self, url, *args, **kwargs):
        self.play_media_calls.append(url)
        self._states = [FakeStatus("PLAYING")]

    def block_until_active(self, timeout=None):
        pass

    def pause(self):
        self.pause_calls += 1

    def play(self):
        self.play_calls += 1


class FakeReceiverStatus:
    def __init__(self, volume_level=0.5, volume_muted=False):
        self.volume_level = volume_level
        self.volume_muted = volume_muted


class FakeChromecast:
    def __init__(self, states, volume=0.5, muted=False):
        self.media_controller = FakeMediaController(states)
        self.status = FakeReceiverStatus(volume, muted)

    def volume_up(self, delta):
        self.status.volume_level = min(1.0, self.status.volume_level + delta)
        return self.status.volume_level

    def volume_down(self, delta):
        self.status.volume_level = max(0.0, self.status.volume_level - delta)
        return self.status.volume_level

    def set_volume_muted(self, muted):
        self.status.volume_muted = muted


def _caster(states) -> TabCaster:
    caster = TabCaster(device=None)
    caster._chromecast = FakeChromecast(states)
    caster._playlist_url = "http://host/stream.m3u8"
    return caster


def test_playing_resets_idle_counter():
    caster = _caster([FakeStatus("IDLE"), FakeStatus("PLAYING"), FakeStatus("IDLE")])
    assert caster.ensure_playing() is None  # first idle: below threshold
    assert caster.ensure_playing() is None  # playing: counter resets
    assert caster.ensure_playing() is None  # idle again: still below threshold
    assert caster.reconnects == 0


def test_recasts_after_consecutive_idle_polls():
    caster = _caster([FakeStatus("IDLE", "FINISHED")] * IDLE_POLLS_BEFORE_RECAST)
    for _ in range(IDLE_POLLS_BEFORE_RECAST - 1):
        assert caster.ensure_playing() is None
    event = caster.ensure_playing()
    assert event is not None and "re-cast the stream" in event
    assert "IDLE (FINISHED)" in event
    assert caster.reconnects == 1
    mc = caster._chromecast.media_controller
    assert mc.play_media_calls == ["http://host/stream.m3u8"]


def test_buffering_and_paused_are_not_idle():
    caster = _caster([FakeStatus("BUFFERING"), FakeStatus("PAUSED")])
    assert caster.ensure_playing() is None
    assert caster.ensure_playing() is None
    assert caster.reconnects == 0


def test_noop_before_play_or_connect():
    caster = TabCaster(device=None)
    assert caster.ensure_playing() is None  # not connected

    caster = TabCaster(device=None)
    caster._chromecast = FakeChromecast([])
    assert caster.ensure_playing() is None  # connected but never played


def test_toggle_pause_and_resume():
    caster = _caster([FakeStatus("PLAYING"), FakeStatus("PAUSED")])
    mc = caster._chromecast.media_controller
    assert caster.toggle_pause() == "paused"
    assert mc.pause_calls == 1
    assert caster.toggle_pause() == "resumed"
    assert mc.play_calls == 1


def test_toggle_pause_noop_when_idle_or_disconnected():
    caster = _caster([FakeStatus("IDLE")])
    assert caster.toggle_pause() is None
    assert TabCaster(device=None).toggle_pause() is None


def test_volume_step_and_clamp():
    caster = _caster([])
    assert caster.volume_step(0.05) == 0.55
    assert caster.volume_step(-0.05) == 0.5
    assert caster.volume_step(0.9) == 1.0  # clamped by the device
    assert TabCaster(device=None).volume_step(0.05) is None


def test_toggle_mute_flips():
    caster = _caster([])
    assert caster.toggle_mute() is True
    assert caster.toggle_mute() is False
    assert TabCaster(device=None).toggle_mute() is None


def test_status_exception_is_swallowed():
    caster = _caster([])

    def boom():
        raise OSError("socket down")

    caster._chromecast.media_controller.update_status = boom
    assert caster.ensure_playing() is None
    assert caster.reconnects == 0
