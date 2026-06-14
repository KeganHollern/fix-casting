"""CLI entry point: cast <url>"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from cast_tab.audio import (
    AudioCapture,
    AudioCaptureError,
    audiotee_available,
    install_hint,
    try_start_chrome_audio_capture,
    stop_audio_capture,
)
from cast_tab.browser import TabScreencaster
from cast_tab.caster import TabCaster
from cast_tab.devices import discover_devices, select_device
from cast_tab.streamer import (
    HLSStreamer,
    codec_label,
    default_fps_for_resolution,
    default_jpeg_quality,
    resolve_codec,
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
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="Disable tab audio capture (video only)",
    )
    parser.add_argument(
        "--codec",
        choices=("auto", "h264", "hevc", "av1"),
        default="auto",
        help=(
            "Video codec (default: auto = HEVC if available). "
            "HEVC gives better quality per bitrate; AV1 is experimental."
        ),
    )
    parser.add_argument(
        "--buffered",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Buffer ~45s on the TV for higher quality and smoother playback "
            "(default: on). Use --no-buffered for lower latency."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    print("Searching for Chromecast devices...")
    devices = discover_devices(timeout=args.discovery_timeout)
    device = select_device(devices)

    codec = resolve_codec(args.codec)
    fps = args.fps or default_fps_for_resolution(
        args.width, args.height, buffered=args.buffered
    )
    jpeg_quality = default_jpeg_quality(args.width, args.height)
    capture_audio = not args.no_audio

    screencaster = TabScreencaster(
        args.url,
        width=args.width,
        height=args.height,
        fps=fps,
        jpeg_quality=jpeg_quality,
        on_frame=lambda _frame: None,
        headless=args.headless,
        capture_audio=capture_audio,
    )
    streamer: HLSStreamer | None = None
    audio_capture: AudioCapture | None = None
    caster = TabCaster(device)

    shutting_down = False

    def shutdown(_signum=None, _frame=None) -> None:
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        print("\nStopping cast...")
        screencaster.stop()
        if streamer is not None:
            streamer.stop()
        stop_audio_capture(audio_capture)
        caster.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        screencaster.start()
        screencaster.wait_until_ready()
        screencaster.enable_capture()

        if capture_audio:
            if not audiotee_available():
                print(f"Audio unavailable: AudioTee not found.\n{install_hint()}")
                capture_audio = False
            else:
                print("Waiting for cast browser audio...")
                try:
                    audio_capture = try_start_chrome_audio_capture(
                        screencaster.user_data_dir,
                        on_retry=screencaster.nudge_playback,
                    )
                    print(
                        "Capturing audio from cast browser only "
                        f"(PIDs: {', '.join(str(pid) for pid in audio_capture.pids)})."
                    )
                    print("Other Mac apps keep their normal audio output.")
                except AudioCaptureError as exc:
                    print(f"Audio unavailable: {exc}")
                    print(install_hint())
                    capture_audio = False

        streamer = HLSStreamer(
            width=args.width,
            height=args.height,
            fps=fps,
            codec=codec,
            buffered=args.buffered,
            audio_fd=audio_capture.read_fd if audio_capture else None,
        )
        screencaster.on_frame = streamer.publish_frame

        audio_mode = "with tab audio" if capture_audio else "video only"
        latency_mode = "buffered (~45s TV delay)" if args.buffered else "low-latency"
        print(
            f"Streaming at {args.width}x{args.height} {fps} fps "
            f"(jpeg q={jpeg_quality}) {audio_mode} using {codec_label(codec)}, "
            f"{latency_mode}."
        )
        if codec == "av1":
            print("AV1 uses software encoding and may not play on older Chromecasts.")
        elif codec == "hevc":
            print("If the TV shows an error, retry with --codec h264.")

        streamer.start()
        streamer.wait_until_ready()

        caster.connect()
        caster.play_hls(streamer.playlist_url)

        print("Casting. Press Ctrl+C to stop.")
        print(f"Source page: {args.url}")
        if not args.headless:
            print("A browser window is rendering the page locally.")
        if capture_audio:
            print("Cast browser audio plays on your TV only; other Mac audio is unchanged.")

        while not shutting_down:
            time.sleep(1)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        shutdown()
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())