"""Connect to a Chromecast and play the mirrored tab stream."""

from __future__ import annotations

import time

import pychromecast
from pychromecast import Chromecast

from cast_tab.devices import CastDevice


class TabCaster:
    """Load an HLS mirror stream on the default media receiver."""

    def __init__(self, device: CastDevice) -> None:
        self.device = device
        self._chromecast: Chromecast | None = None

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

    def wait_until_stopped(self) -> None:
        if self._chromecast is None:
            return

        try:
            while True:
                self._chromecast.media_controller.update_status()
                status = self._chromecast.media_controller.status
                if status is None or status.player_state in ("IDLE", "UNKNOWN"):
                    time.sleep(1)
                    continue
                time.sleep(2)
        except KeyboardInterrupt:
            pass

    def poll_playback_stats(self) -> tuple[str | None, float | None]:
        if self._chromecast is None:
            return None, None
        try:
            self._chromecast.media_controller.update_status()
            status = self._chromecast.media_controller.status
        except Exception:
            return None, None
        if status is None:
            return None, None
        position = status.current_time
        return status.player_state, float(position) if position is not None else None

    def stop(self) -> None:
        if self._chromecast is None:
            return
        try:
            self._chromecast.media_controller.stop()
        except Exception:
            pass
        try:
            self._chromecast.disconnect()
        except Exception:
            pass
        self._chromecast = None