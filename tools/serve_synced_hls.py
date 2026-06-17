"""Serve the production HLS pipeline fed a KNOWN-SYNCED clip — judge it yourself.

No Chrome, no AudioTee. This pushes a clip whose white flash and 1 kHz beep are
encoded together every 3s (tools/clapboard_clip.mp4) through the EXACT
HLSStreamer path the caster uses — MJPEG image2pipe video, raw-PCM audio fd,
sampler pacing, the same HLS args — and serves the result over HTTP.

Open the printed stream.m3u8 URL in VLC (or a browser HLS player, or cast it).
If the white flash and the beep land at the same instant, ffmpeg/HLS preserved
A/V sync and adds no audio-ahead skew. If they're ~700ms apart, it doesn't.

Establish ground truth first by playing the source so you know what "synced"
looks/sounds like:

    ffplay tools/clapboard_clip.mp4        # or open it in VLC/QuickTime

Then run this and compare:

    .venv/bin/python tools/serve_synced_hls.py
    # open the printed URL in VLC: File > Open Network > paste the m3u8 URL

--buffered matches production exactly (but VLC waits ~45s before playing);
the default unbuffered mode starts in a couple seconds and has the SAME muxing,
so the A/V sync you see is identical — only the buffer depth differs.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cast_tab.audio import DEFAULT_AUDIO_FORMAT  # noqa: E402
from cast_tab.streamer import HLSStreamer, get_local_ip  # noqa: E402

DEFAULT_CLIP = ROOT / "tools" / "clapboard_clip.mp4"
WORK_DIR = Path("/tmp/cast-synced-hls")


def load_frames(clip: Path) -> list[bytes]:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", str(clip), "-q:v", "3", str(Path(tmp) / "f%05d.jpg"),
            ],
            check=True,
        )
        return [f.read_bytes() for f in sorted(Path(tmp).glob("f*.jpg"))]


def load_pcm(clip: Path) -> bytes:
    fmt = DEFAULT_AUDIO_FORMAT
    out = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(clip),
            "-f", fmt.ffmpeg_format, "-ar", str(fmt.sample_rate),
            "-ac", str(fmt.channels), "-",
        ],
        check=True, capture_output=True,
    )
    return out.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clip", type=Path, default=DEFAULT_CLIP)
    parser.add_argument(
        "--buffered", action=argparse.BooleanOptionalAction, default=False,
        help="Match production's ~45s buffer (default off for a fast VLC start; "
        "sync is identical either way).",
    )
    parser.add_argument(
        "--offset-ms", type=int, default=0,
        help="Apply --audio-offset-ms (default 0, so you see the RAW sync).",
    )
    args = parser.parse_args()

    if not args.clip.exists():
        raise SystemExit(f"Missing clip: {args.clip}")

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    for old in WORK_DIR.glob("*"):
        old.unlink()

    print("Decoding clip…")
    frames = load_frames(args.clip)
    pcm = load_pcm(args.clip)
    fmt = DEFAULT_AUDIO_FORMAT
    fps = 30
    print(f"  {len(frames)} frames, {len(pcm) / fmt.bytes_per_second:.1f}s audio")

    audio_r, audio_w = os.pipe()
    streamer = HLSStreamer(
        width=1280, height=720, fps=fps, buffered=args.buffered,
        audio_fd=audio_r, audio_format=fmt, audio_offset_ms=args.offset_ms,
        work_dir=WORK_DIR,
    )

    stop = threading.Event()

    def pump_audio() -> None:
        chunk = int(fmt.bytes_per_second * 0.1)
        pos = 0
        next_t = time.monotonic()
        try:
            while not stop.is_set():
                if pos >= len(pcm):
                    pos = 0
                try:
                    os.write(audio_w, pcm[pos:pos + chunk])
                except (BrokenPipeError, OSError):
                    break
                pos += chunk
                next_t += 0.1
                d = next_t - time.monotonic()
                if d > 0:
                    stop.wait(d)
        finally:
            try:
                os.close(audio_w)
            except OSError:
                pass

    def pump_video() -> None:
        period = 1.0 / fps
        next_t = time.monotonic()
        i = 0
        while not stop.is_set():
            streamer.publish_frame(frames[i % len(frames)])
            i += 1
            next_t += period
            d = next_t - time.monotonic()
            if d > 0:
                stop.wait(d)

    threading.Thread(target=pump_audio, daemon=True).start()
    threading.Thread(target=pump_video, daemon=True).start()

    streamer.start()
    streamer.wait_until_ready()

    host = get_local_ip()
    port = streamer._http_server.server_port if streamer._http_server else 0
    url = f"http://{host}:{port}/stream.m3u8"
    mode = "buffered (~45s delay)" if args.buffered else "unbuffered (fast start)"
    banner = (
        "\n" + "=" * 64 + "\n"
        "HLS is live — open this in VLC (File > Open Network) or a browser:\n"
        f"   {url}\n"
        f"   (also: http://localhost:{port}/stream.m3u8 on this machine)\n"
        f"Mode: {mode}.  Offset applied: {args.offset_ms}ms.\n"
        "Watch/listen: flash and beep should hit together every 3s.\n"
        "Ctrl+C to stop.\n"
        + "=" * 64 + "\n"
    )
    print(banner, flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        stop.set()
        streamer.stop()
        try:
            os.close(audio_r)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
