"""Thread-lifecycle tests for browser capture (no browser process required)."""

import threading
import time

import pytest

from cast_tab.browser import TabScreencaster


def _screencaster() -> TabScreencaster:
    return TabScreencaster("https://example.test", on_frame=lambda _frame: None)


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
