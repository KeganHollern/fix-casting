"""Lifecycle tests for the composed cast session."""

import threading

import pytest

import cast_tab.session as session_module
from cast_tab.session import CastSession, SessionConfig


class FakeComponent:
    def __init__(self, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1
        if self.failure is not None:
            failure, self.failure = self.failure, None
            raise failure


def _session() -> CastSession:
    return CastSession(SessionConfig(url="https://example.test", capture_audio=False))


def test_stop_attempts_all_cleanup_and_retries_only_failed_step(monkeypatch) -> None:
    cast_session = _session()
    browser = FakeComponent(RuntimeError("browser stop failed"))
    streamer = FakeComponent()
    audio_capture = object()
    audio_stops = []
    cast_session.screencaster = browser  # type: ignore[assignment]
    cast_session.streamer = streamer  # type: ignore[assignment]
    cast_session.audio_capture = audio_capture  # type: ignore[assignment]
    monkeypatch.setattr(
        session_module,
        "stop_audio_capture",
        lambda capture: audio_stops.append(capture),
    )

    with pytest.raises(RuntimeError, match="browser stop failed"):
        cast_session.stop()

    assert browser.stop_calls == 1
    assert streamer.stop_calls == 1
    assert audio_stops == [audio_capture]

    # A failed cleanup remains retryable, while successful, potentially
    # non-idempotent cleanup functions are not called twice.
    cast_session.stop()
    cast_session.stop()
    assert browser.stop_calls == 2
    assert streamer.stop_calls == 1
    assert audio_stops == [audio_capture]


def test_stop_aggregates_multiple_failures_after_audio_cleanup(monkeypatch) -> None:
    cast_session = _session()
    browser_error = RuntimeError("browser cleanup broke")
    streamer_error = ValueError("streamer cleanup broke")
    browser = FakeComponent(browser_error)
    streamer = FakeComponent(streamer_error)
    audio_stops = []
    cast_session.screencaster = browser  # type: ignore[assignment]
    cast_session.streamer = streamer  # type: ignore[assignment]
    monkeypatch.setattr(
        session_module,
        "stop_audio_capture",
        lambda capture: audio_stops.append(capture),
    )

    with pytest.raises(ExceptionGroup, match="browser capture, HLS streamer") as caught:
        cast_session.stop()

    assert caught.value.exceptions == (browser_error, streamer_error)
    assert audio_stops == [None]


def test_session_health_check_delegates_to_browser() -> None:
    cast_session = _session()
    failure = RuntimeError("browser died")

    class FailedBrowser:
        def raise_if_failed(self) -> None:
            raise failure

    cast_session.screencaster = FailedBrowser()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="browser died") as caught:
        cast_session.raise_if_failed()

    assert caught.value is failure


def test_session_health_check_delegates_to_streamer() -> None:
    cast_session = _session()
    failure = RuntimeError("encoder retries exhausted")

    class FailedStreamer:
        def raise_if_failed(self) -> None:
            raise failure

    cast_session.streamer = FailedStreamer()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="encoder retries exhausted") as caught:
        cast_session.raise_if_failed()

    assert caught.value is failure


def test_session_health_check_delegates_to_audio_capture() -> None:
    cast_session = _session()
    failure = RuntimeError("AudioTee stopped after startup")

    class FailedAudioCapture:
        def raise_if_failed(self) -> None:
            raise failure

    cast_session.audio_capture = FailedAudioCapture()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="AudioTee stopped after startup") as caught:
        cast_session.raise_if_failed()

    assert caught.value is failure


def test_reentrant_stop_returns_without_skipping_outer_cleanup(monkeypatch) -> None:
    cast_session = _session()
    streamer = FakeComponent()
    audio_stops = []

    class ReentrantBrowser:
        stop_calls = 0

        def stop(self) -> None:
            self.stop_calls += 1
            cast_session.stop()

    browser = ReentrantBrowser()
    cast_session.screencaster = browser  # type: ignore[assignment]
    cast_session.streamer = streamer  # type: ignore[assignment]
    monkeypatch.setattr(
        session_module,
        "stop_audio_capture",
        lambda capture: audio_stops.append(capture),
    )

    cast_session.stop()

    assert browser.stop_calls == 1
    assert streamer.stop_calls == 1
    assert audio_stops == [None]


def test_stop_is_safe_when_signal_reenters_with_condition_owned() -> None:
    cast_session = _session()

    # A Python signal handler may call stop() while the interrupted start()
    # frame owns this condition. This would deadlock with a plain Lock.
    with cast_session._stop_condition:
        cast_session.stop()

    assert cast_session._stopped


def test_interruption_before_audio_ownership_transfer_reaps_capture(
    monkeypatch,
) -> None:
    capture = object()
    stopped: list[object] = []

    class Browser:
        user_data_dir = None

    class InterruptingShutdownState:
        def is_set(self) -> bool:
            raise KeyboardInterrupt("SIGINT during AudioTee ownership handoff")

    def try_audio(_profile, **_kwargs):
        return capture

    cast_session = _session()
    cast_session.screencaster = Browser()  # type: ignore[assignment]
    cast_session._shutdown_started = InterruptingShutdownState()  # type: ignore[assignment]
    monkeypatch.setattr(session_module, "audiotee_available", lambda: True)
    monkeypatch.setattr(session_module, "try_start_chrome_audio_capture", try_audio)
    monkeypatch.setattr(
        session_module,
        "stop_audio_capture",
        lambda value: stopped.append(value) if value is not None else None,
    )

    with pytest.raises(KeyboardInterrupt, match="ownership handoff"):
        cast_session._attach_audio()

    assert cast_session.audio_capture is None
    assert stopped == [capture]


def test_interruption_after_audio_transfer_does_not_close_capture_twice(
    monkeypatch,
) -> None:
    capture = object()
    stopped: list[object] = []

    class Browser:
        user_data_dir = None

        def stop(self) -> None:
            pass

    class InterruptingSession(CastSession):
        def __init__(self, *args, **kwargs) -> None:
            self._stored_audio_capture = None
            self.interrupt_transfer = False
            super().__init__(*args, **kwargs)

        @property
        def audio_capture(self):
            return self._stored_audio_capture

        @audio_capture.setter
        def audio_capture(self, value) -> None:
            self._stored_audio_capture = value
            if self.interrupt_transfer and value is capture:
                # Model the CLI's same-thread signal handler: it tears the
                # session down, then raises before the caller clears its local.
                self.stop()
                raise KeyboardInterrupt("SIGINT after AudioTee ownership transfer")

    def try_audio(_profile, **_kwargs):
        return capture

    cast_session = InterruptingSession(
        SessionConfig(url="https://example.test", capture_audio=False)
    )
    cast_session.screencaster = Browser()  # type: ignore[assignment]
    cast_session.interrupt_transfer = True
    monkeypatch.setattr(session_module, "audiotee_available", lambda: True)
    monkeypatch.setattr(session_module, "try_start_chrome_audio_capture", try_audio)
    monkeypatch.setattr(
        session_module,
        "stop_audio_capture",
        lambda value: stopped.append(value) if value is not None else None,
    )

    with pytest.raises(KeyboardInterrupt, match="after AudioTee ownership transfer"):
        cast_session._attach_audio()

    assert cast_session.audio_capture is capture
    assert stopped == [capture]


def test_concurrent_stop_waits_for_active_teardown(monkeypatch) -> None:
    cast_session = _session()
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    second_returned = threading.Event()
    streamer = FakeComponent()
    audio_stops = []

    class BlockingBrowser:
        stop_calls = 0

        def stop(self) -> None:
            self.stop_calls += 1
            cleanup_entered.set()
            assert release_cleanup.wait(timeout=1)

    browser = BlockingBrowser()
    cast_session.screencaster = browser  # type: ignore[assignment]
    cast_session.streamer = streamer  # type: ignore[assignment]
    monkeypatch.setattr(
        session_module,
        "stop_audio_capture",
        lambda capture: audio_stops.append(capture),
    )

    first = threading.Thread(target=cast_session.stop)
    first.start()
    assert cleanup_entered.wait(timeout=1)

    condition_wait_entered = threading.Event()
    real_condition_wait = cast_session._stop_condition.wait

    def observed_condition_wait(*args, **kwargs):
        condition_wait_entered.set()
        return real_condition_wait(*args, **kwargs)

    monkeypatch.setattr(cast_session._stop_condition, "wait", observed_condition_wait)

    def stop_again() -> None:
        cast_session.stop()
        second_returned.set()

    second = threading.Thread(target=stop_again)
    second.start()
    assert condition_wait_entered.wait(timeout=1)
    assert not second_returned.is_set()

    release_cleanup.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_returned.is_set()
    assert browser.stop_calls == 1
    assert streamer.stop_calls == 1
    assert audio_stops == [None]


@pytest.mark.parametrize(
    ("fail_on_check", "expected_start_calls", "expected_ready_calls"),
    [
        (2, 1, 0),  # Browser dies during the streamer's first-frame wait.
        (3, 1, 1),  # Browser dies while waiting for the first HLS segment.
    ],
)
def test_start_monitors_browser_health_through_streamer_readiness(
    monkeypatch,
    fail_on_check,
    expected_start_calls,
    expected_ready_calls,
) -> None:
    failure = RuntimeError("browser died after page readiness")

    class Browser:
        def __init__(self, *_args, on_frame, **_kwargs) -> None:
            self.on_frame = on_frame
            self.user_data_dir = None
            self.health_checks = 0
            self.stop_calls = 0

        def start(self) -> None:
            pass

        def wait_until_ready(self, *, cancelled=None) -> None:
            assert cancelled is not None and not cancelled()

        def enable_capture(self) -> None:
            pass

        def raise_if_failed(self) -> None:
            self.health_checks += 1
            if self.health_checks == fail_on_check:
                raise failure

        def stop(self) -> None:
            self.stop_calls += 1

    class Streamer:
        last_instance = None

        def __init__(self, **_kwargs) -> None:
            type(self).last_instance = self
            self.start_calls = 0
            self.ready_calls = 0
            self.stop_calls = 0

        def publish_frame(self, _frame, _captured_at=None) -> None:
            pass

        def start(self, *, health_check) -> None:
            self.start_calls += 1
            health_check()

        def wait_until_ready(self, *, health_check) -> None:
            self.ready_calls += 1
            health_check()

        def raise_if_failed(self) -> None:
            pass

        def stop(self) -> None:
            self.stop_calls += 1

    monkeypatch.setattr(session_module, "TabScreencaster", Browser)
    monkeypatch.setattr(session_module, "HLSStreamer", Streamer)
    cast_session = _session()

    with pytest.raises(RuntimeError, match="browser died after page readiness") as caught:
        cast_session.start()

    assert caught.value is failure
    streamer = Streamer.last_instance
    assert streamer is not None
    assert streamer.start_calls == expected_start_calls
    assert streamer.ready_calls == expected_ready_calls
    cast_session.stop()
    assert streamer.stop_calls == 1


def test_audio_retry_aborts_when_ready_browser_dies(monkeypatch) -> None:
    failure = RuntimeError("browser died during audio attachment")

    class Browser:
        def __init__(self, *_args, on_frame, **_kwargs) -> None:
            self.on_frame = on_frame
            self.user_data_dir = None
            self.health_checks = 0

        def start(self) -> None:
            pass

        def wait_until_ready(self, *, cancelled=None) -> None:
            assert cancelled is not None and not cancelled()

        def enable_capture(self) -> None:
            pass

        def raise_if_failed(self) -> None:
            self.health_checks += 1
            if self.health_checks == 2:
                raise failure

        def nudge_playback(self) -> None:
            raise AssertionError("dead browser must not be nudged")

        def stop(self) -> None:
            pass

    def try_audio(_user_data_dir, *, on_retry, on_stderr, cancelled):
        del on_stderr
        assert not cancelled()
        on_retry()
        raise AssertionError("dead browser retry should have raised")

    monkeypatch.setattr(session_module, "TabScreencaster", Browser)
    monkeypatch.setattr(session_module, "audiotee_available", lambda: True)
    monkeypatch.setattr(session_module, "try_start_chrome_audio_capture", try_audio)
    cast_session = CastSession(
        SessionConfig(url="https://example.test", capture_audio=True)
    )

    with pytest.raises(RuntimeError, match="browser died during audio attachment") as caught:
        cast_session.start()

    assert caught.value is failure
    cast_session.stop()


def test_concurrent_stop_cancels_audio_start_before_streamer_creation(
    monkeypatch,
) -> None:
    attach_entered = threading.Event()
    cancellation_seen = threading.Event()
    release_attach = threading.Event()
    retry_pause = threading.Event()
    stop_returned = threading.Event()
    start_failures: list[BaseException] = []

    class Browser:
        last_instance = None

        def __init__(self, *_args, on_frame, **_kwargs) -> None:
            type(self).last_instance = self
            self.on_frame = on_frame
            self.user_data_dir = None
            self.stop_calls = 0

        def start(self) -> None:
            pass

        def wait_until_ready(self, *, cancelled=None) -> None:
            assert cancelled is not None and not cancelled()

        def enable_capture(self) -> None:
            pass

        def raise_if_failed(self) -> None:
            pass

        def nudge_playback(self) -> None:
            pass

        def stop(self) -> None:
            self.stop_calls += 1

    def try_audio(
        _user_data_dir,
        *,
        on_retry,
        on_stderr,
        cancelled,
    ):
        del on_retry, on_stderr
        attach_entered.set()
        while not cancelled():
            retry_pause.wait(0.01)
        cancellation_seen.set()
        assert release_attach.wait(timeout=1)
        raise session_module.AudioCaptureCancelled(
            "simulated cancellation inside AudioTee startup"
        )

    def unexpected_streamer(**_kwargs):
        raise AssertionError("streamer created after session stop")

    monkeypatch.setattr(session_module, "TabScreencaster", Browser)
    monkeypatch.setattr(session_module, "HLSStreamer", unexpected_streamer)
    monkeypatch.setattr(session_module, "audiotee_available", lambda: True)
    monkeypatch.setattr(session_module, "try_start_chrome_audio_capture", try_audio)

    cast_session = CastSession(
        SessionConfig(url="https://example.test", capture_audio=True)
    )

    def start() -> None:
        try:
            cast_session.start()
        except BaseException as exc:
            start_failures.append(exc)

    start_thread = threading.Thread(target=start)
    stop_thread = threading.Thread(
        target=lambda: (cast_session.stop(), stop_returned.set())
    )
    start_thread.start()
    assert attach_entered.wait(timeout=1)
    stop_thread.start()
    assert cancellation_seen.wait(timeout=1)
    assert not stop_returned.is_set()

    release_attach.set()
    start_thread.join(timeout=1)
    stop_thread.join(timeout=1)

    assert not start_thread.is_alive()
    assert not stop_thread.is_alive()
    assert stop_returned.is_set()
    assert len(start_failures) == 1
    assert isinstance(start_failures[0], session_module.AudioCaptureCancelled)
    assert cast_session.streamer is None
    browser = Browser.last_instance
    assert browser is not None and browser.stop_calls == 1


def test_health_failure_racing_shutdown_is_suppressed(monkeypatch) -> None:
    health_entered = threading.Event()
    release_health = threading.Event()
    health_failures: list[BaseException] = []

    class AudioCapture:
        def raise_if_failed(self) -> None:
            health_entered.set()
            assert release_health.wait(timeout=1)
            raise RuntimeError("helper exited during intentional stop")

    cast_session = _session()
    cast_session.audio_capture = AudioCapture()  # type: ignore[assignment]
    monkeypatch.setattr(session_module, "stop_audio_capture", lambda _capture: None)

    def check_health() -> None:
        try:
            cast_session.raise_if_failed()
        except BaseException as exc:
            health_failures.append(exc)

    health_thread = threading.Thread(target=check_health)
    health_thread.start()
    assert health_entered.wait(timeout=1)
    cast_session.stop()
    release_health.set()
    health_thread.join(timeout=1)

    assert not health_thread.is_alive()
    assert health_failures == []
