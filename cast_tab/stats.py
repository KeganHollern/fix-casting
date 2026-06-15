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
    _encode_skipped: int = 0
    _encode_write: _Window = field(default_factory=_Window, repr=False)
    _ffmpeg_restarts: int = 0
    _hls_segment_age_s: float | None = None
    _hls_segment_count: int = 0
    _tv_state: str | None = None
    _tv_position_s: float | None = None

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

    def record_encode_skip(self) -> None:
        with self._lock:
            self._encode_skipped += 1

    def record_encode_write(self, write_s: float) -> None:
        with self._lock:
            self._encode.add(1.0)
            self._encode_write.add(write_s)

    def record_ffmpeg_restart(self) -> None:
        with self._lock:
            self._ffmpeg_restarts += 1

    def record_hls(self, *, segment_count: int, newest_age_s: float | None) -> None:
        with self._lock:
            self._hls_segment_count = segment_count
            self._hls_segment_age_s = newest_age_s

    def record_tv(self, *, state: str | None, position_s: float | None) -> None:
        with self._lock:
            self._tv_state = state
            self._tv_position_s = position_s

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
            skipped = self._encode_skipped
            ffmpeg_restarts = self._ffmpeg_restarts
            hls_count = self._hls_segment_count
            hls_age = self._hls_segment_age_s
            tv_state = self._tv_state or "unknown"
            tv_pos = self._tv_position_s

            self._capture.reset()
            self._capture_behind = 0
            self._capture_errors = 0
            self._capture_timeouts = 0
            self._publish.reset()
            self._frame_age.reset()
            self._encode.reset()
            self._encode_skipped = 0
            self._encode_write.reset()
            self._ffmpeg_restarts = 0

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
                + (f", skipped dupes {skipped}" if skipped else "")
                + (f", ffmpeg restarts {ffmpeg_restarts}" if ffmpeg_restarts else "")
            ),
        ]

        if hls_age is not None:
            lines.append(f"hls     {hls_count} segments, newest segment {hls_age:.1f}s old")
        else:
            lines.append(f"hls     {hls_count} segments")

        if tv_pos is not None:
            lines.append(f"tv      {tv_state}, playback position {tv_pos:.0f}s")
        else:
            lines.append(f"tv      {tv_state}")

        return "\n".join(f"[stats] {line}" for line in lines)