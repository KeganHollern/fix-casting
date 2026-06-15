"""Manual harness to validate A/V sync calibration without casting to a TV.

Runs the flash+beep calibration page through the real capture path, dumps the
captured video-size and audio-RMS time series and detected events, and reports
the measured offset. Lets us tune detection thresholds in isolation.

    .venv/bin/python tools/calibration_harness.py            # screencast, windowed
    .venv/bin/python tools/calibration_harness.py --headless
    .venv/bin/python tools/calibration_harness.py --capture screenshot
"""

from __future__ import annotations

import argparse
import time

from cast_tab import calibration as cal
from cast_tab.audio import (
    AudioCaptureError,
    stop_audio_capture,
    try_start_chrome_audio_capture,
)
from cast_tab.browser import TabScreencaster


def _summary(label: str, values: list[float]) -> None:
    if not values:
        print(f"  {label}: (none)")
        return
    ordered = sorted(values)
    median = ordered[len(ordered) // 2]
    print(
        f"  {label}: n={len(values)} min={min(values):.4g} "
        f"median={median:.4g} max={max(values):.4g}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--capture", default="screencast",
        choices=["screencast", "screenshot", "playwright"],
    )
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--duration", type=float, default=12.0)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--video-ratio", type=float, default=3.0)
    ap.add_argument("--audio-ratio", type=float, default=4.0)
    args = ap.parse_args()

    frames: list[tuple[float, int]] = []

    def collect(jpeg: bytes) -> None:
        frames.append((time.monotonic(), len(jpeg)))

    sc = TabScreencaster(
        cal._calibration_url(),
        width=args.width,
        height=args.height,
        fps=30,
        jpeg_quality=75,
        on_frame=collect,
        headless=args.headless,
        capture_audio=True,
        capture_method=args.capture,
    )
    audio_cap = None
    audio_series: list[tuple[float, float]] = []
    try:
        sc.start()
        sc.wait_until_ready()
        sc.enable_capture()
        print(f"page ready (capture={args.capture}, headless={args.headless}); attaching audio...")
        try:
            audio_cap = try_start_chrome_audio_capture(
                sc.user_data_dir, on_retry=sc.nudge_playback
            )
            print(f"audio attached: {audio_cap.audio_format}")
        except AudioCaptureError as exc:
            print(f"AUDIO ATTACH FAILED: {exc}")

        if audio_cap is not None:
            fmt = audio_cap.audio_format
            audio_series = cal._read_audio_rms(
                audio_cap.read_fd,
                sample_rate=fmt.sample_rate,
                channels=fmt.channels,
                sample_bytes=fmt.sample_bytes,
                duration_s=args.duration,
            )
        else:
            time.sleep(args.duration)
    finally:
        sc.stop()
        stop_audio_capture(audio_cap)

    t0 = frames[0][0] if frames else 0.0

    print(f"\n=== VIDEO ({args.duration:.0f}s) ===")
    _summary("jpeg bytes", [float(s) for _, s in frames])
    video_events = cal._detect_events(frames, ratio=args.video_ratio)
    print(f"  flash events @ {[round(t - t0, 2) for t in video_events]}")

    print("\n=== AUDIO ===")
    _summary("rms", [v for _, v in audio_series])
    audio_events = cal._detect_events(audio_series, ratio=args.audio_ratio)
    at0 = audio_series[0][0] if audio_series else 0.0
    print(f"  beep events  @ {[round(t - at0, 2) for t in audio_events]}")

    print("\n=== OFFSET ===")
    result = cal._median_offset(video_events, audio_events)
    if result is None:
        print("  could not match flash/beep pairs")
    else:
        offset_s, matched = result
        print(f"  audio leads video by {offset_s * 1000:+.0f}ms ({matched} pairs)")


if __name__ == "__main__":
    main()
