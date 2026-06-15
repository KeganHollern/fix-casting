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
    _publish: _Window = field(default_factory=_Window, repr=False)
    _frame_age: _Window = field(default_factory=_Window, repr=False)
    _encode: _Window = field(default_factory=_Window, repr=False)
    _encode_repeats: int = 0
    _encode_resyncs: int = 0
    _encode_write: _Window = field(default_factory=_Window, repr=False)
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

    def record_capture(self, latency_s: float, *, behind: bool = False) -> None:
        with self._lock:
            self._capture.add(latency_s)
            if behind:
                self._capture_behind += 1

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

    def record_encode_write(self, write_s: float) -> None:
        with self._lock:
            self._encode.add(1.0)
            self._encode_write.add(write_s)

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
            frame_age_ms = self._frame_age.avg() * 1000
            frame_age_peak_ms = self._frame_age.peak * 1000
            write_ms = self._encode_write.avg() * 1000
            write_peak_ms = self._encode_write.peak * 1000

            behind = self._capture_behind
            errors = self._capture_errors
            timeouts = self._capture_timeouts
            repeats = self._encode_repeats
            resyncs = self._encode_resyncs
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
            self._publish.reset()
            self._frame_age.reset()
            self._encode.reset()
            self._encode_repeats = 0
            self._encode_resyncs = 0
            self._encode_write.reset()
            self._ffmpeg_restarts = 0
            self._audio_warnings = 0
            self._hls_segments_deleted = 0
            self._tv_polls = 0
            self._tv_non_playing_polls = 0
            self._tv_non_playing_states.clear()
            self._tv_stall_accum_s = 0.0
            if tv_pos is not None:
                self._tv_interval_start_pos_s = tv_pos

        lines = [
            (
                f"capture {capture_fps:.1f}/{self.target_fps:.0f} fps, "
                f"capture avg {capture_ms:.0f}ms peak {capture_peak_ms:.0f}ms"
                + (f", behind {behind}x" if behind else "")
                + (f", timeouts {timeouts}" if timeouts else "")
                + (f", errors {errors}" if errors else "")
            ),
            (
                f"encode  {encode_fps:.1f}/{self.target_fps:.0f} fps to ffmpeg, "
                f"frame age avg {frame_age_ms:.0f}ms peak {frame_age_peak_ms:.0f}ms, "
                f"stdin write avg {write_ms:.1f}ms peak {write_peak_ms:.1f}ms"
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

        if audio_warnings:
            audio_line = f"audio   {audio_warnings} warnings this interval"
            if audio_last_warning:
                audio_line += f' (last: "{audio_last_warning}")'
            lines.append(audio_line)

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