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

        def wait_until_ready(self) -> None:
            pass

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

        def publish_frame(self, _frame) -> None:
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

        def wait_until_ready(self) -> None:
            pass

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

    def try_audio(_user_data_dir, *, on_retry, on_stderr):
        del on_stderr
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
