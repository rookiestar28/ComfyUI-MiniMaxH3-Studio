"""Explicit loopback Ollama native-API fallback for M10-03.

This module is optional and never imported by the pure core. It exposes a narrow transport and
adapter: only version/tags/show/ps/chat/generate paths are accepted, redirects and credentials are
forbidden, and a caller must explicitly provide an approved manifest plus a successful preflight.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import http.client
import ipaddress
import json
import re
import select
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from math import isfinite
from typing import Protocol, TypeGuard, cast
from urllib.parse import SplitResult, urlsplit

from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.contracts import MediaKind, TaskMode
from comfyui_h3_context.core.errors import (
    ContractValidationError,
    LocalAdapterCancelledError,
    LocalAdapterCapabilityError,
    LocalAdapterTimeoutError,
    ModelManifestError,
    ModelOutputError,
    ModelTransportError,
    SecurityPolicyError,
)
from comfyui_h3_context.core.local_adapters import (
    LocalAdapter,
    LocalAdapterDescriptor,
    LocalAdapterExecutionRequest,
    LocalAdapterKind,
    LocalAdapterResult,
    LocalBudgetGuard,
    LocalCancellationProbe,
    LocalDeviceKind,
)
from comfyui_h3_context.core.model_manifest import (
    ModelBackendFamily,
    ModelCapability,
    ModelGenerationRequest,
    ModelGenerationResult,
    ModelManifest,
    ModelProbeReport,
    ModelProbeStatus,
    OllamaModelObservation,
    OllamaProcessObservation,
    OllamaServerObservation,
    build_model_result,
    qualify_ollama_manifest,
    validate_model_generation_request,
)
from comfyui_h3_context.core.prompt_model_dialects.discovery import read_native_models
from comfyui_h3_context.core.prompt_model_provider import (
    MAX_DISCOVERY_ROWS,
    PromptModelContractError,
    PromptModelFamily,
    parse_exact_tags_row,
    read_ollama_choice_metadata,
)
from comfyui_h3_context.core.provider_setup import (
    ProviderConsentAuthority,
    ProviderLocalBackend,
    ProviderPreflightReceipt,
    ProviderPreflightStatus,
    ProviderSetup,
    admit_provider_execution,
    build_ollama_provider_preflight,
    build_provider_consent_authority,
)
from comfyui_h3_context.core.semantic_proposal_producer import (
    FIXED_OLLAMA_ENDPOINT,
    SemanticChosenModelProfile,
    SemanticExecutionProfile,
    SemanticProposalProducerError,
    SemanticProviderConnection,
    SemanticProviderProfile,
    ollama_native_digest_to_model_digest,
    parse_ollama_native_digest,
)

OLLAMA_ALLOWED_PATHS = frozenset(
    {"/api/version", "/api/tags", "/api/show", "/api/ps", "/api/chat", "/api/generate"}
)
OLLAMA_TRANSPORT_SCHEMA = "h3.ollama.transport.v1"
MAX_OLLAMA_REQUEST_BYTES = 1_000_000
MAX_OLLAMA_RESPONSE_BYTES = 4_000_000


class OllamaTransport(Protocol):
    """Bounded native API transport implemented by the explicit client or a test double."""

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        cancellation: LocalCancellationProbe | None = None,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]: ...


class SemanticOllamaTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        cancellation: LocalCancellationProbe | None = None,
        absolute_deadline: float,
        phase_timeout_seconds: float,
    ) -> Mapping[str, object]: ...


def _endpoint_parts(endpoint: str) -> tuple[SplitResult, str, int]:
    if type(endpoint) is not str or len(endpoint) > 256:
        raise ModelTransportError("Ollama endpoint is not bounded")
    # CRITICAL: urlsplit normalizes surrounding controls; reject them before parsing authority.
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in endpoint
    ):
        raise ModelTransportError("Ollama endpoint must use an exact bounded spelling")
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        parsed = None
    if parsed is None:
        raise ModelTransportError("Ollama endpoint syntax is invalid")
    if parsed.scheme != "http" or parsed.username is not None or parsed.password is not None:
        raise ModelTransportError("Ollama endpoint must be credential-free loopback HTTP")
    if parsed.query or parsed.fragment:
        raise ModelTransportError("Ollama endpoint must not contain query or fragment data")
    if parsed.path not in {"", "/", "/api"}:
        raise ModelTransportError("Ollama endpoint base path is not allowlisted")
    host = parsed.hostname
    if host is None or not host or host.casefold() in {"0.0.0.0", "::", "[::]"}:  # noqa: S104 - reject wildcard destinations
        raise ModelTransportError("Ollama endpoint host must be explicit loopback")
    try:
        host_address = ipaddress.ip_address(host)
    except ValueError:
        host_address = None
    if host_address is None:
        raise ModelTransportError("Ollama endpoint host must be a loopback literal")
    if not host_address.is_loopback:
        raise ModelTransportError("Ollama endpoint host is not loopback")
    invalid_port = False
    try:
        parsed_port = parsed.port
    except ValueError:
        parsed_port = None
        invalid_port = True
    if invalid_port:
        raise ModelTransportError("Ollama endpoint port is invalid")
    # CRITICAL: only a truly omitted port may inherit the Ollama default.
    if parsed_port is None:
        if parsed.netloc.endswith(":"):
            raise ModelTransportError("Ollama endpoint port is empty")
        port = 11434
    else:
        port = parsed_port
    if not 1 <= port <= 65_535:
        raise ModelTransportError("Ollama endpoint port is outside the valid range")
    return parsed, host, port


class LoopbackOllamaTransport:
    """Small stdlib client with a closed native endpoint and no redirect behavior."""

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:11434/api",
        *,
        timeout_seconds: float = 15.0,
        max_response_bytes: int = MAX_OLLAMA_RESPONSE_BYTES,
    ) -> None:
        parsed, host, port = _endpoint_parts(endpoint)
        if (
            type(timeout_seconds) not in {int, float}
            or not isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
            or timeout_seconds > 300
        ):
            raise ModelTransportError("Ollama timeout is outside the finite range")
        if (
            type(max_response_bytes) is not int
            or max_response_bytes <= 0
            or max_response_bytes > MAX_OLLAMA_RESPONSE_BYTES
        ):
            raise ModelTransportError("Ollama response limit is outside the finite range")
        self._host = host
        self._port = port
        self._timeout_seconds = float(timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._endpoint_fingerprint = canonical_fingerprint(
            {"scheme": parsed.scheme, "host": host.casefold(), "port": port, "path": "/api"}
        )

    @property
    def endpoint_fingerprint(self) -> str:
        return self._endpoint_fingerprint

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        cancellation: LocalCancellationProbe | None = None,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        if (
            type(method) is not str
            or type(path) is not str
            or method not in {"GET", "POST"}
            or path not in OLLAMA_ALLOWED_PATHS
        ):
            raise ModelTransportError("Ollama method/path is not allowlisted")
        if cancellation is not None and not isinstance(cancellation, LocalCancellationProbe):
            raise ModelTransportError("Ollama cancellation probe is invalid")
        if cancellation is not None and cancellation.is_cancelled():
            raise LocalAdapterCancelledError("Ollama request was cancelled before connect")
        request_timeout = self._timeout_seconds
        if timeout_seconds is not None:
            if (
                type(timeout_seconds) not in {int, float}
                or not isfinite(float(timeout_seconds))
                or not 0 < float(timeout_seconds) <= 30
            ):
                raise ModelTransportError("Ollama request timeout is outside the finite range")
            request_timeout = min(request_timeout, float(timeout_seconds))
        body: bytes | None = None
        if payload is not None:
            if not isinstance(payload, Mapping):
                raise ModelTransportError("Ollama request payload must be an object")
            try:
                body = json.dumps(
                    payload,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ModelTransportError("Ollama request payload is not bounded JSON") from exc
            if len(body) > MAX_OLLAMA_REQUEST_BYTES:
                raise ModelTransportError("Ollama request exceeds the bounded byte limit")
        connection: http.client.HTTPConnection | None = None
        try:
            connection = http.client.HTTPConnection(self._host, self._port, timeout=request_timeout)
            connection.request(
                method,
                path,
                body=body,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            if response.status != 200:
                raise ModelTransportError("Ollama returned a non-success status")
            raw = response.read(self._max_response_bytes + 1)
            if len(raw) > self._max_response_bytes:
                raise ModelTransportError("Ollama response exceeds the bounded byte limit")
            try:
                value = json.loads(
                    raw.decode("utf-8"),
                    object_pairs_hook=_reject_duplicate_json_members,
                    parse_constant=_reject_non_finite_json,
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ModelTransportError("Ollama response is not bounded JSON") from exc
            if not isinstance(value, Mapping):
                raise ModelTransportError("Ollama response must be a JSON object")
            if cancellation is not None and cancellation.is_cancelled():
                raise LocalAdapterCancelledError("Ollama request was cancelled after response")
            return cast(Mapping[str, object], value)
        except (ModelTransportError, LocalAdapterCancelledError):
            raise
        except TimeoutError as exc:
            raise LocalAdapterTimeoutError("Ollama loopback transport timed out") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise ModelTransportError("Ollama loopback transport failed") from exc
        finally:
            if connection is not None:
                connection.close()


_SEMANTIC_HTTP_HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_SEMANTIC_HTTP_CONTENT_LENGTH = re.compile(rb"0|[1-9][0-9]{0,6}")
_SEMANTIC_HTTP_CHUNK_SIZE = re.compile(rb"0|[1-9A-Fa-f][0-9A-Fa-f]{0,5}")
_SEMANTIC_HTTP_ALLOWED_HEADERS = frozenset(
    {"content-type", "date", "content-length", "transfer-encoding", "connection"}
)
_SEMANTIC_HTTP_CONTENT_TYPES = frozenset({"application/json", "application/json; charset=utf-8"})
_SEMANTIC_HTTP_PATH_LIMITS = {
    "/api/version": 30.0,
    "/api/tags": 30.0,
    "/api/show": 30.0,
    "/api/ps": 30.0,
    "/api/chat": 120.0,
    "/api/generate": 5.0,
}
_SEMANTIC_HTTP_METHODS = {
    "/api/version": "GET",
    "/api/tags": "GET",
    "/api/show": "POST",
    "/api/ps": "GET",
    "/api/chat": "POST",
    "/api/generate": "POST",
}
_SEMANTIC_HTTP_RESPONSE_BYTES = 1_048_576
_SEMANTIC_HTTP_HEADER_BYTES = 16_384
_SEMANTIC_HTTP_HEADER_LINE_BYTES = 4_096
_SEMANTIC_HTTP_STATUS_LINE_BYTES = 64
_SEMANTIC_HTTP_REQUEST_LINE_BYTES = 64
_SEMANTIC_HTTP_CHUNK_LINE_BYTES = 8
_SEMANTIC_HTTP_HEADER_COUNT = 32
_SEMANTIC_HTTP_CANCEL_POLL_SECONDS = 0.1
_SEMANTIC_SHOW_TENSOR_ITEMS = 4_096
_SEMANTIC_SHOW_TENSOR_DIMENSIONS = 8
_SEMANTIC_SHOW_TENSOR_DIMENSION_MAX = 16_777_216
_SEMANTIC_CONNECT_PENDING = frozenset(
    {
        errno.EINPROGRESS,
        errno.EWOULDBLOCK,
        errno.EALREADY,
        10035,
        10036,
        10037,
    }
)


class SemanticDeadlineOllamaTransport:
    """Exact cumulative-deadline HTTP/1.1 transport for the M17-16 semantic lane."""

    def __init__(self, expected_model_id: str) -> None:
        if (
            type(expected_model_id) is not str
            or not expected_model_id
            or len(expected_model_id) > 256
        ):
            raise ModelTransportError("semantic_http_request")
        self._expected_model_id = expected_model_id
        self._last_request_body_bytes = 0
        self._last_response_body_bytes = 0
        self._endpoint_fingerprint = canonical_fingerprint(
            {"scheme": "http", "host": "127.0.0.1", "port": 11434, "path": "/api"}
        )

    @property
    def last_request_body_bytes(self) -> int:
        return self._last_request_body_bytes

    @property
    def last_response_body_bytes(self) -> int:
        return self._last_response_body_bytes

    @property
    def endpoint_fingerprint(self) -> str:
        return self._endpoint_fingerprint

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        cancellation: LocalCancellationProbe | None = None,
        absolute_deadline: float,
        phase_timeout_seconds: float,
    ) -> Mapping[str, object]:
        now = time.monotonic()
        phase_limit = _SEMANTIC_HTTP_PATH_LIMITS.get(path)
        if (
            type(method) is not str
            or type(path) is not str
            or phase_limit is None
            or _SEMANTIC_HTTP_METHODS[path] != method
            or type(absolute_deadline) not in {int, float}
            or not isfinite(float(absolute_deadline))
            or type(phase_timeout_seconds) not in {int, float}
            or not isfinite(float(phase_timeout_seconds))
            or not 0 < float(phase_timeout_seconds) <= phase_limit
            or float(absolute_deadline) <= now
        ):
            raise ModelTransportError("semantic_http_request")
        if cancellation is not None and not isinstance(cancellation, LocalCancellationProbe):
            raise ModelTransportError("semantic_http_request")
        self._validate_payload(path, payload, float(absolute_deadline))
        effective_deadline = min(
            float(absolute_deadline), now + float(phase_timeout_seconds), now + phase_limit
        )
        request_bytes = self._request_bytes(method, path, payload)
        self._last_request_body_bytes = len(request_bytes.partition(b"\r\n\r\n")[2])
        self._last_response_body_bytes = 0
        connection: socket.socket | None = None
        try:
            connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            connection.setblocking(False)
            outcome = connection.connect_ex(("127.0.0.1", 11434))
            if outcome != 0:
                if outcome not in _SEMANTIC_CONNECT_PENDING:
                    raise ModelTransportError("semantic_http_transport")
                self._wait(
                    connection,
                    writable=True,
                    deadline=effective_deadline,
                    cancellation=cancellation,
                )
                if connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) != 0:
                    raise ModelTransportError("semantic_http_transport")
            self._send_all(connection, request_bytes, effective_deadline, cancellation)
            raw = self._read_response(connection, effective_deadline, cancellation)
            self._last_response_body_bytes = len(raw)
            try:
                decoded = json.loads(
                    raw.decode("utf-8", errors="strict"),
                    object_pairs_hook=_reject_duplicate_json_members,
                    parse_constant=_reject_non_finite_json,
                )
            except ModelTransportError:
                raise
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ModelTransportError("semantic_http_invalid") from None
            if not isinstance(decoded, Mapping):
                raise ModelTransportError("semantic_http_invalid")
            if cancellation is not None and cancellation.is_cancelled():
                raise LocalAdapterCancelledError("semantic_http_cancelled")
            return cast(Mapping[str, object], decoded)
        except (LocalAdapterCancelledError, LocalAdapterTimeoutError, ModelTransportError):
            raise
        except OSError:
            raise ModelTransportError("semantic_http_transport") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass

    def _validate_payload(
        self,
        path: str,
        payload: Mapping[str, object] | None,
        absolute_deadline: float,
    ) -> None:
        if path in {"/api/version", "/api/tags", "/api/ps"}:
            if payload is not None:
                raise ModelTransportError("semantic_http_request")
            return
        if type(payload) is not dict:
            raise ModelTransportError("semantic_http_request")
        if path == "/api/show" and payload != {"model": self._expected_model_id}:
            raise ModelTransportError("semantic_http_request")
        if path == "/api/generate" and payload != {
            "model": self._expected_model_id,
            "keep_alive": 0,
        }:
            raise ModelTransportError("semantic_http_request")
        if path == "/api/chat":
            _consume_semantic_chat_lease(self, payload, absolute_deadline)

    def _request_bytes(self, method: str, path: str, payload: Mapping[str, object] | None) -> bytes:
        request_line = f"{method} {path} HTTP/1.1\r\n".encode("ascii")
        if len(request_line) > _SEMANTIC_HTTP_REQUEST_LINE_BYTES:
            raise ModelTransportError("semantic_http_request")
        lines = [
            request_line,
            b"Host: 127.0.0.1:11434\r\n",
            b"Accept: application/json\r\n",
            b"Connection: close\r\n",
        ]
        body = b""
        if payload is not None:
            try:
                body = json.dumps(
                    payload,
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
            except (TypeError, ValueError, UnicodeEncodeError):
                raise ModelTransportError("semantic_http_request") from None
            if len(body) > MAX_OLLAMA_REQUEST_BYTES:
                raise ModelTransportError("semantic_http_request")
            lines.extend(
                (
                    b"Content-Type: application/json; charset=utf-8\r\n",
                    f"Content-Length: {len(body)}\r\n".encode("ascii"),
                )
            )
        lines.append(b"\r\n")
        lines.append(body)
        return b"".join(lines)

    def _wait(
        self,
        connection: socket.socket,
        *,
        writable: bool,
        deadline: float,
        cancellation: LocalCancellationProbe | None,
    ) -> None:
        while True:
            if cancellation is not None and cancellation.is_cancelled():
                raise LocalAdapterCancelledError("semantic_http_cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LocalAdapterTimeoutError("semantic_http_timeout")
            wait_seconds = min(remaining, _SEMANTIC_HTTP_CANCEL_POLL_SECONDS)
            readable, writeable, exceptional = select.select(
                [] if writable else [connection],
                [connection] if writable else [],
                [connection],
                wait_seconds,
            )
            if exceptional:
                raise ModelTransportError("semantic_http_transport")
            ready = writeable if writable else readable
            if ready:
                return

    def _send_all(
        self,
        connection: socket.socket,
        data: bytes,
        deadline: float,
        cancellation: LocalCancellationProbe | None,
    ) -> None:
        sent = 0
        while sent < len(data):
            self._wait(
                connection,
                writable=True,
                deadline=deadline,
                cancellation=cancellation,
            )
            try:
                amount = connection.send(data[sent:])
            except BlockingIOError:
                continue
            if amount <= 0:
                raise ModelTransportError("semantic_http_transport")
            sent += amount

    def _receive(
        self,
        connection: socket.socket,
        maximum: int,
        deadline: float,
        cancellation: LocalCancellationProbe | None,
    ) -> bytes:
        while True:
            self._wait(
                connection,
                writable=False,
                deadline=deadline,
                cancellation=cancellation,
            )
            try:
                block = connection.recv(maximum)
            except BlockingIOError:
                continue
            if not block:
                raise ModelTransportError("semantic_http_invalid")
            return block

    def _read_line(
        self,
        connection: socket.socket,
        buffer: bytearray,
        maximum: int,
        deadline: float,
        cancellation: LocalCancellationProbe | None,
    ) -> tuple[bytes, int]:
        while True:
            marker = buffer.find(b"\r\n")
            bare_lf = buffer.find(b"\n")
            if bare_lf >= 0 and (marker < 0 or bare_lf != marker + 1):
                raise ModelTransportError("semantic_http_invalid")
            if marker >= 0:
                consumed = marker + 2
                if consumed > maximum:
                    raise ModelTransportError("semantic_http_invalid")
                line = bytes(buffer[:marker])
                del buffer[:consumed]
                return line, consumed
            if len(buffer) >= maximum:
                raise ModelTransportError("semantic_http_invalid")
            buffer.extend(
                self._receive(
                    connection,
                    min(4096, maximum - len(buffer)),
                    deadline,
                    cancellation,
                )
            )

    def _read_exact(
        self,
        connection: socket.socket,
        buffer: bytearray,
        count: int,
        deadline: float,
        cancellation: LocalCancellationProbe | None,
    ) -> bytes:
        while len(buffer) < count:
            buffer.extend(
                self._receive(
                    connection,
                    min(65_536, count - len(buffer)),
                    deadline,
                    cancellation,
                )
            )
        result = bytes(buffer[:count])
        del buffer[:count]
        return result

    def _read_response(
        self,
        connection: socket.socket,
        deadline: float,
        cancellation: LocalCancellationProbe | None,
    ) -> bytes:
        buffer = bytearray()
        status, status_bytes = self._read_line(
            connection,
            buffer,
            _SEMANTIC_HTTP_STATUS_LINE_BYTES,
            deadline,
            cancellation,
        )
        if status != b"HTTP/1.1 200 OK":
            raise ModelTransportError("semantic_http_invalid")
        total_headers = status_bytes
        headers: dict[str, str] = {}
        while True:
            line, consumed = self._read_line(
                connection,
                buffer,
                _SEMANTIC_HTTP_HEADER_LINE_BYTES,
                deadline,
                cancellation,
            )
            total_headers += consumed
            if total_headers > _SEMANTIC_HTTP_HEADER_BYTES:
                raise ModelTransportError("semantic_http_invalid")
            if not line:
                break
            if len(headers) >= _SEMANTIC_HTTP_HEADER_COUNT or b": " not in line:
                raise ModelTransportError("semantic_http_invalid")
            raw_name, raw_value = line.split(b": ", 1)
            if (
                _SEMANTIC_HTTP_HEADER_NAME.fullmatch(raw_name) is None
                or not raw_value
                or raw_value[:1] == b" "
                or raw_value[-1:] == b" "
                or any(value < 0x20 or value > 0x7E for value in raw_name + raw_value)
            ):
                raise ModelTransportError("semantic_http_invalid")
            name = raw_name.decode("ascii").lower()
            value = raw_value.decode("ascii")
            if name not in _SEMANTIC_HTTP_ALLOWED_HEADERS or name in headers:
                raise ModelTransportError("semantic_http_invalid")
            headers[name] = value
        if headers.get("content-type") not in _SEMANTIC_HTTP_CONTENT_TYPES:
            raise ModelTransportError("semantic_http_invalid")
        if "connection" in headers and headers["connection"] != "close":
            raise ModelTransportError("semantic_http_invalid")
        content_length = headers.get("content-length")
        transfer_encoding = headers.get("transfer-encoding")
        if (content_length is None) == (transfer_encoding is None):
            raise ModelTransportError("semantic_http_invalid")
        if content_length is not None:
            raw_length = content_length.encode("ascii")
            if _SEMANTIC_HTTP_CONTENT_LENGTH.fullmatch(raw_length) is None:
                raise ModelTransportError("semantic_http_invalid")
            length = int(content_length)
            if length > _SEMANTIC_HTTP_RESPONSE_BYTES or len(buffer) > length:
                raise ModelTransportError("semantic_http_invalid")
            return self._read_exact(connection, buffer, length, deadline, cancellation)
        if transfer_encoding != "chunked":
            raise ModelTransportError("semantic_http_invalid")
        body = bytearray()
        while True:
            chunk_line, _ = self._read_line(
                connection,
                buffer,
                _SEMANTIC_HTTP_CHUNK_LINE_BYTES,
                deadline,
                cancellation,
            )
            if _SEMANTIC_HTTP_CHUNK_SIZE.fullmatch(chunk_line) is None:
                raise ModelTransportError("semantic_http_invalid")
            chunk_size = int(chunk_line, 16)
            if chunk_size == 0:
                terminal, _ = self._read_line(
                    connection,
                    buffer,
                    2,
                    deadline,
                    cancellation,
                )
                if terminal or buffer:
                    raise ModelTransportError("semantic_http_invalid")
                return bytes(body)
            if (
                chunk_size > _SEMANTIC_HTTP_RESPONSE_BYTES
                or len(body) + chunk_size > _SEMANTIC_HTTP_RESPONSE_BYTES
            ):
                raise ModelTransportError("semantic_http_invalid")
            body.extend(self._read_exact(connection, buffer, chunk_size, deadline, cancellation))
            if self._read_exact(connection, buffer, 2, deadline, cancellation) != b"\r\n":
                raise ModelTransportError("semantic_http_invalid")


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ModelManifestError(f"Ollama {field_name} must be an object")
    return cast(Mapping[str, object], value)


def _reject_duplicate_json_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ModelTransportError("Ollama response contains duplicate JSON members")
        result[key] = value
    return result


def _reject_non_finite_json(_: str) -> object:
    raise ModelTransportError("Ollama response contains a non-finite JSON constant")


def _bounded_string(value: object, field_name: str, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ModelManifestError(f"Ollama {field_name} is not bounded text")
    return value


def _capabilities(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    mapping = {
        "completion": ModelCapability.TEXT_GENERATION.value,
        "generate": ModelCapability.TEXT_GENERATION.value,
        "vision": ModelCapability.VISION.value,
        "video": ModelCapability.VIDEO.value,
        "audio": ModelCapability.AUDIO.value,
        "structured_output": ModelCapability.STRUCTURED_OUTPUT.value,
    }
    values = tuple(
        mapping[str(item).casefold()] for item in value if str(item).casefold() in mapping
    )
    return tuple(dict.fromkeys(values))


def _parse_ollama_model(
    tags_item: Mapping[str, object], show: Mapping[str, object]
) -> OllamaModelObservation:
    name = _bounded_string(tags_item.get("name"), "model name")
    digest = _bounded_string(tags_item.get("digest"), "model digest")
    size = tags_item.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ModelManifestError("Ollama model size is invalid")
    details = _mapping(show.get("details", tags_item.get("details", {})), "model details")
    family = _bounded_string(details.get("family", "unknown"), "model family")
    quantization = details.get("quantization_level")
    capabilities = _capabilities(show.get("capabilities"))
    context_length = show.get("model_info", {})
    context_value: int | None = None
    if isinstance(context_length, Mapping):
        raw_context = context_length.get("context_length")
        if isinstance(raw_context, int) and not isinstance(raw_context, bool) and raw_context > 0:
            context_value = raw_context
    return OllamaModelObservation(
        name=name,
        digest=digest,
        size_bytes=size,
        family=family,
        capabilities=capabilities,
        context_length=context_value,
        quantization=None
        if quantization is None
        else _bounded_string(quantization, "quantization"),
    )


def _parse_ollama_process(
    value: object, model: OllamaModelObservation
) -> OllamaProcessObservation | None:
    root = _mapping(value, "ps response")
    models = root.get("models", ())
    if not isinstance(models, Sequence) or isinstance(models, (str, bytes)):
        raise ModelManifestError("Ollama ps models must be an array")
    for raw in models:
        item = _mapping(raw, "ps model")
        if item.get("name") != model.name:
            continue
        digest = item.get("digest", model.digest)
        vram = item.get("size_vram", 0)
        context = item.get("context_length")
        if not isinstance(vram, int) or isinstance(vram, bool) or vram < 0:
            raise ModelManifestError("Ollama process VRAM size is invalid")
        if context is not None and (
            not isinstance(context, int) or isinstance(context, bool) or context <= 0
        ):
            raise ModelManifestError("Ollama process context length is invalid")
        return OllamaProcessObservation(
            model.name, _bounded_string(digest, "process digest"), vram, context
        )
    return None


def preflight_ollama(
    transport: OllamaTransport,
    manifest: ModelManifest,
    *,
    cancellation: LocalCancellationProbe | None = None,
) -> ModelProbeReport:
    """Run only the four read-only local preflight endpoints and reconcile their identity."""

    if (
        not isinstance(manifest, ModelManifest)
        or manifest.backend_family is not ModelBackendFamily.OLLAMA
    ):
        raise ModelManifestError("Ollama preflight requires an Ollama manifest")
    version_payload = transport.request("GET", "/api/version", cancellation=cancellation)
    version = _bounded_string(version_payload.get("version"), "server version")
    server = OllamaServerObservation(version, bool(version_payload.get("cloud", False)))
    tags_payload = transport.request("GET", "/api/tags", cancellation=cancellation)
    tags = tags_payload.get("models")
    if not isinstance(tags, Sequence) or isinstance(tags, (str, bytes)):
        raise ModelManifestError("Ollama tags models must be an array")
    selected: Mapping[str, object] | None = None
    for raw in tags:
        item = _mapping(raw, "tags model")
        if item.get("name") == manifest.model_id:
            selected = item
            break
    if selected is None:
        return ModelProbeReport(
            manifest.manifest_id, ModelProbeStatus.UNAVAILABLE, ("model_missing",), server=server
        )
    show_payload = transport.request(
        "POST", "/api/show", {"name": manifest.model_id}, cancellation=cancellation
    )
    model = _parse_ollama_model(selected, _mapping(show_payload, "show response"))
    ps_payload = transport.request("GET", "/api/ps", cancellation=cancellation)
    process = _parse_ollama_process(ps_payload, model)
    return qualify_ollama_manifest(manifest, server=server, model=model, process=process)


def run_ollama_provider_preflight(
    setup: ProviderSetup,
    transport: OllamaTransport,
    manifest: ModelManifest,
    *,
    endpoint_fingerprint: str,
    cancellation: LocalCancellationProbe | None = None,
) -> ProviderPreflightReceipt:
    """Run the real read-only Ollama preflight flow and emit redacted operational authority."""

    if (
        not isinstance(setup, ProviderSetup)
        or setup.local_backend is not ProviderLocalBackend.OLLAMA
    ):
        raise ContractValidationError("Ollama provider preflight requires an Ollama setup")
    if not setup.policy_valid:
        return ProviderPreflightReceipt(
            setup.setup_fingerprint,
            setup.revision,
            setup.provider,
            setup.local_backend,
            ProviderPreflightStatus.UNSAFE_CONFIGURATION,
            diagnostics=setup.diagnostics,
            endpoint_fingerprint=endpoint_fingerprint,
        )
    if cancellation is not None and not isinstance(cancellation, LocalCancellationProbe):
        raise ContractValidationError("Ollama cancellation probe is invalid")
    if cancellation is not None and cancellation.is_cancelled():
        return ProviderPreflightReceipt(
            setup.setup_fingerprint,
            setup.revision,
            setup.provider,
            setup.local_backend,
            ProviderPreflightStatus.CANCELLED,
            diagnostics=("cancelled_before_connect",),
            endpoint_fingerprint=endpoint_fingerprint,
        )
    try:
        report = preflight_ollama(transport, manifest, cancellation=cancellation)
    except LocalAdapterCancelledError:
        return ProviderPreflightReceipt(
            setup.setup_fingerprint,
            setup.revision,
            setup.provider,
            setup.local_backend,
            ProviderPreflightStatus.CANCELLED,
            diagnostics=("cancelled",),
            endpoint_fingerprint=endpoint_fingerprint,
            network_attempted=True,
        )
    except LocalAdapterTimeoutError:
        return ProviderPreflightReceipt(
            setup.setup_fingerprint,
            setup.revision,
            setup.provider,
            setup.local_backend,
            ProviderPreflightStatus.TIMEOUT,
            diagnostics=("timeout",),
            endpoint_fingerprint=endpoint_fingerprint,
            network_attempted=True,
        )
    except ModelTransportError:
        return ProviderPreflightReceipt(
            setup.setup_fingerprint,
            setup.revision,
            setup.provider,
            setup.local_backend,
            ProviderPreflightStatus.TRANSPORT,
            diagnostics=("transport",),
            endpoint_fingerprint=endpoint_fingerprint,
            network_attempted=True,
        )
    except ModelManifestError:
        return ProviderPreflightReceipt(
            setup.setup_fingerprint,
            setup.revision,
            setup.provider,
            setup.local_backend,
            ProviderPreflightStatus.UNSAFE_CONFIGURATION,
            diagnostics=("malformed_observation",),
            endpoint_fingerprint=endpoint_fingerprint,
            network_attempted=True,
        )
    return build_ollama_provider_preflight(
        setup,
        report,
        endpoint_fingerprint=endpoint_fingerprint,
        expected_manifest_id=manifest.manifest_id,
        expected_model_digest=manifest.model_digest,
        expected_server_version=manifest.server_version or "unqualified",
    )


def _descriptor(manifest: ModelManifest) -> LocalAdapterDescriptor:
    supported_media = frozenset(
        {
            media
            for capability, media in (
                (ModelCapability.VISION, MediaKind.IMAGE),
                (ModelCapability.VIDEO, MediaKind.VIDEO),
                (ModelCapability.AUDIO, MediaKind.AUDIO),
            )
            if capability in manifest.capabilities
        }
    )
    return LocalAdapterDescriptor(
        adapter_id=manifest.adapter_id,
        kind=LocalAdapterKind.REASONING,
        adapter_version=manifest.adapter_version,
        minimum_version=manifest.adapter_version,
        maximum_version=manifest.adapter_version,
        supported_task_modes=frozenset(TaskMode),
        supported_media=supported_media,
        supported_devices=frozenset({LocalDeviceKind.AUTO, LocalDeviceKind.CPU}),
        optional_dependencies=(),
        output_schema="h3.model.generation.result.v1",
        limits=manifest.runtime.limits,
        supports_determinism=True,
        supports_seed=True,
        supports_cancellation=False,
    )


def _is_semantic_deadline_transport(
    transport: object,
) -> TypeGuard[SemanticDeadlineOllamaTransport]:
    # CRITICAL: subclasses inherit real socket behavior and must never enter the fake-test lane.
    return isinstance(transport, SemanticDeadlineOllamaTransport)


class OllamaNativeAdapter(LocalAdapter):
    """Explicitly selected Ollama adapter; it never chooses itself as a fallback."""

    def __init__(
        self,
        transport: OllamaTransport,
        manifest: ModelManifest,
        preflight: ModelProbeReport,
        *,
        setup: ProviderSetup,
        consent: ProviderConsentAuthority,
        provider_preflight: ProviderPreflightReceipt,
    ) -> None:
        if manifest.backend_family is not ModelBackendFamily.OLLAMA:
            raise LocalAdapterCapabilityError("Ollama adapter received a non-Ollama manifest")
        # CRITICAL: a successful probe is authority only for its exact manifest/model/server tuple.
        if (
            not manifest.approved
            or preflight.status is not ModelProbeStatus.SUPPORTED
            or preflight.manifest_id != manifest.manifest_id
            or preflight.observed_model is None
            or preflight.observed_model.name != manifest.model_id
            or preflight.observed_model.digest != manifest.model_digest
            or preflight.server is None
            or preflight.server.cloud_enabled
            or (
                manifest.server_version is not None
                and preflight.server.version != manifest.server_version
            )
        ):
            raise LocalAdapterCapabilityError("Ollama model has not passed explicit preflight")
        try:
            admit_provider_execution(setup, consent, provider_preflight)
        except (ContractValidationError, SecurityPolicyError) as exc:
            raise LocalAdapterCapabilityError(
                "Ollama operational provider authority is stale or not ready"
            ) from exc
        if setup.local_backend is not ProviderLocalBackend.OLLAMA:
            raise LocalAdapterCapabilityError("Ollama adapter requires the explicit Ollama backend")
        self._transport = transport
        self._manifest = manifest
        self._descriptor = _descriptor(manifest)

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._descriptor

    def run(
        self, request: LocalAdapterExecutionRequest, guard: LocalBudgetGuard
    ) -> LocalAdapterResult:
        if not isinstance(request.input_value, ModelGenerationRequest):
            raise LocalAdapterCapabilityError("Ollama adapter requires ModelGenerationRequest")
        generation = request.input_value
        validate_model_generation_request(self._manifest, generation)
        guard.checkpoint()
        options: dict[str, object] = {
            "temperature": generation.temperature,
            "top_k": generation.top_k,
            "top_p": generation.top_p,
            "min_p": generation.min_p,
            "repeat_penalty": generation.repetition_penalty,
            "presence_penalty": generation.presence_penalty,
            "num_predict": generation.max_tokens,
        }
        if generation.seed is not None:
            options["seed"] = generation.seed
        payload: dict[str, object] = {
            "model": self._manifest.model_id,
            "messages": [{"role": "user", "content": generation.prompt}],
            "stream": False,
            "format": generation.structured_schema
            if generation.structured_schema is not None
            else "json",
            "options": options,
            "keep_alive": "0",
            "think": generation.thinking,
        }
        if generation.image_payloads:
            payload["messages"] = [
                {
                    "role": "user",
                    "content": generation.prompt,
                    "images": [
                        base64.b64encode(item).decode("ascii") for item in generation.image_payloads
                    ],
                }
            ]
        response = self._transport.request("POST", "/api/chat", payload)
        guard.checkpoint()
        if response.get("done") is not True or response.get("done_reason") != "stop":
            raise ModelOutputError("Ollama response did not reach a terminal stop")
        message = response.get("message")
        message_mapping = _mapping(message, "chat message")
        text = message_mapping.get("content")
        if not isinstance(text, str) or not text:
            raise ModelOutputError("Ollama chat response has no bounded content")
        result = build_model_result(self._manifest, generation, text)
        guard.record_output(byte_count=len(text.encode("utf-8")), item_count=1)
        return LocalAdapterResult(
            adapter_id=self.descriptor.adapter_id,
            adapter_version=self.descriptor.adapter_version,
            device=request.device,
            value=result,
            output_bytes=len(text.encode("utf-8")),
            output_items=1,
        )


SEMANTIC_OLLAMA_OUTER_SECONDS = 120.0
SEMANTIC_OLLAMA_CLEANUP_SECONDS = 30.0
SEMANTIC_OLLAMA_CLEANUP_POLL_SECONDS = 0.5
SEMANTIC_OLLAMA_CLEANUP_MAX_POLLS = 61
SEMANTIC_OLLAMA_RESPONSE_BYTES = 1_048_576
_SEMANTIC_TAG_KEYS = frozenset(
    {"name", "model", "modified_at", "size", "digest", "details", "capabilities"}
)
_SEMANTIC_DETAILS_KEYS = frozenset(
    {
        "parent_model",
        "format",
        "family",
        "families",
        "parameter_size",
        "quantization_level",
        "context_length",
        "embedding_length",
    }
)
_SEMANTIC_SHOW_KEYS = frozenset(
    {
        "license",
        "modelfile",
        "parameters",
        "template",
        "details",
        "model_info",
        "projector_info",
        "modified_at",
        "capabilities",
        "tensors",
        "requires",
    }
)
_SEMANTIC_PS_KEYS = frozenset(
    {
        "name",
        "model",
        "size",
        "digest",
        "details",
        "expires_at",
        "size_vram",
        "context_length",
    }
)
_SEMANTIC_CHAT_KEYS = frozenset(
    {
        "model",
        "created_at",
        "message",
        "done",
        "done_reason",
        "total_duration",
        "load_duration",
        "prompt_eval_count",
        "prompt_eval_duration",
        "eval_count",
        "eval_duration",
    }
)
_SEMANTIC_UNLOAD_KEYS = frozenset(
    {
        "model",
        "created_at",
        "response",
        "done",
        "done_reason",
        "total_duration",
        "load_duration",
    }
)


class SemanticOllamaExecutionError(RuntimeError):
    """A content-free, terminal M17-16 provider outcome."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SemanticOllamaExecution:
    model_result: object = field(repr=False)
    provider_preflight: ProviderPreflightReceipt = field(repr=False)
    qualification_fingerprint: str
    capability_fingerprint: str
    request_bytes: int = 0
    response_bytes: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or not 0 <= value <= 2**63 - 1
            for value in (
                self.request_bytes,
                self.response_bytes,
                self.prompt_tokens,
                self.completion_tokens,
            )
        ):
            raise SemanticOllamaExecutionError("execution_usage_invalid")


@dataclass(frozen=True, slots=True)
class _SemanticExecutionAuthority:
    execution: SemanticOllamaExecution
    model_result: ModelGenerationResult
    provider_preflight: ProviderPreflightReceipt
    execution_fields: tuple[tuple[str, object], ...]
    model_result_fields: tuple[tuple[str, object], ...]
    provider_preflight_fields: tuple[tuple[str, object], ...]
    fingerprint: str


_SEMANTIC_EXECUTION_LOCK = threading.Lock()
_SEMANTIC_EXECUTION_AUTHORITY_LOCK = threading.Lock()
_SEMANTIC_EXECUTION_AUTHORITIES: dict[int, _SemanticExecutionAuthority] = {}
_MAX_SEMANTIC_EXECUTION_AUTHORITIES = 64


def _semantic_execution_fingerprint(value: SemanticOllamaExecution) -> str:
    model_result = cast(ModelGenerationResult, value.model_result)
    return canonical_fingerprint(
        {
            "model_result": {
                "public": model_result.to_public_dict(),
                "text": model_result.text,
                "parsed_output": model_result.parsed_output,
            },
            "provider_preflight": value.provider_preflight.to_public_dict(),
            "qualification_fingerprint": value.qualification_fingerprint,
            "capability_fingerprint": value.capability_fingerprint,
            "usage": {
                "request_bytes": value.request_bytes,
                "response_bytes": value.response_bytes,
                "prompt_tokens": value.prompt_tokens,
                "completion_tokens": value.completion_tokens,
            },
        }
    )


_SemanticAuthorityDataclass = (
    SemanticOllamaExecution | ModelGenerationResult | ProviderPreflightReceipt
)


def _semantic_field_referents(
    value: _SemanticAuthorityDataclass,
) -> tuple[tuple[str, object], ...]:
    return tuple((item.name, getattr(value, item.name)) for item in dataclass_fields(value))


def _semantic_execution_authority(value: SemanticOllamaExecution) -> _SemanticExecutionAuthority:
    if type(value.model_result) is not ModelGenerationResult:
        raise SemanticOllamaExecutionError("execution_authority")
    if type(value.provider_preflight) is not ProviderPreflightReceipt:
        raise SemanticOllamaExecutionError("execution_authority")
    model_result = value.model_result
    provider_preflight = value.provider_preflight
    return _SemanticExecutionAuthority(
        execution=value,
        model_result=model_result,
        provider_preflight=provider_preflight,
        execution_fields=_semantic_field_referents(value),
        model_result_fields=_semantic_field_referents(model_result),
        provider_preflight_fields=_semantic_field_referents(provider_preflight),
        fingerprint=_semantic_execution_fingerprint(value),
    )


def _semantic_exact_field_referents(
    value: _SemanticAuthorityDataclass,
    expected: tuple[tuple[str, object], ...],
) -> bool:
    current_fields = dataclass_fields(value)
    if len(current_fields) != len(expected):
        return False
    for item, (expected_name, expected_value) in zip(current_fields, expected, strict=True):
        if item.name != expected_name or getattr(value, item.name) is not expected_value:
            return False
    return True


def consume_semantic_ollama_execution_authority(value: object) -> SemanticOllamaExecution:
    """Consume one exact adapter-issued execution fact; copies and mutations reject."""

    if type(value) is not SemanticOllamaExecution:
        raise SemanticOllamaExecutionError("execution_authority")
    with _SEMANTIC_EXECUTION_AUTHORITY_LOCK:
        authority = _SEMANTIC_EXECUTION_AUTHORITIES.pop(id(value), None)
    try:
        exact = (
            authority is not None
            and authority.execution is value
            and value.model_result is authority.model_result
            and value.provider_preflight is authority.provider_preflight
            and _semantic_exact_field_referents(value, authority.execution_fields)
            and _semantic_exact_field_referents(
                authority.model_result, authority.model_result_fields
            )
            and _semantic_exact_field_referents(
                authority.provider_preflight, authority.provider_preflight_fields
            )
            and authority.fingerprint == _semantic_execution_fingerprint(value)
        )
    # CRITICAL: private authority inspection must never disclose a mutated field or payload path.
    except Exception:
        raise SemanticOllamaExecutionError("execution_authority") from None
    if not exact:
        raise SemanticOllamaExecutionError("execution_authority")
    return value


def _semantic_exact_mapping(
    value: object,
    expected: frozenset[str],
    field_name: str,
    *,
    required: frozenset[str] | None = None,
) -> dict[str, object]:
    if type(value) is not dict:
        raise SemanticOllamaExecutionError(f"{field_name}_shape")
    mapped = cast(dict[str, object], value)
    keys = frozenset(mapped)
    if not keys.issubset(expected) or (required is not None and not required.issubset(keys)):
        raise SemanticOllamaExecutionError(f"{field_name}_members")
    return mapped


def _semantic_strings(value: object, field_name: str, maximum: int = 64) -> tuple[str, ...]:
    if type(value) is not list or len(value) > maximum:
        raise SemanticOllamaExecutionError(f"{field_name}_shape")
    values = tuple(value)
    if not all(type(item) is str and 0 < len(item) <= 128 for item in values):
        raise SemanticOllamaExecutionError(f"{field_name}_value")
    if len(values) != len(set(values)):
        raise SemanticOllamaExecutionError(f"{field_name}_duplicate")
    return cast(tuple[str, ...], values)


def _semantic_details(
    value: object, profile: SemanticProviderProfile | None = None
) -> dict[str, object]:
    details = _semantic_exact_mapping(value, _SEMANTIC_DETAILS_KEYS, "details")
    for key in ("format", "family", "parameter_size", "quantization_level"):
        if type(details.get(key)) is not str or not cast(str, details[key]):
            raise SemanticOllamaExecutionError("details_value")
    families = details.get("families")
    if families is not None:
        _semantic_strings(families, "details_families", 16)
    parent = details.get("parent_model")
    if parent is not None and type(parent) is not str:
        raise SemanticOllamaExecutionError("details_parent_model")
    limits = {"context_length": 16_777_216, "embedding_length": 1_048_576}
    for key, maximum in limits.items():
        dimension = details.get(key)
        if dimension is not None and (type(dimension) is not int or not 0 < dimension <= maximum):
            raise SemanticOllamaExecutionError("details_value")
        if profile is not None and dimension is not None and dimension != getattr(profile, key):
            raise SemanticOllamaExecutionError("details_mismatch")
    return details


def _semantic_cancelled(cancellation: LocalCancellationProbe | None) -> bool:
    return cancellation is not None and cancellation.is_cancelled()


def _semantic_request(
    transport: SemanticOllamaTransport,
    method: str,
    path: str,
    payload: Mapping[str, object] | None,
    *,
    cancellation: LocalCancellationProbe | None,
    deadline: float,
    clock: Callable[[], float],
) -> Mapping[str, object]:
    if _semantic_cancelled(cancellation):
        raise SemanticOllamaExecutionError("cancelled")
    remaining = deadline - clock()
    if remaining <= 0:
        raise SemanticOllamaExecutionError("timeout")
    try:
        result = transport.request(
            method,
            path,
            payload,
            cancellation=cancellation,
            absolute_deadline=deadline,
            phase_timeout_seconds=min(_SEMANTIC_HTTP_PATH_LIMITS[path], remaining),
        )
    except LocalAdapterCancelledError as exc:
        raise SemanticOllamaExecutionError("cancelled") from exc
    except LocalAdapterTimeoutError as exc:
        raise SemanticOllamaExecutionError("timeout") from exc
    except ModelTransportError as exc:
        raise SemanticOllamaExecutionError("transport") from exc
    if clock() >= deadline:
        raise SemanticOllamaExecutionError("timeout")
    if _semantic_cancelled(cancellation):
        raise SemanticOllamaExecutionError("cancelled")
    if not isinstance(result, Mapping):
        raise SemanticOllamaExecutionError("transport_shape")
    return _validate_semantic_response_json(result, path=path)


def _validate_semantic_response_json(
    value: object,
    *,
    path: str | None = None,
) -> dict[str, object]:
    """Validate the complete decoded M17-16 response envelope before field selection."""

    def valid_text(item: object, maximum: int) -> bool:
        if type(item) is not str or not item or len(item) > maximum:
            return False
        try:
            item.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return True

    def validate_show_tensors(item: object, depth: int) -> list[object]:
        if type(item) is not list or depth > 8 or len(item) > _SEMANTIC_SHOW_TENSOR_ITEMS:
            raise SemanticOllamaExecutionError("provider_output_invalid")
        result: list[object] = []
        for raw in cast(list[object], item):
            if type(raw) is not dict or depth + 1 > 8 or set(raw) != {"name", "shape", "type"}:
                raise SemanticOllamaExecutionError("provider_output_invalid")
            tensor = cast(dict[str, object], raw)
            shape = tensor["shape"]
            if (
                not valid_text(tensor["name"], 128)
                or not valid_text(tensor["type"], 32)
                or type(shape) is not list
                or depth + 2 > 8
                or not 1 <= len(shape) <= _SEMANTIC_SHOW_TENSOR_DIMENSIONS
                or not all(
                    type(dimension) is int and 0 < dimension <= _SEMANTIC_SHOW_TENSOR_DIMENSION_MAX
                    for dimension in shape
                )
            ):
                raise SemanticOllamaExecutionError("provider_output_invalid")
            result.append(
                {
                    "name": tensor["name"],
                    "shape": list(shape),
                    "type": tensor["type"],
                }
            )
        return result

    def validate(item: object, depth: int, location: tuple[str, ...]) -> object:
        if type(item) is dict:
            if depth > 8 or len(item) > 64:
                raise SemanticOllamaExecutionError("provider_output_invalid")
            mapped = cast(dict[object, object], item)
            result: dict[str, object] = {}
            for key, child in mapped.items():
                if type(key) is not str:
                    raise SemanticOllamaExecutionError("provider_output_invalid")
                try:
                    key.encode("utf-8")
                except UnicodeEncodeError:
                    raise SemanticOllamaExecutionError("provider_output_invalid") from None
                next_depth = depth + 1 if type(child) in {dict, list} else depth
                # CRITICAL: pinned Ollama 0.32.13 returns this documented verbose field even when
                # verbose is false.  Keep the larger bound exact to the show-root field only.
                if path == "/api/show" and not location and key == "tensors":
                    result[key] = validate_show_tensors(child, next_depth)
                else:
                    result[key] = validate(child, next_depth, location + (key,))
            return result
        if type(item) is list:
            maximum = MAX_DISCOVERY_ROWS if path == "/api/tags" and location == ("models",) else 64
            if depth > 8 or len(item) > maximum:
                raise SemanticOllamaExecutionError("provider_output_invalid")
            return [
                validate(
                    child,
                    depth + 1 if type(child) in {dict, list} else depth,
                    location,
                )
                for child in item
            ]
        if type(item) is str:
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                raise SemanticOllamaExecutionError("provider_output_invalid") from None
            return item
        if item is None or type(item) in {bool, int}:
            return item
        if type(item) is float and isfinite(item):
            return item
        raise SemanticOllamaExecutionError("provider_output_invalid")

    validated = validate(value, 1, ())
    if type(validated) is not dict:
        raise SemanticOllamaExecutionError("provider_output_invalid")
    return validated


def _semantic_endpoint_fingerprint() -> str:
    return canonical_fingerprint(
        {"scheme": "http", "host": "127.0.0.1", "port": 11434, "path": "/api"}
    )


def _semantic_native_model_digest(value: object) -> str:
    try:
        return ollama_native_digest_to_model_digest(parse_ollama_native_digest(value))
    except SemanticProposalProducerError:
        raise SemanticOllamaExecutionError("native_digest_invalid") from None


def _semantic_current_identity(
    tags: object,
    show: object,
    model_id: str,
    connection: SemanticProviderConnection,
    server_version: str,
) -> SemanticChosenModelProfile:
    try:
        read_native_models(PromptModelFamily.OLLAMA, tags, "/api/tags")
        row = parse_exact_tags_row(tags, model_id)
        metadata = read_ollama_choice_metadata(show, row.model_digest)
        return SemanticChosenModelProfile(
            connection, model_id, row.model_size_bytes, server_version, metadata
        )
    except PromptModelContractError as exc:
        code = "model_missing" if exc.code == "readiness_tags_absent" else "model_identity_invalid"
        raise SemanticOllamaExecutionError(code) from None
    except SemanticProposalProducerError as exc:
        raise SemanticOllamaExecutionError(exc.code) from None


def resolve_semantic_model_profile(
    transport: SemanticOllamaTransport,
    setup: ProviderSetup,
    connection: SemanticProviderConnection,
    model_id: object,
    *,
    cancellation: LocalCancellationProbe | None = None,
    action_deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> SemanticChosenModelProfile:
    """Resolve one local choice without chat, media, aliases or provider prose in failures."""
    if type(model_id) is not str or not model_id:
        raise SemanticOllamaExecutionError("choose_local_model")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,255}", model_id) is None or ".." in model_id:
        raise SemanticOllamaExecutionError("model_choice_invalid")
    if (
        type(setup) is not ProviderSetup
        or setup.local_backend is not ProviderLocalBackend.OLLAMA
        or not setup.policy_valid
        or setup.fallback_provider is not None
    ):
        raise SemanticOllamaExecutionError("unsafe_configuration")
    if type(connection) is not SemanticProviderConnection:
        raise SemanticOllamaExecutionError("connection_authority")
    if cancellation is not None and not isinstance(cancellation, LocalCancellationProbe):
        raise SemanticOllamaExecutionError("cancellation_invalid")
    if (
        type(action_deadline) not in {float, int}
        or not isfinite(action_deadline)
        or getattr(transport, "endpoint_fingerprint", None) != _semantic_endpoint_fingerprint()
    ):
        raise SemanticOllamaExecutionError("endpoint_mismatch")
    version = _semantic_exact_mapping(
        _semantic_request(
            transport,
            "GET",
            "/api/version",
            None,
            cancellation=cancellation,
            deadline=action_deadline,
            clock=clock,
        ),
        frozenset({"version"}),
        "version",
    )
    tags = _semantic_request(
        transport,
        "GET",
        "/api/tags",
        None,
        cancellation=cancellation,
        deadline=action_deadline,
        clock=clock,
    )
    try:
        # SECURITY: reject the entire malformed/duplicate census before narrowing to a choice.
        read_native_models(PromptModelFamily.OLLAMA, tags, "/api/tags")
        row = parse_exact_tags_row(tags, model_id)
    except PromptModelContractError as exc:
        code = "model_missing" if exc.code == "readiness_tags_absent" else "model_identity_invalid"
        raise SemanticOllamaExecutionError(code) from None
    if row.model_size_bytes > connection.max_model_bytes:
        raise SemanticOllamaExecutionError("model_resource_limit")
    show = _semantic_request(
        transport,
        "POST",
        "/api/show",
        {"model": model_id},
        cancellation=cancellation,
        deadline=action_deadline,
        clock=clock,
    )
    return _semantic_current_identity(
        tags, show, model_id, connection, cast(str, version["version"])
    )


def _semantic_model_observation(
    tags_payload: Mapping[str, object],
    show_payload: Mapping[str, object],
    profile: SemanticExecutionProfile,
) -> OllamaModelObservation:
    if type(profile) is SemanticChosenModelProfile:
        exact = _semantic_current_identity(
            tags_payload, show_payload, profile.model_id, profile.connection, profile.server_version
        )
        if exact != profile:
            # CRITICAL: a new native digest/metadata is not permission to silently rebind source.
            raise SemanticOllamaExecutionError("model_identity_mismatch")
        return OllamaModelObservation(
            profile.model_id,
            profile.model_digest,
            profile.model_size_bytes,
            profile.model_family,
            (ModelCapability.TEXT_GENERATION.value, ModelCapability.STRUCTURED_OUTPUT.value),
            profile.context_length,
            profile.metadata.quantization,
        )
    profile = cast(SemanticProviderProfile, profile)
    tags = _semantic_exact_mapping(
        tags_payload,
        frozenset({"models"}),
        "tags",
        required=frozenset({"models"}),
    )
    raw_models = tags["models"]
    if type(raw_models) is not list or not 1 <= len(raw_models) <= 64:
        raise SemanticOllamaExecutionError("tags_models")
    selected: dict[str, object] | None = None
    for raw in raw_models:
        item = _semantic_exact_mapping(
            raw,
            _SEMANTIC_TAG_KEYS,
            "tags_model",
            required=frozenset({"name", "model", "size", "digest", "details", "capabilities"}),
        )
        details = _semantic_details(item["details"])
        _semantic_strings(item["capabilities"], "tags_capabilities", 16)
        if item.get("name") == profile.model_id:
            if selected is not None:
                raise SemanticOllamaExecutionError("model_identity_conflict")
            selected = item
            _semantic_details(details, profile)
    if selected is None:
        raise SemanticOllamaExecutionError("model_missing")
    if selected.get("model") != profile.model_id:
        raise SemanticOllamaExecutionError("model_identity_mismatch")
    if selected.get("size") != profile.model_size_bytes:
        raise SemanticOllamaExecutionError("model_size_mismatch")
    if _semantic_native_model_digest(selected.get("digest")) != profile.model_digest:
        raise SemanticOllamaExecutionError("digest_mismatch")

    show = _semantic_exact_mapping(
        show_payload,
        _SEMANTIC_SHOW_KEYS,
        "show",
        required=frozenset({"license", "details", "model_info", "capabilities"}),
    )
    details = _semantic_details(show["details"], profile)
    capabilities = _semantic_strings(show["capabilities"], "show_capabilities", 16)
    if "completion" not in capabilities:
        raise SemanticOllamaExecutionError("capability_mismatch")
    if (
        details["family"] != profile.model_family
        or details["format"] != profile.model_format
        or details["parameter_size"] != profile.parameter_size
        or details["quantization_level"] != profile.quantization_level
    ):
        raise SemanticOllamaExecutionError("model_details_mismatch")
    license_text = show.get("license")
    if type(license_text) is not str or not license_text or len(license_text) > 65_536:
        raise SemanticOllamaExecutionError("license_evidence_invalid")
    license_fingerprint = "sha256:" + hashlib.sha256(license_text.encode("utf-8")).hexdigest()
    if license_fingerprint != profile.license_text_sha256:
        raise SemanticOllamaExecutionError("license_evidence_mismatch")
    model_info = show.get("model_info")
    if type(model_info) is not dict or len(model_info) > 64:
        raise SemanticOllamaExecutionError("model_info_shape")
    context_length = model_info.get(f"{profile.model_family}.context_length")
    if context_length != profile.context_length:
        raise SemanticOllamaExecutionError("context_length_mismatch")
    identity = canonical_fingerprint(
        {
            "model_id": profile.model_id,
            "details": {
                key: details[key]
                for key in ("family", "format", "parameter_size", "quantization_level")
            },
            "capabilities": sorted(capabilities),
            "license_text_sha256": license_fingerprint,
        }
    )
    if identity != profile.show_identity_sha256:
        raise SemanticOllamaExecutionError("show_identity_mismatch")
    return OllamaModelObservation(
        name=profile.model_id,
        digest=profile.model_digest,
        size_bytes=profile.model_size_bytes,
        family=profile.model_family,
        capabilities=(
            ModelCapability.TEXT_GENERATION.value,
            ModelCapability.STRUCTURED_OUTPUT.value,
        ),
        context_length=profile.context_length,
        quantization=profile.quantization_level,
    )


def _semantic_processes(
    payload: Mapping[str, object], profile: SemanticExecutionProfile
) -> tuple[OllamaProcessObservation, ...]:
    root = _semantic_exact_mapping(
        payload,
        frozenset({"models"}),
        "ps",
        required=frozenset({"models"}),
    )
    raw_models = root["models"]
    if type(raw_models) is not list or len(raw_models) > 64:
        raise SemanticOllamaExecutionError("ps_models")
    results: list[OllamaProcessObservation] = []
    for raw in raw_models:
        item = _semantic_exact_mapping(
            raw,
            _SEMANTIC_PS_KEYS,
            "ps_model",
            required=frozenset(
                {
                    "name",
                    "model",
                    "size",
                    "digest",
                    "details",
                    "expires_at",
                    "size_vram",
                    "context_length",
                }
            ),
        )
        name = item.get("name")
        if type(profile) is SemanticProviderProfile:
            _semantic_details(item["details"], profile if name == profile.model_id else None)
        elif name == profile.model_id:
            current = cast(SemanticChosenModelProfile, profile)
            details = item["details"]
            if not isinstance(details, Mapping):
                raise SemanticOllamaExecutionError("process_identity_mismatch")
            for key, expected in (
                ("family", current.metadata.family),
                ("parameter_size", current.metadata.parameter_size),
                ("quantization_level", current.metadata.quantization),
            ):
                if expected is not None and details.get(key) != expected:
                    raise SemanticOllamaExecutionError("process_identity_mismatch")
        if name != item.get("model"):
            raise SemanticOllamaExecutionError("process_identity_mismatch")
        digest = _semantic_native_model_digest(item.get("digest"))
        process_size = item.get("size")
        size_vram = item.get("size_vram")
        context = item.get("context_length")
        if type(process_size) is not int or process_size <= 0:
            raise SemanticOllamaExecutionError("process_resource_invalid")
        if type(size_vram) is not int or size_vram < 0:
            raise SemanticOllamaExecutionError("process_resource_invalid")
        if type(context) is not int or context <= 0:
            raise SemanticOllamaExecutionError("process_resource_invalid")
        if name == profile.model_id:
            if digest != profile.model_digest:
                raise SemanticOllamaExecutionError("process_digest_mismatch")
            if type(profile) is SemanticChosenModelProfile and (
                process_size > profile.connection.max_model_bytes
                or size_vram > profile.connection.max_model_bytes
                or context > profile.context_length
            ):
                raise SemanticOllamaExecutionError("process_resource_limit")
            results.append(OllamaProcessObservation(name, digest, size_vram, context))
    if len(results) > 1:
        raise SemanticOllamaExecutionError("process_identity_conflict")
    return tuple(results)


def _semantic_chat_result(payload: Mapping[str, object], profile: SemanticExecutionProfile) -> str:
    root = _semantic_exact_mapping(
        payload,
        # IMPORTANT: Ollama 0.35 adds this numeric metric; do not confuse it with unknown
        # content or relax the sealed legacy envelope. All other unknown fields still refuse.
        _SEMANTIC_CHAT_KEYS | {"prompt_eval_cached_count"}
        if type(profile) is SemanticChosenModelProfile
        else _SEMANTIC_CHAT_KEYS,
        "chat",
        required=frozenset({"model", "created_at", "message", "done", "done_reason"}),
    )
    if (
        root["model"] != profile.model_id
        or root["done"] is not True
        or root["done_reason"] != "stop"
    ):
        raise SemanticOllamaExecutionError("generation_incomplete")
    message = _semantic_exact_mapping(
        root["message"],
        frozenset({"role", "content", "thinking"})
        if type(profile) is SemanticChosenModelProfile
        else frozenset({"role", "content"}),
        "chat_message",
        required=frozenset({"role", "content"}),
    )
    content = message["content"]
    thinking = message.get("thinking")
    if thinking is not None and (
        type(thinking) is not str or len(thinking.encode("utf-8")) > 262_144
    ):
        raise SemanticOllamaExecutionError("generation_output_limit")
    # SECURITY: only final content is a proposal; native thinking is discarded, never replayed.
    if message["role"] != "assistant" or type(content) is not str or not content:
        raise SemanticOllamaExecutionError("generation_message_invalid")
    if len(content) > 131_072 or len(content.encode("utf-8")) > 262_144:
        raise SemanticOllamaExecutionError("generation_output_limit")
    created_at = root["created_at"]
    if type(created_at) is not str or not 1 <= len(created_at) <= 128:
        raise SemanticOllamaExecutionError("generation_metadata_invalid")
    for key in (
        "total_duration",
        "load_duration",
        "prompt_eval_count",
        "prompt_eval_cached_count",
        "prompt_eval_duration",
        "eval_count",
        "eval_duration",
    ):
        value = root.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise SemanticOllamaExecutionError("generation_metadata_invalid")
    return content


def _semantic_unload_ok(payload: Mapping[str, object], profile: SemanticExecutionProfile) -> None:
    root = _semantic_exact_mapping(
        payload,
        _SEMANTIC_UNLOAD_KEYS,
        "unload",
        required=frozenset({"model", "created_at", "response", "done", "done_reason"}),
    )
    if root["model"] != profile.model_id or root["done"] is not True:
        raise SemanticOllamaExecutionError("cleanup_failed")
    if root["response"] != "" or root["done_reason"] not in {"unload", "stop"}:
        raise SemanticOllamaExecutionError("cleanup_failed")
    if type(root["created_at"]) is not str or not 1 <= len(root["created_at"]) <= 128:
        raise SemanticOllamaExecutionError("cleanup_failed")
    for key in ("total_duration", "load_duration"):
        value = root.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise SemanticOllamaExecutionError("cleanup_failed")


def _semantic_chat_payload(
    profile: SemanticExecutionProfile,
    generation: ModelGenerationRequest,
) -> dict[str, object]:
    """Build the sole exact M17-16 chat payload without retaining private content."""

    payload: dict[str, object] = {
        "model": profile.model_id,
        "messages": [{"role": "user", "content": generation.prompt}],
        "stream": False,
        "format": (
            generation.structured_schema if generation.structured_schema is not None else "json"
        ),
        "options": {
            "temperature": 0.0,
            "top_k": 0,
            "top_p": 1.0,
            "min_p": 0.0,
            "repeat_penalty": 1.0,
            "presence_penalty": 0.0,
            "num_predict": generation.max_tokens,
            "seed": 0,
        },
        "keep_alive": "30s",
        "think": False,
    }
    if type(profile) is SemanticChosenModelProfile:
        options = cast(dict[str, object], payload["options"])
        measured = (
            len(
                json.dumps(
                    payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("ascii")
            )
            + generation.max_tokens
            + 512
        )
        if measured > profile.context_length:
            raise SemanticOllamaExecutionError("model_context_limit")
        options["num_ctx"] = min(profile.context_length, max(8_192, measured))
        if profile.metadata.reasoning_control_supported is not True:
            payload.pop("think")
    return payload


def _semantic_chat_payload_fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _execute_semantic_ollama_body(
    transport: SemanticOllamaTransport,
    setup: ProviderSetup,
    profile: SemanticExecutionProfile,
    generation: ModelGenerationRequest,
    *,
    cancellation: LocalCancellationProbe | None,
    clock: Callable[[], float],
    sleeper: Callable[[float], None] = time.sleep,
    real_chat_request: Callable[..., Mapping[str, object]],
    real_execution_capability: object | None,
    allow_invalid_json: bool = False,
    action_deadline: float | None = None,
) -> SemanticOllamaExecution:
    if type(allow_invalid_json) is not bool:
        raise SemanticOllamaExecutionError("generation_request_invalid")
    if action_deadline is not None and (
        type(action_deadline) not in {int, float} or not isfinite(action_deadline)
    ):
        raise SemanticOllamaExecutionError("generation_request_invalid")
    if _is_semantic_deadline_transport(transport):
        _consume_semantic_real_execution_capability(transport, real_execution_capability)
    elif real_execution_capability is not None:
        raise SemanticOllamaExecutionError("test_transport_invalid")
    if type(setup) is not ProviderSetup or setup.local_backend is not ProviderLocalBackend.OLLAMA:
        raise SemanticOllamaExecutionError("unsafe_configuration")
    if not setup.policy_valid or setup.fallback_provider is not None:
        raise SemanticOllamaExecutionError("unsafe_configuration")
    if (
        type(profile) not in {SemanticProviderProfile, SemanticChosenModelProfile}
        or profile.endpoint != FIXED_OLLAMA_ENDPOINT
    ):
        raise SemanticOllamaExecutionError("profile_authority")
    if cancellation is not None and not isinstance(cancellation, LocalCancellationProbe):
        raise SemanticOllamaExecutionError("cancellation_invalid")
    if getattr(transport, "endpoint_fingerprint", None) != _semantic_endpoint_fingerprint():
        raise SemanticOllamaExecutionError("endpoint_mismatch")
    if not isinstance(generation, ModelGenerationRequest) or generation.media_fingerprints:
        raise SemanticOllamaExecutionError("generation_request_invalid")
    manifest = profile.model_manifest
    validate_model_generation_request(manifest, generation)
    deadline = clock() + SEMANTIC_OLLAMA_OUTER_SECONDS
    if action_deadline is not None:
        deadline = min(deadline, action_deadline)
    action_limit = deadline
    if type(profile) is SemanticChosenModelProfile:
        # IMPORTANT: current choice discovery/repair/cleanup share one action; reserve cleanup
        # before chat so unloading cannot silently add a fresh wall-clock budget after timeout.
        deadline -= SEMANTIC_OLLAMA_CLEANUP_SECONDS
        _semantic_chat_payload(profile, generation)
    generation_attempted = False
    result: SemanticOllamaExecution | None = None
    primary_error: SemanticOllamaExecutionError | None = None
    try:
        version = _semantic_exact_mapping(
            _semantic_request(
                transport,
                "GET",
                "/api/version",
                None,
                cancellation=cancellation,
                deadline=deadline,
                clock=clock,
            ),
            frozenset({"version"}),
            "version",
        )
        if version["version"] != profile.server_version:
            raise SemanticOllamaExecutionError("version_mismatch")
        tags = _semantic_request(
            transport,
            "GET",
            "/api/tags",
            None,
            cancellation=cancellation,
            deadline=deadline,
            clock=clock,
        )
        show = _semantic_request(
            transport,
            "POST",
            "/api/show",
            {"model": profile.model_id},
            cancellation=cancellation,
            deadline=deadline,
            clock=clock,
        )
        model = _semantic_model_observation(tags, show, profile)
        pre_ps = _semantic_processes(
            _semantic_request(
                transport,
                "GET",
                "/api/ps",
                None,
                cancellation=cancellation,
                deadline=deadline,
                clock=clock,
            ),
            profile,
        )
        pre_process = pre_ps[0] if pre_ps else None
        provider_preflight = ProviderPreflightReceipt(
            setup_fingerprint=setup.setup_fingerprint,
            setup_revision=setup.revision,
            provider=setup.provider,
            local_backend=setup.local_backend,
            status=ProviderPreflightStatus.READY,
            diagnostics=(),
            backend_revision=profile.server_version,
            endpoint_fingerprint=_semantic_endpoint_fingerprint(),
            manifest_id=manifest.manifest_id,
            model_digest=manifest.model_digest,
            network_attempted=True,
        )
        consent = build_provider_consent_authority(setup)
        admit_provider_execution(setup, consent, provider_preflight)
        generation_attempted = True
        if _is_semantic_deadline_transport(transport):
            response = real_chat_request(
                transport,
                setup,
                profile,
                generation,
                provider_preflight,
                cancellation=cancellation,
                deadline=deadline,
                clock=clock,
            )
        else:
            response = _semantic_request(
                transport,
                "POST",
                "/api/chat",
                _semantic_chat_payload(profile, generation),
                cancellation=cancellation,
                deadline=deadline,
                clock=clock,
            )
        text = _semantic_chat_result(response, profile)
        if _is_semantic_deadline_transport(transport):
            generation_request_bytes = transport.last_request_body_bytes
            generation_response_bytes = transport.last_response_body_bytes
        else:
            # Hermetic transports expose decoded objects only; these counts are canonical payload
            # measurements. The real deadline transport records actual HTTP body byte counts.
            generation_request_bytes = len(
                json.dumps(
                    _semantic_chat_payload(profile, generation),
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("ascii")
            )
            generation_response_bytes = len(
                json.dumps(
                    response,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("ascii")
            )
        try:
            model_result = build_model_result(manifest, generation, text)
        except ModelOutputError:
            if not allow_invalid_json:
                raise
            # IMPORTANT: this opt-in carries bounded final output to the constrained repair gate.
            # It grants no proposal/apply authority; the default native parser remains strict.
            model_result = ModelGenerationResult(
                manifest.backend_family,
                manifest.model_id,
                manifest.model_digest,
                canonical_fingerprint({"text": text, "schema": generation.output_schema}),
                {},
                text,
                manifest.parser_path,
                diagnostics=("semantic_json_invalid",),
            )
        post_processes = _semantic_processes(
            _semantic_request(
                transport,
                "GET",
                "/api/ps",
                None,
                cancellation=cancellation,
                deadline=deadline,
                clock=clock,
            ),
            profile,
        )
        if len(post_processes) != 1:
            raise SemanticOllamaExecutionError("postflight_process_missing")
        qualification_fingerprint = canonical_fingerprint(
            {
                "manifest": manifest.fingerprint,
                "model_observation": model.to_public_dict(),
                "preexisting_process": (
                    None if pre_process is None else pre_process.to_public_dict()
                ),
                "provider_preflight": provider_preflight.to_public_dict(),
            }
        )
        if clock() >= deadline:
            raise SemanticOllamaExecutionError("timeout")
        if _semantic_cancelled(cancellation):
            raise SemanticOllamaExecutionError("cancelled")
        result = SemanticOllamaExecution(
            model_result=model_result,
            provider_preflight=provider_preflight,
            qualification_fingerprint=qualification_fingerprint,
            capability_fingerprint=canonical_fingerprint(
                {
                    "qualification": qualification_fingerprint,
                    "profile": profile.fingerprint,
                    "raw_media": False,
                }
            ),
            request_bytes=generation_request_bytes,
            response_bytes=generation_response_bytes,
            prompt_tokens=cast(int, response.get("prompt_eval_count", 0)),
            completion_tokens=cast(int, response.get("eval_count", 0)),
        )
    except SemanticOllamaExecutionError as exc:
        primary_error = exc
    except (
        ContractValidationError,
        SecurityPolicyError,
        ModelManifestError,
        ModelOutputError,
    ) as exc:
        primary_error = SemanticOllamaExecutionError("provider_output_invalid")
        primary_error.__cause__ = exc
    finally:
        if generation_attempted:
            cleanup_deadline = clock() + SEMANTIC_OLLAMA_CLEANUP_SECONDS
            if type(profile) is SemanticChosenModelProfile:
                cleanup_deadline = min(cleanup_deadline, action_limit)
            try:
                unload = _semantic_request(
                    transport,
                    "POST",
                    "/api/generate",
                    {"model": profile.model_id, "keep_alive": 0},
                    cancellation=None,
                    deadline=cleanup_deadline,
                    clock=clock,
                )
                _semantic_unload_ok(unload, profile)
                cleanup_polls = 0
                while True:
                    remaining = _semantic_processes(
                        _semantic_request(
                            transport,
                            "GET",
                            "/api/ps",
                            None,
                            cancellation=None,
                            deadline=cleanup_deadline,
                            clock=clock,
                        ),
                        profile,
                    )
                    cleanup_polls += 1
                    if not remaining:
                        break
                    if cleanup_polls >= SEMANTIC_OLLAMA_CLEANUP_MAX_POLLS:
                        raise SemanticOllamaExecutionError("cleanup_failed")
                    cleanup_remaining = cleanup_deadline - clock()
                    if cleanup_remaining <= 0:
                        raise SemanticOllamaExecutionError("cleanup_failed")
                    wait_seconds = min(
                        SEMANTIC_OLLAMA_CLEANUP_POLL_SECONDS,
                        cleanup_remaining,
                    )
                    # IMPORTANT: async unload may acknowledge before residency disappears. Keep the
                    # polling schedule non-busy and anchored after each completed observation.
                    sleeper(wait_seconds)
                    if wait_seconds < SEMANTIC_OLLAMA_CLEANUP_POLL_SECONDS:
                        raise SemanticOllamaExecutionError("cleanup_failed")
            except SemanticOllamaExecutionError as exc:
                result = None
                if primary_error is None:
                    primary_error = SemanticOllamaExecutionError("cleanup_failed")
                    primary_error.__cause__ = exc
    if primary_error is not None:
        raise primary_error
    if result is None:
        raise SemanticOllamaExecutionError("provider_execution_failed")
    # CRITICAL: cleanup is unconditional, but it cannot extend successful publication authority.
    if clock() >= deadline:
        raise SemanticOllamaExecutionError("timeout")
    if _semantic_cancelled(cancellation):
        raise SemanticOllamaExecutionError("cancelled")
    return result


def _build_semantic_execution_boundary() -> tuple[
    Callable[..., SemanticOllamaExecution],
    Callable[[SemanticDeadlineOllamaTransport, object | None], None],
    Callable[[SemanticDeadlineOllamaTransport, dict[str, object], float], None],
    Callable[..., SemanticOllamaExecution],
]:
    lease_lock = threading.Lock()
    active_execution: tuple[object, SemanticDeadlineOllamaTransport] | None = None
    active_lease: (
        tuple[
            object,
            SemanticDeadlineOllamaTransport,
            dict[str, object],
            str,
            float,
        ]
        | None
    ) = None

    def consume_real_execution_capability(
        transport: SemanticDeadlineOllamaTransport,
        capability: object | None,
    ) -> None:
        nonlocal active_execution
        with lease_lock:
            admitted = active_execution
            if (
                admitted is None
                or admitted[0] is not capability
                or admitted[1] is not transport
                or not _SEMANTIC_EXECUTION_LOCK.locked()
            ):
                raise SemanticOllamaExecutionError("test_transport_invalid")
            active_execution = None

    def consume_chat_lease(
        transport: SemanticDeadlineOllamaTransport,
        payload: dict[str, object],
        absolute_deadline: float,
    ) -> None:
        nonlocal active_lease
        with lease_lock:
            lease = active_lease
            if (
                lease is None
                or lease[1] is not transport
                or lease[2] is not payload
                or lease[4] != absolute_deadline
            ):
                raise ModelTransportError("semantic_http_request")
            active_lease = None
        try:
            exact = lease[3] == _semantic_chat_payload_fingerprint(payload)
        # CRITICAL: a malformed or mutated private chat payload must fail before socket contact.
        except Exception:
            raise ModelTransportError("semantic_http_request") from None
        if not exact:
            raise ModelTransportError("semantic_http_request")

    def real_chat_request(
        transport: SemanticDeadlineOllamaTransport,
        setup: ProviderSetup,
        profile: SemanticExecutionProfile,
        generation: ModelGenerationRequest,
        preflight: ProviderPreflightReceipt,
        *,
        cancellation: LocalCancellationProbe | None,
        deadline: float,
        clock: Callable[[], float],
    ) -> Mapping[str, object]:
        nonlocal active_lease
        try:
            if (
                not _is_semantic_deadline_transport(transport)
                or type(setup) is not ProviderSetup
                or type(profile) not in {SemanticProviderProfile, SemanticChosenModelProfile}
                or type(generation) is not ModelGenerationRequest
                or type(preflight) is not ProviderPreflightReceipt
                or transport._expected_model_id != profile.model_id
                or transport.endpoint_fingerprint != _semantic_endpoint_fingerprint()
                or type(deadline) not in {int, float}
                or not isfinite(float(deadline))
                or float(deadline) <= clock()
            ):
                raise ModelTransportError("semantic_http_request")
            validate_model_generation_request(profile.model_manifest, generation)
            consent = build_provider_consent_authority(setup)
            admit_provider_execution(setup, consent, preflight)
            payload = _semantic_chat_payload(profile, generation)
            payload_fingerprint = _semantic_chat_payload_fingerprint(payload)
        except ModelTransportError:
            raise
        except Exception:
            raise ModelTransportError("semantic_http_request") from None
        lease_identity = object()
        lease = (
            lease_identity,
            transport,
            payload,
            payload_fingerprint,
            float(deadline),
        )
        with lease_lock:
            if active_lease is not None:
                raise ModelTransportError("semantic_http_request")
            active_lease = lease
        try:
            return _semantic_request(
                transport,
                "POST",
                "/api/chat",
                payload,
                cancellation=cancellation,
                deadline=float(deadline),
                clock=clock,
            )
        finally:
            with lease_lock:
                if active_lease is lease:
                    active_lease = None

    def execute_public(
        transport: SemanticOllamaTransport,
        setup: ProviderSetup,
        profile: SemanticExecutionProfile,
        generation: ModelGenerationRequest,
        *,
        cancellation: LocalCancellationProbe | None = None,
        allow_invalid_json: bool = False,
        action_deadline: float | None = None,
    ) -> SemanticOllamaExecution:
        nonlocal active_execution
        if not _SEMANTIC_EXECUTION_LOCK.acquire(blocking=False):
            raise SemanticOllamaExecutionError("provider_busy")
        execution_admission: tuple[object, SemanticDeadlineOllamaTransport] | None = None
        try:
            with _SEMANTIC_EXECUTION_AUTHORITY_LOCK:
                if len(_SEMANTIC_EXECUTION_AUTHORITIES) >= _MAX_SEMANTIC_EXECUTION_AUTHORITIES:
                    raise SemanticOllamaExecutionError("execution_authority_capacity")
            capability: object | None = None
            if _is_semantic_deadline_transport(transport):
                capability = object()
                execution_admission = (capability, transport)
                with lease_lock:
                    if active_execution is not None:
                        raise SemanticOllamaExecutionError("provider_busy")
                    active_execution = execution_admission
            result = _execute_semantic_ollama_body(
                transport,
                setup,
                profile,
                generation,
                cancellation=cancellation,
                clock=time.monotonic,
                real_chat_request=real_chat_request,
                real_execution_capability=capability,
                allow_invalid_json=allow_invalid_json,
                action_deadline=action_deadline,
            )
            with _SEMANTIC_EXECUTION_AUTHORITY_LOCK:
                _SEMANTIC_EXECUTION_AUTHORITIES[id(result)] = _semantic_execution_authority(result)
            return result
        finally:
            if execution_admission is not None:
                with lease_lock:
                    if active_execution is execution_admission:
                        active_execution = None
            _SEMANTIC_EXECUTION_LOCK.release()

    def execute_test_only(
        transport: SemanticOllamaTransport,
        setup: ProviderSetup,
        profile: SemanticExecutionProfile,
        generation: ModelGenerationRequest,
        *,
        cancellation: LocalCancellationProbe | None,
        clock: Callable[[], float],
        sleeper: Callable[[float], None] = time.sleep,
        allow_invalid_json: bool = False,
        action_deadline: float | None = None,
    ) -> SemanticOllamaExecution:
        if _is_semantic_deadline_transport(transport):
            raise SemanticOllamaExecutionError("test_transport_invalid")
        return _execute_semantic_ollama_body(
            transport,
            setup,
            profile,
            generation,
            cancellation=cancellation,
            clock=clock,
            sleeper=sleeper,
            real_chat_request=real_chat_request,
            real_execution_capability=None,
            allow_invalid_json=allow_invalid_json,
            action_deadline=action_deadline,
        )

    return (
        execute_public,
        consume_real_execution_capability,
        consume_chat_lease,
        execute_test_only,
    )


(
    execute_semantic_ollama,
    _consume_semantic_real_execution_capability,
    _consume_semantic_chat_lease,
    _execute_semantic_ollama_test_only,
) = _build_semantic_execution_boundary()


__all__ = [
    "OLLAMA_ALLOWED_PATHS",
    "OLLAMA_TRANSPORT_SCHEMA",
    "LoopbackOllamaTransport",
    "OllamaNativeAdapter",
    "OllamaTransport",
    "SemanticDeadlineOllamaTransport",
    "SemanticOllamaTransport",
    "SemanticOllamaExecution",
    "SemanticOllamaExecutionError",
    "execute_semantic_ollama",
    "consume_semantic_ollama_execution_authority",
    "preflight_ollama",
    "run_ollama_provider_preflight",
]
