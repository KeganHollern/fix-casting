"""LAN-facing HTTP server for the HLS work dir."""

from __future__ import annotations

import io
import os
import re
import socket
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

SegmentEpochParser = Callable[[str], int | None]
PlaylistTransform = Callable[[bytes], bytes]

_SEGMENT_EPOCH_RE = re.compile(r"^seg-e(?P<epoch>[0-9]+)(?:[-_.]|$)")


def parse_hls_segment_epoch(uri: str) -> int | None:
    """Return the timeline epoch from a generation-qualified segment URI.

    The streamer names segments ``seg-e<epoch>-...``.  The attempt-specific
    suffix may change after a failed encoder spawn, while the epoch identifies
    the HLS discontinuity sequence to which the published segment belongs.
    Query strings and parent paths do not affect the parsed basename.
    """
    try:
        name = unquote(urlsplit(uri).path).rsplit("/", 1)[-1]
    except (TypeError, ValueError):
        return None
    match = _SEGMENT_EPOCH_RE.match(name)
    return int(match.group("epoch")) if match is not None else None


class HLSDiscontinuitySequenceNormalizer:
    """Synthesize a receiver-safe discontinuity-sequence header.

    FFmpeg preserves ``#EXT-X-DISCONTINUITY`` while its following segment is
    retained, but does not emit ``#EXT-X-DISCONTINUITY-SEQUENCE`` after that
    boundary scrolls out of a live playlist.  Segment timeline epochs provide
    the missing durable count.  This callable transforms only valid playlists
    whose URI epochs agree with their visible discontinuity tags; unrecognized
    input is returned byte-for-byte unchanged.
    """

    def __init__(
        self,
        segment_epoch: SegmentEpochParser = parse_hls_segment_epoch,
    ) -> None:
        self._segment_epoch = segment_epoch

    def __call__(self, playlist: bytes) -> bytes:
        return self.normalize(playlist)

    def normalize(self, playlist: bytes) -> bytes:
        try:
            text = playlist.decode("utf-8")
            return self._normalize_text(text).encode("utf-8")
        except (AttributeError, UnicodeError, ValueError, TypeError):
            return playlist

    def _normalize_text(self, text: str) -> str:
        lines = text.splitlines()
        if not lines or lines[0] != "#EXTM3U":
            raise ValueError("not an HLS playlist")

        # Existing values may be stale after FFmpeg has rolled a boundary out.
        # Remove them before validating and inserting the epoch-derived value.
        dsn_prefix = "#EXT-X-DISCONTINUITY-SEQUENCE:"
        if any(
            line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE") and not line.startswith(dsn_prefix)
            for line in lines
        ):
            raise ValueError("malformed discontinuity sequence")
        lines = [line for line in lines if not line.startswith(dsn_prefix)]

        media_prefix = "#EXT-X-MEDIA-SEQUENCE:"
        media_indexes = [index for index, line in enumerate(lines) if line.startswith(media_prefix)]
        if len(media_indexes) != 1:
            raise ValueError("expected one media sequence")
        media_index = media_indexes[0]
        if int(lines[media_index][len(media_prefix) :]) < 0:
            raise ValueError("negative media sequence")

        uri_indexes = [
            index for index, line in enumerate(lines) if line and not line.startswith("#")
        ]
        if not uri_indexes or media_index >= uri_indexes[0]:
            raise ValueError("playlist has no ordered media segment")
        first_uri_index = uri_indexes[0]

        discontinuity = "#EXT-X-DISCONTINUITY"
        visible_before_first = sum(line == discontinuity for line in lines[:first_uri_index])
        # Inserting after MEDIA-SEQUENCE must also put the synthesized header
        # before every discontinuity, as required by HLS.
        if any(line == discontinuity for line in lines[: media_index + 1]):
            raise ValueError("discontinuity precedes media sequence")

        first_epoch = self._parse_epoch(lines[first_uri_index])
        sequence = first_epoch - visible_before_first
        if sequence < 0:
            raise ValueError("segment epoch predates visible discontinuities")

        # Validate the whole retained window.  A tag advances the sequence for
        # the next URI; a rolled-out tag is represented solely by `sequence`.
        current = sequence
        for line in lines:
            if line == discontinuity:
                current += 1
            elif line and not line.startswith("#"):
                if self._parse_epoch(line) != current:
                    raise ValueError("segment epoch does not match discontinuities")

        lines.insert(
            media_index + 1,
            f"#EXT-X-DISCONTINUITY-SEQUENCE:{sequence}",
        )
        newline = "\r\n" if "\r\n" in text else "\n"
        trailing_newline = text.endswith(("\r", "\n"))
        normalized = newline.join(lines)
        return normalized + newline if trailing_newline else normalized

    def _parse_epoch(self, uri: str) -> int:
        try:
            epoch = self._segment_epoch(uri)
        except Exception as exc:
            raise ValueError("segment epoch parser failed") from exc
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise ValueError("segment URI has no valid timeline epoch")
        return epoch


def get_local_ip() -> str:
    """Return the LAN IP address used for outbound traffic."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]


class HLSHTTPServer:
    """Serve the HLS playlist + segments to the Chromecast."""

    def __init__(
        self,
        directory: Path,
        port: int = 0,
        *,
        playlist_transform: PlaylistTransform | None = None,
        playlist_name: str = "stream.m3u8",
    ) -> None:
        if Path(playlist_name).name != playlist_name or playlist_name in {"", ".", ".."}:
            raise ValueError("playlist_name must be a file name")
        self._directory = str(directory)
        self._requested_port = port
        self._playlist_transform = playlist_transform
        self._playlist_name = playlist_name
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
            playlist_transform = self._playlist_transform
            playlist_name = self._playlist_name
            playlist_path = Path(serve_dir) / playlist_name

            class Handler(SimpleHTTPRequestHandler):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, directory=serve_dir, **kwargs)

                def send_head(self):
                    request_path = unquote(urlsplit(self.path).path)
                    if playlist_transform is None or request_path != f"/{playlist_name}":
                        return super().send_head()

                    try:
                        # FFmpeg publishes live file playlists by atomic rename.
                        # Reading through one open descriptor therefore yields
                        # one complete old-or-new version, never a mixed one.
                        with playlist_path.open("rb") as source:
                            raw = source.read()
                            modified_at = os.fstat(source.fileno()).st_mtime
                    except OSError:
                        return super().send_head()

                    try:
                        body = playlist_transform(raw)
                        if not isinstance(body, bytes):
                            body = raw
                    except Exception:
                        # Availability is preferable to crashing a request
                        # thread because an optional synthesizer rejected input.
                        body = raw

                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Last-Modified", self.date_time_string(modified_at))
                    self.end_headers()
                    return io.BytesIO(body)

                def log_message(self, _format: str, *_args) -> None:
                    pass

                def end_headers(self) -> None:
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    super().end_headers()

            server: ThreadingHTTPServer | None = None
            try:
                server = ThreadingHTTPServer(("0.0.0.0", self._requested_port), Handler)
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
