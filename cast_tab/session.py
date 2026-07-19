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
from dataclasses import dataclass
from pathlib import Path

from cast_tab.audio import (
    AudioCapture,
    AudioCaptureCancelled,
    AudioCaptureError,
    audiotee_available,
    install_hint,
    stop_audio_capture,
    try_start_chrome_audio_capture,
)
from cast_tab.browser import TabScreencaster
from cast_tab.encoder import DEFAULT_JPEG_QUALITY
from cast_tab.stats import PipelineStats
from cast_tab.streamer import HLSStreamer


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
        self._stopped = False
        self._cleanup_complete: set[str] = set()

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
        if self._shutdown_started.is_set():
            return
        try:
            screencaster = self.screencaster
            if screencaster is not None:
                screencaster.raise_if_failed()
            audio_capture = self.audio_capture
            if audio_capture is not None:
                audio_capture.raise_if_failed()
            streamer = self.streamer
            if streamer is not None:
                streamer.raise_if_failed()
        except Exception:
            # A health poll can race intentional component termination from a
            # different thread (notably the TUI poller).  Shutdown owns that
            # exit; do not turn it into a spurious runtime failure.
            if self._shutdown_started.is_set():
                return
            raise

    def start(self) -> None:
        caller = threading.get_ident()
        with self._stop_condition:
            if self._start_started or self._shutdown_started.is_set():
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
            cancelled=self._shutdown_started.is_set,
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
        if self._shutdown_started.is_set():
            raise AudioCaptureCancelled("Cast session startup was cancelled.")

    def _raise_input_if_failed(self) -> None:
        """Check capture workers while ffmpeg waits for its first segment."""
        self._raise_start_not_cancelled()
        screencaster = self.screencaster
        if screencaster is not None:
            screencaster.raise_if_failed()
        audio_capture = self.audio_capture
        if audio_capture is not None:
            audio_capture.raise_if_failed()

    def _attach_audio(self) -> None:
        """Tap the cast browser's audio; degrade to video-only unless required."""
        if not audiotee_available():
            if self.config.require_audio:
                raise AudioCaptureError(
                    f"AudioTee not found.\n{install_hint()}"
                )
            print(f"Audio unavailable: AudioTee not found.\n{install_hint()}")
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
                cancelled=self._shutdown_started.is_set,
            )
            if self._shutdown_started.is_set():
                raise AudioCaptureCancelled("Cast session startup was cancelled.")
            self.audio_capture = capture
            # Ownership transferred to the session. Until this assignment and
            # local clear both complete, the finally block owns/reaps the child
            # if a same-thread signal raises SystemExit between bytecodes.
            capture = None
            attached = True
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
        finally:
            # If assignment completed before an asynchronous signal, stop()
            # already owns (and may already have closed) this exact capture.
            # Never close the numeric fd twice: it could have been reused.
            if capture is not None and self.audio_capture is not capture:
                stop_audio_capture(capture)

    def stop(self) -> None:
        self._shutdown_started.set()
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
            stop_audio_capture(self.audio_capture)

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
