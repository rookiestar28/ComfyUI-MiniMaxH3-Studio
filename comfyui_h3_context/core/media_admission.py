"""Fail-closed media admission, probing, timestamp, and preprocessing contracts.

The values in this module are portable descriptors only. They never open a path, resolve DNS,
decode bytes, import a codec, or retain a media payload. Runtime adapters may use the descriptors
to own those operations, but only bounded fingerprints and source timestamps cross back into the
pure core.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from enum import Enum
from typing import cast

from .canonical import canonical_fingerprint
from .contracts import MediaKind
from .errors import MediaAdmissionError
from .security import (
    MediaSource,
    MediaSourceKind,
    MediaTransferPolicy,
    ValidatedMediaSource,
    validate_media_sources,
)

MEDIA_ADMISSION_SCHEMA = "h3.media.admission.v1"
MEDIA_PREPROCESS_SCHEMA = "h3.media.preprocess.v1"
MAX_MEDIA_DIAGNOSTICS = 32
MAX_MEDIA_OPERATIONS = 32
MAX_MEDIA_STREAMS = 16
MAX_MEDIA_JSON_BYTES = 2_000_000
MAX_MEDIA_STRING_LENGTH = 256
MAX_MEDIA_FRAMES = 10_000_000
MAX_MEDIA_ARTIFACTS = 4096

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODEC_PATTERN = re.compile(r"[a-z0-9][a-z0-9._+-]{0,63}\Z")
_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REVISION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SENSITIVE_MARKERS = (
    "api_key",
    "authorization",
    "bearer ",
    "cookie",
    "credential",
    "password",
    "private",
    "secret",
    "signed",
    "token",
    "url",
    "path",
    "\\",
    "/",
)


class MediaAdmissionStatus(str, Enum):
    """Admission status; only ADMITTED can proceed to a decoder."""

    ADMITTED = "admitted"
    REJECTED = "rejected"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProbeStatus(str, Enum):
    """Probe completion state; partial metadata never becomes a complete input."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise MediaAdmissionError(f"{field_name} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise MediaAdmissionError(f"{field_name} contains sensitive material")
    return value


def _code(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _CODEC_PATTERN.fullmatch(value.casefold()) is None:
        raise MediaAdmissionError(f"{field_name} must be a bounded media code")
    return value.casefold()


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise MediaAdmissionError(f"{field_name} must be a lowercase SHA-256 fingerprint")
    return value


def _text(value: object, field_name: str, maximum: int = MAX_MEDIA_STRING_LENGTH) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise MediaAdmissionError(f"{field_name} must be a bounded non-empty string")
    if any(ord(char) == 0 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise MediaAdmissionError(f"{field_name} contains an unsafe code point")
    return value


def _positive_int(value: object, field_name: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MediaAdmissionError(f"{field_name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise MediaAdmissionError(f"{field_name} exceeds its declared limit")
    return value


def _non_negative_int(value: object, field_name: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MediaAdmissionError(f"{field_name} must be a non-negative integer")
    if maximum is not None and value > maximum:
        raise MediaAdmissionError(f"{field_name} exceeds its declared limit")
    return value


def _decimal(value: object, field_name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise MediaAdmissionError(f"{field_name} must be a finite Decimal")
    if positive and value <= 0:
        raise MediaAdmissionError(f"{field_name} must be positive")
    if not positive and value < 0:
        raise MediaAdmissionError(f"{field_name} must be non-negative")
    return value


def _bounded_string_tuple(
    values: object, field_name: str, *, maximum: int, code: bool = False
) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not values or len(values) > maximum:
        raise MediaAdmissionError(f"{field_name} must be a non-empty bounded tuple")
    result = tuple(
        _code(value, f"{field_name} item") if code else _text(value, f"{field_name} item", 64)
        for value in values
    )
    if len(result) != len(set(result)):
        raise MediaAdmissionError(f"{field_name} must not contain duplicates")
    return result


def _json_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MediaAdmissionError(f"{field_name} must be an object")
    if len(value) > MAX_MEDIA_STREAMS * 16:
        raise MediaAdmissionError(f"{field_name} exceeds its bounded field limit")
    for key in value:
        if not isinstance(key, str) or len(key) > 64:
            raise MediaAdmissionError(f"{field_name} contains an invalid field")
    return cast(Mapping[str, object], value)


@dataclass(frozen=True, slots=True)
class MediaLimits:
    """Finite limits applied before probing and again before decoding."""

    max_bytes: int
    max_duration_seconds: Decimal
    max_width: int
    max_height: int
    max_frame_rate: Decimal
    max_frames: int
    max_sample_rate: int
    max_channels: int
    max_references: int
    max_temp_bytes: int
    max_decoded_bytes: int
    max_probe_stdout_bytes: int
    max_probe_stderr_bytes: int
    max_wall_time_seconds: Decimal
    max_redirects: int = 0

    def __post_init__(self) -> None:
        for field_name in (
            "max_bytes",
            "max_width",
            "max_height",
            "max_frames",
            "max_sample_rate",
            "max_channels",
            "max_references",
            "max_temp_bytes",
            "max_decoded_bytes",
            "max_probe_stdout_bytes",
            "max_probe_stderr_bytes",
        ):
            _positive_int(getattr(self, field_name), field_name)
        for field_name in (
            "max_duration_seconds",
            "max_frame_rate",
            "max_wall_time_seconds",
        ):
            _decimal(getattr(self, field_name), field_name, positive=True)
        _non_negative_int(self.max_redirects, "max_redirects")
        if self.max_redirects > 5:
            raise MediaAdmissionError("max_redirects exceeds the finite redirect limit")

    def to_wire(self) -> dict[str, object]:
        return {
            "max_bytes": self.max_bytes,
            "max_duration_seconds": format(self.max_duration_seconds, "f"),
            "max_width": self.max_width,
            "max_height": self.max_height,
            "max_frame_rate": format(self.max_frame_rate, "f"),
            "max_frames": self.max_frames,
            "max_sample_rate": self.max_sample_rate,
            "max_channels": self.max_channels,
            "max_references": self.max_references,
            "max_temp_bytes": self.max_temp_bytes,
            "max_decoded_bytes": self.max_decoded_bytes,
            "max_probe_stdout_bytes": self.max_probe_stdout_bytes,
            "max_probe_stderr_bytes": self.max_probe_stderr_bytes,
            "max_wall_time_seconds": format(self.max_wall_time_seconds, "f"),
            "max_redirects": self.max_redirects,
        }


@dataclass(frozen=True, slots=True)
class MediaAllowlist:
    """Closed protocol/container/codec names selected by the caller."""

    protocols: tuple[str, ...]
    containers: tuple[str, ...]
    video_codecs: tuple[str, ...]
    audio_codecs: tuple[str, ...]
    image_codecs: tuple[str, ...] = ("png", "jpeg", "webp")

    def __post_init__(self) -> None:
        protocols = _bounded_string_tuple(self.protocols, "protocols", maximum=8, code=True)
        containers = _bounded_string_tuple(self.containers, "containers", maximum=64, code=True)
        video = _bounded_string_tuple(self.video_codecs, "video_codecs", maximum=64, code=True)
        audio = _bounded_string_tuple(self.audio_codecs, "audio_codecs", maximum=64, code=True)
        image = _bounded_string_tuple(self.image_codecs, "image_codecs", maximum=64, code=True)
        if not set(protocols).issubset({"file", "https"}):
            raise MediaAdmissionError("protocol allowlist contains an unsupported scheme")
        object.__setattr__(self, "protocols", protocols)
        object.__setattr__(self, "containers", containers)
        object.__setattr__(self, "video_codecs", video)
        object.__setattr__(self, "audio_codecs", audio)
        object.__setattr__(self, "image_codecs", image)

    def to_wire(self) -> dict[str, object]:
        return {
            "protocols": list(self.protocols),
            "containers": list(self.containers),
            "video_codecs": list(self.video_codecs),
            "audio_codecs": list(self.audio_codecs),
            "image_codecs": list(self.image_codecs),
        }


@dataclass(frozen=True, slots=True)
class PreprocessingSpec:
    """Explicit deterministic preprocessing whose revision enters every artifact identity."""

    revision: str
    operations: tuple[str, ...]
    target_pixel_format: str | None = None
    target_audio_sample_rate: int | None = None
    target_audio_channels: int | None = None
    preserve_source_pts: bool = True
    schema: str = MEDIA_PREPROCESS_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.revision, str) or _REVISION_PATTERN.fullmatch(self.revision) is None:
            raise MediaAdmissionError("preprocessing revision must be a bounded identifier")
        if any(marker in self.revision.casefold() for marker in _SENSITIVE_MARKERS):
            raise MediaAdmissionError("preprocessing revision contains sensitive material")
        if not isinstance(self.operations, tuple) or len(self.operations) > MAX_MEDIA_OPERATIONS:
            raise MediaAdmissionError("preprocessing operations exceed the finite limit")
        operations = tuple(_code(item, "preprocessing operation") for item in self.operations)
        if len(operations) != len(set(operations)):
            raise MediaAdmissionError("preprocessing operations must not repeat")
        object.__setattr__(self, "operations", operations)
        if self.target_pixel_format is not None:
            _code(self.target_pixel_format, "target_pixel_format")
        if self.target_audio_sample_rate is not None:
            _positive_int(self.target_audio_sample_rate, "target_audio_sample_rate")
        if self.target_audio_channels is not None:
            _positive_int(self.target_audio_channels, "target_audio_channels")
        if not isinstance(self.preserve_source_pts, bool):
            raise MediaAdmissionError("preserve_source_pts must be a boolean")
        if self.schema != MEDIA_PREPROCESS_SCHEMA:
            raise MediaAdmissionError("unsupported preprocessing schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "revision": self.revision,
            "operations": list(self.operations),
            "target_pixel_format": self.target_pixel_format,
            "target_audio_sample_rate": self.target_audio_sample_rate,
            "target_audio_channels": self.target_audio_channels,
            "preserve_source_pts": self.preserve_source_pts,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """Hash-only source/open identity used to detect admission-to-open changes."""

    source_fingerprint: str
    size_bytes: int
    identity_fingerprint: str

    def __post_init__(self) -> None:
        _fingerprint(self.source_fingerprint, "source_fingerprint")
        _positive_int(self.size_bytes, "size_bytes")
        _fingerprint(self.identity_fingerprint, "identity_fingerprint")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "source_fingerprint": self.source_fingerprint,
            "size_bytes": self.size_bytes,
            "identity_fingerprint": self.identity_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class RedirectTrace:
    """A redacted redirect decision; URLs and hosts never enter the canonical value."""

    hop_fingerprints: tuple[str, ...] = ()
    final_source_fingerprint: str | None = None
    policy_match: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.hop_fingerprints, tuple) or len(self.hop_fingerprints) > 8:
            raise MediaAdmissionError("redirect trace exceeds the finite hop limit")
        values = tuple(
            _fingerprint(value, "redirect hop fingerprint") for value in self.hop_fingerprints
        )
        if len(values) != len(set(values)):
            raise MediaAdmissionError("redirect hop fingerprints must be unique")
        object.__setattr__(self, "hop_fingerprints", values)
        if self.final_source_fingerprint is not None:
            _fingerprint(self.final_source_fingerprint, "final source fingerprint")
        if not isinstance(self.policy_match, bool):
            raise MediaAdmissionError("redirect policy_match must be a boolean")

    @property
    def redirect_count(self) -> int:
        return len(self.hop_fingerprints)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "redirect_count": self.redirect_count,
            "final_source_fingerprint": self.final_source_fingerprint,
            "policy_match": self.policy_match,
        }


@dataclass(frozen=True, slots=True)
class MediaAdmissionRequest:
    """One explicit media source and all limits needed before a decoder may open it."""

    source: MediaSource = field(repr=False, compare=False)
    expected_kind: MediaKind
    transfer_policy: MediaTransferPolicy = field(repr=False, compare=False)
    limits: MediaLimits
    allowlist: MediaAllowlist
    preprocessing: PreprocessingSpec
    expected_identity: SourceIdentity | None = field(default=None, repr=False, compare=False)
    redirect_trace: RedirectTrace | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.source, MediaSource):
            raise MediaAdmissionError("source must be a MediaSource")
        if not isinstance(self.expected_kind, MediaKind):
            raise MediaAdmissionError("expected_kind must be a MediaKind")
        if self.source.media_kind is not self.expected_kind:
            raise MediaAdmissionError("source media kind does not match expected_kind")
        if not isinstance(self.transfer_policy, MediaTransferPolicy):
            raise MediaAdmissionError("transfer_policy must be a MediaTransferPolicy")
        if not isinstance(self.limits, MediaLimits):
            raise MediaAdmissionError("limits must be MediaLimits")
        if not isinstance(self.allowlist, MediaAllowlist):
            raise MediaAdmissionError("allowlist must be MediaAllowlist")
        if not isinstance(self.preprocessing, PreprocessingSpec):
            raise MediaAdmissionError("preprocessing must be PreprocessingSpec")
        if self.expected_identity is not None and not isinstance(
            self.expected_identity, SourceIdentity
        ):
            raise MediaAdmissionError("expected_identity must be SourceIdentity or None")
        if self.redirect_trace is not None and not isinstance(self.redirect_trace, RedirectTrace):
            raise MediaAdmissionError("redirect_trace must be RedirectTrace or None")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": MEDIA_ADMISSION_SCHEMA,
            "expected_kind": self.expected_kind.value,
            "limits": self.limits.to_wire(),
            "allowlist": self.allowlist.to_wire(),
            "preprocessing_fingerprint": self.preprocessing.fingerprint,
            "expected_identity": (
                None if self.expected_identity is None else self.expected_identity.to_public_dict()
            ),
            "redirects": (
                None if self.redirect_trace is None else self.redirect_trace.to_public_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class MediaAdmissionDecision:
    """Redacted admission result; the validated locator stays inside the runtime adapter."""

    status: MediaAdmissionStatus | str
    media_kind: MediaKind
    source_kind: MediaSourceKind
    preprocessing_fingerprint: str
    source_fingerprint: str | None = None
    diagnostics: tuple[str, ...] = ()
    schema: str = MEDIA_ADMISSION_SCHEMA

    def __post_init__(self) -> None:
        try:
            status = (
                self.status
                if isinstance(self.status, MediaAdmissionStatus)
                else MediaAdmissionStatus(self.status)
            )
        except (TypeError, ValueError):
            raise MediaAdmissionError("unsupported media admission status") from None
        object.__setattr__(self, "status", status)
        if not isinstance(self.media_kind, MediaKind):
            raise MediaAdmissionError("media_kind must be MediaKind")
        if not isinstance(self.source_kind, MediaSourceKind):
            raise MediaAdmissionError("source_kind must be MediaSourceKind")
        _fingerprint(self.preprocessing_fingerprint, "preprocessing_fingerprint")
        if self.source_fingerprint is not None:
            _fingerprint(self.source_fingerprint, "source_fingerprint")
        if not isinstance(self.diagnostics, tuple) or len(self.diagnostics) > MAX_MEDIA_DIAGNOSTICS:
            raise MediaAdmissionError("diagnostics exceed the finite limit")
        values = tuple(_code(item, "diagnostic code") for item in self.diagnostics)
        object.__setattr__(self, "diagnostics", values)
        if self.schema != MEDIA_ADMISSION_SCHEMA:
            raise MediaAdmissionError("unsupported media admission schema")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": cast(MediaAdmissionStatus, self.status).value,
            "media_kind": self.media_kind.value,
            "source_kind": self.source_kind.value,
            "preprocessing_fingerprint": self.preprocessing_fingerprint,
            "source_fingerprint": self.source_fingerprint,
            "diagnostics": list(self.diagnostics),
        }


def _source_protocol(source_kind: MediaSourceKind) -> str:
    return "file" if source_kind is MediaSourceKind.LOCAL_PATH else "https"


def admit_media_source(
    request: MediaAdmissionRequest,
    *,
    opened_identity: SourceIdentity | None = None,
) -> MediaAdmissionDecision:
    """Apply all pre-open admission and post-open identity checks without decoding."""

    if not isinstance(request, MediaAdmissionRequest):
        raise MediaAdmissionError("request must be a MediaAdmissionRequest")
    protocol = _source_protocol(request.source.source_kind)
    if protocol not in request.allowlist.protocols:
        raise MediaAdmissionError("media source protocol is not allowlisted")
    if request.source.media_type.startswith("video/"):
        codeces = request.allowlist.video_codecs
    elif request.source.media_type.startswith("audio/"):
        codeces = request.allowlist.audio_codecs
    else:
        codeces = request.allowlist.image_codecs
    if not codeces:
        raise MediaAdmissionError("media codec allowlist is empty for the source kind")
    if request.redirect_trace is not None:
        if request.redirect_trace.redirect_count > min(
            request.limits.max_redirects, request.transfer_policy.max_redirects
        ):
            raise MediaAdmissionError("media redirect count exceeds the declared limit")
        if not request.redirect_trace.policy_match:
            raise MediaAdmissionError("media redirect leaves the explicit URL policy")
    try:
        validated = validate_media_sources((request.source,), request.transfer_policy)
    except Exception as exc:
        if isinstance(exc, MediaAdmissionError):
            raise
        raise MediaAdmissionError("media source failed first-stage admission") from exc
    if len(validated) != 1 or not isinstance(validated[0], ValidatedMediaSource):
        raise MediaAdmissionError("media source admission returned an invalid result")
    if opened_identity is not None:
        if request.expected_identity is None:
            raise MediaAdmissionError("opened media identity requires an expected identity")
        if opened_identity != request.expected_identity:
            raise MediaAdmissionError("media identity changed between admission and open")
        if opened_identity.size_bytes != request.source.size_bytes:
            raise MediaAdmissionError("media size changed between admission and open")
    return MediaAdmissionDecision(
        status=MediaAdmissionStatus.ADMITTED,
        media_kind=request.expected_kind,
        source_kind=request.source.source_kind,
        preprocessing_fingerprint=request.preprocessing.fingerprint,
        source_fingerprint=(
            None if opened_identity is None else opened_identity.source_fingerprint
        ),
    )


def _mapping_value(mapping: Mapping[str, object], key: str, *, required: bool = True) -> object:
    value = mapping.get(key)
    if value is None and required:
        raise MediaAdmissionError("probe manifest is missing a required field")
    return value


def _parse_decimal(value: object, field_name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise MediaAdmissionError(f"probe {field_name} must be a decimal string")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise MediaAdmissionError(f"probe {field_name} is not a finite decimal") from exc
    return _decimal(result, f"probe {field_name}", positive=positive)


def _parse_optional_decimal(value: object, field_name: str) -> Decimal | None:
    if value in (None, "", "N/A"):
        return None
    return _parse_decimal(value, field_name)


def _parse_probe_int(value: object, field_name: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MediaAdmissionError(f"probe {field_name} must be an integer")
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise MediaAdmissionError(f"probe {field_name} must be an integer") from exc
    return _positive_int(result, f"probe {field_name}", maximum)


def _parse_ratio(value: object, field_name: str, *, allow_zero: bool = False) -> Decimal | None:
    if value in (None, "", "0/0", "N/A"):
        if allow_zero:
            return Decimal(0)
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        numerator, denominator = value, 1
    elif isinstance(value, str) and "/" in value:
        parts = value.split("/")
        if len(parts) != 2:
            raise MediaAdmissionError(f"probe {field_name} ratio is malformed")
        try:
            numerator, denominator = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise MediaAdmissionError(f"probe {field_name} ratio is malformed") from exc
    else:
        return _parse_decimal(value, field_name, positive=True)
    if denominator <= 0 or numerator < 0 or (numerator == 0 and not allow_zero):
        raise MediaAdmissionError(f"probe {field_name} ratio is outside the finite range")
    with localcontext() as context:
        context.prec = 28
        result = Decimal(numerator) / Decimal(denominator)
    return _decimal(result, field_name, positive=not allow_zero)


def _parse_time_base(value: object, field_name: str = "time_base") -> tuple[int, int]:
    if not isinstance(value, str) or "/" not in value:
        raise MediaAdmissionError(f"probe {field_name} must be numerator/denominator")
    parts = value.split("/")
    if len(parts) != 2:
        raise MediaAdmissionError(f"probe {field_name} is malformed")
    try:
        numerator, denominator = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise MediaAdmissionError(f"probe {field_name} is malformed") from exc
    _positive_int(numerator, f"probe {field_name} numerator")
    _positive_int(denominator, f"probe {field_name} denominator")
    return numerator, denominator


def _stream_id(value: object, fallback: int) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return _identifier(f"stream.{value}", "stream id")
    if isinstance(value, str) and value:
        return _identifier(f"stream.{fallback}", "stream id")
    return _identifier(f"stream.{fallback}", "stream id")


@dataclass(frozen=True, slots=True)
class VideoStreamProbe:
    """Bounded video/image stream metadata returned by an injected probe."""

    stream_id: str
    codec: str
    width: int
    height: int
    time_base: tuple[int, int]
    frame_count: int | None = None
    duration_seconds: Decimal | None = None
    average_frame_rate: Decimal | None = None
    variable_frame_rate: bool = False

    def __post_init__(self) -> None:
        _identifier(self.stream_id, "video stream_id")
        _code(self.codec, "video codec")
        _positive_int(self.width, "video width")
        _positive_int(self.height, "video height")
        if (
            not isinstance(self.time_base, tuple)
            or len(self.time_base) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in self.time_base
            )
        ):
            raise MediaAdmissionError("video time_base must contain two positive integers")
        if self.frame_count is not None:
            _positive_int(self.frame_count, "video frame_count", MAX_MEDIA_FRAMES)
        if self.duration_seconds is not None:
            _decimal(self.duration_seconds, "video duration_seconds", positive=True)
        if self.average_frame_rate is not None:
            _decimal(self.average_frame_rate, "video average_frame_rate", positive=True)
        if not isinstance(self.variable_frame_rate, bool):
            raise MediaAdmissionError("variable_frame_rate must be a boolean")

    def to_wire(self) -> dict[str, object]:
        return {
            "stream_id": self.stream_id,
            "codec": self.codec,
            "width": self.width,
            "height": self.height,
            "time_base": list(self.time_base),
            "frame_count": self.frame_count,
            "duration_seconds": (
                None if self.duration_seconds is None else format(self.duration_seconds, "f")
            ),
            "average_frame_rate": (
                None if self.average_frame_rate is None else format(self.average_frame_rate, "f")
            ),
            "variable_frame_rate": self.variable_frame_rate,
        }


@dataclass(frozen=True, slots=True)
class AudioStreamProbe:
    """Bounded audio stream metadata returned by an injected probe."""

    stream_id: str
    codec: str
    sample_rate: int
    channels: int
    time_base: tuple[int, int]
    duration_seconds: Decimal | None = None
    sample_count: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.stream_id, "audio stream_id")
        _code(self.codec, "audio codec")
        _positive_int(self.sample_rate, "audio sample_rate")
        _positive_int(self.channels, "audio channels")
        if (
            not isinstance(self.time_base, tuple)
            or len(self.time_base) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in self.time_base
            )
        ):
            raise MediaAdmissionError("audio time_base must contain two positive integers")
        if self.duration_seconds is not None:
            _decimal(self.duration_seconds, "audio duration_seconds", positive=True)
        if self.sample_count is not None:
            _positive_int(self.sample_count, "audio sample_count", MAX_MEDIA_FRAMES * 100_000)

    def to_wire(self) -> dict[str, object]:
        return {
            "stream_id": self.stream_id,
            "codec": self.codec,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "time_base": list(self.time_base),
            "duration_seconds": (
                None if self.duration_seconds is None else format(self.duration_seconds, "f")
            ),
            "sample_count": self.sample_count,
        }


@dataclass(frozen=True, slots=True)
class ProbeManifest:
    """Validated, bounded ffprobe-style stream/container metadata."""

    source_identity: SourceIdentity
    media_kind: MediaKind
    container: str
    video: tuple[VideoStreamProbe, ...] = ()
    audio: tuple[AudioStreamProbe, ...] = ()
    duration_seconds: Decimal | None = None
    status: ProbeStatus | str = ProbeStatus.COMPLETE
    schema: str = MEDIA_ADMISSION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.source_identity, SourceIdentity):
            raise MediaAdmissionError("probe source_identity must be SourceIdentity")
        if not isinstance(self.media_kind, MediaKind):
            raise MediaAdmissionError("probe media_kind must be MediaKind")
        _code(self.container, "container")
        if not isinstance(self.video, tuple) or not isinstance(self.audio, tuple):
            raise MediaAdmissionError("probe streams must be tuples")
        if len(self.video) + len(self.audio) > MAX_MEDIA_STREAMS:
            raise MediaAdmissionError("probe stream count exceeds the finite limit")
        if not all(isinstance(item, VideoStreamProbe) for item in self.video):
            raise MediaAdmissionError("video must contain VideoStreamProbe values")
        if not all(isinstance(item, AudioStreamProbe) for item in self.audio):
            raise MediaAdmissionError("audio must contain AudioStreamProbe values")
        if self.media_kind in {MediaKind.IMAGE, MediaKind.VIDEO} and not self.video:
            raise MediaAdmissionError("video/image probe requires a video stream")
        if self.media_kind is MediaKind.AUDIO and not self.audio:
            raise MediaAdmissionError("audio probe requires an audio stream")
        if self.duration_seconds is not None:
            _decimal(self.duration_seconds, "probe duration_seconds", positive=True)
        try:
            status = (
                self.status if isinstance(self.status, ProbeStatus) else ProbeStatus(self.status)
            )
        except (TypeError, ValueError):
            raise MediaAdmissionError("unsupported probe status") from None
        object.__setattr__(self, "status", status)
        if self.schema != MEDIA_ADMISSION_SCHEMA:
            raise MediaAdmissionError("unsupported probe schema")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_public_dict())

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": cast(ProbeStatus, self.status).value,
            "source_identity": self.source_identity.to_public_dict(),
            "media_kind": self.media_kind.value,
            "container": self.container,
            "duration_seconds": (
                None if self.duration_seconds is None else format(self.duration_seconds, "f")
            ),
            "video": [item.to_wire() for item in self.video],
            "audio": [item.to_wire() for item in self.audio],
        }

    @classmethod
    def from_json_bytes(
        cls,
        payload: bytes,
        request: MediaAdmissionRequest,
        source_identity: SourceIdentity,
    ) -> ProbeManifest:
        if not isinstance(payload, bytes) or len(payload) > min(
            request.limits.max_probe_stdout_bytes, MAX_MEDIA_JSON_BYTES
        ):
            raise MediaAdmissionError("probe output exceeds its bounded byte limit")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MediaAdmissionError("probe output is not valid bounded JSON") from exc
        return parse_ffprobe_manifest(value, request, source_identity)


def _allowed_container(value: str, allowlist: MediaAllowlist) -> str:
    candidates = tuple(item.strip().casefold() for item in value.split(",") if item.strip())
    for candidate in candidates:
        if candidate in allowlist.containers:
            return candidate
    raise MediaAdmissionError("probe container is not allowlisted")


def _duration_within(value: Decimal | None, limits: MediaLimits, field_name: str) -> None:
    if value is not None and value > limits.max_duration_seconds:
        raise MediaAdmissionError(f"{field_name} exceeds the declared duration limit")


def parse_ffprobe_manifest(
    payload: object,
    request: MediaAdmissionRequest,
    source_identity: SourceIdentity,
) -> ProbeManifest:
    """Parse only bounded, allowlisted ffprobe JSON fields; never retain raw tool output."""

    if not isinstance(request, MediaAdmissionRequest):
        raise MediaAdmissionError("request must be a MediaAdmissionRequest")
    if not isinstance(source_identity, SourceIdentity):
        raise MediaAdmissionError("source_identity must be SourceIdentity")
    root = _json_mapping(payload, "probe root")
    format_value = _json_mapping(_mapping_value(root, "format"), "probe format")
    streams_value = _mapping_value(root, "streams")
    if not isinstance(streams_value, Sequence) or isinstance(streams_value, (str, bytes)):
        raise MediaAdmissionError("probe streams must be a bounded array")
    if len(streams_value) == 0 or len(streams_value) > MAX_MEDIA_STREAMS:
        raise MediaAdmissionError("probe stream count is outside the finite range")
    format_name = _text(_mapping_value(format_value, "format_name"), "probe format_name", 512)
    container = _allowed_container(format_name, request.allowlist)
    duration = _parse_optional_decimal(format_value.get("duration"), "format.duration")
    _duration_within(duration, request.limits, "format.duration")

    videos: list[VideoStreamProbe] = []
    audios: list[AudioStreamProbe] = []
    for index, raw_stream in enumerate(streams_value):
        stream = _json_mapping(raw_stream, "probe stream")
        codec_type = _text(_mapping_value(stream, "codec_type"), "stream codec_type", 32).casefold()
        codec = _code(_mapping_value(stream, "codec_name"), "stream codec_name")
        stream_id = _stream_id(stream.get("index"), index)
        if codec_type == "video":
            codec_allowlist = (
                request.allowlist.image_codecs
                if request.expected_kind is MediaKind.IMAGE
                else request.allowlist.video_codecs
            )
            if codec not in codec_allowlist:
                raise MediaAdmissionError("probe video codec is not allowlisted")
            width = _positive_int(_mapping_value(stream, "width"), "stream width")
            height = _positive_int(_mapping_value(stream, "height"), "stream height")
            if width > request.limits.max_width or height > request.limits.max_height:
                raise MediaAdmissionError("probe dimensions exceed the declared limit")
            time_base = _parse_time_base(_mapping_value(stream, "time_base"), "stream.time_base")
            frame_count_value = stream.get("nb_frames")
            frame_count = (
                None
                if frame_count_value in (None, "", "N/A")
                else _parse_probe_int(
                    frame_count_value,
                    "stream frame_count",
                    maximum=request.limits.max_frames,
                )
            )
            stream_duration = _parse_optional_decimal(stream.get("duration"), "stream.duration")
            _duration_within(stream_duration, request.limits, "stream.duration")
            average_rate = _parse_ratio(
                stream.get("avg_frame_rate"), "stream.avg_frame_rate", allow_zero=True
            )
            if average_rate == 0:
                average_rate = _parse_ratio(
                    stream.get("r_frame_rate"), "stream.r_frame_rate", allow_zero=True
                )
            if average_rate is not None and average_rate > request.limits.max_frame_rate:
                raise MediaAdmissionError("probe frame rate exceeds the declared limit")
            variable_rate = (
                stream.get("r_frame_rate") not in (None, "", "0/0")
                and stream.get("avg_frame_rate") not in (None, "", "0/0")
                and str(stream.get("r_frame_rate")) != str(stream.get("avg_frame_rate"))
            )
            videos.append(
                VideoStreamProbe(
                    stream_id=stream_id,
                    codec=codec,
                    width=width,
                    height=height,
                    time_base=time_base,
                    frame_count=frame_count,
                    duration_seconds=stream_duration,
                    average_frame_rate=average_rate if average_rate != 0 else None,
                    variable_frame_rate=variable_rate,
                )
            )
        elif codec_type == "audio":
            if codec not in request.allowlist.audio_codecs:
                raise MediaAdmissionError("probe audio codec is not allowlisted")
            sample_rate = _parse_probe_int(
                _mapping_value(stream, "sample_rate"), "stream sample_rate"
            )
            channels = _parse_probe_int(_mapping_value(stream, "channels"), "stream channels")
            if (
                sample_rate > request.limits.max_sample_rate
                or channels > request.limits.max_channels
            ):
                raise MediaAdmissionError("probe audio rate/channels exceed the declared limit")
            time_base = _parse_time_base(_mapping_value(stream, "time_base"), "stream.time_base")
            stream_duration = _parse_optional_decimal(stream.get("duration"), "stream.duration")
            _duration_within(stream_duration, request.limits, "stream.duration")
            sample_count_value = stream.get("nb_samples")
            sample_count = (
                None
                if sample_count_value in (None, "", "N/A")
                else _parse_probe_int(sample_count_value, "stream sample_count")
            )
            audios.append(
                AudioStreamProbe(
                    stream_id=stream_id,
                    codec=codec,
                    sample_rate=sample_rate,
                    channels=channels,
                    time_base=time_base,
                    duration_seconds=stream_duration,
                    sample_count=sample_count,
                )
            )
        else:
            continue

    if request.expected_kind in {MediaKind.IMAGE, MediaKind.VIDEO} and not videos:
        raise MediaAdmissionError("probe contains no admitted video/image stream")
    if request.expected_kind is MediaKind.AUDIO and not audios:
        raise MediaAdmissionError("probe contains no admitted audio stream")
    video_durations = tuple(
        video_item.duration_seconds
        for video_item in videos
        if video_item.duration_seconds is not None
    )
    audio_durations = tuple(
        audio_item.duration_seconds
        for audio_item in audios
        if audio_item.duration_seconds is not None
    )
    stream_durations = (*video_durations, *audio_durations)
    if duration is None and stream_durations:
        duration = max(stream_durations)
    return ProbeManifest(
        source_identity=source_identity,
        media_kind=request.expected_kind,
        container=container,
        video=tuple(videos),
        audio=tuple(audios),
        duration_seconds=duration,
    )


@dataclass(frozen=True, slots=True)
class PresentationTimestamp:
    """An integer source PTS and explicit time base; no average-FPS conversion is implied."""

    ticks: int
    time_base_num: int
    time_base_den: int

    def __post_init__(self) -> None:
        _non_negative_int(self.ticks, "timestamp ticks")
        _positive_int(self.time_base_num, "timestamp time_base_num")
        _positive_int(self.time_base_den, "timestamp time_base_den")

    @property
    def seconds(self) -> Decimal:
        with localcontext() as context:
            context.prec = 40
            return Decimal(self.ticks * self.time_base_num) / Decimal(self.time_base_den)

    def to_wire(self) -> dict[str, object]:
        return {
            "ticks": self.ticks,
            "time_base": [self.time_base_num, self.time_base_den],
            "seconds": format(self.seconds, "f"),
        }


def _timestamp_at_or_after(left: PresentationTimestamp, right: PresentationTimestamp) -> bool:
    return (
        left.ticks * left.time_base_num * right.time_base_den
        >= right.ticks * right.time_base_num * left.time_base_den
    )


def _timestamp_span_valid(start: PresentationTimestamp, end: PresentationTimestamp) -> None:
    if (start.time_base_num, start.time_base_den) != (end.time_base_num, end.time_base_den):
        raise MediaAdmissionError("timestamp span endpoints must share a time base")
    if not _timestamp_at_or_after(end, start):
        raise MediaAdmissionError("timestamp span end precedes start")


@dataclass(frozen=True, slots=True)
class DecodedFrameArtifact:
    """One decoded frame descriptor; pixels remain in the owning runtime adapter."""

    frame_id: str
    source_fingerprint: str
    timestamp: PresentationTimestamp
    width: int
    height: int
    pixel_format: str
    payload_fingerprint: str
    preprocessing_fingerprint: str
    sequence: int
    payload_bytes: int = 1

    def __post_init__(self) -> None:
        _identifier(self.frame_id, "frame_id")
        _fingerprint(self.source_fingerprint, "frame source_fingerprint")
        if not isinstance(self.timestamp, PresentationTimestamp):
            raise MediaAdmissionError("frame timestamp must be PresentationTimestamp")
        _positive_int(self.width, "frame width")
        _positive_int(self.height, "frame height")
        _code(self.pixel_format, "frame pixel_format")
        _fingerprint(self.payload_fingerprint, "frame payload_fingerprint")
        _fingerprint(self.preprocessing_fingerprint, "frame preprocessing_fingerprint")
        _non_negative_int(self.sequence, "frame sequence")
        _positive_int(self.payload_bytes, "frame payload_bytes")

    def to_wire(self) -> dict[str, object]:
        return {
            "frame_id": self.frame_id,
            "source_fingerprint": self.source_fingerprint,
            "timestamp": self.timestamp.to_wire(),
            "width": self.width,
            "height": self.height,
            "pixel_format": self.pixel_format,
            "payload_fingerprint": self.payload_fingerprint,
            "preprocessing_fingerprint": self.preprocessing_fingerprint,
            "sequence": self.sequence,
            "payload_bytes": self.payload_bytes,
        }


@dataclass(frozen=True, slots=True)
class DecodedAudioSegment:
    """One source-aligned audio segment descriptor; samples remain runtime-owned."""

    segment_id: str
    source_fingerprint: str
    start: PresentationTimestamp
    end: PresentationTimestamp
    sample_rate: int
    channels: int
    payload_fingerprint: str
    preprocessing_fingerprint: str
    payload_bytes: int = 1

    def __post_init__(self) -> None:
        _identifier(self.segment_id, "audio segment_id")
        _fingerprint(self.source_fingerprint, "audio source_fingerprint")
        if not isinstance(self.start, PresentationTimestamp) or not isinstance(
            self.end, PresentationTimestamp
        ):
            raise MediaAdmissionError("audio segment timestamps must be PresentationTimestamp")
        _timestamp_span_valid(self.start, self.end)
        _positive_int(self.sample_rate, "audio segment sample_rate")
        _positive_int(self.channels, "audio segment channels")
        _fingerprint(self.payload_fingerprint, "audio payload_fingerprint")
        _fingerprint(self.preprocessing_fingerprint, "audio preprocessing_fingerprint")
        _positive_int(self.payload_bytes, "audio payload_bytes")

    def to_wire(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "source_fingerprint": self.source_fingerprint,
            "start": self.start.to_wire(),
            "end": self.end.to_wire(),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "payload_fingerprint": self.payload_fingerprint,
            "preprocessing_fingerprint": self.preprocessing_fingerprint,
            "payload_bytes": self.payload_bytes,
        }


@dataclass(frozen=True, slots=True)
class CanonicalMediaBatch:
    """Hash-only decoded media descriptors ready for a model adapter boundary."""

    source_identity: SourceIdentity
    preprocessing_fingerprint: str
    frames: tuple[DecodedFrameArtifact, ...] = ()
    audio_segments: tuple[DecodedAudioSegment, ...] = ()
    status: str = "complete"
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source_identity, SourceIdentity):
            raise MediaAdmissionError("batch source_identity must be SourceIdentity")
        _fingerprint(self.preprocessing_fingerprint, "batch preprocessing_fingerprint")
        if not isinstance(self.frames, tuple) or not isinstance(self.audio_segments, tuple):
            raise MediaAdmissionError("batch artifacts must be tuples")
        if len(self.frames) + len(self.audio_segments) > MAX_MEDIA_ARTIFACTS:
            raise MediaAdmissionError("batch artifact count exceeds the finite limit")
        if not all(isinstance(item, DecodedFrameArtifact) for item in self.frames):
            raise MediaAdmissionError("batch frames must contain DecodedFrameArtifact values")
        if not all(isinstance(item, DecodedAudioSegment) for item in self.audio_segments):
            raise MediaAdmissionError("batch audio must contain DecodedAudioSegment values")
        frame_ids = tuple(item.frame_id for item in self.frames)
        if len(frame_ids) != len(set(frame_ids)):
            raise MediaAdmissionError("batch frame IDs must be unique")
        segment_ids = tuple(item.segment_id for item in self.audio_segments)
        if len(segment_ids) != len(set(segment_ids)):
            raise MediaAdmissionError("batch audio segment IDs must be unique")
        for frame_item in self.frames:
            if frame_item.source_fingerprint != self.source_identity.source_fingerprint:
                raise MediaAdmissionError("frame source identity does not match batch")
            if frame_item.preprocessing_fingerprint != self.preprocessing_fingerprint:
                raise MediaAdmissionError("frame preprocessing identity does not match batch")
        for audio_item in self.audio_segments:
            if audio_item.source_fingerprint != self.source_identity.source_fingerprint:
                raise MediaAdmissionError("audio source identity does not match batch")
            if audio_item.preprocessing_fingerprint != self.preprocessing_fingerprint:
                raise MediaAdmissionError("audio preprocessing identity does not match batch")
        for previous, current in zip(self.frames, self.frames[1:], strict=False):
            if current.sequence <= previous.sequence or not _timestamp_at_or_after(
                current.timestamp, previous.timestamp
            ):
                raise MediaAdmissionError("frame source PTS must be monotone")
        for previous_audio, current_audio in zip(
            self.audio_segments, self.audio_segments[1:], strict=False
        ):
            if not _timestamp_at_or_after(current_audio.start, previous_audio.end):
                raise MediaAdmissionError("audio segment spans must be ordered and non-overlapping")
        if self.status not in {"complete", "partial", "cancelled", "failed"}:
            raise MediaAdmissionError("unsupported media batch status")
        if not isinstance(self.diagnostics, tuple) or len(self.diagnostics) > MAX_MEDIA_DIAGNOSTICS:
            raise MediaAdmissionError("batch diagnostics exceed the finite limit")
        object.__setattr__(
            self, "diagnostics", tuple(_code(item, "batch diagnostic") for item in self.diagnostics)
        )

    @property
    def decoded_payload_bytes(self) -> int:
        """Return the bounded byte count declared by the decoder adapter."""

        return sum(item.payload_bytes for item in self.frames) + sum(
            item.payload_bytes for item in self.audio_segments
        )

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": MEDIA_ADMISSION_SCHEMA,
            "source_identity": self.source_identity.to_public_dict(),
            "preprocessing_fingerprint": self.preprocessing_fingerprint,
            "frames": [item.to_wire() for item in self.frames],
            "audio_segments": [item.to_wire() for item in self.audio_segments],
            "decoded_payload_bytes": self.decoded_payload_bytes,
            "status": self.status,
            "diagnostics": list(self.diagnostics),
        }


def validate_media_batch_limits(
    batch: CanonicalMediaBatch,
    limits: MediaLimits,
    *,
    temporary_bytes: int = 0,
) -> None:
    """Apply decode and temporary-storage budgets before a model adapter sees a batch.

    The pure batch deliberately carries only the adapter-declared byte sizes.  This function is
    the explicit post-decode gate that binds those sizes to the request limits; a caller cannot
    obtain a successful validation by merely constructing a descriptor.
    """

    if not isinstance(batch, CanonicalMediaBatch):
        raise MediaAdmissionError("batch must be a CanonicalMediaBatch")
    if not isinstance(limits, MediaLimits):
        raise MediaAdmissionError("limits must be MediaLimits")
    _non_negative_int(temporary_bytes, "temporary_bytes")
    if temporary_bytes > limits.max_temp_bytes:
        raise MediaAdmissionError("temporary storage exceeds the declared limit")
    if batch.decoded_payload_bytes > limits.max_decoded_bytes:
        raise MediaAdmissionError("decoded media exceeds the declared output limit")
    if len(batch.frames) > limits.max_frames:
        raise MediaAdmissionError("decoded frame count exceeds the declared limit")
    for frame in batch.frames:
        if frame.width > limits.max_width or frame.height > limits.max_height:
            raise MediaAdmissionError("decoded frame dimensions exceed the declared limit")
        if frame.timestamp.seconds > limits.max_duration_seconds:
            raise MediaAdmissionError("decoded frame timestamp exceeds the declared duration")
    for segment in batch.audio_segments:
        if segment.sample_rate > limits.max_sample_rate or segment.channels > limits.max_channels:
            raise MediaAdmissionError("decoded audio rate/channels exceed the declared limit")
        if segment.end.seconds > limits.max_duration_seconds:
            raise MediaAdmissionError("decoded audio timestamp exceeds the declared duration")


__all__ = [
    "MEDIA_ADMISSION_SCHEMA",
    "MEDIA_PREPROCESS_SCHEMA",
    "AudioStreamProbe",
    "CanonicalMediaBatch",
    "DecodedAudioSegment",
    "DecodedFrameArtifact",
    "MediaAdmissionDecision",
    "MediaAdmissionRequest",
    "MediaAdmissionStatus",
    "MediaAllowlist",
    "MediaLimits",
    "MediaSource",
    "MediaSourceKind",
    "PresentationTimestamp",
    "PreprocessingSpec",
    "ProbeManifest",
    "ProbeStatus",
    "RedirectTrace",
    "SourceIdentity",
    "VideoStreamProbe",
    "admit_media_source",
    "parse_ffprobe_manifest",
    "validate_media_batch_limits",
]
