"""Bounded video sampling and shot-analysis contracts for optional local assistance.

The pure core records safe media metadata and validates injected adapter output. It never opens a
video, assumes a constant frame rate, decodes pixels, imports a media runtime, or interprets an
observation as a user-owned constraint.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Protocol, cast, runtime_checkable

from .constraints import TimePoint
from .contracts import MediaKind, ProviderIdentity, TaskMode, ValidationSeverity
from .errors import VideoAnalysisError
from .evidence import EvidenceOrigin, EvidenceRecord, EvidenceSourceKind, Uncertainty
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
from .reference_window import ReferenceConditioningWindow
from .registry import MAX_DURATION_SECONDS, MAX_REFERENCE_VIDEOS, ReferenceRegistry

VIDEO_ANALYSIS_SCHEMA = "h3.video.analysis.v1"
MAX_VIDEO_INPUT_BYTES = 4 * 1024 * 1024 * 1024
MAX_VIDEO_DIMENSION = 1_048_576
MAX_VIDEO_FRAME_COUNT = 10_000_000
MAX_VIDEO_FPS = Decimal("1000")
MAX_VIDEO_SAMPLES = 2048
MAX_VIDEO_KEYFRAMES = 512
MAX_VIDEO_SHOTS = 256
MAX_VIDEO_OBSERVATIONS = 512
MAX_VIDEO_UNCERTAINTIES = 32
MAX_VIDEO_DIAGNOSTICS = 256
MAX_VIDEO_TEXT_LENGTH = 4096

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_SENSITIVE_MARKERS = (
    "api_key",
    "authorization",
    "bearer ",
    "password",
    "secret",
    "token=",
    "https://",
    "http://",
    "file://",
    "/",
    "\\",
)


class VideoOrientation(str, Enum):
    """Declared video orientation retained without an automatic transform."""

    UP = "up"
    ROTATE_90 = "rotate_90"
    ROTATE_180 = "rotate_180"
    ROTATE_270 = "rotate_270"
    MIRROR_HORIZONTAL = "mirror_horizontal"
    MIRROR_VERTICAL = "mirror_vertical"
    UNKNOWN = "unknown"


class VideoSamplingStrategy(str, Enum):
    """Explicit sampling strategy delegated to the selected adapter."""

    UNIFORM = "uniform"
    SCENE_CHANGE = "scene_change"
    HYBRID = "hybrid"


class VideoAnalysisStatus(str, Enum):
    """Analysis completion state; degraded states never masquerade as complete."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"


class VideoObservationKind(str, Enum):
    """Observation families owned by M6-01; subject identity is a later graph concern."""

    SCENE = "scene"
    ACTION = "action"
    CAMERA = "camera"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise VideoAnalysisError(f"{field_name} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise VideoAnalysisError(f"{field_name} contains sensitive material")
    return value


def _text(value: object, field_name: str, maximum: int = MAX_VIDEO_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise VideoAnalysisError(f"{field_name} must be a bounded non-empty string")
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise VideoAnalysisError(f"{field_name} contains an unsafe wire code point")
    return value


def _positive_int(value: object, field_name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise VideoAnalysisError(f"{field_name} must be between 1 and {maximum}")
    return value


def _non_negative_int(value: object, field_name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise VideoAnalysisError(f"{field_name} must be between 0 and {maximum}")
    return value


def _decimal(value: object, field_name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise VideoAnalysisError(f"{field_name} must be a finite Decimal")
    if positive and value <= 0:
        raise VideoAnalysisError(f"{field_name} must be positive")
    if not positive and value < 0:
        raise VideoAnalysisError(f"{field_name} must be non-negative")
    return value


def _orientation(value: VideoOrientation | str) -> VideoOrientation:
    try:
        return value if isinstance(value, VideoOrientation) else VideoOrientation(value)
    except (TypeError, ValueError):
        raise VideoAnalysisError("video orientation is unsupported") from None


def _strategy(value: VideoSamplingStrategy | str) -> VideoSamplingStrategy:
    try:
        return value if isinstance(value, VideoSamplingStrategy) else VideoSamplingStrategy(value)
    except (TypeError, ValueError):
        raise VideoAnalysisError("video sampling strategy is unsupported") from None


def _id_tuple(values: object, field_name: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise VideoAnalysisError(f"{field_name} is over its finite limit")
    result = tuple(_identifier(value, f"{field_name} item") for value in values)
    if len(result) != len(set(result)):
        raise VideoAnalysisError(f"{field_name} must not contain duplicate identifiers")
    return result


def _uncertainties(values: object, field_name: str) -> tuple[Uncertainty, ...]:
    if not isinstance(values, tuple) or len(values) > MAX_VIDEO_UNCERTAINTIES:
        raise VideoAnalysisError(f"{field_name} is over its finite limit")
    if not all(isinstance(value, Uncertainty) for value in values):
        raise VideoAnalysisError(f"{field_name} must contain Uncertainty values")
    return values


@dataclass(frozen=True, slots=True)
class VideoSelection:
    """Canonical video identity plus caller-declared metadata, never a locator or media object."""

    asset_id: str
    source_id: str
    orientation: VideoOrientation | str = VideoOrientation.UP
    declared_size_bytes: int | None = None
    declared_duration_seconds: Decimal | None = None
    declared_width: int | None = None
    declared_height: int | None = None
    declared_frame_count: int | None = None
    declared_frame_rate: Decimal | None = None
    variable_frame_rate: bool = False
    has_audio_track: bool | None = None

    def __post_init__(self) -> None:
        _identifier(self.asset_id, "video asset_id")
        _identifier(self.source_id, "video source_id")
        object.__setattr__(self, "orientation", _orientation(self.orientation))
        if self.declared_size_bytes is not None:
            _positive_int(self.declared_size_bytes, "declared_size_bytes", MAX_VIDEO_INPUT_BYTES)
        if self.declared_duration_seconds is not None:
            duration = _decimal(
                self.declared_duration_seconds, "declared_duration_seconds", positive=True
            )
            if duration > MAX_DURATION_SECONDS:
                raise VideoAnalysisError("declared_duration_seconds exceeds the media limit")
        if (self.declared_width is None) != (self.declared_height is None):
            raise VideoAnalysisError("declared video width and height must be supplied together")
        for value, field_name in (
            (self.declared_width, "declared_width"),
            (self.declared_height, "declared_height"),
        ):
            if value is not None:
                _positive_int(value, field_name, MAX_VIDEO_DIMENSION)
        if self.declared_frame_count is not None:
            _positive_int(self.declared_frame_count, "declared_frame_count", MAX_VIDEO_FRAME_COUNT)
        if self.declared_frame_rate is not None:
            frame_rate = _decimal(self.declared_frame_rate, "declared_frame_rate", positive=True)
            if frame_rate > MAX_VIDEO_FPS:
                raise VideoAnalysisError("declared_frame_rate exceeds the media limit")
        if not isinstance(self.variable_frame_rate, bool):
            raise VideoAnalysisError("variable_frame_rate must be a boolean")
        if self.has_audio_track is not None and not isinstance(self.has_audio_track, bool):
            raise VideoAnalysisError("has_audio_track must be a boolean or None")

    def to_wire(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "orientation": cast(VideoOrientation, self.orientation).value,
            "declared_size_bytes": self.declared_size_bytes,
            "declared_duration_seconds": (
                None
                if self.declared_duration_seconds is None
                else format(self.declared_duration_seconds, "f")
            ),
            "declared_width": self.declared_width,
            "declared_height": self.declared_height,
            "declared_frame_count": self.declared_frame_count,
            "declared_frame_rate": (
                None if self.declared_frame_rate is None else format(self.declared_frame_rate, "f")
            ),
            "variable_frame_rate": self.variable_frame_rate,
            "has_audio_track": self.has_audio_track,
        }


@dataclass(frozen=True, slots=True)
class VideoSamplingConfig:
    """Finite adapter settings; the strategy itself is never inferred from media."""

    strategy: VideoSamplingStrategy | str = VideoSamplingStrategy.UNIFORM
    max_samples: int = 64
    max_keyframes: int = 32
    max_shots: int = 64
    minimum_shot_duration: Decimal = Decimal("0")
    scene_change_threshold: Decimal | None = None
    seed: int | None = None
    schema: str = VIDEO_ANALYSIS_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy", _strategy(self.strategy))
        _positive_int(self.max_samples, "max_samples", MAX_VIDEO_SAMPLES)
        _positive_int(self.max_keyframes, "max_keyframes", MAX_VIDEO_KEYFRAMES)
        _positive_int(self.max_shots, "max_shots", MAX_VIDEO_SHOTS)
        if self.max_keyframes > self.max_samples or self.max_shots > self.max_samples:
            raise VideoAnalysisError("keyframe and shot limits cannot exceed max_samples")
        minimum = _decimal(self.minimum_shot_duration, "minimum_shot_duration")
        if minimum > MAX_DURATION_SECONDS:
            raise VideoAnalysisError("minimum_shot_duration exceeds the media limit")
        if self.scene_change_threshold is not None:
            threshold = _decimal(self.scene_change_threshold, "scene_change_threshold")
            if threshold > 1:
                raise VideoAnalysisError("scene_change_threshold must be between 0 and 1")
        if self.seed is not None and (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not -(2**63) <= self.seed <= 2**63 - 1
        ):
            raise VideoAnalysisError("seed must be a signed 64-bit integer")
        if self.schema != VIDEO_ANALYSIS_SCHEMA:
            raise VideoAnalysisError("unsupported video analysis schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "strategy": cast(VideoSamplingStrategy, self.strategy).value,
            "max_samples": self.max_samples,
            "max_keyframes": self.max_keyframes,
            "max_shots": self.max_shots,
            "minimum_shot_duration": format(self.minimum_shot_duration, "f"),
            "scene_change_threshold": (
                None
                if self.scene_change_threshold is None
                else format(self.scene_change_threshold, "f")
            ),
            "seed": self.seed,
        }


@dataclass(frozen=True, slots=True)
class VideoAnalysisRequest:
    """Explicit Full-Reference video selection and sampling request."""

    task_mode: TaskMode
    reference_registry: ReferenceRegistry
    selections: tuple[VideoSelection, ...]
    sampling: VideoSamplingConfig

    def __post_init__(self) -> None:
        if self.task_mode is not TaskMode.REF2VA:
            raise VideoAnalysisError("video analysis currently requires REF2VA")
        if not isinstance(self.reference_registry, ReferenceRegistry):
            raise VideoAnalysisError("reference_registry must be a ReferenceRegistry")
        if (
            not isinstance(self.selections, tuple)
            or not self.selections
            or len(self.selections) > MAX_REFERENCE_VIDEOS
            or not all(isinstance(item, VideoSelection) for item in self.selections)
        ):
            raise VideoAnalysisError("selections must contain one to three VideoSelection values")
        if not isinstance(self.sampling, VideoSamplingConfig):
            raise VideoAnalysisError("sampling must be a VideoSamplingConfig")
        assets = {asset.asset_id: asset for asset in self.reference_registry.assets}
        asset_ids = tuple(item.asset_id for item in self.selections)
        source_ids = tuple(item.source_id for item in self.selections)
        if len(asset_ids) != len(set(asset_ids)) or len(source_ids) != len(set(source_ids)):
            raise VideoAnalysisError("video selections must have unique asset and source IDs")
        for item in self.selections:
            asset = assets.get(item.asset_id)
            if asset is None or asset.kind is not MediaKind.VIDEO:
                raise VideoAnalysisError("every selection must identify a canonical video asset")

    @property
    def selected_asset_ids(self) -> tuple[str, ...]:
        return tuple(item.asset_id for item in self.selections)

    @property
    def estimated_input_bytes(self) -> int | None:
        values = tuple(item.declared_size_bytes for item in self.selections)
        if any(value is None for value in values):
            return None
        return sum(cast(tuple[int, ...], values))

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": VIDEO_ANALYSIS_SCHEMA,
            "task_mode": self.task_mode.value,
            "selected_asset_ids": list(self.selected_asset_ids),
            "selections": [item.to_wire() for item in self.selections],
            "sampling": self.sampling.to_wire(),
        }


VideoSamplingRequest = VideoAnalysisRequest


@dataclass(frozen=True, slots=True)
class VideoKeyframe:
    """One adapter-selected frame timestamp with retained orientation and uncertainty."""

    keyframe_id: str
    asset_id: str
    source_id: str
    timestamp: TimePoint
    frame_index: int | None = None
    orientation: VideoOrientation | str = VideoOrientation.UP
    uncertainties: tuple[Uncertainty, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.keyframe_id, "keyframe_id")
        _identifier(self.asset_id, "keyframe asset_id")
        _identifier(self.source_id, "keyframe source_id")
        if not isinstance(self.timestamp, TimePoint):
            raise VideoAnalysisError("keyframe timestamp must be a TimePoint")
        if self.frame_index is not None:
            _positive_int(self.frame_index, "frame_index", MAX_VIDEO_FRAME_COUNT)
        orientation = _orientation(self.orientation)
        uncertainties = _uncertainties(self.uncertainties, "keyframe uncertainties")
        if orientation is VideoOrientation.UNKNOWN and not uncertainties:
            raise VideoAnalysisError("unknown keyframe orientation requires uncertainty")
        object.__setattr__(self, "orientation", orientation)

    def to_wire(self) -> dict[str, object]:
        return {
            "keyframe_id": self.keyframe_id,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "timestamp": self.timestamp.to_wire(),
            "frame_index": self.frame_index,
            "orientation": cast(VideoOrientation, self.orientation).value,
            "uncertainties": [item.to_wire() for item in self.uncertainties],
        }


@dataclass(frozen=True, slots=True)
class VideoObservation:
    """One source-timestamped scene/action/camera observation kept as observed evidence."""

    observation_id: str
    asset_id: str
    kind: VideoObservationKind
    evidence: EvidenceRecord

    def __post_init__(self) -> None:
        _identifier(self.observation_id, "observation_id")
        _identifier(self.asset_id, "observation asset_id")
        if not isinstance(self.kind, VideoObservationKind):
            raise VideoAnalysisError("video observation kind must be a VideoObservationKind")
        if not isinstance(self.evidence, EvidenceRecord):
            raise VideoAnalysisError("video observation evidence must be an EvidenceRecord")
        source = self.evidence.provenance.source
        if self.evidence.origin is not EvidenceOrigin.OBSERVED:
            raise VideoAnalysisError("video observations must remain OBSERVED evidence")
        if source.kind is not EvidenceSourceKind.MEDIA_ASSET or source.asset_id != self.asset_id:
            raise VideoAnalysisError("video observation source must match its asset ID")
        if self.evidence.provenance.provider is not ProviderIdentity.LOCAL:
            raise VideoAnalysisError("video observations require local provenance")
        if source.start is None or source.end is None:
            raise VideoAnalysisError("video observations require a source timestamp span")
        _text(self.evidence.claim, "video observation claim")

    @property
    def claim(self) -> str:
        return self.evidence.claim

    @property
    def source_id(self) -> str:
        return self.evidence.provenance.source.source_id

    @property
    def start(self) -> TimePoint:
        source = self.evidence.provenance.source
        if source.start is None:
            raise VideoAnalysisError("video observation start timestamp is missing")
        return source.start

    @property
    def end(self) -> TimePoint:
        source = self.evidence.provenance.source
        if source.end is None:
            raise VideoAnalysisError("video observation end timestamp is missing")
        return source.end

    def to_wire(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "asset_id": self.asset_id,
            "kind": self.kind.value,
            "evidence": self.evidence.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class VideoShot:
    """A half-open source interval with explicit keyframe and observation ownership."""

    shot_id: str
    asset_id: str
    source_id: str
    start: TimePoint
    end: TimePoint
    keyframe_ids: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()
    uncertainties: tuple[Uncertainty, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.shot_id, "shot_id")
        _identifier(self.asset_id, "shot asset_id")
        _identifier(self.source_id, "shot source_id")
        if not isinstance(self.start, TimePoint) or not isinstance(self.end, TimePoint):
            raise VideoAnalysisError("shot bounds must be TimePoint values")
        if self.end.seconds <= self.start.seconds:
            raise VideoAnalysisError("shot end must be after shot start")
        _id_tuple(self.keyframe_ids, "keyframe_ids", MAX_VIDEO_KEYFRAMES)
        _id_tuple(self.observation_ids, "observation_ids", MAX_VIDEO_OBSERVATIONS)
        _uncertainties(self.uncertainties, "shot uncertainties")

    def to_wire(self) -> dict[str, object]:
        return {
            "shot_id": self.shot_id,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "start": self.start.to_wire(),
            "end": self.end.to_wire(),
            "keyframe_ids": list(self.keyframe_ids),
            "observation_ids": list(self.observation_ids),
            "uncertainties": [item.to_wire() for item in self.uncertainties],
        }


@dataclass(frozen=True, slots=True)
class VideoAnalysisDiagnostic:
    """Safe non-content diagnostic for a degraded video analysis run."""

    code: str
    message: str
    severity: ValidationSeverity = ValidationSeverity.WARNING

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or _CODE_PATTERN.fullmatch(self.code) is None:
            raise VideoAnalysisError("diagnostic code must be a bounded code")
        _text(self.message, "diagnostic message", 1024)
        if not isinstance(self.severity, ValidationSeverity):
            raise VideoAnalysisError("diagnostic severity must be a ValidationSeverity")

    def to_wire(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "severity": self.severity.value}


@dataclass(frozen=True, slots=True)
class VideoAnalysisBatch:
    """Validated video-analysis output with explicit degraded states and finite collections."""

    batch_id: str
    schema: str
    status: VideoAnalysisStatus
    selected_asset_ids: tuple[str, ...]
    sampled_frame_count: int = 0
    keyframes: tuple[VideoKeyframe, ...] = ()
    observations: tuple[VideoObservation, ...] = ()
    shots: tuple[VideoShot, ...] = ()
    diagnostics: tuple[VideoAnalysisDiagnostic, ...] = ()
    admitted_frame_count: int | None = None
    frame_rate: Decimal | None = None
    conditioning_window: ReferenceConditioningWindow | None = None

    def __post_init__(self) -> None:
        _identifier(self.batch_id, "batch_id")
        if self.schema != VIDEO_ANALYSIS_SCHEMA:
            raise VideoAnalysisError("unsupported video analysis schema")
        if not isinstance(self.status, VideoAnalysisStatus):
            raise VideoAnalysisError("batch status must be a VideoAnalysisStatus")
        selected = _id_tuple(self.selected_asset_ids, "selected_asset_ids", MAX_REFERENCE_VIDEOS)
        if not selected:
            raise VideoAnalysisError("selected_asset_ids must not be empty")
        _non_negative_int(self.sampled_frame_count, "sampled_frame_count", MAX_VIDEO_SAMPLES)
        if (self.admitted_frame_count is None) != (self.frame_rate is None):
            raise VideoAnalysisError("admitted count and rate must be paired")
        if self.admitted_frame_count is not None:
            _positive_int(self.admitted_frame_count, "admitted_frame_count", 300)
            if (
                type(self.frame_rate) is not Decimal
                or not self.frame_rate.is_finite()
                or not 0 <= self.frame_rate <= 60
            ):
                raise VideoAnalysisError("invalid admitted frame rate")
        if self.conditioning_window is not None:
            if type(self.conditioning_window) is not ReferenceConditioningWindow:
                raise VideoAnalysisError("conditioning window must be exact")
            self.conditioning_window.__post_init__()
            if (
                self.admitted_frame_count is None
                or self.frame_rate is None
                or self.frame_rate <= 0
                or len(selected) != 1
                or self.conditioning_window.window_frame_count > self.admitted_frame_count
            ):
                raise VideoAnalysisError("conditioning window does not fit the admitted video")
        if not isinstance(self.keyframes, tuple) or not all(
            isinstance(item, VideoKeyframe) for item in self.keyframes
        ):
            raise VideoAnalysisError("keyframes must contain VideoKeyframe values")
        if not isinstance(self.observations, tuple) or not all(
            isinstance(item, VideoObservation) for item in self.observations
        ):
            raise VideoAnalysisError("observations must contain VideoObservation values")
        if not isinstance(self.shots, tuple) or not all(
            isinstance(item, VideoShot) for item in self.shots
        ):
            raise VideoAnalysisError("shots must contain VideoShot values")
        if not isinstance(self.diagnostics, tuple) or len(self.diagnostics) > MAX_VIDEO_DIAGNOSTICS:
            raise VideoAnalysisError("diagnostics must be a bounded tuple")
        if not all(isinstance(item, VideoAnalysisDiagnostic) for item in self.diagnostics):
            raise VideoAnalysisError("diagnostics must contain VideoAnalysisDiagnostic values")
        if len(self.keyframes) > MAX_VIDEO_KEYFRAMES:
            raise VideoAnalysisError("keyframes exceed the finite limit")
        if len(self.observations) > MAX_VIDEO_OBSERVATIONS:
            raise VideoAnalysisError("observations exceed the finite limit")
        if len(self.shots) > MAX_VIDEO_SHOTS:
            raise VideoAnalysisError("shots exceed the finite limit")
        identifiers = (
            tuple(item.keyframe_id for item in self.keyframes)
            + tuple(item.observation_id for item in self.observations)
            + tuple(item.shot_id for item in self.shots)
        )
        if len(identifiers) != len(set(identifiers)):
            raise VideoAnalysisError("video analysis output IDs must be unique")
        if self.status in {
            VideoAnalysisStatus.EMPTY,
            VideoAnalysisStatus.CORRUPT,
            VideoAnalysisStatus.UNSUPPORTED,
        } and (self.sampled_frame_count or self.keyframes or self.observations or self.shots):
            raise VideoAnalysisError(
                "degraded empty/corrupt/unsupported batches cannot contain output"
            )
        if self.status is VideoAnalysisStatus.COMPLETE and (
            self.sampled_frame_count <= 0 or not self.keyframes or not self.shots
        ):
            raise VideoAnalysisError("complete video analysis requires sampled shots and keyframes")

    @property
    def complete(self) -> bool:
        return self.status is VideoAnalysisStatus.COMPLETE

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "batch_id": self.batch_id,
            "status": self.status.value,
            "complete": self.complete,
            "selected_asset_ids": list(self.selected_asset_ids),
            "sampled_frame_count": self.sampled_frame_count,
            "keyframes": [item.to_wire() for item in self.keyframes],
            "observations": [item.to_wire() for item in self.observations],
            "shots": [item.to_wire() for item in self.shots],
            "diagnostics": [item.to_wire() for item in self.diagnostics],
            "admitted_frame_count": self.admitted_frame_count,
            "frame_rate": None if self.frame_rate is None else format(self.frame_rate, "f"),
            "conditioning_window": None
            if self.conditioning_window is None
            else self.conditioning_window.to_wire(),
        }


@runtime_checkable
class VideoAnalysisAdapter(Protocol):
    """Explicit local adapter seam for video sampling and shot analysis."""

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        """Return a local perception descriptor supporting video media."""

    def analyze(self, request: VideoAnalysisRequest, guard: LocalBudgetGuard) -> VideoAnalysisBatch:
        """Analyze selected videos without mutating hard constraints."""


class _VideoAdapterBridge:
    def __init__(self, adapter: VideoAnalysisAdapter) -> None:
        self._adapter = adapter

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._adapter.descriptor

    def run(
        self,
        request: LocalAdapterExecutionRequest,
        guard: LocalBudgetGuard,
    ) -> LocalAdapterResult:
        if not isinstance(request.input_value, VideoAnalysisRequest):
            raise VideoAnalysisError("video adapter input is not a VideoAnalysisRequest")
        batch = self._adapter.analyze(request.input_value, guard)
        if not isinstance(batch, VideoAnalysisBatch):
            raise VideoAnalysisError("video adapter returned an invalid analysis batch")
        if batch.schema != self.descriptor.output_schema:
            raise VideoAnalysisError("video adapter returned an unexpected output schema")
        output_bytes = sum(len(item.claim.encode("utf-8")) for item in batch.observations) + sum(
            len(item.message.encode("utf-8")) for item in batch.diagnostics
        )
        return LocalAdapterResult(
            adapter_id=self.descriptor.adapter_id,
            adapter_version=self.descriptor.adapter_version,
            device=request.device,
            value=batch,
            output_bytes=output_bytes,
            output_items=len(batch.keyframes) + len(batch.observations) + len(batch.shots),
        )


def _within(timestamp: TimePoint, selection: VideoSelection) -> bool:
    return (
        selection.declared_duration_seconds is None
        or timestamp.seconds <= selection.declared_duration_seconds
    )


def _validate_batch(batch: VideoAnalysisBatch, request: VideoAnalysisRequest) -> None:
    if batch.selected_asset_ids != request.selected_asset_ids:
        raise VideoAnalysisError("analysis batch selection does not match the request")
    if batch.sampled_frame_count > request.sampling.max_samples:
        raise VideoAnalysisError("analysis batch exceeds the sample limit")
    if len(batch.keyframes) > request.sampling.max_keyframes:
        raise VideoAnalysisError("analysis batch exceeds the keyframe limit")
    if len(batch.shots) > request.sampling.max_shots:
        raise VideoAnalysisError("analysis batch exceeds the shot limit")
    if len(batch.observations) > MAX_VIDEO_OBSERVATIONS:
        raise VideoAnalysisError("analysis batch exceeds the observation limit")
    selections = {item.asset_id: item for item in request.selections}
    keyframes = {item.keyframe_id: item for item in batch.keyframes}
    observations = {item.observation_id: item for item in batch.observations}
    selected_keyframes: set[str] = set()
    selected_observations: set[str] = set()
    previous_end: dict[str, Decimal] = {}
    for shot in batch.shots:
        selection = selections.get(shot.asset_id)
        if selection is None or shot.source_id != selection.source_id:
            raise VideoAnalysisError("shot source identity does not match the request")
        if not _within(shot.end, selection):
            raise VideoAnalysisError("shot exceeds the declared video duration")
        if shot.end.seconds - shot.start.seconds < request.sampling.minimum_shot_duration:
            raise VideoAnalysisError("shot is shorter than the configured minimum duration")
        prior = previous_end.get(shot.asset_id)
        if prior is not None and shot.start.seconds < prior:
            raise VideoAnalysisError("shots must be ordered and must not overlap per source")
        previous_end[shot.asset_id] = shot.end.seconds
        for keyframe_id in shot.keyframe_ids:
            keyframe = keyframes.get(keyframe_id)
            if keyframe is None or keyframe.asset_id != shot.asset_id:
                raise VideoAnalysisError("shot references an unknown or foreign keyframe")
            if keyframe.source_id != shot.source_id or not _within(keyframe.timestamp, selection):
                raise VideoAnalysisError("keyframe source or duration does not match the shot")
            if not shot.start.seconds <= keyframe.timestamp.seconds < shot.end.seconds:
                raise VideoAnalysisError("keyframe timestamp does not belong to the shot")
            if (
                selection.declared_frame_count is not None
                and keyframe.frame_index is not None
                and keyframe.frame_index > selection.declared_frame_count
            ):
                raise VideoAnalysisError("keyframe index exceeds the declared frame count")
            if keyframe.orientation is not selection.orientation:
                raise VideoAnalysisError("keyframe orientation does not match the source")
            selected_keyframes.add(keyframe_id)
        for observation_id in shot.observation_ids:
            observation = observations.get(observation_id)
            if observation is None or observation.asset_id != shot.asset_id:
                raise VideoAnalysisError("shot references an unknown or foreign observation")
            if observation.source_id != shot.source_id:
                raise VideoAnalysisError("observation source does not match the shot")
            if not shot.start.seconds <= observation.start.seconds:
                raise VideoAnalysisError("observation starts before its shot")
            if observation.end.seconds > shot.end.seconds:
                raise VideoAnalysisError("observation ends after its shot")
            if not _within(observation.end, selection):
                raise VideoAnalysisError("observation exceeds the declared video duration")
            selected_observations.add(observation_id)
    for keyframe in batch.keyframes:
        selection = selections.get(keyframe.asset_id)
        if selection is None or keyframe.source_id != selection.source_id:
            raise VideoAnalysisError("keyframe source identity does not match the request")
    for observation in batch.observations:
        selection = selections.get(observation.asset_id)
        if selection is None or observation.source_id != selection.source_id:
            raise VideoAnalysisError("observation source identity does not match the request")
        if not _within(observation.end, selection):
            raise VideoAnalysisError("observation exceeds the declared video duration")
    if selected_keyframes != set(keyframes) or selected_observations != set(observations):
        raise VideoAnalysisError("analysis outputs must be owned by a declared shot")


def execute_video_analysis(
    adapter: VideoAnalysisAdapter,
    request: VideoAnalysisRequest,
    *,
    runtime: LocalAdapterRuntime | None = None,
    device: LocalDeviceSpec | None = None,
    deterministic_required: bool = False,
    seed: int | None = None,
    cancellation_probe: LocalCancellationProbe | None = None,
    clock: Callable[[], float] | None = None,
    memory_meter: Callable[[], int] | None = None,
) -> VideoAnalysisBatch:
    """Run one explicitly selected video adapter through the M5-01 bounded runtime seam."""

    if not isinstance(adapter, VideoAnalysisAdapter):
        raise VideoAnalysisError("adapter must implement VideoAnalysisAdapter")
    if not isinstance(request, VideoAnalysisRequest):
        raise VideoAnalysisError("request must be a VideoAnalysisRequest")
    device_value = LocalDeviceSpec(LocalDeviceKind.AUTO) if device is None else device
    if not isinstance(device_value, LocalDeviceSpec):
        raise VideoAnalysisError("device must be a LocalDeviceSpec")
    seed_value = request.sampling.seed if seed is None else seed
    execution_request = LocalAdapterExecutionRequest(
        adapter_id=adapter.descriptor.adapter_id,
        task_mode=request.task_mode,
        media_kinds=(MediaKind.VIDEO,),
        reference_count=len(request.selections),
        device=device_value,
        estimated_memory_bytes=request.estimated_input_bytes,
        deterministic_required=deterministic_required,
        seed=seed_value,
        cancellation_required=cancellation_probe is not None,
        input_value=request,
    )
    bridge = _VideoAdapterBridge(adapter)
    if clock is None:
        result = run_local_adapter(
            bridge,
            execution_request,
            runtime=runtime,
            cancellation_probe=cancellation_probe,
            memory_meter=memory_meter,
        )
    else:
        result = run_local_adapter(
            bridge,
            execution_request,
            runtime=runtime,
            cancellation_probe=cancellation_probe,
            clock=clock,
            memory_meter=memory_meter,
        )
    if not isinstance(result.value, VideoAnalysisBatch):
        raise VideoAnalysisError("video adapter result does not contain an analysis batch")
    _validate_batch(result.value, request)
    return result.value


__all__ = [
    "MAX_VIDEO_DIAGNOSTICS",
    "MAX_VIDEO_DIMENSION",
    "MAX_VIDEO_FRAME_COUNT",
    "MAX_VIDEO_INPUT_BYTES",
    "MAX_VIDEO_KEYFRAMES",
    "MAX_VIDEO_OBSERVATIONS",
    "MAX_VIDEO_SAMPLES",
    "MAX_VIDEO_SHOTS",
    "MAX_VIDEO_UNCERTAINTIES",
    "VIDEO_ANALYSIS_SCHEMA",
    "VideoAnalysisAdapter",
    "VideoAnalysisBatch",
    "VideoAnalysisDiagnostic",
    "VideoAnalysisRequest",
    "VideoAnalysisStatus",
    "VideoKeyframe",
    "VideoObservation",
    "VideoObservationKind",
    "VideoOrientation",
    "VideoSamplingConfig",
    "VideoSamplingRequest",
    "VideoSamplingStrategy",
    "VideoSelection",
    "VideoShot",
    "execute_video_analysis",
]
