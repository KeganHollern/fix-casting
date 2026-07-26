"""Connect to a Chromecast and play the mirrored tab stream."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import pychromecast
from pychromecast import IDLE_APP_ID, Chromecast
from pychromecast.const import CAST_TYPE_CHROMECAST, MESSAGE_TYPE
from pychromecast.controllers.media import TYPE_GET_STATUS, TYPE_MEDIA_STATUS
from pychromecast.error import PyChromecastError, RequestFailed, RequestTimeout
from pychromecast.models import CastInfo, HostServiceInfo

from cast_tab.devices import CastDevice


@dataclass(frozen=True)
class TvPlaybackSnapshot:
    state: str | None
    position_s: float | None
    idle_reason: str | None


@dataclass(frozen=True)
class _MediaStatusSnapshot:
    state: str | None
    position_s: float | None
    idle_reason: str | None
    content_id: str | None
    media_session_id: int | None


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
MEDIA_STATUS_TIMEOUT_S = 2.0
PLAYBACK_STALL_TIMEOUT_S = 30.0
CONNECT_TIMEOUT_S = 12.0
CONNECT_SOCKET_TIMEOUT_S = 5.0
CONNECT_RETRY_WAIT_S = 0.25
DISCONNECT_TIMEOUT_S = 2.0
RECEIVER_OPERATION_STOP_TIMEOUT_S = 1.0

DeliveryProbe = Callable[[], tuple[int, bool] | None]


class _CastingCancelled(Exception):
    """A receiver operation lost ownership because the caster stopped."""


class _ReceiverResponse:
    """A correlated PyChromecast response wait that shutdown can cancel."""

    def __init__(self, timeout_s: float, request: str) -> None:
        self._timeout_s = timeout_s
        self._request = request
        self._event = threading.Event()
        self.msg_sent = False
        self.response: dict | None = None

    def callback(self, msg_sent: bool, response: dict | None) -> None:
        self.msg_sent = msg_sent
        self.response = response
        self._event.set()

    def wait(self, cancelled: Callable[[], bool]) -> None:
        deadline = time.monotonic() + self._timeout_s
        while not self._event.is_set():
            if cancelled():
                raise _CastingCancelled("Chromecast session stopped.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RequestTimeout(self._request, self._timeout_s)
            self._event.wait(min(0.05, remaining))
        if not self.msg_sent:
            raise RequestFailed(self._request)


class TabCaster:
    """Load an HLS mirror stream on the default media receiver."""

    def __init__(
        self,
        device: CastDevice,
        *,
        buffering_timeout_s: float = BUFFERING_TIMEOUT_S,
        playback_stall_timeout_s: float = PLAYBACK_STALL_TIMEOUT_S,
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
        disconnect_timeout_s: float = DISCONNECT_TIMEOUT_S,
        receiver_operation_stop_timeout_s: float = RECEIVER_OPERATION_STOP_TIMEOUT_S,
    ) -> None:
        self.device = device
        self._chromecast: Chromecast | None = None
        self._playlist_url: str | None = None
        self._idle_polls = 0
        self._buffering_started_at: float | None = None
        self._buffering_timeout_s = buffering_timeout_s
        self._playback_stall_timeout_s = playback_stall_timeout_s
        self._connect_timeout_s = connect_timeout_s
        self._disconnect_timeout_s = disconnect_timeout_s
        self._receiver_operation_stop_timeout_s = receiver_operation_stop_timeout_s
        self._monotonic = time.monotonic
        self.reconnects = 0
        self._watchdog_stop = threading.Event()
        self._receiver_wait = self._watchdog_stop.wait
        self._external_cancellation: Callable[[], bool] = lambda: False
        self._watchdog_thread: threading.Thread | None = None
        self._lifecycle_lock = threading.RLock()
        self._stop_condition = threading.Condition(self._lifecycle_lock)
        self._stop_in_progress = False
        self._stop_owner: int | None = None
        self._cleanup_complete: set[str] = set()
        # PyChromecast controllers are asynchronous and may launch their
        # supporting app as a side effect of a status request.  Keep every
        # receiver operation in one lane so a dashboard poll cannot race a
        # quit/load recovery sequence. UI observations acquire this lock
        # non-blocking and report "unknown" while recovery owns the receiver.
        self._receiver_operation_lock = threading.RLock()
        self._delivery_probe: DeliveryProbe | None = None
        self._delivery_baseline = 0
        self._last_media_session_id: int | None = None
        self._last_playback_position: float | None = None
        self._playback_observed_at: float | None = None
        self._last_playback_progress_at: float | None = None
        self._stopped = False
        self._connection_state = "new"

    def set_cancellation_probe(self, cancelled: Callable[[], bool]) -> None:
        """Install a lock-free signal cancellation reader before connect()."""
        with self._lifecycle_lock:
            if self._connection_state != "new":
                raise RuntimeError("Caster cancellation must be configured before connect().")
            self._external_cancellation = cancelled

    def _cancelled(self) -> bool:
        return self._watchdog_stop.is_set() or self._external_cancellation()

    def _wait_until_cancelled(self, timeout: float) -> bool:
        """Wait in short slices so an external signal flag cancels promptly."""
        deadline = self._monotonic() + timeout
        while True:
            if self._cancelled():
                return True
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return self._cancelled()
            if self._receiver_wait(min(0.05, remaining)):
                return True

    def set_delivery_probe(self, probe: DeliveryProbe) -> None:
        """Install selected-receiver HLS delivery telemetry before playback."""
        with self._receiver_operation_lock:
            if self._playlist_url is not None:
                raise RuntimeError("Delivery telemetry must be configured before playback.")
            self._delivery_probe = probe

    def connect(self) -> None:
        print(f"Connecting to {self.device.name}...")
        deadline = self._monotonic() + self._connect_timeout_s
        with self._lifecycle_lock:
            if self._connection_state != "new" or self._external_cancellation():
                raise RuntimeError(
                    "Chromecast connect() is one-shot and unavailable after "
                    "cancellation or a previous connect attempt."
                )
            self._connection_state = "connecting"

        chromecast: Chromecast | None = None
        try:
            discovered = self.device.cast_info
            cast_info = CastInfo(
                services={HostServiceInfo(self.device.host, self.device.port)},
                uuid=discovered.uuid,
                model_name=self.device.model,
                friendly_name=self.device.name,
                host=self.device.host,
                port=self.device.port,
                cast_type=getattr(discovered, "cast_type", None),
                manufacturer=getattr(discovered, "manufacturer", None),
            )
            if cast_info.cast_type is None:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timed out determining Chromecast device type.")
                cast_info = pychromecast.get_cast_type(cast_info, timeout=remaining)
            if cast_info.cast_type != CAST_TYPE_CHROMECAST:
                resolved_type = cast_info.cast_type or "unknown"
                raise RuntimeError(
                    f"{self.device.name} is a {resolved_type} Cast target, not a "
                    "video-capable Chromecast."
                )
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out preparing Chromecast connection.")
            with self._lifecycle_lock:
                if (
                    self._stopped
                    or self._connection_state != "connecting"
                    or self._external_cancellation()
                ):
                    raise RuntimeError("Chromecast session stopped while connecting.")
                # Start while publication and ownership are atomic. Waiting on
                # status_event below (rather than Chromecast.wait()) is
                # essential: wait() auto-starts a dead SocketClient, which can
                # resurrect it after stop() disconnected it in this gap.
                # SocketClient uses this same retry budget for later transport
                # reconnects. Keep it unbounded internally so one transient
                # refusal never kills the worker; our independently enforced
                # deadline below still bounds initial readiness and disconnects
                # the worker on timeout/cancellation.
                chromecast = pychromecast.Chromecast(
                    cast_info,
                    tries=None,
                    timeout=min(CONNECT_SOCKET_TIMEOUT_S, remaining),
                    retry_wait=CONNECT_RETRY_WAIT_S,
                )
                self._chromecast = chromecast
                chromecast.start()

            while True:
                with self._lifecycle_lock:
                    if (
                        self._stopped
                        or self._chromecast is not chromecast
                        or self._external_cancellation()
                    ):
                        raise RuntimeError("Chromecast session stopped while connecting.")
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise RequestTimeout("wait", self._connect_timeout_s)
                if chromecast.status_event.wait(timeout=min(0.05, remaining)):
                    break

            with self._lifecycle_lock:
                if self._cancelled() or self._chromecast is not chromecast:
                    raise RuntimeError("Chromecast session stopped while connecting.")
                self._connection_state = "connected"
        except BaseException as failure:
            cleanup_owned = False
            with self._lifecycle_lock:
                if chromecast is not None and self._chromecast is chromecast:
                    cleanup_owned = True
                if not self._stopped:
                    self._connection_state = "failed"
            if cleanup_owned and chromecast is not None:
                try:
                    self._disconnect_chromecast(chromecast)
                except BaseException as cleanup_failure:
                    # Retain self._chromecast so stop() can make its bounded
                    # retry. Preserve the connection error as the primary
                    # failure while making incomplete cleanup visible.
                    failure.add_note(
                        "Initial Chromecast cleanup was incomplete: "
                        f"{cleanup_failure}"
                    )
                else:
                    with self._lifecycle_lock:
                        if self._chromecast is chromecast:
                            self._chromecast = None
                            self._cleanup_complete.add("transport")
            raise

    def _disconnect_chromecast(self, chromecast: Chromecast) -> None:
        """Disconnect and require the underlying SocketClient to terminate."""
        chromecast.disconnect(timeout=self._disconnect_timeout_s)
        socket_client = getattr(chromecast, "socket_client", None)
        is_alive = getattr(socket_client, "is_alive", None)
        if callable(is_alive) and is_alive():
            raise TimeoutError(
                "Chromecast socket worker did not stop before the disconnect deadline"
            )

    def _is_current_receiver(self, chromecast: Chromecast) -> bool:
        with self._lifecycle_lock:
            return not self._cancelled() and self._chromecast is chromecast

    def _require_current_receiver(self, chromecast: Chromecast) -> None:
        if not self._is_current_receiver(chromecast):
            raise _CastingCancelled("Chromecast session stopped.")

    def play_hls(self, playlist_url: str, *, announce: bool = True) -> None:
        with self._receiver_operation_lock:
            self._play_hls(playlist_url, announce=announce)

    def _play_hls(self, playlist_url: str, *, announce: bool) -> None:
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
        self._prepare_load_verification()
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
            self._prepare_load_verification()
            self._load_media(chromecast, playlist_url, announce=announce)
            self._verify_playback(chromecast, playlist_url, announce=announce)

    def _prepare_load_verification(self) -> None:
        observation = self._delivery_observation()
        self._delivery_baseline = observation[0] if observation is not None else 0
        self._last_media_session_id = None
        self._last_playback_position = None
        self._playback_observed_at = None
        self._last_playback_progress_at = None

    def _delivery_observation(self) -> tuple[int, bool] | None:
        probe = self._delivery_probe
        if probe is None:
            return None
        try:
            return probe()
        except Exception:
            return None

    def _new_delivery_is_ready(self) -> bool:
        if self._delivery_probe is None:
            return True
        observation = self._delivery_observation()
        return bool(
            observation is not None
            and observation[0] > self._delivery_baseline
            and observation[1]
        )

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
        response = _ReceiverResponse(MEDIA_LOAD_TIMEOUT_S, "load cast stream")
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
        response.wait(self._cancelled)
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
            if self._wait_until_cancelled(min(RECEIVER_RESET_POLL_S, remaining)):
                raise _CastingCancelled("Chromecast session stopped.")
            self._require_current_receiver(chromecast)
        # The receiver needs a beat between quitting and relaunching; loading
        # immediately after the app disappears can lose the LOAD again.
        if self._wait_until_cancelled(RECEIVER_RELAUNCH_DELAY_S):
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
        deadline = self._monotonic() + PLAYBACK_VERIFY_TIMEOUT_S
        status = None
        while self._monotonic() < deadline:
            self._require_current_receiver(chromecast)
            try:
                status = self._fresh_media_status(chromecast)
            except _CastingCancelled:
                raise
            except Exception:
                status = None
            if (
                status
                and status.state == "PLAYING"
                and status.content_id == playlist_url
                and status.media_session_id is not None
                and self._new_delivery_is_ready()
            ):
                self._record_playback_progress(status)
                if announce:
                    print("Chromecast is playing.")
                return
            if status and status.idle_reason == "ERROR":
                raise RuntimeError(
                    "Chromecast rejected the stream. The TV may show the idle backdrop."
                )
            remaining = deadline - self._monotonic()
            if remaining > 0 and self._wait_until_cancelled(min(1.0, remaining)):
                raise _CastingCancelled("Chromecast session stopped.")

        state = status.state if status else "UNKNOWN"
        idle = status.idle_reason if status else None
        raise RuntimeError(f"Chromecast did not start playback (state={state}, idle={idle}).")

    def _fresh_media_status(self, chromecast: Chromecast) -> _MediaStatusSnapshot | None:
        """Return a correlated media status without ever launching an app.

        ``MediaController.update_status()`` calls ``send_message()``, whose
        documented behavior is to launch the controller's supporting app when
        the media namespace is absent.  Observation must not mutate receiver
        lifecycle, so use the no-check send path after confirming the namespace
        is active.  A namespace race becomes an ordinary failed observation.
        """
        self._require_current_receiver(chromecast)
        mc = chromecast.media_controller
        if not mc.is_active:
            return None
        response = _ReceiverResponse(MEDIA_STATUS_TIMEOUT_S, "read media status")
        mc.send_message_nocheck(
            {MESSAGE_TYPE: TYPE_GET_STATUS},
            callback_function=response.callback,
        )
        response.wait(self._cancelled)
        self._require_current_receiver(chromecast)
        payload = response.response or {}
        if payload.get(MESSAGE_TYPE) != TYPE_MEDIA_STATUS:
            return None
        statuses = payload.get("status")
        if not isinstance(statuses, list) or not statuses or not isinstance(statuses[0], dict):
            return None
        raw = statuses[0]
        media = raw.get("media")
        content_id = media.get("contentId") if isinstance(media, dict) else None
        position = raw.get("currentTime")
        session_id = raw.get("mediaSessionId")
        return _MediaStatusSnapshot(
            state=raw.get("playerState") if isinstance(raw.get("playerState"), str) else None,
            position_s=(
                float(position)
                if isinstance(position, (int, float)) and not isinstance(position, bool)
                else None
            ),
            idle_reason=(
                raw.get("idleReason") if isinstance(raw.get("idleReason"), str) else None
            ),
            content_id=content_id if isinstance(content_id, str) else None,
            media_session_id=(
                session_id
                if isinstance(session_id, int) and not isinstance(session_id, bool)
                else None
            ),
        )

    def _owns_status(self, status: _MediaStatusSnapshot | None) -> bool:
        return bool(
            status is not None
            and status.content_id == self._playlist_url
            and status.media_session_id is not None
        )

    def _record_playback_progress(self, status: _MediaStatusSnapshot) -> bool:
        """Return True while target media has recent position or HLS progress."""
        now = self._monotonic()
        if status.media_session_id != self._last_media_session_id:
            self._last_media_session_id = status.media_session_id
            self._last_playback_position = status.position_s
            self._playback_observed_at = now
            self._last_playback_progress_at = None
        else:
            previous = self._last_playback_position
            position = status.position_s
            if previous is not None and position is not None:
                if position > previous + 0.1:
                    self._last_playback_progress_at = now
                elif position + 1.0 < previous:
                    # A receiver-side reload/rewind starts a fresh grace window.
                    self._playback_observed_at = now
                    self._last_playback_progress_at = None
            self._last_playback_position = position

        observation = self._delivery_observation()
        if observation is not None and observation[1]:
            return True
        if (
            self._last_playback_progress_at is not None
            and now - self._last_playback_progress_at < self._playback_stall_timeout_s
        ):
            return True
        return bool(
            self._playback_observed_at is not None
            and now - self._playback_observed_at < self._playback_stall_timeout_s
        )

    def ensure_playing(self, *, announce_recovery: bool = True) -> str | None:
        """Re-cast the stream if the TV stopped playing it (app killed on the
        TV, stream error, receiver idle). Call periodically after play_hls;
        acts after IDLE_POLLS_BEFORE_RECAST consecutive idle polls. Returns a
        human-readable event line when it acted (or tried to), else None.

        Transport drops are NOT handled here: pychromecast's socket client
        reconnects itself, and update_status just fails until it has.
        """
        with self._receiver_operation_lock:
            return self._ensure_playing(announce_recovery=announce_recovery)

    def _ensure_playing(self, *, announce_recovery: bool) -> str | None:
        if self._cancelled():
            return None
        # Local snapshot: stop() nulls self._chromecast from another thread;
        # a local keeps the object alive so we never deref None mid-call.
        chromecast = self._chromecast
        if chromecast is None or self._playlist_url is None:
            return None
        try:
            status = self._fresh_media_status(chromecast)
        except Exception:
            return None  # transient; the socket client is reconnecting
        if self._cancelled():
            return None
        state = status.state if status else None
        owned = self._owns_status(status)
        label_override: str | None = None
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
            if state == "PAUSED" and owned:
                assert status is not None
                self._record_playback_progress(status)
                self._idle_polls = 0
                return None
            if state == "PLAYING" and owned:
                assert status is not None
                if self._record_playback_progress(status):
                    self._idle_polls = 0
                    return None
                label_override = "PLAYING (stalled)"
            elif state in ("PLAYING", "PAUSED"):
                label_override = f"{state} (wrong media)"

        if state != "BUFFERING":
            self._idle_polls += 1
            if self._idle_polls < IDLE_POLLS_BEFORE_RECAST:
                return None
            self._idle_polls = 0
        label = label_override or state or "UNKNOWN"
        if status is not None and status.idle_reason:
            label += f" ({status.idle_reason})"
        if self._cancelled():
            return None
        try:
            self.play_hls(self._playlist_url, announce=announce_recovery)
        except Exception as exc:
            if self._cancelled():
                return None
            return f"TV went {label}; re-cast failed: {exc}"
        if self._cancelled():
            return None
        self.reconnects += 1
        return f"TV went {label}; re-cast the stream"

    def toggle_pause(self) -> str | None:
        """Pause/resume playback on the TV. Returns "paused"/"resumed" or None.

        Note: the stream is live HLS with a rolling window, so a pause longer
        than playlist retention (~12s production, ~4s low-latency) can stall
        on resume — the watchdog then recovers with a re-cast (a jump to live).
        """
        if not self._receiver_operation_lock.acquire(blocking=False):
            return None
        try:
            chromecast = self._chromecast
            if chromecast is None:
                return None
            try:
                mc = chromecast.media_controller
                status = self._fresh_media_status(chromecast)
                state = status.state if status else None
                if state == "PAUSED":
                    mc.play()
                    return "resumed"
                if state == "PLAYING":
                    mc.pause()
                    return "paused"
            except Exception:
                return None
        finally:
            self._receiver_operation_lock.release()
        return None

    def volume_step(self, delta: float) -> float | None:
        """Nudge the TV volume by delta (-1..1). Returns the new level."""
        if not self._receiver_operation_lock.acquire(blocking=False):
            return None
        try:
            chromecast = self._chromecast
            if chromecast is None:
                return None
            try:
                if delta >= 0:
                    return chromecast.volume_up(delta)
                return chromecast.volume_down(-delta)
            except Exception:
                return None
        finally:
            self._receiver_operation_lock.release()

    def toggle_mute(self) -> bool | None:
        """Flip TV mute. Returns the new muted state, or None if unavailable."""
        if not self._receiver_operation_lock.acquire(blocking=False):
            return None
        try:
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
        finally:
            self._receiver_operation_lock.release()

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
            if self._wait_until_cancelled(grace_s):
                return
            while not self._cancelled():
                event = self.ensure_playing(announce_recovery=announce_recovery)
                if self._cancelled():
                    return
                if event and on_event is not None:
                    on_event(event)
                if self._wait_until_cancelled(interval_s):
                    return

        watchdog = threading.Thread(target=run, name="tv-watchdog", daemon=True)
        with self._lifecycle_lock:
            if self._cancelled():
                raise RuntimeError("Cannot start TV watchdog after caster stop.")
            if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
                raise RuntimeError("TV watchdog is already running.")
            self._watchdog_stop.clear()
            self._watchdog_thread = watchdog
            watchdog.start()

    def poll_playback_stats(self) -> TvPlaybackSnapshot:
        if not self._receiver_operation_lock.acquire(blocking=False):
            return TvPlaybackSnapshot(None, None, None)
        try:
            chromecast = self._chromecast
            if chromecast is None:
                return TvPlaybackSnapshot(None, None, None)
            try:
                status = self._fresh_media_status(chromecast)
            except Exception:
                return TvPlaybackSnapshot(None, None, None)
            if status is None:
                return TvPlaybackSnapshot(None, None, None)
            return TvPlaybackSnapshot(
                status.state,
                status.position_s,
                status.idle_reason,
            )
        finally:
            self._receiver_operation_lock.release()

    def stop(self) -> None:
        caller = threading.get_ident()
        with self._stop_condition:
            self._stopped = True
            self._connection_state = "stopped"
            self._watchdog_stop.set()
            required = {"watchdog", "receiver", "transport"}
            if required.issubset(self._cleanup_complete):
                return
            if self._stop_in_progress and self._stop_owner == caller:
                return
            while self._stop_in_progress:
                self._stop_condition.wait()
                if required.issubset(self._cleanup_complete):
                    return
            self._stop_in_progress = True
            self._stop_owner = caller

        failures: list[tuple[str, BaseException]] = []
        try:
            if "watchdog" not in self._cleanup_complete:
                watchdog = self._watchdog_thread
                try:
                    if watchdog is not None and watchdog is not threading.current_thread():
                        watchdog.join(timeout=WATCHDOG_JOIN_TIMEOUT_S)
                        if watchdog.is_alive():
                            raise TimeoutError(
                                "Chromecast watchdog did not stop before its deadline"
                            )
                except BaseException as exc:
                    failures.append(("watchdog", exc))
                else:
                    self._cleanup_complete.add("watchdog")

            chromecast = self._chromecast
            if "receiver" not in self._cleanup_complete:
                try:
                    if chromecast is not None:
                        operation_acquired = self._receiver_operation_lock.acquire(
                            timeout=self._receiver_operation_stop_timeout_s
                        )
                    else:
                        operation_acquired = False
                    if operation_acquired:
                        assert chromecast is not None
                        try:
                            try:
                                chromecast.media_controller.stop(
                                    timeout=RECEIVER_CLEANUP_TIMEOUT_S
                                )
                            except Exception:
                                pass
                            # Leave the TV home instead of parked in a dead receiver.
                            try:
                                chromecast.quit_app(timeout=RECEIVER_CLEANUP_TIMEOUT_S)
                            except Exception:
                                pass
                        finally:
                            self._receiver_operation_lock.release()
                except BaseException as exc:
                    failures.append(("receiver", exc))
                else:
                    # Receiver UI cleanup is best-effort. Whether the operation
                    # lock was available or the TV accepted the commands must
                    # not prevent the transport itself from being terminated.
                    self._cleanup_complete.add("receiver")

            if "transport" not in self._cleanup_complete:
                try:
                    if chromecast is not None:
                        self._disconnect_chromecast(chromecast)
                except BaseException as exc:
                    failures.append(("transport", exc))
                else:
                    self._cleanup_complete.add("transport")
                    with self._lifecycle_lock:
                        if self._chromecast is chromecast:
                            self._chromecast = None
        finally:
            with self._stop_condition:
                self._stop_in_progress = False
                self._stop_owner = None
                self._stop_condition.notify_all()

        if len(failures) == 1:
            name, failure = failures[0]
            failure.add_note(f"TabCaster failed while stopping {name}.")
            raise failure
        if failures:
            names = ", ".join(name for name, _failure in failures)
            raise BaseExceptionGroup(
                f"TabCaster failed while stopping: {names}",
                [failure for _name, failure in failures],
            )
