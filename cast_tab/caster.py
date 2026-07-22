"""Connect to a Chromecast and play the mirrored tab stream."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import pychromecast
from pychromecast import IDLE_APP_ID, Chromecast
from pychromecast.error import PyChromecastError
from pychromecast.response_handler import WaitResponse

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

# Buffering is a normal, short-lived receiver state during playlist reloads.
# Remaining there this long means playback is wedged and should be restarted.
BUFFERING_TIMEOUT_S = 30.0

# Receiver operations can block inside pychromecast. Give a normal watchdog
# poll time to finish, but never let a wedged receiver hang shutdown forever.
WATCHDOG_JOIN_TIMEOUT_S = 2.0

# How long to require the receiver to return to the home screen after quit_app.
# Sending LOAD into an app that ignored the reset recreates the swallowed-LOAD
# failure, so a reset that cannot be confirmed is an error, not best-effort.
RECEIVER_RESET_TIMEOUT_S = 8.0
RECEIVER_RESET_POLL_S = 0.25
RECEIVER_RELAUNCH_DELAY_S = 0.5
MEDIA_LOAD_TIMEOUT_S = 30.0
PLAYBACK_VERIFY_TIMEOUT_S = 20.0
RECEIVER_CLEANUP_TIMEOUT_S = 1.0


class _CastingCancelled(Exception):
    """A receiver operation lost ownership because the caster stopped."""


class TabCaster:
    """Load an HLS mirror stream on the default media receiver."""

    def __init__(
        self,
        device: CastDevice,
        *,
        buffering_timeout_s: float = BUFFERING_TIMEOUT_S,
    ) -> None:
        self.device = device
        self._chromecast: Chromecast | None = None
        self._playlist_url: str | None = None
        self._idle_polls = 0
        self._buffering_started_at: float | None = None
        self._buffering_timeout_s = buffering_timeout_s
        self._monotonic = time.monotonic
        self.reconnects = 0
        self._watchdog_stop = threading.Event()
        self._receiver_wait = self._watchdog_stop.wait
        self._watchdog_thread: threading.Thread | None = None
        self._lifecycle_lock = threading.RLock()
        self._stopped = False

    def connect(self) -> None:
        print(f"Connecting to {self.device.name}...")
        with self._lifecycle_lock:
            if self._stopped:
                raise RuntimeError("Chromecast session has already stopped.")
        chromecast = pychromecast.get_chromecast_from_host(
            (
                self.device.host,
                self.device.port,
                self.device.cast_info.uuid,
                self.device.model,
                self.device.name,
            ),
            timeout=10,
        )
        with self._lifecycle_lock:
            if self._stopped:
                should_disconnect = True
            else:
                self._chromecast = chromecast
                should_disconnect = False
        if should_disconnect:
            chromecast.disconnect()
            raise RuntimeError("Chromecast session stopped while connecting.")
        try:
            chromecast.wait()
        except BaseException:
            with self._lifecycle_lock:
                if self._chromecast is chromecast:
                    self._chromecast = None
            try:
                chromecast.disconnect()
            except Exception:
                pass
            raise
        with self._lifecycle_lock:
            if self._stopped or self._chromecast is not chromecast:
                raise RuntimeError("Chromecast session stopped while connecting.")

    def _is_current_receiver(self, chromecast: Chromecast) -> bool:
        with self._lifecycle_lock:
            return not self._stopped and self._chromecast is chromecast

    def _require_current_receiver(self, chromecast: Chromecast) -> None:
        if not self._is_current_receiver(chromecast):
            raise _CastingCancelled("Chromecast session stopped.")

    def play_hls(self, playlist_url: str, *, announce: bool = True) -> None:
        # Local snapshot: stop() nulls self._chromecast from another thread;
        # a local keeps the object alive so we never deref None mid-call.
        with self._lifecycle_lock:
            chromecast = self._chromecast
            if self._stopped or chromecast is None:
                raise RuntimeError("Not connected to a Chromecast device.")
            self._playlist_url = playlist_url
        # A receiver app left over from an earlier cast (crashed sender, dead
        # media session) can silently swallow the LOAD, leaving no new media
        # session and never fetching the playlist. Seen on a Sony
        # BRAVIA; the state survives standby and clean disconnects, and only
        # quitting the app clears it. Always hand the LOAD a fresh receiver.
        if self._receiver_app_running(chromecast):
            self._reset_receiver(
                chromecast, "a receiver app is already running", announce=announce
            )
        try:
            self._load_media(chromecast, playlist_url, announce=announce)
            self._verify_playback(chromecast, playlist_url, announce=announce)
        except _CastingCancelled:
            raise
        except (RuntimeError, PyChromecastError):
            # The wedge can also pre-exist without a visible resident app.
            # One full receiver reset + reload recovers it; a second failure
            # is a real error and propagates.
            self._require_current_receiver(chromecast)
            self._reset_receiver(
                chromecast, "the Chromecast did not start playback", announce=announce
            )
            self._load_media(chromecast, playlist_url, announce=announce)
            self._verify_playback(chromecast, playlist_url, announce=announce)

    def _load_media(
        self,
        chromecast: Chromecast,
        playlist_url: str,
        *,
        announce: bool,
    ) -> None:
        self._require_current_receiver(chromecast)
        mc = chromecast.media_controller
        if announce:
            print(f"Casting tab mirror stream: {playlist_url}")
        response = WaitResponse(MEDIA_LOAD_TIMEOUT_S, "load cast stream")
        # The lifecycle lock covers only the command send, not the response
        # wait. This gives LOAD and stop() a total order without making an
        # unresponsive TV block shutdown for the full media timeout.
        with self._lifecycle_lock:
            if self._stopped or self._chromecast is not chromecast:
                raise _CastingCancelled("Chromecast session stopped.")
            mc.play_media(
                playlist_url,
                "application/vnd.apple.mpegurl",
                stream_type="LIVE",
                title="Cast Tab",
                autoplay=True,
                callback_function=response.callback,
            )
        response.wait_response()
        self._require_current_receiver(chromecast)
        response_type = (response.response or {}).get("type")
        # WaitResponse only proves that a correlated reply arrived; it treats
        # INVALID_REQUEST and even an empty response as a successful send.
        # MEDIA_STATUS is the sole successful LOAD reply from this receiver.
        if response_type != "MEDIA_STATUS":
            reply = response_type or "EMPTY_RESPONSE"
            raise RuntimeError(f"Chromecast rejected the media LOAD request ({reply}).")

    @staticmethod
    def _receiver_app_running(chromecast: Chromecast) -> bool:
        app_id = chromecast.app_id
        return app_id is not None and app_id != IDLE_APP_ID

    def _reset_receiver(
        self, chromecast: Chromecast, reason: str, *, announce: bool
    ) -> None:
        """Quit the resident receiver app and require a clean home screen."""
        self._require_current_receiver(chromecast)
        if announce:
            print(f"Resetting TV receiver app ({reason})...")
        try:
            chromecast.quit_app(timeout=RECEIVER_RESET_TIMEOUT_S)
        except Exception as exc:
            self._require_current_receiver(chromecast)
            raise RuntimeError("Chromecast receiver app could not be reset.") from exc
        self._require_current_receiver(chromecast)
        deadline = self._monotonic() + RECEIVER_RESET_TIMEOUT_S
        while self._receiver_app_running(chromecast):
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise RuntimeError("Chromecast receiver app did not exit.")
            if self._receiver_wait(min(RECEIVER_RESET_POLL_S, remaining)):
                raise _CastingCancelled("Chromecast session stopped.")
            self._require_current_receiver(chromecast)
        # The receiver needs a beat between quitting and relaunching; loading
        # immediately after the app disappears can lose the LOAD again.
        if self._receiver_wait(RECEIVER_RELAUNCH_DELAY_S):
            raise _CastingCancelled("Chromecast session stopped.")
        self._require_current_receiver(chromecast)

    def _verify_playback(
        self,
        chromecast: Chromecast,
        playlist_url: str,
        *,
        announce: bool = True,
    ) -> None:
        """Require a live media session for this exact playlist URL."""
        mc = chromecast.media_controller
        deadline = self._monotonic() + PLAYBACK_VERIFY_TIMEOUT_S
        status = None
        while self._monotonic() < deadline:
            self._require_current_receiver(chromecast)
            mc.update_status()
            status = mc.status
            if (
                status
                and status.player_state == "PLAYING"
                and getattr(status, "content_id", None) == playlist_url
                and getattr(status, "media_session_id", None) is not None
            ):
                if announce:
                    print("Chromecast is playing.")
                return
            if status and status.idle_reason == "ERROR":
                raise RuntimeError(
                    "Chromecast rejected the stream. The TV may show the idle backdrop."
                )
            remaining = deadline - self._monotonic()
            if remaining > 0 and self._receiver_wait(min(1.0, remaining)):
                raise _CastingCancelled("Chromecast session stopped.")

        state = status.player_state if status else "UNKNOWN"
        idle = status.idle_reason if status else None
        raise RuntimeError(f"Chromecast did not start playback (state={state}, idle={idle}).")

    def ensure_playing(self, *, announce_recovery: bool = True) -> str | None:
        """Re-cast the stream if the TV stopped playing it (app killed on the
        TV, stream error, receiver idle). Call periodically after play_hls;
        acts after IDLE_POLLS_BEFORE_RECAST consecutive idle polls. Returns a
        human-readable event line when it acted (or tried to), else None.

        Transport drops are NOT handled here: pychromecast's socket client
        reconnects itself, and update_status just fails until it has.
        """
        if self._watchdog_stop.is_set():
            return None
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
        if self._watchdog_stop.is_set():
            return None
        state = status.player_state if status else None
        if state == "BUFFERING":
            self._idle_polls = 0
            now = self._monotonic()
            if self._buffering_started_at is None:
                self._buffering_started_at = now
            if now - self._buffering_started_at < self._buffering_timeout_s:
                return None
            # Rate-limit repeated failed recovery attempts by requiring another
            # full buffering interval before trying again.
            self._buffering_started_at = None
        else:
            self._buffering_started_at = None
            if state in ("PLAYING", "PAUSED"):
                self._idle_polls = 0
                return None

        if state != "BUFFERING":
            self._idle_polls += 1
            if self._idle_polls < IDLE_POLLS_BEFORE_RECAST:
                return None
            self._idle_polls = 0
        label = state or "UNKNOWN"
        if status is not None and status.idle_reason:
            label += f" ({status.idle_reason})"
        if self._watchdog_stop.is_set():
            return None
        try:
            self.play_hls(self._playlist_url, announce=announce_recovery)
        except Exception as exc:
            if self._watchdog_stop.is_set():
                return None
            return f"TV went {label}; re-cast failed: {exc}"
        if self._watchdog_stop.is_set():
            return None
        self.reconnects += 1
        return f"TV went {label}; re-cast the stream"

    def toggle_pause(self) -> str | None:
        """Pause/resume playback on the TV. Returns "paused"/"resumed" or None.

        Note: the stream is live HLS with a rolling window, so a pause longer
        than playlist retention (~12s production, ~4s low-latency) can stall
        on resume — the watchdog then recovers with a re-cast (a jump to live).
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
        announce_recovery: bool = True,
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
                event = self.ensure_playing(announce_recovery=announce_recovery)
                if self._watchdog_stop.is_set():
                    return
                if event and on_event is not None:
                    on_event(event)
                if self._watchdog_stop.wait(interval_s):
                    return

        watchdog = threading.Thread(target=run, name="tv-watchdog", daemon=True)
        with self._lifecycle_lock:
            if self._stopped:
                raise RuntimeError("Cannot start TV watchdog after caster stop.")
            if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
                raise RuntimeError("TV watchdog is already running.")
            self._watchdog_stop.clear()
            self._watchdog_thread = watchdog
            watchdog.start()

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
        with self._lifecycle_lock:
            if self._stopped:
                return
            self._stopped = True
            self._watchdog_stop.set()
            chromecast = self._chromecast
            self._chromecast = None
        if chromecast is not None:
            try:
                chromecast.media_controller.stop(timeout=RECEIVER_CLEANUP_TIMEOUT_S)
            except Exception:
                pass
            # Leave the TV on its home screen rather than parked in the media
            # receiver: a resident app with a dead session is exactly the
            # state that swallows the next cast's LOAD (see play_hls).
            try:
                chromecast.quit_app(timeout=RECEIVER_CLEANUP_TIMEOUT_S)
            except Exception:
                pass
            try:
                chromecast.disconnect()
            except Exception:
                pass

        watchdog = self._watchdog_thread
        if watchdog is not None and watchdog is not threading.current_thread():
            watchdog.join(timeout=WATCHDOG_JOIN_TIMEOUT_S)
