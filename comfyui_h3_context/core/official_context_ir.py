"""Explicit, injected adapter for the published MiniMax H3-Context-IR API contract.

This module owns request mapping and response validation only. It does not open media, upload
files, resolve URLs, choose a provider, retry, or import an HTTP SDK. A caller
must supply an explicitly consented remote policy, a runtime credential resolver, and a transport
implementation. The public result contains the official prompt and a redacted receipt; terminal
API errors never become a successful prompt.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Protocol, cast, runtime_checkable
from urllib.parse import urlsplit

from .canonical import canonical_fingerprint
from .context_reporting import ProviderOutcome, ProviderReceipt
from .contracts import AssetRole, MediaKind, ProviderIdentity, TaskMode
from .errors import ContractValidationError, OfficialContextIRError, SecurityPolicyError
from .normalization import NormalizedContextRequest
from .provider_policy import (
    CredentialResolver,
    ProviderExecutionPolicy,
    ResolvedCredential,
    resolve_provider_credential,
    validate_provider_policy,
)
from .providers import (
    CURRENT_PROVIDER_PROTOCOL_VERSION,
    CredentialRequirement,
    NetworkRequirement,
    PrivacyLocation,
    ProviderCapabilities,
    ProviderCapabilityRequest,
    ProviderDescriptor,
    ProviderOutputContract,
    ResourceLimits,
    validate_provider_capabilities,
)
from .registry import ReferenceRegistry

OFFICIAL_CONTEXT_IR_ENDPOINT = "https://api.minimax.io/v2/h3_context_ir"
OFFICIAL_CONTEXT_IR_QUERY_PATH = "/v2/query/video_generation/{task_id}"
OFFICIAL_CONTEXT_IR_MODEL = "MiniMax-H3"
OFFICIAL_CONTEXT_IR_PROVIDER_VERSION = "MiniMax-H3"
OFFICIAL_CONTEXT_IR_ENDPOINT_REVISION = "api-v2-h3-context-ir"

MAX_OFFICIAL_DURATION_SECONDS = 15
MIN_OFFICIAL_DURATION_SECONDS = 4
MAX_OFFICIAL_PROMPT_CHARS = 7000
MAX_OFFICIAL_REQUEST_BYTES = 64 * 1024 * 1024
MAX_OFFICIAL_MIXED_FILES = 12
MAX_OFFICIAL_IMAGE_REFERENCES = 9
MAX_OFFICIAL_VIDEO_REFERENCES = 3
MAX_OFFICIAL_AUDIO_REFERENCES = 3
MAX_OFFICIAL_MEDIA_BYTES = {
    MediaKind.IMAGE: 30 * 1024 * 1024,
    MediaKind.VIDEO: 50 * 1024 * 1024,
    MediaKind.AUDIO: 15 * 1024 * 1024,
}
MAX_OFFICIAL_DIMENSION = 5760
MIN_OFFICIAL_DIMENSION = 256
MIN_OFFICIAL_ASPECT_RATIO = 0.4
MAX_OFFICIAL_ASPECT_RATIO = 2.5
MIN_OFFICIAL_REFERENCE_DURATION = 2.0
MAX_OFFICIAL_REFERENCE_DURATION = 15.0
MIN_OFFICIAL_FRAME_RATE = 23.976
MAX_OFFICIAL_FRAME_RATE = 60.0

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\\]")
_URL_CONTROL_PATTERN = re.compile(r"[\x00-\x20\x7f\\]")
_IMAGE_TYPES = frozenset(
    {
        "jpg",
        "jpeg",
        "png",
        "webp",
        "heic",
        "heif",
        "image/jpg",
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/heic",
        "image/heif",
    }
)
_VIDEO_TYPES = frozenset({"mp4", "mov", "video/mp4", "video/quicktime"})
_AUDIO_TYPES = frozenset({"wav", "mp3", "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp3"})
_VIDEO_CODECS = frozenset({"h264", "h.264", "avc", "h265", "h.265", "hevc"})


class OfficialAspectRatio(str, Enum):
    """Aspect-ratio values currently accepted by the official endpoint."""

    ADAPTIVE = "adaptive"
    RATIO_21_9 = "21:9"
    RATIO_16_9 = "16:9"
    RATIO_4_3 = "4:3"
    RATIO_1_1 = "1:1"
    RATIO_3_4 = "3:4"
    RATIO_9_16 = "9:16"


class OfficialContextIRMediaRole(str, Enum):
    """Published media-role labels for H3-Context-IR content items."""

    FIRST_FRAME = "first_frame"
    LAST_FRAME = "last_frame"
    REFERENCE_IMAGE = "reference_image"
    REFERENCE_VIDEO = "reference_video"
    REFERENCE_AUDIO = "reference_audio"


class OfficialContextIRTaskStatus(str, Enum):
    """Documented asynchronous task states returned by the shared query endpoint."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class OfficialContextIRLifecyclePolicy:
    """Finite polling and wall-time budgets for one official task."""

    max_polls: int = 30
    max_wall_time_seconds: float = 600.0
    poll_interval_seconds: float = 10.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_polls, bool)
            or not isinstance(self.max_polls, int)
            or self.max_polls <= 0
        ):
            raise ContractValidationError("max_polls must be a positive integer")
        for value, field in (
            (self.max_wall_time_seconds, "max_wall_time_seconds"),
            (self.poll_interval_seconds, "poll_interval_seconds"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
                or float(value) <= 0
            ):
                raise ContractValidationError(f"{field} must be a finite positive number")


@runtime_checkable
class OfficialContextIRCancellationProbe(Protocol):
    """Injected local cancellation signal checked before each lifecycle operation."""

    def is_cancelled(self) -> bool:
        """Return whether the caller has cancelled local polling."""


DEFAULT_OFFICIAL_CONTEXT_IR_LIFECYCLE_POLICY = OfficialContextIRLifecyclePolicy()


def _fail(category: str, message: str) -> OfficialContextIRError:
    return OfficialContextIRError(category, message)


def _require_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise _fail("invalid_request", f"{field} must be a bounded identifier")
    return value


def _require_non_empty_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _fail(
            "invalid_request", f"{field} must be non-empty and at most {maximum} characters"
        )
    if _CONTROL_PATTERN.search(value):
        raise _fail("invalid_request", f"{field} contains an unsafe control character")
    return value


def _normalise_url(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _fail("invalid_media", "media URL must be a non-empty trimmed string")
    if _URL_CONTROL_PATTERN.search(value):
        raise _fail("invalid_media", "media URL contains whitespace, control, or backslash data")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
    except ValueError:
        raise _fail("invalid_media", "media URL is malformed") from None
    if parsed.scheme.casefold() != "https" or hostname is None:
        raise _fail("invalid_media", "media URL must use HTTPS with a public host")
    if username is not None or password is not None or parsed.query or parsed.fragment:
        raise _fail("invalid_media", "media URL userinfo, query, and fragment are not accepted")
    if not parsed.path or parsed.path == "/":
        raise _fail("invalid_media", "media URL must identify a resource path")
    return value


def _normalise_media_type(value: object) -> str:
    if not isinstance(value, str) or not value or value.casefold() != value.strip().casefold():
        raise _fail("invalid_media", "media_type must be a bounded media format")
    return value.strip().casefold()


def _require_positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _fail("invalid_media", f"{field} must be a positive integer")
    return value


def _require_dimension(value: object, field: str) -> int:
    integer = _require_positive_int(value, field)
    if not MIN_OFFICIAL_DIMENSION <= integer <= MAX_OFFICIAL_DIMENSION:
        raise _fail(
            "media_limit",
            f"{field} must be between {MIN_OFFICIAL_DIMENSION} and {MAX_OFFICIAL_DIMENSION}",
        )
    return integer


def _require_duration(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail("invalid_media", f"{field} must be a finite number")
    duration = float(value)
    if (
        not isfinite(duration)
        or not MIN_OFFICIAL_REFERENCE_DURATION <= duration <= MAX_OFFICIAL_REFERENCE_DURATION
    ):
        raise _fail(
            "media_limit",
            f"{field} must be between {MIN_OFFICIAL_REFERENCE_DURATION:g} and "
            f"{MAX_OFFICIAL_REFERENCE_DURATION:g} seconds",
        )
    return duration


def _validate_aspect_ratio(width: int, height: int) -> None:
    ratio = width / height
    if not MIN_OFFICIAL_ASPECT_RATIO <= ratio <= MAX_OFFICIAL_ASPECT_RATIO:
        raise _fail("media_limit", "media aspect ratio must be between 0.4 and 2.5")


@dataclass(frozen=True, slots=True)
class OfficialContextIRMedia:
    """Caller-supplied, public media reference and metadata; no media is opened here."""

    asset_id: str
    kind: MediaKind
    role: OfficialContextIRMediaRole | str
    url: str
    media_type: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    frame_rate: float | None = None
    codec: str | None = None
    connection_order: int = 1

    def __post_init__(self) -> None:
        _require_identifier(self.asset_id, "asset_id")
        if not isinstance(self.kind, MediaKind):
            raise ContractValidationError("media kind must be a MediaKind")
        try:
            role = (
                self.role
                if isinstance(self.role, OfficialContextIRMediaRole)
                else OfficialContextIRMediaRole(self.role)
            )
        except ValueError:
            raise _fail(
                "invalid_media", "media role is not supported by the official endpoint"
            ) from None
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "url", _normalise_url(self.url))
        media_type = _normalise_media_type(self.media_type)
        object.__setattr__(self, "media_type", media_type)
        size_bytes = _require_positive_int(self.size_bytes, "size_bytes")
        if size_bytes > MAX_OFFICIAL_MEDIA_BYTES[self.kind]:
            raise _fail(
                "media_limit", f"{self.kind.value} media exceeds the documented file-size limit"
            )
        object.__setattr__(self, "size_bytes", size_bytes)
        if (
            isinstance(self.connection_order, bool)
            or not isinstance(self.connection_order, int)
            or self.connection_order <= 0
        ):
            raise _fail("invalid_media", "connection_order must be a positive integer")

        if self.kind is MediaKind.IMAGE:
            if (
                role
                not in {
                    OfficialContextIRMediaRole.FIRST_FRAME,
                    OfficialContextIRMediaRole.LAST_FRAME,
                    OfficialContextIRMediaRole.REFERENCE_IMAGE,
                }
                or media_type not in _IMAGE_TYPES
            ):
                raise _fail("invalid_media", "image media type and role do not match")
            if self.width is None or self.height is None:
                raise _fail("invalid_media", "image width and height metadata are required")
            width = _require_dimension(self.width, "width")
            height = _require_dimension(self.height, "height")
            _validate_aspect_ratio(width, height)
            object.__setattr__(self, "width", width)
            object.__setattr__(self, "height", height)
            if (
                self.duration_seconds is not None
                or self.frame_rate is not None
                or self.codec is not None
            ):
                raise _fail("invalid_media", "image media must not carry video-only metadata")
            return

        if self.kind is MediaKind.VIDEO:
            if (
                role is not OfficialContextIRMediaRole.REFERENCE_VIDEO
                or media_type not in _VIDEO_TYPES
            ):
                raise _fail("invalid_media", "video media type and role do not match")
            if (
                self.width is None
                or self.height is None
                or self.duration_seconds is None
                or self.frame_rate is None
            ):
                raise _fail(
                    "invalid_media", "video dimensions, duration, and frame rate are required"
                )
            width = _require_dimension(self.width, "width")
            height = _require_dimension(self.height, "height")
            _validate_aspect_ratio(width, height)
            duration = _require_duration(self.duration_seconds, "duration_seconds")
            if isinstance(self.frame_rate, bool) or not isinstance(self.frame_rate, (int, float)):
                raise _fail("invalid_media", "frame_rate must be a finite number")
            frame_rate = float(self.frame_rate)
            if (
                not isfinite(frame_rate)
                or not MIN_OFFICIAL_FRAME_RATE <= frame_rate <= MAX_OFFICIAL_FRAME_RATE
            ):
                raise _fail("media_limit", "video frame_rate must be between 23.976 and 60")
            if self.codec is None or self.codec.casefold() not in _VIDEO_CODECS:
                raise _fail("invalid_media", "video codec must be H.264/AVC or H.265/HEVC")
            object.__setattr__(self, "width", width)
            object.__setattr__(self, "height", height)
            object.__setattr__(self, "duration_seconds", duration)
            object.__setattr__(self, "frame_rate", frame_rate)
            object.__setattr__(self, "codec", self.codec.casefold())
            return

        if role is not OfficialContextIRMediaRole.REFERENCE_AUDIO or media_type not in _AUDIO_TYPES:
            raise _fail("invalid_media", "audio media type and role do not match")
        if self.duration_seconds is None:
            raise _fail("invalid_media", "audio duration metadata is required")
        duration = _require_duration(self.duration_seconds, "duration_seconds")
        if (
            self.width is not None
            or self.height is not None
            or self.frame_rate is not None
            or self.codec is not None
        ):
            raise _fail("invalid_media", "audio media must not carry image/video-only metadata")
        object.__setattr__(self, "duration_seconds", duration)

    def to_payload(self) -> dict[str, object]:
        """Return the transient official content item; callers must not persist this mapping."""

        item: dict[str, object] = {
            "type": {
                MediaKind.IMAGE: "image_url",
                MediaKind.VIDEO: "video_url",
                MediaKind.AUDIO: "audio_url",
            }[self.kind],
            f"{self.kind.value}_url": {"url": self.url},
        }
        if (
            self.role is not OfficialContextIRMediaRole.REFERENCE_IMAGE
            or self.kind is not MediaKind.IMAGE
        ):
            item["role"] = cast(OfficialContextIRMediaRole, self.role).value
        else:
            item["role"] = OfficialContextIRMediaRole.REFERENCE_IMAGE.value
        return item

    def to_public_dict(self) -> dict[str, object]:
        """Return redacted metadata suitable for reports; URL and raw media are excluded."""

        return {
            "asset_id": self.asset_id,
            "kind": self.kind.value,
            "role": cast(OfficialContextIRMediaRole, self.role).value,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "duration_seconds": self.duration_seconds,
            "frame_rate": self.frame_rate,
            "codec": self.codec,
            "connection_order": self.connection_order,
        }


def _normalise_ratio(value: OfficialAspectRatio | str) -> OfficialAspectRatio:
    try:
        return value if isinstance(value, OfficialAspectRatio) else OfficialAspectRatio(value)
    except ValueError:
        raise _fail("invalid_request", "ratio is not supported by the official endpoint") from None


def _duration_from_normalized(request: NormalizedContextRequest) -> int:
    requested = request.requested_duration_seconds
    if requested is None or not isfinite(requested) or requested != int(requested):
        raise _fail("invalid_request", "official Context-IR requires an explicit integer duration")
    duration = int(requested)
    if not MIN_OFFICIAL_DURATION_SECONDS <= duration <= MAX_OFFICIAL_DURATION_SECONDS:
        raise _fail("duration_limit", "duration must be an integer between 4 and 15 seconds")
    return duration


def _expected_asset_ids(registry: ReferenceRegistry) -> tuple[str, ...]:
    return tuple(asset.asset_id for asset in registry.assets)


def _validate_mode_and_media(
    request: NormalizedContextRequest,
    ratio: OfficialAspectRatio,
    media: tuple[OfficialContextIRMedia, ...],
) -> None:
    mode = request.task_mode
    roles = tuple(cast(OfficialContextIRMediaRole, value.role) for value in media)
    if len(media) > MAX_OFFICIAL_MIXED_FILES:
        raise _fail("media_limit", "mixed reference input exceeds the documented twelve-file limit")
    counts = {
        MediaKind.IMAGE: sum(item.kind is MediaKind.IMAGE for item in media),
        MediaKind.VIDEO: sum(item.kind is MediaKind.VIDEO for item in media),
        MediaKind.AUDIO: sum(item.kind is MediaKind.AUDIO for item in media),
    }
    if counts[MediaKind.IMAGE] > MAX_OFFICIAL_IMAGE_REFERENCES:
        raise _fail("media_limit", "reference images exceed the documented limit of nine")
    if counts[MediaKind.VIDEO] > MAX_OFFICIAL_VIDEO_REFERENCES:
        raise _fail("media_limit", "reference videos exceed the documented limit of three")
    if counts[MediaKind.AUDIO] > MAX_OFFICIAL_AUDIO_REFERENCES:
        raise _fail("media_limit", "reference audio exceeds the documented limit of three")
    for kind in (MediaKind.VIDEO, MediaKind.AUDIO):
        durations = [
            float(item.duration_seconds)
            for item in media
            if item.kind is kind and item.duration_seconds is not None
        ]
        if sum(durations) > MAX_OFFICIAL_REFERENCE_DURATION:
            raise _fail("media_limit", f"reference {kind.value} duration exceeds 15 seconds total")
    if mode is TaskMode.T2VA:
        if media or ratio is OfficialAspectRatio.ADAPTIVE:
            raise _fail("invalid_request", "T2VA requires text only and a concrete ratio")
        return
    if mode is TaskMode.I2VA:
        if (
            len(media) != 1
            or roles != (OfficialContextIRMediaRole.FIRST_FRAME,)
            or ratio is not OfficialAspectRatio.ADAPTIVE
        ):
            raise _fail("invalid_request", "I2VA requires one first_frame image and adaptive ratio")
        return
    if mode is TaskMode.L2VA:
        if (
            len(media) != 1
            or roles != (OfficialContextIRMediaRole.LAST_FRAME,)
            or ratio is not OfficialAspectRatio.ADAPTIVE
        ):
            raise _fail("invalid_request", "L2VA requires one last_frame image and adaptive ratio")
        return
    if mode is TaskMode.FL2VA:
        if (
            len(media) != 2
            or set(roles)
            != {
                OfficialContextIRMediaRole.FIRST_FRAME,
                OfficialContextIRMediaRole.LAST_FRAME,
            }
            or ratio is not OfficialAspectRatio.ADAPTIVE
        ):
            raise _fail(
                "invalid_request",
                "FL2VA requires first_frame and last_frame images with adaptive ratio",
            )
        return
    if not media or any(
        role in {OfficialContextIRMediaRole.FIRST_FRAME, OfficialContextIRMediaRole.LAST_FRAME}
        for role in roles
    ):
        raise _fail(
            "invalid_request", "REF2VA requires reference roles and cannot mix frame anchors"
        )
    # IMPORTANT: Context-IR accepts text plus any reference-media combination. The CLI's
    # visual-reference prerequisite is a different contract and must not reject audio here.
    if ratio not in set(OfficialAspectRatio):
        raise _fail("invalid_request", "REF2VA ratio is invalid")


@dataclass(frozen=True, slots=True)
class OfficialContextIRRequest:
    """Validated official request projection derived from one normalized H3 request."""

    request: NormalizedContextRequest
    ratio: OfficialAspectRatio
    duration: int
    media: tuple[OfficialContextIRMedia, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.request, NormalizedContextRequest):
            raise ContractValidationError("official request must contain a normalized request")
        if not isinstance(self.ratio, OfficialAspectRatio):
            raise ContractValidationError("official request ratio must be an OfficialAspectRatio")
        if isinstance(self.duration, bool) or not isinstance(self.duration, int):
            raise ContractValidationError("official request duration must be an integer")
        if not MIN_OFFICIAL_DURATION_SECONDS <= self.duration <= MAX_OFFICIAL_DURATION_SECONDS:
            raise _fail("duration_limit", "duration must be an integer between 4 and 15 seconds")
        if not isinstance(self.media, tuple) or not all(
            isinstance(item, OfficialContextIRMedia) for item in self.media
        ):
            raise ContractValidationError("official request media must be a tuple of media values")
        _validate_mode_and_media(self.request, self.ratio, self.media)
        registry = self.request.reference_registry
        expected = _expected_asset_ids(registry)
        actual = tuple(item.asset_id for item in self.media)
        if actual != expected:
            if actual or expected:
                raise _fail(
                    "reference_order", "media must match the canonical registry order exactly"
                )
        if len(actual) != len(set(actual)):
            raise _fail("reference_order", "media asset IDs must be unique")
        for item, asset in zip(self.media, registry.assets, strict=True):
            if item.kind is not asset.kind or item.connection_order != asset.connection_order:
                raise _fail(
                    "reference_order",
                    "media kind and connection order must match the canonical registry",
                )
            canonical_role = asset.role
            media_role = cast(OfficialContextIRMediaRole, item.role)
            if (
                self.request.task_mode is TaskMode.I2VA
                and canonical_role is not AssetRole.FIRST_FRAME
            ):
                raise _fail("invalid_request", "I2VA media must own the canonical first_frame role")
            if (
                self.request.task_mode is TaskMode.L2VA
                and canonical_role is not AssetRole.LAST_FRAME
            ):
                raise _fail("invalid_request", "L2VA media must own the canonical last_frame role")
            if self.request.task_mode is TaskMode.FL2VA:
                expected_role = (
                    OfficialContextIRMediaRole.FIRST_FRAME
                    if canonical_role is AssetRole.FIRST_FRAME
                    else OfficialContextIRMediaRole.LAST_FRAME
                    if canonical_role is AssetRole.LAST_FRAME
                    else None
                )
                if expected_role is None or media_role is not expected_role:
                    raise _fail(
                        "invalid_request", "FL2VA media roles must match canonical frame roles"
                    )
            if self.request.task_mode is TaskMode.REF2VA:
                if canonical_role in {AssetRole.FIRST_FRAME, AssetRole.LAST_FRAME}:
                    raise _fail(
                        "invalid_request", "reference mode cannot contain canonical frame anchors"
                    )
                expected_role = {
                    MediaKind.IMAGE: OfficialContextIRMediaRole.REFERENCE_IMAGE,
                    MediaKind.VIDEO: OfficialContextIRMediaRole.REFERENCE_VIDEO,
                    MediaKind.AUDIO: OfficialContextIRMediaRole.REFERENCE_AUDIO,
                }[item.kind]
                if media_role is not expected_role:
                    raise _fail("invalid_request", "reference media role must match its media kind")
        payload = self.to_payload()
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, UnicodeError, ValueError):
            raise _fail("invalid_request", "official request cannot be encoded as JSON") from None
        if len(encoded) > MAX_OFFICIAL_REQUEST_BYTES:
            raise _fail("request_limit", "official request body exceeds 64 MiB")

    def to_payload(self) -> dict[str, object]:
        content: list[dict[str, object]] = [
            {
                "type": "text",
                "text": _require_non_empty_text(
                    self.request.user_intent, "user_intent", MAX_OFFICIAL_PROMPT_CHARS
                ),
            }
        ]
        content.extend(media.to_payload() for media in self.media)
        return {
            "model": OFFICIAL_CONTEXT_IR_MODEL,
            "content": content,
            "duration": self.duration,
            "ratio": self.ratio.value,
        }

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "task_mode": self.request.task_mode.value,
            "duration": self.duration,
            "ratio": self.ratio.value,
            "asset_ids": [media.asset_id for media in self.media],
            "media": [media.to_public_dict() for media in self.media],
        }


def build_official_context_ir_request(
    request: NormalizedContextRequest,
    *,
    ratio: OfficialAspectRatio | str,
    media: tuple[OfficialContextIRMedia, ...] = (),
) -> OfficialContextIRRequest:
    """Build and validate one deterministic official request projection."""

    if not isinstance(request, NormalizedContextRequest):
        raise ContractValidationError("request must be a NormalizedContextRequest")
    return OfficialContextIRRequest(
        request=request,
        ratio=_normalise_ratio(ratio),
        duration=_duration_from_normalized(request),
        media=media,
    )


@dataclass(frozen=True, slots=True)
class OfficialContextIRTransportResponse:
    """JSON response returned by an injected transport implementation."""

    status_code: int
    body: Mapping[str, object]

    def __post_init__(self) -> None:
        if (
            isinstance(self.status_code, bool)
            or not isinstance(self.status_code, int)
            or not 100 <= self.status_code <= 599
        ):
            raise ContractValidationError("transport status_code must be an HTTP status integer")
        if not isinstance(self.body, Mapping):
            raise ContractValidationError("transport body must be a mapping")


@runtime_checkable
class OfficialContextIRTransport(Protocol):
    """Injected create/query seam; concrete HTTP clients belong outside pure core."""

    def create(
        self,
        payload: dict[str, object],
        credential: ResolvedCredential,
    ) -> OfficialContextIRTransportResponse:
        """Submit an already validated payload."""

    def query(
        self, task_id: str, credential: ResolvedCredential
    ) -> OfficialContextIRTransportResponse:
        """Return a task response; lifecycle polling is bounded by the caller."""


def _official_limits() -> ResourceLimits:
    return ResourceLimits(
        max_bytes=MAX_OFFICIAL_REQUEST_BYTES,
        max_duration_seconds=float(MAX_OFFICIAL_DURATION_SECONDS),
        max_width=MAX_OFFICIAL_DIMENSION,
        max_height=MAX_OFFICIAL_DIMENSION,
        max_frames=3600,
        max_sample_rate=192000,
        max_references=MAX_OFFICIAL_MIXED_FILES,
        max_concurrency=1,
        max_memory_bytes=MAX_OFFICIAL_REQUEST_BYTES,
        max_wall_time_seconds=600.0,
        max_retries=0,
        max_cache_ttl_seconds=0.0,
    )


OFFICIAL_CONTEXT_IR_DESCRIPTOR = ProviderDescriptor(
    protocol_version=CURRENT_PROVIDER_PROTOCOL_VERSION,
    identity=ProviderIdentity.OFFICIAL_MINIMAX,
    provider_version=OFFICIAL_CONTEXT_IR_PROVIDER_VERSION,
    capabilities=ProviderCapabilities(
        supported_task_modes=frozenset(TaskMode),
        supported_media=frozenset(MediaKind),
        privacy_location=PrivacyLocation.REMOTE,
        network_requirement=NetworkRequirement.INTERNET,
        credential_requirement=CredentialRequirement.REQUIRED,
        limits=_official_limits(),
        supports_determinism=False,
        supports_seed=False,
        supports_cancellation=True,
    ),
    output_contract=ProviderOutputContract(
        schema_version=CURRENT_PROVIDER_PROTOCOL_VERSION,
        output_schema="h3.context.ir.v1",
        required_fields=("prompt", "receipt"),
    ),
)


@dataclass(frozen=True, slots=True)
class OfficialContextIRResult:
    """Successful official prompt and redacted lifecycle receipt."""

    prompt: str
    receipt: ProviderReceipt

    def __post_init__(self) -> None:
        _require_non_empty_text(self.prompt, "prompt", 65_536)
        if not isinstance(self.receipt, ProviderReceipt):
            raise ContractValidationError("receipt must be a ProviderReceipt")
        if (
            self.receipt.provider is not ProviderIdentity.OFFICIAL_MINIMAX
            or not self.receipt.is_successful
        ):
            raise ContractValidationError(
                "official result receipt must be a successful official receipt"
            )

    def to_public_dict(self) -> dict[str, object]:
        return {"prompt": self.prompt, "receipt": self.receipt.to_wire()}


_STATUS_MAPPING: dict[int, tuple[str, ProviderOutcome]] = {
    400: ("bad_request", ProviderOutcome.FAILED),
    401: ("authentication", ProviderOutcome.AUTHENTICATION),
    402: ("quota", ProviderOutcome.QUOTA),
    422: ("moderated", ProviderOutcome.MODERATED),
    429: ("rate_limit", ProviderOutcome.RETRYABLE_TRANSPORT),
    500: ("server_error", ProviderOutcome.FAILED),
}


def _error_type_marker(value: object) -> str:
    if isinstance(value, Mapping):
        marker = value.get("type")
        if isinstance(marker, str):
            return marker.casefold()
    return ""


def _classify_error(
    status_code: int,
    body: Mapping[str, object],
) -> tuple[str, ProviderOutcome]:
    marker = _error_type_marker(body.get("error"))
    if "unsupported" in marker or ("media" in marker and status_code in {400, 422}):
        return "unsupported_media", ProviderOutcome.UNSUPPORTED_MEDIA
    return _STATUS_MAPPING.get(status_code, ("api_error", ProviderOutcome.FAILED))


def _classify_task_failure(task: Mapping[str, object]) -> tuple[str, ProviderOutcome]:
    marker = _error_type_marker(task.get("error"))
    if "unsupported" in marker or "media" in marker:
        return "unsupported_media", ProviderOutcome.UNSUPPORTED_MEDIA
    if "moder" in marker or "sensitive" in marker:
        return "moderated", ProviderOutcome.MODERATED
    if "auth" in marker:
        return "authentication", ProviderOutcome.AUTHENTICATION
    if "quota" in marker or "balance" in marker:
        return "quota", ProviderOutcome.QUOTA
    return "failed", ProviderOutcome.FAILED


def _receipt(
    receipt_id: str,
    outcome: ProviderOutcome,
    *,
    task_id: str | None = None,
    input_fingerprint: str | None = None,
    output_fingerprint: str | None = None,
    message: str | None = None,
) -> ProviderReceipt:
    return ProviderReceipt(
        receipt_id=_require_identifier(receipt_id, "receipt_id"),
        provider=ProviderIdentity.OFFICIAL_MINIMAX,
        outcome=outcome,
        provider_version=OFFICIAL_CONTEXT_IR_PROVIDER_VERSION,
        endpoint_revision=OFFICIAL_CONTEXT_IR_ENDPOINT_REVISION,
        task_id=task_id,
        input_fingerprint=input_fingerprint,
        output_fingerprint=output_fingerprint,
        redacted_message=message,
    )


def _receipt_fingerprint(value: str) -> str:
    """Convert the canonical ``sha256:`` token to the existing receipt wire vocabulary."""

    return value.removeprefix("sha256:")


def _response_error(
    response: OfficialContextIRTransportResponse,
    receipt_id: str,
    input_fingerprint: str,
    *,
    task_id: str | None = None,
) -> OfficialContextIRError:
    category, outcome = _classify_error(response.status_code, response.body)
    receipt = _receipt(
        receipt_id,
        outcome,
        task_id=task_id,
        input_fingerprint=input_fingerprint,
        message=f"official API returned HTTP {response.status_code}",
    )
    return OfficialContextIRError(
        category,
        f"official API request failed with HTTP {response.status_code}",
        receipt=receipt,
    )


class OfficialContextIRAdapter:
    """Execute one explicit official request through an injected transport."""

    @property
    def descriptor(self) -> ProviderDescriptor:
        return OFFICIAL_CONTEXT_IR_DESCRIPTOR

    def execute(
        self,
        request: OfficialContextIRRequest,
        *,
        policy: ProviderExecutionPolicy,
        resolver: CredentialResolver | None,
        transport: OfficialContextIRTransport,
        receipt_id: str = "official-context-ir",
        lifecycle: OfficialContextIRLifecyclePolicy = DEFAULT_OFFICIAL_CONTEXT_IR_LIFECYCLE_POLICY,
        cancellation_probe: OfficialContextIRCancellationProbe | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> OfficialContextIRResult:
        if not isinstance(request, OfficialContextIRRequest):
            raise ContractValidationError("request must be an OfficialContextIRRequest")
        if not isinstance(policy, ProviderExecutionPolicy):
            raise ContractValidationError("policy must be a ProviderExecutionPolicy")
        if not isinstance(transport, OfficialContextIRTransport):
            raise ContractValidationError("transport must implement OfficialContextIRTransport")
        if not isinstance(lifecycle, OfficialContextIRLifecyclePolicy):
            raise ContractValidationError("lifecycle must be an OfficialContextIRLifecyclePolicy")
        if cancellation_probe is not None and not isinstance(
            cancellation_probe, OfficialContextIRCancellationProbe
        ):
            raise ContractValidationError(
                "cancellation_probe must implement OfficialContextIRCancellationProbe"
            )
        clock_fn = time.monotonic if clock is None else clock
        sleep_fn = time.sleep if sleep is None else sleep
        if not callable(clock_fn) or not callable(sleep_fn):
            raise ContractValidationError("clock and sleep must be callable")
        diagnostics = validate_provider_policy(self.descriptor, policy)
        if diagnostics:
            raise SecurityPolicyError(f"official provider policy rejected: {diagnostics[0].code}")
        capability_diagnostics = validate_provider_capabilities(
            self.descriptor,
            ProviderCapabilityRequest(
                task_mode=request.request.task_mode,
                media_kinds=tuple(dict.fromkeys(media.kind for media in request.media)),
                reference_count=len(request.media),
                duration_seconds=float(request.duration),
                required_output_fields=("prompt", "receipt"),
            ),
        )
        if capability_diagnostics:
            raise OfficialContextIRError("capability", capability_diagnostics[0].code)
        payload = request.to_payload()
        input_fingerprint = _receipt_fingerprint(canonical_fingerprint(payload))

        def now() -> float:
            value = clock_fn()
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
            ):
                raise ContractValidationError("lifecycle clock must return a finite number")
            return float(value)

        def cancelled(task_id: str | None = None) -> OfficialContextIRError:
            return OfficialContextIRError(
                "cancelled",
                "official lifecycle was cancelled before the next operation",
                receipt=_receipt(
                    receipt_id,
                    ProviderOutcome.CANCELLED,
                    task_id=task_id,
                    input_fingerprint=input_fingerprint,
                    message="official lifecycle cancelled",
                ),
            )

        def timeout(task_id: str | None = None) -> OfficialContextIRError:
            return OfficialContextIRError(
                "timeout",
                "official lifecycle exceeded its bounded polling budget",
                receipt=_receipt(
                    receipt_id,
                    ProviderOutcome.TIMEOUT,
                    task_id=task_id,
                    input_fingerprint=input_fingerprint,
                    message="official lifecycle timeout",
                ),
            )

        def malformed(message: str, task_id: str | None = None) -> OfficialContextIRError:
            return OfficialContextIRError(
                "malformed_response",
                message,
                receipt=_receipt(
                    receipt_id,
                    ProviderOutcome.FAILED,
                    task_id=task_id,
                    input_fingerprint=input_fingerprint,
                    message="official response was malformed",
                ),
            )

        def cancellation_requested() -> bool:
            if cancellation_probe is None:
                return False
            try:
                value = cancellation_probe.is_cancelled()
            except Exception:
                raise OfficialContextIRError(
                    "cancellation_probe",
                    "official cancellation probe failed",
                    receipt=_receipt(
                        receipt_id,
                        ProviderOutcome.FAILED,
                        input_fingerprint=input_fingerprint,
                        message="official cancellation probe failed",
                    ),
                ) from None
            if not isinstance(value, bool):
                raise ContractValidationError("cancellation probe must return a boolean")
            return value

        started_at = now()
        if cancellation_requested():
            raise cancelled()
        credential = resolve_provider_credential(policy, self.descriptor, resolver)
        if credential is None:
            raise SecurityPolicyError("official provider credential is required")
        try:
            created = transport.create(payload, credential)
        except Exception:
            receipt = _receipt(
                receipt_id,
                ProviderOutcome.RETRYABLE_TRANSPORT,
                input_fingerprint=input_fingerprint,
                message="official transport create failed",
            )
            raise OfficialContextIRError(
                "transport", "official transport create failed", receipt=receipt
            ) from None
        if created.status_code < 200 or created.status_code >= 300:
            raise _response_error(created, receipt_id, input_fingerprint)
        task_id_value = created.body.get("task_id")
        if not isinstance(task_id_value, str) or _TASK_ID_PATTERN.fullmatch(task_id_value) is None:
            raise malformed("official create response has no valid task_id")

        if cancellation_requested():
            raise cancelled(task_id_value)
        poll_count = 0
        while True:
            if cancellation_requested():
                raise cancelled(task_id_value)
            if now() - started_at >= lifecycle.max_wall_time_seconds:
                raise timeout(task_id_value)
            try:
                queried = transport.query(task_id_value, credential)
            except Exception:
                receipt = _receipt(
                    receipt_id,
                    ProviderOutcome.RETRYABLE_TRANSPORT,
                    task_id=task_id_value,
                    input_fingerprint=input_fingerprint,
                    message="official transport query failed",
                )
                raise OfficialContextIRError(
                    "transport", "official transport query failed", receipt=receipt
                ) from None
            poll_count += 1
            if queried.status_code < 200 or queried.status_code >= 300:
                raise _response_error(queried, receipt_id, input_fingerprint, task_id=task_id_value)
            task_value = queried.body.get("task")
            if not isinstance(task_value, Mapping):
                raise malformed("official query response has no task object", task_id_value)
            try:
                status = OfficialContextIRTaskStatus(task_value.get("status"))
            except ValueError:
                raise malformed("official task status is not documented", task_id_value) from None
            if status is OfficialContextIRTaskStatus.CANCELLED:
                receipt = _receipt(
                    receipt_id,
                    ProviderOutcome.CANCELLED,
                    task_id=task_id_value,
                    input_fingerprint=input_fingerprint,
                    message="official task was cancelled",
                )
                raise OfficialContextIRError(
                    "cancelled", "official task was cancelled", receipt=receipt
                )
            if status is OfficialContextIRTaskStatus.FAILED:
                category, outcome = _classify_task_failure(task_value)
                receipt = _receipt(
                    receipt_id,
                    outcome,
                    task_id=task_id_value,
                    input_fingerprint=input_fingerprint,
                    message="official task failed",
                )
                raise OfficialContextIRError(category, "official task failed", receipt=receipt)
            if status is OfficialContextIRTaskStatus.SUCCEEDED:
                if task_value.get("task_type") != "h3_context_ir":
                    raise malformed("official task type is not h3_context_ir", task_id_value)
                content = task_value.get("content")
                if not isinstance(content, Mapping):
                    raise malformed("official task content is missing", task_id_value)
                prompt = content.get("prompt")
                if not isinstance(prompt, str) or not prompt.strip():
                    raise malformed("official task prompt is missing", task_id_value)
                output_fingerprint = _receipt_fingerprint(canonical_fingerprint(prompt))
                result_receipt = _receipt(
                    receipt_id,
                    ProviderOutcome.SUCCEEDED,
                    task_id=task_id_value,
                    input_fingerprint=input_fingerprint,
                    output_fingerprint=output_fingerprint,
                    message="official Context-IR prompt received",
                )
                return OfficialContextIRResult(prompt=prompt, receipt=result_receipt)
            if poll_count >= lifecycle.max_polls:
                raise timeout(task_id_value)
            if cancellation_requested():
                raise cancelled(task_id_value)
            elapsed = now() - started_at
            if elapsed >= lifecycle.max_wall_time_seconds:
                raise timeout(task_id_value)
            sleep_fn(
                min(lifecycle.poll_interval_seconds, lifecycle.max_wall_time_seconds - elapsed)
            )


def execute_official_context_ir(
    request: OfficialContextIRRequest,
    *,
    policy: ProviderExecutionPolicy,
    resolver: CredentialResolver | None,
    transport: OfficialContextIRTransport,
    receipt_id: str = "official-context-ir",
    lifecycle: OfficialContextIRLifecyclePolicy = DEFAULT_OFFICIAL_CONTEXT_IR_LIFECYCLE_POLICY,
    cancellation_probe: OfficialContextIRCancellationProbe | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> OfficialContextIRResult:
    """Functional convenience wrapper around :class:`OfficialContextIRAdapter`."""

    return OfficialContextIRAdapter().execute(
        request,
        policy=policy,
        resolver=resolver,
        transport=transport,
        receipt_id=receipt_id,
        lifecycle=lifecycle,
        cancellation_probe=cancellation_probe,
        clock=clock,
        sleep=sleep,
    )


__all__ = [
    "MAX_OFFICIAL_AUDIO_REFERENCES",
    "MAX_OFFICIAL_DURATION_SECONDS",
    "MAX_OFFICIAL_IMAGE_REFERENCES",
    "MAX_OFFICIAL_MIXED_FILES",
    "MAX_OFFICIAL_PROMPT_CHARS",
    "MAX_OFFICIAL_REQUEST_BYTES",
    "MAX_OFFICIAL_VIDEO_REFERENCES",
    "MIN_OFFICIAL_DURATION_SECONDS",
    "DEFAULT_OFFICIAL_CONTEXT_IR_LIFECYCLE_POLICY",
    "OFFICIAL_CONTEXT_IR_DESCRIPTOR",
    "OFFICIAL_CONTEXT_IR_ENDPOINT",
    "OFFICIAL_CONTEXT_IR_ENDPOINT_REVISION",
    "OFFICIAL_CONTEXT_IR_MODEL",
    "OFFICIAL_CONTEXT_IR_PROVIDER_VERSION",
    "OFFICIAL_CONTEXT_IR_QUERY_PATH",
    "OfficialAspectRatio",
    "OfficialContextIRAdapter",
    "OfficialContextIRMedia",
    "OfficialContextIRMediaRole",
    "OfficialContextIRTaskStatus",
    "OfficialContextIRLifecyclePolicy",
    "OfficialContextIRCancellationProbe",
    "OfficialContextIRRequest",
    "OfficialContextIRResult",
    "OfficialContextIRTransport",
    "OfficialContextIRTransportResponse",
    "build_official_context_ir_request",
    "execute_official_context_ir",
]
