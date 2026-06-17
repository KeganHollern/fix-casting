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
class PipelineStats:
    """Thread-safe counters for each stage of the cast pipeline."""

    target_fps: float = 30.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _capture: _Window = field(default_factory=_Window, repr=False)
    _capture_behind: int = 0
    _capture_errors: int = 0
    _capture_timeouts: int = 0
    # How stale each frame is when it reaches us: time.time() - the Chrome
    # capture timestamp on the screencast frame. This is the suspected home of
    # the audio-ahead skew (video arriving from Chrome already seconds old).
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
    _publish: _Window = field(default_factory=_Window, repr=False)
    _frame_age: _Window = field(default_factory=_Window, repr=False)
    _encode: _Window = field(default_factory=_Window, repr=False)
    _encode_repeats: int = 0
    _encode_resyncs: int = 0
    _encode_write: _Window = field(default_factory=_Window, repr=False)
    _queue_peak: int = 0
    _queue_dropped: int = 0
    _ffmpeg_restarts: int = 0
    _audio_warnings: int = 0
    _audio_last_warning: str | None = None
    _hls_segment_age_s: float | None = None
    _hls_segment_count: int = 0
    _hls_segments_deleted: int = 0
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
        each stage truly began. Without --stats there is no PipelineStats and
        these calls don't happen, so traces are opt-in with the rest of --stats.
        """
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

    def record_capture_timeout(self) -> None:
        with self._lock:
            self._capture_timeouts += 1

    def record_publish(self) -> None:
        with self._lock:
            self._publish.add(1.0)

    def record_frame_age(self, age_s: float) -> None:
        with self._lock:
            self._frame_age.add(age_s)

    def record_encode_repeat(self) -> None:
        """A tick re-sent the last frame because capture produced nothing new."""
        with self._lock:
            self._encode_repeats += 1

    def record_encode_resync(self) -> None:
        """The encoder fell >1s behind and reset its clock instead of bursting."""
        with self._lock:
            self._encode_resyncs += 1

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
            self._queue_dropped += dropped
            if self._ts_enabled:
                self._ts_queue.append(
                    (time.monotonic() - self._ts_start, depth, dropped)
                )

    def format_timeseries(self, window_s: float = 2.0) -> str:
        """Per-window queue depth + write stalls — shows if the queue drains."""
        with self._lock:
            q = list(self._ts_queue)
            w = list(self._ts_write)
        if not q:
            return "queue time-series: (no data; call enable_timeseries first)"
        end = max(q[-1][0], w[-1][0] if w else 0.0)
        lines = [
            "queue depth + write stalls over time "
            f"(per {window_s:.0f}s window):",
            "  window      depth(avg/max)  drops   write(max)",
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

    def record_hls(
        self,
        *,
        segment_count: int,
        newest_age_s: float | None,
        segments_deleted: int = 0,
    ) -> None:
        with self._lock:
            self._hls_segment_count = segment_count
            self._hls_segment_age_s = newest_age_s
            self._hls_segments_deleted += segments_deleted

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
                self._tv_non_playing_states[label] = (
                    self._tv_non_playing_states.get(label, 0) + 1
                )
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

    def format_report(self, interval_s: float) -> str:
        with self._lock:
            capture_fps = self._capture.count / interval_s if interval_s > 0 else 0.0
            encode_fps = self._encode.count / interval_s if interval_s > 0 else 0.0

            capture_ms = self._capture.avg() * 1000
            capture_peak_ms = self._capture.peak * 1000
            screencast_lag_ms = self._screencast_lag.avg() * 1000
            screencast_lag_peak_ms = self._screencast_lag.peak * 1000
            screencast_lag_count = self._screencast_lag.count
            audio_backlog_ms = self._audio_backlog_ms
            audio_backlog_peak_ms = self._audio_backlog_peak_ms
            frame_age_ms = self._frame_age.avg() * 1000
            frame_age_peak_ms = self._frame_age.peak * 1000
            write_ms = self._encode_write.avg() * 1000
            write_peak_ms = self._encode_write.peak * 1000

            behind = self._capture_behind
            errors = self._capture_errors
            timeouts = self._capture_timeouts
            repeats = self._encode_repeats
            resyncs = self._encode_resyncs
            queue_peak = self._queue_peak
            queue_dropped = self._queue_dropped
            ffmpeg_restarts = self._ffmpeg_restarts
            audio_warnings = self._audio_warnings
            audio_last_warning = self._audio_last_warning
            hls_count = self._hls_segment_count
            hls_age = self._hls_segment_age_s
            hls_deleted = self._hls_segments_deleted
            tv_state = self._tv_state or "unknown"
            tv_pos = self._tv_position_s
            tv_idle = self._tv_idle_reason
            tv_polls = self._tv_polls
            tv_non_playing = self._tv_non_playing_polls
            tv_non_playing_states = dict(self._tv_non_playing_states)
            interval_start_pos = self._tv_interval_start_pos_s
            stall_accum = self._tv_stall_accum_s

            pos_delta: float | None = None
            if tv_pos is not None and interval_start_pos is not None:
                pos_delta = tv_pos - interval_start_pos

            self._capture.reset()
            self._capture_behind = 0
            self._capture_errors = 0
            self._capture_timeouts = 0
            self._screencast_lag.reset()
            self._audio_backlog_peak_ms = 0.0
            self._publish.reset()
            self._frame_age.reset()
            self._encode.reset()
            self._encode_repeats = 0
            self._encode_resyncs = 0
            self._encode_write.reset()
            self._queue_peak = 0
            self._queue_dropped = 0
            self._ffmpeg_restarts = 0
            self._audio_warnings = 0
            self._hls_segments_deleted = 0
            self._tv_polls = 0
            self._tv_non_playing_polls = 0
            self._tv_non_playing_states.clear()
            self._tv_stall_accum_s = 0.0
            if tv_pos is not None:
                self._tv_interval_start_pos_s = tv_pos

        capture_line = (
            f"capture {capture_fps:.1f}/{self.target_fps:.0f} fps, "
            f"capture avg {capture_ms:.0f}ms peak {capture_peak_ms:.0f}ms"
            + (f", behind {behind}x" if behind else "")
            + (f", timeouts {timeouts}" if timeouts else "")
            + (f", errors {errors}" if errors else "")
        )
        if screencast_lag_count:
            capture_line += (
                f", chrome→app lag avg {screencast_lag_ms:.0f}ms "
                f"peak {screencast_lag_peak_ms:.0f}ms"
            )

        lines = [
            capture_line,
            (
                f"encode  {encode_fps:.1f}/{self.target_fps:.0f} fps to ffmpeg, "
                f"frame age avg {frame_age_ms:.0f}ms peak {frame_age_peak_ms:.0f}ms, "
                f"stdin write avg {write_ms:.1f}ms peak {write_peak_ms:.1f}ms"
                + (f", queue peak {queue_peak}" if queue_peak else "")
                + (f", dropped {queue_dropped}" if queue_dropped else "")
                + (f", repeats {repeats}" if repeats else "")
                + (f", resyncs {resyncs}" if resyncs else "")
                + (f", ffmpeg restarts {ffmpeg_restarts}" if ffmpeg_restarts else "")
            ),
        ]

        hls_line = f"hls     {hls_count} segments"
        if hls_age is not None:
            hls_line += f", newest segment {hls_age:.1f}s old"
        if hls_deleted:
            hls_line += f", deleted {hls_deleted}"
        lines.append(hls_line)

        audio_bits: list[str] = []
        if audio_backlog_ms is not None:
            audio_bits.append(
                f"pipe backlog {audio_backlog_ms:.0f}ms peak {audio_backlog_peak_ms:.0f}ms"
            )
        if audio_warnings:
            warn = f"{audio_warnings} warnings this interval"
            if audio_last_warning:
                warn += f' (last: "{audio_last_warning}")'
            audio_bits.append(warn)
        if audio_bits:
            lines.append("audio   " + ", ".join(audio_bits))

        tv_line = f"tv      {tv_state}"
        if tv_idle and tv_state != "PLAYING":
            tv_line += f", idle {tv_idle}"
        if tv_pos is not None:
            tv_line += f", playback position {tv_pos:.0f}s"
        if pos_delta is not None:
            tv_line += f", position +{pos_delta:.0f}s/{interval_s:.0f}s"
            interval_stall = max(0.0, interval_s - pos_delta)
            if interval_stall >= 2.0:
                tv_line += f", stall ~{interval_stall:.0f}s"
            elif pos_delta > interval_s + 2.0:
                tv_line += f", catch-up +{pos_delta - interval_s:.0f}s"
        if stall_accum >= 2.0:
            tv_line += f", micro-stalls ~{stall_accum:.0f}s"
        if tv_polls:
            tv_line += f", polls {tv_polls}"
        if tv_non_playing:
            reasons = ", ".join(
                f"{label} x{count}"
                for label, count in sorted(
                    tv_non_playing_states.items(), key=lambda item: (-item[1], item[0])
                )
            )
            tv_line += f", non-playing {tv_non_playing}/{tv_polls} ({reasons})"
        lines.append(tv_line)

        return "\n".join(f"[stats] {line}" for line in lines)