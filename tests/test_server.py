"""HTTP server lifecycle regressions."""

import io
import threading
from pathlib import Path

import pytest

import cast_tab.server as server_module
from cast_tab.server import (
    HLSDiscontinuitySequenceNormalizer,
    HLSHTTPServer,
    parse_hls_segment_epoch,
)


def _playlist(*lines: str, media_sequence: int = 0) -> bytes:
    return (
        "\n".join(
            [
                "#EXTM3U",
                "#EXT-X-VERSION:6",
                "#EXT-X-TARGETDURATION:2",
                f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}",
                "#EXT-X-INDEPENDENT-SEGMENTS",
                *lines,
            ]
        )
        + "\n"
    ).encode()


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("seg-e000012-a000003-000000004.ts", 12),
        ("https://cast.test/live/seg-e7-a2-9.ts?token=x", 7),
        ("nested/seg-e3_attempt_4.ts", 3),
        ("seg003.ts", None),
        ("seg-e-a1.ts", None),
    ],
)
def test_parse_hls_segment_epoch(uri: str, expected: int | None):
    assert parse_hls_segment_epoch(uri) == expected


@pytest.mark.parametrize(
    ("raw", "expected_sequence"),
    [
        (
            _playlist("#EXTINF:2.0,", "seg-e0-a0-000.ts"),
            0,
        ),
        (
            _playlist(
                "#EXTINF:2.0,",
                "seg-e0-a0-000.ts",
                "#EXT-X-DISCONTINUITY",
                "#EXTINF:2.0,",
                "seg-e1-a1-001.ts",
            ),
            0,
        ),
        (
            _playlist(
                "#EXT-X-DISCONTINUITY",
                "#EXTINF:2.0,",
                "seg-e1-a1-001.ts",
            ),
            0,
        ),
        (
            _playlist("#EXTINF:2.0,", "seg-e1-a1-002.ts", media_sequence=2),
            1,
        ),
        (
            _playlist("#EXTINF:2.0,", "seg-e2-a2-010.ts", media_sequence=10),
            2,
        ),
    ],
)
def test_discontinuity_sequence_is_derived_from_first_segment_epoch(
    raw: bytes,
    expected_sequence: int,
):
    normalized = HLSDiscontinuitySequenceNormalizer()(raw).decode()
    lines = normalized.splitlines()
    tag = f"#EXT-X-DISCONTINUITY-SEQUENCE:{expected_sequence}"

    assert lines.count(tag) == 1
    assert (
        lines.index(tag)
        == lines.index(next(line for line in lines if line.startswith("#EXT-X-MEDIA-SEQUENCE:")))
        + 1
    )
    first_boundary = next(
        (
            index
            for index, line in enumerate(lines)
            if line == "#EXT-X-DISCONTINUITY" or (line and not line.startswith("#"))
        ),
        len(lines),
    )
    assert lines.index(tag) < first_boundary


def test_discontinuity_sequence_advances_only_after_boundary_rolls_out():
    normalizer = HLSDiscontinuitySequenceNormalizer()
    snapshots = [
        _playlist("#EXTINF:2.0,", "seg-e0-a0-000.ts"),
        _playlist(
            "#EXTINF:2.0,",
            "seg-e0-a0-001.ts",
            "#EXT-X-DISCONTINUITY",
            "#EXTINF:2.0,",
            "seg-e1-a1-002.ts",
            media_sequence=1,
        ),
        _playlist(
            "#EXT-X-DISCONTINUITY",
            "#EXTINF:2.0,",
            "seg-e1-a1-002.ts",
            media_sequence=2,
        ),
        _playlist("#EXTINF:2.0,", "seg-e1-a1-003.ts", media_sequence=3),
        _playlist("#EXTINF:2.0,", "seg-e2-a2-008.ts", media_sequence=8),
    ]

    values = []
    for snapshot in snapshots:
        normalized = normalizer(snapshot).decode()
        tag = next(
            line
            for line in normalized.splitlines()
            if line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:")
        )
        values.append(int(tag.rsplit(":", 1)[1]))

    assert values == [0, 0, 0, 1, 2]
    assert values == sorted(values)


def test_normalizer_replaces_stale_header_and_is_idempotent():
    raw = _playlist(
        "#EXT-X-DISCONTINUITY-SEQUENCE:99",
        "#EXTINF:2.0,",
        "seg-e1-a3-004.ts",
        media_sequence=4,
    )
    normalizer = HLSDiscontinuitySequenceNormalizer()

    once = normalizer(raw)
    twice = normalizer(once)

    assert once == twice
    assert once.count(b"#EXT-X-DISCONTINUITY-SEQUENCE:") == 1
    assert b"#EXT-X-DISCONTINUITY-SEQUENCE:1\n" in once


@pytest.mark.parametrize(
    "raw",
    [
        b"not a playlist\n",
        b"\xff\xfe",
        _playlist("#EXTINF:2.0,", "seg000.ts"),
        _playlist(
            "#EXT-X-DISCONTINUITY",
            "#EXTINF:2.0,",
            "seg-e0-a0-000.ts",
        ),
        _playlist(
            "#EXTINF:2.0,",
            "seg-e0-a0-000.ts",
            "#EXT-X-DISCONTINUITY",
            "#EXTINF:2.0,",
            "seg-e2-a2-001.ts",
        ),
    ],
)
def test_normalizer_leaves_unrecognized_or_inconsistent_playlist_unchanged(raw: bytes):
    assert HLSDiscontinuitySequenceNormalizer()(raw) == raw


class _FakeSocket:
    def __init__(self) -> None:
        self.fd = 42

    def fileno(self) -> int:
        return self.fd


class _FakeHTTPServer:
    instances: list["_FakeHTTPServer"] = []

    def __init__(self, address, _handler) -> None:
        self.server_port = address[1] or 54321
        self.handler = _handler
        self.socket = _FakeSocket()
        self._shutdown = threading.Event()
        self.shutdown_calls = 0
        self.close_calls = 0
        self.instances.append(self)

    def serve_forever(self) -> None:
        self._shutdown.wait(timeout=5)

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self._shutdown.set()

    def server_close(self) -> None:
        self.close_calls += 1
        self.socket.fd = -1


class _HandlerSocket:
    def __init__(self, request: bytes) -> None:
        self._request = io.BytesIO(request)
        self.response = bytearray()

    def makefile(self, mode: str, *_args, **_kwargs):
        assert "r" in mode
        return self._request

    def sendall(self, data: bytes) -> None:
        self.response.extend(data)


def _request(
    handler,
    path: str = "/stream.m3u8",
    *,
    method: str = "GET",
) -> tuple[bytes, bytes]:
    sock = _HandlerSocket(
        f"{method} {path} HTTP/1.1\r\nHost: cast.test\r\nConnection: close\r\n\r\n".encode()
    )
    handler(sock, ("127.0.0.1", 12345), object())
    headers, body = bytes(sock.response).split(b"\r\n\r\n", 1)
    return headers, body


def test_http_server_synthesizes_playlist_response_without_mutating_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _FakeHTTPServer.instances.clear()
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", _FakeHTTPServer)
    raw = _playlist("#EXTINF:2.0,", "seg-e2-a4-010.ts", media_sequence=10)
    playlist = tmp_path / "stream.m3u8"
    playlist.write_bytes(raw)
    normalizer = HLSDiscontinuitySequenceNormalizer()
    server = HLSHTTPServer(tmp_path, playlist_transform=normalizer)
    server.start()

    try:
        underlying = server._server
        assert underlying is not None
        headers, body = _request(underlying.handler, "/stream.m3u8?cache-bust=1")
    finally:
        server.stop()

    assert b"200 OK" in headers
    assert b"Content-Type: application/vnd.apple.mpegurl" in headers
    assert f"Content-Length: {len(body)}".encode() in headers
    assert b"Cache-Control: no-cache, no-store, must-revalidate" in headers
    assert body == normalizer(raw)
    assert playlist.read_bytes() == raw


def test_http_server_falls_back_to_raw_playlist_when_transform_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _FakeHTTPServer.instances.clear()
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", _FakeHTTPServer)
    raw = _playlist("#EXTINF:2.0,", "seg-e0-a0-000.ts")
    (tmp_path / "stream.m3u8").write_bytes(raw)

    def fail(_playlist: bytes) -> bytes:
        raise RuntimeError("bad transform")

    server = HLSHTTPServer(tmp_path, playlist_transform=fail)
    server.start()
    try:
        underlying = server._server
        assert underlying is not None
        _headers, body = _request(underlying.handler)
    finally:
        server.stop()

    assert body == raw


def test_delivery_telemetry_reports_hls_requests_bytes_and_timing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _FakeHTTPServer.instances.clear()
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", _FakeHTTPServer)
    playlist_body = _playlist("#EXTINF:2.0,", "segment.ts")
    segment_body = b"mpeg-ts-payload" * 32
    (tmp_path / "stream.m3u8").write_bytes(playlist_body)
    (tmp_path / "segment.ts").write_bytes(segment_body)
    callback_snapshots = []
    server = HLSHTTPServer(tmp_path, on_delivery=callback_snapshots.append)
    server.start()

    try:
        underlying = server._server
        assert underlying is not None
        _playlist_headers, delivered_playlist = _request(
            underlying.handler, "/stream.m3u8?reload=1"
        )
        _segment_headers, delivered_segment = _request(underlying.handler, "/segment.ts?token=abc")
        missing_headers, _missing_body = _request(underlying.handler, "/missing.txt")
        snapshot = server.delivery_snapshot()
    finally:
        server.stop()

    assert delivered_playlist == playlist_body
    assert delivered_segment == segment_body
    assert b"404 File not found" in missing_headers
    assert snapshot.playlist_requests == 1
    assert snapshot.segment_requests == 1
    assert snapshot.other_requests == 1
    assert snapshot.active_requests == 0
    assert snapshot.completed_requests == 3
    assert snapshot.error_requests == 1
    assert snapshot.playlist_bytes_sent == len(playlist_body)
    assert snapshot.segment_bytes_sent == len(segment_body)
    assert snapshot.bytes_sent == len(playlist_body) + len(segment_body)
    assert snapshot.segment_responses == 1
    assert snapshot.segment_response_bytes_total == len(segment_body)
    assert snapshot.segment_response_duration_s_total >= 0
    assert snapshot.average_segment_response_duration_s is not None
    assert snapshot.average_segment_throughput_bps is not None
    assert snapshot.latest_segment_bytes == len(segment_body)
    assert snapshot.latest_segment_duration_s is not None
    assert snapshot.latest_segment_throughput_bps is not None
    assert snapshot.latest_segment_started_at is not None
    assert snapshot.latest_segment_completed_at is not None
    assert snapshot.latest_request_method == "GET"
    assert snapshot.latest_request_path == "/missing.txt"
    assert snapshot.latest_request_kind == "other"
    assert snapshot.latest_request_started_at is not None
    assert snapshot.latest_request_completed_at is not None
    assert snapshot.latest_request_duration_s is not None
    assert snapshot.latest_request_status == 404
    assert snapshot.latest_request_error
    assert len(callback_snapshots) == 3
    assert callback_snapshots[-1] == snapshot


def test_segment_head_is_not_counted_as_successful_media_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _FakeHTTPServer.instances.clear()
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", _FakeHTTPServer)
    segment_body = b"mpeg-ts-payload"
    (tmp_path / "segment.ts").write_bytes(segment_body)
    server = HLSHTTPServer(tmp_path)
    server.start()

    try:
        underlying = server._server
        assert underlying is not None
        headers, body = _request(
            underlying.handler,
            "/segment.ts",
            method="HEAD",
        )
        snapshot = server.delivery_snapshot()
    finally:
        server.stop()

    assert b"200 OK" in headers
    assert body == b""
    assert snapshot.segment_requests == 1
    assert snapshot.segment_responses == 0
    assert snapshot.segment_response_bytes_total == 0
    assert snapshot.segment_bytes_sent == 0
    assert snapshot.latest_segment_completed_at is None
    assert snapshot.latest_segment_duration_s is None
    assert snapshot.latest_segment_bytes == 0


class _BlockingBodySocket(_HandlerSocket):
    def __init__(self, request: bytes, body: bytes) -> None:
        super().__init__(request)
        self._body = body
        self.body_write_started = threading.Event()
        self.release_body = threading.Event()

    def sendall(self, data: bytes) -> None:
        if data == self._body:
            self.body_write_started.set()
            self.release_body.wait(timeout=2)
        super().sendall(data)


def test_delivery_telemetry_snapshot_is_safe_during_active_segment_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _FakeHTTPServer.instances.clear()
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", _FakeHTTPServer)
    segment_body = b"x" * 4096
    (tmp_path / "segment.ts").write_bytes(segment_body)
    callback_snapshots = []
    server = HLSHTTPServer(tmp_path, on_delivery=callback_snapshots.append)
    server.start()
    underlying = server._server
    assert underlying is not None
    sock = _BlockingBodySocket(
        b"GET /segment.ts HTTP/1.1\r\nHost: cast.test\r\nConnection: close\r\n\r\n",
        segment_body,
    )
    request_thread = threading.Thread(
        target=underlying.handler,
        args=(sock, ("127.0.0.1", 12345), object()),
    )

    try:
        request_thread.start()
        assert sock.body_write_started.wait(timeout=1)
        active = server.delivery_snapshot()
        assert active.segment_requests == 1
        assert active.active_requests == 1
        assert active.active_segment_requests == 1
        assert active.oldest_active_segment_started_at is not None
        assert active.oldest_active_segment_age_s is not None
        assert active.oldest_active_segment_age_s >= 0
        assert active.completed_requests == 0
        assert active.latest_request_path == "/segment.ts"
        assert active.latest_request_completed_at is None

        sock.release_body.set()
        request_thread.join(timeout=1)
        completed = server.delivery_snapshot()
    finally:
        sock.release_body.set()
        request_thread.join(timeout=1)
        server.stop()

    assert not request_thread.is_alive()
    assert completed.active_requests == 0
    assert completed.active_segment_requests == 0
    assert completed.oldest_active_segment_started_at is None
    assert completed.oldest_active_segment_age_s is None
    assert completed.completed_requests == 1
    assert completed.error_requests == 0
    assert completed.segment_bytes_sent == len(segment_body)
    assert len(callback_snapshots) == 1
    assert callback_snapshots[0] == completed


def test_active_segment_remains_visible_after_later_playlist_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _FakeHTTPServer.instances.clear()
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", _FakeHTTPServer)
    playlist_body = _playlist("#EXTINF:2.0,", "segment.ts")
    segment_body = b"x" * 4096
    (tmp_path / "stream.m3u8").write_bytes(playlist_body)
    (tmp_path / "segment.ts").write_bytes(segment_body)
    server = HLSHTTPServer(tmp_path)
    server.start()
    underlying = server._server
    assert underlying is not None
    sock = _BlockingBodySocket(
        b"GET /segment.ts HTTP/1.1\r\nHost: cast.test\r\nConnection: close\r\n\r\n",
        segment_body,
    )
    request_thread = threading.Thread(
        target=underlying.handler,
        args=(sock, ("127.0.0.1", 12345), object()),
    )

    try:
        request_thread.start()
        assert sock.body_write_started.wait(timeout=1)
        before_playlist = server.delivery_snapshot()

        _headers, delivered_playlist = _request(underlying.handler, "/stream.m3u8")
        after_playlist = server.delivery_snapshot()

        assert delivered_playlist == playlist_body
        assert after_playlist.latest_request_kind == "playlist"
        assert after_playlist.latest_request_completed_at is not None
        assert after_playlist.active_requests == 1
        assert after_playlist.active_segment_requests == 1
        assert (
            after_playlist.oldest_active_segment_started_at
            == before_playlist.oldest_active_segment_started_at
        )
        assert after_playlist.oldest_active_segment_age_s is not None
        assert before_playlist.oldest_active_segment_age_s is not None
        assert (
            after_playlist.oldest_active_segment_age_s
            >= before_playlist.oldest_active_segment_age_s
        )

        sock.release_body.set()
        request_thread.join(timeout=1)
        completed = server.delivery_snapshot()
    finally:
        sock.release_body.set()
        request_thread.join(timeout=1)
        server.stop()

    assert not request_thread.is_alive()
    assert completed.active_requests == 0
    assert completed.active_segment_requests == 0
    assert completed.oldest_active_segment_started_at is None
    assert completed.oldest_active_segment_age_s is None


def test_delivery_callback_failure_is_silent_and_does_not_break_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _FakeHTTPServer.instances.clear()
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", _FakeHTTPServer)
    segment_body = b"segment"
    (tmp_path / "segment.ts").write_bytes(segment_body)

    def fail_callback(_snapshot) -> None:
        raise RuntimeError("telemetry consumer failed")

    server = HLSHTTPServer(tmp_path, on_delivery=fail_callback)
    server.start()
    try:
        underlying = server._server
        assert underlying is not None
        headers, body = _request(underlying.handler, "/segment.ts")
    finally:
        server.stop()

    assert b"200 OK" in headers
    assert body == segment_body
    assert server.delivery_snapshot().completed_requests == 1
    assert capsys.readouterr() == ("", "")


def test_stop_closes_listener_and_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _FakeHTTPServer.instances.clear()
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", _FakeHTTPServer)
    server = HLSHTTPServer(tmp_path)
    server.start()
    port = server.port
    underlying = server._server

    assert port > 0
    assert underlying is not None

    server.stop()
    server.stop()

    assert underlying.socket.fileno() == -1
    assert underlying.shutdown_calls == 1
    assert underlying.close_calls == 1
    assert server.port == 0

    # Closing the listener, rather than merely stopping serve_forever(), makes
    # the same configured port immediately reusable by a replacement server.
    replacement = HLSHTTPServer(tmp_path, port)
    try:
        replacement.start()
        assert replacement.port == port
    finally:
        replacement.stop()


def test_stop_before_start_is_a_noop(tmp_path: Path):
    server = HLSHTTPServer(tmp_path)
    server.stop()
    server.stop()
    with pytest.raises(RuntimeError, match="already been started or stopped"):
        server.start()


class _FailingHTTPServer(_FakeHTTPServer):
    def serve_forever(self) -> None:
        raise OSError("listener failed")


def test_worker_failure_is_exposed_to_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", _FailingHTTPServer)
    server = HLSHTTPServer(tmp_path)
    server.start()
    assert server._finished.wait(timeout=1)

    with pytest.raises(OSError, match="listener failed"):
        server.raise_if_failed()

    underlying = server._server
    server.stop()
    assert underlying is not None
    assert underlying.close_calls == 1


class _BlockingConstructorHTTPServer(_FakeHTTPServer):
    entered = threading.Event()
    release = threading.Event()

    def __init__(self, address, handler) -> None:
        self.entered.set()
        self.release.wait(timeout=2)
        super().__init__(address, handler)


def test_stop_waits_for_concurrent_start_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _BlockingConstructorHTTPServer.entered.clear()
    _BlockingConstructorHTTPServer.release.clear()
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", _BlockingConstructorHTTPServer)
    server = HLSHTTPServer(tmp_path)
    stop_returned = threading.Event()

    start_thread = threading.Thread(target=server.start)
    stop_thread = threading.Thread(target=lambda: (server.stop(), stop_returned.set()))
    start_thread.start()
    assert _BlockingConstructorHTTPServer.entered.wait(timeout=1)
    stop_thread.start()
    assert not stop_returned.wait(timeout=0.05)

    _BlockingConstructorHTTPServer.release.set()
    start_thread.join(timeout=1)
    stop_thread.join(timeout=1)

    assert not start_thread.is_alive()
    assert not stop_thread.is_alive()
    assert stop_returned.is_set()
    assert server.port == 0


def test_duplicate_start_is_rejected_without_replacing_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", _FakeHTTPServer)
    server = HLSHTTPServer(tmp_path)
    server.start()
    underlying = server._server
    try:
        with pytest.raises(RuntimeError, match="already been started or stopped"):
            server.start()
        assert server._server is underlying
    finally:
        server.stop()
