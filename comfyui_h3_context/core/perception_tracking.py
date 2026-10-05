"""Source-owned detection, segmentation, tracking, and cross-shot re-ID contracts for M11-05.

The module is pure Python.  It consumes an already admitted M11-04 decode document and records
bounded, timestamped observations without importing a detector, segmenter, embedding runtime, or
provider SDK.  Identity-sensitive values are opt-in and portable receipts contain fingerprints,
never vectors or private media.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Protocol, cast, runtime_checkable

from .canonical import canonical_fingerprint
from .contracts import MediaKind, TaskMode
from .errors import PerceptionTrackingError
from .evidence import Uncertainty
from .local_adapters import (
    LocalAdapterDescriptor,
    LocalAdapterExecutionRequest,
    LocalAdapterResult,
    LocalAdapterRuntime,
    LocalBudgetGuard,
    LocalCancellationProbe,
    LocalDeviceKind,
    LocalDeviceSpec,
    run_local_adapter,
)
from .video_decode import (
    DecodedFrame,
    SourcePTS,
    VideoDecodeDocument,
)

PERCEPTION_TRACKING_SCHEMA = "h3.visual.perception_tracking.v1"
TRACKING_BENCHMARK_SCHEMA = "h3.visual.perception_tracking.benchmark.v1"
MAX_TRACKING_ASSETS = 3
MAX_TRACKING_DETECTIONS = 256
MAX_TRACKING_MASKS = 256
MAX_TRACKING_TRACKS = 64
MAX_TRACKING_LINKS = 256
MAX_TRACKING_CANDIDATES = 256
MAX_TRACKING_REFERENCES = 32
MAX_TRACKING_EMBEDDINGS = 64
MAX_TRACKING_CASES = 32
MAX_TRACKING_METRICS = 16
MAX_TRACKING_OUTPUT_BYTES = 65_536

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}\Z")
_SENSITIVE_MARKERS = (
    "http://",
    "https://",
    "file://",
    "api_key",
    "authorization",
    "bearer ",
    "password",
    "secret",
    "token=",
)


class TrackingStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"


class PerceptionRoute(str, Enum):
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"
    SPECIALIST = "specialist"


class IdentityPolicy(str, Enum):
    DISABLED = "disabled"
    LOCAL_OPT_IN = "local_opt_in"


class MaskRepresentation(str, Enum):
    POLYGON = "polygon"
    FINGERPRINT = "fingerprint"


class TrackVisibility(str, Enum):
    VISIBLE = "visible"
    OCCLUDED = "occluded"
    RE_ENTERED = "re_entered"


class ReIDResolution(str, Enum):
    RESOLVED = "resolved"
    USER_SELECTED = "user_selected"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    CONFLICTING = "conflicting"


class TrackingCaseKind(str, Enum):
    CLEAN_MULTI_OBJECT = "clean_multi_object"
    PARTIAL_OCCLUSION = "partial_occlusion"
    RE_ENTRY = "re_entry"
    SHOT_CUT = "shot_cut"
    DUPLICATE_INSTANCES = "duplicate_instances"
    LOOKALIKE_AMBIGUITY = "lookalike_ambiguity"
    MULTI_REFERENCE_AMBIGUITY = "multi_reference_ambiguity"
    MASK_TRACK_DISAGREEMENT = "mask_track_disagreement"
    CORRUPT_UNSUPPORTED = "corrupt_unsupported"


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise PerceptionTrackingError(f"{field} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise PerceptionTrackingError(f"{field} contains sensitive or locator material")
    return value


def _code(value: object, field: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value.casefold()) is None:
        raise PerceptionTrackingError(f"{field} must be a lower-case bounded code")
    return value.casefold()


def _text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise PerceptionTrackingError(f"{field} must be bounded non-empty text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise PerceptionTrackingError(f"{field} contains a control character")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise PerceptionTrackingError(f"{field} contains sensitive or locator material")
    return value


def _fingerprint(value: object, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise PerceptionTrackingError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _version(value: object, field: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise PerceptionTrackingError(f"{field} must be a numeric version")
    return value


def _confidence(value: object, field: str = "confidence") -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or not Decimal("0") <= value <= 1:
        raise PerceptionTrackingError(f"{field} must be a finite Decimal between 0 and 1")
    return value


def _pts(value: object, field: str) -> SourcePTS:
    if not isinstance(value, SourcePTS):
        raise PerceptionTrackingError(f"{field} must be SourcePTS")
    return value


def _ids(values: object, field: str, maximum: int, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise PerceptionTrackingError(f"{field} is outside the finite limit")
    if required and not values:
        raise PerceptionTrackingError(f"{field} must not be empty")
    result = tuple(_identifier(value, f"{field} item") for value in values)
    if len(result) != len(set(result)):
        raise PerceptionTrackingError(f"{field} must not contain duplicates")
    return result


def _uncertainties(values: object, field: str) -> tuple[Uncertainty, ...]:
    if (
        not isinstance(values, tuple)
        or len(values) > 8
        or not all(isinstance(value, Uncertainty) for value in values)
    ):
        raise PerceptionTrackingError(f"{field} must contain bounded Uncertainty values")
    return values


def _positive_int(value: object, field: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PerceptionTrackingError(f"{field} must be a positive integer")
    if maximum is not None and value > maximum:
        raise PerceptionTrackingError(f"{field} exceeds the finite limit")
    return value


def _decimal(value: object, field: str, *, maximum: Decimal | None = None) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise PerceptionTrackingError(f"{field} must be a non-negative finite Decimal")
    if maximum is not None and value > maximum:
        raise PerceptionTrackingError(f"{field} exceeds the finite limit")
    return value


@dataclass(frozen=True, slots=True)
class NormalizedPoint:
    x: Decimal
    y: Decimal

    def __post_init__(self) -> None:
        for value, field in ((self.x, "point x"), (self.y, "point y")):
            if not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 1:
                raise PerceptionTrackingError(f"{field} must be a Decimal between 0 and 1")

    def to_wire(self) -> dict[str, str]:
        return {"x": format(self.x, "f"), "y": format(self.y, "f")}


@dataclass(frozen=True, slots=True)
class NormalizedRegion:
    x: Decimal
    y: Decimal
    width: Decimal
    height: Decimal
    polygon: tuple[NormalizedPoint, ...] = ()

    def __post_init__(self) -> None:
        for value, field in ((self.x, "region x"), (self.y, "region y")):
            if not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 1:
                raise PerceptionTrackingError(f"{field} must be a Decimal between 0 and 1")
        for value, field in ((self.width, "region width"), (self.height, "region height")):
            if not isinstance(value, Decimal) or not value.is_finite() or not 0 < value <= 1:
                raise PerceptionTrackingError(
                    f"{field} must be a positive Decimal no greater than 1"
                )
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise PerceptionTrackingError("region must remain inside normalized bounds")
        if (
            not isinstance(self.polygon, tuple)
            or len(self.polygon) > 32
            or not all(isinstance(point, NormalizedPoint) for point in self.polygon)
        ):
            raise PerceptionTrackingError("region polygon is outside the finite limit")
        if self.polygon and len(self.polygon) < 3:
            raise PerceptionTrackingError("region polygon requires at least three points")

    def to_wire(self) -> dict[str, object]:
        return {
            "x": format(self.x, "f"),
            "y": format(self.y, "f"),
            "width": format(self.width, "f"),
            "height": format(self.height, "f"),
            "polygon": [point.to_wire() for point in self.polygon],
        }


@dataclass(frozen=True, slots=True)
class Detection:
    detection_id: str
    asset_id: str
    source_id: str
    frame_id: str
    shot_id: str
    source_pts: SourcePTS
    label: str
    region: NormalizedRegion
    confidence: Decimal | None = None
    mask_id: str | None = None
    uncertainties: tuple[Uncertainty, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.detection_id, "detection_id")
        _identifier(self.asset_id, "detection asset_id")
        _identifier(self.source_id, "detection source_id")
        _identifier(self.frame_id, "detection frame_id")
        _identifier(self.shot_id, "detection shot_id")
        _pts(self.source_pts, "detection source_pts")
        _text(self.label, "detection label")
        if not isinstance(self.region, NormalizedRegion):
            raise PerceptionTrackingError("detection region must be NormalizedRegion")
        _confidence(self.confidence, "detection confidence")
        if self.mask_id is not None:
            _identifier(self.mask_id, "detection mask_id")
        object.__setattr__(
            self, "uncertainties", _uncertainties(self.uncertainties, "detection uncertainties")
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "detection_id": self.detection_id,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "frame_id": self.frame_id,
            "shot_id": self.shot_id,
            "source_pts": self.source_pts.to_wire(),
            "label": self.label,
            "region": self.region.to_wire(),
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "mask_id": self.mask_id,
            "uncertainties": [item.to_wire() for item in self.uncertainties],
        }


@dataclass(frozen=True, slots=True)
class SegmentationMask:
    mask_id: str
    asset_id: str
    source_id: str
    frame_id: str
    shot_id: str
    detection_id: str
    source_pts: SourcePTS
    width: int
    height: int
    representation: MaskRepresentation | str
    polygon: tuple[NormalizedPoint, ...] = ()
    mask_fingerprint: str | None = None
    uncertainties: tuple[Uncertainty, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.mask_id, "mask_id")
        _identifier(self.asset_id, "mask asset_id")
        _identifier(self.source_id, "mask source_id")
        _identifier(self.frame_id, "mask frame_id")
        _identifier(self.shot_id, "mask shot_id")
        _identifier(self.detection_id, "mask detection_id")
        _pts(self.source_pts, "mask source_pts")
        _positive_int(self.width, "mask width")
        _positive_int(self.height, "mask height")
        try:
            representation = (
                self.representation
                if isinstance(self.representation, MaskRepresentation)
                else MaskRepresentation(self.representation)
            )
        except (TypeError, ValueError):
            raise PerceptionTrackingError("mask representation is unsupported") from None
        object.__setattr__(self, "representation", representation)
        if (
            not isinstance(self.polygon, tuple)
            or len(self.polygon) > 64
            or not all(isinstance(point, NormalizedPoint) for point in self.polygon)
        ):
            raise PerceptionTrackingError("mask polygon is outside the finite limit")
        if representation is MaskRepresentation.POLYGON:
            if len(self.polygon) < 3 or self.mask_fingerprint is not None:
                raise PerceptionTrackingError("polygon mask requires only a bounded polygon")
        elif not self.mask_fingerprint or self.polygon:
            raise PerceptionTrackingError("fingerprint mask requires a fingerprint and no polygon")
        if self.mask_fingerprint is not None:
            _fingerprint(self.mask_fingerprint, "mask_fingerprint")
        object.__setattr__(
            self, "uncertainties", _uncertainties(self.uncertainties, "mask uncertainties")
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "mask_id": self.mask_id,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "frame_id": self.frame_id,
            "shot_id": self.shot_id,
            "detection_id": self.detection_id,
            "source_pts": self.source_pts.to_wire(),
            "width": self.width,
            "height": self.height,
            "representation": cast(MaskRepresentation, self.representation).value,
            "polygon": [point.to_wire() for point in self.polygon],
            "mask_fingerprint": self.mask_fingerprint,
            "uncertainties": [item.to_wire() for item in self.uncertainties],
        }


@dataclass(frozen=True, slots=True)
class TrackLink:
    detection_id: str
    frame_id: str
    shot_id: str
    source_pts: SourcePTS
    visibility: TrackVisibility | str = TrackVisibility.VISIBLE

    def __post_init__(self) -> None:
        _identifier(self.detection_id, "track link detection_id")
        _identifier(self.frame_id, "track link frame_id")
        _identifier(self.shot_id, "track link shot_id")
        _pts(self.source_pts, "track link source_pts")
        try:
            visibility = (
                self.visibility
                if isinstance(self.visibility, TrackVisibility)
                else TrackVisibility(self.visibility)
            )
        except (TypeError, ValueError):
            raise PerceptionTrackingError("track visibility is unsupported") from None
        object.__setattr__(self, "visibility", visibility)

    def to_wire(self) -> dict[str, object]:
        return {
            "detection_id": self.detection_id,
            "frame_id": self.frame_id,
            "shot_id": self.shot_id,
            "source_pts": self.source_pts.to_wire(),
            "visibility": cast(TrackVisibility, self.visibility).value,
        }


@dataclass(frozen=True, slots=True)
class TrackingTrack:
    track_id: str
    asset_id: str
    source_id: str
    links: tuple[TrackLink, ...]
    shot_ids: tuple[str, ...]
    label: str
    confidence: Decimal | None = None
    identity_reference_ids: tuple[str, ...] = ()
    uncertainties: tuple[Uncertainty, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.track_id, "track_id")
        _identifier(self.asset_id, "track asset_id")
        _identifier(self.source_id, "track source_id")
        if not isinstance(self.links, tuple) or not 1 <= len(self.links) <= MAX_TRACKING_LINKS:
            raise PerceptionTrackingError("track links are outside the finite limit")
        if not all(isinstance(item, TrackLink) for item in self.links):
            raise PerceptionTrackingError("track links are invalid")
        frame_ids = [item.frame_id for item in self.links]
        detection_ids = [item.detection_id for item in self.links]
        if len(frame_ids) != len(set(frame_ids)) or len(detection_ids) != len(set(detection_ids)):
            raise PerceptionTrackingError("track links must not repeat frames or detections")
        for previous, current in zip(self.links, self.links[1:], strict=False):
            if current.source_pts.timestamp.seconds < previous.source_pts.timestamp.seconds:
                raise PerceptionTrackingError("track links must be monotonic by source PTS")
        _ids(self.shot_ids, "track shot_ids", 32, required=True)
        if tuple(dict.fromkeys(item.shot_id for item in self.links)) != self.shot_ids:
            raise PerceptionTrackingError("track shot_ids must match link order")
        _text(self.label, "track label")
        _confidence(self.confidence, "track confidence")
        _ids(self.identity_reference_ids, "track identity_reference_ids", MAX_TRACKING_REFERENCES)
        object.__setattr__(
            self, "uncertainties", _uncertainties(self.uncertainties, "track uncertainties")
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "track_id": self.track_id,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "links": [item.to_wire() for item in self.links],
            "shot_ids": list(self.shot_ids),
            "label": self.label,
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "identity_reference_ids": list(self.identity_reference_ids),
            "uncertainties": [item.to_wire() for item in self.uncertainties],
        }


@dataclass(frozen=True, slots=True)
class IdentityEmbedding:
    embedding_id: str
    asset_id: str
    source_id: str
    track_id: str
    adapter_id: str
    adapter_version: str
    model_id: str
    model_digest: str
    dimension: int
    embedding_fingerprint: str
    policy: IdentityPolicy

    def __post_init__(self) -> None:
        _identifier(self.embedding_id, "embedding_id")
        _identifier(self.asset_id, "embedding asset_id")
        _identifier(self.source_id, "embedding source_id")
        _identifier(self.track_id, "embedding track_id")
        _code(self.adapter_id, "embedding adapter_id")
        _version(self.adapter_version, "embedding adapter_version")
        _identifier(self.model_id, "embedding model_id")
        _fingerprint(self.model_digest, "embedding model_digest")
        _positive_int(self.dimension, "embedding dimension", 4096)
        _fingerprint(self.embedding_fingerprint, "embedding_fingerprint")
        if self.policy is not IdentityPolicy.LOCAL_OPT_IN:
            raise PerceptionTrackingError("identity embeddings require local_opt_in policy")

    def to_wire(self) -> dict[str, object]:
        return {
            "embedding_id": self.embedding_id,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "track_id": self.track_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "dimension": self.dimension,
            "embedding_fingerprint": self.embedding_fingerprint,
            "policy": self.policy.value,
        }


@dataclass(frozen=True, slots=True)
class ReIDCandidate:
    candidate_id: str
    track_id: str
    reference_ids: tuple[str, ...]
    score: Decimal
    resolution: ReIDResolution | str
    embedding_id: str | None = None
    uncertainties: tuple[Uncertainty, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "re-ID candidate_id")
        _identifier(self.track_id, "re-ID track_id")
        _ids(self.reference_ids, "re-ID reference_ids", MAX_TRACKING_REFERENCES, required=True)
        _decimal(self.score, "re-ID score", maximum=Decimal("1"))
        try:
            resolution = (
                self.resolution
                if isinstance(self.resolution, ReIDResolution)
                else ReIDResolution(self.resolution)
            )
        except (TypeError, ValueError):
            raise PerceptionTrackingError("re-ID resolution is unsupported") from None
        object.__setattr__(self, "resolution", resolution)
        if resolution is ReIDResolution.AMBIGUOUS and len(self.reference_ids) < 2:
            raise PerceptionTrackingError("ambiguous re-ID requires multiple references")
        if resolution is ReIDResolution.RESOLVED and len(self.reference_ids) != 1:
            raise PerceptionTrackingError("resolved re-ID requires exactly one reference")
        if self.embedding_id is not None:
            _identifier(self.embedding_id, "re-ID embedding_id")
        object.__setattr__(
            self, "uncertainties", _uncertainties(self.uncertainties, "re-ID uncertainties")
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "track_id": self.track_id,
            "reference_ids": list(self.reference_ids),
            "score": format(self.score, "f"),
            "resolution": cast(ReIDResolution, self.resolution).value,
            "embedding_id": self.embedding_id,
            "uncertainties": [item.to_wire() for item in self.uncertainties],
        }


@dataclass(frozen=True, slots=True)
class TrackingReceipt:
    adapter_id: str
    adapter_version: str
    route: PerceptionRoute | str
    model_id: str
    model_digest: str
    output_fingerprint: str
    source_fingerprints: tuple[str, ...]
    identity_policy: IdentityPolicy

    def __post_init__(self) -> None:
        _code(self.adapter_id, "tracking receipt adapter_id")
        _version(self.adapter_version, "tracking receipt adapter_version")
        try:
            route = (
                self.route
                if isinstance(self.route, PerceptionRoute)
                else PerceptionRoute(self.route)
            )
        except (TypeError, ValueError):
            raise PerceptionTrackingError("tracking receipt route is unsupported") from None
        object.__setattr__(self, "route", route)
        _identifier(self.model_id, "tracking receipt model_id")
        _fingerprint(self.model_digest, "tracking receipt model_digest")
        _fingerprint(self.output_fingerprint, "tracking receipt output_fingerprint")
        if (
            not isinstance(self.source_fingerprints, tuple)
            or not self.source_fingerprints
            or len(self.source_fingerprints) > MAX_TRACKING_ASSETS
        ):
            raise PerceptionTrackingError("tracking receipt source fingerprints are invalid")
        for fingerprint in self.source_fingerprints:
            _fingerprint(fingerprint, "tracking receipt source fingerprint")
        if not isinstance(self.identity_policy, IdentityPolicy):
            raise PerceptionTrackingError("tracking receipt identity policy is invalid")

    def to_wire(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "route": cast(PerceptionRoute, self.route).value,
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "output_fingerprint": self.output_fingerprint,
            "source_fingerprints": list(self.source_fingerprints),
            "identity_policy": self.identity_policy.value,
        }


@dataclass(frozen=True, slots=True)
class TrackingRequest:
    decode_document: VideoDecodeDocument
    route: PerceptionRoute | str = PerceptionRoute.COMFYUI_NATIVE
    adapter_id: str = "injected_tracking"
    identity_policy: IdentityPolicy = IdentityPolicy.DISABLED
    reference_ids: tuple[str, ...] = ("ref_a", "ref_b")
    max_detections: int = MAX_TRACKING_DETECTIONS
    max_masks: int = MAX_TRACKING_MASKS
    max_tracks: int = MAX_TRACKING_TRACKS
    max_candidates: int = MAX_TRACKING_CANDIDATES

    def __post_init__(self) -> None:
        if not isinstance(self.decode_document, VideoDecodeDocument):
            raise PerceptionTrackingError("decode_document must be VideoDecodeDocument")
        try:
            route = (
                self.route
                if isinstance(self.route, PerceptionRoute)
                else PerceptionRoute(self.route)
            )
        except (TypeError, ValueError):
            raise PerceptionTrackingError("tracking route is unsupported") from None
        object.__setattr__(self, "route", route)
        _code(self.adapter_id, "tracking adapter_id")
        if not isinstance(self.identity_policy, IdentityPolicy):
            raise PerceptionTrackingError("identity_policy is invalid")
        _ids(self.reference_ids, "tracking reference_ids", MAX_TRACKING_REFERENCES, required=True)
        for value, field, maximum in (
            (self.max_detections, "max_detections", MAX_TRACKING_DETECTIONS),
            (self.max_masks, "max_masks", MAX_TRACKING_MASKS),
            (self.max_tracks, "max_tracks", MAX_TRACKING_TRACKS),
            (self.max_candidates, "max_candidates", MAX_TRACKING_CANDIDATES),
        ):
            _positive_int(value, field, maximum)

    @property
    def selected_asset_ids(self) -> tuple[str, ...]:
        return self.decode_document.selected_asset_ids

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": PERCEPTION_TRACKING_SCHEMA,
            "route": cast(PerceptionRoute, self.route).value,
            "adapter_id": self.adapter_id,
            "identity_policy": self.identity_policy.value,
            "selected_asset_ids": list(self.selected_asset_ids),
            "reference_ids": list(self.reference_ids),
            "max_detections": self.max_detections,
            "max_masks": self.max_masks,
            "max_tracks": self.max_tracks,
            "max_candidates": self.max_candidates,
        }


def _frame_index(document: VideoDecodeDocument) -> dict[str, DecodedFrame]:
    return {frame.frame_id: frame for frame in document.frames}


def _shot_index(document: VideoDecodeDocument) -> dict[str, object]:
    return {shot.shot_id: shot for shot in document.shots}


@dataclass(frozen=True, slots=True)
class TrackingDocument:
    document_id: str
    schema: str
    status: TrackingStatus
    decode_document: VideoDecodeDocument
    route: PerceptionRoute | str
    identity_policy: IdentityPolicy
    detections: tuple[Detection, ...] = ()
    masks: tuple[SegmentationMask, ...] = ()
    tracks: tuple[TrackingTrack, ...] = ()
    embeddings: tuple[IdentityEmbedding, ...] = ()
    reid_candidates: tuple[ReIDCandidate, ...] = ()
    receipt: TrackingReceipt | None = None
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.document_id, "tracking document_id")
        if self.schema != PERCEPTION_TRACKING_SCHEMA:
            raise PerceptionTrackingError("unsupported perception-tracking schema")
        if not isinstance(self.status, TrackingStatus):
            raise PerceptionTrackingError("tracking status is invalid")
        if not isinstance(self.decode_document, VideoDecodeDocument):
            raise PerceptionTrackingError("tracking decode_document is invalid")
        try:
            route = (
                self.route
                if isinstance(self.route, PerceptionRoute)
                else PerceptionRoute(self.route)
            )
        except (TypeError, ValueError):
            raise PerceptionTrackingError("tracking document route is unsupported") from None
        object.__setattr__(self, "route", route)
        if not isinstance(self.identity_policy, IdentityPolicy):
            raise PerceptionTrackingError("tracking document identity policy is invalid")
        for values, field, maximum, expected, identifier_field in (
            (self.detections, "detections", MAX_TRACKING_DETECTIONS, Detection, "detection_id"),
            (self.masks, "masks", MAX_TRACKING_MASKS, SegmentationMask, "mask_id"),
            (self.tracks, "tracks", MAX_TRACKING_TRACKS, TrackingTrack, "track_id"),
            (
                self.embeddings,
                "embeddings",
                MAX_TRACKING_EMBEDDINGS,
                IdentityEmbedding,
                "embedding_id",
            ),
            (
                self.reid_candidates,
                "reid_candidates",
                MAX_TRACKING_CANDIDATES,
                ReIDCandidate,
                "candidate_id",
            ),
        ):
            if (
                not isinstance(values, tuple)
                or len(values) > maximum
                or not all(isinstance(item, expected) for item in values)
            ):
                raise PerceptionTrackingError(f"{field} is outside the finite limit")
            identifiers = [getattr(item, identifier_field) for item in values]
            if len(identifiers) != len(set(identifiers)):
                raise PerceptionTrackingError(f"{field} IDs must be unique")
        if self.receipt is not None and not isinstance(self.receipt, TrackingReceipt):
            raise PerceptionTrackingError("tracking receipt is invalid")
        if (
            not isinstance(self.diagnostics, tuple)
            or len(self.diagnostics) > 16
            or not all(isinstance(item, str) and item for item in self.diagnostics)
        ):
            raise PerceptionTrackingError("tracking diagnostics are outside the finite limit")

        frames = _frame_index(self.decode_document)
        shots = _shot_index(self.decode_document)
        detection_map = {item.detection_id: item for item in self.detections}
        mask_map = {item.mask_id: item for item in self.masks}
        track_map = {item.track_id: item for item in self.tracks}
        embedding_map = {item.embedding_id: item for item in self.embeddings}
        for detection in self.detections:
            frame = frames.get(detection.frame_id)
            shot = shots.get(detection.shot_id)
            if frame is None or shot is None:
                raise PerceptionTrackingError("detection references an unknown frame or shot")
            if (
                detection.asset_id != frame.asset_id
                or detection.source_id != frame.source_id
                or detection.source_pts != frame.source_pts
                or detection.asset_id not in self.decode_document.selected_asset_ids
            ):
                raise PerceptionTrackingError("detection source ownership does not match frame")
            if getattr(
                shot, "asset_id", None
            ) != detection.asset_id or detection.frame_id not in getattr(shot, "frame_ids", ()):
                raise PerceptionTrackingError("detection frame is not owned by its shot")
            if detection.mask_id is not None and detection.mask_id not in mask_map:
                raise PerceptionTrackingError("detection references an unknown mask")
        for mask in self.masks:
            frame = frames.get(mask.frame_id)
            mask_detection = detection_map.get(mask.detection_id)
            if frame is None or mask_detection is None:
                raise PerceptionTrackingError("mask references an unknown frame or detection")
            if (
                mask.asset_id != frame.asset_id
                or mask.source_id != frame.source_id
                or mask.source_pts != frame.source_pts
                or mask.detection_id != mask_detection.detection_id
                or mask.frame_id != mask_detection.frame_id
                or mask.shot_id != mask_detection.shot_id
            ):
                raise PerceptionTrackingError("mask source ownership does not match detection")
            if mask_detection.mask_id != mask.mask_id:
                raise PerceptionTrackingError("mask must be declared by its detection")
        for track in self.tracks:
            if track.asset_id not in self.decode_document.selected_asset_ids:
                raise PerceptionTrackingError("track asset is not selected")
            for link in track.links:
                link_detection = detection_map.get(link.detection_id)
                frame = frames.get(link.frame_id)
                if link_detection is None or frame is None:
                    raise PerceptionTrackingError(
                        "track link references an unknown detection/frame"
                    )
                if (
                    track.asset_id != link_detection.asset_id
                    or track.source_id != link_detection.source_id
                    or link.frame_id != link_detection.frame_id
                    or link.shot_id != link_detection.shot_id
                    or link.source_pts != link_detection.source_pts
                    or frame.source_pts != link.source_pts
                ):
                    raise PerceptionTrackingError(
                        "track link source ownership does not match detection"
                    )
        for embedding in self.embeddings:
            linked_track = track_map.get(embedding.track_id)
            if (
                linked_track is None
                or embedding.asset_id != linked_track.asset_id
                or embedding.source_id != linked_track.source_id
            ):
                raise PerceptionTrackingError("embedding source ownership does not match track")
            if (
                self.identity_policy is not IdentityPolicy.LOCAL_OPT_IN
                or embedding.policy is not IdentityPolicy.LOCAL_OPT_IN
            ):
                raise PerceptionTrackingError(
                    "identity embedding is not covered by explicit local opt-in"
                )
        for candidate in self.reid_candidates:
            candidate_track = track_map.get(candidate.track_id)
            if candidate_track is None:
                raise PerceptionTrackingError("re-ID candidate references an unknown track")
            if any(
                reference_id not in candidate_track.identity_reference_ids
                for reference_id in candidate.reference_ids
            ):
                raise PerceptionTrackingError("re-ID candidate references an unadmitted reference")
            if candidate.embedding_id is not None:
                if candidate.embedding_id not in embedding_map:
                    raise PerceptionTrackingError("re-ID candidate references an unknown embedding")
                if self.identity_policy is not IdentityPolicy.LOCAL_OPT_IN:
                    raise PerceptionTrackingError("re-ID embedding requires explicit local opt-in")
        if self.receipt is not None:
            if (
                self.receipt.route is not self.route
                or self.receipt.identity_policy is not self.identity_policy
            ):
                raise PerceptionTrackingError("tracking receipt route/policy differs from document")
            decode_receipt = self.decode_document.receipt
            if (
                decode_receipt is not None
                and decode_receipt.source_fingerprint not in self.receipt.source_fingerprints
            ):
                raise PerceptionTrackingError("tracking receipt does not cover decoded source")
        if self.status is TrackingStatus.COMPLETE:
            if (
                not self.decode_document.complete
                or not self.detections
                or not self.tracks
                or self.receipt is None
            ):
                raise PerceptionTrackingError(
                    "complete tracking requires decoded source, detections, tracks, and receipt"
                )
        elif self.status in {
            TrackingStatus.EMPTY,
            TrackingStatus.CORRUPT,
            TrackingStatus.UNSUPPORTED,
            TrackingStatus.CANCELLED,
        } and (
            self.detections
            or self.masks
            or self.tracks
            or self.embeddings
            or self.reid_candidates
            or self.receipt is not None
        ):
            raise PerceptionTrackingError("terminal tracking abstention cannot contain output")
        if len(repr(self.to_wire()).encode("utf-8")) > MAX_TRACKING_OUTPUT_BYTES:
            raise PerceptionTrackingError("tracking document exceeds portable output budget")

    @property
    def complete(self) -> bool:
        return self.status is TrackingStatus.COMPLETE

    @property
    def route_value(self) -> PerceptionRoute:
        return cast(PerceptionRoute, self.route)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "document_id": self.document_id,
            "status": self.status.value,
            "complete": self.complete,
            "route": self.route_value.value,
            "identity_policy": self.identity_policy.value,
            "decode_document_id": self.decode_document.document_id,
            "selected_asset_ids": list(self.decode_document.selected_asset_ids),
            "detections": [item.to_wire() for item in self.detections],
            "masks": [item.to_wire() for item in self.masks],
            "tracks": [item.to_wire() for item in self.tracks],
            "embeddings": [item.to_wire() for item in self.embeddings],
            "reid_candidates": [item.to_wire() for item in self.reid_candidates],
            "receipt": None if self.receipt is None else self.receipt.to_wire(),
            "diagnostics": list(self.diagnostics),
        }

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "document_id": self.document_id,
            "status": self.status.value,
            "complete": self.complete,
            "route": self.route_value.value,
            "identity_policy": self.identity_policy.value,
            "decode_document_id": self.decode_document.document_id,
            "selected_asset_ids": list(self.decode_document.selected_asset_ids),
            "detection_count": len(self.detections),
            "mask_count": len(self.masks),
            "track_count": len(self.tracks),
            "embedding_count": len(self.embeddings),
            "reid_candidate_count": len(self.reid_candidates),
            "receipt": None if self.receipt is None else self.receipt.to_wire(),
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class TrackingCorpusCase:
    case_id: str
    kind: TrackingCaseKind
    source_fingerprint: str
    expected_detections: int
    expected_tracks: int
    should_abstain: bool = False

    def __post_init__(self) -> None:
        _identifier(self.case_id, "tracking case_id")
        if not isinstance(self.kind, TrackingCaseKind):
            raise PerceptionTrackingError("tracking case kind is unsupported")
        _fingerprint(self.source_fingerprint, "tracking case source_fingerprint")
        if (
            isinstance(self.expected_detections, bool)
            or not isinstance(self.expected_detections, int)
            or not 0 <= self.expected_detections <= MAX_TRACKING_DETECTIONS
        ):
            raise PerceptionTrackingError("expected_detections is outside the finite limit")
        if (
            isinstance(self.expected_tracks, bool)
            or not isinstance(self.expected_tracks, int)
            or not 0 <= self.expected_tracks <= MAX_TRACKING_TRACKS
        ):
            raise PerceptionTrackingError("expected_tracks is outside the finite limit")
        if not isinstance(self.should_abstain, bool):
            raise PerceptionTrackingError("should_abstain must be boolean")

    def to_wire(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "kind": self.kind.value,
            "source_fingerprint": self.source_fingerprint,
            "expected_detections": self.expected_detections,
            "expected_tracks": self.expected_tracks,
            "should_abstain": self.should_abstain,
        }


@dataclass(frozen=True, slots=True)
class TrackingMetricThreshold:
    metric: str
    minimum: Decimal | None = None
    maximum: Decimal | None = None

    def __post_init__(self) -> None:
        _identifier(self.metric, "tracking metric")
        if (self.minimum is None) == (self.maximum is None):
            raise PerceptionTrackingError("exactly one tracking metric bound is required")
        value = self.minimum if self.minimum is not None else self.maximum
        if value is None or not value.is_finite() or not 0 <= value <= 1:
            raise PerceptionTrackingError("tracking metric threshold must be between 0 and 1")

    def to_wire(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "minimum": None if self.minimum is None else format(self.minimum, "f"),
            "maximum": None if self.maximum is None else format(self.maximum, "f"),
        }


@dataclass(frozen=True, slots=True)
class TrackingBenchmarkPlan:
    cases: tuple[TrackingCorpusCase, ...]
    thresholds: tuple[TrackingMetricThreshold, ...]
    schema: str = TRACKING_BENCHMARK_SCHEMA
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.cases, tuple)
            or not self.cases
            or len(self.cases) > MAX_TRACKING_CASES
        ):
            raise PerceptionTrackingError("tracking cases are outside the finite limit")
        if not all(isinstance(item, TrackingCorpusCase) for item in self.cases):
            raise PerceptionTrackingError("tracking cases are invalid")
        if len({item.case_id for item in self.cases}) != len(self.cases):
            raise PerceptionTrackingError("tracking case IDs must be unique")
        if (
            not isinstance(self.thresholds, tuple)
            or not self.thresholds
            or len(self.thresholds) > MAX_TRACKING_METRICS
        ):
            raise PerceptionTrackingError("tracking thresholds are outside the finite limit")
        if not all(isinstance(item, TrackingMetricThreshold) for item in self.thresholds):
            raise PerceptionTrackingError("tracking thresholds are invalid")
        if len({item.metric for item in self.thresholds}) != len(self.thresholds):
            raise PerceptionTrackingError("tracking metrics must be unique")
        expected = canonical_fingerprint(
            {
                "schema": self.schema,
                "cases": [item.to_wire() for item in self.cases],
                "thresholds": [item.to_wire() for item in self.thresholds],
            }
        )
        if self.fingerprint and self.fingerprint != expected:
            raise PerceptionTrackingError("tracking benchmark fingerprint does not match contents")
        object.__setattr__(self, "fingerprint", expected)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "fingerprint": self.fingerprint,
            "cases": [item.to_wire() for item in self.cases],
            "thresholds": [item.to_wire() for item in self.thresholds],
        }


def build_default_tracking_benchmark_plan() -> TrackingBenchmarkPlan:
    def case(
        case_id: str, kind: TrackingCaseKind, detections: int, tracks: int, abstain: bool = False
    ) -> TrackingCorpusCase:
        return TrackingCorpusCase(
            case_id,
            kind,
            canonical_fingerprint({"case_id": case_id}),
            detections,
            tracks,
            abstain,
        )

    cases = (
        case("tracking.clean_multi_object", TrackingCaseKind.CLEAN_MULTI_OBJECT, 4, 2),
        case("tracking.partial_occlusion", TrackingCaseKind.PARTIAL_OCCLUSION, 3, 1),
        case("tracking.re_entry", TrackingCaseKind.RE_ENTRY, 3, 1),
        case("tracking.shot_cut", TrackingCaseKind.SHOT_CUT, 3, 1),
        case("tracking.duplicate_instances", TrackingCaseKind.DUPLICATE_INSTANCES, 4, 2),
        case("tracking.lookalike_ambiguity", TrackingCaseKind.LOOKALIKE_AMBIGUITY, 3, 2),
        case(
            "tracking.multi_reference_ambiguity", TrackingCaseKind.MULTI_REFERENCE_AMBIGUITY, 2, 1
        ),
        case("tracking.mask_track_disagreement", TrackingCaseKind.MASK_TRACK_DISAGREEMENT, 2, 1),
        case("tracking.corrupt_unsupported", TrackingCaseKind.CORRUPT_UNSUPPORTED, 0, 0, True),
    )
    thresholds = (
        TrackingMetricThreshold("detection_precision", minimum=Decimal("0.90")),
        TrackingMetricThreshold("detection_recall", minimum=Decimal("0.90")),
        TrackingMetricThreshold("mask_iou", minimum=Decimal("0.80")),
        TrackingMetricThreshold("track_association_accuracy", minimum=Decimal("0.90")),
        TrackingMetricThreshold("reid_recall", minimum=Decimal("0.90")),
        TrackingMetricThreshold("false_merge_rate", maximum=Decimal("0.05")),
    )
    return TrackingBenchmarkPlan(cases=cases, thresholds=thresholds)


@runtime_checkable
class PerceptionTrackingAdapter(Protocol):
    @property
    def descriptor(self) -> LocalAdapterDescriptor: ...

    def track(self, request: TrackingRequest, guard: LocalBudgetGuard) -> TrackingDocument: ...


class _TrackingBridge:
    def __init__(self, adapter: PerceptionTrackingAdapter) -> None:
        self._adapter = adapter

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._adapter.descriptor

    def run(
        self, request: LocalAdapterExecutionRequest, guard: LocalBudgetGuard
    ) -> LocalAdapterResult:
        if not isinstance(request.input_value, TrackingRequest):
            raise PerceptionTrackingError("tracking adapter input is not TrackingRequest")
        document = self._adapter.track(request.input_value, guard)
        if not isinstance(document, TrackingDocument):
            raise PerceptionTrackingError("tracking adapter returned an invalid document")
        return LocalAdapterResult(
            adapter_id=self.descriptor.adapter_id,
            adapter_version=self.descriptor.adapter_version,
            device=request.device,
            value=document,
            output_bytes=len(repr(document.to_wire()).encode("utf-8")),
            output_items=len(document.detections) + len(document.tracks),
        )


def _validate_document(document: TrackingDocument, request: TrackingRequest) -> None:
    if document.decode_document.document_id != request.decode_document.document_id:
        raise PerceptionTrackingError(
            "tracking document does not use the requested decode document"
        )
    if (
        document.route is not request.route
        or document.identity_policy is not request.identity_policy
    ):
        raise PerceptionTrackingError("tracking document route/policy does not match request")
    if len(document.detections) > request.max_detections:
        raise PerceptionTrackingError("tracking document exceeds detection limit")
    if len(document.masks) > request.max_masks:
        raise PerceptionTrackingError("tracking document exceeds mask limit")
    if len(document.tracks) > request.max_tracks:
        raise PerceptionTrackingError("tracking document exceeds track limit")
    if len(document.reid_candidates) > request.max_candidates:
        raise PerceptionTrackingError("tracking document exceeds re-ID candidate limit")
    reference_ids = set(request.reference_ids)
    for candidate in document.reid_candidates:
        if not set(candidate.reference_ids).issubset(reference_ids):
            raise PerceptionTrackingError("re-ID candidate reference is not admitted by request")


def execute_perception_tracking(
    adapter: PerceptionTrackingAdapter,
    request: TrackingRequest,
    *,
    runtime: LocalAdapterRuntime | None = None,
    device: LocalDeviceSpec | None = None,
    cancellation_probe: LocalCancellationProbe | None = None,
    clock: Callable[[], float] | None = None,
    memory_meter: Callable[[], int] | None = None,
) -> TrackingDocument:
    """Execute one explicitly selected tracking adapter through the bounded local seam."""

    if not isinstance(adapter, PerceptionTrackingAdapter):
        raise PerceptionTrackingError("adapter must implement PerceptionTrackingAdapter")
    if not isinstance(request, TrackingRequest):
        raise PerceptionTrackingError("request must be TrackingRequest")
    device_value = LocalDeviceSpec(LocalDeviceKind.AUTO) if device is None else device
    if not isinstance(device_value, LocalDeviceSpec):
        raise PerceptionTrackingError("device must be LocalDeviceSpec")
    execution_request = LocalAdapterExecutionRequest(
        adapter_id=adapter.descriptor.adapter_id,
        task_mode=TaskMode.REF2VA,
        media_kinds=(MediaKind.VIDEO,),
        reference_count=len(request.selected_asset_ids) + len(request.reference_ids),
        device=device_value,
        estimated_memory_bytes=1,
        estimated_output_bytes=adapter.descriptor.limits.max_output_bytes,
        deterministic_required=True,
        seed=0,
        cancellation_required=cancellation_probe is not None,
        input_value=request,
    )
    bridge = _TrackingBridge(adapter)
    if clock is not None:
        result = run_local_adapter(
            bridge,
            execution_request,
            runtime=runtime,
            cancellation_probe=cancellation_probe,
            clock=clock,
            memory_meter=memory_meter,
        )
    else:
        result = run_local_adapter(
            bridge,
            execution_request,
            runtime=runtime,
            cancellation_probe=cancellation_probe,
            memory_meter=memory_meter,
        )
    if not isinstance(result.value, TrackingDocument):
        raise PerceptionTrackingError("tracking result did not contain a document")
    _validate_document(result.value, request)
    return result.value


def build_tracking_abstention(
    request: TrackingRequest, status: TrackingStatus, diagnostic: str
) -> TrackingDocument:
    if not isinstance(request, TrackingRequest):
        raise PerceptionTrackingError("request must be TrackingRequest")
    if status is TrackingStatus.COMPLETE:
        raise PerceptionTrackingError("complete status requires tracking output")
    if not isinstance(diagnostic, str) or not diagnostic:
        raise PerceptionTrackingError("tracking diagnostic must be non-empty")
    return TrackingDocument(
        document_id=canonical_fingerprint(
            {"decode_document": request.decode_document.document_id, "status": status.value}
        )[7:39],
        schema=PERCEPTION_TRACKING_SCHEMA,
        status=status,
        decode_document=request.decode_document,
        route=request.route,
        identity_policy=request.identity_policy,
        diagnostics=(diagnostic,),
    )


__all__ = [
    "PERCEPTION_TRACKING_SCHEMA",
    "TRACKING_BENCHMARK_SCHEMA",
    "MAX_TRACKING_ASSETS",
    "MAX_TRACKING_DETECTIONS",
    "MAX_TRACKING_MASKS",
    "MAX_TRACKING_TRACKS",
    "MAX_TRACKING_LINKS",
    "MAX_TRACKING_CANDIDATES",
    "MAX_TRACKING_REFERENCES",
    "MAX_TRACKING_EMBEDDINGS",
    "MAX_TRACKING_CASES",
    "MAX_TRACKING_METRICS",
    "MAX_TRACKING_OUTPUT_BYTES",
    "TrackingStatus",
    "PerceptionRoute",
    "IdentityPolicy",
    "MaskRepresentation",
    "TrackVisibility",
    "ReIDResolution",
    "TrackingCaseKind",
    "NormalizedPoint",
    "NormalizedRegion",
    "Detection",
    "SegmentationMask",
    "TrackLink",
    "TrackingTrack",
    "IdentityEmbedding",
    "ReIDCandidate",
    "TrackingReceipt",
    "TrackingRequest",
    "TrackingDocument",
    "TrackingCorpusCase",
    "TrackingMetricThreshold",
    "TrackingBenchmarkPlan",
    "build_default_tracking_benchmark_plan",
    "PerceptionTrackingAdapter",
    "execute_perception_tracking",
    "build_tracking_abstention",
]
