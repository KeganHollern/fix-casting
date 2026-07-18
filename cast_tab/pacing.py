"""Frame-pacing primitives: the latest-frame holder and the bounded queue.

The sampler thread (in HLSStreamer) reads LatestFrame at an exactly even
cadence and puts into BoundedFrameQueue; the writer thread gets from the
queue and feeds ffmpeg. Decoupling the two keeps sampling perfectly paced
even when an ffmpeg write stalls (HLS segment flush, keyframe), which is
what otherwise distorts motion into judder.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class LatestFrame:
    """Thread-safe holder for the most recent captured frame."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: bytes | None = None
        self._published_at: float | None = None
        self._generation = 0

    def publish(self, frame: bytes) -> None:
        with self._lock:
            self._frame = frame
            self._published_at = time.monotonic()
            self._generation += 1

    def peek(self) -> tuple[bytes | None, float | None, int]:
        with self._lock:
            return self._frame, self._published_at, self._generation


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
