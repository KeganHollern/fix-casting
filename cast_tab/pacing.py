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

    Depth IS the audio lead. ffmpeg's image2pipe timestamps frames by the
    time they ARRIVE on its stdin, so a frame that waits `depth/fps` seconds
    in this queue reaches the muxer that much later than its audio and the
    output plays audio ahead by ~depth/fps. (A deep queue was the real
    "audio leads over long runtime" — it grew under encoder contention and
    the lead grew with it; an earlier 8s bound let the lead reach 8s.) In
    normal operation the writer drains the queue to depth ~1 (the encoder
    has ample headroom), so the lead is ~one frame. Bound it at ~1s so even
    a sustained stall caps the audio lead at ~1s (dropping the oldest frames
    past that — a brief stutter — rather than letting the lead grow
    unbounded). stats exposes the live lead as queue_depth/fps.
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

    def clear(self) -> None:
        with self._cond:
            self._frames.clear()

    def wake_all(self) -> None:
        with self._cond:
            self._cond.notify_all()
