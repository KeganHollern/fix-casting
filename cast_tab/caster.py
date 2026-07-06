"""Connect to a Chromecast and play the mirrored tab stream."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import pychromecast
from pychromecast import Chromecast

from cast_tab.devices import CastDevice


@dataclass(frozen=True)
class TvPlaybackSnapshot:
    state: str | None
    position_s: float | None
    idle_reason: str | None


# Consecutive idle polls before re-casting. One idle poll can be a blip
# (status race during a playlist reload); two in a row means the receiver
# really stopped playing our stream.
IDLE_POLLS_BEFORE_RECAST = 2


class TabCaster:
    """Load an HLS mirror stream on the default media receiver."""

    def __init__(self, device: CastDevice) -> None:
        self.device = device
        self._chromecast: Chromecast | None = None
        self._playlist_url: str | None = None
        self._idle_polls = 0
        self.reconnects = 0
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None

    def connect(self) -> None:
        print(f"Connecting to {self.device.name}...")
        self._chromecast = pychromecast.get_chromecast_from_host(
            (
                self.device.host,
                self.device.port,
                self.device.cast_info.uuid,
                self.device.model,
                self.device.name,
            ),
            timeout=10,
        )
        self._chromecast.wait()

    def play_hls(self, playlist_url: str) -> None:
        if self._chromecast is None:
            raise RuntimeError("Not connected to a Chromecast device.")

        self._playlist_url = playlist_url
        mc = self._chromecast.media_controller
        print(f"Casting tab mirror stream: {playlist_url}")
        mc.play_media(
            playlist_url,
            "application/vnd.apple.mpegurl",
            stream_type="LIVE",
            title="Cast Tab",
            autoplay=True,
        )
        mc.block_until_active(timeout=30)
        self._verify_playback()

    def _verify_playback(self) -> None:
        if self._chromecast is None:
            return

        mc = self._chromecast.media_controller
        for _ in range(20):
            mc.update_status()
            status = mc.status
            if status and status.player_state == "PLAYING":
                print("Chromecast is playing.")
                return
            if status and status.idle_reason == "ERROR":
                raise RuntimeError(
                    "Chromecast rejected the stream. The TV may show the idle backdrop."
                )
            time.sleep(1)

        state = status.player_state if status else "UNKNOWN"
        idle = status.idle_reason if status else None
        raise RuntimeError(f"Chromecast did not start playback (state={state}, idle={idle}).")

    def ensure_playing(self) -> str | None:
        """Re-cast the stream if the TV stopped playing it (app killed on the
        TV, stream error, receiver idle). Call periodically after play_hls;
        acts after IDLE_POLLS_BEFORE_RECAST consecutive idle polls. Returns a
        human-readable event line when it acted (or tried to), else None.

        Transport drops are NOT handled here: pychromecast's socket client
        reconnects itself, and update_status just fails until it has.
        """
        # Local snapshot: stop() nulls self._chromecast from another thread;
        # a local keeps the object alive so we never deref None mid-call.
        chromecast = self._chromecast
        if chromecast is None or self._playlist_url is None:
            return None
        try:
            mc = chromecast.media_controller
            mc.update_status()
            status = mc.status
        except Exception:
            return None  # transient; the socket client is reconnecting
        state = status.player_state if status else None
        if state in ("PLAYING", "BUFFERING", "PAUSED"):
            self._idle_polls = 0
            return None
        self._idle_polls += 1
        if self._idle_polls < IDLE_POLLS_BEFORE_RECAST:
            return None
        self._idle_polls = 0
        label = state or "UNKNOWN"
        if status is not None and status.idle_reason:
            label += f" ({status.idle_reason})"
        try:
            self.play_hls(self._playlist_url)
        except Exception as exc:
            return f"TV went {label}; re-cast failed: {exc}"
        self.reconnects += 1
        return f"TV went {label}; re-cast the stream"

    def toggle_pause(self) -> str | None:
        """Pause/resume playback on the TV. Returns "paused"/"resumed" or None.

        Note: the stream is live HLS with a rolling window, so a pause longer
        than the buffer (~48s buffered, ~4s low-latency) will stall on resume —
        the watchdog then recovers with a re-cast (a jump to live).
        """
        chromecast = self._chromecast
        if chromecast is None:
            return None
        try:
            mc = chromecast.media_controller
            mc.update_status()
            state = mc.status.player_state if mc.status else None
            if state == "PAUSED":
                mc.play()
                return "resumed"
            if state == "PLAYING":
                mc.pause()
                return "paused"
        except Exception:
            return None
        return None

    def volume_step(self, delta: float) -> float | None:
        """Nudge the TV volume by delta (-1..1). Returns the new level."""
        chromecast = self._chromecast
        if chromecast is None:
            return None
        try:
            if delta >= 0:
                return chromecast.volume_up(delta)
            return chromecast.volume_down(-delta)
        except Exception:
            return None

    def toggle_mute(self) -> bool | None:
        """Flip TV mute. Returns the new muted state, or None if unavailable."""
        chromecast = self._chromecast
        if chromecast is None:
            return None
        try:
            status = chromecast.status
            muted = bool(status.volume_muted) if status else False
            chromecast.set_volume_muted(not muted)
            return not muted
        except Exception:
            return None

    def start_watchdog(
        self,
        *,
        grace_s: float = 15.0,
        interval_s: float = 5.0,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        """Run ensure_playing() on a background thread until stop().

        One watchdog serves both the CLI and the TUI: recovery (which can
        block ~50s re-casting to a dead TV) never runs on a caller's loop.
        The grace period keeps startup buffering from counting as idle.
        """

        def run() -> None:
            if self._watchdog_stop.wait(grace_s):
                return
            while not self._watchdog_stop.is_set():
                event = self.ensure_playing()
                if event and on_event is not None:
                    on_event(event)
                if self._watchdog_stop.wait(interval_s):
                    return

        self._watchdog_thread = threading.Thread(
            target=run, name="tv-watchdog", daemon=True
        )
        self._watchdog_thread.start()

    def poll_playback_stats(self) -> TvPlaybackSnapshot:
        chromecast = self._chromecast
        if chromecast is None:
            return TvPlaybackSnapshot(None, None, None)
        try:
            chromecast.media_controller.update_status()
            status = chromecast.media_controller.status
        except Exception:
            return TvPlaybackSnapshot(None, None, None)
        if status is None:
            return TvPlaybackSnapshot(None, None, None)
        position = status.current_time
        return TvPlaybackSnapshot(
            status.player_state,
            float(position) if position is not None else None,
            status.idle_reason,
        )

    def stop(self) -> None:
        self._watchdog_stop.set()
        chromecast = self._chromecast
        self._chromecast = None
        if chromecast is None:
            return
        try:
            chromecast.media_controller.stop()
        except Exception:
            pass
        try:
            chromecast.disconnect()
        except Exception:
            pass