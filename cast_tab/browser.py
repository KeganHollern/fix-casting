"""Launch a browser tab and capture rendered frames (full tab, not media elements)."""

from __future__ import annotations

import base64
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from playwright.sync_api import sync_playwright

from cast_tab.stats import PipelineStats

# AudioTee can only translate a Chrome PID into a CoreAudio process object once
# that process owns an active audio client.  A page which is silent at startup
# therefore used to make audio attachment fail permanently.  Keep an inaudible
# Web Audio graph running in the top-level page: its -140 dB carrier is far
# below audibility, but unlike an exact zero (which Chrome may optimize away)
# it makes Chrome continuously render samples until the site's real audio
# begins.  The graph is stored on globalThis so repeated playback nudges are
# idempotent and so the nodes are not garbage-collected.
_AUDIO_KEEPALIVE_INIT_SCRIPT = r"""
(() => {
    if (globalThis.top !== globalThis) return;

    const stateKey = "__fixCastingAudioKeepalive";
    const ensureKey = "__fixCastingEnsureAudioKeepalive";

    globalThis[ensureKey] = async () => {
        let state = globalThis[stateKey];
        if (!state || state.context.state === "closed") {
            const AudioContextClass =
                globalThis.AudioContext || globalThis.webkitAudioContext;
            if (!AudioContextClass) return "unavailable";

            const context = new AudioContextClass({latencyHint: "playback"});
            const oscillator = context.createOscillator();
            const gain = context.createGain();
            gain.gain.setValueAtTime(1e-7, context.currentTime);
            oscillator.connect(gain);
            gain.connect(context.destination);
            oscillator.start();
            state = {context, oscillator, gain};
            globalThis[stateKey] = state;
        }

        if (state.context.state !== "running") {
            await state.context.resume();
        }
        return state.context.state;
    };

    void globalThis[ensureKey]().catch(() => {});
})();
"""

_AUDIO_KEEPALIVE_RESUME_SCRIPT = r"""
() => {
    const ensure = globalThis.__fixCastingEnsureAudioKeepalive;
    if (!ensure) return "unavailable";
    void ensure().catch(() => {});
    return "requested";
}
"""

BROWSER_LAUNCH_TIMEOUT_MS = 15_000
BROWSER_NAVIGATION_TIMEOUT_MS = 30_000
BROWSER_STOP_JOIN_S = 10.0
BROWSER_TERMINATION_GRACE_S = 2.0
BROWSER_POST_TERMINATE_JOIN_S = 3.0


class TabScreencaster:
    """Mirror a browser tab by capturing frames at a steady pace."""

    def __init__(
        self,
        url: str,
        *,
        width: int = 1920,
        height: int = 1080,
        fps: int = 24,
        jpeg_quality: int = 75,
        on_frame: Callable[[bytes, float | None], None],
        headless: bool = False,
        capture_audio: bool = False,
        stats: PipelineStats | None = None,
        adblock_patterns: list[str] | None = None,
    ) -> None:
        self.url = url
        self.width = width
        self.height = height
        # The encoder's consumption rate. Capture itself is paint-driven (CDP
        # screencast has no rate knob); this only sets the "behind" threshold
        # for capture-latency stats.
        self.fps = fps
        self.jpeg_quality = jpeg_quality
        self._on_frame = on_frame
        self._frame_state_lock = threading.Lock()
        self._latest_frame: tuple[bytes, float | None] | None = None
        self._replay_latest = False
        self.headless = headless
        self.capture_audio = capture_audio
        self._stats = stats
        self._adblock_patterns = adblock_patterns

        self.user_data_dir = Path(tempfile.mkdtemp(prefix="cast-tab-chrome-"))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        # _startup_finished wakes wait_until_ready() for both success and
        # failure.  A bare _ready event leaves the caller waiting for the full
        # timeout when Playwright fails on its worker thread.
        self._startup_finished = threading.Event()
        self._finished = threading.Event()
        self._failure: BaseException | None = None
        self._capture_enabled = threading.Event()
        self._nudge_playback = threading.Event()
        self._restore_local_audio = threading.Event()
        self._chrome_may_be_alive = False

    @property
    def on_frame(self) -> Callable[[bytes, float | None], None]:
        with self._frame_state_lock:
            return self._on_frame

    @on_frame.setter
    def on_frame(self, callback: Callable[[bytes, float | None], None]) -> None:
        # Callback execution remains browser-thread-owned. The setter only
        # requests a replay, avoiding cross-thread reordering/deadlocks.
        with self._frame_state_lock:
            self._on_frame = callback
            self._replay_latest = self._latest_frame is not None

    def _deliver_frame(self, jpeg_data: bytes, captured_at: float | None) -> None:
        """Publish a real frame and atomically satisfy any pending handoff."""
        with self._frame_state_lock:
            self._latest_frame = (jpeg_data, captured_at)
            callback = self._on_frame
            # A newer actual frame supersedes replaying the retained one.
            self._replay_latest = False
        callback(jpeg_data, captured_at)

    def _replay_latest_if_requested(self) -> bool:
        """Replay one retained frame on the browser worker after handoff."""
        with self._frame_state_lock:
            if not self._replay_latest or self._latest_frame is None:
                return False
            self._replay_latest = False
            jpeg_data, captured_at = self._latest_frame
            callback = self._on_frame
        callback(jpeg_data, captured_at)
        return True

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Browser capture has already been started.")
        self._thread = threading.Thread(target=self._run, name="tab-screencast", daemon=True)
        self._thread.start()

    def wait_until_ready(
        self,
        timeout: float = 120.0,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        deadline = time.monotonic() + timeout
        while not self._startup_finished.is_set():
            if cancelled is not None and cancelled():
                raise RuntimeError("Browser startup was cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for the browser tab to load.")
            self._startup_finished.wait(min(0.1, remaining))
        if cancelled is not None and cancelled():
            raise RuntimeError("Browser startup was cancelled.")
        if self._ready.is_set():
            # The capture loop can fail immediately after marking the page
            # ready.  Do not report a successful startup if that already
            # happened by the time this thread resumed.
            self.raise_if_failed()
            return
        if self._stop.is_set():
            raise RuntimeError("Browser capture stopped before the tab was ready.")
        self.raise_if_failed()
        raise RuntimeError("Browser capture exited before the tab was ready.")

    def raise_if_failed(self) -> None:
        """Raise a browser-worker failure in the owning thread.

        Callers should poll this after startup.  Chrome can disappear after
        HLS has a last frame to repeat, which otherwise looks like a healthy
        but permanently frozen stream.
        """
        if self._stop.is_set():
            return
        failure = self._failure
        if failure is not None:
            raise failure
        if self._finished.is_set():
            raise RuntimeError("Browser capture exited unexpectedly.")

    def enable_capture(self) -> None:
        self._capture_enabled.set()

    def nudge_playback(self) -> None:
        """Ask the browser thread to retry autoplay (helps audio tap attach)."""
        self._nudge_playback.set()

    def restore_local_audio(self) -> None:
        """Unmute page media after optional tab-audio capture degrades."""
        self._restore_local_audio.set()

    def stop(self) -> None:
        self._stop.set()
        self._capture_enabled.set()
        # Wake a concurrent startup waiter immediately; the browser thread may
        # still be inside a long navigation while its bounded join runs.
        self._startup_finished.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=BROWSER_STOP_JOIN_S)
            if self._thread.is_alive():
                self._terminate_owned_browser_processes()
                self._thread.join(timeout=BROWSER_POST_TERMINATE_JOIN_S)
            if self._thread.is_alive():
                raise TimeoutError(
                    "Browser capture did not stop after its bounded teardown; "
                    f"profile retained at {self.user_data_dir}."
                )

        # Playwright can return/raise after its worker has already exited while
        # a Chrome child launched with this exact profile remains alive. Never
        # use worker liveness as a proxy for browser-process ownership.
        if not self._chrome_may_be_alive:
            shutil.rmtree(self.user_data_dir, ignore_errors=True)
            return
        owned = self._owned_browser_process_ids()
        if owned is None or owned:
            self._terminate_owned_browser_processes()
            owned = self._owned_browser_process_ids()
        if owned == []:
            self._chrome_may_be_alive = False
            shutil.rmtree(self.user_data_dir, ignore_errors=True)
        elif owned is None:
            raise RuntimeError(
                "Could not verify that profile-owned Chrome processes stopped; "
                f"profile retained at {self.user_data_dir}."
            )
        else:
            raise TimeoutError(
                "Profile-owned Chrome processes did not stop after termination; "
                f"profile retained at {self.user_data_dir}."
            )

    def _record_failure(self, failure: BaseException) -> None:
        # Keep the first/root failure if cleanup itself subsequently fails.
        if self._failure is None:
            self._failure = failure
        self._startup_finished.set()

    def _run(self) -> None:
        # Each run gets a fresh mkdtemp profile; without cleanup they
        # accumulate in /tmp at tens-to-hundreds of MB per cast. Only remove
        # it when Chrome is known dead — if context.close() itself failed,
        # a live Chrome may still be using the dir, and deleting it out from
        # under the process is worse than leaking one profile.
        self._chrome_may_be_alive = False
        try:
            self._run_browser()
        except BaseException as exc:
            # Exceptions cannot cross a thread boundary by themselves.  Store
            # the original object so wait_until_ready()/raise_if_failed() can
            # preserve its useful type and message in the owning thread.
            self._record_failure(exc)
        finally:
            if self._chrome_may_be_alive:
                owned = self._owned_browser_process_ids()
                if owned == []:
                    self._chrome_may_be_alive = False
            if not self._chrome_may_be_alive:
                shutil.rmtree(self.user_data_dir, ignore_errors=True)
            self._finished.set()
            self._startup_finished.set()

    def _run_browser(self) -> None:
        with sync_playwright() as playwright:
            launch_args = [
                "--autoplay-policy=no-user-gesture-required",
                "--disable-features=MediaRouter",
                "--disable-cast-streaming-hw-encoding",
                "--hide-scrollbars",
                "--no-first-run",
                "--no-default-browser-check",
            ]

            context = self._launch_context(playwright.chromium, launch_args)

            self._chrome_may_be_alive = True
            try:
                if self._stop.is_set():
                    return
                context.grant_permissions(["notifications", "geolocation"])
                self._install_audio_keepalive(context)
                page = context.pages[0] if context.pages else context.new_page()
                if self._adblock_patterns:
                    # Native CDP blocking on a dedicated session, set before the
                    # navigation so the page's requests are filtered from the start.
                    from cast_tab.adblocking import apply_to_page

                    apply_to_page(context.new_cdp_session(page), self._adblock_patterns)
                print(f"Loading {self.url} ...")
                page.goto(
                    self.url,
                    wait_until="domcontentloaded",
                    timeout=BROWSER_NAVIGATION_TIMEOUT_MS,
                )
                page.add_style_tag(
                    content="html,body{overflow:hidden!important;margin:0!important;}"
                )
                self._try_start_playback(page)
                self._ensure_audio_keepalive(page)
                page.wait_for_timeout(1_500)
                print("Page loaded, starting capture.")
                self._ready.set()
                self._startup_finished.set()
                self._capture_enabled.wait()

                cdp = context.new_cdp_session(page)
                self._run_screencast(page, cdp)
            except BaseException as exc:
                # Publish the operational failure before context.close();
                # closing a damaged Chrome connection can itself be slow.
                self._record_failure(exc)
                raise
            finally:
                # Close in all paths (goto/setup failures included) so Chrome
                # is not left running against the profile dir we remove after.
                try:
                    context.close()
                except BaseException:
                    owned = self._owned_browser_process_ids()
                    if owned == []:
                        self._chrome_may_be_alive = False
                    raise
                else:
                    self._chrome_may_be_alive = False

    def _launch_context(self, chromium, launch_args: list[str]):
        options = {
            "headless": self.headless,
            "args": launch_args,
            "viewport": {"width": self.width, "height": self.height},
            "device_scale_factor": 1,
            "ignore_https_errors": True,
            "timeout": BROWSER_LAUNCH_TIMEOUT_MS,
        }
        # A Playwright launch call can spawn Chrome and still time out/raise.
        # Claim possible process ownership before entering that call so the
        # worker-finally/stop path retains and scans the exact profile.
        self._chrome_may_be_alive = True
        try:
            return chromium.launch_persistent_context(
                str(self.user_data_dir),
                channel="chrome",
                **options,
            )
        except Exception as system_chrome_error:
            if self._stop.is_set():
                raise RuntimeError("Browser startup was cancelled.") from system_chrome_error
            try:
                context = chromium.launch_persistent_context(
                    str(self.user_data_dir),
                    **options,
                )
            except Exception as bundled_chromium_error:
                raise ExceptionGroup(
                    "Both system Chrome and bundled Chromium failed to launch.",
                    [system_chrome_error, bundled_chromium_error],
                ) from system_chrome_error
            print(
                "Warning: system Chrome failed "
                f"({system_chrome_error}); using bundled Chromium.",
                flush=True,
            )
            return context

    def _owned_browser_process_ids(self) -> list[int] | None:
        """Find only Chrome processes launched against this unique profile."""
        try:
            result = subprocess.run(
                ["ps", "-axo", "pid=,command="],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        equals_marker = f"--user-data-dir={self.user_data_dir}"
        spaced_marker = f"--user-data-dir {self.user_data_dir}"
        pids: list[int] = []
        for line in result.stdout.splitlines():
            stripped = line.strip()
            pid_text, separator, command = stripped.partition(" ")
            if not separator or (
                equals_marker not in command and spaced_marker not in command
            ):
                continue
            try:
                pid = int(pid_text)
            except ValueError:
                continue
            if pid != os.getpid():
                pids.append(pid)
        return pids

    def _terminate_owned_browser_processes(self) -> int:
        """Gracefully terminate Chrome processes tied to this exact profile."""
        pids = self._owned_browser_process_ids()
        if not pids:
            return 0
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + BROWSER_TERMINATION_GRACE_S
        survivors = pids
        while survivors and time.monotonic() < deadline:
            time.sleep(0.05)
            current = self._owned_browser_process_ids()
            if current is None:
                break
            survivors = current
        if survivors:
            current = self._owned_browser_process_ids()
            if current is not None:
                for pid in current:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                kill_deadline = time.monotonic() + BROWSER_TERMINATION_GRACE_S
                while current and time.monotonic() < kill_deadline:
                    time.sleep(0.05)
                    rescanned = self._owned_browser_process_ids()
                    if rescanned is None:
                        break
                    current = rescanned
        return len(pids)

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

    @staticmethod
    def _capture_monotonic_time(
        capture_timestamp: float | None,
        *,
        wall_now: float,
        monotonic_now: float,
    ) -> tuple[float, float | None]:
        """Translate Chrome's wall timestamp into the sampler clock domain."""
        if capture_timestamp is None:
            return monotonic_now, None
        lag = wall_now - capture_timestamp
        if not 0.0 <= lag < 60.0:
            return monotonic_now, None
        return monotonic_now - lag, lag

    def _run_screencast(self, page, cdp) -> None:
        """Push model: Chrome streams frames as the page paints (up to ~60fps).

        Each Page.screencastFrame MUST be acknowledged or Chrome stops sending
        after a few frames (the classic screencast "freeze"). The event handler
        only enqueues; we ack and publish from this loop so we never re-enter
        Playwright from inside a CDP callback.
        """
        pace_period = 1.0 / self.fps
        pending: deque[tuple[str | None, str | None, float | None]] = deque()

        def on_screencast_frame(params: dict) -> None:
            # metadata.timestamp is Chrome's capture time (seconds since epoch),
            # comparable to time.time(); it lets us measure how stale the frame
            # already is by the moment it reaches us.
            metadata = params.get("metadata") or {}
            pending.append(
                (params.get("data"), params.get("sessionId"), metadata.get("timestamp"))
            )

        cdp.on("Page.screencastFrame", on_screencast_frame)
        cdp.send("Page.enable")
        self._start_screencast(cdp)

        try:
            while not self._stop.is_set():
                while pending:
                    data_b64, session_id, capture_ts = pending.popleft()
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
                    # Chrome's timestamp is wall-clock based, but the sampler
                    # uses monotonic time. Translate it at receipt so downstream
                    # selection can preserve capture cadence across delivery
                    # bursts and late sampler wake-ups.
                    captured_at, lag = self._capture_monotonic_time(
                        capture_ts,
                        wall_now=time.time(),
                        monotonic_now=started,
                    )
                    if self._stats is not None:
                        self._stats.trace("first screencast frame from chrome", once=True)
                        if lag is not None:
                            self._stats.record_screencast_lag(lag)
                    try:
                        self._deliver_frame(base64.b64decode(data_b64), captured_at)
                    except Exception:
                        if self._stats is not None:
                            self._stats.record_capture_error()
                        if self._stop.is_set():
                            return
                        continue
                    if self._stats is not None:
                        latency = time.monotonic() - started
                        self._stats.record_capture(latency, behind=latency > pace_period)

                try:
                    self._replay_latest_if_requested()
                except Exception:
                    if self._stats is not None:
                        self._stats.record_capture_error()
                    if self._stop.is_set():
                        return

                self._service_playback_nudge(page)
                self._service_local_audio_restore(page)

                # Pump the Playwright/CDP event loop so new frames are delivered.
                page.wait_for_timeout(5)
        finally:
            try:
                cdp.send("Page.stopScreencast")
            except Exception:
                pass

    def _install_audio_keepalive(self, context) -> None:
        """Prime Chrome's CoreAudio client with an inaudible Web Audio graph.

        The init script runs again after a full-page navigation, while the
        top-frame guard prevents every embedded frame from creating its own
        AudioContext.  It is only installed when this session requested audio.
        """
        if not self.capture_audio:
            return
        context.add_init_script(script=_AUDIO_KEEPALIVE_INIT_SCRIPT)

    def _ensure_audio_keepalive(self, page) -> None:
        """Request resumption after autoplay attempts or navigation."""
        if not self.capture_audio:
            return
        try:
            page.evaluate(_AUDIO_KEEPALIVE_RESUME_SCRIPT)
        except Exception:
            # Some transient navigation states reject evaluation.  The init
            # script and the next playback nudge will try again.
            pass

    def _try_start_playback(self, page) -> None:
        """Click common play buttons, then enforce the capture audio policy."""
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
                break
            except Exception:
                continue

        try:
            page.evaluate(
                f"""() => {{
                    for (const media of document.querySelectorAll('video,audio')) {{
                        media.muted = {str(not self.capture_audio).lower()};
                        const request = media.play();
                        if (request && typeof request.catch === "function") {{
                            void request.catch(() => {{}});
                        }}
                    }}
                }}"""
            )
        except Exception:
            pass

    def _service_playback_nudge(self, page) -> bool:
        """Service one coalesced capture-time autoplay request."""
        if not self._nudge_playback.is_set():
            return False
        self._nudge_playback.clear()
        self._try_nudge_playback(page)
        self._ensure_audio_keepalive(page)
        return True

    def _service_local_audio_restore(self, page) -> bool:
        """Service one browser-thread-owned local-audio fallback request."""
        if not self._restore_local_audio.is_set():
            return False
        self._restore_local_audio.clear()
        try:
            page.evaluate(
                r"""
() => {
    for (const media of document.querySelectorAll("video,audio")) {
        media.muted = false;
        const request = media.play();
        if (request && typeof request.catch === "function") {
            void request.catch(() => {});
        }
    }
}
"""
            )
        except Exception:
            # Navigation can invalidate the context; a later degradation or
            # playback retry can request the operation again. Keep this one
            # pending so the fallback cannot be lost in a navigation race.
            self._restore_local_audio.set()
            return False
        self._ensure_audio_keepalive(page)
        return True

    def _try_nudge_playback(self, page) -> None:
        """Retry playback without Playwright locator auto-waiting.

        This runs inside the sole CDP pump, so it must be one synchronous DOM
        evaluation with no awaited play promise or selector timeout.
        """
        try:
            script = r"""
() => {
    const selectors = [
        "button[aria-label*='Play' i]",
        "button[title*='Play' i]",
        ".vjs-big-play-button",
        "[class*='play-button']"
    ];
    for (const selector of selectors) {
        const button = Array.from(document.querySelectorAll(selector)).find((node) => {
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 &&
                style.visibility !== "hidden" && style.display !== "none";
        });
        if (button) {
            button.click();
            break;
        }
    }
    for (const media of document.querySelectorAll("video,audio")) {
        media.muted = __FIX_CASTING_MUTED__;
        const request = media.play();
        if (request && typeof request.catch === "function") {
            void request.catch(() => {});
        }
    }
}
"""
            page.evaluate(
                script.replace(
                    "__FIX_CASTING_MUTED__",
                    str(not self.capture_audio).lower(),
                )
            )
        except Exception:
            # Navigation can invalidate the execution context. The next audio
            # retry will coalesce into another bounded nudge.
            pass
