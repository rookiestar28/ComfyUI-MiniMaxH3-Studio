"""Closed, content-free Authoring output transport values; no execution authority."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, fields
from typing import Final, Literal, NoReturn, cast

from .composition_contract import OUTPUT_PROFILE_ID

OUTPUT_CREATE_SCHEMA: Final = "h3.authoring.output_create.v1"
OUTPUT_CANCEL_SCHEMA: Final = "h3.authoring.output_cancel.v1"
OUTPUT_STATUS_SCHEMA: Final = "h3.authoring.output_status.v1"
OUTPUT_CAPABILITY_SCHEMA: Final = "h3.authoring.output_capability.v1"
OUTPUT_ERROR_SCHEMA: Final = "h3.authoring.output_error.v1"
OUTPUT_PREVIEW_PROFILE: Final = "authoring.final_preview.h264_aac_640x360_24fps.v1"
OUTPUT_REQUEST_BYTES: Final = 4096
OUTPUT_MAX_BYTES: Final = 512 * 1024 * 1024
PREVIEW_MAX_BYTES: Final = 16 * 1024 * 1024
OUTPUT_MAX_FRAMES: Final = 3600
OUTPUT_RESPONSE_CHUNK: Final = 64 * 1024
OUTPUT_TTL_SECONDS: Final = 3600
OUTPUT_RESPONSE_SECONDS: Final = 120
OUTPUT_WRITE_SECONDS: Final = 10
OUTPUT_JOB_PATH: Final = "/h3-context/v1/authoring/render"
OUTPUT_MEDIA_PATH: Final = "/h3-context/v1/authoring/output"
#: M25-16: the read-only capability projection the expanded workspace reads before it mounts
#: the final-output leaf. Its path deliberately shares no prefix with the `{handle}` routes.
OUTPUT_CAPABILITY_PATH: Final = "/h3-context/v1/authoring/output-capability"


def output_capability_wire(*, supported: bool) -> dict[str, object]:
    """The closed capability projection; `supported` is the only runtime-dependent field."""
    return {
        "schema": OUTPUT_CAPABILITY_SCHEMA,
        "supported": bool(supported),
        "output_profile_id": OUTPUT_PROFILE_ID,
        "preview_profile_id": OUTPUT_PREVIEW_PROFILE,
        "job_path": OUTPUT_JOB_PATH,
        "output_path": OUTPUT_MEDIA_PATH,
        "max_output_bytes": OUTPUT_MAX_BYTES,
        "max_preview_bytes": PREVIEW_MAX_BYTES,
        "max_duration_frames": OUTPUT_MAX_FRAMES,
        "frame_rate_num": 24,
        "frame_rate_den": 1,
        "job_ttl_seconds": OUTPUT_TTL_SECONDS,
        "max_jobs": 32,
        "max_outputs": 16,
        "max_byte_responses": 1,
        "max_http_requests": 4,
        "max_response_seconds": OUTPUT_RESPONSE_SECONDS,
        "max_write_seconds": OUTPUT_WRITE_SECONDS,
        "preview_threads": 2,
        "preview_memory_bytes": 256 * 1024 * 1024,
        "preview_seconds": 60,
    }


_WORKSPACE = re.compile(r"authoring-[0-9a-f]{32}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDEMPOTENCY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{15,127}\Z")
_HANDLES = {
    "job": re.compile(r"arj_[A-Za-z0-9_-]{22}\Z"),
    "output": re.compile(r"aro_[A-Za-z0-9_-]{22}\Z"),
}
_RANGE = re.compile(r"bytes=([0-9]{0,19})-([0-9]{0,19})\Z")
_ERROR_STATUSES: Final = {
    "invalid_request": 400,
    "forbidden": 403,
    "unavailable": 404,
    "expired": 410,
    "revision_conflict": 409,
    "idempotency_conflict": 409,
    "not_ready": 409,
    "resource_limit": 429,
    "runtime_unavailable": 503,
    "preview_unavailable": 503,
    "internal_error": 500,
}


class OutputProtocolError(ValueError):
    """Only closed reason codes may reach a public response or exception string."""

    def __init__(self, code: str = "invalid_request") -> None:
        self.code = code if type(code) is str and code in _ERROR_STATUSES else "internal_error"
        self.status = _ERROR_STATUSES[self.code]
        super().__init__(self.code)

    def to_wire(self) -> dict[str, str]:
        return {"schema": OUTPUT_ERROR_SCHEMA, "code": self.code}


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def require_output_workspace(value: object) -> str:
    if not _matches(_WORKSPACE, value):
        raise OutputProtocolError()
    return cast(str, value)


def require_output_handle(value: object, *, kind: Literal["job", "output"]) -> str:
    if type(kind) is not str or kind not in _HANDLES or not _matches(_HANDLES[kind], value):
        raise OutputProtocolError()
    return cast(str, value)


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthoringOutputCreate:
    schema: str
    workspace_handle: str
    workspace_revision: int
    timeline_revision: int
    snapshot_fingerprint: str
    output_profile_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        require_output_workspace(self.workspace_handle)
        if (
            type(self.schema) is not str
            or self.schema != OUTPUT_CREATE_SCHEMA
            or type(self.output_profile_id) is not str
            or self.output_profile_id != OUTPUT_PROFILE_ID
            or not _matches(_FINGERPRINT, self.snapshot_fingerprint)
            or not _matches(_IDEMPOTENCY, self.idempotency_key)
            or any(
                type(value) is not int or not 0 <= value <= 1_000_000
                for value in (self.workspace_revision, self.timeline_revision)
            )
        ):
            raise OutputProtocolError()

    def to_wire(self) -> dict[str, object]:
        return asdict(self)


def decode_output_create(value: object) -> AuthoringOutputCreate:
    if type(value) is not dict:
        raise OutputProtocolError()
    wire = cast(dict[str, object], value)
    if set(wire) != {field.name for field in fields(AuthoringOutputCreate)}:
        raise OutputProtocolError()
    return AuthoringOutputCreate(
        schema=cast(str, wire["schema"]),
        workspace_handle=cast(str, wire["workspace_handle"]),
        workspace_revision=cast(int, wire["workspace_revision"]),
        timeline_revision=cast(int, wire["timeline_revision"]),
        snapshot_fingerprint=cast(str, wire["snapshot_fingerprint"]),
        output_profile_id=cast(str, wire["output_profile_id"]),
        idempotency_key=cast(str, wire["idempotency_key"]),
    )


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise OutputProtocolError()
        result[key] = value
    return result


def _nonfinite(_value: str) -> NoReturn:
    raise OutputProtocolError()


def _object_json(body: bytes) -> dict[str, object]:
    if type(body) is not bytes or not 1 <= len(body) <= OUTPUT_REQUEST_BYTES:
        raise OutputProtocolError()
    try:
        value = json.loads(
            body.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_nonfinite
        )
    except (ValueError, UnicodeError, RecursionError):
        raise OutputProtocolError() from None
    if type(value) is not dict:
        raise OutputProtocolError()
    return cast(dict[str, object], value)


def decode_output_create_json(body: bytes) -> AuthoringOutputCreate:
    return decode_output_create(_object_json(body))


def decode_output_cancel_json(body: bytes) -> str:
    wire = _object_json(body)
    if set(wire) != {"schema", "workspace_handle"} or wire["schema"] != OUTPUT_CANCEL_SCHEMA:
        raise OutputProtocolError()
    return require_output_workspace(wire["workspace_handle"])


@dataclass(frozen=True, slots=True)
class OutputByteRange:
    status: int
    start: int
    stop: int
    complete_length: int

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not int
                for value in (self.status, self.start, self.stop, self.complete_length)
            )
            or not 1 <= self.complete_length <= OUTPUT_MAX_BYTES
            or self.status not in {200, 206, 416}
            or (self.status == 416 and (self.start, self.stop) != (0, 0))
            or (self.status != 416 and not 0 <= self.start < self.stop <= self.complete_length)
            or (self.status == 200 and (self.start, self.stop) != (0, self.complete_length))
        ):
            raise OutputProtocolError()

    @property
    def byte_length(self) -> int:
        return self.stop - self.start


def select_output_range(header: object, complete_length: int) -> OutputByteRange:
    """Select one interval only after the caller has verified the complete private body."""
    if type(complete_length) is not int or not 1 <= complete_length <= OUTPUT_MAX_BYTES:
        raise OutputProtocolError()
    if header is None:
        return OutputByteRange(200, 0, complete_length, complete_length)
    if type(header) is not str or len(header) > 128 or not header.isascii():
        raise OutputProtocolError()
    match = _RANGE.fullmatch(header)
    if match is None or not any(match.groups()):
        raise OutputProtocolError()
    first, last = match.groups()
    if any(int(value) > (1 << 63) - 1 for value in (first, last) if value):
        raise OutputProtocolError()
    if not first:
        suffix = int(last)
        if suffix == 0:
            return OutputByteRange(416, 0, 0, complete_length)
        return OutputByteRange(
            206, max(0, complete_length - suffix), complete_length, complete_length
        )
    start = int(first)
    if last and int(last) < start:
        raise OutputProtocolError()
    if start >= complete_length:
        return OutputByteRange(416, 0, 0, complete_length)
    stop = min(int(last) + 1, complete_length) if last else complete_length
    return OutputByteRange(206, start, stop, complete_length)


def output_media_headers(selection: OutputByteRange, *, preview: bool) -> dict[str, str]:
    if type(selection) is not OutputByteRange or type(preview) is not bool:
        raise OutputProtocolError()
    # IMPORTANT: filenames and media metadata are fixed output policy, never source locators.
    # Interpolating a source name here would expose private paths and enable header injection.
    headers = {
        "Content-Type": "video/mp4",
        "Content-Length": str(selection.byte_length),
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Content-Disposition": (
            'inline; filename="authoring-preview.mp4"'
            if preview
            else 'attachment; filename="authoring-final.mp4"'
        ),
    }
    if selection.status == 206:
        headers["Content-Range"] = (
            f"bytes {selection.start}-{selection.stop - 1}/{selection.complete_length}"
        )
    elif selection.status == 416:
        headers["Content-Range"] = f"bytes */{selection.complete_length}"
    return headers
