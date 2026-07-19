"""Lightweight pipeline timing stats for diagnosing cast lag."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Window:
    count: int = 0
    total: float = 0.0
    peak: float = 0.0

    def add(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.peak = max(self.peak, value)

    def avg(self) -> float:
        return self.total / self.count if self.count else 0.0

    def reset(self) -> None:
        self.count = 0
        self.total = 0.0
        self.peak = 0.0


@dataclass
class StatsSnapshot:
    """One interval's worth of metrics, grouped by pipeline segment.

    Produced by PipelineStats.snapshot(); consumed by both the text report and
    the TUI so there is a single collect-and-reset path.
    """

    interval_s: float
    target_fps: float
    # --- Capture (CDP screencast + AudioTee, incoming) ---
    capture_fps: float
    capture_ms: float
    capture_peak_ms: float
    behind: int
    errors: int
    screencast_lag_ms: float
    screencast_lag_peak_ms: float
    screencast_lag_count: int
    audio_backlog_ms: float | None
    audio_backlog_peak_ms: float
    audio_warnings: int
    audio_last_warning: str | None
    # --- Encode pipeline (sampler -> queue -> ffmpeg stdin, internal) ---
    encode_fps: float
    frame_age_ms: float
    frame_age_peak_ms: float
    write_ms: float
    write_peak_ms: float
    queue_peak: int
    queue_dropped: int
    queue_residence_ms: float
    repeats: int
    history_recoveries: int
    cadence_error_ms: float
    cadence_error_peak_ms: float
    sampler_late_ms: float
    sampler_late_peak_ms: float
    resyncs: int
    resyncs_total: int
    resync_last_reason: str | None
    ffmpeg_restarts: int
    ffmpeg_errors: int
    ffmpeg_last_error: str | None
    # --- Sync (cumulative) ---
    dropped_total: int
    restarts_total: int
    # --- HLS (outgoing) ---
    hls_count: int
    hls_age: float | None
    hls_deleted: int
    hls_target_s: float
    hls_publish_ms: float
    hls_publish_peak_ms: float
    hls_publish_count: int
    hls_segment_requests: int
    hls_delivery_ms: float | None
    hls_delivery_mbps: float | None
    hls_delivery_active: int
    hls_delivery_active_ms: float | None
    hls_delivery_idle_ms: float | None
    hls_delivery_seen: bool
    hls_delivery_errors: int
    # --- TV / Chromecast (playback) ---
    tv_state: str
    tv_pos: float | None
    tv_idle: str | None
    tv_polls: int
    tv_non_playing: int
    tv_non_playing_states: dict
    pos_delta: float | None
    stall_accum: float


@dataclass
class PipelineStats:
    """Thread-safe counters for each stage of the cast pipeline."""

    target_fps: float = 30.0
    # The default CLI always keeps the inexpensive cumulative counters so its
    # exit summary can report lost video timeline ticks and ffmpeg restarts. Lifecycle
    # trace lines remain opt-in with --stats/--tui.
    trace_enabled: bool = True
    # RLock, not Lock: the CLI's signal handler reads stats from the same
    # main thread that may be holding the lock mid-report when SIGINT lands;
    # a non-reentrant lock deadlocks that path.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _capture: _Window = field(default_factory=_Window, repr=False)
    _capture_behind: int = 0
    _capture_errors: int = 0
    # How stale each frame is when it reaches us: time.time() - Chrome's
    # capture timestamp. Source timestamps also let the sampler preserve the
    # original cadence when callback delivery or its own wake-up is bursty.
    _screencast_lag: _Window = field(default_factory=_Window, repr=False)
    # Bytes sitting unread in the audio pipe (ffmpeg's read backlog), in ms of
    # audio. Stays ~0 if ffmpeg keeps up; grows if audio is being delayed.
    _audio_backlog_ms: float | None = None
    _audio_backlog_peak_ms: float = 0.0
    # One-shot lifecycle markers (relative monotonic seconds) for ordering the
    # startup sequence and seeing the audio-vs-video PTS=0 anchor gap.
    _trace_seen: set[str] = field(default_factory=set, repr=False)
    _start_monotonic: float = field(default_factory=time.monotonic, repr=False)
    # Optional full time-series of queue depth and stdin write times, for
    # validating whether the video queue drains after a stall or stays deep.
    _ts_enabled: bool = False
    _ts_start: float = field(default_factory=time.monotonic, repr=False)
    _ts_queue: list = field(default_factory=list, repr=False)  # (t, depth, dropped)
    _ts_write: list = field(default_factory=list, repr=False)  # (t, write_s)
    _frame_age: _Window = field(default_factory=_Window, repr=False)
    _encode: _Window = field(default_factory=_Window, repr=False)
    _encode_repeats: int = 0
    _history_recoveries: int = 0
    _cadence_error: _Window = field(default_factory=_Window, repr=False)
    _sampler_late: _Window = field(default_factory=_Window, repr=False)
    _encode_resyncs: int = 0
    _encode_resyncs_total: int = 0
    _encode_last_resync: str | None = None
    _encode_write: _Window = field(default_factory=_Window, repr=False)
    _queue_peak: int = 0
    # Interval running mean of queue depth.  Converted to milliseconds, this is
    # queue residence/backpressure only. image2pipe timestamps by accepted frame
    # count, not wall-clock arrival, so depth must never be reported as A/V skew.
    _queue_depth_sum: float = 0.0
    _queue_depth_n: int = 0
    _queue_dropped: int = 0
    _ffmpeg_restarts: int = 0
    # Cumulative (never reset). A lost sampled frame breaks the old CFR
    # frame-count timeline. The streamer responds with an A/V re-anchor; this
    # counter therefore measures discarded visual timeline, not residual skew.
    _queue_dropped_total: int = 0
    _ffmpeg_restarts_total: int = 0
    # ffmpeg stderr lines this interval (at -loglevel error these are real
    # encoder/mux errors); fed by the streamer's stderr drain thread.
    _ffmpeg_errors: int = 0
    _ffmpeg_last_error: str | None = None
    _audio_warnings: int = 0
    _audio_last_warning: str | None = None
    _hls_segment_age_s: float | None = None
    _hls_segment_count: int = 0
    _hls_segments_deleted: int = 0
    _hls_target_s: float = 0.0
    _hls_publish_interval: _Window = field(default_factory=_Window, repr=False)
    _hls_segment_requests: int = 0
    _hls_delivery_s: float | None = None
    _hls_delivery_mbps: float | None = None
    _hls_delivery_active: int = 0
    _hls_delivery_active_s: float | None = None
    _hls_delivery_idle_s: float | None = None
    _hls_delivery_seen: bool = False
    _hls_delivery_errors: int = 0
    _tv_state: str | None = None
    _tv_position_s: float | None = None
    _tv_idle_reason: str | None = None
    _tv_polls: int = 0
    _tv_non_playing_polls: int = 0
    _tv_non_playing_states: dict[str, int] = field(default_factory=dict, repr=False)
    _tv_interval_start_pos_s: float | None = None
    _tv_stall_accum_s: float = 0.0
    _tv_last_poll_pos_s: float | None = None
    _tv_last_poll_at: float | None = None

    def trace(self, label: str, *, once: bool = False) -> None:
        """Print a lifecycle marker with elapsed monotonic time since startup.

        once=True fires the first time only — use for one-shot anchors (first
        publish, first sampler tick, first writer write) so the line marks when
        each stage truly began. ``trace_enabled=False`` retains counters while
        keeping these diagnostic lines quiet.
        """
        if not self.trace_enabled:
            return
        with self._lock:
            if once:
                if label in self._trace_seen:
                    return
                self._trace_seen.add(label)
            elapsed = time.monotonic() - self._start_monotonic
        print(f"[trace] +{elapsed:8.3f}s {label}", flush=True)

    def record_capture(self, latency_s: float, *, behind: bool = False) -> None:
        with self._lock:
            self._capture.add(latency_s)
            if behind:
                self._capture_behind += 1

    def record_screencast_lag(self, lag_s: float) -> None:
        """Age of a frame (Chrome capture time → arrival here)."""
        with self._lock:
            self._screencast_lag.add(lag_s)

    def record_audio_backlog(self, backlog_ms: float) -> None:
        """Unread audio bytes in the ffmpeg pipe, expressed as ms of audio."""
        with self._lock:
            self._audio_backlog_ms = backlog_ms
            self._audio_backlog_peak_ms = max(self._audio_backlog_peak_ms, backlog_ms)

    def record_capture_error(self) -> None:
        with self._lock:
            self._capture_errors += 1

    def record_frame_age(self, age_s: float) -> None:
        with self._lock:
            self._frame_age.add(age_s)

    def record_encode_repeat(self) -> None:
        """A tick re-sent the last frame because capture produced nothing new."""
        with self._lock:
            self._encode_repeats += 1

    def record_sampler_cadence(
        self,
        *,
        source_interval_s: float | None,
        tick_late_s: float,
        recovered_from_history: bool = False,
    ) -> None:
        """Record temporal sampling quality independently of output CFR.

        FFmpeg gives every accepted frame an even output timestamp. This metric
        checks whether the *captured content* selected for those ticks also
        advanced evenly; a repeat at 30 fps is therefore a 33 ms cadence error.
        """
        with self._lock:
            if source_interval_s is not None:
                target_period = 1.0 / self.target_fps if self.target_fps else 0.0
                self._cadence_error.add(abs(source_interval_s - target_period))
            self._sampler_late.add(max(0.0, tick_late_s))
            if recovered_from_history:
                self._history_recoveries += 1

    def record_encode_resync(self, reason: str, *, lost_frames: int = 0) -> None:
        """A CFR break requested a fresh, jointly anchored ffmpeg generation."""
        with self._lock:
            self._encode_resyncs += 1
            self._encode_resyncs_total += 1
            self._encode_last_resync = reason
            self._queue_dropped += lost_frames
            self._queue_dropped_total += lost_frames

    def record_timeline_loss(self, frames: int) -> None:
        """Count sampled frames intentionally discarded at a re-anchor."""
        if frames <= 0:
            return
        with self._lock:
            self._queue_dropped += frames
            self._queue_dropped_total += frames

    def record_audio_warning(self, text: str) -> None:
        with self._lock:
            self._audio_warnings += 1
            self._audio_last_warning = text

    def enable_timeseries(self) -> None:
        """Start recording the full queue-depth / write-time time-series."""
        with self._lock:
            self._ts_enabled = True
            self._ts_start = time.monotonic()
            self._ts_queue.clear()
            self._ts_write.clear()

    def record_encode_write(self, write_s: float) -> None:
        with self._lock:
            self._encode.add(1.0)
            self._encode_write.add(write_s)
            if self._ts_enabled:
                self._ts_write.append((time.monotonic() - self._ts_start, write_s))

    def record_queue(self, *, depth: int, dropped: int = 0) -> None:
        with self._lock:
            self._queue_peak = max(self._queue_peak, depth)
            self._queue_depth_sum += depth
            self._queue_depth_n += 1
            self._queue_dropped += dropped
            self._queue_dropped_total += dropped
            if self._ts_enabled:
                self._ts_queue.append((time.monotonic() - self._ts_start, depth, dropped))

    def format_timeseries(self, window_s: float = 2.0) -> str:
        """Per-window queue depth + write stalls — shows if the queue drains."""
        with self._lock:
            q = list(self._ts_queue)
            w = list(self._ts_write)
        if not q:
            return "queue time-series: (no data; call enable_timeseries first)"
        end = max(q[-1][0], w[-1][0] if w else 0.0)
        lines = [
            f"queue depth + write stalls over time (per {window_s:.0f}s window):",
            "  window      depth(avg/max)  losses write(max)",
        ]
        n = int(end // window_s) + 1
        for b in range(n):
            t0, t1 = b * window_s, (b + 1) * window_s
            depths = [d for (t, d, _) in q if t0 <= t < t1]
            drops = sum(dr for (t, _, dr) in q if t0 <= t < t1)
            writes = [x for (t, x) in w if t0 <= t < t1]
            if not depths and not writes:
                continue
            avg_d = sum(depths) / len(depths) if depths else 0.0
            max_d = max(depths) if depths else 0
            max_w = max(writes) * 1000 if writes else 0.0
            lines.append(
                f"  t={t0:5.0f}-{t1:<4.0f}s  {avg_d:5.1f} / {max_d:<4d}     "
                f"{drops:4d}   {max_w:6.0f}ms"
            )
        return "\n".join(lines)

    def record_ffmpeg_restart(self) -> None:
        with self._lock:
            self._ffmpeg_restarts += 1
            self._ffmpeg_restarts_total += 1

    def record_ffmpeg_stderr(self, line: str) -> None:
        """A line ffmpeg wrote to stderr (an error at -loglevel error)."""
        with self._lock:
            self._ffmpeg_errors += 1
            self._ffmpeg_last_error = line

    def record_hls(
        self,
        *,
        segment_count: int,
        newest_age_s: float | None,
        segments_deleted: int = 0,
        target_duration_s: float = 0.0,
        publish_intervals_s: tuple[float, ...] = (),
        segment_requests: int = 0,
        delivery_s: float | None = None,
        delivery_mbps: float | None = None,
        delivery_active: int = 0,
        delivery_active_s: float | None = None,
        delivery_idle_s: float | None = None,
        delivery_seen: bool | None = None,
        delivery_errors: int = 0,
    ) -> None:
        with self._lock:
            self._hls_segment_count = segment_count
            self._hls_segment_age_s = newest_age_s
            self._hls_segments_deleted += segments_deleted
            self._hls_target_s = max(0.0, target_duration_s)
            for interval in publish_intervals_s:
                if interval >= 0:
                    self._hls_publish_interval.add(interval)
            self._hls_segment_requests += max(0, segment_requests)
            self._hls_delivery_s = delivery_s
            self._hls_delivery_mbps = delivery_mbps
            self._hls_delivery_active = max(0, delivery_active)
            self._hls_delivery_active_s = (
                max(0.0, delivery_active_s) if delivery_active_s is not None else None
            )
            self._hls_delivery_idle_s = (
                max(0.0, delivery_idle_s) if delivery_idle_s is not None else None
            )
            if delivery_seen is not None:
                self._hls_delivery_seen = delivery_seen
            self._hls_delivery_errors += max(0, delivery_errors)

    def record_tv_poll(
        self,
        *,
        state: str | None,
        position_s: float | None,
        idle_reason: str | None,
    ) -> list[str]:
        """Record a Chromecast status poll. Returns immediate event lines."""
        events: list[str] = []
        now = time.monotonic()

        with self._lock:
            self._tv_polls += 1
            self._tv_state = state
            self._tv_position_s = position_s
            self._tv_idle_reason = idle_reason

            if state and state != "PLAYING":
                self._tv_non_playing_polls += 1
                label = state if not idle_reason else f"{state} ({idle_reason})"
                self._tv_non_playing_states[label] = self._tv_non_playing_states.get(label, 0) + 1
                events.append(f"tv event {label}")

            if position_s is not None and self._tv_last_poll_pos_s is not None:
                if self._tv_last_poll_at is not None:
                    wall_s = now - self._tv_last_poll_at
                    pos_s = position_s - self._tv_last_poll_pos_s
                    if wall_s >= 2.0 and pos_s + 1.0 < wall_s:
                        self._tv_stall_accum_s += wall_s - pos_s

            if position_s is not None:
                self._tv_last_poll_pos_s = position_s
                self._tv_last_poll_at = now

        return events

    def totals(self) -> tuple[int, int]:
        """Cumulative (lost video timeline ticks, ffmpeg restarts), no reset.

        The exit summary uses this instead of snapshot(): snapshot resets the
        interval windows as a side effect, which is wrong at exit time and can
        race a still-running poller.
        """
        with self._lock:
            return self._queue_dropped_total, self._ffmpeg_restarts_total

    def snapshot(self, interval_s: float) -> StatsSnapshot:
        """Collect this interval's metrics into a struct and reset the windows.

        Single collect-and-reset path shared by the text report and the TUI.
        """
        with self._lock:
            tv_pos = self._tv_position_s
            interval_start_pos = self._tv_interval_start_pos_s
            pos_delta: float | None = None
            if tv_pos is not None and interval_start_pos is not None:
                pos_delta = tv_pos - interval_start_pos
            dropped_total = self._queue_dropped_total
            avg_depth = self._queue_depth_sum / self._queue_depth_n if self._queue_depth_n else 0.0
            queue_residence_ms = avg_depth / self.target_fps * 1000 if self.target_fps else 0.0

            snap = StatsSnapshot(
                interval_s=interval_s,
                target_fps=self.target_fps,
                capture_fps=self._capture.count / interval_s if interval_s > 0 else 0.0,
                capture_ms=self._capture.avg() * 1000,
                capture_peak_ms=self._capture.peak * 1000,
                behind=self._capture_behind,
                errors=self._capture_errors,
                screencast_lag_ms=self._screencast_lag.avg() * 1000,
                screencast_lag_peak_ms=self._screencast_lag.peak * 1000,
                screencast_lag_count=self._screencast_lag.count,
                audio_backlog_ms=self._audio_backlog_ms,
                audio_backlog_peak_ms=self._audio_backlog_peak_ms,
                audio_warnings=self._audio_warnings,
                audio_last_warning=self._audio_last_warning,
                encode_fps=self._encode.count / interval_s if interval_s > 0 else 0.0,
                frame_age_ms=self._frame_age.avg() * 1000,
                frame_age_peak_ms=self._frame_age.peak * 1000,
                write_ms=self._encode_write.avg() * 1000,
                write_peak_ms=self._encode_write.peak * 1000,
                queue_peak=self._queue_peak,
                queue_dropped=self._queue_dropped,
                queue_residence_ms=queue_residence_ms,
                repeats=self._encode_repeats,
                history_recoveries=self._history_recoveries,
                cadence_error_ms=self._cadence_error.avg() * 1000,
                cadence_error_peak_ms=self._cadence_error.peak * 1000,
                sampler_late_ms=self._sampler_late.avg() * 1000,
                sampler_late_peak_ms=self._sampler_late.peak * 1000,
                resyncs=self._encode_resyncs,
                resyncs_total=self._encode_resyncs_total,
                resync_last_reason=self._encode_last_resync,
                ffmpeg_restarts=self._ffmpeg_restarts,
                ffmpeg_errors=self._ffmpeg_errors,
                ffmpeg_last_error=self._ffmpeg_last_error,
                dropped_total=dropped_total,
                restarts_total=self._ffmpeg_restarts_total,
                hls_count=self._hls_segment_count,
                hls_age=self._hls_segment_age_s,
                hls_deleted=self._hls_segments_deleted,
                hls_target_s=self._hls_target_s,
                hls_publish_ms=self._hls_publish_interval.avg() * 1000,
                hls_publish_peak_ms=self._hls_publish_interval.peak * 1000,
                hls_publish_count=self._hls_publish_interval.count,
                hls_segment_requests=self._hls_segment_requests,
                hls_delivery_ms=(
                    self._hls_delivery_s * 1000 if self._hls_delivery_s is not None else None
                ),
                hls_delivery_mbps=self._hls_delivery_mbps,
                hls_delivery_active=self._hls_delivery_active,
                hls_delivery_active_ms=(
                    self._hls_delivery_active_s * 1000
                    if self._hls_delivery_active_s is not None
                    else None
                ),
                hls_delivery_idle_ms=(
                    self._hls_delivery_idle_s * 1000
                    if self._hls_delivery_idle_s is not None
                    else None
                ),
                hls_delivery_seen=self._hls_delivery_seen,
                hls_delivery_errors=self._hls_delivery_errors,
                tv_state=self._tv_state or "unknown",
                tv_pos=tv_pos,
                tv_idle=self._tv_idle_reason,
                tv_polls=self._tv_polls,
                tv_non_playing=self._tv_non_playing_polls,
                tv_non_playing_states=dict(self._tv_non_playing_states),
                pos_delta=pos_delta,
                stall_accum=self._tv_stall_accum_s,
            )

            self._capture.reset()
            self._capture_behind = 0
            self._capture_errors = 0
            self._screencast_lag.reset()
            self._audio_backlog_peak_ms = 0.0
            self._frame_age.reset()
            self._encode.reset()
            self._encode_repeats = 0
            self._history_recoveries = 0
            self._cadence_error.reset()
            self._sampler_late.reset()
            self._encode_resyncs = 0
            self._encode_last_resync = None
            self._encode_write.reset()
            self._queue_peak = 0
            self._queue_depth_sum = 0.0
            self._queue_depth_n = 0
            self._queue_dropped = 0
            self._ffmpeg_restarts = 0
            self._ffmpeg_errors = 0
            self._audio_warnings = 0
            self._hls_segments_deleted = 0
            self._hls_publish_interval.reset()
            self._hls_segment_requests = 0
            self._hls_delivery_errors = 0
            self._tv_polls = 0
            self._tv_non_playing_polls = 0
            self._tv_non_playing_states.clear()
            self._tv_stall_accum_s = 0.0
            if tv_pos is not None:
                self._tv_interval_start_pos_s = tv_pos

        return snap

    def format_report(self, interval_s: float) -> str:
        s = self.snapshot(interval_s)

        capture_line = (
            f"capture {s.capture_fps:.1f}/{s.target_fps:.0f} fps, "
            f"capture avg {s.capture_ms:.0f}ms peak {s.capture_peak_ms:.0f}ms"
            + (f", behind {s.behind}x" if s.behind else "")
            + (f", errors {s.errors}" if s.errors else "")
        )
        if s.screencast_lag_count:
            capture_line += (
                f", chrome→app lag avg {s.screencast_lag_ms:.0f}ms "
                f"peak {s.screencast_lag_peak_ms:.0f}ms"
            )

        lines = [
            capture_line,
            (
                f"encode  {s.encode_fps:.1f}/{s.target_fps:.0f} fps to ffmpeg, "
                f"frame age avg {s.frame_age_ms:.0f}ms peak {s.frame_age_peak_ms:.0f}ms, "
                f"stdin write avg {s.write_ms:.1f}ms peak {s.write_peak_ms:.1f}ms"
                + (f", queue peak {s.queue_peak}" if s.queue_peak else "")
                + (f", timeline loss {s.queue_dropped}" if s.queue_dropped else "")
                + (f", held frames {s.repeats}" if s.repeats else "")
                + (f", history recoveries {s.history_recoveries}" if s.history_recoveries else "")
                + (
                    f", cadence error avg {s.cadence_error_ms:.1f}ms "
                    f"peak {s.cadence_error_peak_ms:.1f}ms"
                    if s.cadence_error_peak_ms
                    else ""
                )
                + (
                    f", sampler wake peak {s.sampler_late_peak_ms:.1f}ms late"
                    if s.sampler_late_peak_ms >= 1.0
                    else ""
                )
                + (f", A/V re-anchors {s.resyncs}" if s.resyncs else "")
                + (
                    f' (last: "{s.resync_last_reason}")'
                    if s.resyncs and s.resync_last_reason
                    else ""
                )
                + (f", ffmpeg restarts {s.ffmpeg_restarts}" if s.ffmpeg_restarts else "")
            ),
        ]

        if s.ffmpeg_errors:
            err_line = f"ffmpeg  {s.ffmpeg_errors} stderr lines this interval"
            if s.ffmpeg_last_error:
                err_line += f' (last: "{s.ffmpeg_last_error}")'
            lines.append(err_line)

        hls_line = f"hls     {s.hls_count} active playlist segments"
        if s.hls_age is not None:
            hls_line += f", newest segment {s.hls_age:.1f}s old"
        if s.hls_publish_count:
            hls_line += (
                f", publish avg {s.hls_publish_ms / 1000:.2f}s "
                f"peak {s.hls_publish_peak_ms / 1000:.2f}s"
            )
        if s.hls_segment_requests:
            hls_line += f", TV requested {s.hls_segment_requests} segments"
        if s.hls_delivery_ms is not None:
            hls_line += f", latest fetch {s.hls_delivery_ms:.0f}ms"
        if s.hls_delivery_mbps is not None:
            hls_line += f" at {s.hls_delivery_mbps:.1f}Mbps"
        if s.hls_delivery_active:
            hls_line += f", {s.hls_delivery_active} segment fetches active"
            if s.hls_delivery_active_ms is not None:
                hls_line += f" for {s.hls_delivery_active_ms:.0f}ms"
        elif s.hls_delivery_idle_ms is not None:
            if s.hls_delivery_seen:
                hls_line += f", last segment fetch {s.hls_delivery_idle_ms / 1000:.1f}s ago"
            else:
                hls_line += f", no segment fetch in {s.hls_delivery_idle_ms / 1000:.1f}s"
        if s.hls_delivery_errors:
            hls_line += f", {s.hls_delivery_errors} HTTP errors"
        if s.hls_deleted:
            hls_line += f", deleted {s.hls_deleted}"
        lines.append(hls_line)

        sync_state = "CFR timeline re-anchored" if s.resyncs_total else "CFR timeline intact"
        extra = f", queue residence ~{s.queue_residence_ms:.0f}ms (backpressure, not A/V offset)"
        if s.dropped_total:
            extra += f", {s.dropped_total} video timeline ticks discarded/skipped"
        if s.resyncs_total:
            extra += f", {s.resyncs_total} A/V re-anchors"
        if s.restarts_total:
            extra += f", {s.restarts_total} ffmpeg restarts"
        lines.append(f"sync    {sync_state}{extra}")

        audio_bits: list[str] = []
        if s.audio_backlog_ms is not None:
            audio_bits.append(
                f"pipe backlog {s.audio_backlog_ms:.0f}ms peak {s.audio_backlog_peak_ms:.0f}ms"
            )
        if s.audio_warnings:
            warn = f"{s.audio_warnings} warnings this interval"
            if s.audio_last_warning:
                warn += f' (last: "{s.audio_last_warning}")'
            audio_bits.append(warn)
        if audio_bits:
            lines.append("audio   " + ", ".join(audio_bits))

        tv_line = f"tv      {s.tv_state}"
        if s.tv_idle and s.tv_state != "PLAYING":
            tv_line += f", idle {s.tv_idle}"
        if s.tv_pos is not None:
            tv_line += f", playback position {s.tv_pos:.0f}s"
        if s.pos_delta is not None:
            tv_line += f", position +{s.pos_delta:.0f}s/{interval_s:.0f}s"
            interval_stall = max(0.0, interval_s - s.pos_delta)
            if interval_stall >= 2.0:
                tv_line += f", stall ~{interval_stall:.0f}s"
            elif s.pos_delta > interval_s + 2.0:
                tv_line += f", catch-up +{s.pos_delta - interval_s:.0f}s"
        if s.stall_accum >= 2.0:
            tv_line += f", micro-stalls ~{s.stall_accum:.0f}s"
        if s.tv_polls:
            tv_line += f", polls {s.tv_polls}"
        if s.tv_non_playing:
            reasons = ", ".join(
                f"{label} x{count}"
                for label, count in sorted(
                    s.tv_non_playing_states.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            )
            tv_line += f", non-playing {s.tv_non_playing}/{s.tv_polls} ({reasons})"
        lines.append(tv_line)

        return "\n".join(f"[stats] {line}" for line in lines)
