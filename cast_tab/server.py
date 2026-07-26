"""LAN-facing HTTP server for the HLS work dir."""

from __future__ import annotations

import io
import ipaddress
import os
import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

SegmentEpochParser = Callable[[str], int | None]
PlaylistTransform = Callable[[bytes], bytes]
HLSRequestKind = Literal["playlist", "segment", "other"]

_SEGMENT_EPOCH_RE = re.compile(r"^seg-e(?P<epoch>[0-9]+)(?:[-_.]|$)")


@dataclass(frozen=True)
class HLSDeliverySnapshot:
    """Thread-safe point-in-time view of HLS HTTP delivery.

    Byte counters cover response bodies successfully written through the file
    delivery path (playlists and media segments), not HTTP headers. Wall-clock
    timestamps are Unix seconds; durations use a monotonic clock.
    """

    playlist_requests: int
    segment_requests: int
    other_requests: int
    active_requests: int
    active_segment_requests: int
    oldest_active_segment_started_at: float | None
    oldest_active_segment_age_s: float | None
    completed_requests: int
    error_requests: int
    bytes_sent: int
    playlist_bytes_sent: int
    segment_bytes_sent: int
    segment_responses: int
    segment_response_bytes_total: int
    segment_response_duration_s_total: float
    latest_request_method: str | None
    latest_request_path: str | None
    latest_request_kind: HLSRequestKind | None
    latest_request_started_at: float | None
    latest_request_completed_at: float | None
    latest_request_duration_s: float | None
    latest_request_status: int | None
    latest_request_bytes: int
    latest_request_error: bool
    latest_segment_started_at: float | None
    latest_segment_completed_at: float | None
    latest_segment_duration_s: float | None
    latest_segment_bytes: int
    latest_segment_throughput_bps: float | None

    @property
    def average_segment_response_duration_s(self) -> float | None:
        if self.segment_responses == 0:
            return None
        return self.segment_response_duration_s_total / self.segment_responses

    @property
    def average_segment_throughput_bps(self) -> float | None:
        if self.segment_response_duration_s_total <= 0:
            return None
        return self.segment_response_bytes_total / self.segment_response_duration_s_total


@dataclass(frozen=True)
class HLSClientDeliverySnapshot:
    """Successful media delivery attributable to one receiver address."""

    segment_requests: int
    active_segment_requests: int
    oldest_active_segment_age_s: float | None
    segment_responses: int
    latest_segment_completed_at: float | None


HLSDeliveryCallback = Callable[[HLSDeliverySnapshot], None]


@dataclass(frozen=True)
class ReceiverRoute:
    """Frozen LAN route and numeric peer aliases for one Chromecast."""

    local_ip: str
    peer_hosts: tuple[str, ...]


@dataclass(frozen=True)
class _RequestToken:
    sequence: int
    method: str
    path: str
    kind: HLSRequestKind
    client_host: str
    started_at: float
    started_monotonic: float


class _HLSDeliveryTelemetry:
    """Small lock-protected request ledger shared by handler threads."""

    def __init__(self, callback: HLSDeliveryCallback | None) -> None:
        self._callback = callback
        self._lock = threading.Lock()
        self._sequence = 0
        self._playlist_requests = 0
        self._segment_requests = 0
        self._other_requests = 0
        self._active_requests_by_kind: dict[
            HLSRequestKind,
            dict[int, _RequestToken],
        ] = {
            "playlist": {},
            "segment": {},
            "other": {},
        }
        self._completed_requests = 0
        self._error_requests = 0
        self._bytes_sent = 0
        self._playlist_bytes_sent = 0
        self._segment_bytes_sent = 0
        self._segment_responses = 0
        self._segment_response_bytes_total = 0
        self._segment_response_duration_s_total = 0.0
        self._latest_request_sequence = 0
        self._latest_request_method: str | None = None
        self._latest_request_path: str | None = None
        self._latest_request_kind: HLSRequestKind | None = None
        self._latest_request_started_at: float | None = None
        self._latest_request_completed_at: float | None = None
        self._latest_request_duration_s: float | None = None
        self._latest_request_status: int | None = None
        self._latest_request_bytes = 0
        self._latest_request_error = False
        self._latest_segment_started_at: float | None = None
        self._latest_segment_completed_at: float | None = None
        self._latest_segment_duration_s: float | None = None
        self._latest_segment_bytes = 0
        self._latest_segment_throughput_bps: float | None = None
        self._client_segment_requests: dict[str, int] = {}
        self._client_segment_responses: dict[str, int] = {}
        self._client_latest_segment_completed_at: dict[str, float] = {}

    def begin(
        self,
        method: str,
        path: str,
        kind: HLSRequestKind,
        client_host: str,
    ) -> _RequestToken:
        started_at = time.time()
        started_monotonic = time.monotonic()
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            token = _RequestToken(
                sequence=sequence,
                method=method,
                path=path,
                kind=kind,
                client_host=client_host,
                started_at=started_at,
                started_monotonic=started_monotonic,
            )
            if kind == "playlist":
                self._playlist_requests += 1
            elif kind == "segment":
                self._segment_requests += 1
                self._client_segment_requests[client_host] = (
                    self._client_segment_requests.get(client_host, 0) + 1
                )
            else:
                self._other_requests += 1
            self._active_requests_by_kind[kind][sequence] = token
            self._latest_request_sequence = sequence
            self._latest_request_method = method
            self._latest_request_path = path
            self._latest_request_kind = kind
            self._latest_request_started_at = started_at
            self._latest_request_completed_at = None
            self._latest_request_duration_s = None
            self._latest_request_status = None
            self._latest_request_bytes = 0
            self._latest_request_error = False
        return token

    def finish(
        self,
        token: _RequestToken,
        *,
        bytes_sent: int,
        status: int | None,
        failed: bool,
    ) -> None:
        completed_at = time.time()
        duration_s = max(0.0, time.monotonic() - token.started_monotonic)
        bytes_sent = max(0, bytes_sent)
        request_error = failed or status is None or status >= HTTPStatus.BAD_REQUEST
        with self._lock:
            self._active_requests_by_kind[token.kind].pop(token.sequence, None)
            self._completed_requests += 1
            if request_error:
                self._error_requests += 1
            self._bytes_sent += bytes_sent
            if token.kind == "playlist":
                self._playlist_bytes_sent += bytes_sent
            elif token.kind == "segment":
                self._segment_bytes_sent += bytes_sent
                # HEAD, 404s, and broken/partial transfers are requests, but
                # they are not evidence that media reached the receiver. Keep
                # successful body-delivery health separate from request/error
                # counters so probes cannot make a stalled cast look healthy.
                if bytes_sent > 0 and not request_error:
                    self._segment_responses += 1
                    self._segment_response_bytes_total += bytes_sent
                    self._segment_response_duration_s_total += duration_s
                    self._latest_segment_started_at = token.started_at
                    self._latest_segment_completed_at = completed_at
                    self._latest_segment_duration_s = duration_s
                    self._latest_segment_bytes = bytes_sent
                    self._latest_segment_throughput_bps = (
                        bytes_sent / duration_s if duration_s > 0 else None
                    )
                    host = token.client_host
                    self._client_segment_responses[host] = (
                        self._client_segment_responses.get(host, 0) + 1
                    )
                    self._client_latest_segment_completed_at[host] = completed_at

            # An earlier concurrent request must not overwrite the request that
            # most recently started. Its totals still contribute above.
            if token.sequence == self._latest_request_sequence:
                self._latest_request_completed_at = completed_at
                self._latest_request_duration_s = duration_s
                self._latest_request_status = status
                self._latest_request_bytes = bytes_sent
                self._latest_request_error = request_error
            snapshot = self._snapshot_locked()

        callback = self._callback
        if callback is not None:
            try:
                callback(snapshot)
            except Exception:
                # Telemetry is observational. A consumer must never break an
                # HLS response or create noisy request-thread tracebacks.
                pass

    def snapshot(self) -> HLSDeliverySnapshot:
        with self._lock:
            return self._snapshot_locked()

    def client_snapshot(self, client_host: str) -> HLSClientDeliverySnapshot:
        with self._lock:
            active = [
                token
                for token in self._active_requests_by_kind["segment"].values()
                if token.client_host == client_host
            ]
            oldest = min(active, key=lambda token: token.started_monotonic, default=None)
            return HLSClientDeliverySnapshot(
                segment_requests=self._client_segment_requests.get(client_host, 0),
                active_segment_requests=len(active),
                oldest_active_segment_age_s=(
                    max(0.0, time.monotonic() - oldest.started_monotonic)
                    if oldest is not None
                    else None
                ),
                segment_responses=self._client_segment_responses.get(client_host, 0),
                latest_segment_completed_at=self._client_latest_segment_completed_at.get(
                    client_host
                ),
            )

    def _snapshot_locked(self) -> HLSDeliverySnapshot:
        active_segments = self._active_requests_by_kind["segment"]
        oldest_active_segment = min(
            active_segments.values(),
            key=lambda token: token.started_monotonic,
            default=None,
        )
        return HLSDeliverySnapshot(
            playlist_requests=self._playlist_requests,
            segment_requests=self._segment_requests,
            other_requests=self._other_requests,
            active_requests=sum(
                len(active_requests) for active_requests in self._active_requests_by_kind.values()
            ),
            active_segment_requests=len(active_segments),
            oldest_active_segment_started_at=(
                oldest_active_segment.started_at if oldest_active_segment is not None else None
            ),
            oldest_active_segment_age_s=(
                max(0.0, time.monotonic() - oldest_active_segment.started_monotonic)
                if oldest_active_segment is not None
                else None
            ),
            completed_requests=self._completed_requests,
            error_requests=self._error_requests,
            bytes_sent=self._bytes_sent,
            playlist_bytes_sent=self._playlist_bytes_sent,
            segment_bytes_sent=self._segment_bytes_sent,
            segment_responses=self._segment_responses,
            segment_response_bytes_total=self._segment_response_bytes_total,
            segment_response_duration_s_total=self._segment_response_duration_s_total,
            latest_request_method=self._latest_request_method,
            latest_request_path=self._latest_request_path,
            latest_request_kind=self._latest_request_kind,
            latest_request_started_at=self._latest_request_started_at,
            latest_request_completed_at=self._latest_request_completed_at,
            latest_request_duration_s=self._latest_request_duration_s,
            latest_request_status=self._latest_request_status,
            latest_request_bytes=self._latest_request_bytes,
            latest_request_error=self._latest_request_error,
            latest_segment_started_at=self._latest_segment_started_at,
            latest_segment_completed_at=self._latest_segment_completed_at,
            latest_segment_duration_s=self._latest_segment_duration_s,
            latest_segment_bytes=self._latest_segment_bytes,
            latest_segment_throughput_bps=self._latest_segment_throughput_bps,
        )


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


def get_receiver_route(destination_host: str) -> ReceiverRoute:
    """Return the local IPv4 route and numeric aliases for one receiver.

    UDP ``connect`` only asks the kernel to select a route; it sends no packet.
    Routing toward a public DNS server chooses the wrong interface under many
    VPNs and fails entirely on internet-isolated LANs.
    """
    try:
        resolved = socket.getaddrinfo(
            destination_host,
            8009,
            family=socket.AF_INET,
            type=socket.SOCK_DGRAM,
        )
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((destination_host, 8009))
            local_ip = str(sock.getsockname()[0])
            routed_peer = str(sock.getpeername()[0])
    except OSError as exc:
        raise RuntimeError(
            "Could not determine the LAN interface used to reach Chromecast "
            f"{destination_host!r}. Check the TV route/VPN configuration."
        ) from exc
    if not local_ip or local_ip == "0.0.0.0":
        raise RuntimeError(
            f"No usable IPv4 route to Chromecast {destination_host!r}."
        )
    peer_hosts: list[str] = []
    for candidate in [routed_peer, *(str(item[4][0]) for item in resolved)]:
        try:
            normalized = str(ipaddress.IPv4Address(candidate))
        except ipaddress.AddressValueError:
            continue
        if normalized not in peer_hosts:
            peer_hosts.append(normalized)
    if not peer_hosts:
        raise RuntimeError(
            f"No usable IPv4 address resolved for Chromecast {destination_host!r}."
        )
    return ReceiverRoute(local_ip=local_ip, peer_hosts=tuple(peer_hosts))


def get_local_ip(destination_host: str) -> str:
    """Return the IPv4 address routed toward the selected Chromecast."""
    return get_receiver_route(destination_host).local_ip


class HLSHTTPServer:
    """Serve the HLS playlist + segments to the Chromecast."""

    def __init__(
        self,
        directory: Path,
        port: int = 0,
        *,
        playlist_transform: PlaylistTransform | None = None,
        playlist_name: str = "stream.m3u8",
        on_delivery: HLSDeliveryCallback | None = None,
    ) -> None:
        if Path(playlist_name).name != playlist_name or playlist_name in {"", ".", ".."}:
            raise ValueError("playlist_name must be a file name")
        self._directory = str(directory)
        self._requested_port = port
        self._playlist_transform = playlist_transform
        self._playlist_name = playlist_name
        self._delivery_telemetry = _HLSDeliveryTelemetry(on_delivery)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # Signal handlers can re-enter stop() on the interrupted start()/port
        # call in the same thread, so this ownership lock must be reentrant.
        self._stop_lock = threading.RLock()
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

    def delivery_snapshot(self) -> HLSDeliverySnapshot:
        """Return an immutable, thread-safe HTTP delivery snapshot."""
        return self._delivery_telemetry.snapshot()

    def client_delivery_snapshot(self, client_host: str) -> HLSClientDeliverySnapshot:
        """Return successful segment delivery for exactly one client address."""
        return self._delivery_telemetry.client_snapshot(client_host)

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
            delivery_telemetry = self._delivery_telemetry

            class Handler(SimpleHTTPRequestHandler):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, directory=serve_dir, **kwargs)

                def _request_path(self) -> str:
                    try:
                        return unquote(urlsplit(self.path).path)
                    except (TypeError, ValueError):
                        return str(self.path)

                def _request_kind(self, request_path: str) -> HLSRequestKind:
                    if request_path == f"/{playlist_name}":
                        return "playlist"
                    if request_path.lower().endswith(".ts"):
                        return "segment"
                    return "other"

                def _serve_with_telemetry(self, method: Callable[[], None]) -> None:
                    request_path = self._request_path()
                    token = delivery_telemetry.begin(
                        self.command,
                        request_path,
                        self._request_kind(request_path),
                        str(self.client_address[0]),
                    )
                    self._delivery_body_bytes = 0
                    self._delivery_status: int | None = None
                    failed = False
                    try:
                        method()
                    except BaseException:
                        failed = True
                        raise
                    finally:
                        delivery_telemetry.finish(
                            token,
                            bytes_sent=self._delivery_body_bytes,
                            status=self._delivery_status,
                            failed=failed,
                        )

                def do_GET(self) -> None:
                    self._serve_with_telemetry(super().do_GET)

                def do_HEAD(self) -> None:
                    self._serve_with_telemetry(super().do_HEAD)

                def send_response(
                    self,
                    code: int,
                    message: str | None = None,
                ) -> None:
                    self._delivery_status = code
                    super().send_response(code, message)

                def copyfile(self, source, outputfile) -> None:
                    handler = self

                    class CountingOutput:
                        def write(self, data):
                            written = outputfile.write(data)
                            handler._delivery_body_bytes += (
                                len(data) if written is None else max(0, written)
                            )
                            return written

                        def __getattr__(self, name):
                            return getattr(outputfile, name)

                    super().copyfile(source, CountingOutput())

                def send_head(self):
                    request_path = self._request_path()
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
                if (
                    self._lifecycle_state != "starting"
                    or self._stop_requested.is_set()
                ):
                    server.server_close()
                    self._lifecycle_state = "stopped"
                    return
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
