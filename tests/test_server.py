"""HTTP server lifecycle regressions."""

import threading
from pathlib import Path

import pytest

import cast_tab.server as server_module
from cast_tab.server import HLSHTTPServer


class _FakeSocket:
    def __init__(self) -> None:
        self.fd = 42

    def fileno(self) -> int:
        return self.fd


class _FakeHTTPServer:
    instances: list["_FakeHTTPServer"] = []

    def __init__(self, address, _handler) -> None:
        self.server_port = address[1] or 54321
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


def test_stop_closes_listener_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
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


def test_worker_failure_is_exposed_to_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
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
    monkeypatch.setattr(
        server_module, "ThreadingHTTPServer", _BlockingConstructorHTTPServer
    )
    server = HLSHTTPServer(tmp_path)
    stop_returned = threading.Event()

    start_thread = threading.Thread(target=server.start)
    stop_thread = threading.Thread(
        target=lambda: (server.stop(), stop_returned.set())
    )
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
