"""CLI entry point: cast <url>"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from cast_tab.caster import TabCaster
from cast_tab.devices import discover_devices, find_device, select_device
from cast_tab.encoder import tv_delay_s
from cast_tab.session import CastSession, SessionConfig
from cast_tab.stats import PipelineStats
from cast_tab.streamer import (
    DEFAULT_JPEG_QUALITY,
    codec_label,
    default_fps_for_resolution,
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
        "--jpeg-quality",
        type=int,
        default=None,
        metavar="Q",
        help=(
            "JPEG quality (1-100) for tab capture (default: 92). Higher = "
            "sharper but more CPU/bandwidth."
        ),
    )
    parser.add_argument(
        "--video-bitrate",
        type=float,
        default=None,
        metavar="MBPS",
        help=(
            "Override the H.264 target bitrate in Mbps (default: chosen by "
            "resolution, 15 at 1080p). Raise it with --stats to find how high "
            "your Chromecast's network sustains before it buffers."
        ),
    )
    parser.add_argument(
        "--device",
        metavar="NAME",
        default=None,
        help=(
            "Cast to the device with this name, skipping the interactive "
            "picker (case-insensitive; a unique substring works too)."
        ),
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
        "--adblock",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Block ads/trackers in the captured tab using uBlock Origin's "
            "network filter lists + Peter Lowe's ad-server list (default: on). "
            "Use --no-adblock to disable."
        ),
    )
    parser.add_argument(
        "--audio-offset-ms",
        type=int,
        default=0,
        help=(
            "Manual A/V trim in ms (default: 0). Positive delays audio (use if "
            "audio is ahead of video); negative advances it. Use to dial in "
            "lip-sync."
        ),
    )
    parser.add_argument(
        "--audio-drift-ppm",
        type=float,
        default=0.0,
        metavar="PPM",
        help=(
            "Correct slow audio clock drift in parts-per-million (default: 0). "
            "If audio drifts AHEAD of video over a long session, set this "
            "positive; ffmpeg constant-resamples audio to lock it to real time "
            "(smooth, inaudible). Measure your value with "
            "tools/measure_source_skew.py (prints 'clock error ≈ N ppm')."
        ),
    )
    parser.add_argument(
        "--buffered",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Buffer ~48s on the TV for higher quality and smoother playback "
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
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Show a live full-screen dashboard of all pipeline stats with a "
        "real-time audio-offset knob (instead of the scrolling --stats text).",
    )
    args = parser.parse_args(argv)
    if args.video_bitrate is not None and args.video_bitrate <= 0:
        parser.error("--video-bitrate must be greater than 0 (Mbps)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    print("Searching for Chromecast devices...")
    devices = discover_devices(timeout=args.discovery_timeout)
    if args.device:
        device = find_device(devices, args.device)
        print(f"Casting to {device.name}.")
    else:
        device = select_device(devices)

    encode_fps = args.fps or default_fps_for_resolution(
        args.width, args.height, buffered=args.buffered
    )
    jpeg_quality = (
        max(1, min(100, args.jpeg_quality))
        if args.jpeg_quality is not None
        else DEFAULT_JPEG_QUALITY
    )
    # The TUI is a live view of the same stats, so it needs them collected too.
    collect_stats = args.stats or args.tui
    stats = PipelineStats(target_fps=float(encode_fps)) if collect_stats else None

    adblock_patterns = None
    if args.adblock:
        from cast_tab.adblocking import build_block_patterns

        print("Loading ad-block filter lists...")
        adblock_patterns = build_block_patterns()

    session = CastSession(
        SessionConfig(
            url=args.url,
            width=args.width,
            height=args.height,
            fps=encode_fps,
            jpeg_quality=jpeg_quality,
            buffered=args.buffered,
            headless=args.headless,
            capture_audio=not args.no_audio,
            audio_offset_ms=args.audio_offset_ms,
            audio_drift_ppm=args.audio_drift_ppm,
            video_bitrate_mbps=args.video_bitrate,
            adblock_patterns=adblock_patterns,
        ),
        stats=stats,
    )
    caster = TabCaster(device)

    shutting_down = False
    started_at = time.monotonic()

    def _print_exit_summary() -> None:
        elapsed = int(time.monotonic() - started_at)
        hours, rest = divmod(elapsed, 3600)
        minutes, seconds = divmod(rest, 60)
        duration = (
            f"{hours}h{minutes:02d}m{seconds:02d}s" if hours
            else f"{minutes}m{seconds:02d}s" if minutes
            else f"{seconds}s"
        )
        parts = [f"cast ran {duration}"]
        if caster.reconnects:
            parts.append(f"{caster.reconnects} TV re-casts")
        if stats is not None:
            snap = stats.snapshot(1.0)
            if snap.dropped_total:
                parts.append(f"{snap.dropped_total} frames dropped (stutter)")
            if snap.restarts_total:
                parts.append(f"{snap.restarts_total} ffmpeg restarts")
        print(f"Summary: {', '.join(parts)}.")

    def shutdown() -> None:
        """Stop all components (idempotent). Exit codes are the caller's job:
        an embedded sys.exit(0) here would eat the error path's non-zero exit
        (SystemExit raised mid-handler preempts its `return 1`)."""
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        print("\nStopping cast...")
        session.stop()
        caster.stop()
        _print_exit_summary()

    def handle_signal(_signum=None, _frame=None) -> None:
        shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        session.start()

        audio_mode = "with tab audio" if session.audio_active else "video only"
        latency_mode = (
            f"buffered (~{tv_delay_s(buffered=True)}s TV delay)"
            if args.buffered
            else "low-latency"
        )
        bitrate_note = (
            f", {args.video_bitrate:g}M video bitrate"
            if args.video_bitrate is not None
            else ""
        )
        print(
            f"Streaming at {args.width}x{args.height} {encode_fps} fps "
            f"(capture=screencast (paint-driven), jpeg q={jpeg_quality}{bitrate_note}) "
            f"{audio_mode} using {codec_label()}, {latency_mode}."
        )

        caster.connect()
        caster.play_hls(session.playlist_url)

        if args.tui:
            # Full-screen dashboard owns the terminal and its own poll loop.
            from cast_tab.tui import run_tui

            assert stats is not None  # --tui always collects stats
            run_tui(
                stats=stats,
                streamer=session.streamer,
                caster=caster,
                initial_offset_ms=args.audio_offset_ms,
                tv_poll_interval=args.tv_poll_interval,
                playlist_url=session.playlist_url,
            )
            shutdown()
            return 0

        print("Casting. Press Ctrl+C to stop.")
        print(f"Source page: {args.url}")
        if not args.headless:
            print("A browser window is rendering the page locally.")
        if session.audio_active:
            print("Cast browser audio plays on your TV only; other Mac audio is unchanged.")
        if stats is not None:
            print(
                f"Stats enabled (every {args.stats_interval:.0f}s, "
                f"tv polls every {args.tv_poll_interval:.0f}s)."
            )

        streamer = session.streamer
        assert streamer is not None  # session.start() succeeded above
        next_stats_at = time.monotonic() + args.stats_interval
        next_tv_poll_at = time.monotonic()
        # Watchdog: re-cast if the TV stops playing (app killed, stream error).
        # Grace period so startup buffering never counts as idle.
        next_ensure_at = time.monotonic() + 15.0
        while not shutting_down:
            now = time.monotonic()
            if now >= next_ensure_at:
                event = caster.ensure_playing()
                if event:
                    print(f"[recover] {event}", flush=True)
                next_ensure_at = now + 5.0
            if stats is not None and now >= next_tv_poll_at:
                streamer.poll_audio_backlog()
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