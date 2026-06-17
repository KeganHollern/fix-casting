"""Measure the audio/video skew AT OUR INPUT BOUNDARY — before any muxing.

This is the explicit test for the claim "the AudioTee audio and the CDP video
arrive at our code already out of sync." It plays the +0-synced clapboard clip
(flash + beep encoded together) in Chrome WITH SOUND, then records the two input
streams SEPARATELY — never muxed:

  * CDP screencast frames -> an .mkv, with each frame's arrival wall-time logged
  * AudioTee PCM          -> a .pcm file, anchored to the wall-time of byte 0

Then, offline, it finds each white flash (in the video) and each beep (in the
audio), converts both to absolute wall-clock arrival times, and reports
flash_arrival - beep_arrival:

    ~0ms      -> the inputs arrive in sync (skew would have to be in our code,
                 which we already disproved)
    ~ +1000ms -> the FLASH arrives ~1s AFTER the beep => the video frame reaches
                 us ~1s staler than the audio. Explicit proof of input skew.

No ffmpeg muxing sits between the two streams, and nothing relies on
subtracting one test from another — it's a direct measurement.

    .venv/bin/python tools/probe_input_skew.py [--seconds 30]
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from statistics import median

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cast_tab.audio import (  # noqa: E402
    audiotee_available,
    stop_audio_capture,
    try_start_chrome_audio_capture,
    _pipe_bytes_available,
)

PAGE = ROOT / "tools" / "clapboard_av_page.html"
OUT_DIR = Path("/tmp/cdp-input-skew")
FPS_REC = 30  # framerate we stamp the recorded .mkv with (for index<->pts math)

LAUNCH_ARGS = [
    "--autoplay-policy=no-user-gesture-required",
    "--disable-features=MediaRouter",
    "--disable-cast-streaming-hw-encoding",
    "--hide-scrollbars",
    "--no-first-run",
    "--no-default-browser-check",
]

_spec = importlib.util.spec_from_file_location(
    "msk", ROOT / "tools" / "measure_source_skew.py"
)
msk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(msk)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--quality", type=int, default=80)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    args = parser.parse_args()

    if not audiotee_available():
        raise SystemExit("AudioTee not found — build it first.")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*"):
        if old.is_file():
            old.unlink()

    mkv = OUT_DIR / "video.mkv"
    pcm_path = OUT_DIR / "audio.pcm"
    profile = OUT_DIR / "chrome-profile"

    recorder = subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-framerate", str(FPS_REC),
            "-i", "pipe:0", "-c:v", "copy", str(mkv),
        ],
        stdin=subprocess.PIPE,
    )

    stop = threading.Event()
    frame_times: list[float] = []  # wall-clock arrival time of each recorded frame
    pending: list[tuple[str | None, str | None, float]] = []
    pending_lock = threading.Lock()

    audio_state: dict[str, float] = {}

    with sync_playwright() as pw:
        try:
            context = pw.chromium.launch_persistent_context(
                str(profile), channel="chrome", headless=False,
                args=LAUNCH_ARGS,
                viewport={"width": args.width, "height": args.height},
                device_scale_factor=1,
            )
        except Exception:
            context = pw.chromium.launch_persistent_context(
                str(profile), headless=False, args=LAUNCH_ARGS,
                viewport={"width": args.width, "height": args.height},
                device_scale_factor=1,
            )

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(PAGE.resolve().as_uri(), wait_until="load")
        page.wait_for_timeout(1500)

        def nudge() -> None:
            try:
                page.evaluate(
                    "document.querySelectorAll('video').forEach(v=>{"
                    "v.muted=false; v.play();})"
                )
            except Exception:
                pass

        print("Attaching audio…")
        capture = try_start_chrome_audio_capture(profile, on_retry=nudge)
        fmt = capture.audio_format
        print(f"Audio attached (pids={capture.pids}).")

        # Drain whatever buffered in the pipe so byte 0 we record ≈ "now".
        try:
            avail = _pipe_bytes_available(capture.read_fd)
            while avail > 0:
                os.read(capture.read_fd, min(avail, 1 << 16))
                avail = _pipe_bytes_available(capture.read_fd)
        except OSError:
            pass

        def read_audio() -> None:
            # Anchor: the first byte read here is captured at ~t0. Sample offset
            # N then maps to wall-clock t0 + N / bytes_per_second, independent of
            # how jittery our reads are (bytes wait in the pipe, never lost).
            with pcm_path.open("wb") as out:
                first = True
                while not stop.is_set():
                    try:
                        chunk = os.read(capture.read_fd, 1 << 16)
                    except OSError:
                        break
                    if not chunk:
                        break
                    if first:
                        audio_state["t0"] = time.time()
                        first = False
                    out.write(chunk)

        audio_thread = threading.Thread(target=read_audio, daemon=True)
        audio_thread.start()

        cdp = context.new_cdp_session(page)

        def on_frame(params: dict) -> None:
            # Stamp arrival the instant CDP hands us the frame.
            with pending_lock:
                pending.append(
                    (params.get("data"), params.get("sessionId"), time.time())
                )

        cdp.on("Page.screencastFrame", on_frame)
        cdp.send("Page.enable")
        cdp.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": args.quality,
             "maxWidth": args.width, "maxHeight": args.height,
             "everyNthFrame": 1},
        )

        print(f"Recording {args.seconds:.0f}s (separate video + audio)…")
        deadline = time.monotonic() + args.seconds
        try:
            while time.monotonic() < deadline and not stop.is_set():
                with pending_lock:
                    batch = pending[:]
                    pending.clear()
                for data_b64, session_id, recv_t in batch:
                    if session_id is not None:
                        try:
                            cdp.send("Page.screencastFrameAck",
                                     {"sessionId": session_id})
                        except Exception:
                            pass
                    if data_b64 is None:
                        continue
                    jpeg = base64.b64decode(data_b64)
                    if recorder.stdin is not None:
                        try:
                            recorder.stdin.write(jpeg)
                        except (BrokenPipeError, OSError):
                            pass
                    frame_times.append(recv_t)
                page.wait_for_timeout(5)
        finally:
            stop.set()
            try:
                cdp.send("Page.stopScreencast")
            except Exception:
                pass
            context.close()

    audio_thread.join(timeout=2)
    if recorder.stdin is not None:
        try:
            recorder.stdin.close()
        except OSError:
            pass
    recorder.wait(timeout=10)
    stop_audio_capture(capture)

    if "t0" not in audio_state:
        raise SystemExit("No audio was captured.")
    t0_audio = audio_state["t0"]

    # --- offline analysis -------------------------------------------------
    # Beeps: convert raw PCM to wav, detect onsets, map to wall-clock.
    wav = OUT_DIR / "audio.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", fmt.ffmpeg_format, "-ar", str(fmt.sample_rate),
         "-ac", str(fmt.channels), "-i", str(pcm_path), str(wav)],
        check=True,
    )
    beep_rel = msk.find_beeps(wav)
    beep_wall = [t0_audio + t for t in beep_rel]

    # Flashes: detect in the mkv, map pts -> recorded-frame index -> arrival time.
    flash_pts = msk.find_flashes(mkv)
    flash_wall: list[float] = []
    for pts in flash_pts:
        idx = round(pts * FPS_REC)
        if 0 <= idx < len(frame_times):
            flash_wall.append(frame_times[idx])

    print(f"\nframes recorded: {len(frame_times)}, "
          f"flashes: {len(flash_wall)}, beeps: {len(beep_wall)}")
    if not flash_wall or not beep_wall:
        print("Not enough events detected to measure.")
        return 1

    # Pair each flash with the nearest beep (events are ~3s apart, so a real
    # skew up to ~1.5s is unambiguous).
    offsets: list[float] = []
    for f in flash_wall:
        nearest = min(beep_wall, key=lambda b: abs(b - f))
        if abs(nearest - f) <= 1.5:
            offsets.append((f - nearest) * 1000)  # flash_arrival - beep_arrival
    if not offsets:
        print("Could not pair flash/beep events within 1.5s.")
        return 1

    med = median(offsets)
    print("\nflash_arrival - beep_arrival, per pulse (ms):")
    print("  " + ", ".join(f"{x:+.0f}" for x in offsets))
    print(f"\nmedian: {med:+.0f} ms  (spread {max(offsets) - min(offsets):.0f} ms)")
    if med > 200:
        print(f"=> At our INPUT, the flash (video) arrives ~{med:.0f}ms AFTER the "
              "beep (audio).")
        print("   The CDP video frame reaches us that much staler than the "
              "AudioTee audio.")
        print("   Explicit proof: the inputs are already out of sync before our "
              "pipeline.")
    elif med < -200:
        print(f"=> Video arrives ~{abs(med):.0f}ms BEFORE audio at the input "
              "(unexpected).")
    else:
        print("=> Inputs arrive within ~200ms — NOT meaningfully skewed at the "
              "boundary. The skew would be elsewhere; my earlier claim is wrong.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
