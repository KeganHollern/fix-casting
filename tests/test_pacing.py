"""Unit tests for the frame-pacing primitives."""

import threading

import pytest

from cast_tab.pacing import (
    BoundedFrameQueue,
    CapturedFrameHistory,
    LatestFrame,
)


def test_latest_frame_generations():
    lf = LatestFrame()
    assert lf.peek() == (None, None, 0)
    published = lf.publish(b"a")
    assert published.generation == 1
    assert published.captured_at == published.published_at
    frame, published_at, gen = lf.peek()
    assert frame == b"a" and published_at is not None and gen == 1
    lf.publish(b"b")
    frame, _, gen = lf.peek()
    assert frame == b"b" and gen == 2
    # Peek does not consume.
    assert lf.peek()[2] == 2


def test_history_selects_newest_capture_at_or_before_tick():
    history = CapturedFrameHistory(max_frames=10, max_age_s=None)
    history.publish(b"a", captured_at=1.0, published_at=11.0)
    history.publish(b"b", captured_at=2.0, published_at=12.0)
    history.publish(b"c", captured_at=3.0, published_at=13.0)

    selected = history.select(2.5, previous_generation=1)

    assert selected is not None
    assert selected.frame == b"b"
    assert selected.captured_at == 2.0
    assert selected.published_at == 12.0
    assert selected.generation == 2
    assert selected.generation_delta == 1
    assert not selected.repeated
    assert selected.skipped_generations == 0
    assert not selected.used_future_fallback
    assert selected.source_age_s == 0.5


def test_history_uses_oldest_frame_when_tick_predates_retained_history():
    history = CapturedFrameHistory(max_frames=10, max_age_s=None)
    history.publish(b"first", captured_at=10.0, published_at=20.0)
    history.publish(b"second", captured_at=11.0, published_at=21.0)

    selected = history.select(9.5)

    assert selected is not None
    assert selected.frame == b"first"
    assert selected.used_future_fallback
    assert selected.source_age_s == -0.5


def test_history_marks_repeat_when_tick_is_newer_than_latest_capture():
    history = CapturedFrameHistory(max_frames=10, max_age_s=None)
    item = history.publish(b"latest", captured_at=4.0, published_at=5.0)

    selected = history.select(5.0, previous_generation=item.generation)

    assert selected is not None
    assert selected.frame == b"latest"
    assert selected.generation_delta == 0
    assert selected.repeated
    assert selected.skipped_generations == 0
    assert not selected.used_future_fallback


def test_history_boundary_excludes_delayed_pre_boundary_capture():
    history = CapturedFrameHistory(max_frames=10, max_age_s=None)
    history.publish(b"old", captured_at=9.0, published_at=9.0)
    # This frame arrived after the boundary but belongs to the old capture
    # timeline, so publication time must not make it eligible.
    history.publish(b"delayed-old", captured_at=9.5, published_at=11.0)

    assert history.select(11.0, not_before=10.0) is None

    fresh = history.publish(b"fresh", captured_at=10.5, published_at=11.5)
    selected = history.select(
        11.0,
        previous_generation=None,
        not_before=10.0,
    )
    assert selected is not None
    assert selected.frame == b"fresh"
    assert selected.generation == fresh.generation
    assert not selected.used_future_fallback


def test_history_boundary_uses_allowed_future_fallback_not_old_frame():
    history = CapturedFrameHistory(max_frames=10, max_age_s=None)
    history.publish(b"old", captured_at=9.0, published_at=9.0)
    history.publish(b"fresh", captured_at=10.5, published_at=10.5)

    selected = history.select(10.25, not_before=10.0)

    assert selected is not None
    assert selected.frame == b"fresh"
    assert selected.used_future_fallback


def test_history_reanchor_holds_latest_capture_and_rejects_delayed_old():
    history = CapturedFrameHistory(max_frames=10, max_age_s=None)
    latest = history.publish(b"newest-visual", captured_at=9.9, published_at=9.9)
    # A delayed older source frame must not become the boundary's visual merely
    # because it was delivered most recently.
    delayed = history.publish(
        b"delayed-older-capture",
        captured_at=9.8,
        published_at=9.95,
    )
    assert delayed.generation > latest.generation

    held = history.reanchor_latest(10.0)

    assert held is not None
    assert held.frame == latest.frame == b"newest-visual"
    assert held.captured_at == held.published_at == 10.0
    assert held.generation == delayed.generation + 1

    history.publish(
        b"delayed-pre-boundary",
        captured_at=9.99,
        published_at=10.01,
    )
    selected = history.select(10.02, not_before=10.0)
    assert selected is not None
    assert selected.frame == b"newest-visual"
    assert selected.generation == held.generation


def test_late_sampler_uses_historical_frames_for_missed_ticks():
    history = CapturedFrameHistory(max_frames=20, max_age_s=None)
    for index in range(7):
        captured_at = index / 60
        history.publish(
            bytes([index]),
            captured_at=captured_at,
            published_at=10.0 + captured_at,
        )

    # Model a sampler which wakes only after every capture is already present,
    # then services its missed 30 fps ticks in chronological order.
    selections = []
    previous_generation = None
    for tick in range(4):
        selected = history.select(
            tick / 30,
            previous_generation=previous_generation,
        )
        assert selected is not None
        selections.append(selected)
        previous_generation = selected.generation

    assert [selected.frame for selected in selections] == [
        b"\x00",
        b"\x02",
        b"\x04",
        b"\x06",
    ]
    assert [selected.generation_delta for selected in selections] == [
        None,
        2,
        2,
        2,
    ]
    assert [selected.skipped_generations for selected in selections] == [0, 1, 1, 1]
    assert not any(selected.repeated for selected in selections)


def test_history_orders_out_of_order_capture_timestamps():
    history = CapturedFrameHistory(max_frames=10, max_age_s=None)
    later = history.publish(b"later", captured_at=2.0, published_at=10.0)
    earlier = history.publish(b"earlier", captured_at=1.0, published_at=11.0)

    first = history.select(1.5)
    second = history.select(2.0, previous_generation=earlier.generation)

    assert first is not None and first.frame == b"earlier"
    assert second is not None and second.frame == b"later"
    assert second.generation == later.generation
    # Publication generations expose the reordering instead of pretending this
    # was a normal forward capture skip.
    assert second.generation_delta == -1
    assert second.skipped_generations == 0


def test_history_prunes_by_frame_count_and_publication_age():
    by_count = CapturedFrameHistory(max_frames=3, max_age_s=None)
    for index in range(5):
        by_count.publish(
            bytes([index]),
            captured_at=float(index),
            published_at=float(index),
        )
    assert len(by_count) == 3
    oldest_retained = by_count.select(-1.0)
    assert oldest_retained is not None
    assert oldest_retained.frame == b"\x02"
    assert oldest_retained.used_future_fallback

    by_age = CapturedFrameHistory(max_frames=10, max_age_s=2.0)
    by_age.publish(b"expired", captured_at=0.0, published_at=0.0)
    by_age.publish(b"edge", captured_at=1.0, published_at=1.0)
    by_age.publish(b"latest", captured_at=3.0, published_at=3.0)
    assert len(by_age) == 2
    oldest_retained = by_age.select(0.0)
    assert oldest_retained is not None
    assert oldest_retained.frame == b"edge"


def test_history_is_thread_safe_and_clear_does_not_reuse_generations():
    history = CapturedFrameHistory(max_frames=64, max_age_s=None)

    def publish_batch(batch: int) -> None:
        for index in range(50):
            timestamp = float(batch * 100 + index)
            history.publish(
                f"{batch}:{index}".encode(),
                captured_at=timestamp,
                published_at=timestamp,
            )

    threads = [threading.Thread(target=publish_batch, args=(batch,)) for batch in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert len(history) == 64
    newest = history.select(1_000.0)
    assert newest is not None and newest.frame == b"3:49"
    assert history.clear() == 64
    assert len(history) == 0
    assert history.select(1_000.0) is None
    assert history.peek() == (None, None, 200)
    after_clear = history.publish(b"new", captured_at=1_001.0, published_at=1_001.0)
    assert after_clear.generation == 201


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"max_frames": 0}, ValueError),
        ({"max_frames": True}, TypeError),
        ({"max_age_s": 0.0}, ValueError),
        ({"max_age_s": float("inf")}, ValueError),
    ],
)
def test_history_rejects_invalid_bounds(kwargs, error):
    with pytest.raises(error):
        CapturedFrameHistory(**kwargs)


def test_history_rejects_non_finite_timestamps():
    history = CapturedFrameHistory()
    with pytest.raises(ValueError, match="captured_at"):
        history.publish(b"frame", captured_at=float("nan"))
    with pytest.raises(ValueError, match="scheduled_at"):
        history.select(float("inf"))
    with pytest.raises(ValueError, match="not_before"):
        history.select(1.0, not_before=float("nan"))


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
