"""Visualize how delayed CDP screencast capture is vs. the real browser.

Opens Chrome on tools/cdp_clock.html (a DOM millisecond clock + a <video>
playing a clip with a burned-in timecode), captures the tab with the exact
CDP Page.startScreencast path the caster uses, and emits the captured frames
two ways so you can eyeball the delay:

  * a LIVE MJPEG stream at http://localhost:<port>/  (open in any browser)
  * a saved .mkv at /tmp/cdp-delay/capture.mkv       (frame-step in VLC)

What to look for:
  1) Whole-image delay: put this stream (or the real Chrome window) next to the
     captured stream and compare the cyan DOM clock. That's how stale the whole
     captured frame is.
  2) <video>-layer delay: in a single captured frame, compare the yellow number
     (what the browser thinks the video is presenting) with the timecode burned
     into the video picture. A gap means the <video> layer is read back late by
     the screencast — the suspected source of the audio-ahead skew.

Usage:
    .venv/bin/python tools/test_cdp_delay.py [--port 8099] [--quality 80]
Press Ctrl+C to stop and finalize the .mkv.
"""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "tools" / "cdp_clock.html"
OUT_DIR = Path("/tmp/cdp-delay")

# Match the caster's launch flags so the test reflects production behavior
# (hardware video decode/compositing is exactly what we're probing).
LAUNCH_ARGS = [
    "--autoplay-policy=no-user-gesture-required",
    "--disable-features=MediaRouter",
    "--disable-cast-streaming-hw-encoding",
    "--hide-scrollbars",
    "--no-first-run",
    "--no-default-browser-check",
]


class LatestJpeg:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: bytes | None = None

    def set(self, data: bytes) -> None:
        with self._lock:
            self._data = data

    def get(self) -> bytes | None:
        with self._lock:
            return self._data


def make_mjpeg_server(port: int, latest: LatestJpeg) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a) -> None:
            pass

        def do_GET(self) -> None:
            if self.path not in ("/", "/stream", "/stream.mjpg"):
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=frame"
            )
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    frame = latest.get()
                    if frame is None:
                        time.sleep(0.02)
                        continue
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(
                        f"Content-Length: {len(frame)}\r\n\r\n".encode()
                    )
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    # ~30fps cap; the latest-frame holder means we never block
                    # the capture loop on a slow viewer.
                    time.sleep(1 / 30)
            except (BrokenPipeError, ConnectionResetError):
                return

    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def start_recorder(fps: int) -> subprocess.Popen[bytes]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "capture.mkv"
    out.unlink(missing_ok=True)
    # Copy the JPEGs straight into an MKV (no re-encode) so every captured
    # pixel is preserved exactly for frame stepping. MKV tolerates an abrupt
    # Ctrl+C far better than MP4.
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-framerate",
        str(fps),
        "-i",
        "pipe:0",
        "-c:v",
        "copy",
        str(out),
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--quality", type=int, default=80)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--fps", type=int, default=30, help="Recorder framerate.")
    args = parser.parse_args()

    latest = LatestJpeg()
    server = make_mjpeg_server(args.port, latest)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    recorder = start_recorder(args.fps)

    print(f"Live capture stream:  http://localhost:{args.port}/")
    print(f"Saved video (VLC):    {OUT_DIR / 'capture.mkv'}")
    print("Watch: cyan DOM clock = whole-image freshness; yellow number vs the")
    print("video's burned timecode = <video>-layer delay. Ctrl+C to stop.\n")

    stop = threading.Event()
    pending: deque[tuple[str | None, str | None, float | None]] = deque()

    with sync_playwright() as pw:
        try:
            context = pw.chromium.launch_persistent_context(
                str(OUT_DIR / "chrome-profile"),
                channel="chrome",
                headless=False,
                args=LAUNCH_ARGS,
                viewport={"width": args.width, "height": args.height},
                device_scale_factor=1,
            )
        except Exception:
            context = pw.chromium.launch_persistent_context(
                str(OUT_DIR / "chrome-profile"),
                headless=False,
                args=LAUNCH_ARGS,
                viewport={"width": args.width, "height": args.height},
                device_scale_factor=1,
            )

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(PAGE.resolve().as_uri(), wait_until="load")
        page.wait_for_timeout(1000)

        cdp = context.new_cdp_session(page)

        def on_frame(params: dict) -> None:
            md = params.get("metadata") or {}
            pending.append(
                (params.get("data"), params.get("sessionId"), md.get("timestamp"))
            )

        cdp.on("Page.screencastFrame", on_frame)
        cdp.send("Page.enable")
        cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": args.quality,
                "maxWidth": args.width,
                "maxHeight": args.height,
                "everyNthFrame": 1,
            },
        )

        frames = 0
        lag_total = 0.0
        last_report = time.monotonic()
        try:
            while not stop.is_set():
                while pending:
                    data_b64, session_id, swap_ts = pending.popleft()
                    if session_id is not None:
                        try:
                            cdp.send(
                                "Page.screencastFrameAck", {"sessionId": session_id}
                            )
                        except Exception:
                            pass
                    if data_b64 is None:
                        continue
                    jpeg = base64.b64decode(data_b64)
                    latest.set(jpeg)
                    if recorder.stdin is not None:
                        try:
                            recorder.stdin.write(jpeg)
                        except (BrokenPipeError, OSError):
                            pass
                    frames += 1
                    if swap_ts is not None:
                        lag_total += time.time() - swap_ts

                now = time.monotonic()
                if now - last_report >= 2.0:
                    n = max(1, frames)
                    print(
                        f"captured {frames} frames, "
                        f"avg swap→receive {lag_total / n * 1000:.0f}ms "
                        f"({frames / (now - last_report):.0f} fps)",
                        flush=True,
                    )
                    frames = 0
                    lag_total = 0.0
                    last_report = now
                page.wait_for_timeout(5)
        except KeyboardInterrupt:
            print("\nStopping…")
        finally:
            stop.set()
            try:
                cdp.send("Page.stopScreencast")
            except Exception:
                pass
            context.close()

    if recorder.stdin is not None:
        try:
            recorder.stdin.close()
        except OSError:
            pass
    recorder.wait(timeout=10)
    server.shutdown()
    print(f"\nSaved: {OUT_DIR / 'capture.mkv'}  — open in VLC and step frames (E).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
