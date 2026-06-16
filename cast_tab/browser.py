"""Launch a browser tab and capture rendered frames (full tab, not media elements)."""

from __future__ import annotations

import base64
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Literal

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from cast_tab.audio import chrome_pids_for_profile
from cast_tab.stats import PipelineStats

CaptureMethod = Literal["screencast", "screenshot", "playwright"]
CAPTURE_METHODS: frozenset[str] = frozenset({"screencast", "screenshot", "playwright"})
CAPTURE_TIMEOUT_MS = 250


class TabScreencaster:
    """Mirror a browser tab by capturing frames at a steady pace."""

    def __init__(
        self,
        url: str,
        *,
        width: int = 1920,
        height: int = 1080,
        fps: int = 24,
        pace_fps: int | None = None,
        jpeg_quality: int = 75,
        on_frame: Callable[[bytes], None],
        headless: bool = False,
        capture_audio: bool = False,
        capture_method: CaptureMethod = "screencast",
        stats: PipelineStats | None = None,
    ) -> None:
        if capture_method not in CAPTURE_METHODS:
            raise ValueError(f"Unknown capture method: {capture_method}")

        self.url = url
        self.width = width
        self.height = height
        self.fps = fps
        # The rate the encoder consumes at. Capturing faster than this (fps >
        # pace_fps) keeps a fresh frame ready at every encoder tick, so the
        # encoder rarely has to repeat -> smoother constant-rate output.
        self._pace_fps = pace_fps or fps
        self.jpeg_quality = jpeg_quality
        self._on_frame = on_frame
        self.headless = headless
        self.capture_audio = capture_audio
        self.capture_method = capture_method
        self._stats = stats

        self.user_data_dir = Path(tempfile.mkdtemp(prefix="cast-tab-chrome-"))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._capture_enabled = threading.Event()
        self._nudge_playback = threading.Event()
        self._navigate_url: str | None = None
        self._navigate_done = threading.Event()
        self.chrome_pids: list[int] = []

    @property
    def on_frame(self) -> Callable[[bytes], None]:
        return self._on_frame

    @on_frame.setter
    def on_frame(self, callback: Callable[[bytes], None]) -> None:
        self._on_frame = callback

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="tab-screencast", daemon=True)
        self._thread.start()

    def wait_until_ready(self, timeout: float = 120.0) -> None:
        if not self._ready.wait(timeout):
            raise TimeoutError("Timed out waiting for the browser tab to load.")

    def enable_capture(self) -> None:
        self._capture_enabled.set()

    def nudge_playback(self) -> None:
        """Ask the browser thread to retry autoplay (helps audio tap attach)."""
        self._nudge_playback.set()

    def navigate(self, url: str, timeout: float = 130.0) -> None:
        """Navigate the live tab to a new URL (keeps the same capture streams).

        Used to switch from the calibration page to the user's URL without
        tearing down the screencast/audio tap that calibration measured.
        """
        self._navigate_done.clear()
        self._navigate_url = url
        if not self._navigate_done.wait(timeout):
            raise TimeoutError("Timed out navigating the browser tab.")

    def _perform_navigate(self, page) -> None:
        url = self._navigate_url
        self._navigate_url = None
        self.url = url or self.url
        try:
            print(f"Loading {url} ...")
            page.goto(url, wait_until="load", timeout=120_000)
            page.add_style_tag(
                content="html,body{overflow:hidden!important;margin:0!important;}"
            )
            self._try_start_playback(page)
            page.wait_for_timeout(1_000)
            self.chrome_pids = chrome_pids_for_profile(self.user_data_dir)
        except Exception:
            pass
        finally:
            self._navigate_done.set()

    def stop(self) -> None:
        self._stop.set()
        self._capture_enabled.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

    def _run(self) -> None:
        with sync_playwright() as playwright:
            launch_args = [
                "--autoplay-policy=no-user-gesture-required",
                "--disable-features=MediaRouter",
                "--disable-cast-streaming-hw-encoding",
                "--hide-scrollbars",
                "--no-first-run",
                "--no-default-browser-check",
            ]

            try:
                context = playwright.chromium.launch_persistent_context(
                    str(self.user_data_dir),
                    channel="chrome",
                    headless=self.headless,
                    args=launch_args,
                    viewport={"width": self.width, "height": self.height},
                    device_scale_factor=1,
                    ignore_https_errors=True,
                )
            except Exception:
                context = playwright.chromium.launch_persistent_context(
                    str(self.user_data_dir),
                    headless=self.headless,
                    args=launch_args,
                    viewport={"width": self.width, "height": self.height},
                    device_scale_factor=1,
                    ignore_https_errors=True,
                )

            context.grant_permissions(["notifications", "geolocation"])
            page = context.pages[0] if context.pages else context.new_page()
            print(f"Loading {self.url} ...")
            page.goto(self.url, wait_until="load", timeout=120_000)
            page.add_style_tag(
                content="html,body{overflow:hidden!important;margin:0!important;}"
            )
            self._try_start_playback(page)
            page.wait_for_timeout(1_500)
            self.chrome_pids = chrome_pids_for_profile(self.user_data_dir)
            print("Page loaded, starting capture.")
            self._ready.set()
            self._capture_enabled.wait()

            needs_cdp = self.capture_method in ("screenshot", "screencast")
            cdp = context.new_cdp_session(page) if needs_cdp else None

            try:
                if self.capture_method == "screencast":
                    self._run_screencast(page, cdp)
                else:
                    self._run_screenshot_loop(page, cdp)
            finally:
                context.close()

    def _run_screenshot_loop(self, page, cdp) -> None:
        """Pull model: grab one frame per tick at a steady pace."""
        capture_period = 1.0 / self.fps
        pace_period = 1.0 / self._pace_fps
        next_tick = time.monotonic()

        while not self._stop.is_set():
            if self._navigate_url is not None:
                self._perform_navigate(page)
            if self._nudge_playback.is_set():
                self._nudge_playback.clear()
                self._try_start_playback(page)
                self.chrome_pids = chrome_pids_for_profile(self.user_data_dir)

            now = time.monotonic()
            if now >= next_tick:
                latency = None
                try:
                    started = time.monotonic()
                    self._on_frame(self._capture_frame(page, cdp))
                    latency = time.monotonic() - started
                except PlaywrightTimeoutError:
                    if self._stats is not None:
                        self._stats.record_capture_timeout()
                except Exception:
                    if self._stats is not None:
                        self._stats.record_capture_error()
                    if self._stop.is_set():
                        break
                next_tick += capture_period
                if next_tick < now:
                    # Capture is the bottleneck (slower than the target
                    # rate); run flat-out instead of building a backlog.
                    next_tick = now + capture_period
                if latency is not None and self._stats is not None:
                    # "behind" = capture blew the encoder's frame budget,
                    # so the encoder may have to repeat this frame.
                    self._stats.record_capture(latency, behind=latency > pace_period)

            self._stop.wait(timeout=0.002)

    def _start_screencast(self, cdp) -> None:
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

    def _run_screencast(self, page, cdp) -> None:
        """Push model: Chrome streams frames as the page paints (up to ~60fps).

        Each Page.screencastFrame MUST be acknowledged or Chrome stops sending
        after a few frames (the classic screencast "freeze"). The event handler
        only enqueues; we ack and publish from this loop so we never re-enter
        Playwright from inside a CDP callback.
        """
        assert cdp is not None
        pace_period = 1.0 / self._pace_fps
        pending: deque[tuple[str | None, str | None]] = deque()

        def on_screencast_frame(params: dict) -> None:
            pending.append((params.get("data"), params.get("sessionId")))

        cdp.on("Page.screencastFrame", on_screencast_frame)
        cdp.send("Page.enable")
        self._start_screencast(cdp)

        try:
            while not self._stop.is_set():
                if self._navigate_url is not None:
                    try:
                        cdp.send("Page.stopScreencast")
                    except Exception:
                        pass
                    pending.clear()
                    self._perform_navigate(page)
                    self._start_screencast(cdp)
                if self._nudge_playback.is_set():
                    self._nudge_playback.clear()
                    self._try_start_playback(page)
                    self.chrome_pids = chrome_pids_for_profile(self.user_data_dir)

                while pending:
                    data_b64, session_id = pending.popleft()
                    # Ack first so Chrome keeps the frames flowing.
                    if session_id is not None:
                        try:
                            cdp.send(
                                "Page.screencastFrameAck", {"sessionId": session_id}
                            )
                        except Exception:
                            if self._stop.is_set():
                                return
                    if data_b64 is None:
                        continue
                    started = time.monotonic()
                    try:
                        self._on_frame(base64.b64decode(data_b64))
                    except Exception:
                        if self._stats is not None:
                            self._stats.record_capture_error()
                        if self._stop.is_set():
                            return
                        continue
                    if self._stats is not None:
                        latency = time.monotonic() - started
                        self._stats.record_capture(latency, behind=latency > pace_period)

                # Pump the Playwright/CDP event loop so new frames are delivered.
                page.wait_for_timeout(5)
        finally:
            try:
                cdp.send("Page.stopScreencast")
            except Exception:
                pass

    def _capture_frame(self, page, cdp) -> bytes:
        page.set_default_timeout(CAPTURE_TIMEOUT_MS)
        try:
            if self.capture_method == "screenshot":
                assert cdp is not None
                shot = cdp.send(
                    "Page.captureScreenshot",
                    {
                        "format": "jpeg",
                        "quality": self.jpeg_quality,
                        # Trade a little quality for a faster encode path so the
                        # screenshot round-trip can clear the encoder's budget.
                        "optimizeForSpeed": True,
                    },
                )
                return base64.b64decode(shot["data"])

            return page.screenshot(
                type="jpeg",
                quality=self.jpeg_quality,
                animations="disabled",
                caret="hide",
            )
        finally:
            page.set_default_timeout(5_000)

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