"""Measure end-to-end audio/video skew through the real capture pipeline.

Runs the actual TabScreencaster + AudioTee + HLSStreamer path (NO Chromecast)
against tools/clapboard.html, which flashes the screen white and beeps at the
same instant every 2s. We then find each flash (luma spike) and each beep
(silence->sound onset) in the recorded HLS and report the offset between them.

    offset = beep_time - flash_time
      offset < 0  -> audio is AHEAD of video by |offset| (delay audio to fix)
      offset > 0  -> audio is BEHIND video

This isolates the skew that lives at the Chrome capture boundary — the thing
our pipeline stats can't see, because they time the pipeline, not the source.

Usage:
    .venv/bin/python tools/measure_source_skew.py [--seconds 30] [--offset-ms 0]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cast_tab.audio import (  # noqa: E402
    audiotee_available,
    stop_audio_capture,
    try_start_chrome_audio_capture,
)
from cast_tab.browser import TabScreencaster  # noqa: E402
from cast_tab.streamer import HLSStreamer  # noqa: E402

CLAPBOARD = ROOT / "tools" / "clapboard.html"
WORK_DIR = Path("/tmp/cast-skew-measure")


def _run_ffmpeg_filter(input_path: Path, args: list[str]) -> str:
    """Run ffmpeg with a null output and return combined stderr (where the
    metadata/silencedetect prints land)."""
    cmd = ["ffmpeg", "-hide_banner", "-i", str(input_path), *args, "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.stderr


def find_flashes(input_path: Path) -> list[float]:
    """Frame PTS (s) where average luma jumps high — the white flashes."""
    stderr = _run_ffmpeg_filter(
        input_path,
        [
            "-vf",
            "signalstats,metadata=print:key=lavfi.signalstats.YAVG",
        ],
    )
    pts: float | None = None
    samples: list[tuple[float, float]] = []
    for line in stderr.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            pts = float(m.group(1))
            continue
        m = re.search(r"signalstats\.YAVG=([0-9.]+)", line)
        if m and pts is not None:
            samples.append((pts, float(m.group(1))))

    if not samples:
        return []
    # Flash = luma well above the running baseline. Use a high absolute gate
    # (white screen YAVG ~235) plus a rising edge so a sustained bright scene
    # doesn't register every frame.
    flashes: list[float] = []
    prev_bright = False
    for t, yavg in samples:
        bright = yavg > 160
        if bright and not prev_bright:
            flashes.append(t)
        prev_bright = bright
    return flashes


def find_beeps(input_path: Path) -> list[float]:
    """Onset PTS (s) of each beep, via silencedetect's silence_end marks."""
    stderr = _run_ffmpeg_filter(
        input_path,
        ["-af", "silencedetect=noise=-30dB:d=0.05"],
    )
    beeps: list[float] = []
    for line in stderr.splitlines():
        m = re.search(r"silence_end:\s*([0-9.]+)", line)
        if m:
            beeps.append(float(m.group(1)))
    return beeps


def match_offsets(
    flashes: list[float], beeps: list[float], *, max_pair_s: float = 1.4
) -> list[float]:
    """Signed offset (beep - flash) for each paired pulse.

    Nearest-time matching aliases badly when the true skew approaches half the
    pulse period (a flash is then equidistant from the beep before and after).
    We instead phase-align the two periodic trains: try every flash/beep index
    shift, pick the shift whose paired events are most consistent (lowest offset
    variance), and report those pairs. max_pair_s rejects pairs further apart
    than a real skew could be, so leftover startup detections don't pollute it.
    """
    if not flashes or not beeps:
        return []

    best: list[float] | None = None
    best_var = float("inf")
    # Shift beeps relative to flashes by k pulses in either direction.
    for k in range(-(len(beeps) - 1), len(flashes)):
        pairs: list[float] = []
        for i, f in enumerate(flashes):
            j = i + k
            if 0 <= j < len(beeps):
                d = beeps[j] - f
                if abs(d) <= max_pair_s:
                    pairs.append(d)
        if len(pairs) < 3:
            continue
        mean = sum(pairs) / len(pairs)
        var = sum((d - mean) ** 2 for d in pairs) / len(pairs)
        if var < best_var:
            best_var = var
            best = pairs
    return best or []


def capture(seconds: float, offset_ms: int, page: Path) -> Path:
    if not audiotee_available():
        raise SystemExit("AudioTee not found — build it first (see install.sh).")

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    for old in WORK_DIR.glob("*"):
        old.unlink()

    url = page.resolve().as_uri()
    print(f"Capturing {url}")
    screencaster = TabScreencaster(
        url,
        width=1920,
        height=1080,
        fps=45,
        pace_fps=30,
        jpeg_quality=75,
        on_frame=lambda _f: None,
        headless=False,
        capture_audio=True,
    )
    streamer: HLSStreamer | None = None
    audio_capture = None
    try:
        screencaster.start()
        screencaster.wait_until_ready()
        screencaster.enable_capture()

        print("Attaching audio…")
        audio_capture = try_start_chrome_audio_capture(
            screencaster.user_data_dir,
            on_retry=screencaster.nudge_playback,
        )
        print(f"Audio attached (pids={audio_capture.pids}).")

        streamer = HLSStreamer(
            width=1920,
            height=1080,
            fps=30,
            buffered=True,
            audio_fd=audio_capture.read_fd,
            audio_format=audio_capture.audio_format,
            audio_offset_ms=offset_ms,
            work_dir=WORK_DIR,
        )
        screencaster.on_frame = streamer.publish_frame
        streamer.start()
        streamer.wait_until_ready()

        print(f"Recording {seconds:.0f}s of clapboard…")
        time.sleep(seconds)
    finally:
        screencaster.stop()
        if streamer is not None:
            streamer.stop()
        stop_audio_capture(audio_capture)

    segments = sorted(WORK_DIR.glob("seg*.ts"))
    if not segments:
        raise SystemExit("No HLS segments were produced.")
    combined = WORK_DIR / "all.ts"
    with combined.open("wb") as out:
        for seg in segments:
            out.write(seg.read_bytes())
    return combined


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument(
        "--offset-ms",
        type=int,
        default=0,
        help="Pass through to HLSStreamer --audio-offset-ms (default 0 to "
        "measure the raw skew).",
    )
    parser.add_argument(
        "--analyze",
        type=Path,
        default=None,
        help="Skip capture and analyze an existing .ts file.",
    )
    parser.add_argument(
        "--page",
        type=Path,
        default=CLAPBOARD,
        help="Page to cast (default: DOM clapboard; use tools/clapboard_video.html "
        "for the <video>-element path that matches YouTube).",
    )
    args = parser.parse_args()

    combined = args.analyze or capture(args.seconds, args.offset_ms, args.page)
    print(f"\nAnalyzing {combined} …")
    flashes = find_flashes(combined)
    beeps = find_beeps(combined)
    print(f"Detected {len(flashes)} flashes, {len(beeps)} beeps.")
    if flashes:
        print("  flash times:", ", ".join(f"{t:.3f}" for t in flashes))
    if beeps:
        print("  beep times: ", ", ".join(f"{t:.3f}" for t in beeps))

    offsets = match_offsets(flashes, beeps)
    if not offsets:
        print("\nCould not pair any flash/beep events — check detection gates.")
        return 1

    ms = [o * 1000 for o in offsets]
    med = median(ms)
    print("\nPer-pulse offset (beep - flash), ms:")
    print("  " + ", ".join(f"{x:+.0f}" for x in ms))
    print(f"\nmedian offset: {med:+.0f} ms   "
          f"(spread {max(ms) - min(ms):.0f} ms over {len(ms)} pulses)")
    if med < -40:
        print(f"=> audio is AHEAD of video by ~{abs(med):.0f} ms "
              f"(use --audio-offset-ms {round(abs(med) / 10) * 10}).")
    elif med > 40:
        print(f"=> audio is BEHIND video by ~{med:.0f} ms.")
    else:
        print("=> audio and video are within ~40 ms (effectively synced).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
