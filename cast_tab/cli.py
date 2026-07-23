"""CLI entry point: cast <url>"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import math
import signal
import sys
import time
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

from cast_tab.audiotee_provenance import verify_installed_audiotee
from cast_tab.caster import TabCaster
from cast_tab.devices import discover_devices, find_device, select_device
from cast_tab.encoder import estimated_hls_holdback_s, hls_playlist_retention_s
from cast_tab.paths import AUDIOTEE_INSTALL_PATH, INSTALL_PROVENANCE_PATH
from cast_tab.session import CastSession, SessionConfig
from cast_tab.stats import PipelineStats
from cast_tab.streamer import (
    DEFAULT_JPEG_QUALITY,
    MAX_AUTO_AV_OFFSET_MS,
    codec_label,
    default_fps_for_resolution,
)

MAX_VIDEO_BITRATE_MBPS = 1_000.0
MAX_ABS_AUDIO_DRIFT_PPM = 100_000.0
INSTALL_RECEIPT_FORMAT = "1"


class _ShutdownRequested(Exception):
    """Internal control flow after an OS signal requested a clean shutdown."""


def _package_fingerprint(package_dir: Path) -> str:
    """Hash the relative paths and contents of an installed/source package tree."""
    source_files = sorted(
        (path for path in package_dir.rglob("*.py") if path.is_file()),
        key=lambda path: path.relative_to(package_dir).as_posix(),
    )
    if not source_files:
        raise ValueError(f"no Python sources found under {package_dir}")

    digest = hashlib.sha256()
    for source_file in source_files:
        relative_path = source_file.relative_to(package_dir).as_posix().encode("utf-8")
        digest.update(len(relative_path).to_bytes(4, "big"))
        digest.update(relative_path)
        digest.update(hashlib.sha256(source_file.read_bytes()).digest())
    return digest.hexdigest()


def _verified_install_provenance(package_dir: Path) -> str:
    """Return receipt provenance only when it describes this package tree."""
    try:
        receipt_lines = INSTALL_PROVENANCE_PATH.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return "revision unknown"

    fields: dict[str, str] = {}
    for line in receipt_lines:
        key, separator, value = line.partition("=")
        if not separator or not key or key in fields:
            return "revision unknown (unverified install receipt)"
        fields[key] = value

    revision = fields.get("revision", "")
    expected_fingerprint = fields.get("package_fingerprint", "")
    if (
        fields.get("format") != INSTALL_RECEIPT_FORMAT
        or not revision
        or len(expected_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in expected_fingerprint)
    ):
        return "revision unknown (unverified install receipt)"
    if not revision.endswith(f"+source.{expected_fingerprint[:16]}"):
        return "revision unknown (install receipt is internally inconsistent)"

    try:
        actual_fingerprint = _package_fingerprint(package_dir)
    except (OSError, UnicodeError, ValueError):
        return "revision unknown (could not verify installed source)"
    if not hmac.compare_digest(actual_fingerprint, expected_fingerprint):
        return "revision unknown (installed source does not match receipt)"
    return revision


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _positive_even_int(value: str) -> int:
    parsed = _positive_int(value)
    if parsed % 2:
        raise argparse.ArgumentTypeError("must be even for yuv420p video")
    return parsed


def _jpeg_quality(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 100:
        raise argparse.ArgumentTypeError("must be between 1 and 100")
    return parsed


def _audio_offset_ms(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 0 <= parsed <= MAX_AUTO_AV_OFFSET_MS:
        raise argparse.ArgumentTypeError(
            f"must be between 0 and {MAX_AUTO_AV_OFFSET_MS}"
        )
    return parsed


def _finite_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be finite")
    return parsed


def _positive_finite_float(value: str) -> float:
    parsed = _finite_float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _video_bitrate_mbps(value: str) -> float:
    parsed = _positive_finite_float(value)
    if parsed > MAX_VIDEO_BITRATE_MBPS:
        raise argparse.ArgumentTypeError(
            f"must be at most {MAX_VIDEO_BITRATE_MBPS:g} Mbps"
        )
    return parsed


def _audio_drift_ppm(value: str) -> float:
    parsed = _finite_float(value)
    if abs(parsed) > MAX_ABS_AUDIO_DRIFT_PPM:
        raise argparse.ArgumentTypeError(
            f"must be between {-MAX_ABS_AUDIO_DRIFT_PPM:g} and "
            f"{MAX_ABS_AUDIO_DRIFT_PPM:g}"
        )
    return parsed


def _version_text() -> str:
    try:
        installed_version = package_version("fix-casting")
    except PackageNotFoundError:
        installed_version = "unknown"

    # An editable install executes this checkout directly. Never label it with
    # a stale installation receipt from an earlier snapshot.
    package_dir = Path(__file__).resolve().parent
    if (package_dir.parent / ".git").exists():
        provenance = "development checkout (editable/source import)"
    else:
        provenance = _verified_install_provenance(package_dir)
    verified_helper, helper_failure = verify_installed_audiotee(AUDIOTEE_INSTALL_PATH)
    if verified_helper is not None:
        helper = (
            f"; AudioTee sha256:{verified_helper.binary_sha256[:12]}"
            f" source:{verified_helper.source_fingerprint[:12]}"
        )
    elif AUDIOTEE_INSTALL_PATH.exists() or AUDIOTEE_INSTALL_PATH.is_symlink():
        helper = f"; AudioTee unverified ({helper_failure})"
    else:
        helper = ""
    return f"fix-casting {installed_version} ({provenance}{helper})"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cast",
        description=(
            "Cast a browser tab to Chromecast. Renders the full page and mirrors "
            "it to your TV — does not use Chrome's dominant-video detection."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=_version_text(),
        help="show installed version and source provenance",
    )
    parser.add_argument("url", help="URL to open and mirror")
    parser.add_argument(
        "--width",
        type=_positive_even_int,
        default=1920,
        help="Viewport width (default: 1920)",
    )
    parser.add_argument(
        "--height",
        type=_positive_even_int,
        default=1080,
        help="Viewport height (default: 1080)",
    )
    parser.add_argument(
        "--fps",
        type=_positive_int,
        default=None,
        help=(
            "Encode frame rate (default: 30 in the production profile; "
            "23 at 1080p / 24 at 720p with --no-buffered)"
        ),
    )
    parser.add_argument(
        "--jpeg-quality",
        type=_jpeg_quality,
        default=None,
        metavar="Q",
        help=(
            "JPEG quality (1-100) for tab capture (default: 92). Higher = "
            "sharper but more CPU/bandwidth."
        ),
    )
    parser.add_argument(
        "--video-bitrate",
        type=_video_bitrate_mbps,
        default=None,
        metavar="MBPS",
        help=(
            f"Override the H.264 target bitrate in Mbps, up to "
            f"{MAX_VIDEO_BITRATE_MBPS:g} (default: chosen by resolution, 15 at "
            "1080p). Raise it with --stats to find how high your Chromecast's "
            "network sustains before it buffers."
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
        type=_positive_finite_float,
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
        type=_audio_offset_ms,
        default=0,
        help=(
            f"Manual A/V trim in ms, 0-{MAX_AUTO_AV_OFFSET_MS} (default: 0). "
            "Positive delays audio (use if audio is ahead of video)."
        ),
    )
    parser.add_argument(
        "--audio-drift-ppm",
        type=_audio_drift_ppm,
        default=0.0,
        metavar="PPM",
        help=(
            "Correct slow audio clock drift in parts-per-million "
            f"({-MAX_ABS_AUDIO_DRIFT_PPM:g} to {MAX_ABS_AUDIO_DRIFT_PPM:g}; "
            "default: 0). "
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
            "Use the production encode profile with 2s HLS segments and a 12s "
            "rolling playlist (default: on). Use --no-buffered for the existing "
            "1s/4s low-latency profile; actual TV delay is receiver-controlled."
        ),
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print pipeline timing stats every 10s to diagnose lag",
    )
    parser.add_argument(
        "--stats-interval",
        type=_positive_finite_float,
        default=10.0,
        help="Seconds between stats reports when --stats is set (default: 10)",
    )
    parser.add_argument(
        "--tv-poll-interval",
        type=_positive_finite_float,
        default=2.0,
        help="Seconds between Chromecast status polls when --stats is set (default: 2)",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Show a live full-screen dashboard of all pipeline stats with a "
        "real-time audio-offset knob (instead of the scrolling --stats text).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    print("Searching for Chromecast devices...")
    try:
        devices = discover_devices(timeout=args.discovery_timeout)
        if args.device:
            device = find_device(devices, args.device)
            print(f"Casting to {device.name}.")
        else:
            device = select_device(devices)
    except RuntimeError as exc:
        # No devices / bad --device: a clean one-line error, not a traceback.
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    encode_fps = args.fps or default_fps_for_resolution(
        args.width, args.height, buffered=args.buffered
    )
    jpeg_quality = args.jpeg_quality or DEFAULT_JPEG_QUALITY
    # The TUI is a live view of the same stats, so it needs them collected too.
    collect_stats = args.stats or args.tui
    # Keep cumulative counters even in the default quiet mode so the exit
    # summary can report lost video timeline ticks and ffmpeg restarts.
    stats = PipelineStats(
        target_fps=float(encode_fps),
        trace_enabled=collect_stats,
    )

    adblock_patterns = None
    if args.adblock:
        from cast_tab.adblocking import build_block_patterns

        print("Loading ad-block filter lists...")
        adblock_patterns = build_block_patterns()

    # The signal handler below may interrupt any Python bytecode, including an
    # Event/Condition critical section. A plain boolean assignment is the only
    # operation it performs; session/caster workers poll this reader without
    # asking the handler to acquire a lock or tear anything down.
    stop_requested = False

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
    set_session_cancellation = getattr(session, "set_cancellation_probe", None)
    if set_session_cancellation is not None:
        set_session_cancellation(lambda: stop_requested)
    set_caster_cancellation = getattr(caster, "set_cancellation_probe", None)
    if set_caster_cancellation is not None:
        set_caster_cancellation(lambda: stop_requested)

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
        dropped_total, restarts_total = stats.totals()
        if dropped_total:
            parts.append(
                f"{dropped_total} video timeline ticks discarded/skipped "
                "during A/V re-anchors"
            )
        if restarts_total:
            parts.append(f"{restarts_total} ffmpeg restarts")
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
        failures: list[tuple[str, BaseException]] = []
        # Stop the receiver/watchdog while HLS is still available. Otherwise a
        # final watchdog poll can reload a URL as its server is disappearing.
        cleanup_steps = (("Chromecast", caster.stop), ("session", session.stop))
        for name, stop in cleanup_steps:
            try:
                stop()
            except BaseException as exc:
                # Cleanup is best-effort across independent components. A
                # browser teardown failure must not leave the TV connected.
                failures.append((name, exc))

        # Component stop methods retain ownership of incomplete phases and are
        # intentionally retryable. A process can miss its first bounded reap
        # window while exiting normally; make one immediate second pass only
        # over failed owners. Successful, potentially non-idempotent phases
        # are never repeated, and only the final failure is reported.
        if failures:
            retry_steps = {name: stop for name, stop in cleanup_steps}
            retry_failures: list[tuple[str, BaseException]] = []
            for name, _first_failure in failures:
                try:
                    retry_steps[name]()
                except BaseException as exc:
                    retry_failures.append((name, exc))
            failures = retry_failures
        _print_exit_summary()
        for name, failure in failures:
            print(f"Warning: failed to stop {name}: {failure}", file=sys.stderr)

    def handle_signal(_signum=None, _frame=None) -> None:
        # A Python signal handler can run between the CALL and STORE bytecodes
        # of any resource factory. Raising or tearing down here can therefore
        # strand a newly spawned Chrome/AudioTee/ffmpeg before its owner stores
        # it. Only publish a lock-free CPython boolean assignment; ordinary
        # control flow reaches the single finally block that owns teardown.
        nonlocal stop_requested
        stop_requested = True

    def raise_if_stop_requested() -> None:
        if stop_requested:
            raise _ShutdownRequested

    def check_runtime_health() -> None:
        raise_if_stop_requested()
        session.raise_if_failed()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        session.start()
        raise_if_stop_requested()
        streamer = session.streamer
        assert streamer is not None  # session.start() succeeded above
        playlist_url = streamer.configure_receiver(device.host)
        caster.set_delivery_probe(streamer.receiver_delivery_observation)

        audio_mode = "with tab audio" if session.audio_active else "video only"
        holdback = estimated_hls_holdback_s(buffered=args.buffered)
        retention = hls_playlist_retention_s(buffered=args.buffered)
        profile_name = "production HLS" if args.buffered else "low-latency HLS"
        latency_mode = (
            f"{profile_name} (~{holdback}s estimated player holdback, "
            f"{retention}s playlist retention)"
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

        raise_if_stop_requested()
        caster.connect()
        raise_if_stop_requested()
        caster.play_hls(playlist_url)
        raise_if_stop_requested()
        # Background watchdog: re-casts if the TV stops playing (app killed,
        # stream error). Off-loop so a ~50s dead-TV recovery never blocks
        # stats/TUI. In TUI mode the non-playing card shows the state, so no
        # print callback (it would write over the full-screen UI).
        caster.start_watchdog(
            on_event=None if args.tui else (
                lambda event: print(f"[recover] {event}", flush=True)
            ),
            announce_recovery=not args.tui,
        )

        if args.tui:
            # Full-screen dashboard owns the terminal and its own poll loop.
            from cast_tab.tui import run_tui

            run_tui(
                stats=stats,
                streamer=streamer,
                caster=caster,
                initial_offset_ms=streamer.audio_offset_ms,
                tv_poll_interval=args.tv_poll_interval,
                playlist_url=playlist_url,
                health_check=check_runtime_health,
            )
            return 0

        print("Casting. Press Ctrl+C to stop.")
        print(f"Source page: {args.url}")
        if not args.headless:
            print("A browser window is rendering the page locally.")
        if session.audio_active:
            print("Cast browser audio plays on your TV only; other Mac audio is unchanged.")
        if args.stats:
            print(
                f"Stats enabled (every {args.stats_interval:.0f}s, "
                f"tv polls every {args.tv_poll_interval:.0f}s)."
            )

        next_stats_at = time.monotonic() + args.stats_interval
        next_tv_poll_at = time.monotonic()
        while not stop_requested:
            session.raise_if_failed()
            now = time.monotonic()
            if args.stats and now >= next_tv_poll_at:
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

            if args.stats and now >= next_stats_at:
                print(stats.format_report(args.stats_interval), flush=True)
                next_stats_at = now + args.stats_interval
            time.sleep(0.25)
    except _ShutdownRequested:
        return 0
    except Exception as exc:
        if stop_requested:
            return 0
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
