"""Unit tests for the TV re-cast watchdog (fake chromecast, no network)."""

import threading
import time
from types import SimpleNamespace

import pytest

from cast_tab.caster import IDLE_POLLS_BEFORE_RECAST, TabCaster


class FakeStatus:
    def __init__(
        self,
        state,
        idle_reason=None,
        *,
        content_id=None,
        media_session_id=None,
    ):
        self.player_state = state
        self.idle_reason = idle_reason
        self.current_time = 0.0
        self.content_id = content_id
        self.media_session_id = media_session_id


class FakeMediaController:
    """Serves a scripted sequence of states; PLAYING once playback restarts."""

    def __init__(self, states):
        self._states = list(states)
        self.status = None
        self.play_media_calls = []
        self.pause_calls = 0
        self.play_calls = 0
        self.stop_calls = 0

    def update_status(self):
        if self._states:
            self.status = self._states.pop(0)

    def play_media(self, url, *args, callback_function=None, **kwargs):
        self.play_media_calls.append(url)
        self._states = [
            FakeStatus(
                "PLAYING",
                content_id=url,
                media_session_id=len(self.play_media_calls),
            )
        ]
        if callback_function is not None:
            callback_function(True, {"type": "MEDIA_STATUS"})

    def block_until_active(self, timeout=None):
        pass

    def pause(self):
        self.pause_calls += 1

    def play(self):
        self.play_calls += 1

    def stop(self, timeout=None):
        self.stop_calls += 1


class FakeReceiverStatus:
    def __init__(self, volume_level=0.5, volume_muted=False):
        self.volume_level = volume_level
        self.volume_muted = volume_muted


class FakeChromecast:
    def __init__(
        self,
        states,
        volume=0.5,
        muted=False,
        app_id=None,
        *,
        quit_clears_app=True,
    ):
        self.media_controller = FakeMediaController(states)
        self.status = FakeReceiverStatus(volume, muted)
        self.app_id = app_id
        self.quit_app_calls = 0
        self.disconnect_calls = 0
        self.quit_clears_app = quit_clears_app

    def quit_app(self, timeout=None):
        self.quit_app_calls += 1
        if self.quit_clears_app:
            self.app_id = None

    def disconnect(self):
        self.disconnect_calls += 1

    def volume_up(self, delta):
        self.status.volume_level = min(1.0, self.status.volume_level + delta)
        return self.status.volume_level

    def volume_down(self, delta):
        self.status.volume_level = max(0.0, self.status.volume_level - delta)
        return self.status.volume_level

    def set_volume_muted(self, muted):
        self.status.volume_muted = muted


def _caster(states, *, buffering_timeout_s=30.0) -> TabCaster:
    caster = TabCaster(device=None, buffering_timeout_s=buffering_timeout_s)
    caster._chromecast = FakeChromecast(states)
    caster._playlist_url = "http://host/stream.m3u8"
    # Unit tests do not need the real receiver settle delay. Tests exercising
    # cancellation replace this with the stop event's actual wait method.
    caster._receiver_wait = lambda _timeout: False
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


def test_persistent_buffering_recasts_after_elapsed_timeout():
    caster = _caster([FakeStatus("BUFFERING")] * 3, buffering_timeout_s=5.0)
    now = 100.0
    caster._monotonic = lambda: now

    assert caster.ensure_playing() is None
    now = 104.9
    assert caster.ensure_playing() is None
    now = 105.0
    event = caster.ensure_playing(announce_recovery=False)

    assert event is not None and "BUFFERING" in event
    assert caster.reconnects == 1
    assert caster._chromecast.media_controller.play_media_calls == [
        "http://host/stream.m3u8"
    ]


def test_transient_buffering_resets_elapsed_timeout():
    caster = _caster(
        [
            FakeStatus("BUFFERING"),
            FakeStatus("BUFFERING"),
            FakeStatus("PLAYING"),
            FakeStatus("BUFFERING"),
            FakeStatus("BUFFERING"),
        ],
        buffering_timeout_s=5.0,
    )
    now = 100.0
    caster._monotonic = lambda: now

    assert caster.ensure_playing() is None
    now = 104.9
    assert caster.ensure_playing() is None
    now = 200.0
    assert caster.ensure_playing() is None  # PLAYING resets the timer
    assert caster.ensure_playing() is None  # a new buffering interval starts
    now = 204.9
    assert caster.ensure_playing() is None

    assert caster.reconnects == 0
    assert caster._chromecast.media_controller.play_media_calls == []


def test_recovery_output_can_be_suppressed_but_defaults_to_announced(capsys):
    quiet = _caster([FakeStatus("IDLE")] * IDLE_POLLS_BEFORE_RECAST)
    for _ in range(IDLE_POLLS_BEFORE_RECAST):
        quiet.ensure_playing(announce_recovery=False)
    assert capsys.readouterr().out == ""

    announced = _caster([FakeStatus("IDLE")] * IDLE_POLLS_BEFORE_RECAST)
    for _ in range(IDLE_POLLS_BEFORE_RECAST):
        announced.ensure_playing()
    output = capsys.readouterr().out
    assert "Casting tab mirror stream:" in output
    assert "Chromecast is playing." in output


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


def test_watchdog_recovers_in_background():
    caster = _caster([FakeStatus("IDLE", "FINISHED")] * 10)
    events = []
    caster.start_watchdog(grace_s=0.05, interval_s=0.05, on_event=events.append)
    deadline = time.monotonic() + 3.0
    while not events and time.monotonic() < deadline:
        time.sleep(0.02)
    caster.stop()
    assert events and "re-cast the stream" in events[0]
    assert caster.reconnects == 1
    # stop() ends the watchdog thread.
    caster._watchdog_thread.join(timeout=2)
    assert not caster._watchdog_thread.is_alive()


def test_watchdog_respects_grace_period():
    caster = _caster([FakeStatus("IDLE", "FINISHED")] * 10)
    events = []
    caster.start_watchdog(grace_s=10.0, interval_s=0.05, on_event=events.append)
    time.sleep(0.3)  # well past several intervals, still inside grace
    caster.stop()
    assert events == []
    assert caster.reconnects == 0


def test_stop_joins_watchdog_without_late_recast_or_callback():
    caster = _caster([FakeStatus("IDLE", "FINISHED")] * 10)
    entered_poll = threading.Event()
    mc = caster._chromecast.media_controller

    def update_status_during_shutdown():
        entered_poll.set()
        assert caster._watchdog_stop.wait(timeout=1.0)
        mc.status = FakeStatus("IDLE", "FINISHED")

    mc.update_status = update_status_during_shutdown
    events = []
    caster.start_watchdog(grace_s=0.0, interval_s=0.01, on_event=events.append)
    assert entered_poll.wait(timeout=1.0)

    watchdog = caster._watchdog_thread
    caster.stop()

    assert watchdog is not None and not watchdog.is_alive()
    assert mc.play_media_calls == []
    assert events == []


def test_stop_is_safe_during_control_calls():
    """stop() nulls _chromecast; concurrent control calls must not raise."""
    caster = _caster([FakeStatus("PLAYING")] * 3)
    caster.stop()
    assert caster.ensure_playing() is None
    assert caster.toggle_pause() is None
    assert caster.volume_step(0.05) is None
    assert caster.toggle_mute() is None


def test_play_hls_quits_resident_receiver_app_before_load():
    """A leftover receiver app can silently swallow the LOAD; quit it first."""
    caster = _caster([])
    cc = caster._chromecast
    cc.app_id = "CC1AD845"  # default media receiver left over from a crash
    caster.play_hls("http://host/stream.m3u8", announce=False)
    assert cc.quit_app_calls == 1
    assert cc.media_controller.play_media_calls == ["http://host/stream.m3u8"]


def test_play_hls_skips_reset_when_receiver_is_idle():
    caster = _caster([])
    cc = caster._chromecast
    assert cc.app_id is None
    caster.play_hls("http://host/stream.m3u8", announce=False)
    assert cc.quit_app_calls == 0
    assert cc.media_controller.play_media_calls == ["http://host/stream.m3u8"]


@pytest.mark.parametrize(
    "failed_response",
    [
        {"type": "LOAD_FAILED", "detailedErrorCode": 311},
        {"type": "INVALID_REQUEST"},
        None,
    ],
    ids=["load-failed", "invalid-request", "empty-response"],
)
def test_play_hls_retries_once_after_receiver_reset(failed_response):
    """Only MEDIA_STATUS proves LOAD success; every other reply gets one retry."""
    caster = _caster([])
    cc = caster._chromecast
    mc = cc.media_controller
    original_play_media = mc.play_media

    def fail_first_load(url, *args, callback_function=None, **kwargs):
        if not mc.play_media_calls:
            mc.play_media_calls.append(url)
            assert callback_function is not None
            callback_function(True, failed_response)
            return
        original_play_media(
            url,
            *args,
            callback_function=callback_function,
            **kwargs,
        )

    mc.play_media = fail_first_load
    caster.play_hls("http://host/stream.m3u8", announce=False)
    assert cc.quit_app_calls == 1
    assert mc.play_media_calls == ["http://host/stream.m3u8"] * 2


def test_play_hls_does_not_load_when_receiver_app_never_exits():
    caster = _caster([])
    cc = caster._chromecast
    cc.app_id = "CC1AD845"
    cc.quit_clears_app = False
    now = 0.0
    caster._monotonic = lambda: now

    def advance_wait(timeout):
        nonlocal now
        now += timeout
        return False

    caster._receiver_wait = advance_wait

    with pytest.raises(RuntimeError, match="did not exit"):
        caster.play_hls("http://host/stream.m3u8", announce=False)

    assert cc.media_controller.play_media_calls == []


def test_stop_quits_receiver_app():
    caster = _caster([])
    cc = caster._chromecast
    caster.stop()
    assert cc.quit_app_calls == 1


def test_stale_playing_status_cannot_false_success_a_swallowed_load():
    caster = _caster([])
    cc = caster._chromecast
    mc = cc.media_controller
    target = "http://host/new-stream.m3u8"
    mc.status = FakeStatus(
        "PLAYING",
        content_id="http://old-host/old-stream.m3u8",
        media_session_id=77,
    )
    now = 0.0
    caster._monotonic = lambda: now

    def advance_wait(timeout):
        nonlocal now
        now += timeout
        return False

    caster._receiver_wait = advance_wait
    attempts = 0

    def swallowed_then_working(url, *args, callback_function=None, **kwargs):
        nonlocal attempts
        attempts += 1
        mc.play_media_calls.append(url)
        if attempts == 2:
            mc._states = [
                FakeStatus(
                    "PLAYING",
                    content_id=url,
                    media_session_id=78,
                )
            ]
        assert callback_function is not None
        callback_function(True, {"type": "MEDIA_STATUS"})

    mc.play_media = swallowed_then_working

    caster.play_hls(target, announce=False)

    assert attempts == 2
    assert cc.quit_app_calls == 1


def test_stop_while_first_load_fails_does_not_retry():
    caster = _caster([])
    caster._receiver_wait = caster._watchdog_stop.wait
    cc = caster._chromecast
    mc = cc.media_controller
    load_sent = threading.Event()
    response_callbacks = []
    failures = []

    def blocked_load(url, *args, callback_function=None, **kwargs):
        mc.play_media_calls.append(url)
        response_callbacks.append(callback_function)
        load_sent.set()

    mc.play_media = blocked_load

    def play():
        try:
            caster.play_hls("http://host/stream.m3u8", announce=False)
        except Exception as exc:
            failures.append(exc)

    worker = threading.Thread(target=play)
    worker.start()
    assert load_sent.wait(timeout=1.0)

    caster.stop()
    response_callbacks[0](False, None)
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert len(mc.play_media_calls) == 1
    # Only stop() quits; the failed LOAD must not enter receiver recovery.
    assert cc.quit_app_calls == 1
    assert failures


def test_stop_during_receiver_reset_never_loads():
    caster = _caster([])
    cc = caster._chromecast
    cc.app_id = "CC1AD845"
    cc.quit_clears_app = False
    reset_waiting = threading.Event()

    def cancellable_wait(timeout):
        reset_waiting.set()
        return caster._watchdog_stop.wait(timeout)

    caster._receiver_wait = cancellable_wait
    failures = []

    def play():
        try:
            caster.play_hls("http://host/stream.m3u8", announce=False)
        except Exception as exc:
            failures.append(exc)

    worker = threading.Thread(target=play)
    worker.start()
    assert reset_waiting.wait(timeout=1.0)

    caster.stop()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert cc.media_controller.play_media_calls == []
    assert failures


def test_start_watchdog_after_stop_cannot_clear_terminal_shutdown():
    caster = _caster([])
    caster.stop()

    with pytest.raises(RuntimeError, match="after caster stop"):
        caster.start_watchdog(grace_s=0.0, interval_s=0.01)

    assert caster._watchdog_stop.is_set()


def test_second_live_watchdog_is_rejected_without_replacing_first():
    caster = _caster([])
    caster.start_watchdog(grace_s=10.0, interval_s=0.01)
    first = caster._watchdog_thread

    with pytest.raises(RuntimeError, match="already running"):
        caster.start_watchdog(grace_s=10.0, interval_s=0.01)

    assert caster._watchdog_thread is first
    caster.stop()


def test_concurrent_stop_cleans_up_receiver_once():
    caster = _caster([])
    cc = caster._chromecast
    callers = [threading.Thread(target=caster.stop) for _ in range(4)]

    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=1.0)

    assert all(not caller.is_alive() for caller in callers)
    assert cc.media_controller.stop_calls == 1
    assert cc.quit_app_calls == 1
    assert cc.disconnect_calls == 1


def test_connect_finishing_after_stop_cannot_resurrect_receiver(monkeypatch):
    device = SimpleNamespace(
        name="Den TV",
        host="192.0.2.10",
        port=8009,
        model="Fake Cast",
        cast_info=SimpleNamespace(uuid="fake-uuid"),
    )
    caster = TabCaster(device=device)
    cc = FakeChromecast([])
    wait_entered = threading.Event()
    release_wait = threading.Event()
    failures = []

    def wait():
        wait_entered.set()
        assert release_wait.wait(timeout=1.0)

    cc.wait = wait
    monkeypatch.setattr(
        "cast_tab.caster.pychromecast.get_chromecast_from_host",
        lambda *_args, **_kwargs: cc,
    )

    def connect():
        try:
            caster.connect()
        except Exception as exc:
            failures.append(exc)

    worker = threading.Thread(target=connect)
    worker.start()
    assert wait_entered.wait(timeout=1.0)

    caster.stop()
    release_wait.set()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert caster._chromecast is None
    assert cc.disconnect_calls == 1
    assert failures and "stopped while connecting" in str(failures[0])


def test_status_exception_is_swallowed():
    caster = _caster([])

    def boom():
        raise OSError("socket down")

    caster._chromecast.media_controller.update_status = boom
    assert caster.ensure_playing() is None
    assert caster.reconnects == 0
