"""CLI entry point: cast <url>"""

from __future__ import annotations

import argparse
import json
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
from cast_tab.stats import PipelineStats
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
        help="Encode frame rate (default: 30 buffered, 23 at 1080p / 24 at 720p otherwise)",
    )
    parser.add_argument(
        "--capture-fps",
        type=int,
        default=None,
        help=(
            "Tab capture rate (default: ~1.5x the encode fps). Capturing faster "
            "than the encoder keeps a fresh frame ready each tick, reducing "
            "duplicate frames and motion judder on the TV."
        ),
    )
    parser.add_argument(
        "--discovery-timeout",
        type=float,
        default=5.0,
        help="Seconds to search for Chromecast devices (default: 5)",
    )
    parser.add_argument(
        "--capture",
        choices=("cdp", "playwright", "screencast"),
        default="cdp",
        help=(
            "Tab frame capture backend (default: cdp screenshot loop). "
            "Use screencast for CDP Page.startScreencast (paint-driven, can "
            "reach ~60fps) or playwright for the legacy screenshot path."
        ),
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=None,
        help=(
            "JPEG quality of captured frames (1-100; default: 75 at 1080p, "
            "80 below). Lower trades image quality for faster capture."
        ),
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
            "Video codec (default: auto = H.264). "
            "HEVC/AV1 may not play on older Chromecasts."
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
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print pipeline timing stats every 10s to diagnose lag",
    )
    parser.add_argument(
        "--stats-interval",
        type=float,
        default=10.0,
        help="Seconds between stats reports when --stats is set (default: 10)",
    )
    parser.add_argument(
        "--tv-poll-interval",
        type=float,
        default=2.0,
        help="Seconds between Chromecast status polls when --stats is set (default: 2)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    print("Searching for Chromecast devices...")
    devices = discover_devices(timeout=args.discovery_timeout)
    device = select_device(devices)

    codec = resolve_codec(args.codec)
    encode_fps = args.fps or default_fps_for_resolution(
        args.width, args.height, buffered=args.buffered
    )
    # Oversample capture so a fresh frame is ready at every encoder tick.
    capture_fps = args.capture_fps or max(encode_fps, round(encode_fps * 1.5))
    jpeg_quality = args.jpeg_quality or default_jpeg_quality(args.width, args.height)
    capture_audio = not args.no_audio
    stats = PipelineStats(target_fps=float(encode_fps)) if args.stats else None

    screencaster = TabScreencaster(
        args.url,
        width=args.width,
        height=args.height,
        fps=capture_fps,
        pace_fps=encode_fps,
        jpeg_quality=jpeg_quality,
        on_frame=lambda _frame: None,
        headless=args.headless,
        capture_audio=capture_audio,
        capture_method=args.capture,
        stats=stats,
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
                audio_attached = False

                def on_audio_stderr(line: str) -> None:
                    mtype = "log"
                    text = line
                    try:
                        parsed = json.loads(line)
                        mtype = str(parsed.get("message_type", "log"))
                        data = parsed.get("data") or {}
                        text = str(data.get("message", line))
                        context = data.get("context")
                        if context:
                            text += f" {context}"
                    except (ValueError, AttributeError):
                        pass
                    # Debug lines are high-volume (per-PID tap attempts); keep
                    # them out of the log but still surface info/warning/error.
                    if mtype == "debug":
                        return
                    # AudioTee probes PID candidates that do not tap on modern
                    # macOS (renderers). "failed to translate" only happens
                    # during that probing, so it is always search noise; a bare
                    # "failure" is suppressed only until a tap succeeds, so a
                    # mid-stream AudioTee death still surfaces.
                    low = text.strip().lower()
                    if "failed to translate" in low:
                        return
                    if not audio_attached and (
                        low in ("error: failure", "failure")
                        or low.startswith("starting audiotee")
                    ):
                        return
                    print(f"[audio:{mtype}] {text}", flush=True)
                    if stats is not None and (
                        mtype in ("error", "warning")
                        or any(
                            kw in text.lower()
                            for kw in (
                                "drop", "underrun", "overrun",
                                "glitch", "discontinu", "xrun",
                            )
                        )
                    ):
                        stats.record_audio_warning(text)

                try:
                    audio_capture = try_start_chrome_audio_capture(
                        screencaster.user_data_dir,
                        on_retry=screencaster.nudge_playback,
                        on_stderr=on_audio_stderr,
                    )
                    audio_attached = True
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
            fps=encode_fps,
            codec=codec,
            buffered=args.buffered,
            audio_fd=audio_capture.read_fd if audio_capture else None,
            stats=stats,
        )
        screencaster.on_frame = streamer.publish_frame

        audio_mode = "with tab audio" if capture_audio else "video only"
        latency_mode = "buffered (~45s TV delay)" if args.buffered else "low-latency"
        if args.capture == "screencast":
            capture_desc = "capture=screencast (paint-driven)"
        else:
            capture_desc = f"capture={args.capture} ~{capture_fps}fps"
        print(
            f"Streaming at {args.width}x{args.height} {encode_fps} fps "
            f"({capture_desc}, jpeg q={jpeg_quality}) {audio_mode} "
            f"using {codec_label(codec)}, {latency_mode}."
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
        if stats is not None:
            print(
                f"Stats enabled (every {args.stats_interval:.0f}s, "
                f"tv polls every {args.tv_poll_interval:.0f}s)."
            )

        next_stats_at = time.monotonic() + args.stats_interval
        next_tv_poll_at = time.monotonic()
        while not shutting_down:
            now = time.monotonic()
            if stats is not None and now >= next_tv_poll_at:
                for event in streamer.poll_hls_stats():
                    print(f"[stats] {event}", flush=True)
                tv = caster.poll_playback_stats()
                for event in stats.record_tv_poll(
                    state=tv.state,
                    position_s=tv.position_s,
                    idle_reason=tv.idle_reason,
                ):
                    print(f"[stats] {event}", flush=True)
                next_tv_poll_at = now + args.tv_poll_interval

            if stats is not None and now >= next_stats_at:
                print(stats.format_report(args.stats_interval), flush=True)
                next_stats_at = now + args.stats_interval
            time.sleep(0.25)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        shutdown()
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())