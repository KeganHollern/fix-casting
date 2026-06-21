"""Measure end-to-end audio/video skew through the real capture pipeline.

Runs the actual TabScreencaster + AudioTee + HLSStreamer path (NO Chromecast)
against tools/clapboard_av_page.html, which plays a clip whose white flash and
1kHz beep are encoded together every 8s. We then find each flash (luma spike)
and each beep (silence->sound onset) in the recorded HLS and report the offset
between them.

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
import shutil
import subprocess
import sys
import threading
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
from cast_tab.stats import PipelineStats  # noqa: E402
from cast_tab.streamer import HLSStreamer  # noqa: E402

CLAPBOARD = ROOT / "tools" / "clapboard_av_page.html"
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
    flashes: list[float], beeps: list[float], *, max_pair_s: float = 3.9
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


def capture(seconds: float, offset_ms: int, page: Path, no_audio: bool = False) -> Path:
    if not no_audio and not audiotee_available():
        raise SystemExit("AudioTee not found — build it first (see install.sh).")

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    for old in WORK_DIR.glob("*"):
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
        else:
            old.unlink(missing_ok=True)

    url = page.resolve().as_uri()
    print(f"Capturing {url}")
    stats = PipelineStats(target_fps=30.0)
    stats.enable_timeseries()
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
        stats=stats,
    )
    streamer: HLSStreamer | None = None
    audio_capture = None
    try:
        screencaster.start()
        screencaster.wait_until_ready()
        screencaster.enable_capture()

        if no_audio:
            print("VIDEO ONLY (no AudioTee) — isolating video-side backpressure.")
        else:
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
            audio_fd=audio_capture.read_fd if audio_capture else None,
            audio_format=audio_capture.audio_format if audio_capture else None,
            audio_offset_ms=offset_ms,
            work_dir=WORK_DIR,
            stats=stats,
        )
        screencaster.on_frame = streamer.publish_frame
        streamer.start()
        streamer.wait_until_ready()

        # Buffered HLS deletes old segments, so the live work dir only ever
        # holds the last ~48s. Archive every segment before it's deleted so we
        # can analyze the WHOLE run (and compare start-vs-end for drift).
        archive = WORK_DIR / "archive"
        archive.mkdir(exist_ok=True)
        for old in archive.glob("*.ts"):
            old.unlink()
        archived: set[str] = set()
        stop_archiver = threading.Event()

        def archiver() -> None:
            while not stop_archiver.is_set():
                for seg in sorted(WORK_DIR.glob("seg*.ts")):
                    if seg.name not in archived:
                        try:
                            shutil.copy2(seg, archive / seg.name)
                            archived.add(seg.name)
                        except OSError:
                            pass
                stop_archiver.wait(0.5)

        arch_thread = threading.Thread(target=archiver, daemon=True)
        arch_thread.start()

        print(f"Recording {seconds:.0f}s of clapboard…")
        time.sleep(seconds)
        stop_archiver.set()
        arch_thread.join(timeout=2)
        # Final sweep for anything created right at the end.
        for seg in sorted(WORK_DIR.glob("seg*.ts")):
            if seg.name not in archived:
                try:
                    shutil.copy2(seg, archive / seg.name)
                    archived.add(seg.name)
                except OSError:
                    pass
    finally:
        screencaster.stop()
        if streamer is not None:
            streamer.stop()
        stop_audio_capture(audio_capture)

    # Show how long video spent in each of our Python stages during this run,
    # so we can see whether the output skew is our pipeline or inside ffmpeg.
    print("\n--- pipeline stage latencies during this run ---")
    print(stats.format_timeseries())
    print(stats.format_report(seconds))
    print("------------------------------------------------")

    # Prefer the archive (full run); fall back to live segments if not present.
    archive = WORK_DIR / "archive"
    segments = sorted(archive.glob("seg*.ts")) or sorted(WORK_DIR.glob("seg*.ts"))
    if not segments:
        raise SystemExit("No HLS segments were produced.")
    print(f"Archived {len(segments)} segments (full run).")
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
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="Skip AudioTee (video only) to isolate whether audio mux causes the "
        "queue backpressure. No A/V offset is measured; read the queue table.",
    )
    args = parser.parse_args()

    combined = args.analyze or capture(
        args.seconds, args.offset_ms, args.page, no_audio=args.no_audio
    )
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

    # Drift vs baseline: compare the first half of the run to the second half.
    # A fixed baseline offset is harmless (dial it out once); a growing delta
    # means the A/V relationship is drifting over the run — the thing the
    # long-run goal must rule out.
    if len(ms) >= 6:
        h = len(ms) // 2
        first, last = median(ms[:h]), median(ms[h:])
        delta = last - first
        print(
            f"\ndrift check: first-half {first:+.0f}ms → second-half {last:+.0f}ms "
            f"(delta {delta:+.0f}ms across the run)"
        )
        if abs(delta) > 80:
            print("=> OFFSET IS DRIFTING over the run (not a fixed baseline).")
        else:
            print("=> offset is STABLE across the run — fixed baseline, no drift.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
