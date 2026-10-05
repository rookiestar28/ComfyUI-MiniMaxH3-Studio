"""Fixed-source HTTPS download of the managed media runtime archive.

This transport fetches exactly one kind of object: the pinned release archive named by the
installer manifest. It accepts the manifest's `https://github.com/...` URL and follows at most a
few redirects, each only to the GitHub release-asset hosts. Every hop resolves through the
repository's killable resolver, refuses non-global addresses, and connects to that one validated
address with a default verifying TLS context. No proxy, cookie, credential, prompt or media is
ever sent, and nothing from a response -- including the signed redirect URL -- is logged, returned
or persisted.

The body is streamed into a caller-supplied sink in bounded chunks. Hashing and storage belong to
the installer; this module only guarantees that exactly `expected_bytes` arrived from an admitted
origin, or raises a closed, content-free `DownloadError`.
"""

from __future__ import annotations

import contextlib
import http.client
import socket
import ssl
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..core.prompt_model_provider import PromptModelOutcomeId
from .prompt_model_transport import PromptModelTransportError, resolve_pinned_address_bounded

INITIAL_HOST = "github.com"
ASSET_HOSTS = frozenset({"release-assets.githubusercontent.com", "objects.githubusercontent.com"})
HTTPS_PORT = 443
MAX_REDIRECTS = 3
MAX_LOCATION_CHARS = 8 * 1024
CHUNK_BYTES = 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 15.0
READ_IDLE_TIMEOUT_SECONDS = 60.0
DNS_TIMEOUT_SECONDS = 10.0
DOWNLOAD_DEADLINE_SECONDS = 3600.0
USER_AGENT = "ComfyUI-MinimaxH3-Context-media-setup/1"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_UNAVAILABLE_STATUSES = frozenset({404, 410})


class DownloadError(RuntimeError):
    """Closed, content-free download failure."""

    CODES = frozenset(
        {
            "cancelled",
            "download_failed",
            "download_timeout",
            "egress_refused",
            "network_timeout",
            "network_unavailable",
            "redirect_refused",
            "size_mismatch",
            "source_unavailable",
            "tls_failed",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self.CODES:
            raise ValueError("unknown download error code")
        self.code = code
        super().__init__(code)


class DownloadPort(Protocol):
    def download(
        self,
        url: str,
        *,
        expected_bytes: int,
        sink: Callable[[bytes], None],
        cancelled: threading.Event,
        progress: Callable[[int], None],
    ) -> None: ...

    def abort(self) -> None: ...


class _Target:
    __slots__ = ("host", "target")

    def __init__(self, host: str, target: str) -> None:
        self.host = host
        self.target = target


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 0x21 or ord(character) == 0x7F for character in value)


def _admit_https(url: object, allowed_hosts: frozenset[str]) -> _Target:
    """Admit one absolute https URL on port 443 whose host is exactly one of `allowed_hosts`."""

    if type(url) is not str or not url or len(url) > MAX_LOCATION_CHARS:
        raise DownloadError("redirect_refused")
    if _has_control_characters(url) or "\\" in url:
        raise DownloadError("redirect_refused")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise DownloadError("redirect_refused") from exc
    host = parts.hostname
    if (
        parts.scheme != "https"
        or "@" in parts.netloc
        or host is None
        or host not in allowed_hosts
        or parts.netloc.lower() not in {host, f"{host}:{HTTPS_PORT}"}
        or port not in {None, HTTPS_PORT}
        or not parts.path.startswith("/")
    ):
        raise DownloadError("redirect_refused")
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    return _Target(host, target)


def admit_source_url(url: object) -> _Target:
    """The manifest URL itself must name the initial release host."""

    return _admit_https(url, frozenset({INITIAL_HOST}))


def admit_redirect(location: object, *, from_host: str) -> _Target:
    """Admit one redirect hop.

    SECURITY: only the initial release host may redirect, and only to a release-asset host. An
    asset host that redirects again, a relative `Location`, another scheme or port, userinfo, or a
    look-alike host is refused rather than followed. Widening this set is how a compromised or
    spoofed hop turns a pinned archive download into a fetch from anywhere.
    """

    if from_host != INITIAL_HOST:
        raise DownloadError("redirect_refused")
    return _admit_https(location, ASSET_HOSTS)


def _content_length(response: Any) -> int | None:
    values = response.msg.get_all("Content-Length") if response.msg is not None else None
    if not values:
        return None
    if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
        raise DownloadError("download_failed")
    return int(values[0])


def _map_resolution(exc: PromptModelTransportError) -> DownloadError:
    if exc.outcome_id is PromptModelOutcomeId.TIMEOUT:
        return DownloadError("network_timeout")
    if exc.outcome_id is PromptModelOutcomeId.EGRESS_REFUSED:
        return DownloadError("egress_refused")
    return DownloadError("network_unavailable")


def _transport_failure(exc: BaseException, cancelled: threading.Event) -> DownloadError:
    if cancelled.is_set():
        return DownloadError("cancelled")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return DownloadError("network_timeout")
    if isinstance(exc, ssl.SSLError):
        return DownloadError("tls_failed")
    if isinstance(exc, http.client.HTTPException):
        return DownloadError("download_failed")
    return DownloadError("network_unavailable")


class HttpsArchiveDownloader:
    """One download at a time; `abort` may be called from any thread."""

    def __init__(
        self,
        *,
        address_resolver: Callable[[str], str] | None = None,
        connection_factory: Callable[[str, int, float], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        deadline_seconds: float = DOWNLOAD_DEADLINE_SECONDS,
    ) -> None:
        self._resolver = address_resolver
        self._connection_factory = connection_factory
        self._clock = clock
        self._deadline_seconds = deadline_seconds
        self._lock = threading.Lock()
        self._connection: Any = None
        self._tls_context: ssl.SSLContext | None = None

    def __repr__(self) -> str:
        return "<HttpsArchiveDownloader>"

    def abort(self) -> None:
        with self._lock:
            connection = self._connection
        sock = getattr(connection, "sock", None)
        if sock is not None:
            try:
                # Shutting the socket down, not closing the connection object, is what releases a
                # read blocked in another thread without racing that thread's own cleanup.
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _open(self, host: str, deadline: float) -> Any:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise DownloadError("download_timeout")
        if self._connection_factory is not None:
            return self._connection_factory(host, HTTPS_PORT, CONNECT_TIMEOUT_SECONDS)
        if self._resolver is None:
            try:
                # CRITICAL: socket timeouts do not bound platform DNS; the killable resolver does,
                # and it refuses any answer that is not globally routable.
                pinned = resolve_pinned_address_bounded(
                    host,
                    timeout_seconds=min(DNS_TIMEOUT_SECONDS, remaining),
                    loopback_required=False,
                )
            except PromptModelTransportError as exc:
                raise _map_resolution(exc) from exc
        else:
            pinned = self._resolver(host)
        if self._tls_context is None:
            self._tls_context = ssl.create_default_context()
        connection = http.client.HTTPSConnection(
            host, HTTPS_PORT, timeout=CONNECT_TIMEOUT_SECONDS, context=self._tls_context
        )
        # The hostname stays on the connection for SNI and certificate verification while the
        # socket goes to the one address that was validated.
        connection._create_connection = (  # type: ignore[attr-defined]
            lambda address, connect_timeout, source: socket.create_connection(
                (pinned, address[1]), connect_timeout, source
            )
        )
        return connection

    def download(
        self,
        url: str,
        *,
        expected_bytes: int,
        sink: Callable[[bytes], None],
        cancelled: threading.Event,
        progress: Callable[[int], None],
    ) -> None:
        if type(expected_bytes) is not int or expected_bytes <= 0:
            raise ValueError("expected_bytes")
        deadline = self._clock() + self._deadline_seconds
        target = admit_source_url(url)
        for _hop in range(MAX_REDIRECTS + 1):
            if cancelled.is_set():
                raise DownloadError("cancelled")
            # Name resolution itself is not interruptible; it is bounded by DNS_TIMEOUT_SECONDS.
            connection = self._open(target.host, deadline)
            with self._lock:
                self._connection = connection
            try:
                # IMPORTANT: a cancel whose `abort` ran while the address was still resolving found
                # no socket to shut down. Re-checking only after registering the connection closes
                # that window; checking before registering would reopen it.
                if cancelled.is_set():
                    raise DownloadError("cancelled")
                next_target = self._exchange(
                    connection, target, expected_bytes, sink, cancelled, progress, deadline
                )
            finally:
                with self._lock:
                    self._connection = None
                # Closing a failed connection is best effort.
                with contextlib.suppress(Exception):
                    connection.close()
            if next_target is None:
                return
            target = next_target
        raise DownloadError("redirect_refused")

    def _exchange(
        self,
        connection: Any,
        target: _Target,
        expected_bytes: int,
        sink: Callable[[bytes], None],
        cancelled: threading.Event,
        progress: Callable[[int], None],
        deadline: float,
    ) -> _Target | None:
        try:
            connection.connect()
            if cancelled.is_set():
                raise DownloadError("cancelled")
            sock = getattr(connection, "sock", None)
            if sock is not None:
                sock.settimeout(READ_IDLE_TIMEOUT_SECONDS)
            connection.putrequest("GET", target.target, skip_accept_encoding=True)
            connection.putheader("User-Agent", USER_AGENT)
            connection.putheader("Accept", "application/octet-stream")
            connection.putheader("Accept-Encoding", "identity")
            connection.putheader("Connection", "close")
            connection.endheaders()
            response = connection.getresponse()
            status = response.status
            if status in _REDIRECT_STATUSES:
                locations = response.msg.get_all("Location") if response.msg is not None else None
                if not locations or len(locations) != 1:
                    raise DownloadError("redirect_refused")
                return admit_redirect(locations[0], from_host=target.host)
            if status in _UNAVAILABLE_STATUSES:
                raise DownloadError("source_unavailable")
            if status != 200:
                raise DownloadError("download_failed")
            encoding = response.getheader("Content-Encoding")
            if encoding not in {None, "identity"}:
                raise DownloadError("download_failed")
            declared = _content_length(response)
            if declared is not None and declared != expected_bytes:
                raise DownloadError("size_mismatch")
        except DownloadError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise _transport_failure(exc, cancelled) from exc
        total = 0
        while True:
            if cancelled.is_set():
                raise DownloadError("cancelled")
            if self._clock() > deadline:
                raise DownloadError("download_timeout")
            try:
                chunk = response.read(min(CHUNK_BYTES, expected_bytes + 1 - total))
            except (OSError, http.client.HTTPException, ValueError) as exc:
                raise _transport_failure(exc, cancelled) from exc
            if not chunk:
                break
            total += len(chunk)
            if total > expected_bytes:
                raise DownloadError("size_mismatch")
            # The sink is called outside the transport `try`: a disk-full or unsafe-store failure
            # raised there is the installer's own typed error, not a network outcome.
            sink(chunk)
            progress(total)
        if cancelled.is_set():
            raise DownloadError("cancelled")
        if total != expected_bytes:
            raise DownloadError("size_mismatch")
        return None


__all__ = [
    "ASSET_HOSTS",
    "DOWNLOAD_DEADLINE_SECONDS",
    "INITIAL_HOST",
    "MAX_REDIRECTS",
    "DownloadError",
    "DownloadPort",
    "HttpsArchiveDownloader",
    "admit_redirect",
    "admit_source_url",
]
