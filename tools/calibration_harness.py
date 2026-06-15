"""Manual harness to validate output-based A/V sync calibration (no TV needed).

Runs the flash+beep page through the FULL pipeline (capture -> frame queue ->
ffmpeg -> HLS), then decodes the HLS output and dumps the per-frame brightness
and audio-RMS series, detected flash/beep events, and the measured offset.

    .venv/bin/python tools/calibration_harness.py                 # windowed (audio works)
    .venv/bin/python tools/calibration_harness.py --capture screenshot
    .venv/bin/python tools/calibration_harness.py --buffered      # match real-stream config
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from cast_tab import calibration as cal


def _summary(label: str, series: list[tuple[float, float]]) -> None:
    if not series:
        print(f"  {label}: (none)")
        return
    values = sorted(v for _, v in series)
    median = values[len(values) // 2]
    print(
        f"  {label}: n={len(series)} min={min(values):.4g} "
        f"median={median:.4g} max={max(values):.4g}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--capture", default="screencast",
        choices=["screencast", "screenshot", "playwright"],
    )
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--buffered", action="store_true", help="match buffered real-stream config")
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--codec", default="h264")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    args = ap.parse_args()

    work_dir = Path(tempfile.mkdtemp(prefix="cast-calib-harness-"))
    print(f"work dir: {work_dir}")
    print(
        f"running pipeline (capture={args.capture}, headless={args.headless}, "
        f"buffered={args.buffered}, {args.duration:.0f}s)..."
    )
    playlist = cal.run_calibration_pipeline(
        work_dir,
        capture_method=args.capture,
        width=args.width, height=args.height, fps=args.fps,
        jpeg_quality=75, codec=args.codec, buffered=args.buffered,
        headless=args.headless, duration_s=args.duration,
    )
    if playlist is None or not playlist.exists():
        print("PIPELINE FAILED: no playlist produced")
        return

    mkv = cal.remux_to_mkv(playlist)
    if mkv is None:
        print("REMUX FAILED")
        return

    video = cal.probe_video_brightness(mkv)
    audio = cal.probe_audio_rms(mkv)

    print("\n=== VIDEO (output brightness) ===")
    _summary("YAVG", video)
    vp = cal._classify(cal._detect_pulses(video, ratio=3.0))
    print(f"  flashes (t, step): {[(round(t, 2), k) for t, k in vp]}")

    print("\n=== AUDIO (output rms) ===")
    _summary("rms", audio)
    ap = cal._classify(cal._detect_pulses(audio, ratio=4.0))
    print(f"  beeps   (t, step): {[(round(t, 2), k) for t, k in ap]}")

    print("\n=== OFFSET (matched by cycle-step identity) ===")
    result = cal._identity_offset(vp, ap)
    if result is None:
        print("  could not match flash/beep pairs")
    else:
        offset_s, matched = result
        print(f"  audio leads video by {offset_s * 1000:+.0f}ms ({matched} pairs)")
    print(f"\n(inspect raw output: {mkv})")


if __name__ == "__main__":
    main()
