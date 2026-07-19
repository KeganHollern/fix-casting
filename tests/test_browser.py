"""Thread-lifecycle tests for browser capture (no browser process required)."""

import shutil
import threading
import time

import pytest

from cast_tab.browser import TabScreencaster


def _screencaster() -> TabScreencaster:
    return TabScreencaster(
        "https://example.test", on_frame=lambda _frame, _captured_at: None
    )


def _mark_ready(screencaster: TabScreencaster) -> None:
    screencaster._ready.set()
    screencaster._startup_finished.set()


def test_wait_until_ready_propagates_worker_failure_immediately() -> None:
    screencaster = _screencaster()
    failure = OSError("chrome launch failed")

    def fail() -> None:
        raise failure

    screencaster._run_browser = fail  # type: ignore[method-assign]
    started = time.monotonic()
    screencaster.start()

    with pytest.raises(OSError, match="chrome launch failed") as caught:
        screencaster.wait_until_ready(timeout=5)

    assert caught.value is failure
    assert time.monotonic() - started < 1
    screencaster.stop()


def test_failure_after_readiness_is_reported_by_health_check() -> None:
    screencaster = _screencaster()
    fail_capture = threading.Event()
    failure = RuntimeError("CDP connection closed")

    def run_until_failure() -> None:
        _mark_ready(screencaster)
        fail_capture.wait()
        raise failure

    screencaster._run_browser = run_until_failure  # type: ignore[method-assign]
    screencaster.start()
    screencaster.wait_until_ready(timeout=1)

    fail_capture.set()
    assert screencaster._finished.wait(1)
    with pytest.raises(RuntimeError, match="CDP connection closed") as caught:
        screencaster.raise_if_failed()

    assert caught.value is failure
    screencaster.stop()


def test_clean_unexpected_worker_exit_is_unhealthy() -> None:
    screencaster = _screencaster()
    exit_capture = threading.Event()

    def exit_after_ready() -> None:
        _mark_ready(screencaster)
        exit_capture.wait()

    screencaster._run_browser = exit_after_ready  # type: ignore[method-assign]
    screencaster.start()
    screencaster.wait_until_ready(timeout=1)
    exit_capture.set()
    assert screencaster._finished.wait(1)

    with pytest.raises(RuntimeError, match="exited unexpectedly"):
        screencaster.raise_if_failed()

    screencaster.stop()


def test_stop_wakes_startup_waiter_and_is_not_a_health_failure() -> None:
    screencaster = _screencaster()
    worker_started = threading.Event()

    def wait_for_stop() -> None:
        worker_started.set()
        screencaster._stop.wait()

    screencaster._run_browser = wait_for_stop  # type: ignore[method-assign]
    screencaster.start()
    assert worker_started.wait(1)
    screencaster.stop()

    with pytest.raises(RuntimeError, match="stopped before the tab was ready"):
        screencaster.wait_until_ready(timeout=1)
    screencaster.raise_if_failed()


def test_startup_wait_honors_session_cancellation_immediately() -> None:
    screencaster = _screencaster()
    cancelled = threading.Event()
    cancelled.set()
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="startup was cancelled"):
            screencaster.wait_until_ready(
                timeout=10,
                cancelled=cancelled.is_set,
            )
    finally:
        shutil.rmtree(screencaster.user_data_dir, ignore_errors=True)

    assert time.monotonic() - started < 0.1


def test_audio_capture_installs_and_resumes_inaudible_keepalive() -> None:
    scripts: list[str] = []
    evaluations: list[str] = []

    class Context:
        def add_init_script(self, *, script: str) -> None:
            scripts.append(script)

    class Page:
        def evaluate(self, script: str) -> None:
            evaluations.append(script)

    screencaster = TabScreencaster(
        "https://example.test",
        capture_audio=True,
        on_frame=lambda _frame, _captured_at: None,
    )
    try:
        screencaster._install_audio_keepalive(Context())
        screencaster._ensure_audio_keepalive(Page())
    finally:
        shutil.rmtree(screencaster.user_data_dir, ignore_errors=True)

    assert len(scripts) == 1
    assert "globalThis.top !== globalThis" in scripts[0]
    assert "createOscillator" in scripts[0]
    assert "gain.gain.setValueAtTime(1e-7" in scripts[0]
    assert "__fixCastingAudioKeepalive" in scripts[0]
    assert len(evaluations) == 1
    assert "__fixCastingEnsureAudioKeepalive" in evaluations[0]
    assert "void ensure().catch" in evaluations[0]


def test_video_only_capture_does_not_touch_web_audio() -> None:
    class UnexpectedTarget:
        def add_init_script(self, **_kwargs) -> None:
            raise AssertionError("video-only session installed an audio keepalive")

        def evaluate(self, _script: str) -> None:
            raise AssertionError("video-only session resumed an audio keepalive")

    screencaster = _screencaster()
    try:
        target = UnexpectedTarget()
        screencaster._install_audio_keepalive(target)
        screencaster._ensure_audio_keepalive(target)
    finally:
        shutil.rmtree(screencaster.user_data_dir, ignore_errors=True)


def test_chrome_capture_timestamp_is_translated_to_monotonic_clock() -> None:
    captured_at, lag = TabScreencaster._capture_monotonic_time(
        999.975,
        wall_now=1000.0,
        monotonic_now=50.0,
    )
    assert lag == pytest.approx(0.025)
    assert captured_at == pytest.approx(49.975)

    fallback, invalid_lag = TabScreencaster._capture_monotonic_time(
        900.0,
        wall_now=1000.0,
        monotonic_now=50.0,
    )
    assert fallback == 50.0
    assert invalid_lag is None
