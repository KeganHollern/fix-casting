"""Launch a browser tab and capture rendered frames (full tab, not media elements)."""

from __future__ import annotations

import base64
import threading
import time
from typing import Callable

from playwright.sync_api import sync_playwright


class TabScreencaster:
    """Mirror a browser tab by capturing frames as fast as the page updates."""

    def __init__(
        self,
        url: str,
        *,
        width: int = 1920,
        height: int = 1080,
        fps: int = 24,
        jpeg_quality: int = 75,
        on_frame: Callable[[bytes], None],
        headless: bool = False,
        capture_audio: bool = False,
    ) -> None:
        self.url = url
        self.width = width
        self.height = height
        self.fps = fps
        self.jpeg_quality = jpeg_quality
        self.on_frame = on_frame
        self.headless = headless
        self.capture_audio = capture_audio

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_publish_at = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="tab-screencast", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

    def _publish(self, frame: bytes) -> None:
        self._last_publish_at = time.monotonic()
        self.on_frame(frame)

    def _run(self) -> None:
        with sync_playwright() as playwright:
            launch_args = [
                "--autoplay-policy=no-user-gesture-required",
                "--disable-features=MediaRouter",
                "--disable-cast-streaming-hw-encoding",
            ]

            try:
                browser = playwright.chromium.launch(
                    channel="chrome",
                    headless=self.headless,
                    args=launch_args,
                )
            except Exception:
                browser = playwright.chromium.launch(
                    headless=self.headless,
                    args=launch_args,
                )

            context = browser.new_context(
                viewport={"width": self.width, "height": self.height},
                device_scale_factor=1,
                ignore_https_errors=True,
            )
            context.grant_permissions(["notifications", "geolocation"])
            page = context.new_page()
            print(f"Loading {self.url} ...")
            page.goto(self.url, wait_until="load", timeout=120_000)
            self._try_start_playback(page)
            print("Page loaded, starting capture.")

            cdp = context.new_cdp_session(page)

            def on_screencast_frame(params: dict) -> None:
                if self._stop.is_set():
                    return
                self._publish(base64.b64decode(params["data"]))
                cdp.send(
                    "Page.screencastFrameAck",
                    {"sessionId": params["sessionId"]},
                )

            cdp.on("Page.screencastFrame", on_screencast_frame)
            cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": self.jpeg_quality,
                    "maxWidth": self.width,
                    "maxHeight": self.height,
                    "everyNthFrame": 1,
                },
            )

            min_frame_interval = 1.0 / (self.fps * 2)
            while not self._stop.is_set():
                stale_for = time.monotonic() - self._last_publish_at
                if stale_for >= min_frame_interval:
                    try:
                        self._publish(
                            page.screenshot(
                                type="jpeg",
                                quality=self.jpeg_quality,
                                timeout=5_000,
                            )
                        )
                    except Exception:
                        if self._stop.is_set():
                            break
                page.wait_for_timeout(16)

            try:
                cdp.send("Page.stopScreencast")
            except Exception:
                pass
            context.close()
            browser.close()

    def _try_start_playback(self, page) -> None:
        """Click common play buttons so the user doesn't have to."""
        play_selectors = [
            "button[aria-label*='Play' i]",
            "button[title*='Play' i]",
            ".vjs-big-play-button",
            "[class*='play-button']",
            "button:has-text('Play')",
        ]
        for selector in play_selectors:
            try:
                page.locator(selector).first.click(timeout=1_500)
                print("Started playback automatically.")
                return
            except Exception:
                continue

        try:
            page.evaluate(
                f"""() => {{
                    for (const video of document.querySelectorAll('video')) {{
                        video.muted = {str(not self.capture_audio).lower()};
                        void video.play();
                    }}
                }}"""
            )
        except Exception:
            pass