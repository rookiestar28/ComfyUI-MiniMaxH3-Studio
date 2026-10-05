"""Bounded same-origin identity authority for an operator-selected ComfyUI input image.

The browser sends one transient host locator. This adapter resolves it only beneath the configured
ComfyUI input root, reads only a bounded image header, and returns an opaque short-lived receipt.
Paths, filenames, image bytes, metadata and dimensions never enter the public receipt or an
exception. Output geometry is deliberately not predicted here.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import struct
import sys
import threading
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

from ..core.canonical import canonical_bytes
from ..core.safe_paths import UnsafePathError, validate_directory, validate_regular_file
from .comfyui_route_seam import RoutePolicy, RouteResult, register_owned_route
from .composition_root import INPUT_GEOMETRY, component
from .segment_artifact_store import (
    ArtifactStoreError,
    _identity,
    _is_link_or_reparse,
    _validated_directories,
    _windows_open_existing_file,
)

INPUT_GEOMETRY_REQUEST_SCHEMA = "h3.context.input_geometry.request.v2"
INPUT_GEOMETRY_RECEIPT_SCHEMA = "h3.context.input_geometry.receipt.v2"
INPUT_GEOMETRY_ERROR_SCHEMA = "h3.context.input_geometry.error.v1"
INPUT_GEOMETRY_ROUTE = "/h3-context/v1/input/geometry"

MAX_INPUT_GEOMETRY_REQUEST_BYTES = 4_096
MAX_INPUT_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_METADATA_BYTES = 64 * 1024
MAX_IMAGE_METADATA_OFFSET = 1 * 1024 * 1024
MAX_SOURCE_DIMENSION = 32_768
MAX_SOURCE_PIXELS = 100_000_000
MAX_GEOMETRY_RECEIPTS = 16
# IMPORTANT: ProductShell's model-free bootstrap may take up to 120 seconds. Keep this
# bounded window long enough to reach prepare; claim still re-probes the file immediately.
GEOMETRY_RECEIPT_TTL_SECONDS = 180.0
MAX_GEOMETRY_CONCURRENCY = 2
GEOMETRY_DEADLINE_SECONDS = 2.0

_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RECEIPT_HANDLE = re.compile(r"ig_[A-Za-z0-9_-]{32,96}\Z")
_WINDOWS_RESERVED_NAME = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])\Z", re.I)
_ROUTE_OWNER_ATTRIBUTE = "__h3_context_input_geometry_v2__"
_ROUTE_REGISTERED = False
_PROCESS_SECRET = secrets.token_bytes(32)


class GeometryPreflightError(ValueError):
    """A typed content-free geometry refusal."""

    def __init__(self, code: str, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _fingerprint(value: object, field_name: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise GeometryPreflightError(f"invalid_{field_name}", 400)
    return value


def _closed(value: object, keys: set[str], field_name: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise GeometryPreflightError(f"invalid_{field_name}", 400)
    return value


def _locator_parts(value: object) -> tuple[str, ...]:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 512
        or value != value.strip()
        or "\x00" in value
        or ":" in value
        or "://" in value
        or value.startswith(("/", "\\"))
    ):
        raise GeometryPreflightError("input_locator_unsafe", 422)
    normalized = value.replace("\\", "/")
    parts = tuple(normalized.split("/"))
    # CRITICAL: Windows normalizes these components before open. Refuse aliases instead of
    # allowing the browser's locator and the host's filesystem target to name different things.
    if not parts or any(
        part in {"", ".", ".."}
        or part.endswith((".", " "))
        or any(ord(character) < 32 or character in '<>"|?*' for character in part)
        or _WINDOWS_RESERVED_NAME.fullmatch(part.split(".", 1)[0]) is not None
        for part in parts
    ):
        raise GeometryPreflightError("input_locator_unsafe", 422)
    return parts


@dataclass(frozen=True, slots=True)
class InputGeometryRequest:
    locator: str = field(repr=False)
    schema: str = INPUT_GEOMETRY_REQUEST_SCHEMA

    @classmethod
    def from_wire(cls, value: object) -> InputGeometryRequest:
        wire = _closed(value, {"schema", "locator"}, "request")
        if wire["schema"] != INPUT_GEOMETRY_REQUEST_SCHEMA:
            raise GeometryPreflightError("invalid_request", 400)
        locator = wire["locator"]
        _locator_parts(locator)
        return cls(locator=locator)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class InputGeometryReceipt:
    receipt_handle: str
    source_fingerprint: str
    schema: str = INPUT_GEOMETRY_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != INPUT_GEOMETRY_RECEIPT_SCHEMA:
            raise GeometryPreflightError("invalid_geometry_receipt", 400)
        if _RECEIPT_HANDLE.fullmatch(self.receipt_handle) is None:
            raise GeometryPreflightError("invalid_geometry_receipt", 400)
        _fingerprint(self.source_fingerprint, "source_fingerprint")

    @classmethod
    def from_wire(cls, value: object) -> InputGeometryReceipt:
        wire = _closed(
            value,
            {"schema", "receipt_handle", "source_fingerprint"},
            "geometry_receipt",
        )
        string_fields = ("schema", "receipt_handle", "source_fingerprint")
        if any(type(wire[name]) is not str for name in string_fields):
            raise GeometryPreflightError("invalid_geometry_receipt", 400)
        return cls(
            schema=wire["schema"],  # type: ignore[arg-type]
            receipt_handle=wire["receipt_handle"],  # type: ignore[arg-type]
            source_fingerprint=wire["source_fingerprint"],  # type: ignore[arg-type]
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "receipt_handle": self.receipt_handle,
            "source_fingerprint": self.source_fingerprint,
        }


class _MetadataReader:
    def __init__(self, descriptor: int, file_size: int, deadline: float) -> None:
        self._descriptor = descriptor
        self._file_size = file_size
        self._deadline = deadline
        self._read_bytes = 0
        self._digest = hashlib.sha256()

    @property
    def file_size(self) -> int:
        return self._file_size

    def read_at(self, offset: int, count: int) -> bytes:
        if (
            time.monotonic() >= self._deadline
            or offset < 0
            or count < 1
            or offset + count > self._file_size
            or offset + count > MAX_IMAGE_METADATA_OFFSET
            or self._read_bytes + count > MAX_IMAGE_METADATA_BYTES
        ):
            raise GeometryPreflightError("image_header_unsupported", 415)
        try:
            os.lseek(self._descriptor, offset, os.SEEK_SET)
            chunks: list[bytes] = []
            remaining = count
            while remaining > 0:
                chunk = os.read(self._descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        except OSError as exc:
            raise GeometryPreflightError("input_unavailable", 404) from exc
        payload = b"".join(chunks)
        if len(payload) != count:
            raise GeometryPreflightError("image_header_unsupported", 415)
        self._read_bytes += count
        self._digest.update(struct.pack(">QI", offset, count))
        self._digest.update(payload)
        return payload

    def digest(self) -> bytes:
        return self._digest.digest()


def _jpeg_dimensions(reader: _MetadataReader) -> tuple[int, int]:
    if reader.read_at(0, 2) != b"\xff\xd8":
        raise GeometryPreflightError("image_format_unsupported", 415)
    offset = 2
    start_of_frame = frozenset(
        {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    )
    for _ in range(256):
        marker_prefix = reader.read_at(offset, 1)
        if marker_prefix != b"\xff":
            raise GeometryPreflightError("image_header_unsupported", 415)
        while marker_prefix == b"\xff":
            offset += 1
            marker_prefix = reader.read_at(offset, 1)
        marker = marker_prefix[0]
        offset += 1
        if marker in {0x01, *range(0xD0, 0xDA)}:
            continue
        if marker in {0xD8, 0xD9, 0xDA}:
            raise GeometryPreflightError("image_header_unsupported", 415)
        length = int.from_bytes(reader.read_at(offset, 2), "big")
        if length < 2:
            raise GeometryPreflightError("image_header_unsupported", 415)
        if marker in start_of_frame:
            frame = reader.read_at(offset + 2, 5)
            height = int.from_bytes(frame[1:3], "big")
            width = int.from_bytes(frame[3:5], "big")
            return width, height
        offset += length
        if offset > MAX_IMAGE_METADATA_OFFSET:
            break
    raise GeometryPreflightError("image_header_unsupported", 415)


def _image_dimensions(reader: _MetadataReader) -> tuple[int, int]:
    if reader.file_size < 16:
        raise GeometryPreflightError("image_format_unsupported", 415)
    prefix = reader.read_at(0, 16)
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        header = reader.read_at(0, 33)
        if header[8:16] != b"\x00\x00\x00\rIHDR":
            raise GeometryPreflightError("image_header_unsupported", 415)
        expected_crc = int.from_bytes(header[29:33], "big")
        if zlib.crc32(header[12:29]) != expected_crc:
            raise GeometryPreflightError("image_header_unsupported", 415)
        return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        header = reader.read_at(0, 10)
        return int.from_bytes(header[6:8], "little"), int.from_bytes(header[8:10], "little")
    if prefix.startswith(b"\xff\xd8"):
        return _jpeg_dimensions(reader)
    if prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        header = reader.read_at(0, 30)
        chunk = header[12:16]
        if chunk == b"VP8X":
            return (
                1 + int.from_bytes(header[24:27], "little"),
                1 + int.from_bytes(header[27:30], "little"),
            )
        if chunk == b"VP8L" and header[20] == 0x2F:
            bits = int.from_bytes(header[21:25], "little")
            return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
        if chunk == b"VP8 " and header[23:26] == b"\x9d\x01\x2a":
            return (
                int.from_bytes(header[26:28], "little") & 0x3FFF,
                int.from_bytes(header[28:30], "little") & 0x3FFF,
            )
        raise GeometryPreflightError("image_header_unsupported", 415)
    raise GeometryPreflightError("image_format_unsupported", 415)


@dataclass(frozen=True, slots=True)
class _FileProbe:
    width: int
    height: int
    identity: tuple[int, int, int, int]
    header_digest: bytes = field(repr=False)


def _probe_file(path: Path, *, deadline: float) -> _FileProbe:
    try:
        leaf = path.lstat()
    except FileNotFoundError as exc:
        raise GeometryPreflightError("input_unavailable", 404) from exc
    except OSError as exc:
        raise GeometryPreflightError("input_unavailable", 404) from exc
    if _is_link_or_reparse(path, leaf) or not stat.S_ISREG(leaf.st_mode) or leaf.st_nlink != 1:
        raise GeometryPreflightError("input_unsafe", 422)
    if not 1 <= leaf.st_size <= MAX_INPUT_IMAGE_BYTES:
        raise GeometryPreflightError("input_too_large", 413)
    try:
        with _validated_directories(path.parent):
            admitted = validate_regular_file(path, maximum_bytes=MAX_INPUT_IMAGE_BYTES)
            before = admitted.lstat()
            descriptor = (
                _windows_open_existing_file(admitted)
                if os.name == "nt"
                else os.open(
                    admitted,
                    os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
            )
            try:
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or _identity(opened) != _identity(before)
                ):
                    raise GeometryPreflightError("input_unsafe", 422)
                reader = _MetadataReader(descriptor, opened.st_size, deadline)
                width, height = _image_dimensions(reader)
                after_handle = os.fstat(descriptor)
                after_path = admitted.lstat()
                if (
                    _is_link_or_reparse(admitted, after_path)
                    or after_path.st_nlink != 1
                    or _identity(after_handle) != _identity(opened)
                    or _identity(after_path) != _identity(opened)
                ):
                    raise GeometryPreflightError("input_changed", 409)
                if (
                    type(width) is not int
                    or type(height) is not int
                    or not 1 <= width <= MAX_SOURCE_DIMENSION
                    or not 1 <= height <= MAX_SOURCE_DIMENSION
                    or width * height > MAX_SOURCE_PIXELS
                ):
                    raise GeometryPreflightError("image_pixels_exceeded", 413)
                return _FileProbe(width, height, _identity(opened), reader.digest())
            finally:
                os.close(descriptor)
    except GeometryPreflightError:
        raise
    except UnsafePathError as exc:
        raise GeometryPreflightError("input_unsafe", 422) from exc
    except ArtifactStoreError as exc:
        # CRITICAL: directory pinning has a private error type; keep this media boundary typed.
        if exc.code == "unsafe_store_entry":
            raise GeometryPreflightError("input_unsafe", 422) from exc
        raise GeometryPreflightError("input_unavailable", 404) from exc
    except OSError as exc:
        raise GeometryPreflightError("input_unavailable", 404) from exc


@dataclass(frozen=True, slots=True)
class _ReceiptEntry:
    receipt: InputGeometryReceipt
    source_path: Path = field(repr=False)
    source_fingerprint: str
    expires_at: float


class InputGeometryRegistry:
    """Bounded process-local owner for short-lived source-identity receipts."""

    def __init__(
        self,
        *,
        input_root_factory: Callable[[], Path],
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(40),
        fingerprint_secret: bytes = _PROCESS_SECRET,
        max_entries: int = MAX_GEOMETRY_RECEIPTS,
        ttl_seconds: float = GEOMETRY_RECEIPT_TTL_SECONDS,
    ) -> None:
        if (
            not callable(input_root_factory)
            or not callable(clock)
            or not callable(token_factory)
            or type(fingerprint_secret) is not bytes
            or len(fingerprint_secret) < 32
            or type(max_entries) is not int
            or not 1 <= max_entries <= MAX_GEOMETRY_RECEIPTS
            or type(ttl_seconds) not in {int, float}
            or not 1 <= float(ttl_seconds) <= GEOMETRY_RECEIPT_TTL_SECONDS
        ):
            raise GeometryPreflightError("invalid_geometry_configuration", 500)
        self._input_root_factory = input_root_factory
        self._clock = clock
        self._token_factory = token_factory
        self._secret = fingerprint_secret
        self._max_entries = max_entries
        self._ttl_seconds = float(ttl_seconds)
        self._entries: OrderedDict[str, _ReceiptEntry] = OrderedDict()
        self._lock = threading.RLock()
        self._slots = threading.BoundedSemaphore(MAX_GEOMETRY_CONCURRENCY)

    def _prune(self, now: float) -> None:
        for handle, entry in tuple(self._entries.items()):
            if now >= entry.expires_at:
                self._entries.pop(handle, None)

    def _source_fingerprint(self, probe: _FileProbe) -> str:
        payload = canonical_bytes(
            {
                "schema": "h3.context.input_geometry.private_identity.v2",
                # Windows volume/file identifiers may exceed the cross-language integer range;
                # decimal strings retain the exact private identity without widening the wire.
                "file_identity": [str(value) for value in probe.identity],
                "header_digest": probe.header_digest.hex(),
                # Source dimensions are part of the private selected-file identity only. They are
                # never returned or used to predict the graph's delivered output geometry.
                "source_dimensions": [probe.width, probe.height],
            }
        )
        return "sha256:" + hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def observe(self, value: object) -> InputGeometryReceipt:
        request = InputGeometryRequest.from_wire(value)
        if not self._slots.acquire(blocking=False):
            raise GeometryPreflightError("geometry_capacity", 429)
        try:
            root_value = self._input_root_factory()
            if not isinstance(root_value, Path):
                raise GeometryPreflightError("host_input_unavailable", 503)
            try:
                root = validate_directory(root_value)
            except UnsafePathError as exc:
                raise GeometryPreflightError("host_input_unavailable", 503) from exc
            source = root.joinpath(*_locator_parts(request.locator))
            probe = _probe_file(source, deadline=time.monotonic() + GEOMETRY_DEADLINE_SECONDS)
            source_fingerprint = self._source_fingerprint(probe)
            token = self._token_factory()
            handle = f"ig_{token}"
            if _RECEIPT_HANDLE.fullmatch(handle) is None:
                raise GeometryPreflightError("geometry_identity_unavailable", 503)
            receipt = InputGeometryReceipt(
                receipt_handle=handle,
                source_fingerprint=source_fingerprint,
            )
            now = self._clock()
            with self._lock:
                self._prune(now)
                if handle in self._entries:
                    raise GeometryPreflightError("geometry_identity_unavailable", 503)
                if len(self._entries) >= self._max_entries:
                    raise GeometryPreflightError("geometry_capacity", 429)
                self._entries[handle] = _ReceiptEntry(
                    receipt=receipt,
                    source_path=source,
                    source_fingerprint=source_fingerprint,
                    expires_at=now + self._ttl_seconds,
                )
            return receipt
        finally:
            self._slots.release()

    def claim(self, value: object) -> InputGeometryReceipt:
        receipt = InputGeometryReceipt.from_wire(value)
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.pop(receipt.receipt_handle, None)
        if entry is None:
            raise GeometryPreflightError("geometry_receipt_stale", 409)
        if entry.receipt != receipt:
            raise GeometryPreflightError("geometry_receipt_mismatch", 422)
        try:
            probe = _probe_file(
                entry.source_path,
                deadline=time.monotonic() + GEOMETRY_DEADLINE_SECONDS,
            )
            current_fingerprint = self._source_fingerprint(probe)
        except GeometryPreflightError as exc:
            if exc.code == "input_unavailable":
                raise GeometryPreflightError("input_changed", 409) from exc
            raise
        if current_fingerprint != entry.source_fingerprint:
            raise GeometryPreflightError("input_changed", 409)
        return receipt


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("non-finite JSON number")


def decode_input_geometry_request_json(payload: bytes) -> dict[str, object]:
    try:
        if (
            type(payload) is not bytes
            or not payload
            or len(payload) > MAX_INPUT_GEOMETRY_REQUEST_BYTES
        ):
            raise ValueError("body bound")
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
        if type(value) is not dict or len(value) > 8:
            raise ValueError("body shape")
        InputGeometryRequest.from_wire(value)
        return value
    except GeometryPreflightError:
        raise
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GeometryPreflightError("invalid_request", 400) from exc


def _host_input_root() -> Path:
    module = sys.modules.get("folder_paths")
    factory = getattr(module, "get_input_directory", None)
    if not callable(factory):
        raise GeometryPreflightError("host_input_unavailable", 503)
    value = factory()
    if type(value) is not str or not value:
        raise GeometryPreflightError("host_input_unavailable", 503)
    return Path(value)


def build_registry() -> InputGeometryRegistry:
    """Construct this adapter's process registry. Called only by the composition root."""

    return InputGeometryRegistry(input_root_factory=_host_input_root)


_registry = component(INPUT_GEOMETRY, InputGeometryRegistry)


def claim_input_geometry_receipt(value: object) -> InputGeometryReceipt:
    return _registry().claim(value)


def _error_body(category: str) -> dict[str, str]:
    return {"schema": INPUT_GEOMETRY_ERROR_SCHEMA, "category": category}


_ROUTE_POLICY = RoutePolicy(
    path=INPUT_GEOMETRY_ROUTE,
    owner=INPUT_GEOMETRY_RECEIPT_SCHEMA,
    owner_attribute=_ROUTE_OWNER_ATTRIBUTE,
    max_bytes=MAX_INPUT_GEOMETRY_REQUEST_BYTES,
    refusals=(GeometryPreflightError,),
)


def _refusal_body(_status: int, reason: str) -> dict[str, str]:
    return _error_body(reason)


def _refuse(error: BaseException) -> RouteResult:
    status = getattr(error, "status", 400)
    code = getattr(error, "code", None)
    return RouteResult(
        status if isinstance(status, int) and not isinstance(status, bool) else 400,
        _error_body(code if isinstance(code, str) else "invalid_request"),
    )


async def _observe(payload: bytes, _context: object) -> RouteResult:
    action = decode_input_geometry_request_json(payload)
    # Header probing opens a file and reads from it; keep that off the ComfyUI event loop.
    receipt = await asyncio.to_thread(_registry().observe, action)
    return RouteResult(200, receipt.to_wire())


def ensure_input_geometry_route_registered() -> bool:
    """Lazily register the owned same-origin geometry route."""

    global _ROUTE_REGISTERED
    _ROUTE_REGISTERED = register_owned_route(
        _ROUTE_POLICY,
        __name__,
        _observe,
        _refusal_body,
        refusal_mapper=_refuse,
    )
    return _ROUTE_REGISTERED


__all__ = [
    "GEOMETRY_RECEIPT_TTL_SECONDS",
    "INPUT_GEOMETRY_ERROR_SCHEMA",
    "INPUT_GEOMETRY_RECEIPT_SCHEMA",
    "INPUT_GEOMETRY_REQUEST_SCHEMA",
    "INPUT_GEOMETRY_ROUTE",
    "MAX_INPUT_GEOMETRY_REQUEST_BYTES",
    "MAX_INPUT_IMAGE_BYTES",
    "GeometryPreflightError",
    "InputGeometryReceipt",
    "InputGeometryRegistry",
    "claim_input_geometry_receipt",
    "decode_input_geometry_request_json",
    "ensure_input_geometry_route_registered",
]
