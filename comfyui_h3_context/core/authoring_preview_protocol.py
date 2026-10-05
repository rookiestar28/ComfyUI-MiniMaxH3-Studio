"""Closed Authoring source-preview identities and exact frame mapping.

The contract is intentionally content-free. It selects an accepted Authoring clip and current
workspace revisions, never a path, URL, byte range, media object, or transport implementation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction

from .timeline_authoring import (
    MAX_EXTENT_FRAMES_CEILING,
    MAX_REVISION,
    TimelineClip,
    TimelineProfileInput,
)

AUTHORING_PREVIEW_REQUEST_SCHEMA = "h3.context.authoring_source_preview.request.v1"
AUTHORING_PREVIEW_CAPABILITY_SCHEMA = "h3.context.authoring_source_preview.capability.v1"
AUTHORING_PREVIEW_SUCCESS_SCHEMA = "h3.context.authoring_source_preview.success.v1"
AUTHORING_PREVIEW_ERROR_SCHEMA = "h3.context.authoring_source_preview.error.v1"

MAX_AUTHORING_PREVIEW_REQUEST_BYTES = 8_192
MAX_AUTHORING_PREVIEW_REQUEST_DEPTH = 2
MAX_AUTHORING_PREVIEW_REQUEST_NODES = 32

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REQUEST_KEYS = frozenset(
    {
        "schema",
        "requestId",
        "workspaceHandle",
        "referenceRevision",
        "timelineRevision",
        "timelineContentFingerprint",
        "clipId",
    }
)
_SUCCESS_KEYS = _REQUEST_KEYS | {"sourceId"}
_CAPABILITY_KEYS = frozenset({"schema", "available", "reason"})
_ERROR_KEYS = frozenset({"schema", "requestId", "reason"})


class AuthoringPreviewCapabilityReason(str, Enum):
    NOT_BOUND = "not_bound"
    STALE = "stale"
    EXPIRED = "expired"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"


class AuthoringPreviewErrorReason(str, Enum):
    INVALID_REQUEST = "invalid_request"
    AUTHORITY_MISMATCH = "authority_mismatch"
    STALE = "stale"
    UNSUPPORTED = "unsupported"
    SOURCE_TOO_LARGE = "source_too_large"
    SOURCE_TOO_LONG = "source_too_long"
    BUSY = "busy"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    CONVERSION_FAILED = "conversion_failed"
    INTERNAL_FAILURE = "internal_failure"


def _closed(value: object, keys: frozenset[str], name: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise ValueError(f"{name} must be closed")
    return value


def _identifier(value: object, name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _fingerprint(value: object, name: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _revision(value: object, name: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_REVISION:
        raise ValueError(f"{name} is invalid")
    return value


def _enum_value(enum_type: type[Enum], value: object, name: str) -> Enum:
    if type(value) is not str:
        raise ValueError(f"{name} is invalid")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ValueError(f"{name} is invalid") from exc


@dataclass(frozen=True, slots=True)
class AuthoringPreviewRequest:
    request_id: str
    workspace_handle: str
    reference_revision: int
    timeline_revision: int
    timeline_content_fingerprint: str
    clip_id: str

    def __post_init__(self) -> None:
        _identifier(self.request_id, "requestId")
        _identifier(self.workspace_handle, "workspaceHandle")
        _revision(self.reference_revision, "referenceRevision")
        _revision(self.timeline_revision, "timelineRevision")
        _fingerprint(self.timeline_content_fingerprint, "timelineContentFingerprint")
        _identifier(self.clip_id, "clipId")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": AUTHORING_PREVIEW_REQUEST_SCHEMA,
            "requestId": self.request_id,
            "workspaceHandle": self.workspace_handle,
            "referenceRevision": self.reference_revision,
            "timelineRevision": self.timeline_revision,
            "timelineContentFingerprint": self.timeline_content_fingerprint,
            "clipId": self.clip_id,
        }


@dataclass(frozen=True, slots=True)
class AuthoringPreviewCapability:
    available: bool
    reason: AuthoringPreviewCapabilityReason | None = None

    def __post_init__(self) -> None:
        if type(self.available) is not bool:
            raise ValueError("available is invalid")
        if self.available:
            if self.reason is not None:
                raise ValueError("available capability reason must be null")
        elif not isinstance(self.reason, AuthoringPreviewCapabilityReason):
            raise ValueError("unavailable capability reason is required")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": AUTHORING_PREVIEW_CAPABILITY_SCHEMA,
            "available": self.available,
            "reason": None if self.reason is None else self.reason.value,
        }


@dataclass(frozen=True, slots=True)
class AuthoringPreviewSuccess:
    request_id: str
    workspace_handle: str
    reference_revision: int
    timeline_revision: int
    timeline_content_fingerprint: str
    clip_id: str
    source_id: str

    def __post_init__(self) -> None:
        _identifier(self.request_id, "requestId")
        _identifier(self.workspace_handle, "workspaceHandle")
        _revision(self.reference_revision, "referenceRevision")
        _revision(self.timeline_revision, "timelineRevision")
        _fingerprint(self.timeline_content_fingerprint, "timelineContentFingerprint")
        _identifier(self.clip_id, "clipId")
        _identifier(self.source_id, "sourceId")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": AUTHORING_PREVIEW_SUCCESS_SCHEMA,
            "requestId": self.request_id,
            "workspaceHandle": self.workspace_handle,
            "referenceRevision": self.reference_revision,
            "timelineRevision": self.timeline_revision,
            "timelineContentFingerprint": self.timeline_content_fingerprint,
            "clipId": self.clip_id,
            "sourceId": self.source_id,
        }


@dataclass(frozen=True, slots=True)
class AuthoringPreviewError:
    request_id: str | None
    reason: AuthoringPreviewErrorReason

    def __post_init__(self) -> None:
        if self.request_id is not None:
            _identifier(self.request_id, "requestId")
        if not isinstance(self.reason, AuthoringPreviewErrorReason):
            raise ValueError("reason is invalid")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": AUTHORING_PREVIEW_ERROR_SCHEMA,
            "requestId": self.request_id,
            "reason": self.reason.value,
        }


@dataclass(frozen=True, slots=True)
class AuthoringSourcePosition:
    timeline_frame: int
    clip_local_frame: int
    source_frame: int
    source_seconds: Fraction


def decode_authoring_preview_request(value: object) -> AuthoringPreviewRequest:
    wire = _closed(value, _REQUEST_KEYS, "authoring preview request")
    if wire["schema"] != AUTHORING_PREVIEW_REQUEST_SCHEMA:
        raise ValueError("authoring preview request schema is unsupported")
    return AuthoringPreviewRequest(
        request_id=_identifier(wire["requestId"], "requestId"),
        workspace_handle=_identifier(wire["workspaceHandle"], "workspaceHandle"),
        reference_revision=_revision(wire["referenceRevision"], "referenceRevision"),
        timeline_revision=_revision(wire["timelineRevision"], "timelineRevision"),
        timeline_content_fingerprint=_fingerprint(
            wire["timelineContentFingerprint"], "timelineContentFingerprint"
        ),
        clip_id=_identifier(wire["clipId"], "clipId"),
    )


def decode_authoring_preview_capability(value: object) -> AuthoringPreviewCapability:
    wire = _closed(value, _CAPABILITY_KEYS, "authoring preview capability")
    if wire["schema"] != AUTHORING_PREVIEW_CAPABILITY_SCHEMA:
        raise ValueError("authoring preview capability schema is unsupported")
    if type(wire["available"]) is not bool:
        raise ValueError("available is invalid")
    reason = (
        None
        if wire["reason"] is None
        else _enum_value(AuthoringPreviewCapabilityReason, wire["reason"], "reason")
    )
    return AuthoringPreviewCapability(
        available=wire["available"],
        reason=reason if isinstance(reason, AuthoringPreviewCapabilityReason) else None,
    )


def decode_authoring_preview_success(value: object) -> AuthoringPreviewSuccess:
    wire = _closed(value, _SUCCESS_KEYS, "authoring preview success")
    if wire["schema"] != AUTHORING_PREVIEW_SUCCESS_SCHEMA:
        raise ValueError("authoring preview success schema is unsupported")
    return AuthoringPreviewSuccess(
        request_id=_identifier(wire["requestId"], "requestId"),
        workspace_handle=_identifier(wire["workspaceHandle"], "workspaceHandle"),
        reference_revision=_revision(wire["referenceRevision"], "referenceRevision"),
        timeline_revision=_revision(wire["timelineRevision"], "timelineRevision"),
        timeline_content_fingerprint=_fingerprint(
            wire["timelineContentFingerprint"], "timelineContentFingerprint"
        ),
        clip_id=_identifier(wire["clipId"], "clipId"),
        source_id=_identifier(wire["sourceId"], "sourceId"),
    )


def decode_authoring_preview_error(value: object) -> AuthoringPreviewError:
    wire = _closed(value, _ERROR_KEYS, "authoring preview error")
    if wire["schema"] != AUTHORING_PREVIEW_ERROR_SCHEMA:
        raise ValueError("authoring preview error schema is unsupported")
    request_id = wire["requestId"]
    if request_id is not None:
        request_id = _identifier(request_id, "requestId")
    reason = _enum_value(AuthoringPreviewErrorReason, wire["reason"], "reason")
    if not isinstance(reason, AuthoringPreviewErrorReason):  # pragma: no cover - enum is closed
        raise ValueError("reason is invalid")
    return AuthoringPreviewError(request_id=request_id, reason=reason)


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ValueError("authoring preview request repeats an object key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ValueError("authoring preview request contains a non-finite constant")


def _shape(value: object, *, depth: int = 0) -> int:
    if depth > MAX_AUTHORING_PREVIEW_REQUEST_DEPTH:
        raise ValueError("authoring preview request exceeds the depth bound")
    if type(value) is dict:
        return 1 + sum(_shape(item, depth=depth + 1) for item in value.values())
    if type(value) is list:
        raise ValueError("authoring preview request arrays are unsupported")
    if type(value) in (str, int, float, bool) or value is None:
        return 1
    raise ValueError("authoring preview request value type is unsupported")


def decode_authoring_preview_request_json(data: bytes) -> AuthoringPreviewRequest:
    if type(data) is not bytes or not data or len(data) > MAX_AUTHORING_PREVIEW_REQUEST_BYTES:
        raise ValueError("authoring preview request bytes are invalid")
    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("authoring preview request JSON is invalid") from exc
    if _shape(value) > MAX_AUTHORING_PREVIEW_REQUEST_NODES:
        raise ValueError("authoring preview request exceeds the node bound")
    return decode_authoring_preview_request(value)


def _mapping_inputs(
    clip: TimelineClip, profile: TimelineProfileInput
) -> tuple[TimelineClip, TimelineProfileInput]:
    if type(clip) is not TimelineClip or type(profile) is not TimelineProfileInput:
        raise TypeError("authoring source mapping requires accepted timeline values")
    return clip, profile


def map_timeline_frame_to_source(
    clip: TimelineClip,
    profile: TimelineProfileInput,
    timeline_frame: int,
) -> AuthoringSourcePosition:
    clip, profile = _mapping_inputs(clip, profile)
    if type(timeline_frame) is not int or not 0 <= timeline_frame <= MAX_EXTENT_FRAMES_CEILING:
        raise ValueError("timeline frame is invalid")
    if not clip.start_frame <= timeline_frame < clip.end_frame:
        raise ValueError("timeline frame is outside the half-open clip interval")
    local = timeline_frame - clip.start_frame
    source_frame = clip.source_start_frame + local
    return AuthoringSourcePosition(
        timeline_frame=timeline_frame,
        clip_local_frame=local,
        source_frame=source_frame,
        source_seconds=Fraction(source_frame, profile.video_fps),
    )


def _exact_seconds(value: Fraction | Decimal | int) -> Fraction:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("source seconds must be an exact rational or finite decimal")
    if isinstance(value, Fraction):
        seconds = value
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("source seconds must be finite")
        seconds = Fraction(value)
    elif isinstance(value, int):
        seconds = Fraction(value)
    else:
        raise TypeError("source seconds must be an exact rational or finite decimal")
    if seconds < 0:
        raise ValueError("source seconds must be non-negative")
    return seconds


def map_source_seconds_to_timeline(
    clip: TimelineClip,
    profile: TimelineProfileInput,
    source_seconds: Fraction | Decimal | int,
) -> AuthoringSourcePosition:
    clip, profile = _mapping_inputs(clip, profile)
    exact_frames = _exact_seconds(source_seconds) * profile.video_fps
    # IMPORTANT: round exactly once at the media-time boundary. Float or repeated rounding makes
    # half-frame observations drift between adjacent clips and breaks deterministic scrubbing.
    source_frame = (2 * exact_frames.numerator + exact_frames.denominator) // (
        2 * exact_frames.denominator
    )
    first_source = clip.source_start_frame
    last_source = first_source + clip.frames - 1
    source_frame = min(max(source_frame, first_source), last_source)
    local = source_frame - first_source
    timeline_frame = clip.start_frame + local
    return AuthoringSourcePosition(
        timeline_frame=timeline_frame,
        clip_local_frame=local,
        source_frame=source_frame,
        source_seconds=Fraction(source_frame, profile.video_fps),
    )


__all__ = [
    "AUTHORING_PREVIEW_CAPABILITY_SCHEMA",
    "AUTHORING_PREVIEW_ERROR_SCHEMA",
    "AUTHORING_PREVIEW_REQUEST_SCHEMA",
    "AUTHORING_PREVIEW_SUCCESS_SCHEMA",
    "MAX_AUTHORING_PREVIEW_REQUEST_BYTES",
    "MAX_AUTHORING_PREVIEW_REQUEST_DEPTH",
    "MAX_AUTHORING_PREVIEW_REQUEST_NODES",
    "AuthoringPreviewCapability",
    "AuthoringPreviewCapabilityReason",
    "AuthoringPreviewError",
    "AuthoringPreviewErrorReason",
    "AuthoringPreviewRequest",
    "AuthoringPreviewSuccess",
    "AuthoringSourcePosition",
    "decode_authoring_preview_capability",
    "decode_authoring_preview_error",
    "decode_authoring_preview_request",
    "decode_authoring_preview_request_json",
    "decode_authoring_preview_success",
    "map_source_seconds_to_timeline",
    "map_timeline_frame_to_source",
]
