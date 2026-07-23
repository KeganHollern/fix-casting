"""Lifecycle tests for the composed cast session."""

import threading
from types import SimpleNamespace

import pytest

import cast_tab.audio as audio_module
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


def test_dead_audiotee_is_replaced_with_fresh_pipe_before_old_capture_closes(
    monkeypatch,
) -> None:
    events: list[object] = []

    class FailedAudioCapture:
        read_fd = 11
        pids = (101,)
        audio_format = object()

        def raise_if_failed(self) -> None:
            raise session_module.AudioCaptureError("native capture stalled")

    failed = FailedAudioCapture()
    replacement = SimpleNamespace(
        read_fd=22,
        pids=(202,),
        audio_format=object(),
        raise_if_failed=lambda: None,
    )

    class Browser:
        user_data_dir = object()

        def raise_if_failed(self) -> None:
            events.append("browser-health")

        def nudge_playback(self) -> None:
            events.append("nudge")

    class Streamer:
        def begin_audio_source_replacement(self) -> bool:
            events.append("begin-replacement")
            return True

        def complete_audio_source_replacement(self, read_fd, audio_format) -> bool:
            events.append(("complete", read_fd, audio_format))
            return True

        def fail_audio_source_replacement(self, _failure) -> None:
            raise AssertionError("successful replacement must not abort")

        def raise_if_failed(self) -> None:
            events.append("streamer-health")

    def start_replacement(_profile, **kwargs):
        events.append("start-replacement")
        assert kwargs["timeout"] == session_module.AUDIO_RECOVERY_TIMEOUT_S
        kwargs["on_retry"]()
        return replacement

    cast_session = CastSession(
        SessionConfig(
            url="https://example.test",
            capture_audio=True,
            require_audio=True,
        )
    )
    cast_session.screencaster = Browser()  # type: ignore[assignment]
    cast_session.streamer = Streamer()  # type: ignore[assignment]
    cast_session.audio_capture = failed  # type: ignore[assignment]
    monkeypatch.setattr(
        session_module,
        "try_start_chrome_audio_capture",
        start_replacement,
    )
    monkeypatch.setattr(
        session_module,
        "stop_audio_capture",
        lambda capture: events.append(("stop", capture)),
    )

    cast_session.raise_if_failed()

    assert cast_session.audio_capture is replacement
    begin_index = events.index("begin-replacement")
    start_index = events.index("start-replacement")
    replace_index = events.index(("complete", 22, replacement.audio_format))
    stop_index = events.index(("stop", failed))
    assert begin_index < start_index < replace_index < stop_index
    assert events.count("start-replacement") == 1
    assert "nudge" in events
    assert events[-1] == "streamer-health"


def test_concurrent_audio_health_polls_perform_one_reattach(monkeypatch) -> None:
    attach_entered = threading.Event()
    release_attach = threading.Event()
    starts = 0

    class FailedAudioCapture:
        def raise_if_failed(self) -> None:
            raise session_module.AudioCaptureError("AudioTee exited")

    failed = FailedAudioCapture()
    replacement = SimpleNamespace(
        read_fd=22,
        pids=(202,),
        audio_format=object(),
        raise_if_failed=lambda: None,
    )

    class Browser:
        user_data_dir = object()

        def raise_if_failed(self) -> None:
            pass

        def nudge_playback(self) -> None:
            pass

    class Streamer:
        def begin_audio_source_replacement(self) -> bool:
            return True

        def complete_audio_source_replacement(self, _fd, _format) -> bool:
            return True

        def fail_audio_source_replacement(self, _failure) -> None:
            raise AssertionError("successful replacement must not abort")

        def raise_if_failed(self) -> None:
            pass

    def start_replacement(_profile, **_kwargs):
        nonlocal starts
        starts += 1
        attach_entered.set()
        assert release_attach.wait(timeout=1)
        return replacement

    cast_session = CastSession(
        SessionConfig(
            url="https://example.test",
            capture_audio=True,
            require_audio=True,
        )
    )
    cast_session.screencaster = Browser()  # type: ignore[assignment]
    cast_session.streamer = Streamer()  # type: ignore[assignment]
    cast_session.audio_capture = failed  # type: ignore[assignment]
    monkeypatch.setattr(
        session_module,
        "try_start_chrome_audio_capture",
        start_replacement,
    )
    monkeypatch.setattr(session_module, "stop_audio_capture", lambda _capture: None)

    failures: list[BaseException] = []

    def check() -> None:
        try:
            cast_session.raise_if_failed()
        except BaseException as exc:
            failures.append(exc)

    first = threading.Thread(target=check)
    second = threading.Thread(target=check)
    first.start()
    assert attach_entered.wait(timeout=1)
    second.start()
    release_attach.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive() and not second.is_alive()
    assert failures == []
    assert starts == 1
    assert cast_session.audio_capture is replacement


def test_deferred_audio_probe_keeps_hls_running_until_pcm_is_ready(
    monkeypatch,
) -> None:
    events: list[object] = []
    replacement = SimpleNamespace(
        read_fd=22,
        pids=(202,),
        audio_format=audio_module.DEFAULT_AUDIO_FORMAT,
        raise_if_failed=lambda: events.append("replacement-health"),
    )

    class Browser:
        user_data_dir = object()

        def raise_if_failed(self) -> None:
            events.append("browser-health")

        def nudge_playback(self) -> None:
            events.append("nudge")

        def restore_local_audio(self) -> None:
            events.append("restore-local")

    class Streamer:
        def begin_audio_source_replacement(self) -> bool:
            events.append("begin")
            return True

        def complete_audio_source_replacement(self, fd, audio_format=None) -> bool:
            events.append(("complete", fd, audio_format))
            return True

        def fail_audio_source_replacement(self, _failure) -> None:
            raise AssertionError("successful deferred attach aborted")

    class Drainer:
        def __init__(self, capture) -> None:
            assert capture is replacement

        def start(self) -> None:
            events.append("drainer-start")

        def stop(self) -> None:
            events.append("drainer-stop")

    def probe(_profile, **kwargs):
        events.append("probe")
        kwargs["on_retry"]()
        return replacement

    cast_session = CastSession(
        SessionConfig(url="https://example.test", capture_audio=True)
    )
    cast_session.screencaster = Browser()  # type: ignore[assignment]
    cast_session.streamer = Streamer()  # type: ignore[assignment]
    cast_session._audio_attach_pending = True
    cast_session._next_audio_attach_at = 0
    monkeypatch.setattr(session_module, "audiotee_available", lambda: True)
    monkeypatch.setattr(session_module, "try_start_chrome_audio_capture", probe)
    monkeypatch.setattr(session_module, "AudioPrerollDrainer", Drainer)

    cast_session._try_deferred_audio_attach()

    assert cast_session.audio_capture is replacement
    assert not cast_session._audio_attach_pending
    assert events.index("probe") < events.index("drainer-start")
    assert events.index("drainer-start") < events.index("begin")
    assert events.index("begin") < events.index("drainer-stop")
    assert events.index("drainer-stop") < events.index(
        ("complete", 22, replacement.audio_format)
    )


def test_failed_deferred_probe_does_not_interrupt_video_only_hls(
    monkeypatch,
) -> None:
    restored: list[bool] = []

    class Browser:
        user_data_dir = object()

        def raise_if_failed(self) -> None:
            pass

        def nudge_playback(self) -> None:
            pass

        def restore_local_audio(self) -> None:
            restored.append(True)

    class Streamer:
        def begin_audio_source_replacement(self) -> bool:
            raise AssertionError("HLS was interrupted before replacement PCM existed")

    cast_session = CastSession(
        SessionConfig(url="https://example.test", capture_audio=True)
    )
    cast_session.screencaster = Browser()  # type: ignore[assignment]
    cast_session.streamer = Streamer()  # type: ignore[assignment]
    cast_session._audio_attach_pending = True
    cast_session._next_audio_attach_at = 0
    monkeypatch.setattr(session_module, "audiotee_available", lambda: True)
    monkeypatch.setattr(
        session_module,
        "try_start_chrome_audio_capture",
        lambda _profile, **_kwargs: (_ for _ in ()).throw(
            session_module.AudioCaptureError("still no browser PCM")
        ),
    )

    cast_session._try_deferred_audio_attach()

    assert cast_session.audio_capture is None
    assert cast_session._audio_attach_pending
    assert cast_session._next_audio_attach_at > 0
    assert restored == [True]


def test_deferred_audio_waits_until_initial_ffmpeg_is_running(monkeypatch) -> None:
    class Browser:
        user_data_dir = object()

    class StartingStreamer:
        accepts_deferred_audio = False

    cast_session = CastSession(
        SessionConfig(url="https://example.test", capture_audio=True)
    )
    cast_session.screencaster = Browser()  # type: ignore[assignment]
    cast_session.streamer = StartingStreamer()  # type: ignore[assignment]
    cast_session._audio_attach_pending = True
    cast_session._next_audio_attach_at = 0
    monkeypatch.setattr(
        session_module,
        "try_start_chrome_audio_capture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("probed before initial ffmpeg")
        ),
    )

    cast_session._try_deferred_audio_attach()

    assert cast_session._audio_attach_pending
    assert cast_session._next_audio_attach_at == 0


def test_optional_midstream_recovery_degrades_to_silence_and_retries(
    monkeypatch,
) -> None:
    events: list[object] = []

    class FailedCapture:
        def raise_if_failed(self) -> None:
            raise session_module.AudioCaptureError("native helper stalled")

    failed = FailedCapture()

    class Browser:
        user_data_dir = object()

        def raise_if_failed(self) -> None:
            pass

        def nudge_playback(self) -> None:
            pass

        def restore_local_audio(self) -> None:
            events.append("restore-local")

    class Streamer:
        def begin_audio_source_replacement(self) -> bool:
            events.append("begin")
            return True

        def complete_audio_source_replacement(self, fd, _format=None) -> bool:
            events.append(("complete", fd))
            return True

        def fail_audio_source_replacement(self, _failure) -> None:
            raise AssertionError("optional failure should commit anullsrc")

    cast_session = CastSession(
        SessionConfig(url="https://example.test", capture_audio=True)
    )
    cast_session.screencaster = Browser()  # type: ignore[assignment]
    cast_session.streamer = Streamer()  # type: ignore[assignment]
    cast_session.audio_capture = failed  # type: ignore[assignment]
    probes: list[bool] = []

    def failed_probe(_profile, **_kwargs):
        probes.append(True)
        raise session_module.AudioCaptureError("replacement unavailable")

    monkeypatch.setattr(session_module, "audiotee_available", lambda: True)
    monkeypatch.setattr(
        session_module,
        "try_start_chrome_audio_capture",
        failed_probe,
    )
    stopped: list[object] = []
    monkeypatch.setattr(
        session_module,
        "stop_audio_capture",
        lambda capture: stopped.append(capture) if capture not in stopped else None,
    )

    cast_session._recover_audio_capture(
        failed,  # type: ignore[arg-type]
        session_module.AudioCaptureError("native helper stalled"),
    )

    assert events[:2] == ["begin", ("complete", None)]
    assert "restore-local" in events
    assert cast_session.audio_capture is None
    assert cast_session._audio_attach_pending
    assert stopped == [failed]
    assert probes == []

    # The later AudioTee proof happens while anullsrc HLS remains live. A
    # failed proof must not begin a second encoder replacement transaction.
    cast_session._next_audio_attach_at = 0
    cast_session._try_deferred_audio_attach()

    assert probes == [True]
    assert events.count("begin") == 1
    assert events.count(("complete", None)) == 1


def test_required_audio_recovery_failure_remains_terminal(monkeypatch) -> None:
    failed_transactions: list[BaseException] = []

    class FailedCapture:
        def raise_if_failed(self) -> None:
            raise session_module.AudioCaptureError("native helper stalled")

    failed = FailedCapture()

    class Browser:
        user_data_dir = object()

        def raise_if_failed(self) -> None:
            pass

        def nudge_playback(self) -> None:
            pass

    class Streamer:
        def begin_audio_source_replacement(self) -> bool:
            return True

        def complete_audio_source_replacement(self, _fd, _format=None) -> bool:
            raise AssertionError("failed required capture cannot commit")

        def fail_audio_source_replacement(self, failure) -> None:
            failed_transactions.append(failure)

    cast_session = CastSession(
        SessionConfig(
            url="https://example.test",
            capture_audio=True,
            require_audio=True,
        )
    )
    cast_session.screencaster = Browser()  # type: ignore[assignment]
    cast_session.streamer = Streamer()  # type: ignore[assignment]
    cast_session.audio_capture = failed  # type: ignore[assignment]
    monkeypatch.setattr(
        session_module,
        "try_start_chrome_audio_capture",
        lambda _profile, **_kwargs: (_ for _ in ()).throw(
            session_module.AudioCaptureError("replacement unavailable")
        ),
    )
    monkeypatch.setattr(session_module, "stop_audio_capture", lambda _capture: None)

    with pytest.raises(session_module.AudioCaptureError, match="replacement unavailable"):
        cast_session._recover_audio_capture(
            failed,  # type: ignore[arg-type]
            session_module.AudioCaptureError("native helper stalled"),
        )

    assert len(failed_transactions) == 1
    assert not cast_session._audio_attach_pending


def test_stop_cancels_inflight_audio_reattach_without_leaking_child(
    monkeypatch,
) -> None:
    attach_entered = threading.Event()
    health_failures: list[BaseException] = []
    stopped: list[object] = []

    class FailedAudioCapture:
        def raise_if_failed(self) -> None:
            raise session_module.AudioCaptureError("AudioTee exited")

    failed = FailedAudioCapture()

    class Browser:
        user_data_dir = object()

        def raise_if_failed(self) -> None:
            pass

        def nudge_playback(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class Streamer:
        failed_replacement = False

        def begin_audio_source_replacement(self) -> bool:
            return True

        def complete_audio_source_replacement(self, _fd, _format) -> bool:
            raise AssertionError("cancelled recovery must not reach handoff")

        def fail_audio_source_replacement(self, _failure) -> None:
            self.failed_replacement = True

        def raise_if_failed(self) -> None:
            pass

        def stop(self) -> None:
            pass

    def cancelled_replacement(_profile, **kwargs):
        attach_entered.set()
        assert kwargs["cancelled"] is not None
        assert threading.Event().wait(0.01) is False
        while not kwargs["cancelled"]():
            threading.Event().wait(0.01)
        raise session_module.AudioCaptureCancelled("cancelled recovery")

    cast_session = CastSession(
        SessionConfig(
            url="https://example.test",
            capture_audio=True,
            require_audio=True,
        )
    )
    cast_session.screencaster = Browser()  # type: ignore[assignment]
    cast_session.streamer = Streamer()  # type: ignore[assignment]
    cast_session.audio_capture = failed  # type: ignore[assignment]
    monkeypatch.setattr(
        session_module,
        "try_start_chrome_audio_capture",
        cancelled_replacement,
    )
    monkeypatch.setattr(
        session_module,
        "stop_audio_capture",
        lambda capture: stopped.append(capture) if capture not in stopped else None,
    )

    def check() -> None:
        try:
            cast_session.raise_if_failed()
        except BaseException as exc:
            health_failures.append(exc)

    health = threading.Thread(target=check)
    health.start()
    assert attach_entered.wait(timeout=1)
    cast_session.stop()
    health.join(timeout=1)

    assert not health.is_alive()
    assert health_failures == []
    assert stopped == [failed]
    # Retaining the identity after cleanup prevents an interrupted ownership
    # transfer from closing a reused numeric descriptor a second time.
    assert cast_session.audio_capture is failed


def test_signal_after_audio_commit_cleans_new_and_retired_captures_once(
    monkeypatch,
) -> None:
    class Process:
        stderr = None

        def __init__(self, returncode) -> None:
            self.returncode = returncode

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = 0

        def wait(self, *, timeout):
            del timeout
            self.returncode = 0
            return 0

    failed = session_module.AudioCapture(
        process=Process(1),  # type: ignore[arg-type]
        read_fd=111,
        pids=(),
        audio_format=audio_module.DEFAULT_AUDIO_FORMAT,
    )
    replacement = session_module.AudioCapture(
        process=Process(None),  # type: ignore[arg-type]
        read_fd=222,
        pids=(),
        audio_format=failed.audio_format,
    )

    class Browser:
        user_data_dir = object()

        def raise_if_failed(self) -> None:
            pass

        def nudge_playback(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class Streamer:
        def begin_audio_source_replacement(self) -> bool:
            return True

        def complete_audio_source_replacement(self, _fd, _format) -> bool:
            return True

        def fail_audio_source_replacement(self, _failure) -> None:
            pass

        def stop(self) -> None:
            pass

    class InterruptingSession(CastSession):
        def __init__(self, *args, **kwargs) -> None:
            self._stored_capture = None
            self.interrupt_commit = False
            super().__init__(*args, **kwargs)

        @property
        def audio_capture(self):
            return self._stored_capture

        @audio_capture.setter
        def audio_capture(self, value) -> None:
            self._stored_capture = value
            if self.interrupt_commit and value is replacement:
                self.stop()
                raise KeyboardInterrupt("SIGINT after replacement commit")

    cast_session = InterruptingSession(
        SessionConfig(
            url="https://example.test",
            capture_audio=True,
            require_audio=True,
        )
    )
    cast_session.screencaster = Browser()  # type: ignore[assignment]
    cast_session.streamer = Streamer()  # type: ignore[assignment]
    cast_session.audio_capture = failed
    cast_session.interrupt_commit = True
    monkeypatch.setattr(
        session_module,
        "try_start_chrome_audio_capture",
        lambda _profile, **_kwargs: replacement,
    )
    monkeypatch.setattr(audio_module.os, "close", lambda _fd: None)

    with pytest.raises(KeyboardInterrupt, match="replacement commit"):
        cast_session._recover_audio_capture(
            failed,
            session_module.AudioCaptureError("old helper failed"),
        )

    assert failed.lifecycle.completed
    assert replacement.lifecycle.completed


def test_incomplete_retired_audio_cleanup_is_retried_by_session_stop(
    monkeypatch,
) -> None:
    class Process:
        stderr = None

        def __init__(self, returncode) -> None:
            self.returncode = returncode

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = 0

        def wait(self, *, timeout):
            del timeout
            self.returncode = 0
            return 0

    failed = session_module.AudioCapture(
        process=Process(1),  # type: ignore[arg-type]
        read_fd=111,
        pids=(),
        audio_format=audio_module.DEFAULT_AUDIO_FORMAT,
    )
    replacement = session_module.AudioCapture(
        process=Process(None),  # type: ignore[arg-type]
        read_fd=222,
        pids=(),
        audio_format=failed.audio_format,
    )

    class Browser:
        user_data_dir = object()

        def raise_if_failed(self) -> None:
            pass

        def nudge_playback(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class Streamer:
        def begin_audio_source_replacement(self) -> bool:
            return True

        def complete_audio_source_replacement(self, _fd, _format) -> bool:
            return True

        def fail_audio_source_replacement(self, _failure) -> None:
            raise AssertionError("successful handoff must not abort")

        def stop(self) -> None:
            pass

    real_stop = audio_module.stop_audio_capture
    stop_attempts: list[object] = []
    closed_fds: list[int] = []
    fail_old_once = True

    def flaky_stop(capture) -> None:
        nonlocal fail_old_once
        stop_attempts.append(capture)
        if capture is failed and fail_old_once:
            fail_old_once = False
            raise TimeoutError("old AudioTee still exiting")
        real_stop(capture)

    cast_session = CastSession(
        SessionConfig(
            url="https://example.test",
            capture_audio=True,
            require_audio=True,
        )
    )
    cast_session.screencaster = Browser()  # type: ignore[assignment]
    cast_session.streamer = Streamer()  # type: ignore[assignment]
    cast_session.audio_capture = failed
    monkeypatch.setattr(
        session_module,
        "try_start_chrome_audio_capture",
        lambda _profile, **_kwargs: replacement,
    )
    monkeypatch.setattr(session_module, "stop_audio_capture", flaky_stop)
    monkeypatch.setattr(audio_module.os, "close", closed_fds.append)

    with pytest.raises(TimeoutError, match="old AudioTee still exiting"):
        cast_session._recover_audio_capture(
            failed,
            session_module.AudioCaptureError("old helper failed"),
        )

    assert cast_session.audio_capture is replacement
    assert cast_session._retired_audio_captures == [failed]
    assert not failed.lifecycle.completed

    cast_session.stop()
    cast_session.stop()

    assert failed.lifecycle.completed
    assert replacement.lifecycle.completed
    assert cast_session._retired_audio_captures == []
    assert stop_attempts.count(failed) == 2
    assert stop_attempts.count(replacement) == 1
    assert closed_fds.count(111) == 1
    assert closed_fds.count(222) == 1


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
