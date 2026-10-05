"""M12-04 source-owned music, ambience, Foley, and sound-event contracts.

Only bounded, redacted event descriptors and injected benchmark metadata cross this seam.  Samples,
decoders, ComfyUI, Ollama, PANNs, BEATs, CLAP, specialist runtimes, and network clients remain
outside the pure core.  Unknown and absent states never fabricate labels.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, cast, runtime_checkable

from .audio_perception_benchmark import AudioSourceSpan
from .canonical import canonical_fingerprint
from .errors import AudioEventPerceptionError
from .media_admission import PresentationTimestamp

AUDIO_EVENT_PERCEPTION_SCHEMA = "h3.audio.event.v1"
AUDIO_EVENT_BENCHMARK_SCHEMA = "h3.audio.event_benchmark.v1"
MAX_AUDIO_EVENT_EVENTS = 96
MAX_AUDIO_EVENT_REFERENCES = 16
MAX_AUDIO_EVENT_UNCERTAINTIES = 8
MAX_AUDIO_EVENT_DIAGNOSTICS = 32
MAX_AUDIO_EVENT_OUTPUT_BYTES = 65_536
MAX_AUDIO_EVENT_BENCHMARK_CASES = 8
MAX_AUDIO_EVENT_BENCHMARK_THRESHOLDS = 64
MAX_AUDIO_EVENT_BENCHMARK_CANDIDATES = 8

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SENSITIVE = (
    "http://",
    "https://",
    "file://",
    "token=",
    "authorization",
    "bearer ",
    "password",
    "secret",
    "signed",
)


class AudioEventStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"


class AudioEventRoute(str, Enum):
    STATIC_INJECTED = "static_injected"
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"
    SPECIALIST = "specialist"


class AudioEventCandidateFamily(str, Enum):
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"
    SPECIALIST = "specialist"


class AudioEventDisposition(str, Enum):
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


class AudioEventKind(str, Enum):
    SPEECH = "speech"
    MUSIC = "music"
    AMBIENCE = "ambience"
    PHYSICAL_EFFECT = "physical_effect"
    RHYTHM = "rhythm"
    TEMPO = "tempo"
    UNKNOWN = "unknown"


class AudioEventPresence(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class AudioEventReferenceSemantic(str, Enum):
    COPIED_AUDIO = "copied_audio"
    REPERFORMED_AUDIO = "reperformed_audio"
    TIMBRE_REFERENCE = "timbre_reference"
    AMBIENT_REFERENCE = "ambient_reference"


class AudioEventUncertaintyKind(str, Enum):
    LOW_CONFIDENCE = "low_confidence"
    BOUNDARY_UNCERTAIN = "boundary_uncertain"
    UNKNOWN = "unknown"
    ABSENT = "absent"
    MASKED = "masked"
    OVERLAP = "overlap"
    CALIBRATION = "calibration"
    CORRUPT = "corrupt"
    ADVERSARIAL = "adversarial"


class AudioEventCapability(str, Enum):
    TIMESTAMP_OWNERSHIP = "timestamp_ownership"
    MULTILABEL = "multilabel"
    BOUNDARY = "boundary"
    SPEECH = "speech"
    MUSIC = "music"
    AMBIENCE = "ambience"
    PHYSICAL_EFFECTS = "physical_effects"
    RHYTHM = "rhythm"
    TEMPO = "tempo"
    COPIED_AUDIO = "copied_audio"
    REPERFORMED_AUDIO = "reperformed_audio"
    TIMBRE_REFERENCE = "timbre_reference"
    AMBIENT_REFERENCE = "ambient_reference"
    CALIBRATION = "calibration"
    UNKNOWN_ABSTENTION = "unknown_abstention"
    CORRUPTION = "corruption"
    ADVERSARIAL_METADATA = "adversarial_metadata"


class AudioEventCaseKind(str, Enum):
    CLEAN_MULTILABEL = "clean_multilabel"
    MASKED_EVENTS = "masked_events"
    EFFECTS_RHYTHM = "effects_rhythm"
    REFERENCE_SEMANTICS = "reference_semantics"
    UNKNOWN_CORRUPT = "unknown_corrupt"


class AudioEventMetricKind(str, Enum):
    MULTILABEL_QUALITY = "multilabel_quality"
    BOUNDARY_ERROR = "boundary_error"
    CALIBRATION_ERROR = "calibration_error"
    UNKNOWN_ABSTENTION = "unknown_abstention"
    REFERENCE_SEMANTIC_ACCURACY = "reference_semantic_accuracy"
    FABRICATION_VIOLATIONS = "fabrication_violations"


class AudioEventMetricUnit(str, Enum):
    BASIS_POINTS = "basis_points"
    MILLISECONDS = "milliseconds"
    COUNT = "count"


def _id(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise AudioEventPerceptionError(f"{field} must be a bounded identifier")
    if any(marker in value.casefold() for marker in ("/", "\\", *_SENSITIVE)):
        raise AudioEventPerceptionError(f"{field} contains locator or sensitive material")
    return value


def _code(value: object, field: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value.casefold()) is None:
        raise AudioEventPerceptionError(f"{field} must be a lower-case bounded code")
    return value.casefold()


def _version(value: object, field: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise AudioEventPerceptionError(f"{field} must be a numeric version")
    return value


def _fp(value: object, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise AudioEventPerceptionError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _text(value: object, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AudioEventPerceptionError(f"{field} must be bounded non-empty text")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise AudioEventPerceptionError(f"{field} contains a control character")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise AudioEventPerceptionError(f"{field} contains locator or sensitive material")
    return value


def _confidence(value: object, field: str) -> object:
    from decimal import Decimal

    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 1:
        raise AudioEventPerceptionError(f"{field} must be a Decimal between 0 and 1")
    return value


def _positive(value: object, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise AudioEventPerceptionError(f"{field} must be between 1 and {maximum}")
    return value


def _enum(value: object, expected: type[Enum], field: str) -> Enum:
    try:
        return value if isinstance(value, expected) else expected(value)
    except (TypeError, ValueError):
        raise AudioEventPerceptionError(f"{field} is unsupported") from None


def _uncertainties(values: object, field: str) -> tuple[AudioEventUncertainty, ...]:
    if not isinstance(values, tuple) or len(values) > MAX_AUDIO_EVENT_UNCERTAINTIES:
        raise AudioEventPerceptionError(f"{field} must be a bounded tuple")
    if not all(isinstance(value, AudioEventUncertainty) for value in values):
        raise AudioEventPerceptionError(f"{field} contains an invalid value")
    kinds = tuple(value.kind for value in values)
    if len(kinds) != len(set(kinds)):
        raise AudioEventPerceptionError(f"{field} must not duplicate uncertainty kinds")
    return values


@dataclass(frozen=True, slots=True)
class AudioEventReference:
    asset_id: str
    source_id: str
    source_fingerprint: str
    semantic: AudioEventReferenceSemantic | str
    schema: str = AUDIO_EVENT_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.asset_id, "reference asset_id")
        _id(self.source_id, "reference source_id")
        _fp(self.source_fingerprint, "reference source_fingerprint")
        object.__setattr__(
            self,
            "semantic",
            _enum(self.semantic, AudioEventReferenceSemantic, "reference semantic"),
        )
        if self.schema != AUDIO_EVENT_PERCEPTION_SCHEMA:
            raise AudioEventPerceptionError("unsupported audio-event perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
            "semantic": cast(AudioEventReferenceSemantic, self.semantic).value,
        }


@dataclass(frozen=True, slots=True)
class AudioEventRequest:
    asset_id: str
    source_id: str
    source_fingerprint: str
    preprocessing_fingerprint: str
    duration_end: PresentationTimestamp
    references: tuple[AudioEventReference, ...] = ()
    route: AudioEventRoute | str = AudioEventRoute.STATIC_INJECTED
    max_events: int = MAX_AUDIO_EVENT_EVENTS
    schema: str = AUDIO_EVENT_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.asset_id, "request asset_id")
        _id(self.source_id, "request source_id")
        _fp(self.source_fingerprint, "request source_fingerprint")
        _fp(self.preprocessing_fingerprint, "request preprocessing_fingerprint")
        if not isinstance(self.duration_end, PresentationTimestamp) or self.duration_end.ticks <= 0:
            raise AudioEventPerceptionError("request duration_end must be positive")
        if (
            not isinstance(self.references, tuple)
            or len(self.references) > MAX_AUDIO_EVENT_REFERENCES
        ):
            raise AudioEventPerceptionError("request references must be bounded")
        if not all(isinstance(value, AudioEventReference) for value in self.references):
            raise AudioEventPerceptionError("request references contain an invalid value")
        ids = tuple(value.asset_id for value in self.references)
        if len(ids) != len(set(ids)):
            raise AudioEventPerceptionError("request reference asset IDs must be unique")
        object.__setattr__(self, "route", _enum(self.route, AudioEventRoute, "request route"))
        _positive(self.max_events, "request max_events", MAX_AUDIO_EVENT_EVENTS)
        if self.schema != AUDIO_EVENT_PERCEPTION_SCHEMA:
            raise AudioEventPerceptionError("unsupported audio-event perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
            "preprocessing_fingerprint": self.preprocessing_fingerprint,
            "duration_end": self.duration_end.to_wire(),
            "references": [value.to_wire() for value in self.references],
            "route": cast(AudioEventRoute, self.route).value,
            "max_events": self.max_events,
        }


@dataclass(frozen=True, slots=True)
class AudioEventUncertainty:
    kind: AudioEventUncertaintyKind | str
    detail: str
    schema: str = AUDIO_EVENT_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "kind", _enum(self.kind, AudioEventUncertaintyKind, "uncertainty kind")
        )
        _text(self.detail, "uncertainty detail")
        if self.schema != AUDIO_EVENT_PERCEPTION_SCHEMA:
            raise AudioEventPerceptionError("unsupported audio-event perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "kind": cast(AudioEventUncertaintyKind, self.kind).value,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class AudioEvent:
    """One source-owned event claim with explicit presence and reference semantics."""

    event_id: str
    span: AudioSourceSpan
    kind: AudioEventKind | str
    presence: AudioEventPresence | str
    label: str | None
    confidence: object
    boundary_confidence: object
    uncertainties: tuple[AudioEventUncertainty, ...] = ()
    overlap_group: str | None = None
    reference_semantic: AudioEventReferenceSemantic | str | None = None
    reference_asset_id: str | None = None
    schema: str = AUDIO_EVENT_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.event_id, "event_id")
        if not isinstance(self.span, AudioSourceSpan):
            raise AudioEventPerceptionError("event span must be AudioSourceSpan")
        object.__setattr__(self, "kind", _enum(self.kind, AudioEventKind, "event kind"))
        object.__setattr__(
            self, "presence", _enum(self.presence, AudioEventPresence, "event presence")
        )
        _confidence(self.confidence, "event confidence")
        _confidence(self.boundary_confidence, "boundary confidence")
        object.__setattr__(
            self, "uncertainties", _uncertainties(self.uncertainties, "event uncertainties")
        )
        presence = cast(AudioEventPresence, self.presence)
        if presence is AudioEventPresence.PRESENT:
            if self.label is None:
                raise AudioEventPerceptionError("present event requires a label")
            _text(self.label, "event label", 256)
            if self.confidence is None:
                raise AudioEventPerceptionError("present event requires confidence")
        elif self.label is not None:
            raise AudioEventPerceptionError("absent/unknown event cannot fabricate a label")
        if presence is AudioEventPresence.ABSENT and not any(
            value.kind is AudioEventUncertaintyKind.ABSENT for value in self.uncertainties
        ):
            raise AudioEventPerceptionError("absent event requires absent uncertainty")
        if presence is AudioEventPresence.UNKNOWN and not any(
            value.kind is AudioEventUncertaintyKind.UNKNOWN for value in self.uncertainties
        ):
            raise AudioEventPerceptionError("unknown event requires unknown uncertainty")
        if self.overlap_group is not None:
            _id(self.overlap_group, "event overlap_group")
        if self.reference_semantic is None and self.reference_asset_id is not None:
            raise AudioEventPerceptionError("reference asset requires reference semantic")
        if self.reference_semantic is not None:
            object.__setattr__(
                self,
                "reference_semantic",
                _enum(self.reference_semantic, AudioEventReferenceSemantic, "reference semantic"),
            )
            if self.reference_asset_id is None:
                raise AudioEventPerceptionError("reference semantic requires reference asset")
        if self.reference_asset_id is not None:
            _id(self.reference_asset_id, "reference asset_id")
        if self.schema != AUDIO_EVENT_PERCEPTION_SCHEMA:
            raise AudioEventPerceptionError("unsupported audio-event perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "event_id": self.event_id,
            "span": self.span.to_wire(),
            "kind": cast(AudioEventKind, self.kind).value,
            "presence": cast(AudioEventPresence, self.presence).value,
            "label": self.label,
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "boundary_confidence": (
                None if self.boundary_confidence is None else format(self.boundary_confidence, "f")
            ),
            "uncertainties": [value.to_wire() for value in self.uncertainties],
            "overlap_group": self.overlap_group,
            "reference_semantic": (
                None
                if self.reference_semantic is None
                else cast(AudioEventReferenceSemantic, self.reference_semantic).value
            ),
            "reference_asset_id": self.reference_asset_id,
        }


@dataclass(frozen=True, slots=True)
class AudioEventReceipt:
    route: AudioEventRoute | str
    adapter_id: str
    adapter_version: str
    model_id: str
    model_fingerprint: str
    source_fingerprint: str
    preprocessing_fingerprint: str
    network_contacted: bool = False
    decoder_started: bool = False
    schema: str = AUDIO_EVENT_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "route", _enum(self.route, AudioEventRoute, "receipt route"))
        _code(self.adapter_id, "receipt adapter_id")
        _version(self.adapter_version, "receipt adapter_version")
        _id(self.model_id, "receipt model_id")
        _fp(self.model_fingerprint, "receipt model_fingerprint")
        _fp(self.source_fingerprint, "receipt source_fingerprint")
        _fp(self.preprocessing_fingerprint, "receipt preprocessing_fingerprint")
        if not isinstance(self.network_contacted, bool) or not isinstance(
            self.decoder_started, bool
        ):
            raise AudioEventPerceptionError("receipt flags must be boolean")
        if cast(AudioEventRoute, self.route) is AudioEventRoute.STATIC_INJECTED and (
            self.network_contacted or self.decoder_started
        ):
            raise AudioEventPerceptionError(
                "static injected receipt cannot contact network or start decoder"
            )
        if self.schema != AUDIO_EVENT_PERCEPTION_SCHEMA:
            raise AudioEventPerceptionError("unsupported audio-event perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "route": cast(AudioEventRoute, self.route).value,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "model_id": self.model_id,
            "model_fingerprint": self.model_fingerprint,
            "source_fingerprint": self.source_fingerprint,
            "preprocessing_fingerprint": self.preprocessing_fingerprint,
            "network_contacted": self.network_contacted,
            "decoder_started": self.decoder_started,
        }


@dataclass(frozen=True, slots=True)
class AudioEventDocument:
    document_id: str
    status: AudioEventStatus | str
    request: AudioEventRequest
    events: tuple[AudioEvent, ...] = ()
    receipt: AudioEventReceipt | None = None
    diagnostics: tuple[str, ...] = ()
    schema: str = AUDIO_EVENT_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.document_id, "document_id")
        object.__setattr__(self, "status", _enum(self.status, AudioEventStatus, "document status"))
        if not isinstance(self.request, AudioEventRequest):
            raise AudioEventPerceptionError("document request must be AudioEventRequest")
        if not isinstance(self.events, tuple) or len(self.events) > self.request.max_events:
            raise AudioEventPerceptionError("document events exceed request limit")
        if not all(isinstance(value, AudioEvent) for value in self.events):
            raise AudioEventPerceptionError("document events contain an invalid value")
        if len({value.event_id for value in self.events}) != len(self.events):
            raise AudioEventPerceptionError("event IDs must be unique")
        references = {value.asset_id: value for value in self.request.references}
        previous: tuple[int, int, str] | None = None
        previous_event: AudioEvent | None = None
        for event in self.events:
            self._validate_span(event.span, "event")
            current = (event.span.start.ticks, event.span.end.ticks, event.event_id)
            if previous is not None and current < previous:
                raise AudioEventPerceptionError(
                    "events must be deterministically ordered by source PTS"
                )
            if (
                previous_event is not None
                and event.span.start.ticks < previous_event.span.end.ticks
            ):
                if (
                    not previous_event.overlap_group
                    or previous_event.overlap_group != event.overlap_group
                ):
                    raise AudioEventPerceptionError(
                        "overlapping event claims require one explicit group"
                    )
            if event.reference_semantic is not None:
                if event.reference_asset_id is None:
                    raise AudioEventPerceptionError(
                        "event reference semantic requires a reference asset"
                    )
                reference = references.get(event.reference_asset_id)
                if reference is None or reference.semantic is not event.reference_semantic:
                    raise AudioEventPerceptionError(
                        "event reference semantic/ownership differs from request"
                    )
            previous = current
            previous_event = event
        if (
            not isinstance(self.diagnostics, tuple)
            or len(self.diagnostics) > MAX_AUDIO_EVENT_DIAGNOSTICS
        ):
            raise AudioEventPerceptionError("document diagnostics exceed the finite limit")
        for diagnostic in self.diagnostics:
            _text(diagnostic, "diagnostic")
        if self.receipt is not None and not isinstance(self.receipt, AudioEventReceipt):
            raise AudioEventPerceptionError("document receipt is invalid")
        terminal = {
            AudioEventStatus.EMPTY,
            AudioEventStatus.CORRUPT,
            AudioEventStatus.UNSUPPORTED,
            AudioEventStatus.CANCELLED,
        }
        if self.status in terminal and (self.events or self.receipt is not None):
            raise AudioEventPerceptionError(
                "terminal document cannot contain event output or receipt"
            )
        if self.status is AudioEventStatus.COMPLETE:
            if not self.events or self.receipt is None:
                raise AudioEventPerceptionError(
                    "complete event document requires events and receipt"
                )
            if cast(AudioEventRoute, self.receipt.route) is not cast(
                AudioEventRoute, self.request.route
            ):
                raise AudioEventPerceptionError("receipt route does not match request")
            if self.receipt.source_fingerprint != self.request.source_fingerprint:
                raise AudioEventPerceptionError("receipt source fingerprint does not match request")
            if self.receipt.preprocessing_fingerprint != self.request.preprocessing_fingerprint:
                raise AudioEventPerceptionError(
                    "receipt preprocessing fingerprint does not match request"
                )
        if self.schema != AUDIO_EVENT_PERCEPTION_SCHEMA:
            raise AudioEventPerceptionError("unsupported audio-event perception schema")
        if len(self.to_wire_bytes()) > MAX_AUDIO_EVENT_OUTPUT_BYTES:
            raise AudioEventPerceptionError("audio-event document exceeds output limit")

    def _validate_span(self, span: AudioSourceSpan, field: str) -> None:
        if span.asset_id != self.request.asset_id or span.source_id != self.request.source_id:
            raise AudioEventPerceptionError(f"{field} source ownership differs from request")
        if span.source_fingerprint != self.request.source_fingerprint:
            raise AudioEventPerceptionError(f"{field} source fingerprint differs from request")
        if (span.start.time_base_num, span.start.time_base_den) != (
            self.request.duration_end.time_base_num,
            self.request.duration_end.time_base_den,
        ):
            raise AudioEventPerceptionError(f"{field} time base differs from request")
        if span.end.ticks > self.request.duration_end.ticks:
            raise AudioEventPerceptionError(f"{field} exceeds request duration")

    @property
    def complete(self) -> bool:
        return self.status is AudioEventStatus.COMPLETE

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "document_id": self.document_id,
            "status": cast(AudioEventStatus, self.status).value,
            "request": self.request.to_wire(),
            "events": [value.to_wire() for value in self.events],
            "receipt": None if self.receipt is None else self.receipt.to_wire(),
            "diagnostics": list(self.diagnostics),
        }

    def to_wire_bytes(self) -> bytes:
        return json.dumps(
            self.to_wire(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()


@runtime_checkable
class AudioEventCancellationProbe(Protocol):
    def is_cancelled(self) -> bool:
        """Return whether the caller requested cancellation."""


AudioEventProducer = Callable[[AudioEventRequest], AudioEventDocument]


def execute_audio_event(
    producer: AudioEventProducer,
    request: AudioEventRequest,
    *,
    cancellation_probe: AudioEventCancellationProbe | None = None,
) -> AudioEventDocument:
    """Run one explicitly injected event producer without process/network/provider discovery."""

    if not callable(producer):
        raise AudioEventPerceptionError("audio-event producer must be callable")
    if not isinstance(request, AudioEventRequest):
        raise AudioEventPerceptionError("request must be AudioEventRequest")
    if cancellation_probe is not None and (
        not isinstance(cancellation_probe, AudioEventCancellationProbe)
        or cancellation_probe.is_cancelled()
    ):
        raise AudioEventPerceptionError("audio-event execution cancelled")
    document = producer(request)
    if not isinstance(document, AudioEventDocument):
        raise AudioEventPerceptionError("producer returned an invalid audio-event document")
    if cancellation_probe is not None and cancellation_probe.is_cancelled():
        raise AudioEventPerceptionError("audio-event execution cancelled")
    return document


def build_audio_event_abstention(
    request: AudioEventRequest, status: AudioEventStatus | str, diagnostic: str
) -> AudioEventDocument:
    status_value = _enum(status, AudioEventStatus, "abstention status")
    if status_value not in {
        AudioEventStatus.EMPTY,
        AudioEventStatus.CORRUPT,
        AudioEventStatus.UNSUPPORTED,
        AudioEventStatus.CANCELLED,
    }:
        raise AudioEventPerceptionError("abstention status must be terminal")
    if not isinstance(request, AudioEventRequest):
        raise AudioEventPerceptionError("request must be AudioEventRequest")
    return AudioEventDocument(
        f"abstention.{cast(AudioEventStatus, status_value).value}",
        cast(AudioEventStatus, status_value),
        request,
        diagnostics=(diagnostic,),
    )


@dataclass(frozen=True, slots=True)
class AudioEventBenchmarkFixture:
    case_id: str
    kind: AudioEventCaseKind | str
    capabilities: tuple[AudioEventCapability, ...]
    source_fingerprint: str
    annotation_fingerprint: str
    expected_status: AudioEventStatus | str
    should_abstain: bool
    overlap: bool = False
    tags: tuple[str, ...] = ()
    schema: str = AUDIO_EVENT_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.case_id, "fixture case_id")
        object.__setattr__(self, "kind", _enum(self.kind, AudioEventCaseKind, "fixture kind"))
        if not isinstance(self.capabilities, tuple) or not self.capabilities:
            raise AudioEventPerceptionError("fixture capabilities must be non-empty")
        if not all(isinstance(value, AudioEventCapability) for value in self.capabilities):
            raise AudioEventPerceptionError("fixture capabilities contain an invalid value")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise AudioEventPerceptionError("fixture capabilities must be unique")
        _fp(self.source_fingerprint, "fixture source_fingerprint")
        _fp(self.annotation_fingerprint, "fixture annotation_fingerprint")
        object.__setattr__(
            self,
            "expected_status",
            _enum(self.expected_status, AudioEventStatus, "fixture expected_status"),
        )
        if not isinstance(self.should_abstain, bool) or not isinstance(self.overlap, bool):
            raise AudioEventPerceptionError("fixture boolean fields must be boolean")
        if not isinstance(self.tags, tuple) or len(self.tags) > 16:
            raise AudioEventPerceptionError("fixture tags must be bounded")
        for tag in self.tags:
            _code(tag, "fixture tag")
        if self.schema != AUDIO_EVENT_BENCHMARK_SCHEMA:
            raise AudioEventPerceptionError("unsupported audio-event benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "case_id": self.case_id,
            "kind": cast(AudioEventCaseKind, self.kind).value,
            "capabilities": [value.value for value in self.capabilities],
            "source_fingerprint": self.source_fingerprint,
            "annotation_fingerprint": self.annotation_fingerprint,
            "expected_status": cast(AudioEventStatus, self.expected_status).value,
            "should_abstain": self.should_abstain,
            "overlap": self.overlap,
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class AudioEventBenchmarkThreshold:
    metric_id: str
    metric: AudioEventMetricKind | str
    capability: AudioEventCapability
    unit: AudioEventMetricUnit | str
    minimum: int | None = None
    maximum: int | None = None
    schema: str = AUDIO_EVENT_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.metric_id, "threshold metric_id")
        object.__setattr__(
            self, "metric", _enum(self.metric, AudioEventMetricKind, "threshold metric")
        )
        if not isinstance(self.capability, AudioEventCapability):
            raise AudioEventPerceptionError("threshold capability must be AudioEventCapability")
        object.__setattr__(self, "unit", _enum(self.unit, AudioEventMetricUnit, "threshold unit"))
        if (self.minimum is None) == (self.maximum is None):
            raise AudioEventPerceptionError("threshold requires exactly one bound")
        value = self.minimum if self.minimum is not None else self.maximum
        if value is None or isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AudioEventPerceptionError("threshold bound must be a non-negative integer")
        if self.schema != AUDIO_EVENT_BENCHMARK_SCHEMA:
            raise AudioEventPerceptionError("unsupported audio-event benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "metric_id": self.metric_id,
            "metric": cast(AudioEventMetricKind, self.metric).value,
            "capability": self.capability.value,
            "unit": cast(AudioEventMetricUnit, self.unit).value,
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


@dataclass(frozen=True, slots=True)
class AudioEventCandidateProfile:
    candidate_id: str
    family: AudioEventCandidateFamily | str
    adapter_id: str
    adapter_version: str
    model_id: str
    rights_status: str
    capabilities: tuple[AudioEventCapability, ...]
    requires_network: bool
    supports_determinism: bool
    supports_cancellation_cleanup: bool
    disposition: AudioEventDisposition | str
    disposition_reason: str
    schema: str = AUDIO_EVENT_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.candidate_id, "candidate_id")
        object.__setattr__(
            self, "family", _enum(self.family, AudioEventCandidateFamily, "candidate family")
        )
        _code(self.adapter_id, "candidate adapter_id")
        _version(self.adapter_version, "candidate adapter_version")
        _id(self.model_id, "candidate model_id")
        _code(self.rights_status, "candidate rights_status")
        if not isinstance(self.capabilities, tuple) or not self.capabilities:
            raise AudioEventPerceptionError("candidate capabilities must be non-empty")
        if not all(isinstance(value, AudioEventCapability) for value in self.capabilities):
            raise AudioEventPerceptionError("candidate capabilities contain an invalid value")
        if not all(
            isinstance(value, bool)
            for value in (
                self.requires_network,
                self.supports_determinism,
                self.supports_cancellation_cleanup,
            )
        ):
            raise AudioEventPerceptionError("candidate flags must be boolean")
        object.__setattr__(
            self,
            "disposition",
            _enum(self.disposition, AudioEventDisposition, "candidate disposition"),
        )
        _text(self.disposition_reason, "candidate disposition_reason")
        if self.family is AudioEventCandidateFamily.OLLAMA and not self.requires_network:
            raise AudioEventPerceptionError("Ollama candidate must disclose loopback transport")
        if self.schema != AUDIO_EVENT_BENCHMARK_SCHEMA:
            raise AudioEventPerceptionError("unsupported audio-event benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "candidate_id": self.candidate_id,
            "family": cast(AudioEventCandidateFamily, self.family).value,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "model_id": self.model_id,
            "rights_status": self.rights_status,
            "capabilities": [value.value for value in self.capabilities],
            "requires_network": self.requires_network,
            "supports_determinism": self.supports_determinism,
            "supports_cancellation_cleanup": self.supports_cancellation_cleanup,
            "disposition": cast(AudioEventDisposition, self.disposition).value,
            "disposition_reason": self.disposition_reason,
        }


@dataclass(frozen=True, slots=True)
class AudioEventBenchmarkLimits:
    max_cases: int
    max_events: int
    max_references: int
    max_wall_time_seconds: int
    max_total_compute_seconds: int
    max_peak_vram_mb: int
    max_peak_ram_mb: int
    max_output_bytes: int
    max_concurrency: int
    network_allowed: bool = False
    media_upload_allowed: bool = False
    schema: str = AUDIO_EVENT_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        for value, field in (
            (self.max_cases, "limits max_cases"),
            (self.max_events, "limits max_events"),
            (self.max_references, "limits max_references"),
            (self.max_wall_time_seconds, "limits max_wall_time_seconds"),
            (self.max_total_compute_seconds, "limits max_total_compute_seconds"),
            (self.max_peak_vram_mb, "limits max_peak_vram_mb"),
            (self.max_peak_ram_mb, "limits max_peak_ram_mb"),
            (self.max_output_bytes, "limits max_output_bytes"),
            (self.max_concurrency, "limits max_concurrency"),
        ):
            _positive(value, field, 1_000_000_000)
        if self.network_allowed or self.media_upload_allowed:
            raise AudioEventPerceptionError(
                "offline audio-event benchmark cannot allow network or upload"
            )
        if self.schema != AUDIO_EVENT_BENCHMARK_SCHEMA:
            raise AudioEventPerceptionError("unsupported audio-event benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "max_cases": self.max_cases,
            "max_events": self.max_events,
            "max_references": self.max_references,
            "max_wall_time_seconds": self.max_wall_time_seconds,
            "max_total_compute_seconds": self.max_total_compute_seconds,
            "max_peak_vram_mb": self.max_peak_vram_mb,
            "max_peak_ram_mb": self.max_peak_ram_mb,
            "max_output_bytes": self.max_output_bytes,
            "max_concurrency": self.max_concurrency,
            "network_allowed": self.network_allowed,
            "media_upload_allowed": self.media_upload_allowed,
        }


@dataclass(frozen=True, slots=True)
class AudioEventRoutingPolicy:
    preference_order: tuple[AudioEventCandidateFamily, ...]
    automatic_fallback: bool = False
    explicit_selection_required: bool = True
    schema: str = AUDIO_EVENT_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.preference_order, tuple) or tuple(self.preference_order) != tuple(
            AudioEventCandidateFamily
        ):
            raise AudioEventPerceptionError(
                "routing must disclose native, Ollama, and specialist order"
            )
        if self.automatic_fallback or not self.explicit_selection_required:
            raise AudioEventPerceptionError(
                "audio-event routing requires explicit selection and no fallback"
            )
        if self.schema != AUDIO_EVENT_BENCHMARK_SCHEMA:
            raise AudioEventPerceptionError("unsupported audio-event benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "preference_order": [value.value for value in self.preference_order],
            "automatic_fallback": self.automatic_fallback,
            "explicit_selection_required": self.explicit_selection_required,
        }


@dataclass(frozen=True, slots=True)
class AudioEventBenchmarkPlan:
    plan_id: str
    plan_version: str
    fixtures: tuple[AudioEventBenchmarkFixture, ...]
    thresholds: tuple[AudioEventBenchmarkThreshold, ...]
    candidates: tuple[AudioEventCandidateProfile, ...]
    limits: AudioEventBenchmarkLimits
    routing: AudioEventRoutingPolicy
    schema: str = AUDIO_EVENT_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.plan_id, "plan_id")
        _version(self.plan_version, "plan_version")
        if (
            not isinstance(self.fixtures, tuple)
            or not self.fixtures
            or len(self.fixtures) > MAX_AUDIO_EVENT_BENCHMARK_CASES
        ):
            raise AudioEventPerceptionError("plan fixtures must be bounded and non-empty")
        if (
            not isinstance(self.thresholds, tuple)
            or not self.thresholds
            or len(self.thresholds) > MAX_AUDIO_EVENT_BENCHMARK_THRESHOLDS
        ):
            raise AudioEventPerceptionError("plan thresholds must be bounded and non-empty")
        if (
            not isinstance(self.candidates, tuple)
            or not self.candidates
            or len(self.candidates) > MAX_AUDIO_EVENT_BENCHMARK_CANDIDATES
        ):
            raise AudioEventPerceptionError("plan candidates must be bounded and non-empty")
        if not all(isinstance(value, AudioEventBenchmarkFixture) for value in self.fixtures):
            raise AudioEventPerceptionError("plan fixtures contain an invalid value")
        if not all(isinstance(value, AudioEventBenchmarkThreshold) for value in self.thresholds):
            raise AudioEventPerceptionError("plan thresholds contain an invalid value")
        if not all(isinstance(value, AudioEventCandidateProfile) for value in self.candidates):
            raise AudioEventPerceptionError("plan candidates contain an invalid value")
        if len({value.case_id for value in self.fixtures}) != len(self.fixtures):
            raise AudioEventPerceptionError("fixture IDs must be unique")
        if len({value.metric_id for value in self.thresholds}) != len(self.thresholds):
            raise AudioEventPerceptionError("threshold IDs must be unique")
        if len({value.candidate_id for value in self.candidates}) != len(self.candidates):
            raise AudioEventPerceptionError("candidate IDs must be unique")
        covered = {capability for fixture in self.fixtures for capability in fixture.capabilities}
        if covered != set(AudioEventCapability):
            raise AudioEventPerceptionError("audio-event fixture capability coverage is incomplete")
        threshold_capabilities = {value.capability for value in self.thresholds}
        if threshold_capabilities != set(AudioEventCapability):
            raise AudioEventPerceptionError(
                "every audio-event capability requires a frozen threshold"
            )
        if len(self.fixtures) > self.limits.max_cases:
            raise AudioEventPerceptionError("fixtures exceed frozen limits")
        if {value.family for value in self.candidates} != set(AudioEventCandidateFamily):
            raise AudioEventPerceptionError(
                "plan must represent native, Ollama, and specialist families"
            )
        if self.schema != AUDIO_EVENT_BENCHMARK_SCHEMA:
            raise AudioEventPerceptionError("unsupported audio-event benchmark schema")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    @property
    def executable_candidate_ids(self) -> tuple[str, ...]:
        return tuple(
            value.candidate_id
            for value in self.candidates
            if value.disposition is AudioEventDisposition.QUALIFIED
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "fixtures": [value.to_wire() for value in self.fixtures],
            "thresholds": [value.to_wire() for value in self.thresholds],
            "candidates": [value.to_wire() for value in self.candidates],
            "limits": self.limits.to_wire(),
            "routing": self.routing.to_wire(),
        }

    def to_public_summary(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "plan_fingerprint": self.fingerprint,
            "case_count": len(self.fixtures),
            "capability_count": len(AudioEventCapability),
            "threshold_count": len(self.thresholds),
            "candidate_count": len(self.candidates),
            "executable_candidate_ids": list(self.executable_candidate_ids),
            "dispositions": {
                disposition.value: sum(
                    value.disposition is disposition for value in self.candidates
                )
                for disposition in AudioEventDisposition
            },
            "specialist_profiles": [
                value.model_id
                for value in self.candidates
                if value.family is AudioEventCandidateFamily.SPECIALIST
            ],
            "automatic_fallback": self.routing.automatic_fallback,
            "claim_ceiling": "structural_only",
        }


def _seed(value: str) -> str:
    return canonical_fingerprint({"audio_event_fixture": value})


def build_default_audio_event_benchmark_plan() -> AudioEventBenchmarkPlan:
    """Build the frozen M12-04 metadata-only event bake-off benchmark."""

    fixtures = (
        AudioEventBenchmarkFixture(
            "event.clean.multilabel",
            AudioEventCaseKind.CLEAN_MULTILABEL,
            (
                AudioEventCapability.TIMESTAMP_OWNERSHIP,
                AudioEventCapability.MULTILABEL,
                AudioEventCapability.BOUNDARY,
                AudioEventCapability.SPEECH,
                AudioEventCapability.MUSIC,
                AudioEventCapability.AMBIENCE,
            ),
            _seed("event.clean.multilabel.source"),
            _seed("event.clean.multilabel.annotation"),
            AudioEventStatus.COMPLETE,
            False,
            overlap=True,
            tags=("speech", "music", "ambience"),
        ),
        AudioEventBenchmarkFixture(
            "event.masked.events",
            AudioEventCaseKind.MASKED_EVENTS,
            (AudioEventCapability.CALIBRATION, AudioEventCapability.UNKNOWN_ABSTENTION),
            _seed("event.masked.events.source"),
            _seed("event.masked.events.annotation"),
            AudioEventStatus.PARTIAL,
            True,
            tags=("masked", "unknown"),
        ),
        AudioEventBenchmarkFixture(
            "event.effects.rhythm",
            AudioEventCaseKind.EFFECTS_RHYTHM,
            (
                AudioEventCapability.PHYSICAL_EFFECTS,
                AudioEventCapability.RHYTHM,
                AudioEventCapability.TEMPO,
            ),
            _seed("event.effects.rhythm.source"),
            _seed("event.effects.rhythm.annotation"),
            AudioEventStatus.COMPLETE,
            False,
            tags=("foley", "rhythm", "tempo"),
        ),
        AudioEventBenchmarkFixture(
            "event.reference.semantics",
            AudioEventCaseKind.REFERENCE_SEMANTICS,
            (
                AudioEventCapability.COPIED_AUDIO,
                AudioEventCapability.REPERFORMED_AUDIO,
                AudioEventCapability.TIMBRE_REFERENCE,
                AudioEventCapability.AMBIENT_REFERENCE,
            ),
            _seed("event.reference.semantics.source"),
            _seed("event.reference.semantics.annotation"),
            AudioEventStatus.COMPLETE,
            False,
            tags=("copied", "reperformed", "timbre", "ambient"),
        ),
        AudioEventBenchmarkFixture(
            "event.unknown.corrupt",
            AudioEventCaseKind.UNKNOWN_CORRUPT,
            (AudioEventCapability.CORRUPTION, AudioEventCapability.ADVERSARIAL_METADATA),
            _seed("event.unknown.corrupt.source"),
            _seed("event.unknown.corrupt.annotation"),
            AudioEventStatus.CORRUPT,
            True,
            tags=("corrupt", "adversarial"),
        ),
    )
    threshold_specs: dict[
        AudioEventCapability,
        tuple[AudioEventMetricKind, AudioEventMetricUnit, int | None, int | None],
    ] = {
        AudioEventCapability.TIMESTAMP_OWNERSHIP: (
            AudioEventMetricKind.BOUNDARY_ERROR,
            AudioEventMetricUnit.MILLISECONDS,
            None,
            80,
        ),
        AudioEventCapability.MULTILABEL: (
            AudioEventMetricKind.MULTILABEL_QUALITY,
            AudioEventMetricUnit.BASIS_POINTS,
            8_500,
            None,
        ),
        AudioEventCapability.BOUNDARY: (
            AudioEventMetricKind.BOUNDARY_ERROR,
            AudioEventMetricUnit.MILLISECONDS,
            None,
            120,
        ),
        AudioEventCapability.SPEECH: (
            AudioEventMetricKind.MULTILABEL_QUALITY,
            AudioEventMetricUnit.BASIS_POINTS,
            8_500,
            None,
        ),
        AudioEventCapability.MUSIC: (
            AudioEventMetricKind.MULTILABEL_QUALITY,
            AudioEventMetricUnit.BASIS_POINTS,
            8_500,
            None,
        ),
        AudioEventCapability.AMBIENCE: (
            AudioEventMetricKind.MULTILABEL_QUALITY,
            AudioEventMetricUnit.BASIS_POINTS,
            8_000,
            None,
        ),
        AudioEventCapability.PHYSICAL_EFFECTS: (
            AudioEventMetricKind.MULTILABEL_QUALITY,
            AudioEventMetricUnit.BASIS_POINTS,
            7_500,
            None,
        ),
        AudioEventCapability.RHYTHM: (
            AudioEventMetricKind.MULTILABEL_QUALITY,
            AudioEventMetricUnit.BASIS_POINTS,
            7_500,
            None,
        ),
        AudioEventCapability.TEMPO: (
            AudioEventMetricKind.BOUNDARY_ERROR,
            AudioEventMetricUnit.MILLISECONDS,
            None,
            200,
        ),
        AudioEventCapability.COPIED_AUDIO: (
            AudioEventMetricKind.REFERENCE_SEMANTIC_ACCURACY,
            AudioEventMetricUnit.BASIS_POINTS,
            8_000,
            None,
        ),
        AudioEventCapability.REPERFORMED_AUDIO: (
            AudioEventMetricKind.REFERENCE_SEMANTIC_ACCURACY,
            AudioEventMetricUnit.BASIS_POINTS,
            8_000,
            None,
        ),
        AudioEventCapability.TIMBRE_REFERENCE: (
            AudioEventMetricKind.REFERENCE_SEMANTIC_ACCURACY,
            AudioEventMetricUnit.BASIS_POINTS,
            8_000,
            None,
        ),
        AudioEventCapability.AMBIENT_REFERENCE: (
            AudioEventMetricKind.REFERENCE_SEMANTIC_ACCURACY,
            AudioEventMetricUnit.BASIS_POINTS,
            8_000,
            None,
        ),
        AudioEventCapability.CALIBRATION: (
            AudioEventMetricKind.CALIBRATION_ERROR,
            AudioEventMetricUnit.BASIS_POINTS,
            None,
            1_500,
        ),
        AudioEventCapability.UNKNOWN_ABSTENTION: (
            AudioEventMetricKind.UNKNOWN_ABSTENTION,
            AudioEventMetricUnit.BASIS_POINTS,
            9_000,
            None,
        ),
        AudioEventCapability.CORRUPTION: (
            AudioEventMetricKind.FABRICATION_VIOLATIONS,
            AudioEventMetricUnit.COUNT,
            None,
            0,
        ),
        AudioEventCapability.ADVERSARIAL_METADATA: (
            AudioEventMetricKind.FABRICATION_VIOLATIONS,
            AudioEventMetricUnit.COUNT,
            None,
            0,
        ),
    }
    thresholds = tuple(
        AudioEventBenchmarkThreshold(
            f"event.{capability.value}",
            metric,
            capability,
            unit,
            minimum,
            maximum,
        )
        for capability, (metric, unit, minimum, maximum) in threshold_specs.items()
    )
    candidates = (
        AudioEventCandidateProfile(
            "native.comfyui.audio_events",
            AudioEventCandidateFamily.COMFYUI_NATIVE,
            "comfyui_native_audio_events",
            "1.0.0",
            "host-owned-audio-event-capability",
            "metadata_only_reviewed",
            tuple(AudioEventCapability),
            False,
            True,
            False,
            AudioEventDisposition.UNAVAILABLE,
            "pinned ComfyUI audio event model and host lane were not admitted",
        ),
        AudioEventCandidateProfile(
            "fallback.ollama.audio_events",
            AudioEventCandidateFamily.OLLAMA,
            "ollama_native_api",
            "1.0.0",
            "explicitly-selected-loopback-audio-event-model",
            "metadata_only_reviewed",
            tuple(AudioEventCapability),
            True,
            False,
            False,
            AudioEventDisposition.UNAVAILABLE,
            "loopback Ollama server and explicitly selected audio-event model were not started",
        ),
        AudioEventCandidateProfile(
            "specialist.panns",
            AudioEventCandidateFamily.SPECIALIST,
            "specialist_panns",
            "1.0.0",
            "panns-audio-events",
            "metadata_only_reviewed",
            tuple(AudioEventCapability),
            False,
            True,
            True,
            AudioEventDisposition.UNSUPPORTED,
            "PANNs runtime and rights-qualified media bake-off were not selected",
        ),
        AudioEventCandidateProfile(
            "specialist.beats",
            AudioEventCandidateFamily.SPECIALIST,
            "specialist_beats",
            "1.0.0",
            "beats-audio-events",
            "metadata_only_reviewed",
            tuple(AudioEventCapability),
            False,
            True,
            True,
            AudioEventDisposition.UNSUPPORTED,
            "BEATs runtime and rights-qualified media bake-off were not selected",
        ),
        AudioEventCandidateProfile(
            "specialist.clap",
            AudioEventCandidateFamily.SPECIALIST,
            "specialist_clap",
            "1.0.0",
            "clap-audio-events",
            "metadata_only_reviewed",
            tuple(AudioEventCapability),
            False,
            True,
            True,
            AudioEventDisposition.UNSUPPORTED,
            "CLAP runtime and rights-qualified media bake-off were not selected",
        ),
    )
    return AudioEventBenchmarkPlan(
        "m12-04.audio-event-perception-benchmark",
        "1.0.0",
        fixtures,
        thresholds,
        candidates,
        AudioEventBenchmarkLimits(
            5,
            MAX_AUDIO_EVENT_EVENTS,
            MAX_AUDIO_EVENT_REFERENCES,
            60,
            180,
            16_384,
            32_768,
            MAX_AUDIO_EVENT_OUTPUT_BYTES,
            1,
        ),
        AudioEventRoutingPolicy(tuple(AudioEventCandidateFamily)),
    )


__all__ = [
    "AUDIO_EVENT_BENCHMARK_SCHEMA",
    "AUDIO_EVENT_PERCEPTION_SCHEMA",
    "MAX_AUDIO_EVENT_BENCHMARK_CANDIDATES",
    "MAX_AUDIO_EVENT_BENCHMARK_CASES",
    "MAX_AUDIO_EVENT_BENCHMARK_THRESHOLDS",
    "MAX_AUDIO_EVENT_DIAGNOSTICS",
    "MAX_AUDIO_EVENT_EVENTS",
    "MAX_AUDIO_EVENT_OUTPUT_BYTES",
    "MAX_AUDIO_EVENT_REFERENCES",
    "MAX_AUDIO_EVENT_UNCERTAINTIES",
    "AudioEvent",
    "AudioEventBenchmarkFixture",
    "AudioEventBenchmarkLimits",
    "AudioEventBenchmarkPlan",
    "AudioEventBenchmarkThreshold",
    "AudioEventCandidateFamily",
    "AudioEventCandidateProfile",
    "AudioEventCancellationProbe",
    "AudioEventCapability",
    "AudioEventCaseKind",
    "AudioEventDisposition",
    "AudioEventDocument",
    "AudioEventKind",
    "AudioEventMetricKind",
    "AudioEventMetricUnit",
    "AudioEventPresence",
    "AudioEventReceipt",
    "AudioEventReference",
    "AudioEventReferenceSemantic",
    "AudioEventRequest",
    "AudioEventRoute",
    "AudioEventRoutingPolicy",
    "AudioEventStatus",
    "AudioEventUncertainty",
    "AudioEventUncertaintyKind",
    "build_audio_event_abstention",
    "build_default_audio_event_benchmark_plan",
    "execute_audio_event",
]
