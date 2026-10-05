"""Source-PTS action, state, motion, camera, edit, and style contracts for M11-06.

This pure-core module consumes an M11-05 ``TrackingDocument`` and records separate temporal
claims.  It does not import optical-flow, action-recognition, style, GPU, ComfyUI, or provider
runtimes.  Claims remain inspectable when models disagree or a capability is unsupported.
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
from .errors import TemporalVisualAnalysisError
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
from .perception_tracking import TrackingDocument
from .video_decode import DecodedFrame, SourcePTS, VideoDecodeDocument

TEMPORAL_VISUAL_SCHEMA = "h3.visual.temporal_analysis.v1"
TEMPORAL_BENCHMARK_SCHEMA = "h3.visual.temporal_analysis.benchmark.v1"
MAX_TEMPORAL_CLAIMS = 256
MAX_TEMPORAL_OBSERVATIONS = 64
MAX_TEMPORAL_FRAMES = 512
MAX_TEMPORAL_SHOTS = 32
MAX_TEMPORAL_TRACKS = 64
MAX_TEMPORAL_CASES = 32
MAX_TEMPORAL_METRICS = 16
MAX_TEMPORAL_OUTPUT_BYTES = 65_536

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


class TemporalStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"


class TemporalRoute(str, Enum):
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"
    SPECIALIST = "specialist"


class TemporalClaimKind(str, Enum):
    SUBJECT_ACTION = "subject_action"
    OBJECT_STATE_CHANGE = "object_state_change"
    OPTICAL_MOTION = "optical_motion"
    CAMERA_MOTION = "camera_motion"
    COMPOSITION = "composition"
    EDIT_TRANSITION = "edit_transition"
    STYLE = "style"


class TemporalSupport(str, Enum):
    SUPPORTED = "supported"
    UNCERTAIN = "uncertain"
    UNSUPPORTED = "unsupported"


class ObservationResolution(str, Enum):
    CONSENSUS = "consensus"
    DISAGREEMENT = "disagreement"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


class TemporalCaseKind(str, Enum):
    CLEAN_ACTION = "clean_action"
    OBJECT_STATE = "object_state"
    OPTICAL_CAMERA_SEPARATION = "optical_camera_separation"
    COMPOSITION_CHANGE = "composition_change"
    EDIT_TRANSITION = "edit_transition"
    STYLE = "style"
    MODEL_DISAGREEMENT = "model_disagreement"
    UNSUPPORTED_LABEL = "unsupported_label"
    CORRUPT_EMPTY = "corrupt_empty"


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise TemporalVisualAnalysisError(f"{field} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise TemporalVisualAnalysisError(f"{field} contains sensitive or locator material")
    return value


def _code(value: object, field: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value.casefold()) is None:
        raise TemporalVisualAnalysisError(f"{field} must be a lower-case bounded code")
    return value.casefold()


def _text(value: object, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise TemporalVisualAnalysisError(f"{field} must be bounded non-empty text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise TemporalVisualAnalysisError(f"{field} contains a control character")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise TemporalVisualAnalysisError(f"{field} contains sensitive or locator material")
    return value


def _fingerprint(value: object, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise TemporalVisualAnalysisError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _version(value: object, field: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise TemporalVisualAnalysisError(f"{field} must be a numeric version")
    return value


def _confidence(value: object, field: str = "confidence") -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 1:
        raise TemporalVisualAnalysisError(f"{field} must be a Decimal between 0 and 1")
    return value


def _ids(values: object, field: str, maximum: int, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise TemporalVisualAnalysisError(f"{field} is outside the finite limit")
    if required and not values:
        raise TemporalVisualAnalysisError(f"{field} must not be empty")
    result = tuple(_identifier(value, f"{field} item") for value in values)
    if len(result) != len(set(result)):
        raise TemporalVisualAnalysisError(f"{field} must not contain duplicates")
    return result


def _uncertainties(values: object, field: str) -> tuple[Uncertainty, ...]:
    if (
        not isinstance(values, tuple)
        or len(values) > 8
        or not all(isinstance(value, Uncertainty) for value in values)
    ):
        raise TemporalVisualAnalysisError(f"{field} must contain bounded Uncertainty values")
    return values


@dataclass(frozen=True, slots=True)
class TemporalInterval:
    asset_id: str
    source_id: str
    start: SourcePTS
    end: SourcePTS
    frame_ids: tuple[str, ...]
    shot_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.asset_id, "interval asset_id")
        _identifier(self.source_id, "interval source_id")
        if not isinstance(self.start, SourcePTS) or not isinstance(self.end, SourcePTS):
            raise TemporalVisualAnalysisError("interval bounds must be SourcePTS")
        if (
            self.start.time_base_num != self.end.time_base_num
            or self.start.time_base_den != self.end.time_base_den
        ):
            raise TemporalVisualAnalysisError("interval bounds must use one source time base")
        if self.end.timestamp.seconds <= self.start.timestamp.seconds:
            raise TemporalVisualAnalysisError("interval end must be after start")
        _ids(self.frame_ids, "interval frame_ids", MAX_TEMPORAL_FRAMES, required=True)
        _ids(self.shot_ids, "interval shot_ids", MAX_TEMPORAL_SHOTS, required=True)

    def to_wire(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "start": self.start.to_wire(),
            "end": self.end.to_wire(),
            "frame_ids": list(self.frame_ids),
            "shot_ids": list(self.shot_ids),
        }


@dataclass(frozen=True, slots=True)
class TemporalClaim:
    claim_id: str
    asset_id: str
    source_id: str
    kind: TemporalClaimKind | str
    label: str | None
    interval: TemporalInterval
    route: TemporalRoute | str
    adapter_id: str
    model_id: str
    model_digest: str
    support: TemporalSupport | str
    confidence: Decimal | None = None
    track_ids: tuple[str, ...] = ()
    uncertainties: tuple[Uncertainty, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.claim_id, "claim_id")
        _identifier(self.asset_id, "claim asset_id")
        _identifier(self.source_id, "claim source_id")
        try:
            kind = (
                self.kind
                if isinstance(self.kind, TemporalClaimKind)
                else TemporalClaimKind(self.kind)
            )
        except (TypeError, ValueError):
            raise TemporalVisualAnalysisError("claim kind is unsupported") from None
        object.__setattr__(self, "kind", kind)
        if self.label is not None:
            _text(self.label, "claim label")
        try:
            route = (
                self.route if isinstance(self.route, TemporalRoute) else TemporalRoute(self.route)
            )
        except (TypeError, ValueError):
            raise TemporalVisualAnalysisError("claim route is unsupported") from None
        object.__setattr__(self, "route", route)
        _code(self.adapter_id, "claim adapter_id")
        _identifier(self.model_id, "claim model_id")
        _fingerprint(self.model_digest, "claim model_digest")
        try:
            support = (
                self.support
                if isinstance(self.support, TemporalSupport)
                else TemporalSupport(self.support)
            )
        except (TypeError, ValueError):
            raise TemporalVisualAnalysisError("claim support is unsupported") from None
        object.__setattr__(self, "support", support)
        if support is TemporalSupport.UNSUPPORTED and self.label is not None:
            raise TemporalVisualAnalysisError("unsupported claim cannot fabricate a label")
        if support is not TemporalSupport.UNSUPPORTED and self.label is None:
            raise TemporalVisualAnalysisError("supported claim requires a label")
        if not isinstance(self.interval, TemporalInterval):
            raise TemporalVisualAnalysisError("claim interval is invalid")
        _confidence(self.confidence, "claim confidence")
        _ids(self.track_ids, "claim track_ids", MAX_TEMPORAL_TRACKS)
        object.__setattr__(
            self, "uncertainties", _uncertainties(self.uncertainties, "claim uncertainties")
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "kind": cast(TemporalClaimKind, self.kind).value,
            "label": self.label,
            "interval": self.interval.to_wire(),
            "route": cast(TemporalRoute, self.route).value,
            "adapter_id": self.adapter_id,
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "support": cast(TemporalSupport, self.support).value,
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "track_ids": list(self.track_ids),
            "uncertainties": [item.to_wire() for item in self.uncertainties],
        }


@dataclass(frozen=True, slots=True)
class TemporalObservation:
    observation_id: str
    asset_id: str
    source_id: str
    interval: TemporalInterval
    claim_ids: tuple[str, ...]
    resolution: ObservationResolution | str
    uncertainties: tuple[Uncertainty, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.observation_id, "observation_id")
        _identifier(self.asset_id, "observation asset_id")
        _identifier(self.source_id, "observation source_id")
        if not isinstance(self.interval, TemporalInterval):
            raise TemporalVisualAnalysisError("observation interval is invalid")
        _ids(self.claim_ids, "observation claim_ids", MAX_TEMPORAL_CLAIMS, required=True)
        try:
            resolution = (
                self.resolution
                if isinstance(self.resolution, ObservationResolution)
                else ObservationResolution(self.resolution)
            )
        except (TypeError, ValueError):
            raise TemporalVisualAnalysisError("observation resolution is unsupported") from None
        object.__setattr__(self, "resolution", resolution)
        object.__setattr__(
            self, "uncertainties", _uncertainties(self.uncertainties, "observation uncertainties")
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "interval": self.interval.to_wire(),
            "claim_ids": list(self.claim_ids),
            "resolution": cast(ObservationResolution, self.resolution).value,
            "uncertainties": [item.to_wire() for item in self.uncertainties],
        }


@dataclass(frozen=True, slots=True)
class TemporalVisualReceipt:
    adapter_id: str
    adapter_version: str
    route: TemporalRoute | str
    model_id: str
    model_digest: str
    output_fingerprint: str
    source_fingerprints: tuple[str, ...]

    def __post_init__(self) -> None:
        _code(self.adapter_id, "receipt adapter_id")
        _version(self.adapter_version, "receipt adapter_version")
        try:
            route = (
                self.route if isinstance(self.route, TemporalRoute) else TemporalRoute(self.route)
            )
        except (TypeError, ValueError):
            raise TemporalVisualAnalysisError("receipt route is unsupported") from None
        object.__setattr__(self, "route", route)
        _identifier(self.model_id, "receipt model_id")
        _fingerprint(self.model_digest, "receipt model_digest")
        _fingerprint(self.output_fingerprint, "receipt output_fingerprint")
        if not isinstance(self.source_fingerprints, tuple) or not self.source_fingerprints:
            raise TemporalVisualAnalysisError("receipt source_fingerprints must be non-empty")
        for fingerprint in self.source_fingerprints:
            _fingerprint(fingerprint, "receipt source fingerprint")

    def to_wire(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "route": cast(TemporalRoute, self.route).value,
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "output_fingerprint": self.output_fingerprint,
            "source_fingerprints": list(self.source_fingerprints),
        }


@dataclass(frozen=True, slots=True)
class TemporalVisualRequest:
    tracking_document: TrackingDocument
    route: TemporalRoute | str = TemporalRoute.COMFYUI_NATIVE
    adapter_id: str = "injected_temporal_analysis"
    max_claims: int = MAX_TEMPORAL_CLAIMS

    def __post_init__(self) -> None:
        if not isinstance(self.tracking_document, TrackingDocument):
            raise TemporalVisualAnalysisError("tracking_document must be TrackingDocument")
        try:
            route = (
                self.route if isinstance(self.route, TemporalRoute) else TemporalRoute(self.route)
            )
        except (TypeError, ValueError):
            raise TemporalVisualAnalysisError("temporal route is unsupported") from None
        object.__setattr__(self, "route", route)
        _code(self.adapter_id, "temporal adapter_id")
        if (
            isinstance(self.max_claims, bool)
            or not isinstance(self.max_claims, int)
            or not 1 <= self.max_claims <= MAX_TEMPORAL_CLAIMS
        ):
            raise TemporalVisualAnalysisError("max_claims is outside the finite limit")


def _frame_index(document: VideoDecodeDocument) -> dict[str, DecodedFrame]:
    return {frame.frame_id: frame for frame in document.frames}


def _shot_index(document: VideoDecodeDocument) -> dict[str, object]:
    return {shot.shot_id: shot for shot in document.shots}


@dataclass(frozen=True, slots=True)
class TemporalVisualDocument:
    document_id: str
    schema: str
    status: TemporalStatus
    tracking_document: TrackingDocument
    route: TemporalRoute | str
    claims: tuple[TemporalClaim, ...] = ()
    observations: tuple[TemporalObservation, ...] = ()
    receipt: TemporalVisualReceipt | None = None
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.document_id, "temporal document_id")
        if self.schema != TEMPORAL_VISUAL_SCHEMA:
            raise TemporalVisualAnalysisError("unsupported temporal visual schema")
        if not isinstance(self.status, TemporalStatus):
            raise TemporalVisualAnalysisError("temporal status is invalid")
        if not isinstance(self.tracking_document, TrackingDocument):
            raise TemporalVisualAnalysisError("tracking_document is invalid")
        try:
            route = (
                self.route if isinstance(self.route, TemporalRoute) else TemporalRoute(self.route)
            )
        except (TypeError, ValueError):
            raise TemporalVisualAnalysisError("temporal document route is unsupported") from None
        object.__setattr__(self, "route", route)
        if (
            not isinstance(self.claims, tuple)
            or len(self.claims) > MAX_TEMPORAL_CLAIMS
            or not all(isinstance(item, TemporalClaim) for item in self.claims)
        ):
            raise TemporalVisualAnalysisError("claims are outside the finite limit")
        if (
            not isinstance(self.observations, tuple)
            or len(self.observations) > MAX_TEMPORAL_OBSERVATIONS
            or not all(isinstance(item, TemporalObservation) for item in self.observations)
        ):
            raise TemporalVisualAnalysisError("observations are outside the finite limit")
        if len({item.claim_id for item in self.claims}) != len(self.claims):
            raise TemporalVisualAnalysisError("claim IDs must be unique")
        if len({item.observation_id for item in self.observations}) != len(self.observations):
            raise TemporalVisualAnalysisError("observation IDs must be unique")
        if self.receipt is not None and not isinstance(self.receipt, TemporalVisualReceipt):
            raise TemporalVisualAnalysisError("temporal receipt is invalid")
        if (
            not isinstance(self.diagnostics, tuple)
            or len(self.diagnostics) > 16
            or not all(isinstance(item, str) and item for item in self.diagnostics)
        ):
            raise TemporalVisualAnalysisError("temporal diagnostics are outside the finite limit")
        decode = self.tracking_document.decode_document
        frames = _frame_index(decode)
        shots = _shot_index(decode)
        tracks = {track.track_id: track for track in self.tracking_document.tracks}
        claim_map = {claim.claim_id: claim for claim in self.claims}
        for claim in self.claims:
            if claim.asset_id not in decode.selected_asset_ids:
                raise TemporalVisualAnalysisError("claim asset is not selected")
            if (
                claim.interval.asset_id != claim.asset_id
                or claim.interval.source_id != claim.source_id
            ):
                raise TemporalVisualAnalysisError("claim interval source ownership differs")
            if claim.route is not self.route:
                raise TemporalVisualAnalysisError("claim route differs from document route")
            for frame_id in claim.interval.frame_ids:
                frame = frames.get(frame_id)
                if frame is None:
                    raise TemporalVisualAnalysisError("claim references an unknown frame")
                if (
                    frame.asset_id != claim.asset_id
                    or frame.source_id != claim.source_id
                    or not (
                        claim.interval.start.timestamp.seconds
                        <= frame.source_pts.timestamp.seconds
                        < claim.interval.end.timestamp.seconds
                    )
                ):
                    raise TemporalVisualAnalysisError("claim frame is outside its source interval")
            for shot_id in claim.interval.shot_ids:
                shot = shots.get(shot_id)
                if shot is None or getattr(shot, "asset_id", None) != claim.asset_id:
                    raise TemporalVisualAnalysisError("claim references an unknown shot")
            for track_id in claim.track_ids:
                track = tracks.get(track_id)
                if (
                    track is None
                    or track.asset_id != claim.asset_id
                    or track.source_id != claim.source_id
                ):
                    raise TemporalVisualAnalysisError("claim track is not source-owned")
        for observation in self.observations:
            if observation.asset_id not in decode.selected_asset_ids:
                raise TemporalVisualAnalysisError("observation asset is not selected")
            if (
                observation.interval.asset_id != observation.asset_id
                or observation.interval.source_id != observation.source_id
            ):
                raise TemporalVisualAnalysisError("observation interval source ownership differs")
            observed_claims: list[TemporalClaim] = []
            for claim_id in observation.claim_ids:
                observed_claim = claim_map.get(claim_id)
                if observed_claim is None:
                    raise TemporalVisualAnalysisError("observation references an unknown claim")
                if (
                    observed_claim.asset_id != observation.asset_id
                    or observed_claim.source_id != observation.source_id
                    or observed_claim.interval != observation.interval
                ):
                    raise TemporalVisualAnalysisError(
                        "observation claims do not share one source interval"
                    )
                observed_claims.append(observed_claim)
            if observation.resolution is ObservationResolution.DISAGREEMENT:
                if len(observed_claims) < 2 or len({claim.label for claim in observed_claims}) < 2:
                    raise TemporalVisualAnalysisError("disagreement requires distinct alternatives")
            if observation.resolution is ObservationResolution.UNSUPPORTED and any(
                claim.support is not TemporalSupport.UNSUPPORTED for claim in observed_claims
            ):
                raise TemporalVisualAnalysisError(
                    "unsupported observation cannot contain supported claims"
                )
        if self.receipt is not None:
            if self.receipt.route is not self.route:
                raise TemporalVisualAnalysisError("temporal receipt route differs from document")
            decode_receipt = decode.receipt
            if (
                decode_receipt is not None
                and decode_receipt.source_fingerprint not in self.receipt.source_fingerprints
            ):
                raise TemporalVisualAnalysisError("temporal receipt does not cover decoded source")
        if self.status is TemporalStatus.COMPLETE:
            if (
                not self.tracking_document.complete
                or not self.claims
                or not self.observations
                or self.receipt is None
            ):
                raise TemporalVisualAnalysisError(
                    "complete temporal document requires source, claims, observations, and receipt"
                )
        elif self.status in {
            TemporalStatus.EMPTY,
            TemporalStatus.CORRUPT,
            TemporalStatus.UNSUPPORTED,
            TemporalStatus.CANCELLED,
        } and (self.claims or self.observations or self.receipt is not None):
            raise TemporalVisualAnalysisError("terminal temporal abstention cannot contain output")
        if len(repr(self.to_wire()).encode("utf-8")) > MAX_TEMPORAL_OUTPUT_BYTES:
            raise TemporalVisualAnalysisError("temporal document exceeds portable output budget")

    @property
    def complete(self) -> bool:
        return self.status is TemporalStatus.COMPLETE

    @property
    def route_value(self) -> TemporalRoute:
        return cast(TemporalRoute, self.route)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "document_id": self.document_id,
            "status": self.status.value,
            "complete": self.complete,
            "route": self.route_value.value,
            "tracking_document_id": self.tracking_document.document_id,
            "claims": [item.to_wire() for item in self.claims],
            "observations": [item.to_wire() for item in self.observations],
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
            "tracking_document_id": self.tracking_document.document_id,
            "claim_count": len(self.claims),
            "observation_count": len(self.observations),
            "receipt": None if self.receipt is None else self.receipt.to_wire(),
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class TemporalCorpusCase:
    case_id: str
    kind: TemporalCaseKind
    source_fingerprint: str
    expected_claims: int
    should_abstain: bool = False

    def __post_init__(self) -> None:
        _identifier(self.case_id, "temporal case_id")
        if not isinstance(self.kind, TemporalCaseKind):
            raise TemporalVisualAnalysisError("temporal case kind is unsupported")
        _fingerprint(self.source_fingerprint, "temporal case source_fingerprint")
        if (
            isinstance(self.expected_claims, bool)
            or not isinstance(self.expected_claims, int)
            or not 0 <= self.expected_claims <= MAX_TEMPORAL_CLAIMS
        ):
            raise TemporalVisualAnalysisError("expected_claims is outside the finite limit")
        if not isinstance(self.should_abstain, bool):
            raise TemporalVisualAnalysisError("should_abstain must be boolean")

    def to_wire(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "kind": self.kind.value,
            "source_fingerprint": self.source_fingerprint,
            "expected_claims": self.expected_claims,
            "should_abstain": self.should_abstain,
        }


@dataclass(frozen=True, slots=True)
class TemporalMetricThreshold:
    metric: str
    minimum: Decimal | None = None
    maximum: Decimal | None = None

    def __post_init__(self) -> None:
        _identifier(self.metric, "temporal metric")
        if (self.minimum is None) == (self.maximum is None):
            raise TemporalVisualAnalysisError("exactly one temporal metric bound is required")
        value = self.minimum if self.minimum is not None else self.maximum
        if value is None or not value.is_finite() or not 0 <= value <= 1:
            raise TemporalVisualAnalysisError("temporal metric threshold must be between 0 and 1")

    def to_wire(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "minimum": None if self.minimum is None else format(self.minimum, "f"),
            "maximum": None if self.maximum is None else format(self.maximum, "f"),
        }


@dataclass(frozen=True, slots=True)
class TemporalBenchmarkPlan:
    cases: tuple[TemporalCorpusCase, ...]
    thresholds: tuple[TemporalMetricThreshold, ...]
    schema: str = TEMPORAL_BENCHMARK_SCHEMA
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.cases, tuple)
            or not self.cases
            or len(self.cases) > MAX_TEMPORAL_CASES
        ):
            raise TemporalVisualAnalysisError("temporal cases are outside the finite limit")
        if not all(isinstance(item, TemporalCorpusCase) for item in self.cases):
            raise TemporalVisualAnalysisError("temporal cases are invalid")
        if len({item.case_id for item in self.cases}) != len(self.cases):
            raise TemporalVisualAnalysisError("temporal case IDs must be unique")
        if (
            not isinstance(self.thresholds, tuple)
            or not self.thresholds
            or len(self.thresholds) > MAX_TEMPORAL_METRICS
        ):
            raise TemporalVisualAnalysisError("temporal thresholds are outside the finite limit")
        if not all(isinstance(item, TemporalMetricThreshold) for item in self.thresholds):
            raise TemporalVisualAnalysisError("temporal thresholds are invalid")
        if len({item.metric for item in self.thresholds}) != len(self.thresholds):
            raise TemporalVisualAnalysisError("temporal metrics must be unique")
        expected = canonical_fingerprint(
            {
                "schema": self.schema,
                "cases": [item.to_wire() for item in self.cases],
                "thresholds": [item.to_wire() for item in self.thresholds],
            }
        )
        if self.fingerprint and self.fingerprint != expected:
            raise TemporalVisualAnalysisError(
                "temporal benchmark fingerprint does not match contents"
            )
        object.__setattr__(self, "fingerprint", expected)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "fingerprint": self.fingerprint,
            "cases": [item.to_wire() for item in self.cases],
            "thresholds": [item.to_wire() for item in self.thresholds],
        }


def build_default_temporal_benchmark_plan() -> TemporalBenchmarkPlan:
    def case(
        case_id: str, kind: TemporalCaseKind, claims: int, abstain: bool = False
    ) -> TemporalCorpusCase:
        return TemporalCorpusCase(
            case_id,
            kind,
            canonical_fingerprint({"case_id": case_id}),
            claims,
            abstain,
        )

    cases = (
        case("temporal.clean_action", TemporalCaseKind.CLEAN_ACTION, 1),
        case("temporal.object_state", TemporalCaseKind.OBJECT_STATE, 1),
        case("temporal.optical_camera", TemporalCaseKind.OPTICAL_CAMERA_SEPARATION, 2),
        case("temporal.composition", TemporalCaseKind.COMPOSITION_CHANGE, 1),
        case("temporal.edit", TemporalCaseKind.EDIT_TRANSITION, 1),
        case("temporal.style", TemporalCaseKind.STYLE, 1),
        case("temporal.disagreement", TemporalCaseKind.MODEL_DISAGREEMENT, 2),
        case("temporal.unsupported", TemporalCaseKind.UNSUPPORTED_LABEL, 1),
        case("temporal.corrupt", TemporalCaseKind.CORRUPT_EMPTY, 0, True),
    )
    thresholds = (
        TemporalMetricThreshold("temporal_grounding_iou", minimum=Decimal("0.80")),
        TemporalMetricThreshold("action_state_accuracy", minimum=Decimal("0.85")),
        TemporalMetricThreshold("motion_camera_separation", minimum=Decimal("0.85")),
        TemporalMetricThreshold("boundary_tolerance", maximum=Decimal("0.04")),
        TemporalMetricThreshold("disagreement_preservation", minimum=Decimal("1.0")),
        TemporalMetricThreshold("unsupported_abstention", minimum=Decimal("1.0")),
    )
    return TemporalBenchmarkPlan(cases=cases, thresholds=thresholds)


@runtime_checkable
class TemporalVisualAdapter(Protocol):
    @property
    def descriptor(self) -> LocalAdapterDescriptor: ...

    def analyze(
        self, request: TemporalVisualRequest, guard: LocalBudgetGuard
    ) -> TemporalVisualDocument: ...


class _TemporalBridge:
    def __init__(self, adapter: TemporalVisualAdapter) -> None:
        self._adapter = adapter

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._adapter.descriptor

    def run(
        self, request: LocalAdapterExecutionRequest, guard: LocalBudgetGuard
    ) -> LocalAdapterResult:
        if not isinstance(request.input_value, TemporalVisualRequest):
            raise TemporalVisualAnalysisError("temporal adapter input is not TemporalVisualRequest")
        document = self._adapter.analyze(request.input_value, guard)
        if not isinstance(document, TemporalVisualDocument):
            raise TemporalVisualAnalysisError("temporal adapter returned an invalid document")
        return LocalAdapterResult(
            adapter_id=self.descriptor.adapter_id,
            adapter_version=self.descriptor.adapter_version,
            device=request.device,
            value=document,
            output_bytes=len(repr(document.to_wire()).encode("utf-8")),
            output_items=len(document.claims) + len(document.observations),
        )


def execute_temporal_visual_analysis(
    adapter: TemporalVisualAdapter,
    request: TemporalVisualRequest,
    *,
    runtime: LocalAdapterRuntime | None = None,
    device: LocalDeviceSpec | None = None,
    cancellation_probe: LocalCancellationProbe | None = None,
    clock: Callable[[], float] | None = None,
    memory_meter: Callable[[], int] | None = None,
) -> TemporalVisualDocument:
    if not isinstance(adapter, TemporalVisualAdapter):
        raise TemporalVisualAnalysisError("adapter must implement TemporalVisualAdapter")
    if not isinstance(request, TemporalVisualRequest):
        raise TemporalVisualAnalysisError("request must be TemporalVisualRequest")
    device_value = LocalDeviceSpec(LocalDeviceKind.AUTO) if device is None else device
    if not isinstance(device_value, LocalDeviceSpec):
        raise TemporalVisualAnalysisError("device must be LocalDeviceSpec")
    execution_request = LocalAdapterExecutionRequest(
        adapter_id=adapter.descriptor.adapter_id,
        task_mode=TaskMode.REF2VA,
        media_kinds=(MediaKind.VIDEO,),
        reference_count=len(request.tracking_document.decode_document.selected_asset_ids),
        device=device_value,
        estimated_memory_bytes=1,
        estimated_output_bytes=adapter.descriptor.limits.max_output_bytes,
        deterministic_required=True,
        seed=0,
        cancellation_required=cancellation_probe is not None,
        input_value=request,
    )
    bridge = _TemporalBridge(adapter)
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
    if not isinstance(result.value, TemporalVisualDocument):
        raise TemporalVisualAnalysisError("temporal result did not contain a document")
    if result.value.tracking_document.document_id != request.tracking_document.document_id:
        raise TemporalVisualAnalysisError(
            "temporal document does not use the requested tracking source"
        )
    if result.value.route is not request.route:
        raise TemporalVisualAnalysisError("temporal document route does not match request")
    if len(result.value.claims) > request.max_claims:
        raise TemporalVisualAnalysisError("temporal document exceeds claim limit")
    return result.value


def build_temporal_visual_abstention(
    request: TemporalVisualRequest, status: TemporalStatus, diagnostic: str
) -> TemporalVisualDocument:
    if not isinstance(request, TemporalVisualRequest):
        raise TemporalVisualAnalysisError("request must be TemporalVisualRequest")
    if status is TemporalStatus.COMPLETE:
        raise TemporalVisualAnalysisError("complete status requires temporal output")
    if not isinstance(diagnostic, str) or not diagnostic:
        raise TemporalVisualAnalysisError("temporal diagnostic must be non-empty")
    return TemporalVisualDocument(
        document_id=canonical_fingerprint(
            {"tracking_document": request.tracking_document.document_id, "status": status.value}
        )[7:39],
        schema=TEMPORAL_VISUAL_SCHEMA,
        status=status,
        tracking_document=request.tracking_document,
        route=request.route,
        diagnostics=(diagnostic,),
    )


__all__ = [
    "TEMPORAL_VISUAL_SCHEMA",
    "TEMPORAL_BENCHMARK_SCHEMA",
    "MAX_TEMPORAL_CLAIMS",
    "MAX_TEMPORAL_OBSERVATIONS",
    "MAX_TEMPORAL_FRAMES",
    "MAX_TEMPORAL_SHOTS",
    "MAX_TEMPORAL_TRACKS",
    "MAX_TEMPORAL_CASES",
    "MAX_TEMPORAL_METRICS",
    "MAX_TEMPORAL_OUTPUT_BYTES",
    "TemporalStatus",
    "TemporalRoute",
    "TemporalClaimKind",
    "TemporalSupport",
    "ObservationResolution",
    "TemporalCaseKind",
    "TemporalInterval",
    "TemporalClaim",
    "TemporalObservation",
    "TemporalVisualReceipt",
    "TemporalVisualRequest",
    "TemporalVisualDocument",
    "TemporalCorpusCase",
    "TemporalMetricThreshold",
    "TemporalBenchmarkPlan",
    "build_default_temporal_benchmark_plan",
    "TemporalVisualAdapter",
    "execute_temporal_visual_analysis",
    "build_temporal_visual_abstention",
]
