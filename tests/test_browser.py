"""Thread-lifecycle tests for browser capture (no browser process required)."""

import base64
import shutil
import threading
import time
from types import SimpleNamespace

import pytest

import cast_tab.browser as browser_module
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


def test_callback_handoff_replays_retained_static_frame_once() -> None:
    discarded = []
    published = []
    screencaster = TabScreencaster(
        "https://example.test",
        on_frame=lambda frame, captured_at: discarded.append((frame, captured_at)),
    )
    try:
        screencaster._deliver_frame(b"only-static-frame", 12.5)
        screencaster.on_frame = (
            lambda frame, captured_at: published.append((frame, captured_at))
        )

        assert published == []  # setter never invokes user code cross-thread
        assert screencaster._replay_latest_if_requested()
        assert not screencaster._replay_latest_if_requested()

        assert discarded == [(b"only-static-frame", 12.5)]
        assert published == [(b"only-static-frame", 12.5)]
    finally:
        shutil.rmtree(screencaster.user_data_dir, ignore_errors=True)


def test_new_frame_supersedes_pending_handoff_replay() -> None:
    published = []
    screencaster = _screencaster()
    try:
        screencaster._deliver_frame(b"old", 1.0)
        screencaster.on_frame = (
            lambda frame, captured_at: published.append((frame, captured_at))
        )
        screencaster._deliver_frame(b"new", 2.0)

        assert not screencaster._replay_latest_if_requested()
        assert published == [(b"new", 2.0)]
    finally:
        shutil.rmtree(screencaster.user_data_dir, ignore_errors=True)


def test_handoff_before_first_frame_does_not_fabricate_or_duplicate() -> None:
    published = []
    screencaster = _screencaster()
    try:
        screencaster.on_frame = (
            lambda frame, captured_at: published.append((frame, captured_at))
        )

        assert not screencaster._replay_latest_if_requested()
        screencaster._deliver_frame(b"first", 3.0)
        assert not screencaster._replay_latest_if_requested()
        assert published == [(b"first", 3.0)]
    finally:
        shutil.rmtree(screencaster.user_data_dir, ignore_errors=True)


def test_capture_time_nudge_is_one_immediate_dom_evaluation() -> None:
    evaluations = []

    class Page:
        def evaluate(self, script):
            evaluations.append(script)

        def locator(self, _selector):
            raise AssertionError("capture-time nudge used locator auto-waiting")

    screencaster = _screencaster()
    try:
        screencaster._try_nudge_playback(Page())
    finally:
        shutil.rmtree(screencaster.user_data_dir, ignore_errors=True)

    assert len(evaluations) == 1
    assert "document.querySelectorAll" in evaluations[0]
    assert "video,audio" in evaluations[0]
    assert "request.catch" in evaluations[0]
    assert "media.muted = true" in evaluations[0]
    assert "await" not in evaluations[0]


def test_successful_play_button_click_still_unmutes_capture_media() -> None:
    clicks: list[int] = []
    evaluations: list[str] = []

    class Locator:
        first = None

        def __init__(self):
            self.first = self

        def click(self, *, timeout):
            clicks.append(timeout)

    class Page:
        def locator(self, _selector):
            return Locator()

        def evaluate(self, script):
            evaluations.append(script)

    screencaster = TabScreencaster(
        "https://example.test",
        capture_audio=True,
        on_frame=lambda _frame, _captured_at: None,
    )
    try:
        screencaster._try_start_playback(Page())
    finally:
        shutil.rmtree(screencaster.user_data_dir, ignore_errors=True)

    assert clicks == [1500]
    assert len(evaluations) == 1
    assert "video,audio" in evaluations[0]
    assert "media.muted = false" in evaluations[0]


def test_capture_audio_nudge_unmutes_before_retrying_playback() -> None:
    evaluations: list[str] = []

    class Page:
        def evaluate(self, script):
            evaluations.append(script)

    screencaster = TabScreencaster(
        "https://example.test",
        capture_audio=True,
        on_frame=lambda _frame, _captured_at: None,
    )
    try:
        screencaster._try_nudge_playback(Page())
    finally:
        shutil.rmtree(screencaster.user_data_dir, ignore_errors=True)

    assert len(evaluations) == 1
    assert "media.muted = false" in evaluations[0]
    assert "request.catch" in evaluations[0]


def test_repeated_playback_nudges_coalesce_to_one_service_action() -> None:
    evaluations = []

    class Page:
        def evaluate(self, script):
            evaluations.append(script)

    screencaster = _screencaster()
    try:
        screencaster.nudge_playback()
        screencaster.nudge_playback()

        assert screencaster._service_playback_nudge(Page())
        assert not screencaster._service_playback_nudge(Page())
        assert len(evaluations) == 1
    finally:
        shutil.rmtree(screencaster.user_data_dir, ignore_errors=True)


def test_optional_audio_degradation_unmutes_media_on_browser_thread() -> None:
    evaluations: list[str] = []

    class Page:
        def evaluate(self, script: str) -> None:
            evaluations.append(script)

    screencaster = TabScreencaster(
        "https://example.test",
        capture_audio=True,
        on_frame=lambda _frame, _captured_at: None,
    )
    try:
        screencaster.restore_local_audio()
        screencaster.restore_local_audio()
        assert screencaster._service_local_audio_restore(Page())
        assert not screencaster._service_local_audio_restore(Page())
    finally:
        shutil.rmtree(screencaster.user_data_dir, ignore_errors=True)

    assert len(evaluations) == 2  # unmute plus keepalive resume
    assert "media.muted = false" in evaluations[0]
    assert "video,audio" in evaluations[0]


def test_local_audio_restore_retries_after_navigation_race() -> None:
    evaluations = 0

    class Page:
        def evaluate(self, _script: str) -> None:
            nonlocal evaluations
            evaluations += 1
            if evaluations == 1:
                raise RuntimeError("execution context destroyed")

    screencaster = TabScreencaster(
        "https://example.test",
        capture_audio=True,
        on_frame=lambda _frame, _captured_at: None,
    )
    try:
        screencaster.restore_local_audio()
        assert not screencaster._service_local_audio_restore(Page())
        assert screencaster._service_local_audio_restore(Page())
    finally:
        shutil.rmtree(screencaster.user_data_dir, ignore_errors=True)

    assert evaluations == 3  # failed unmute, successful unmute, keepalive resume


def test_pending_frame_is_acknowledged_before_playback_nudge() -> None:
    events = []
    delivered = []
    screencaster = TabScreencaster(
        "https://example.test",
        on_frame=lambda frame, _captured_at: delivered.append(frame),
    )

    class Page:
        def evaluate(self, _script):
            events.append("evaluate")

        def wait_for_timeout(self, _timeout):
            screencaster._stop.set()

    class CDP:
        def __init__(self):
            self.handler = None

        def on(self, _event, handler):
            self.handler = handler

        def send(self, method, params=None):
            if method == "Page.startScreencast":
                assert self.handler is not None
                self.handler(
                    {
                        "data": base64.b64encode(b"jpeg").decode(),
                        "sessionId": "frame-1",
                        "metadata": {},
                    }
                )
            elif method == "Page.screencastFrameAck":
                assert params == {"sessionId": "frame-1"}
                events.append("ack")

    try:
        screencaster.nudge_playback()
        screencaster._run_screencast(Page(), CDP())

        assert delivered == [b"jpeg"]
        assert events.index("ack") < events.index("evaluate")
    finally:
        shutil.rmtree(screencaster.user_data_dir, ignore_errors=True)


def test_system_chrome_failure_is_reported_when_fallback_succeeds(capsys) -> None:
    system_error = RuntimeError("system Chrome crashed")
    fallback_context = object()

    class Chromium:
        def __init__(self):
            self.calls = []

        def launch_persistent_context(self, _profile, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise system_error
            return fallback_context

    chromium = Chromium()
    screencaster = _screencaster()
    try:
        result = screencaster._launch_context(chromium, ["--test"])

        assert result is fallback_context
        assert chromium.calls[0]["channel"] == "chrome"
        assert "channel" not in chromium.calls[1]
        assert chromium.calls[0]["timeout"] == browser_module.BROWSER_LAUNCH_TIMEOUT_MS
        assert "system Chrome crashed" in capsys.readouterr().out
    finally:
        shutil.rmtree(screencaster.user_data_dir, ignore_errors=True)


def test_dual_browser_launch_failure_preserves_both_errors() -> None:
    failures = [RuntimeError("system failed"), OSError("bundled failed")]

    class Chromium:
        def launch_persistent_context(self, _profile, **_kwargs):
            raise failures.pop(0)

    screencaster = _screencaster()
    try:
        with pytest.raises(ExceptionGroup) as caught:
            screencaster._launch_context(Chromium(), [])

        assert [str(error) for error in caught.value.exceptions] == [
            "system failed",
            "bundled failed",
        ]
    finally:
        shutil.rmtree(screencaster.user_data_dir, ignore_errors=True)


def test_stop_after_system_launch_failure_prevents_fallback() -> None:
    calls = []
    screencaster = _screencaster()

    class Chromium:
        def launch_persistent_context(self, _profile, **kwargs):
            calls.append(kwargs)
            screencaster._stop.set()
            raise RuntimeError("launch interrupted")

    try:
        with pytest.raises(RuntimeError, match="startup was cancelled"):
            screencaster._launch_context(Chromium(), [])
        assert len(calls) == 1
    finally:
        shutil.rmtree(screencaster.user_data_dir, ignore_errors=True)


def test_stop_escalates_profile_owned_browser_then_joins(monkeypatch) -> None:
    release_worker = threading.Event()
    worker_started = threading.Event()
    screencaster = _screencaster()

    def wedged_browser():
        worker_started.set()
        release_worker.wait()

    def terminate_owned():
        release_worker.set()
        return 1

    screencaster._run_browser = wedged_browser  # type: ignore[method-assign]
    screencaster._terminate_owned_browser_processes = terminate_owned  # type: ignore[method-assign]
    monkeypatch.setattr(browser_module, "BROWSER_STOP_JOIN_S", 0.01)
    monkeypatch.setattr(browser_module, "BROWSER_POST_TERMINATE_JOIN_S", 1.0)
    screencaster.start()
    assert worker_started.wait(timeout=1.0)

    screencaster.stop()

    assert screencaster._thread is not None and not screencaster._thread.is_alive()
    assert not screencaster.user_data_dir.exists()


def test_stop_timeout_is_reported_and_can_be_retried(monkeypatch) -> None:
    release_worker = threading.Event()
    worker_started = threading.Event()
    screencaster = _screencaster()

    def wedged_browser():
        worker_started.set()
        release_worker.wait()

    screencaster._run_browser = wedged_browser  # type: ignore[method-assign]
    screencaster._terminate_owned_browser_processes = lambda: 0  # type: ignore[method-assign]
    monkeypatch.setattr(browser_module, "BROWSER_STOP_JOIN_S", 0.01)
    monkeypatch.setattr(browser_module, "BROWSER_POST_TERMINATE_JOIN_S", 0.01)
    screencaster.start()
    assert worker_started.wait(timeout=1.0)

    with pytest.raises(TimeoutError, match="profile retained"):
        screencaster.stop()

    release_worker.set()
    assert screencaster._finished.wait(timeout=1.0)
    screencaster.stop()
    assert not screencaster.user_data_dir.exists()


@pytest.mark.parametrize("failure_stage", ["launch", "context-close"])
def test_stop_terminates_profile_owned_chrome_after_worker_already_exited(
    failure_stage: str,
) -> None:
    screencaster = _screencaster()
    owned = [4242]
    termination_calls = 0

    def fail_after_possible_launch() -> None:
        # Both a launch timeout and a context.close failure can leave the
        # profile process alive after the Playwright worker exits.
        screencaster._chrome_may_be_alive = True
        raise RuntimeError(f"{failure_stage} failed")

    def scan_owned() -> list[int]:
        return list(owned)

    def terminate_owned() -> int:
        nonlocal termination_calls
        termination_calls += 1
        owned.clear()
        return 1

    screencaster._run_browser = fail_after_possible_launch  # type: ignore[method-assign]
    screencaster._owned_browser_process_ids = scan_owned  # type: ignore[method-assign]
    screencaster._terminate_owned_browser_processes = terminate_owned  # type: ignore[method-assign]
    screencaster.start()
    assert screencaster._finished.wait(timeout=1.0)
    assert screencaster._thread is not None and not screencaster._thread.is_alive()
    assert screencaster.user_data_dir.exists()

    screencaster.stop()

    assert termination_calls == 1
    assert owned == []
    assert not screencaster.user_data_dir.exists()


def test_owned_process_scan_matches_only_exact_profile(monkeypatch) -> None:
    screencaster = _screencaster()
    output = "\n".join(
        [
            f"  111 /Applications/Chrome --user-data-dir={screencaster.user_data_dir}",
            "  222 /Applications/Chrome --user-data-dir=/tmp/someone-else",
            f"  bad /Applications/Chrome --user-data-dir={screencaster.user_data_dir}",
        ]
    )
    monkeypatch.setattr(
        browser_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=output),
    )
    try:
        assert screencaster._owned_browser_process_ids() == [111]
    finally:
        shutil.rmtree(screencaster.user_data_dir, ignore_errors=True)
