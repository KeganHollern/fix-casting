"""Unit tests for the frame-pacing primitives."""

import threading

from cast_tab.pacing import BoundedFrameQueue, LatestFrame


def test_latest_frame_generations():
    lf = LatestFrame()
    assert lf.peek() == (None, None, 0)
    lf.publish(b"a")
    frame, published_at, gen = lf.peek()
    assert frame == b"a" and published_at is not None and gen == 1
    lf.publish(b"b")
    frame, _, gen = lf.peek()
    assert frame == b"b" and gen == 2
    # Peek does not consume.
    assert lf.peek()[2] == 2


def test_queue_bounds_and_drops_oldest():
    q = BoundedFrameQueue(maxlen=3)
    stopped = threading.Event()
    for i in range(3):
        depth, dropped = q.put(bytes([i]))
        assert dropped == 0
    assert depth == 3
    # Past the bound: the OLDEST frame is dropped, depth stays at the cap.
    depth, dropped = q.put(b"\x03")
    assert (depth, dropped) == (3, 1)
    assert q.get(stopped) == b"\x01"  # b"\x00" was dropped


def test_queue_get_returns_none_when_stopped():
    q = BoundedFrameQueue(maxlen=2)
    stopped = threading.Event()
    stopped.set()
    assert q.get(stopped) is None


def test_queue_get_wakes_on_put():
    q = BoundedFrameQueue(maxlen=2)
    stopped = threading.Event()
    got: list[bytes | None] = []

    def consumer():
        got.append(q.get(stopped))

    t = threading.Thread(target=consumer)
    t.start()
    q.put(b"x")
    t.join(timeout=2)
    assert not t.is_alive() and got == [b"x"]


def test_queue_clear():
    q = BoundedFrameQueue(maxlen=4)
    q.put(b"a")
    q.put(b"b")
    assert q.clear() == 2
    assert q.clear() == 0
    stopped = threading.Event()
    stopped.set()
    assert q.get(stopped) is None
