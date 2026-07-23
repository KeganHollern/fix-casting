"""Lifecycle for one cast pipeline: browser capture + tab audio + HLS streamer.

Owns the wiring the CLI and the tools/ measurement harnesses previously each
hand-rolled: browser tab capture → (optional) per-tab audio tap → HLS encoder,
brought up in the order that keeps A/V anchored (capture first, audio attach,
then ffmpeg held until the first frame). The Chromecast itself is deliberately
not part of the session — the harnesses measure the pipeline with no TV.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cast_tab.audio import (
    AudioCapture,
    AudioCaptureCancelled,
    AudioCaptureError,
    AudioPrerollDrainer,
    audiotee_available,
    install_hint,
    stop_audio_capture,
    try_start_chrome_audio_capture,
)
from cast_tab.browser import TabScreencaster
from cast_tab.encoder import DEFAULT_JPEG_QUALITY
from cast_tab.stats import PipelineStats
from cast_tab.streamer import HLSStreamer

AUDIO_RECOVERY_TIMEOUT_S = 5.0
AUDIO_RECOVERY_WINDOW_S = 60.0
AUDIO_RECOVERY_MAX_ATTEMPTS = 3
AUDIO_RECOVERY_BASE_DELAY_S = 0.5
AUDIO_DEFERRED_RETRY_INTERVAL_S = 15.0
AUDIO_DEFERRED_ATTACH_TIMEOUT_S = 5.0


@dataclass
class SessionConfig:
    """Everything one cast pipeline needs, minus the Chromecast."""

    url: str
    width: int = 1920
    height: int = 1080
    fps: int = 30  # encode rate; capture itself is paint-driven
    jpeg_quality: int = DEFAULT_JPEG_QUALITY
    buffered: bool = True
    headless: bool = False
    capture_audio: bool = True
    # Raise instead of degrading to video-only when audio can't attach.
    # The CLI degrades; the measurement harnesses need the audio track.
    require_audio: bool = False
    audio_offset_ms: int = 0
    audio_drift_ppm: float = 0.0
    video_bitrate_mbps: float | None = None
    adblock_patterns: list[str] | None = None
    # None = the streamer makes (and owns) a per-run temp dir.
    work_dir: Path | None = None


class CastSession:
    """Bring up and tear down the capture→encode pipeline as one unit.

    start() blocks until the HLS stream is ready to serve; stop() is
    idempotent and tears down in the safe order regardless of how far
    start() got.
    """

    def __init__(
        self,
        config: SessionConfig,
        *,
        stats: PipelineStats | None = None,
    ) -> None:
        self.config = config
        self.stats = stats
        self.screencaster: TabScreencaster | None = None
        self.streamer: HLSStreamer | None = None
        self.audio_capture: AudioCapture | None = None
        # Signal handlers run on the interrupted main thread and can enter
        # stop() between any two bytecodes in start()/stop().  The condition's
        # lock must therefore be reentrant for same-thread teardown.
        self._stop_lock = threading.RLock()
        self._stop_condition = threading.Condition(self._stop_lock)
        self._stop_in_progress = False
        self._stop_owner: int | None = None
        self._start_in_progress = False
        self._start_owner: int | None = None
        self._start_started = False
        self._shutdown_started = threading.Event()
        # The CLI's POSIX signal handler cannot safely call Event.set(): a
        # repeated signal can re-enter while Event's non-reentrant condition
        # lock is held. It installs a lock-free boolean reader here instead;
        # ordinary stop() still sets the Event to wake owned worker waits.
        self._external_cancellation: Callable[[], bool] = lambda: False
        self._stopped = False
        self._cleanup_complete: set[str] = set()
        # A signal handler can re-enter stop() on the same thread while an
        # attachment transaction owns this lock.
        self._audio_recovery_lock = threading.RLock()
        self._audio_stderr_callback: Callable[[str], None] | None = None
        self._audio_recovery_attempts: deque[float] = deque()
        self._audio_attach_pending = False
        self._next_audio_attach_at = 0.0
        # Captures displaced by a handoff remain session-owned until their
        # phased lifecycle says teardown completed. This prevents a timeout
        # during cleanup from orphaning a child whose fd/process still needs a
        # later retry.
        self._retired_audio_captures: list[AudioCapture] = []

    @property
    def audio_active(self) -> bool:
        return self.audio_capture is not None

    @property
    def playlist_url(self) -> str:
        if self.streamer is None:
            raise RuntimeError("Session not started.")
        return self.streamer.playlist_url

    def raise_if_failed(self) -> None:
        """Surface asynchronous failures in components owned by the session."""
        if self._cancellation_requested():
            return
        try:
            screencaster = self.screencaster
            if screencaster is not None:
                screencaster.raise_if_failed()
            audio_capture = self.audio_capture
            if audio_capture is not None:
                self._raise_audio_if_failed(audio_capture)
            elif self.config.capture_audio:
                self._try_deferred_audio_attach()
            streamer = self.streamer
            if streamer is not None:
                streamer.raise_if_failed()
        except Exception:
            # A health poll can race intentional component termination from a
            # different thread (notably the TUI poller).  Shutdown owns that
            # exit; do not turn it into a spurious runtime failure.
            if self._cancellation_requested():
                return
            raise

    def set_cancellation_probe(self, cancelled: Callable[[], bool]) -> None:
        """Install a signal-safe external cancellation reader before start()."""
        if self._start_started:
            raise RuntimeError("Session cancellation must be configured before start().")
        self._external_cancellation = cancelled

    def _cancellation_requested(self) -> bool:
        return self._shutdown_started.is_set() or self._external_cancellation()

    def _wait_for_cancellation(self, timeout: float) -> bool:
        """Wait responsively for internal Event or signal-safe external flag."""
        deadline = time.monotonic() + timeout
        while True:
            if self._cancellation_requested():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._cancellation_requested()
            # The OS handler never touches this Event, so interruption while
            # its condition lock is held cannot re-enter Event.set().
            self._shutdown_started.wait(min(0.05, remaining))

    def request_stop(self) -> None:
        """Publish cancellation from ordinary (non-signal-handler) control flow."""
        self._shutdown_started.set()

    def start(self) -> None:
        caller = threading.get_ident()
        with self._stop_condition:
            if self._start_started or self._cancellation_requested():
                raise RuntimeError("Cast session has already been started or stopped.")
            self._start_started = True
            self._start_owner = caller
            self._start_in_progress = True
        try:
            self._start_pipeline()
        finally:
            with self._stop_condition:
                self._start_in_progress = False
                self._start_owner = None
                self._stop_condition.notify_all()

    def _start_pipeline(self) -> None:
        self._raise_start_not_cancelled()
        cfg = self.config
        self.screencaster = TabScreencaster(
            cfg.url,
            width=cfg.width,
            height=cfg.height,
            fps=cfg.fps,
            jpeg_quality=cfg.jpeg_quality,
            on_frame=lambda _frame, _captured_at: None,
            headless=cfg.headless,
            capture_audio=cfg.capture_audio,
            stats=self.stats,
            adblock_patterns=cfg.adblock_patterns,
        )
        self.screencaster.start()
        self.screencaster.wait_until_ready(
            cancelled=self._cancellation_requested,
        )
        self._raise_start_not_cancelled()
        self.screencaster.enable_capture()
        if self.stats is not None:
            self.stats.trace("enable_capture")
        self.screencaster.raise_if_failed()

        if cfg.capture_audio:
            self._attach_audio()
            self._raise_start_not_cancelled()
            self.screencaster.raise_if_failed()

        self._raise_start_not_cancelled()
        self.streamer = HLSStreamer(
            width=cfg.width,
            height=cfg.height,
            fps=cfg.fps,
            buffered=cfg.buffered,
            audio_fd=self.audio_capture.read_fd if self.audio_capture else None,
            audio_format=(
                self.audio_capture.audio_format if self.audio_capture else None
            ),
            audio_offset_ms=cfg.audio_offset_ms,
            audio_drift_ppm=cfg.audio_drift_ppm,
            video_bitrate_mbps=cfg.video_bitrate_mbps,
            work_dir=cfg.work_dir,
            stats=self.stats,
        )
        self.screencaster.on_frame = self.streamer.publish_frame
        if self.stats is not None:
            self.stats.trace("on_frame wired to streamer")
        input_health_check = self._raise_input_if_failed
        self.streamer.start(health_check=input_health_check)
        self.streamer.wait_until_ready(health_check=input_health_check)
        self._raise_start_not_cancelled()

    def _raise_start_not_cancelled(self) -> None:
        if self._cancellation_requested():
            raise AudioCaptureCancelled("Cast session startup was cancelled.")

    def _raise_input_if_failed(self) -> None:
        """Check capture workers while ffmpeg waits for its first segment."""
        self._raise_start_not_cancelled()
        screencaster = self.screencaster
        if screencaster is not None:
            screencaster.raise_if_failed()
        audio_capture = self.audio_capture
        if audio_capture is not None:
            self._raise_audio_if_failed(audio_capture)
        elif self.config.capture_audio:
            self._try_deferred_audio_attach()

    def _raise_audio_if_failed(self, audio_capture: AudioCapture) -> None:
        try:
            audio_capture.raise_if_failed()
        except AudioCaptureError as exc:
            if not self.config.capture_audio:
                raise
            self._recover_audio_capture(audio_capture, exc)

    def _mark_audio_degraded(self, failure: BaseException) -> None:
        """Keep optional audio retryable and restore audible browser fallback."""
        self._audio_attach_pending = True
        self._next_audio_attach_at = (
            time.monotonic() + AUDIO_DEFERRED_RETRY_INTERVAL_S
        )
        screencaster = self.screencaster
        if screencaster is not None:
            restore = getattr(screencaster, "restore_local_audio", None)
            if callable(restore):
                restore()
        if self.stats is not None:
            self.stats.trace(f"audio degraded: {failure}")

    def _retire_audio_capture(self, capture: AudioCapture) -> None:
        """Keep teardown ownership until this exact capture is fully closed."""
        with self._audio_recovery_lock:
            if not any(
                existing is capture for existing in self._retired_audio_captures
            ):
                self._retired_audio_captures.append(capture)

    @staticmethod
    def _audio_cleanup_completed(
        capture: AudioCapture,
        *,
        stop_returned: bool,
    ) -> bool:
        lifecycle = getattr(capture, "lifecycle", None)
        if lifecycle is None:
            # Lightweight test doubles have no phased lifecycle. A successful
            # stop call is their completion signal.
            return stop_returned
        return bool(lifecycle.completed)

    def _forget_retired_audio_capture(self, capture: AudioCapture) -> None:
        self._retired_audio_captures = [
            existing
            for existing in self._retired_audio_captures
            if existing is not capture
        ]

    @staticmethod
    def _raise_audio_cleanup_failures(failures: list[BaseException]) -> None:
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup(
                "Multiple AudioTee captures could not be cleaned up",
                failures,
            )

    def _cleanup_retired_audio_captures(self) -> None:
        """Attempt every retired capture and retain incomplete ones for retry."""
        with self._audio_recovery_lock:
            failures: list[BaseException] = []
            for capture in tuple(self._retired_audio_captures):
                stop_returned = False
                try:
                    stop_audio_capture(capture)
                    stop_returned = True
                except BaseException as exc:
                    failures.append(exc)
                if self._audio_cleanup_completed(
                    capture,
                    stop_returned=stop_returned,
                ):
                    self._forget_retired_audio_capture(capture)
            self._raise_audio_cleanup_failures(failures)

    def _cleanup_all_audio_captures(self) -> None:
        """Stop current and retired captures once each, without fail-fast leaks."""
        with self._audio_recovery_lock:
            captures: list[AudioCapture] = []
            current = self.audio_capture
            if current is not None:
                captures.append(current)
            for retired in self._retired_audio_captures:
                if not any(owned is retired for owned in captures):
                    captures.append(retired)

            if not captures:
                # Preserve the cleanup phase even for a video-only session;
                # stop_audio_capture(None) is the canonical no-op.
                stop_audio_capture(None)
                return

            failures: list[BaseException] = []
            for capture in captures:
                stop_returned = False
                try:
                    stop_audio_capture(capture)
                    stop_returned = True
                except BaseException as exc:
                    failures.append(exc)
                if self._audio_cleanup_completed(
                    capture,
                    stop_returned=stop_returned,
                ):
                    self._forget_retired_audio_capture(capture)
            self._raise_audio_cleanup_failures(failures)

    def _attach_audio(self) -> None:
        """Tap the cast browser's audio; degrade to video-only unless required."""
        if not audiotee_available():
            if self.config.require_audio:
                raise AudioCaptureError(
                    f"AudioTee not found.\n{install_hint()}"
                )
            print(f"Audio unavailable: AudioTee not found.\n{install_hint()}")
            self._mark_audio_degraded(AudioCaptureError("AudioTee not found"))
            return

        print("Waiting for cast browser audio...")
        attached = False

        def on_stderr(line: str) -> None:
            mtype = "log"
            text = line
            try:
                parsed = json.loads(line)
                mtype = str(parsed.get("message_type", "log"))
                data = parsed.get("data") or {}
                text = str(data.get("message", line))
                context = data.get("context")
                if context:
                    text += f" {context}"
            except (ValueError, AttributeError):
                pass
            # Debug lines are high-volume (per-PID tap attempts); keep
            # them out of the log but still surface info/warning/error.
            if mtype == "debug":
                return
            # AudioTee probes PID candidates that do not tap on modern
            # macOS (renderers). "failed to translate" only happens
            # during that probing, so it is always search noise; a bare
            # "failure" is suppressed only until a tap succeeds, so a
            # mid-stream AudioTee death still surfaces.
            low = text.strip().lower()
            if "failed to translate" in low:
                return
            if not attached and (
                low in ("error: failure", "failure")
                or low.startswith("starting audiotee")
            ):
                return
            print(f"[audio:{mtype}] {text}", flush=True)
            if self.stats is not None and (
                mtype in ("error", "warning")
                or any(
                    kw in text.lower()
                    for kw in (
                        "drop", "underrun", "overrun",
                        "glitch", "discontinu", "xrun",
                    )
                )
            ):
                self.stats.record_audio_warning(text)

        self._audio_stderr_callback = on_stderr

        if self.stats is not None:
            self.stats.trace("audio try_start begin")
        capture: AudioCapture | None = None
        try:
            screencaster = self.screencaster
            assert screencaster is not None

            def retry_audio_attach() -> None:
                # AudioTee attachment can retry for tens of seconds after the
                # page was marked ready. Do not keep probing dead Chrome PIDs.
                self._raise_start_not_cancelled()
                screencaster.raise_if_failed()
                screencaster.nudge_playback()

            capture = try_start_chrome_audio_capture(
                screencaster.user_data_dir,
                on_retry=retry_audio_attach,
                on_stderr=on_stderr,
                cancelled=self._cancellation_requested,
            )
            if self._cancellation_requested():
                raise AudioCaptureCancelled("Cast session startup was cancelled.")
            self.audio_capture = capture
            # Ownership transferred to the session. Until this assignment and
            # local clear both complete, the finally block owns/reaps the child
            # if a same-thread signal raises SystemExit between bytecodes.
            capture = None
            attached = True
            self._audio_attach_pending = False
            if self.stats is not None:
                self.stats.trace(f"audio attached (pids={self.audio_capture.pids})")
            print(
                "Capturing audio from cast browser only "
                f"(PIDs: {', '.join(str(pid) for pid in self.audio_capture.pids)})."
            )
            print("Other Mac apps keep their normal audio output.")
        except AudioCaptureCancelled:
            raise
        except AudioCaptureError as exc:
            if self.config.require_audio:
                raise
            print(f"Audio unavailable: {exc}")
            print(install_hint())
            self._mark_audio_degraded(exc)
        finally:
            # If assignment completed before an asynchronous signal, stop()
            # already owns (and may already have closed) this exact capture.
            # Never close the numeric fd twice: it could have been reused.
            if capture is not None and self.audio_capture is not capture:
                self._retire_audio_capture(capture)
                self._cleanup_retired_audio_captures()

    def _try_deferred_audio_attach(self) -> None:
        """Late-attach real audio while a healthy anullsrc stream keeps running."""
        if (
            not self._audio_attach_pending
            or self._cancellation_requested()
            or time.monotonic() < self._next_audio_attach_at
        ):
            return

        with self._audio_recovery_lock:
            if (
                not self._audio_attach_pending
                or self.audio_capture is not None
                or self._cancellation_requested()
                or time.monotonic() < self._next_audio_attach_at
            ):
                return
            screencaster = self.screencaster
            streamer = self.streamer
            if screencaster is None or streamer is None:
                return
            if not getattr(streamer, "accepts_deferred_audio", True):
                # Before the initial ffmpeg exists, stopping the temporary
                # drainer would let the replacement's sub-second native queue
                # overflow while start() still waits for its first video frame.
                return
            self._next_audio_attach_at = (
                time.monotonic() + AUDIO_DEFERRED_RETRY_INTERVAL_S
            )
            if not audiotee_available():
                return
            print("Retrying cast browser audio attachment...", flush=True)

            def retry_audio_attach() -> None:
                self._raise_start_not_cancelled()
                screencaster.raise_if_failed()
                screencaster.nudge_playback()

            replacement: AudioCapture | None = None
            drainer: AudioPrerollDrainer | None = None
            replacement_transaction = False
            try:
                # Probe while the current anullsrc encoder continues serving
                # HLS. Once PCM exists, a temporary discard-only reader keeps
                # the small native queue flowing during old-ffmpeg teardown.
                replacement = try_start_chrome_audio_capture(
                    screencaster.user_data_dir,
                    timeout=AUDIO_DEFERRED_ATTACH_TIMEOUT_S,
                    on_retry=retry_audio_attach,
                    on_stderr=self._audio_stderr_callback,
                    cancelled=self._cancellation_requested,
                )
                replacement.raise_if_failed()
                drainer = AudioPrerollDrainer(replacement)
                drainer.start()
                if not streamer.begin_audio_source_replacement():
                    raise AudioCaptureCancelled(
                        "Deferred audio attachment was cancelled before handoff."
                    )
                replacement_transaction = True
                drainer.stop()
                drainer = None
                replacement.raise_if_failed()
                if not streamer.complete_audio_source_replacement(
                    replacement.read_fd,
                    replacement.audio_format,
                ):
                    raise AudioCaptureCancelled(
                        "Deferred audio attachment was cancelled during handoff."
                    )
                replacement_transaction = False
                self._raise_start_not_cancelled()

                self.audio_capture = replacement
                replacement = None
                self._audio_attach_pending = False
                current = self.audio_capture
                assert current is not None
                if self.stats is not None:
                    self.stats.trace(f"deferred audio attached (pids={current.pids})")
                print(
                    "Cast browser audio attached after startup "
                    f"(PIDs: {', '.join(str(pid) for pid in current.pids)}).",
                    flush=True,
                )
            except AudioCaptureCancelled:
                if replacement_transaction:
                    streamer.fail_audio_source_replacement(
                        AudioCaptureCancelled("Deferred audio attachment cancelled")
                    )
                raise
            except Exception as exc:
                if replacement_transaction:
                    try:
                        streamer.complete_audio_source_replacement(None)
                    except Exception:
                        streamer.fail_audio_source_replacement(exc)
                        raise
                    replacement_transaction = False
                self._mark_audio_degraded(exc)
                print(
                    f"Audio still unavailable ({exc}); will retry later.",
                    flush=True,
                )
            finally:
                if drainer is not None:
                    try:
                        drainer.stop()
                    except Exception:
                        pass
                if replacement_transaction:
                    streamer.fail_audio_source_replacement(
                        RuntimeError("Deferred audio handoff did not complete")
                    )
                if replacement is not None and self.audio_capture is not replacement:
                    self._retire_audio_capture(replacement)
                    self._cleanup_retired_audio_captures()

    def _recover_audio_capture(
        self,
        failed_capture: AudioCapture,
        failure: AudioCaptureError,
    ) -> None:
        """Replace a failed helper and its pipe without reusing either timeline."""
        with self._audio_recovery_lock:
            if self._cancellation_requested():
                return
            # Another health poll may have completed the recovery while this
            # caller waited for the serialization lock.
            if self.audio_capture is not failed_capture:
                return
            try:
                failed_capture.raise_if_failed()
            except AudioCaptureError:
                pass
            else:
                return

            screencaster = self.screencaster
            streamer = self.streamer
            if screencaster is None or streamer is None:
                raise failure

            if not self.config.require_audio:
                # Do not hold the HLS encoder offline while probing AudioTee.
                # Commit a fresh silent timeline immediately, then let the
                # deferred attach path prove PCM exists while HLS stays live.
                degradation_transaction = False
                self._retire_audio_capture(failed_capture)
                try:
                    if not streamer.begin_audio_source_replacement():
                        raise AudioCaptureCancelled(
                            "Audio degradation was cancelled before handoff."
                        )
                    degradation_transaction = True
                    if not streamer.complete_audio_source_replacement(None):
                        raise AudioCaptureCancelled(
                            "Audio degradation was cancelled during handoff."
                        )
                    degradation_transaction = False
                    self.audio_capture = None
                    self._mark_audio_degraded(failure)
                    print(
                        f"Audio unavailable ({failure}); continuing with local "
                        "browser audio and retrying later.",
                        flush=True,
                    )
                    return
                except BaseException as exc:
                    if degradation_transaction:
                        streamer.fail_audio_source_replacement(exc)
                    raise
                finally:
                    self._cleanup_retired_audio_captures()

            now = time.monotonic()
            while (
                self._audio_recovery_attempts
                and now - self._audio_recovery_attempts[0]
                > AUDIO_RECOVERY_WINDOW_S
            ):
                self._audio_recovery_attempts.popleft()
            if len(self._audio_recovery_attempts) >= AUDIO_RECOVERY_MAX_ATTEMPTS:
                circuit_failure = AudioCaptureError(
                    "Audio capture failed repeatedly; recovery circuit opened "
                    f"after {AUDIO_RECOVERY_MAX_ATTEMPTS} attempts in "
                    f"{AUDIO_RECOVERY_WINDOW_S:.0f}s"
                )
                raise circuit_failure from failure
            retry_delay = AUDIO_RECOVERY_BASE_DELAY_S * len(
                self._audio_recovery_attempts
            )
            self._audio_recovery_attempts.append(now)
            if retry_delay and self._wait_for_cancellation(retry_delay):
                raise AudioCaptureCancelled("Audio capture recovery was cancelled.")

            print(f"Audio capture failed ({failure}); reattaching...", flush=True)
            if self.stats is not None:
                self.stats.trace("audio reattach begin")

            def retry_audio_attach() -> None:
                self._raise_start_not_cancelled()
                screencaster.raise_if_failed()
                screencaster.nudge_playback()

            replacement: AudioCapture | None = None
            replacement_transaction = False
            self._retire_audio_capture(failed_capture)
            try:
                if not streamer.begin_audio_source_replacement():
                    raise AudioCaptureCancelled(
                        "Audio capture recovery was cancelled before teardown."
                    )
                replacement_transaction = True
                replacement = try_start_chrome_audio_capture(
                    screencaster.user_data_dir,
                    timeout=AUDIO_RECOVERY_TIMEOUT_S,
                    on_retry=retry_audio_attach,
                    on_stderr=self._audio_stderr_callback,
                    cancelled=self._cancellation_requested,
                )
                self._raise_start_not_cancelled()
                replacement.raise_if_failed()
                if not streamer.complete_audio_source_replacement(
                    replacement.read_fd,
                    replacement.audio_format,
                ):
                    raise AudioCaptureCancelled(
                        "Audio capture recovery was cancelled during streamer handoff."
                    )
                self._raise_start_not_cancelled()

                self.audio_capture = replacement
                replacement = None
                replacement_transaction = False
                current = self.audio_capture
                assert current is not None
                if self.stats is not None:
                    self.stats.trace(f"audio reattached (pids={current.pids})")
                print(
                    "Audio capture restored from fresh browser PIDs "
                    f"({', '.join(str(pid) for pid in current.pids)}).",
                    flush=True,
                )
            except BaseException as exc:
                if replacement_transaction:
                    streamer.fail_audio_source_replacement(exc)
                raise
            finally:
                # Until ownership is assigned above, this scope owns the new
                # child and descriptor. Never leave it behind on cancellation
                # or a failed ffmpeg handoff.
                if replacement is not None and self.audio_capture is not replacement:
                    self._retire_audio_capture(replacement)
                # Retired descriptors remain session-owned even if cleanup
                # times out. stop() will retry every incomplete lifecycle.
                self._cleanup_retired_audio_captures()

    def stop(self) -> None:
        self.request_stop()
        caller = threading.get_ident()
        with self._stop_condition:
            while self._start_in_progress and self._start_owner != caller:
                self._stop_condition.wait()
            if self._stopped:
                return
            if self._stop_in_progress and self._stop_owner == caller:
                # Signal handlers and component callbacks can re-enter stop on
                # the same thread. Waiting for ourselves would deadlock; the
                # outer call still owns and will complete every cleanup step.
                return
            while self._stop_in_progress:
                self._stop_condition.wait()
                if self._stopped:
                    return
            self._stop_in_progress = True
            self._stop_owner = caller

        def stop_screencaster() -> None:
            if self.screencaster is not None:
                self.screencaster.stop()

        def stop_streamer() -> None:
            if self.streamer is not None:
                self.streamer.stop()

        def stop_audio() -> None:
            self._cleanup_all_audio_captures()

        steps = (
            ("browser capture", stop_screencaster),
            ("HLS streamer", stop_streamer),
            ("audio capture", stop_audio),
        )
        failures: list[tuple[str, BaseException]] = []
        try:
            for name, cleanup in steps:
                if name in self._cleanup_complete:
                    continue
                try:
                    cleanup()
                except BaseException as exc:
                    failures.append((name, exc))
                else:
                    self._cleanup_complete.add(name)
        finally:
            with self._stop_condition:
                self._stopped = len(self._cleanup_complete) == len(steps)
                self._stop_in_progress = False
                self._stop_owner = None
                self._stop_condition.notify_all()

        if len(failures) == 1:
            name, failure = failures[0]
            failure.add_note(f"CastSession failed while stopping {name}.")
            raise failure
        if failures:
            names = ", ".join(name for name, _failure in failures)
            raise BaseExceptionGroup(
                f"CastSession failed while stopping: {names}",
                [failure for _name, failure in failures],
            )
