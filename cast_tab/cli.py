"""CLI entry point: cast <url>"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from cast_tab.browser import TabScreencaster
from cast_tab.caster import TabCaster
from cast_tab.devices import discover_devices, select_device
from cast_tab.streamer import (
    HLSStreamer,
    _ffmpeg_supports_encoder,
    default_fps_for_resolution,
    default_jpeg_quality,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cast",
        description=(
            "Cast a browser tab to Chromecast. Renders the full page and mirrors "
            "it to your TV — does not use Chrome's dominant-video detection."
        ),
    )
    parser.add_argument("url", help="URL to open and mirror")
    parser.add_argument(
        "--width",
        type=int,
        default=1920,
        help="Viewport width (default: 1920)",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=1080,
        help="Viewport height (default: 1080)",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=None,
        help="Encode frame rate (default: 23 at 1080p, 24 at 720p)",
    )
    parser.add_argument(
        "--discovery-timeout",
        type=float,
        default=5.0,
        help="Seconds to search for Chromecast devices (default: 5)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser without a visible window (may break some players)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    print("Searching for Chromecast devices...")
    devices = discover_devices(timeout=args.discovery_timeout)
    device = select_device(devices)

    fps = args.fps or default_fps_for_resolution(args.width, args.height)
    jpeg_quality = default_jpeg_quality(args.width, args.height)

    streamer = HLSStreamer(
        width=args.width,
        height=args.height,
        fps=fps,
    )
    screencaster = TabScreencaster(
        args.url,
        width=args.width,
        height=args.height,
        fps=fps,
        jpeg_quality=jpeg_quality,
        on_frame=streamer.publish_frame,
        headless=args.headless,
    )
    encoder = (
        "hardware (VideoToolbox)"
        if _ffmpeg_supports_encoder("h264_videotoolbox")
        else "software (x264)"
    )
    print(
        f"Streaming at {args.width}x{args.height} {fps} fps "
        f"(jpeg q={jpeg_quality}) using {encoder}."
    )
    caster = TabCaster(device)

    shutting_down = False

    def shutdown(_signum=None, _frame=None) -> None:
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        print("\nStopping cast...")
        screencaster.stop()
        streamer.stop()
        caster.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        streamer.start()
        screencaster.start()
        streamer.wait_until_ready()

        caster.connect()
        caster.play_hls(streamer.playlist_url)

        print("Casting. Press Ctrl+C to stop.")
        print(f"Source page: {args.url}")
        if not args.headless:
            print("A browser window is rendering the page locally.")

        while not shutting_down:
            time.sleep(1)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        shutdown()
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())