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
    AudioCaptureError,
    audiotee_available,
    install_hint,
    stop_audio_capture,
    try_start_chrome_audio_capture,
)
from cast_tab.browser import TabScreencaster
from cast_tab.stats import PipelineStats
from cast_tab.streamer import HLSStreamer


@dataclass
class SessionConfig:
    """Everything one cast pipeline needs, minus the Chromecast."""

    url: str
    width: int = 1920
    height: int = 1080
    fps: int = 30  # encode rate; capture itself is paint-driven
    jpeg_quality: int = 92
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
        self._stop_lock = threading.Lock()
        self._stopped = False

    @property
    def audio_active(self) -> bool:
        return self.audio_capture is not None

    @property
    def playlist_url(self) -> str:
        if self.streamer is None:
            raise RuntimeError("Session not started.")
        return self.streamer.playlist_url

    def start(self) -> None:
        cfg = self.config
        self.screencaster = TabScreencaster(
            cfg.url,
            width=cfg.width,
            height=cfg.height,
            fps=cfg.fps,
            jpeg_quality=cfg.jpeg_quality,
            on_frame=lambda _frame: None,
            headless=cfg.headless,
            capture_audio=cfg.capture_audio,
            stats=self.stats,
            adblock_patterns=cfg.adblock_patterns,
        )
        self.screencaster.start()
        self.screencaster.wait_until_ready()
        self.screencaster.enable_capture()
        if self.stats is not None:
            self.stats.trace("enable_capture")

        if cfg.capture_audio:
            self._attach_audio()

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
        self.streamer.start()
        self.streamer.wait_until_ready()

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
        try:
            assert self.screencaster is not None
            self.audio_capture = try_start_chrome_audio_capture(
                self.screencaster.user_data_dir,
                on_retry=self.screencaster.nudge_playback,
                on_stderr=on_stderr,
            )
            attached = True
            if self.stats is not None:
                self.stats.trace(f"audio attached (pids={self.audio_capture.pids})")
            print(
                "Capturing audio from cast browser only "
                f"(PIDs: {', '.join(str(pid) for pid in self.audio_capture.pids)})."
            )
            print("Other Mac apps keep their normal audio output.")
        except AudioCaptureError as exc:
            if self.config.require_audio:
                raise
            print(f"Audio unavailable: {exc}")
            print(install_hint())

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        if self.screencaster is not None:
            self.screencaster.stop()
        if self.streamer is not None:
            self.streamer.stop()
        stop_audio_capture(self.audio_capture)
