"""Unit tests for the TV re-cast watchdog (fake chromecast, no network)."""

import socket
import threading
import time
from types import SimpleNamespace

import pychromecast
import pychromecast.socket_client as socket_client_module
import pytest
from pychromecast.models import CastInfo, HostServiceInfo

from cast_tab.caster import IDLE_POLLS_BEFORE_RECAST, TabCaster

TARGET_URL = "http://host/stream.m3u8"


class FakeStatus:
    def __init__(
        self,
        state,
        idle_reason=None,
        *,
        content_id=None,
        media_session_id=None,
        current_time=0.0,
    ):
        self.player_state = state
        self.idle_reason = idle_reason
        self.current_time = current_time
        self.content_id = content_id
        self.media_session_id = media_session_id


class FakeMediaController:
    """Serves a scripted sequence of states; PLAYING once playback restarts."""

    def __init__(self, states):
        self._states = list(states)
        self.status = None
        self.is_active = True
        self.status_requests = 0
        self.launching_status_requests = 0
        self.play_media_calls = []
        self.pause_calls = 0
        self.play_calls = 0
        self.stop_calls = 0

    def update_status(self):
        self.launching_status_requests += 1
        if self._states:
            self.status = self._states.pop(0)

    def send_message_nocheck(self, message, *, callback_function=None):
        assert message == {"type": "GET_STATUS"}
        self.status_requests += 1
        if self._states:
            self.status = self._states.pop(0)
        if callback_function is not None:
            statuses = []
            if self.status is not None:
                statuses.append(
                    {
                        "playerState": self.status.player_state,
                        "idleReason": self.status.idle_reason,
                        "currentTime": self.status.current_time,
                        "mediaSessionId": self.status.media_session_id,
                        "media": {"contentId": self.status.content_id},
                    }
                )
            callback_function(True, {"type": "MEDIA_STATUS", "status": statuses})

    def play_media(self, url, *args, callback_function=None, **kwargs):
        self.is_active = True
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
        self.disconnect_timeouts = []
        self.wait_timeouts = []
        self.quit_clears_app = quit_clears_app
        self.status_event = threading.Event()
        self.start_calls = 0
        self.ready_on_start = True
        self.disconnected = False
        self.start_after_disconnect = False

    def quit_app(self, timeout=None):
        self.quit_app_calls += 1
        if self.quit_clears_app:
            self.app_id = None
            self.media_controller.is_active = False

    def disconnect(self, timeout=None):
        self.disconnect_calls += 1
        self.disconnect_timeouts.append(timeout)
        self.disconnected = True

    def start(self):
        self.start_calls += 1
        self.start_after_disconnect = self.start_after_disconnect or self.disconnected
        if self.ready_on_start:
            self.status_event.set()

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)

    def volume_up(self, delta):
        self.status.volume_level = min(1.0, self.status.volume_level + delta)
        return self.status.volume_level

    def volume_down(self, delta):
        self.status.volume_level = max(0.0, self.status.volume_level - delta)
        return self.status.volume_level

    def set_volume_muted(self, muted):
        self.status.volume_muted = muted


def _caster(
    states,
    *,
    buffering_timeout_s=30.0,
    playback_stall_timeout_s=30.0,
    delivery_probe=None,
    receiver_operation_stop_timeout_s=0.05,
) -> TabCaster:
    caster = TabCaster(
        device=None,
        buffering_timeout_s=buffering_timeout_s,
        playback_stall_timeout_s=playback_stall_timeout_s,
        receiver_operation_stop_timeout_s=receiver_operation_stop_timeout_s,
    )
    caster._chromecast = FakeChromecast(states)
    if delivery_probe is not None:
        caster.set_delivery_probe(delivery_probe)
    caster._playlist_url = TARGET_URL
    # Unit tests do not need the real receiver settle delay. Tests exercising
    # cancellation replace this with the stop event's actual wait method.
    caster._receiver_wait = lambda _timeout: False
    return caster


def _target_status(state, idle_reason=None, *, current_time=0.0, session_id=1):
    return FakeStatus(
        state,
        idle_reason,
        content_id=TARGET_URL,
        media_session_id=session_id,
        current_time=current_time,
    )


def test_playing_resets_idle_counter():
    caster = _caster([FakeStatus("IDLE"), _target_status("PLAYING"), FakeStatus("IDLE")])
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
    caster = _caster([FakeStatus("BUFFERING"), _target_status("PAUSED")])
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
            _target_status("PLAYING"),
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
    caster = _caster([_target_status("PLAYING"), _target_status("PAUSED")])
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

    def update_status_during_shutdown(_message, *, callback_function=None):
        entered_poll.set()
        assert caster._watchdog_stop.wait(timeout=1.0)
        mc.status = FakeStatus("IDLE", "FINISHED")
        if callback_function is not None:
            callback_function(True, {"type": "MEDIA_STATUS"})

    mc.send_message_nocheck = update_status_during_shutdown
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
        mc.is_active = True
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


def test_stop_cancels_pending_load_without_callback_or_retry():
    caster = _caster([], receiver_operation_stop_timeout_s=0.5)
    caster._receiver_wait = caster._watchdog_stop.wait
    cc = caster._chromecast
    mc = cc.media_controller
    load_sent = threading.Event()
    failures = []

    def blocked_load(url, *args, callback_function=None, **kwargs):
        mc.play_media_calls.append(url)
        assert callback_function is not None
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
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert len(mc.play_media_calls) == 1
    # Only stop() quits; the failed LOAD must not enter receiver recovery.
    assert cc.quit_app_calls == 1
    assert failures


def test_stop_never_interleaves_cleanup_with_busy_receiver_operation():
    caster = _caster([], receiver_operation_stop_timeout_s=0.01)
    cc = caster._chromecast
    operation_entered = threading.Event()
    release_operation = threading.Event()

    def hold_operation():
        with caster._receiver_operation_lock:
            operation_entered.set()
            assert release_operation.wait(timeout=1.0)

    worker = threading.Thread(target=hold_operation)
    worker.start()
    assert operation_entered.wait(timeout=1.0)

    caster.stop()

    assert cc.media_controller.stop_calls == 0
    assert cc.quit_app_calls == 0
    assert cc.disconnect_timeouts == [caster._disconnect_timeout_s]

    release_operation.set()
    worker.join(timeout=1.0)
    assert not worker.is_alive()


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
    assert cc.disconnect_timeouts == [caster._disconnect_timeout_s]


def test_stop_retries_transient_disconnect_without_repeating_receiver_cleanup():
    caster = _caster([])
    cc = caster._chromecast
    assert cc is not None
    real_disconnect = cc.disconnect
    disconnect_calls = 0

    def flaky_disconnect(timeout=None):
        nonlocal disconnect_calls
        disconnect_calls += 1
        if disconnect_calls == 1:
            raise TimeoutError("socket worker still alive")
        real_disconnect(timeout=timeout)

    cc.disconnect = flaky_disconnect

    with pytest.raises(TimeoutError, match="still alive"):
        caster.stop()

    assert caster._chromecast is cc
    assert cc.media_controller.stop_calls == 1
    assert cc.quit_app_calls == 1

    caster.stop()
    assert disconnect_calls == 2
    assert caster._chromecast is None
    assert cc.media_controller.stop_calls == 1
    assert cc.quit_app_calls == 1


def test_stop_detects_disconnect_that_returns_with_live_socket_worker():
    caster = _caster([])
    cc = caster._chromecast
    assert cc is not None

    class SocketWorker:
        checks = 0

        def is_alive(self):
            self.checks += 1
            return self.checks == 1

    worker = SocketWorker()
    cc.socket_client = worker

    with pytest.raises(TimeoutError, match="socket worker did not stop"):
        caster.stop()

    assert caster._chromecast is cc
    assert cc.disconnect_calls == 1

    caster.stop()
    assert cc.disconnect_calls == 2
    assert caster._chromecast is None


def test_stop_retains_transport_ownership_after_two_disconnect_failures():
    caster = _caster([])
    cc = caster._chromecast
    assert cc is not None
    disconnect_calls = 0

    def failed_disconnect(timeout=None):
        nonlocal disconnect_calls
        del timeout
        disconnect_calls += 1
        raise TimeoutError(f"disconnect attempt {disconnect_calls} failed")

    cc.disconnect = failed_disconnect

    for attempt in (1, 2):
        with pytest.raises(TimeoutError, match=f"attempt {attempt} failed"):
            caster.stop()

    assert disconnect_calls == 2
    assert caster._chromecast is cc


def test_stop_retries_watchdog_join_failure_after_transport_cleanup():
    caster = _caster([])
    cc = caster._chromecast
    assert cc is not None

    class FlakyWatchdog:
        def __init__(self):
            self.join_calls = 0

        def join(self, *, timeout):
            del timeout
            self.join_calls += 1
            if self.join_calls == 1:
                raise RuntimeError("watchdog join interrupted")

        def is_alive(self):
            return False

    watchdog = FlakyWatchdog()
    caster._watchdog_thread = watchdog  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="join interrupted"):
        caster.stop()

    assert cc.disconnect_calls == 1
    assert caster._chromecast is None

    caster.stop()
    assert watchdog.join_calls == 2
    assert cc.disconnect_calls == 1


def test_connect_failure_retains_transport_until_stop_retry(monkeypatch):
    caster = TabCaster(device=_connect_device(), disconnect_timeout_s=0.1)
    cc = FakeChromecast([])
    cc.ready_on_start = False
    disconnect_calls = 0

    class FailedReadyEvent:
        def set(self):
            pass

        def wait(self, timeout=None):
            del timeout
            raise OSError("status wait failed")

    cc.status_event = FailedReadyEvent()
    real_disconnect = cc.disconnect

    def flaky_disconnect(timeout=None):
        nonlocal disconnect_calls
        disconnect_calls += 1
        if disconnect_calls == 1:
            raise TimeoutError("initial disconnect failed")
        real_disconnect(timeout=timeout)

    cc.disconnect = flaky_disconnect
    monkeypatch.setattr(
        "cast_tab.caster.pychromecast.Chromecast",
        lambda *_args, **_kwargs: cc,
    )

    with pytest.raises(OSError, match="status wait failed") as caught:
        caster.connect()

    assert any("cleanup was incomplete" in note for note in caught.value.__notes__)
    assert caster._chromecast is cc

    caster.stop()
    assert disconnect_calls == 2
    assert caster._chromecast is None


def _connect_device(*, cast_type="cast"):
    return SimpleNamespace(
        name="Den TV",
        host="192.0.2.10",
        port=8009,
        model="Fake Cast",
        cast_info=SimpleNamespace(
            uuid="fake-uuid",
            cast_type=cast_type,
            manufacturer="Test Manufacturer",
        ),
    )


def test_connect_preserves_type_and_keeps_socket_retries_alive(monkeypatch):
    device = _connect_device()
    caster = TabCaster(device=device, connect_timeout_s=3.0)
    cc = FakeChromecast([])
    constructed = {}

    def construct(cast_info, **kwargs):
        constructed["cast_info"] = cast_info
        constructed["kwargs"] = kwargs
        return cc

    monkeypatch.setattr("cast_tab.caster.pychromecast.Chromecast", construct)

    caster.connect()

    cast_info = constructed["cast_info"]
    assert cast_info.cast_type == "cast"
    assert cast_info.manufacturer == "Test Manufacturer"
    assert {(service.host, service.port) for service in cast_info.services} == {
        (device.host, device.port)
    }
    # PyChromecast reuses this budget after a transport drop. None lets both
    # initial connection and later reconnects survive a transient refusal;
    # TabCaster's outer readiness deadline remains finite below.
    assert constructed["kwargs"]["tries"] is None
    assert 0 < constructed["kwargs"]["timeout"] <= 3.0
    assert cc.start_calls == 1


def test_unbounded_socket_retries_still_obey_outer_connect_deadline(monkeypatch):
    now = 0.0
    caster = TabCaster(
        device=_connect_device(),
        connect_timeout_s=0.2,
        disconnect_timeout_s=0.1,
    )
    caster._monotonic = lambda: now
    cc = FakeChromecast([])
    cc.ready_on_start = False
    constructed = {}

    class NeverReadyEvent:
        def set(self) -> None:
            pass

        def wait(self, timeout=None):
            nonlocal now
            assert timeout is not None and timeout > 0
            now += timeout
            return False

    cc.status_event = NeverReadyEvent()

    def construct(*_args, **kwargs):
        constructed.update(kwargs)
        return cc

    monkeypatch.setattr("cast_tab.caster.pychromecast.Chromecast", construct)

    with pytest.raises(Exception, match="wait"):
        caster.connect()

    assert constructed["tries"] is None
    assert now == pytest.approx(0.2)
    assert cc.disconnect_calls == 1
    assert cc.disconnect_timeouts == [0.1]
    assert caster._chromecast is None


def test_pinned_socket_client_retries_initial_and_reconnect_failures(
    monkeypatch,
) -> None:
    """Exercise the retry contract TabCaster relies on in pinned PyChromecast."""
    cast_info = CastInfo(
        services={HostServiceInfo("192.0.2.10", 8009)},
        uuid="fake-uuid",
        model_name="Fake Cast",
        friendly_name="Den TV",
        host="192.0.2.10",
        port=8009,
        cast_type="cast",
        manufacturer="Test",
    )
    chromecast = pychromecast.Chromecast(
        cast_info,
        tries=None,
        timeout=0.01,
        retry_wait=0,
    )
    client = chromecast.socket_client
    # PyChromecast treats 0 as "use default" in __init__; make this isolated
    # retry-contract test immediate after construction.
    client.retry_wait = 0
    attempts: list[int] = []

    class AttemptSocket:
        def __init__(self) -> None:
            self._socket = socket.socket()

        def fileno(self):
            return self._socket.fileno()

        def settimeout(self, _timeout):
            pass

        def connect(self, _address):
            attempts.append(len(attempts) + 1)
            if len(attempts) in (1, 3):
                raise OSError("transient connection refusal")

        def close(self):
            self._socket.close()

    class SSLContext:
        check_hostname = False
        verify_mode = None

        def __init__(self, _protocol):
            pass

        def wrap_socket(self, transport):
            return transport

    class ReceiverController:
        def update_status(self):
            pass

        def disconnected(self):
            pass

    class HeartbeatController:
        def ping(self):
            pass

        def reset(self):
            pass

        def is_expired(self):
            return False

    monkeypatch.setattr(socket_client_module, "new_socket", AttemptSocket)
    monkeypatch.setattr(
        socket_client_module,
        "get_host_from_service",
        lambda _service, _zconf: ("192.0.2.10", 8009, None),
    )
    monkeypatch.setattr(socket_client_module.ssl, "SSLContext", SSLContext)
    client.receiver_controller = ReceiverController()
    client.heartbeat_controller = HeartbeatController()
    client._report_connection_status = lambda _status: None

    try:
        client.initialize_connection()
        assert attempts == [1, 2]
        assert not client.stop.is_set()

        client._force_recon = True
        assert client._check_connection() is False
        assert attempts == [1, 2, 3, 4]
        assert not client.stop.is_set()
    finally:
        client._cleanup()


def test_unknown_cast_type_probe_and_failed_wait_are_bounded(monkeypatch):
    device = _connect_device(cast_type=None)
    caster = TabCaster(
        device=device,
        connect_timeout_s=3.0,
        disconnect_timeout_s=0.25,
    )
    cc = FakeChromecast([])
    probe_timeouts = []

    def resolve_type(cast_info, *, timeout):
        probe_timeouts.append(timeout)
        return type(cast_info)(
            cast_info.services,
            cast_info.uuid,
            cast_info.model_name,
            cast_info.friendly_name,
            cast_info.host,
            cast_info.port,
            "cast",
            "Resolved Manufacturer",
        )

    class FailedReadyEvent:
        def set(self) -> None:
            pass

        def wait(self, timeout=None):
            failed_wait(timeout=timeout)
            return False

    def failed_wait(*, timeout=None):
        cc.wait_timeouts.append(timeout)
        raise OSError("device disappeared")

    cc.status_event = FailedReadyEvent()
    monkeypatch.setattr("cast_tab.caster.pychromecast.get_cast_type", resolve_type)
    monkeypatch.setattr(
        "cast_tab.caster.pychromecast.Chromecast",
        lambda *_args, **_kwargs: cc,
    )

    with pytest.raises(OSError, match="disappeared"):
        caster.connect()

    assert probe_timeouts and 0 < probe_timeouts[0] <= 3.0
    assert cc.wait_timeouts and 0 < cc.wait_timeouts[0] <= 3.0
    assert cc.disconnect_timeouts == [0.25]
    assert caster._chromecast is None


def test_unknown_device_resolving_to_audio_is_rejected_before_connect(monkeypatch):
    device = _connect_device(cast_type=None)
    caster = TabCaster(device=device, connect_timeout_s=3.0)

    def resolve_audio(cast_info, *, timeout):
        assert 0 < timeout <= 3.0
        return type(cast_info)(
            cast_info.services,
            cast_info.uuid,
            cast_info.model_name,
            cast_info.friendly_name,
            cast_info.host,
            cast_info.port,
            "audio",
            "Resolved Manufacturer",
        )

    monkeypatch.setattr("cast_tab.caster.pychromecast.get_cast_type", resolve_audio)
    monkeypatch.setattr(
        "cast_tab.caster.pychromecast.Chromecast",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("audio target reached Chromecast constructor")
        ),
    )

    with pytest.raises(RuntimeError, match="not a video-capable Chromecast"):
        caster.connect()

    assert caster._chromecast is None


def test_connect_finishing_after_stop_cannot_resurrect_receiver(monkeypatch):
    device = SimpleNamespace(
        name="Den TV",
        host="192.0.2.10",
        port=8009,
        model="Fake Cast",
        cast_info=SimpleNamespace(
            uuid="fake-uuid",
            cast_type="cast",
            manufacturer="Test",
        ),
    )
    caster = TabCaster(device=device)
    cc = FakeChromecast([])
    wait_entered = threading.Event()
    failures = []

    class NeverReadyEvent:
        def set(self) -> None:
            pass

        def wait(self, timeout=None):
            cc.wait_timeouts.append(timeout)
            wait_entered.set()
            return threading.Event().wait(timeout)

    cc.ready_on_start = False
    cc.status_event = NeverReadyEvent()
    monkeypatch.setattr(
        "cast_tab.caster.pychromecast.Chromecast",
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
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert caster._chromecast is None
    assert cc.disconnect_calls == 1
    assert cc.disconnect_timeouts == [caster._disconnect_timeout_s]
    assert cc.wait_timeouts and cc.wait_timeouts[0] > 0
    assert failures and "stopped while connecting" in str(failures[0])
    assert cc.start_calls == 1
    assert not cc.start_after_disconnect


def test_connect_is_one_shot_and_does_not_leak_first_socket(monkeypatch):
    caster = TabCaster(device=_connect_device())
    cc = FakeChromecast([])
    constructions = 0

    def construct(*_args, **_kwargs):
        nonlocal constructions
        constructions += 1
        return cc

    monkeypatch.setattr("cast_tab.caster.pychromecast.Chromecast", construct)

    caster.connect()
    with pytest.raises(RuntimeError, match="one-shot"):
        caster.connect()

    assert constructions == 1
    assert cc.start_calls == 1
    caster.stop()
    assert cc.disconnect_calls == 1


def test_concurrent_connect_constructs_only_one_socket(monkeypatch):
    caster = TabCaster(device=_connect_device())
    cc = FakeChromecast([])
    cc.ready_on_start = False
    ready_waiting = threading.Event()
    release_ready = threading.Event()
    constructions = 0
    failures: list[BaseException] = []

    class GatedReadyEvent:
        def set(self) -> None:
            pass

        def wait(self, timeout=None):
            ready_waiting.set()
            return release_ready.wait(timeout)

    cc.status_event = GatedReadyEvent()

    def construct(*_args, **_kwargs):
        nonlocal constructions
        constructions += 1
        return cc

    monkeypatch.setattr("cast_tab.caster.pychromecast.Chromecast", construct)

    first = threading.Thread(
        target=lambda: caster.connect(),
    )
    first.start()
    assert ready_waiting.wait(timeout=1)

    try:
        caster.connect()
    except BaseException as exc:
        failures.append(exc)
    release_ready.set()
    first.join(timeout=1)

    assert not first.is_alive()
    assert constructions == 1
    assert len(failures) == 1
    assert "one-shot" in str(failures[0])
    caster.stop()
    assert cc.disconnect_calls == 1


def test_external_cancellation_aborts_pending_connect_and_reaps_socket(monkeypatch):
    cancelled = False
    caster = TabCaster(device=_connect_device(), connect_timeout_s=12.0)
    caster.set_cancellation_probe(lambda: cancelled)
    cc = FakeChromecast([])
    cc.ready_on_start = False

    class CancelOnWait:
        def set(self) -> None:
            pass

        def wait(self, timeout=None):
            nonlocal cancelled
            del timeout
            cancelled = True
            return False

    cc.status_event = CancelOnWait()
    monkeypatch.setattr(
        "cast_tab.caster.pychromecast.Chromecast",
        lambda *_args, **_kwargs: cc,
    )

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="stopped while connecting"):
        caster.connect()

    assert time.monotonic() - started < 0.5
    assert cc.disconnect_calls == 1
    assert caster._chromecast is None


def test_external_cancellation_aborts_pending_load_without_retry() -> None:
    cancelled = False
    caster = _caster([])
    caster.set_cancellation_probe(lambda: cancelled)
    cc = caster._chromecast
    assert cc is not None
    mc = cc.media_controller

    def pending_load(url, *args, callback_function=None, **kwargs):
        nonlocal cancelled
        del args, callback_function, kwargs
        mc.play_media_calls.append(url)
        cancelled = True

    mc.play_media = pending_load

    started = time.monotonic()
    with pytest.raises(Exception, match="stopped"):
        caster.play_hls(TARGET_URL, announce=False)

    assert time.monotonic() - started < 0.5
    assert mc.play_media_calls == [TARGET_URL]
    caster.stop()


def test_status_exception_is_swallowed():
    caster = _caster([])

    def boom(_message, *, callback_function=None):
        raise OSError("socket down")

    caster._chromecast.media_controller.send_message_nocheck = boom
    assert caster.ensure_playing() is None
    assert caster.reconnects == 0


def test_status_observation_never_uses_app_launching_send_path():
    url = "http://host/stream.m3u8"
    caster = _caster(
        [
            FakeStatus("PLAYING", content_id=url, media_session_id=1),
            FakeStatus("PLAYING", content_id=url, media_session_id=1),
            FakeStatus("PLAYING", content_id=url, media_session_id=1),
        ]
    )
    mc = caster._chromecast.media_controller

    assert caster.ensure_playing() is None
    assert caster.poll_playback_stats().state == "PLAYING"
    assert caster.toggle_pause() == "paused"

    assert mc.status_requests == 3
    assert mc.launching_status_requests == 0


def test_inactive_media_namespace_is_observed_without_launching_receiver():
    caster = _caster([])
    mc = caster._chromecast.media_controller
    mc.is_active = False
    mc.status = FakeStatus(
        "PLAYING",
        content_id="http://unrelated/stream.m3u8",
        media_session_id=99,
    )

    assert caster.ensure_playing() is None
    assert caster.poll_playback_stats().state is None
    assert caster.toggle_pause() is None

    assert mc.status_requests == 0
    assert mc.launching_status_requests == 0


def test_status_poll_returns_unknown_instead_of_blocking_on_receiver_reset():
    caster = _caster([])
    cc = caster._chromecast
    cc.app_id = "CC1AD845"
    reset_entered = threading.Event()
    release_reset = threading.Event()
    original_quit = cc.quit_app

    def blocked_quit(*, timeout=None):
        reset_entered.set()
        assert release_reset.wait(timeout=1.0)
        original_quit(timeout=timeout)

    cc.quit_app = blocked_quit
    play_failure = []

    def play():
        try:
            caster.play_hls("http://host/stream.m3u8", announce=False)
        except Exception as exc:
            play_failure.append(exc)

    play_worker = threading.Thread(target=play)
    play_worker.start()
    assert reset_entered.wait(timeout=1.0)

    poll_worker = threading.Thread(target=caster.poll_playback_stats)
    poll_worker.start()
    poll_worker.join(timeout=0.05)
    assert not poll_worker.is_alive()
    assert cc.media_controller.status_requests == 0

    release_reset.set()
    play_worker.join(timeout=1.0)
    poll_worker.join(timeout=1.0)

    assert not play_worker.is_alive()
    assert not poll_worker.is_alive()
    assert play_failure == []
    assert cc.media_controller.launching_status_requests == 0


def test_correlated_empty_status_cannot_reuse_cached_playing_state():
    caster = _caster([])
    mc = caster._chromecast.media_controller
    mc.status = FakeStatus(
        "PLAYING",
        content_id="http://host/stream.m3u8",
        media_session_id=1,
    )

    def empty_status(_message, *, callback_function=None):
        assert callback_function is not None
        callback_function(True, {"type": "MEDIA_STATUS", "status": []})

    mc.send_message_nocheck = empty_status

    snapshot = caster.poll_playback_stats()
    assert snapshot.state is None
    assert snapshot.position_s is None
    assert snapshot.idle_reason is None


@pytest.mark.parametrize(
    "status",
    [
        FakeStatus(
            "PLAYING",
            content_id="http://unrelated/stream.m3u8",
            media_session_id=1,
        ),
        FakeStatus("PLAYING", content_id=TARGET_URL, media_session_id=None),
    ],
    ids=["wrong-url", "missing-session"],
)
def test_watchdog_recovers_playing_status_not_owned_by_target(status):
    caster = _caster([status, status])

    assert caster.ensure_playing(announce_recovery=False) is None
    event = caster.ensure_playing(announce_recovery=False)

    assert event is not None and "wrong media" in event
    assert caster.reconnects == 1


def test_target_pause_remains_healthy_without_position_or_delivery_progress():
    caster = _caster([_target_status("PAUSED")] * 4, playback_stall_timeout_s=1.0)
    now = 0.0
    caster._monotonic = lambda: now

    assert caster.ensure_playing() is None
    now = 100.0
    assert caster.ensure_playing() is None
    assert caster.reconnects == 0


def test_frozen_target_recasts_after_sustained_stall_window():
    caster = _caster(
        [_target_status("PLAYING", current_time=10.0)] * 5,
        playback_stall_timeout_s=30.0,
    )
    now = 0.0
    caster._monotonic = lambda: now

    assert caster.ensure_playing() is None
    now = 29.9
    assert caster.ensure_playing() is None
    now = 30.0
    assert caster.ensure_playing() is None  # first stale observation is debounced
    now = 35.0
    event = caster.ensure_playing(announce_recovery=False)

    assert event is not None and "PLAYING (stalled)" in event
    assert caster.reconnects == 1


def test_advancing_target_position_resets_stall_window():
    caster = _caster(
        [
            _target_status("PLAYING", current_time=10.0),
            _target_status("PLAYING", current_time=20.0),
            _target_status("PLAYING", current_time=20.0),
        ],
        playback_stall_timeout_s=30.0,
    )
    now = 0.0
    caster._monotonic = lambda: now

    assert caster.ensure_playing() is None
    now = 20.0
    assert caster.ensure_playing() is None
    now = 49.9
    assert caster.ensure_playing() is None
    assert caster.reconnects == 0


def test_selected_receiver_delivery_keeps_static_position_healthy():
    delivery = (7, True)
    caster = _caster(
        [_target_status("PLAYING", current_time=0.0)] * 3,
        playback_stall_timeout_s=1.0,
        delivery_probe=lambda: delivery,
    )
    now = 0.0
    caster._monotonic = lambda: now

    assert caster.ensure_playing() is None
    now = 100.0
    assert caster.ensure_playing() is None
    assert caster.reconnects == 0


def test_stale_selected_receiver_delivery_cannot_mask_frozen_position():
    observations = iter(
        [
            (0, False),  # initial PLAYING baseline
            (0, False),  # first stale poll
            (0, False),  # second stale poll
            (0, False),  # recovery LOAD baseline
            (1, True),  # selected receiver fetches the re-cast stream
            (1, True),
        ]
    )
    caster = _caster(
        [_target_status("PLAYING", current_time=0.0)] * 4,
        playback_stall_timeout_s=1.0,
        delivery_probe=lambda: next(observations),
    )
    now = 0.0
    caster._monotonic = lambda: now

    assert caster.ensure_playing() is None
    now = 1.0
    assert caster.ensure_playing() is None
    now = 2.0
    event = caster.ensure_playing(announce_recovery=False)

    assert event is not None and "stalled" in event


def test_load_verification_requires_new_selected_receiver_segment():
    caster = TabCaster(device=None)
    caster._chromecast = FakeChromecast([])
    observations = iter([(5, True), (5, True), (6, True)])
    caster.set_delivery_probe(lambda: next(observations))
    now = 0.0
    caster._monotonic = lambda: now

    def advance_wait(timeout):
        nonlocal now
        now += timeout
        return False

    caster._receiver_wait = advance_wait

    caster.play_hls(TARGET_URL, announce=False)

    assert caster._chromecast.media_controller.status_requests == 2
    assert caster._delivery_baseline == 5
