"""Frame-pacing primitives: captured-frame history and the encode queue.

The sampler thread (in HLSStreamer) selects from CapturedFrameHistory at an
exactly even cadence and puts into BoundedFrameQueue; the writer thread gets
from the queue and feeds ffmpeg.  Retaining a short, timestamped history lets a
late sampler recover the frames belonging to missed ticks instead of repeating
one latest frame several times and turning scheduler jitter into visible judder.
"""

from __future__ import annotations

import math
import threading
import time
from bisect import bisect_left, bisect_right
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    """One captured frame and its source/publication timing metadata."""

    frame: bytes
    captured_at: float
    published_at: float
    generation: int


@dataclass(frozen=True, slots=True)
class FrameSelection:
    """A frame selected for one scheduled CFR tick.

    ``generation_delta`` is relative to the caller-provided previous selection.
    For example, an ideal 60 -> 30 conversion normally advances by two capture
    generations per tick.  Zero is an actual repeated source frame; values over
    one expose source frames skipped between output ticks.
    """

    frame: bytes
    scheduled_at: float
    captured_at: float
    published_at: float
    generation: int
    generation_delta: int | None
    repeated: bool
    skipped_generations: int
    used_future_fallback: bool

    @property
    def source_age_s(self) -> float:
        """Capture age at the scheduled tick; negative for a future fallback."""
        return self.scheduled_at - self.captured_at


class CapturedFrameHistory:
    """Thread-safe, memory-bounded history for timestamp-aware CFR sampling.

    ``captured_at`` and the value passed to :meth:`select` must use the same
    clock domain.  Callers without a source timestamp may omit it; publication
    monotonic time is then used for both. ``published_at`` drives age pruning
    and should be monotonic, but explicit out-of-order capture timestamps are
    supported and kept sorted.

    The default keeps at most two seconds of 60 fps capture.  Callers can tune
    both limits for their capture rate and desired catch-up window; the hard
    frame-count cap always bounds JPEG memory even if timestamps are malformed.
    """

    def __init__(
        self,
        *,
        max_frames: int = 120,
        max_age_s: float | None = 2.0,
    ) -> None:
        if isinstance(max_frames, bool) or not isinstance(max_frames, int):
            raise TypeError("max_frames must be an integer")
        if max_frames <= 0:
            raise ValueError("max_frames must be greater than 0")
        if max_age_s is not None and (not math.isfinite(max_age_s) or max_age_s <= 0):
            raise ValueError("max_age_s must be finite and greater than 0")

        self._lock = threading.Lock()
        self._max_frames = max_frames
        self._max_age_s = max_age_s
        self._frames: list[CapturedFrame] = []
        self._capture_keys: list[tuple[float, int]] = []
        self._generation = 0
        self._newest_published_at: float | None = None

    @staticmethod
    def _timestamp(value: float, name: str) -> float:
        timestamp = float(value)
        if not math.isfinite(timestamp):
            raise ValueError(f"{name} must be finite")
        return timestamp

    def _prune_locked(self) -> None:
        if self._max_age_s is not None and self._newest_published_at is not None:
            cutoff = self._newest_published_at - self._max_age_s
            retained = [item for item in self._frames if item.published_at >= cutoff]
            if len(retained) != len(self._frames):
                self._frames = retained
                self._capture_keys = [(item.captured_at, item.generation) for item in retained]

        overflow = len(self._frames) - self._max_frames
        if overflow > 0:
            del self._frames[:overflow]
            del self._capture_keys[:overflow]

    def _publish_locked(
        self,
        frame: bytes,
        *,
        captured_at: float,
        published_at: float,
    ) -> CapturedFrame:
        self._generation += 1
        item = CapturedFrame(
            frame=frame,
            captured_at=captured_at,
            published_at=published_at,
            generation=self._generation,
        )
        index = bisect_right(
            self._capture_keys,
            (item.captured_at, item.generation),
        )
        self._capture_keys.insert(index, (item.captured_at, item.generation))
        self._frames.insert(index, item)
        self._newest_published_at = max(
            published_at,
            self._newest_published_at if self._newest_published_at is not None else published_at,
        )
        self._prune_locked()
        return item

    def publish(
        self,
        frame: bytes,
        *,
        captured_at: float | None = None,
        published_at: float | None = None,
    ) -> CapturedFrame:
        """Retain a frame and return its immutable metadata record."""
        if published_at is None:
            published_at = time.monotonic()
        published_at = self._timestamp(published_at, "published_at")
        if captured_at is None:
            captured_at = published_at
        captured_at = self._timestamp(captured_at, "captured_at")

        with self._lock:
            return self._publish_locked(
                frame,
                captured_at=captured_at,
                published_at=published_at,
            )

    def reanchor_latest(self, boundary_at: float) -> CapturedFrame | None:
        """Hold the current visual state at a fresh media boundary.

        Obtaining the newest captured frame and inserting its zero-order hold
        happen under one lock. A capture delivered concurrently either becomes
        the held state or is published afterward with its own source timestamp,
        allowing a generation boundary to reject delayed old-timeline frames
        without starving a static page of video.
        """
        boundary_at = self._timestamp(boundary_at, "boundary_at")
        with self._lock:
            if not self._frames:
                return None
            latest_capture = self._frames[-1]
            return self._publish_locked(
                latest_capture.frame,
                captured_at=boundary_at,
                published_at=boundary_at,
            )

    def select(
        self,
        scheduled_at: float,
        *,
        previous_generation: int | None = None,
        not_before: float | None = None,
    ) -> FrameSelection | None:
        """Select the newest capture at or before a scheduled CFR tick.

        If the tick predates all retained history (startup or pruning), the
        oldest retained frame is returned and ``used_future_fallback`` is true.
        A tick after the newest capture naturally selects that newest frame,
        allowing ``repeated`` to describe an actual capture gap. ``not_before``
        excludes captures from an earlier media timeline even if they arrived
        after its boundary.
        """
        scheduled_at = self._timestamp(scheduled_at, "scheduled_at")
        if not_before is not None:
            not_before = self._timestamp(not_before, "not_before")
        with self._lock:
            if not self._frames:
                return None
            first_allowed = (
                0
                if not_before is None
                else bisect_left(
                    self._capture_keys,
                    (not_before, -math.inf),
                )
            )
            if first_allowed >= len(self._frames):
                return None
            index = (
                bisect_right(
                    self._capture_keys,
                    (scheduled_at, math.inf),
                )
                - 1
            )
            used_future_fallback = index < first_allowed
            if used_future_fallback:
                index = first_allowed
            item = self._frames[index]

        generation_delta = (
            None if previous_generation is None else item.generation - previous_generation
        )
        return FrameSelection(
            frame=item.frame,
            scheduled_at=scheduled_at,
            captured_at=item.captured_at,
            published_at=item.published_at,
            generation=item.generation,
            generation_delta=generation_delta,
            repeated=generation_delta == 0,
            skipped_generations=max(0, (generation_delta or 0) - 1),
            used_future_fallback=used_future_fallback,
        )

    def peek(self) -> tuple[bytes | None, float | None, int]:
        """Return the newest-by-capture frame in the legacy tuple shape."""
        with self._lock:
            if not self._frames:
                return None, None, self._generation
            latest = self._frames[-1]
            return latest.frame, latest.published_at, latest.generation

    def clear(self) -> int:
        """Discard retained captures without reusing generation numbers."""
        with self._lock:
            discarded = len(self._frames)
            self._frames.clear()
            self._capture_keys.clear()
            self._newest_published_at = None
            return discarded

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)


class LatestFrame(CapturedFrameHistory):
    """Backward-compatible name for the timestamped captured-frame history."""


class BoundedFrameQueue:
    """Sampled frames waiting for the writer to push them into ffmpeg.

    Queue depth is residence time/backpressure, not A/V offset.  image2pipe's
    ``-framerate`` assigns video PTS from the number of frames ffmpeg accepts;
    raw PCM PTS likewise comes from the number of samples.  Waiting in this
    queue preserves that relationship as long as every sampled frame is later
    written.  Dropping even one sampled frame, however, shortens the video
    content timeline by 1/fps while audio keeps every sample.  Callers must
    therefore treat a non-zero ``dropped`` result as a broken CFR timeline and
    re-anchor both raw inputs before writing any post-gap frame.

    The bound limits memory and detection latency.  It does *not* make frame
    loss harmless or cap cumulative A/V skew by itself.
    """

    def __init__(self, maxlen: int) -> None:
        self._maxlen = max(1, maxlen)
        self._frames: deque[bytes] = deque()
        self._cond = threading.Condition()

    def put(self, frame: bytes) -> tuple[int, int]:
        """Enqueue; drop the oldest past the bound. Returns (depth, dropped)."""
        with self._cond:
            dropped = 0
            if len(self._frames) >= self._maxlen:
                # ffmpeg is sustainably behind; drop the oldest frame so latency
                # cannot grow without bound. Even sampling is preserved.
                self._frames.popleft()
                dropped = 1
            self._frames.append(frame)
            depth = len(self._frames)
            self._cond.notify()
        return depth, dropped

    def get(self, stopped: threading.Event) -> bytes | None:
        """Block for the next frame; None when woken empty (stop/spurious)."""
        with self._cond:
            while not self._frames and not stopped.is_set():
                self._cond.wait(timeout=0.5)
            if not self._frames:
                return None
            return self._frames.popleft()

    def clear(self) -> int:
        """Discard queued frames and return how many were removed."""
        with self._cond:
            discarded = len(self._frames)
            self._frames.clear()
            return discarded

    def wake_all(self) -> None:
        with self._cond:
            self._cond.notify_all()
