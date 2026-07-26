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
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cast_tab.audio import DEFAULT_AUDIO_FORMAT  # noqa: E402
from cast_tab.server import (  # noqa: E402
    HLSDiscontinuitySequenceNormalizer,
    parse_hls_segment_epoch,
)
from cast_tab.stats import PipelineStats  # noqa: E402
from cast_tab.streamer import HLSStreamer  # noqa: E402

CLIP = ROOT / "tools" / "clapboard_clip.mp4"

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


def nearest_offsets(
    flashes: list[float],
    beeps: list[float],
    *,
    max_pair_s: float = 0.5,
) -> list[float]:
    """Pair only unambiguously near events for short per-generation clips."""
    remaining = list(beeps)
    offsets: list[float] = []
    for flash in flashes:
        if not remaining:
            break
        index = min(range(len(remaining)), key=lambda i: abs(remaining[i] - flash))
        offset = remaining[index] - flash
        if abs(offset) <= max_pair_s:
            offsets.append(offset)
            remaining.pop(index)
    return offsets


def combine_segments(segments: list[Path], output: Path) -> None:
    with output.open("wb") as destination:
        for segment in segments:
            destination.write(segment.read_bytes())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--clip", type=Path, default=CLIP)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help=(
            "Directory for HLS artifacts (default: a unique temporary directory). "
            "A unique default keeps simultaneous stress runs isolated."
        ),
    )
    parser.add_argument(
        "--buffered", action=argparse.BooleanOptionalAction, default=True,
        help="HLSStreamer production mode (2s segments vs 1s low-latency).",
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

    work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="cast-pipeline-skew-"))
    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"HLS artifacts: {work_dir}")

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
        work_dir=work_dir,
        stats=stats,
    )

    stop = threading.Event()
    feed_started_at = time.monotonic()
    os.set_blocking(audio_w, False)

    def pump_audio() -> None:
        # Write PCM at real-time pace. Default 100ms chunks (metronomic, like an
        # ideal feed). --audio-period N writes one N-second chunk every N
        # seconds instead, to test whether a bursty/jittery audio feed (closer
        # to how AudioTee may deliver) reproduces the production stall.
        period = args.audio_period
        chunk = int(fmt.bytes_per_second * period)
        tick = 0
        try:
            while not stop.is_set():
                target = feed_started_at + tick * period
                delay = target - time.monotonic()
                if delay > 0 and stop.wait(delay):
                    break

                # Derive source position from wall time, not the number of
                # successful writes. If ffmpeg stops draining its pipe, old
                # audio is dropped and the next accepted chunk is current,
                # preserving the harness's asserted source A/V relationship.
                wall_tick = max(0, int((time.monotonic() - feed_started_at) / period))
                tick = max(tick, wall_tick)
                pos = (tick * chunk) % len(pcm)
                end = pos + chunk
                piece = (
                    pcm[pos:end]
                    if end <= len(pcm)
                    else pcm[pos:] + pcm[: end - len(pcm)]
                )
                try:
                    os.write(audio_w, piece)
                except BlockingIOError:
                    pass
                except (BrokenPipeError, OSError):
                    break
                tick += 1
        finally:
            try:
                os.close(audio_w)
            except OSError:
                pass

    def pump_video() -> None:
        # Publish one frame per 1/30s of wall-clock, looping the clip if needed.
        period = 1.0 / fps
        tick = 0
        while not stop.is_set():
            target = feed_started_at + tick * period
            delay = target - time.monotonic()
            if delay > 0 and stop.wait(delay):
                break
            wall_tick = max(0, int((time.monotonic() - feed_started_at) / period))
            tick = max(tick, wall_tick)
            streamer.publish_frame(frames[tick % len(frames)])
            tick += 1

    # Start both feeders together so the source going in stays +0 synced.
    audio_thread = threading.Thread(target=pump_audio, daemon=True)
    video_thread = threading.Thread(target=pump_video, daemon=True)
    audio_thread.start()
    video_thread.start()

    # streamer.start() drains audio pre-roll, spawns ffmpeg, starts the sampler.
    streamer.start()
    streamer.wait_until_ready()

    # The production playlist is intentionally short, so archive each completed
    # segment named by an atomically-published playlist before HLS rotation
    # deletes it. Analysis then covers the whole run without changing any
    # production HLS settings.
    archive_dir = work_dir / "archive"
    archive_dir.mkdir(exist_ok=True)
    archived: set[str] = set()
    sequence_by_uri: dict[str, int] = {}
    sequence_errors: set[str] = set()
    archive_stop = threading.Event()

    def archive_once() -> None:
        playlist = work_dir / "stream.m3u8"
        try:
            lines = playlist.read_text().splitlines()
        except OSError:
            return
        media_prefix = "#EXT-X-MEDIA-SEQUENCE:"
        media_lines = [line for line in lines if line.startswith(media_prefix)]
        if len(media_lines) == 1:
            try:
                media_sequence = int(media_lines[0][len(media_prefix) :])
            except ValueError:
                media_sequence = -1
            uris = [line for line in lines if line and not line.startswith("#")]
            for index, name in enumerate(uris):
                sequence = media_sequence + index
                previous = sequence_by_uri.setdefault(name, sequence)
                if previous != sequence:
                    sequence_errors.add(
                        f"{name}: media sequence changed {previous} -> {sequence}"
                    )
        for name in lines:
            if not name or name.startswith("#") or name in archived:
                continue
            source = work_dir / name
            if not source.is_file():
                continue
            shutil.copy2(source, archive_dir / name)
            archived.add(name)

    def archive_segments() -> None:
        while not archive_stop.wait(0.05):
            archive_once()

    archive_thread = threading.Thread(
        target=archive_segments,
        name="hls-test-archiver",
        daemon=True,
    )
    archive_thread.start()

    def monitor_for(duration_s: float) -> None:
        """Exercise the same periodic health check the production CLI uses."""
        deadline = time.monotonic() + duration_s
        while not stop.is_set() and time.monotonic() < deadline:
            streamer.raise_if_failed()
            stop.wait(min(0.05, max(0.0, deadline - time.monotonic())))

    print(f"Recording {args.seconds:.0f}s through the production HLS path…")
    if args.inject_stall > 0:
        # Freeze ffmpeg mid-run to simulate a multi-second encoder stall, then
        # resume — the exact condition that overflowed the old queue and
        # permanently shortened its CFR video timeline. The current streamer
        # must replace that generation and jointly re-anchor audio and video.
        import signal as _signal
        monitor_for(args.seconds / 2)
        ff = streamer._ffmpeg
        if ff is not None:
            print(f"  >> injecting {args.inject_stall:.1f}s ffmpeg stall (SIGSTOP)…")
            os.kill(ff.pid, _signal.SIGSTOP)
            monitor_for(args.inject_stall)
            # The health check should replace a sufficiently long-stalled
            # generation. Resume only when this exact process still exists.
            if ff.poll() is None:
                try:
                    os.kill(ff.pid, _signal.SIGCONT)
                    print("  >> original ffmpeg resumed")
                except ProcessLookupError:
                    pass
            else:
                print("  >> stalled ffmpeg was replaced")
        monitor_for(args.seconds / 2)
    else:
        monitor_for(args.seconds)

    stop.set()
    audio_thread.join(timeout=2)
    streamer.stop()
    archive_stop.set()
    archive_thread.join(timeout=1)
    archive_once()

    print("\n--- queue behavior during this run ---")
    print(stats.format_timeseries())
    print(stats.format_report(args.seconds))
    print("--------------------------------------")
    try:
        os.close(audio_r)
    except OSError:
        pass

    playlist = work_dir / "stream.m3u8"
    playlist_raw = playlist.read_bytes() if playlist.exists() else b""
    playlist_text = playlist_raw.decode(errors="replace")
    discontinuities = playlist_text.count("#EXT-X-DISCONTINUITY")
    normalized = HLSDiscontinuitySequenceNormalizer()(playlist_raw).decode(
        errors="replace"
    )
    sequence_match = re.search(
        r"#EXT-X-DISCONTINUITY-SEQUENCE:(\d+)", normalized
    )
    discontinuity_sequence = int(sequence_match.group(1)) if sequence_match else 0
    timeline_boundaries = discontinuity_sequence + discontinuities
    print(f"HLS discontinuities in retained playlist: {discontinuities}")
    print(f"HLS discontinuity sequence: {discontinuity_sequence}")
    print(f"HLS restart boundaries represented: {timeline_boundaries}")
    if sequence_errors:
        print("HLS media-sequence identity: BROKEN")
        for error in sorted(sequence_errors):
            print(f"  {error}")
        return 1
    print("HLS media-sequence identity: stable")

    segments = sorted(archive_dir.glob("seg*.ts"))
    if not segments:
        segments = sorted(work_dir.glob("seg*.ts"))
    if not segments:
        raise SystemExit("No HLS segments produced.")
    combined = work_dir / "all.ts"
    combine_segments(segments, combined)

    print(f"\nAnalyzing {combined} …")
    flashes = msk.find_flashes(combined)
    beeps = msk.find_beeps(combined)
    print(f"Detected {len(flashes)} flashes, {len(beeps)} beeps.")
    offsets = msk.match_offsets(flashes, beeps)
    if not offsets and args.inject_stall > 0:
        # A short pre/post-stall generation can contain only one useful pulse,
        # while the general phase matcher intentionally requires three. Near
        # pairing is unambiguous at the sub-frame offsets this regression tests.
        offsets = nearest_offsets(flashes, beeps)
    if not offsets:
        print("Could not pair flash/beep events.")
        return 1
    ms = [o * 1000 for o in offsets]
    from statistics import median
    med = median(ms)
    print("Per-pulse offset (beep - flash), ms:")
    print("  " + ", ".join(f"{x:+.0f}" for x in ms))
    print(f"\nmedian: {med:+.0f} ms  (spread {max(ms) - min(ms):.0f} ms)")
    if abs(med) <= 100:
        print("=> ffmpeg/HLS path is CLEAN. The casting skew is Chrome's "
              "<video> A/V, not our muxing.")
    else:
        sign = "ahead" if med < 0 else "behind"
        print(f"=> our ffmpeg/HLS path INDUCES ~{abs(med):.0f}ms (audio {sign}). "
              "Fixable in our code.")

    if args.inject_stall > 0:
        epochs: dict[int, list[Path]] = {}
        for segment in segments:
            epoch = parse_hls_segment_epoch(segment.name)
            if epoch is not None:
                epochs.setdefault(epoch, []).append(segment)
        if len(epochs) < 2:
            print("No post-restart HLS timeline epoch was archived.")
            return 1
        post_epoch = max(epochs)
        post_restart = work_dir / f"post-restart-e{post_epoch}.ts"
        combine_segments(sorted(epochs[post_epoch]), post_restart)
        post_flashes = msk.find_flashes(post_restart)
        post_beeps = msk.find_beeps(post_restart)
        post_offsets = nearest_offsets(post_flashes, post_beeps)
        if not post_offsets:
            print("Could not pair a post-restart flash/beep event.")
            return 1
        post_ms = [offset * 1000 for offset in post_offsets]
        post_median = median(post_ms)
        print(
            f"post-restart median: {post_median:+.0f} ms "
            f"({len(post_ms)} pulse{'s' if len(post_ms) != 1 else ''})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
