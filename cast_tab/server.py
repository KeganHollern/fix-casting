"""LAN-facing HTTP server for the HLS work dir."""

from __future__ import annotations

import socket
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def get_local_ip() -> str:
    """Return the LAN IP address used for outbound traffic."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]


class HLSHTTPServer:
    """Serve the HLS playlist + segments to the Chromecast."""

    def __init__(self, directory: Path, port: int = 0) -> None:
        self._directory = str(directory)
        self._requested_port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop_lock = threading.Lock()
        self._health_lock = threading.Lock()
        self._failure: BaseException | None = None
        self._finished = threading.Event()
        self._stop_requested = threading.Event()
        self._lifecycle_state = "new"

    @property
    def port(self) -> int:
        with self._stop_lock:
            server = self._server
            return server.server_port if server else 0

    def _serve(self, server: ThreadingHTTPServer) -> None:
        failure: BaseException | None = None
        try:
            server.serve_forever()
        except BaseException as exc:
            failure = exc
        finally:
            if not self._stop_requested.is_set():
                if failure is None:
                    failure = RuntimeError("HLS HTTP server exited unexpectedly.")
                with self._health_lock:
                    if self._failure is None:
                        self._failure = failure
            self._finished.set()

    def raise_if_failed(self) -> None:
        """Raise an unexpected HTTP worker failure in the owning thread."""
        with self._health_lock:
            failure = self._failure
        if failure is not None:
            raise failure

    def start(self) -> None:
        # Binding, publication, and thread start are one lifecycle transaction
        # against stop(); stop can never return before a concurrent start has
        # either failed or published resources that stop then closes.
        with self._stop_lock:
            if self._lifecycle_state != "new":
                raise RuntimeError("HLS HTTP server has already been started or stopped.")
            self._lifecycle_state = "starting"
            serve_dir = self._directory

            class Handler(SimpleHTTPRequestHandler):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, directory=serve_dir, **kwargs)

                def log_message(self, _format: str, *_args) -> None:
                    pass

                def end_headers(self) -> None:
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    super().end_headers()

            server: ThreadingHTTPServer | None = None
            try:
                server = ThreadingHTTPServer(
                    ("0.0.0.0", self._requested_port), Handler
                )
                thread = threading.Thread(
                    target=self._serve,
                    args=(server,),
                    name="hls-http",
                    daemon=True,
                )
                self._server = server
                self._thread = thread
                thread.start()
            except BaseException:
                if server is not None:
                    server.server_close()
                self._server = None
                self._thread = None
                self._lifecycle_state = "stopped"
                raise
            self._lifecycle_state = "running"

    def stop(self) -> None:
        # shutdown() stops serve_forever but deliberately does not release the
        # listening socket.  Always pair it with server_close(), and serialize
        # repeated calls so stop is safe and fully complete when it returns.
        with self._stop_lock:
            if self._lifecycle_state == "stopped":
                return
            self._lifecycle_state = "stopping"
            self._stop_requested.set()
            server = self._server
            thread = self._thread
            if server is None:
                self._lifecycle_state = "stopped"
                return
            try:
                # BaseServer.shutdown() deadlocks if serve_forever() was never
                # entered, so only call it while our server thread is alive.
                if thread is not None and thread.is_alive():
                    server.shutdown()
                    if thread is not threading.current_thread():
                        thread.join(timeout=3)
            finally:
                server.server_close()
                self._server = None
                self._thread = None
                self._lifecycle_state = "stopped"
