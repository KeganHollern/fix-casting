"""Isolate whether OUR ffmpeg/HLS path induces the audio/video skew.

The clapboard clip (tools/clapboard_clip.mp4) has its flash and beep encoded
perfectly in sync (+0ms). This harness pushes that clip through the REAL
production path — the same HLSStreamer, the same image2pipe MJPEG video pipe and
raw-PCM audio fd, the same sampler pacing and HLS args — but with NO Chrome and
NO AudioTee in the loop. Video frames are published at a paced 30fps; audio PCM
is written to the fd at real-time pace. Both start together, so the source stays
perfectly synced going in.

If the output HLS shows the flash and beep still aligned, our ffmpeg/HLS muxing
is clean and the casting skew lives in Chrome's <video> A/V. If the output shows
a ~700ms offset, our pipe-feeding/mux induces it and it's fixable in our code.

    .venv/bin/python tools/test_pipeline_skew.py [--seconds 30]
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cast_tab.audio import DEFAULT_AUDIO_FORMAT  # noqa: E402
from cast_tab.stats import PipelineStats  # noqa: E402
from cast_tab.streamer import HLSStreamer  # noqa: E402

CLIP = ROOT / "tools" / "clapboard_clip.mp4"
WORK_DIR = Path("/tmp/cast-pipeline-skew")

# Reuse the flash/beep analysis from the source-skew tool.
_spec = importlib.util.spec_from_file_location(
    "msk", ROOT / "tools" / "measure_source_skew.py"
)
msk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(msk)


def load_frames(clip: Path) -> list[bytes]:
    """Decode the clip to individual JPEG frames (what the screencast emits)."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", str(clip),
                "-q:v", "3",
                str(Path(tmp) / "f%05d.jpg"),
            ],
            check=True,
        )
        files = sorted(Path(tmp).glob("f*.jpg"))
        return [f.read_bytes() for f in files]


def load_pcm(clip: Path) -> bytes:
    """Decode the clip's audio to the exact raw PCM AudioTee would emit."""
    fmt = DEFAULT_AUDIO_FORMAT
    out = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(clip),
            "-f", fmt.ffmpeg_format,
            "-ar", str(fmt.sample_rate),
            "-ac", str(fmt.channels),
            "-",
        ],
        check=True,
        capture_output=True,
    )
    return out.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--clip", type=Path, default=CLIP)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument(
        "--buffered", action=argparse.BooleanOptionalAction, default=True,
        help="HLSStreamer buffered mode (hls_time 4 vs 1).",
    )
    parser.add_argument(
        "--audio-period", type=float, default=0.1,
        help="Seconds per audio write (default 0.1 = smooth; larger = bursty, "
        "to test whether jittery audio delivery reproduces the stall).",
    )
    parser.add_argument(
        "--inject-stall", type=float, default=0.0,
        help="Freeze ffmpeg this many seconds mid-run (SIGSTOP) to test that the "
        "frame queue absorbs a stall without dropping frames.",
    )
    args = parser.parse_args()

    clip = args.clip
    if not clip.exists():
        raise SystemExit(f"Missing {clip} — generate the clapboard clip first.")

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    for old in WORK_DIR.glob("*"):
        old.unlink()

    print("Decoding clip into frames + PCM…")
    frames = load_frames(clip)
    pcm = load_pcm(clip)
    fmt = DEFAULT_AUDIO_FORMAT
    fps = 30
    print(f"  {len(frames)} frames, {len(pcm)} PCM bytes "
          f"({len(pcm) / fmt.bytes_per_second:.1f}s audio)")

    stats = PipelineStats(target_fps=float(fps))
    stats.enable_timeseries()
    audio_r, audio_w = os.pipe()
    streamer = HLSStreamer(
        width=args.width,
        height=args.height,
        fps=fps,
        buffered=args.buffered,
        audio_fd=audio_r,
        audio_format=fmt,
        audio_offset_ms=0,
        work_dir=WORK_DIR,
        stats=stats,
    )

    stop = threading.Event()

    def pump_audio() -> None:
        # Write PCM at real-time pace. Default 100ms chunks (metronomic, like an
        # ideal feed). --audio-period N writes one N-second chunk every N
        # seconds instead, to test whether a bursty/jittery audio feed (closer
        # to how AudioTee may deliver) reproduces the production stall.
        period = args.audio_period
        chunk = int(fmt.bytes_per_second * period)
        pos = 0
        next_t = time.monotonic()
        try:
            while not stop.is_set():
                if pos >= len(pcm):
                    pos = 0
                piece = pcm[pos:pos + chunk]
                pos += chunk
                try:
                    os.write(audio_w, piece)
                except (BrokenPipeError, OSError):
                    break
                next_t += period
                delay = next_t - time.monotonic()
                if delay > 0:
                    stop.wait(delay)
        finally:
            try:
                os.close(audio_w)
            except OSError:
                pass

    def pump_video() -> None:
        # Publish one frame per 1/30s of wall-clock, looping the clip if needed.
        period = 1.0 / fps
        next_t = time.monotonic()
        i = 0
        while not stop.is_set():
            streamer.publish_frame(frames[i % len(frames)])
            i += 1
            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                stop.wait(delay)

    # Start both feeders together so the source going in stays +0 synced.
    audio_thread = threading.Thread(target=pump_audio, daemon=True)
    video_thread = threading.Thread(target=pump_video, daemon=True)
    audio_thread.start()
    video_thread.start()

    # streamer.start() drains audio pre-roll, spawns ffmpeg, starts the sampler.
    streamer.start()
    streamer.wait_until_ready()

    print(f"Recording {args.seconds:.0f}s through the production HLS path…")
    if args.inject_stall > 0:
        # Freeze ffmpeg mid-run to simulate a multi-second encoder stall, then
        # resume — the exact condition that overflowed the old queue and dropped
        # frames. Verifies the deeper queue absorbs it with 0 drops.
        import signal as _signal
        time.sleep(args.seconds / 2)
        ff = streamer._ffmpeg
        if ff is not None:
            print(f"  >> injecting {args.inject_stall:.1f}s ffmpeg stall (SIGSTOP)…")
            os.kill(ff.pid, _signal.SIGSTOP)
            time.sleep(args.inject_stall)
            os.kill(ff.pid, _signal.SIGCONT)
            print("  >> ffmpeg resumed")
        time.sleep(args.seconds / 2)
    else:
        time.sleep(args.seconds)

    stop.set()
    audio_thread.join(timeout=2)
    streamer.stop()

    print("\n--- queue behavior during this run ---")
    print(stats.format_timeseries())
    print(stats.format_report(args.seconds))
    print("--------------------------------------")
    try:
        os.close(audio_r)
    except OSError:
        pass

    segments = sorted(WORK_DIR.glob("seg*.ts"))
    if not segments:
        raise SystemExit("No HLS segments produced.")
    combined = WORK_DIR / "all.ts"
    with combined.open("wb") as out:
        for seg in segments:
            out.write(seg.read_bytes())

    print(f"\nAnalyzing {combined} …")
    flashes = msk.find_flashes(combined)
    beeps = msk.find_beeps(combined)
    print(f"Detected {len(flashes)} flashes, {len(beeps)} beeps.")
    offsets = msk.match_offsets(flashes, beeps)
    if not offsets:
        print("Could not pair flash/beep events.")
        return 1
    ms = [o * 1000 for o in offsets]
    from statistics import median
    med = median(ms)
    print("Per-pulse offset (beep - flash), ms:")
    print("  " + ", ".join(f"{x:+.0f}" for x in ms))
    print(f"\nmedian: {med:+.0f} ms  (spread {max(ms) - min(ms):.0f} ms)")
    if abs(med) <= 60:
        print("=> ffmpeg/HLS path is CLEAN. The casting skew is Chrome's "
              "<video> A/V, not our muxing.")
    else:
        sign = "ahead" if med < 0 else "behind"
        print(f"=> our ffmpeg/HLS path INDUCES ~{abs(med):.0f}ms (audio {sign}). "
              "Fixable in our code.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
