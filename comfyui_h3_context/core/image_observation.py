"""Typed image observation and OCR boundaries for optional local assistance.

This module never opens media or interprets OCR as instructions. It retains source identity,
provenance, uncertainty, orientation, normalized regions, and optional source time spans while
delegating execution to an explicitly selected M5-01 local adapter.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Protocol, cast, runtime_checkable

from .constraints import TimePoint
from .contracts import (
    MediaKind,
    ProviderIdentity,
    TaskMode,
    ValidationSeverity,
)
from .errors import ImageObservationError
from .evidence import (
    EvidenceOrigin,
    EvidenceRecord,
    EvidenceSourceKind,
)
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
from .registry import MAX_REFERENCE_IMAGES, ReferenceRegistry

LOCAL_IMAGE_OBSERVATION_SCHEMA = "h3.image.observation.v1"
MAX_IMAGE_INPUT_BYTES = 128 * 1024 * 1024
MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_OBSERVATIONS = 256
MAX_VISIBLE_TEXT_CANDIDATES = 128
MAX_VISIBLE_TEXT_LENGTH = 4_096
MAX_LANGUAGE_HINTS = 16

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_LANGUAGE_PATTERN = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*\Z")


class ImageOrientation(str, Enum):
    """Declared source orientation; no automatic transform is implied."""

    UP = "up"
    ROTATE_90 = "rotate_90"
    ROTATE_180 = "rotate_180"
    ROTATE_270 = "rotate_270"
    MIRROR_HORIZONTAL = "mirror_horizontal"
    MIRROR_VERTICAL = "mirror_vertical"
    UNKNOWN = "unknown"


class ImageObservationKind(str, Enum):
    """Bounded observation labels that remain evidence rather than hard facts."""

    SUBJECT = "subject"
    OBJECT = "object"
    SCENE = "scene"
    ACTION = "action"
    CAMERA = "camera"
    STYLE = "style"


class ImageObservationBatchStatus(str, Enum):
    """Whether the selected image batch was fully processed or visibly degraded."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ImageObservationError(f"{field_name} must be a bounded identifier")
    return value


def _text(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ImageObservationError(f"{field_name} must be a non-empty bounded string")
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ImageObservationError(f"{field_name} contains an unsafe wire code point")
    return value


def _orientation(value: ImageOrientation | str) -> ImageOrientation:
    try:
        return value if isinstance(value, ImageOrientation) else ImageOrientation(value)
    except (TypeError, ValueError):
        raise ImageObservationError("orientation is unsupported") from None


def _positive_int(value: object, field_name: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ImageObservationError(f"{field_name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ImageObservationError(f"{field_name} exceeds its declared limit")
    return value


def _language(value: object, field_name: str = "language") -> str:
    if not isinstance(value, str) or _LANGUAGE_PATTERN.fullmatch(value) is None:
        raise ImageObservationError(f"{field_name} must be a bounded language tag")
    return value


@dataclass(frozen=True, slots=True)
class ImageRegion:
    """Normalized image coordinates in the half-open ``[0, 1]`` rectangle."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        values = (self.x, self.y, self.width, self.height)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise ImageObservationError("image region coordinates must be numbers")
        if any(not isfinite(float(value)) for value in values):
            raise ImageObservationError("image region coordinates must be finite")
        if not 0 <= self.x < 1 or not 0 <= self.y < 1:
            raise ImageObservationError("image region origin must be inside the image")
        if not 0 < self.width <= 1 or not 0 < self.height <= 1:
            raise ImageObservationError("image region size must be positive and bounded")
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ImageObservationError("image region must remain inside the image")

    def to_wire(self) -> dict[str, float]:
        return {
            "x": float(self.x),
            "y": float(self.y),
            "width": float(self.width),
            "height": float(self.height),
        }


@dataclass(frozen=True, slots=True)
class ImageSelection:
    """A selected canonical image asset and safe source metadata for one observation run."""

    asset_id: str
    source_id: str
    orientation: ImageOrientation | str = ImageOrientation.UP
    start: TimePoint | None = None
    end: TimePoint | None = None
    declared_size_bytes: int | None = None
    declared_width: int | None = None
    declared_height: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.asset_id, "asset_id")
        _identifier(self.source_id, "source_id")
        object.__setattr__(self, "orientation", _orientation(self.orientation))
        if self.start is not None and not isinstance(self.start, TimePoint):
            raise ImageObservationError("selection start must be a TimePoint or None")
        if self.end is not None and not isinstance(self.end, TimePoint):
            raise ImageObservationError("selection end must be a TimePoint or None")
        if self.end is not None and self.start is None:
            raise ImageObservationError("selection end requires selection start")
        if (
            self.start is not None
            and self.end is not None
            and self.end.seconds < self.start.seconds
        ):
            raise ImageObservationError("selection end must not precede selection start")
        if self.declared_size_bytes is not None:
            _positive_int(self.declared_size_bytes, "declared_size_bytes", MAX_IMAGE_INPUT_BYTES)
        if (self.declared_width is None) != (self.declared_height is None):
            raise ImageObservationError("declared image width and height must be supplied together")
        for value, field_name in (
            (self.declared_width, "declared_width"),
            (self.declared_height, "declared_height"),
        ):
            if value is not None:
                _positive_int(value, field_name, MAX_IMAGE_DIMENSION)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "orientation": cast(ImageOrientation, self.orientation).value,
            "start": None if self.start is None else self.start.to_wire(),
            "end": None if self.end is None else self.end.to_wire(),
            "declared_size_bytes": self.declared_size_bytes,
            "declared_width": self.declared_width,
            "declared_height": self.declared_height,
        }


@dataclass(frozen=True, slots=True)
class ImageObservationRequest:
    """Explicit selected-image request with no raw media locator or bytes."""

    task_mode: TaskMode
    reference_registry: ReferenceRegistry
    selections: tuple[ImageSelection, ...]
    language_hints: tuple[str, ...] = ()
    max_observations: int = MAX_IMAGE_OBSERVATIONS
    max_text_candidates: int = MAX_VISIBLE_TEXT_CANDIDATES

    def __post_init__(self) -> None:
        if not isinstance(self.task_mode, TaskMode):
            raise ImageObservationError("task_mode must be a TaskMode")
        if not isinstance(self.reference_registry, ReferenceRegistry):
            raise ImageObservationError("reference_registry must be a ReferenceRegistry")
        if (
            not isinstance(self.selections, tuple)
            or not self.selections
            or len(self.selections) > MAX_REFERENCE_IMAGES
            or not all(isinstance(item, ImageSelection) for item in self.selections)
        ):
            raise ImageObservationError("selections must contain one to nine ImageSelection values")
        asset_map = {asset.asset_id: asset for asset in self.reference_registry.assets}
        seen_assets: set[str] = set()
        seen_sources: set[str] = set()
        for item in self.selections:
            if item.asset_id in seen_assets:
                raise ImageObservationError("selected image asset IDs must be unique")
            seen_assets.add(item.asset_id)
            if item.source_id in seen_sources:
                raise ImageObservationError("selected image source IDs must be unique")
            seen_sources.add(item.source_id)
            asset = asset_map.get(item.asset_id)
            if asset is None or asset.kind is not MediaKind.IMAGE:
                raise ImageObservationError("every selection must identify a canonical image asset")
        if (
            not isinstance(self.language_hints, tuple)
            or len(self.language_hints) > MAX_LANGUAGE_HINTS
            or len(set(self.language_hints)) != len(self.language_hints)
        ):
            raise ImageObservationError("language_hints must be a bounded unique tuple")
        for language in self.language_hints:
            _language(language, "language hint")
        for value, field_name, maximum in (
            (self.max_observations, "max_observations", MAX_IMAGE_OBSERVATIONS),
            (self.max_text_candidates, "max_text_candidates", MAX_VISIBLE_TEXT_CANDIDATES),
        ):
            _positive_int(value, field_name, maximum)

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
            "schema": LOCAL_IMAGE_OBSERVATION_SCHEMA,
            "task_mode": self.task_mode.value,
            "selected_asset_ids": list(self.selected_asset_ids),
            "selections": [item.to_public_dict() for item in self.selections],
            "language_hints": list(self.language_hints),
            "max_observations": self.max_observations,
            "max_text_candidates": self.max_text_candidates,
        }


@dataclass(frozen=True, slots=True)
class ImageObservationDiagnostic:
    """Safe non-content diagnostic for a degraded image run."""

    code: str
    message: str
    severity: ValidationSeverity = ValidationSeverity.WARNING

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or _CODE_PATTERN.fullmatch(self.code) is None:
            raise ImageObservationError("diagnostic code must be a bounded code")
        _text(self.message, "diagnostic message", 1024)
        if not isinstance(self.severity, ValidationSeverity):
            raise ImageObservationError("diagnostic severity must be a ValidationSeverity")

    def to_wire(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "severity": self.severity.value}


@dataclass(frozen=True, slots=True)
class ImageObservation:
    """One source-bound, provenance-bearing visual observation."""

    observation_id: str
    asset_id: str
    kind: ImageObservationKind
    evidence: EvidenceRecord
    region: ImageRegion | None
    orientation: ImageOrientation | str

    def __post_init__(self) -> None:
        _identifier(self.observation_id, "observation_id")
        _identifier(self.asset_id, "asset_id")
        if not isinstance(self.kind, ImageObservationKind):
            raise ImageObservationError("observation kind must be an ImageObservationKind")
        if not isinstance(self.evidence, EvidenceRecord):
            raise ImageObservationError("observation evidence must be an EvidenceRecord")
        source = self.evidence.provenance.source
        if self.evidence.origin is not EvidenceOrigin.OBSERVED:
            raise ImageObservationError("image observations must remain OBSERVED evidence")
        if source.kind is not EvidenceSourceKind.MEDIA_ASSET or source.asset_id != self.asset_id:
            raise ImageObservationError("image observation source must match its asset ID")
        if self.evidence.provenance.provider is not ProviderIdentity.LOCAL:
            raise ImageObservationError("image observations require the local provider provenance")
        orientation = _orientation(self.orientation)
        if orientation is ImageOrientation.UNKNOWN and not self.evidence.uncertainties:
            raise ImageObservationError("unknown orientation requires an uncertainty entry")
        object.__setattr__(self, "orientation", orientation)
        if self.region is not None and not isinstance(self.region, ImageRegion):
            raise ImageObservationError("observation region must be an ImageRegion or None")

    @property
    def claim(self) -> str:
        return self.evidence.claim

    def to_wire(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "asset_id": self.asset_id,
            "kind": self.kind.value,
            "evidence": self.evidence.to_wire(),
            "region": None if self.region is None else self.region.to_wire(),
            "orientation": cast(ImageOrientation, self.orientation).value,
        }


@dataclass(frozen=True, slots=True)
class VisibleTextCandidate:
    """Untrusted OCR text candidate; it is never a user-owned hard constraint."""

    candidate_id: str
    evidence: EvidenceRecord
    region: ImageRegion | None
    language: str | None
    reading_order: int
    orientation: ImageOrientation | str
    untrusted: bool = True

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "candidate_id")
        if not isinstance(self.evidence, EvidenceRecord):
            raise ImageObservationError("visible text evidence must be an EvidenceRecord")
        source = self.evidence.provenance.source
        if self.evidence.origin is not EvidenceOrigin.OBSERVED:
            raise ImageObservationError("visible text candidates must remain OBSERVED evidence")
        if source.kind is not EvidenceSourceKind.MEDIA_ASSET or source.asset_id is None:
            raise ImageObservationError("visible text source must identify a media asset")
        if self.evidence.provenance.provider is not ProviderIdentity.LOCAL:
            raise ImageObservationError("visible text candidates require local provenance")
        _text(self.evidence.claim, "visible text", MAX_VISIBLE_TEXT_LENGTH)
        if self.language is not None:
            _language(self.language)
        _positive_int(self.reading_order, "reading_order", MAX_VISIBLE_TEXT_CANDIDATES)
        orientation = _orientation(self.orientation)
        if orientation is ImageOrientation.UNKNOWN and not self.evidence.uncertainties:
            raise ImageObservationError("unknown OCR orientation requires an uncertainty entry")
        if self.region is not None and not isinstance(self.region, ImageRegion):
            raise ImageObservationError("text region must be an ImageRegion or None")
        if self.untrusted is not True:
            raise ImageObservationError("visible text candidates must remain marked untrusted")
        object.__setattr__(self, "orientation", orientation)

    @property
    def text(self) -> str:
        return self.evidence.claim

    def to_wire(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "evidence": self.evidence.to_wire(),
            "region": None if self.region is None else self.region.to_wire(),
            "language": self.language,
            "reading_order": self.reading_order,
            "orientation": cast(ImageOrientation, self.orientation).value,
            "untrusted": self.untrusted,
        }


@dataclass(frozen=True, slots=True)
class ImageObservationBatch:
    """Validated observation/OCR output with explicit degraded statuses."""

    batch_id: str
    schema: str
    status: ImageObservationBatchStatus
    selected_asset_ids: tuple[str, ...]
    observations: tuple[ImageObservation, ...] = ()
    visible_text: tuple[VisibleTextCandidate, ...] = ()
    diagnostics: tuple[ImageObservationDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.batch_id, "batch_id")
        if self.schema != LOCAL_IMAGE_OBSERVATION_SCHEMA:
            raise ImageObservationError("unsupported image observation schema")
        if not isinstance(self.status, ImageObservationBatchStatus):
            raise ImageObservationError("batch status must be an ImageObservationBatchStatus")
        if (
            not isinstance(self.selected_asset_ids, tuple)
            or not self.selected_asset_ids
            or not all(_IDENTIFIER_PATTERN.fullmatch(value) for value in self.selected_asset_ids)
            or len(set(self.selected_asset_ids)) != len(self.selected_asset_ids)
        ):
            raise ImageObservationError("selected_asset_ids must be unique bounded identifiers")
        if not isinstance(self.observations, tuple) or not all(
            isinstance(item, ImageObservation) for item in self.observations
        ):
            raise ImageObservationError("observations must be a tuple of ImageObservation values")
        if not isinstance(self.visible_text, tuple) or not all(
            isinstance(item, VisibleTextCandidate) for item in self.visible_text
        ):
            raise ImageObservationError(
                "visible_text must be a tuple of VisibleTextCandidate values"
            )
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(item, ImageObservationDiagnostic) for item in self.diagnostics
        ):
            raise ImageObservationError(
                "diagnostics must be a tuple of ImageObservationDiagnostic values"
            )
        if len(self.observations) > MAX_IMAGE_OBSERVATIONS:
            raise ImageObservationError("observation output exceeds its finite limit")
        if len(self.visible_text) > MAX_VISIBLE_TEXT_CANDIDATES:
            raise ImageObservationError("visible text output exceeds its finite limit")
        ids = [item.observation_id for item in self.observations]
        text_ids = [item.candidate_id for item in self.visible_text]
        if len(ids) != len(set(ids)) or len(text_ids) != len(set(text_ids)):
            raise ImageObservationError("observation and OCR IDs must be unique")
        selected = set(self.selected_asset_ids)
        if any(item.asset_id not in selected for item in self.observations):
            raise ImageObservationError("observation asset is not selected")
        if any(
            item.evidence.provenance.source.asset_id not in selected for item in self.visible_text
        ):
            raise ImageObservationError("OCR source asset is not selected")
        if self.status in {
            ImageObservationBatchStatus.EMPTY,
            ImageObservationBatchStatus.CORRUPT,
            ImageObservationBatchStatus.UNSUPPORTED,
        } and (self.observations or self.visible_text):
            raise ImageObservationError(
                "degraded empty/corrupt/unsupported batches cannot contain output"
            )

    @property
    def complete(self) -> bool:
        return self.status is ImageObservationBatchStatus.COMPLETE

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "batch_id": self.batch_id,
            "status": self.status.value,
            "complete": self.complete,
            "selected_asset_ids": list(self.selected_asset_ids),
            "observations": [item.to_wire() for item in self.observations],
            "visible_text": [item.to_wire() for item in self.visible_text],
            "diagnostics": [item.to_wire() for item in self.diagnostics],
        }


@runtime_checkable
class ImageObservationAdapter(Protocol):
    """Explicit local adapter seam for image observation/OCR implementations."""

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        """Return a local perception descriptor supporting image media."""

    def observe(
        self, request: ImageObservationRequest, guard: LocalBudgetGuard
    ) -> ImageObservationBatch:
        """Observe selected image references without mutating hard constraints."""


class _ImageAdapterBridge:
    def __init__(self, adapter: ImageObservationAdapter) -> None:
        self._adapter = adapter

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._adapter.descriptor

    def run(
        self,
        request: LocalAdapterExecutionRequest,
        guard: LocalBudgetGuard,
    ) -> LocalAdapterResult:
        if not isinstance(request.input_value, ImageObservationRequest):
            raise ImageObservationError("image adapter input is not an ImageObservationRequest")
        batch = self._adapter.observe(request.input_value, guard)
        if not isinstance(batch, ImageObservationBatch):
            raise ImageObservationError("image adapter returned an invalid observation batch")
        output_bytes = sum(len(item.claim.encode("utf-8")) for item in batch.observations)
        output_bytes += sum(len(item.text.encode("utf-8")) for item in batch.visible_text)
        return LocalAdapterResult(
            adapter_id=self.descriptor.adapter_id,
            adapter_version=self.descriptor.adapter_version,
            device=request.device,
            value=batch,
            output_bytes=output_bytes,
            output_items=len(batch.observations) + len(batch.visible_text),
        )


def _validate_batch(batch: ImageObservationBatch, request: ImageObservationRequest) -> None:
    if batch.selected_asset_ids != request.selected_asset_ids:
        raise ImageObservationError("observation batch selection does not match the request")
    if len(batch.observations) > request.max_observations:
        raise ImageObservationError("observation batch exceeds request output limit")
    if len(batch.visible_text) > request.max_text_candidates:
        raise ImageObservationError("OCR batch exceeds request output limit")
    source_ids = {selection.asset_id: selection.source_id for selection in request.selections}
    for observation in batch.observations:
        source = observation.evidence.provenance.source
        if source.source_id != source_ids[observation.asset_id]:
            raise ImageObservationError("observation source identity does not match the request")
    for candidate in batch.visible_text:
        source = candidate.evidence.provenance.source
        if source.asset_id is None or source.source_id != source_ids[source.asset_id]:
            raise ImageObservationError("OCR source identity does not match the request")


def execute_image_observation(
    adapter: ImageObservationAdapter,
    request: ImageObservationRequest,
    *,
    runtime: LocalAdapterRuntime | None = None,
    device: LocalDeviceSpec | None = None,
    deterministic_required: bool = False,
    seed: int | None = None,
    cancellation_probe: LocalCancellationProbe | None = None,
    clock: Callable[[], float] | None = None,
    memory_meter: Callable[[], int] | None = None,
) -> ImageObservationBatch:
    """Run one explicitly selected observer through the M5-01 bounded runtime seam."""

    if not isinstance(adapter, ImageObservationAdapter):
        raise ImageObservationError("adapter must implement ImageObservationAdapter")
    if not isinstance(request, ImageObservationRequest):
        raise ImageObservationError("request must be an ImageObservationRequest")
    device_value = LocalDeviceSpec(LocalDeviceKind.AUTO) if device is None else device
    if not isinstance(device_value, LocalDeviceSpec):
        raise ImageObservationError("device must be a LocalDeviceSpec")
    estimated_bytes = request.estimated_input_bytes
    execution_request = LocalAdapterExecutionRequest(
        adapter_id=adapter.descriptor.adapter_id,
        task_mode=request.task_mode,
        media_kinds=(MediaKind.IMAGE,),
        reference_count=len(request.selections),
        device=device_value,
        estimated_memory_bytes=estimated_bytes,
        deterministic_required=deterministic_required,
        seed=seed,
        cancellation_required=cancellation_probe is not None,
        input_value=request,
    )
    bridge = _ImageAdapterBridge(adapter)
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
    if not isinstance(result.value, ImageObservationBatch):
        raise ImageObservationError("image adapter result does not contain an observation batch")
    _validate_batch(result.value, request)
    return result.value


__all__ = [
    "ImageObservation",
    "ImageObservationAdapter",
    "ImageObservationBatch",
    "ImageObservationBatchStatus",
    "ImageObservationDiagnostic",
    "ImageObservationKind",
    "ImageObservationRequest",
    "ImageOrientation",
    "ImageRegion",
    "ImageSelection",
    "LOCAL_IMAGE_OBSERVATION_SCHEMA",
    "MAX_IMAGE_INPUT_BYTES",
    "MAX_IMAGE_OBSERVATIONS",
    "MAX_VISIBLE_TEXT_CANDIDATES",
    "MAX_VISIBLE_TEXT_LENGTH",
    "VisibleTextCandidate",
    "execute_image_observation",
]
