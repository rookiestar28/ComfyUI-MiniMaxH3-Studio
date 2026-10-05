"""Bounded audio-analysis contracts for optional local assistance.

The pure core records safe audio metadata and validates injected adapter output. It never opens an
audio source, decodes samples, imports an ASR/diarization runtime, or treats a transcript as a
user-owned exact dialogue constraint.
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
from .errors import AudioAnalysisError
from .evidence import EvidenceOrigin, EvidenceRecord, EvidenceSourceKind
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
from .registry import MAX_CHANNELS, MAX_DURATION_SECONDS, MAX_SAMPLE_RATE, ReferenceRegistry

AUDIO_ANALYSIS_SCHEMA = "h3.audio.analysis.v1"
MAX_AUDIO_INPUT_BYTES = 4 * 1024 * 1024 * 1024
MAX_AUDIO_SAMPLE_RATE = MAX_SAMPLE_RATE
MAX_AUDIO_CHANNELS = MAX_CHANNELS
MAX_AUDIO_SAMPLE_COUNT = 10_000_000_000
MAX_AUDIO_SELECTIONS = 3
MAX_AUDIO_OBSERVATIONS = 1024
MAX_AUDIO_DIAGNOSTICS = 256
MAX_AUDIO_LANGUAGES = 16
MAX_AUDIO_TEXT_LENGTH = 4096
MAX_AUDIO_SPEAKER_HINT_LENGTH = 128

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_LANGUAGE_PATTERN = re.compile(r"[A-Za-z0-9]{2,16}(?:[-_][A-Za-z0-9]{1,16}){0,3}\Z")
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


class AudioObservationKind(str, Enum):
    """Explicit optional audio-analysis families."""

    TRANSCRIPT = "transcript"
    SPEAKER = "speaker"
    VOICE = "voice"
    MUSIC = "music"
    AMBIENCE = "ambience"
    SFX = "sfx"


class AudioAnalysisStatus(str, Enum):
    """Analysis completion state; degraded states never masquerade as complete."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise AudioAnalysisError(f"{field_name} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise AudioAnalysisError(f"{field_name} contains sensitive material")
    return value


def _text(value: object, field_name: str, maximum: int = MAX_AUDIO_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AudioAnalysisError(f"{field_name} must be a bounded non-empty string")
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise AudioAnalysisError(f"{field_name} contains an unsafe wire code point")
    return value


def _positive_int(value: object, field_name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise AudioAnalysisError(f"{field_name} must be between 1 and {maximum}")
    return value


def _non_negative_int(value: object, field_name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise AudioAnalysisError(f"{field_name} must be between 0 and {maximum}")
    return value


def _decimal(value: object, field_name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise AudioAnalysisError(f"{field_name} must be a finite Decimal")
    if positive and value <= 0:
        raise AudioAnalysisError(f"{field_name} must be positive")
    if not positive and value < 0:
        raise AudioAnalysisError(f"{field_name} must be non-negative")
    return value


def _kind(value: AudioObservationKind | str) -> AudioObservationKind:
    try:
        return value if isinstance(value, AudioObservationKind) else AudioObservationKind(value)
    except (TypeError, ValueError):
        raise AudioAnalysisError("audio observation kind is unsupported") from None


def _kind_tuple(values: object) -> tuple[AudioObservationKind, ...]:
    if not isinstance(values, tuple) or not 0 < len(values) <= len(AudioObservationKind):
        raise AudioAnalysisError("enabled_kinds must be a non-empty bounded tuple")
    result = tuple(_kind(value) for value in values)
    if len(result) != len(set(result)):
        raise AudioAnalysisError("enabled_kinds must not contain duplicates")
    return result


def _language(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _LANGUAGE_PATTERN.fullmatch(value) is None:
        raise AudioAnalysisError(f"{field_name} must be a bounded language tag")
    return value


def _optional_language(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _language(value, field_name)


def _optional_hint(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    hint = _text(value, field_name, MAX_AUDIO_SPEAKER_HINT_LENGTH)
    if any(marker in hint.casefold() for marker in _SENSITIVE_MARKERS):
        raise AudioAnalysisError(f"{field_name} contains sensitive material")
    return hint


@dataclass(frozen=True, slots=True)
class AudioSelection:
    """Canonical audio identity plus caller-declared metadata, never a locator or media object."""

    asset_id: str
    source_id: str
    declared_size_bytes: int | None = None
    declared_duration_seconds: Decimal | None = None
    declared_sample_rate: int | None = None
    declared_channels: int | None = None
    declared_sample_count: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.asset_id, "audio asset_id")
        _identifier(self.source_id, "audio source_id")
        if self.declared_size_bytes is not None:
            _positive_int(self.declared_size_bytes, "declared_size_bytes", MAX_AUDIO_INPUT_BYTES)
        if self.declared_duration_seconds is not None:
            duration = _decimal(
                self.declared_duration_seconds, "declared_duration_seconds", positive=True
            )
            if duration > MAX_DURATION_SECONDS:
                raise AudioAnalysisError("declared_duration_seconds exceeds the media limit")
        if self.declared_sample_rate is not None:
            _positive_int(self.declared_sample_rate, "declared_sample_rate", MAX_AUDIO_SAMPLE_RATE)
        if self.declared_channels is not None:
            _positive_int(self.declared_channels, "declared_channels", MAX_AUDIO_CHANNELS)
        if self.declared_sample_count is not None:
            _positive_int(
                self.declared_sample_count, "declared_sample_count", MAX_AUDIO_SAMPLE_COUNT
            )

    def to_wire(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "declared_size_bytes": self.declared_size_bytes,
            "declared_duration_seconds": (
                None
                if self.declared_duration_seconds is None
                else format(self.declared_duration_seconds, "f")
            ),
            "declared_sample_rate": self.declared_sample_rate,
            "declared_channels": self.declared_channels,
            "declared_sample_count": self.declared_sample_count,
        }


@dataclass(frozen=True, slots=True)
class AudioAnalysisConfig:
    """Finite adapter settings with explicit optional analysis families."""

    enabled_kinds: tuple[AudioObservationKind | str, ...]
    max_observations: int = 128
    max_transcript_characters: int = MAX_AUDIO_TEXT_LENGTH
    language_hints: tuple[str, ...] = ()
    seed: int | None = None
    schema: str = AUDIO_ANALYSIS_SCHEMA

    def __post_init__(self) -> None:
        kinds = _kind_tuple(self.enabled_kinds)
        object.__setattr__(self, "enabled_kinds", kinds)
        _positive_int(self.max_observations, "max_observations", MAX_AUDIO_OBSERVATIONS)
        _positive_int(
            self.max_transcript_characters,
            "max_transcript_characters",
            MAX_AUDIO_TEXT_LENGTH,
        )
        if (
            not isinstance(self.language_hints, tuple)
            or len(self.language_hints) > MAX_AUDIO_LANGUAGES
        ):
            raise AudioAnalysisError("language_hints must be a bounded tuple")
        languages = tuple(_language(value, "language hint") for value in self.language_hints)
        if len(languages) != len(set(language.casefold() for language in languages)):
            raise AudioAnalysisError("language_hints must not contain duplicates")
        object.__setattr__(self, "language_hints", languages)
        if self.seed is not None and (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not -(2**63) <= self.seed <= 2**63 - 1
        ):
            raise AudioAnalysisError("seed must be a signed 64-bit integer")
        if self.schema != AUDIO_ANALYSIS_SCHEMA:
            raise AudioAnalysisError("unsupported audio analysis schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "enabled_kinds": [
                cast(AudioObservationKind, kind).value for kind in self.enabled_kinds
            ],
            "max_observations": self.max_observations,
            "max_transcript_characters": self.max_transcript_characters,
            "language_hints": list(self.language_hints),
            "seed": self.seed,
        }


@dataclass(frozen=True, slots=True)
class AudioAnalysisRequest:
    """Explicit Full-Reference audio selection and analysis request."""

    task_mode: TaskMode
    reference_registry: ReferenceRegistry
    selections: tuple[AudioSelection, ...]
    sampling: AudioAnalysisConfig

    def __post_init__(self) -> None:
        if self.task_mode is not TaskMode.REF2VA:
            raise AudioAnalysisError("audio analysis currently requires REF2VA")
        if not isinstance(self.reference_registry, ReferenceRegistry):
            raise AudioAnalysisError("reference_registry must be a ReferenceRegistry")
        if (
            not isinstance(self.selections, tuple)
            or not self.selections
            or len(self.selections) > MAX_AUDIO_SELECTIONS
            or not all(isinstance(item, AudioSelection) for item in self.selections)
        ):
            raise AudioAnalysisError(
                f"selections must contain one to {MAX_AUDIO_SELECTIONS} AudioSelection values"
            )
        if not isinstance(self.sampling, AudioAnalysisConfig):
            raise AudioAnalysisError("sampling must be an AudioAnalysisConfig")
        assets = {asset.asset_id: asset for asset in self.reference_registry.assets}
        asset_ids = tuple(item.asset_id for item in self.selections)
        source_ids = tuple(item.source_id for item in self.selections)
        if len(asset_ids) != len(set(asset_ids)) or len(source_ids) != len(set(source_ids)):
            raise AudioAnalysisError("audio selections must have unique asset and source IDs")
        for item in self.selections:
            asset = assets.get(item.asset_id)
            if asset is None or asset.kind is not MediaKind.AUDIO:
                raise AudioAnalysisError("every selection must identify a canonical audio asset")

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
            "schema": AUDIO_ANALYSIS_SCHEMA,
            "task_mode": self.task_mode.value,
            "selected_asset_ids": list(self.selected_asset_ids),
            "selections": [item.to_wire() for item in self.selections],
            "sampling": self.sampling.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class AudioObservation:
    """One source-timestamped observed audio cue kept separate from user constraints."""

    observation_id: str
    asset_id: str
    kind: AudioObservationKind
    evidence: EvidenceRecord
    language: str | None = None
    speaker_hint: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.observation_id, "audio observation_id")
        _identifier(self.asset_id, "audio observation asset_id")
        if not isinstance(self.kind, AudioObservationKind):
            raise AudioAnalysisError("audio observation kind must be an AudioObservationKind")
        if not isinstance(self.evidence, EvidenceRecord):
            raise AudioAnalysisError("audio observation evidence must be an EvidenceRecord")
        source = self.evidence.provenance.source
        if self.evidence.origin is not EvidenceOrigin.OBSERVED:
            raise AudioAnalysisError("audio observations must remain OBSERVED evidence")
        if source.kind is not EvidenceSourceKind.MEDIA_ASSET or source.asset_id != self.asset_id:
            raise AudioAnalysisError("audio observation source must match its asset ID")
        if self.evidence.provenance.provider is not ProviderIdentity.LOCAL:
            raise AudioAnalysisError("audio observations require local provenance")
        if source.start is None or source.end is None:
            raise AudioAnalysisError("audio observations require a source timestamp span")
        if source.end.seconds <= source.start.seconds:
            raise AudioAnalysisError("audio observation end must be after its start")
        _text(self.evidence.claim, "audio observation claim")
        object.__setattr__(
            self, "language", _optional_language(self.language, "observation language")
        )
        object.__setattr__(self, "speaker_hint", _optional_hint(self.speaker_hint, "speaker hint"))

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
            raise AudioAnalysisError("audio observation start timestamp is missing")
        return source.start

    @property
    def end(self) -> TimePoint:
        source = self.evidence.provenance.source
        if source.end is None:
            raise AudioAnalysisError("audio observation end timestamp is missing")
        return source.end

    def to_wire(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "asset_id": self.asset_id,
            "kind": self.kind.value,
            "language": self.language,
            "speaker_hint": self.speaker_hint,
            "evidence": self.evidence.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class AudioAnalysisDiagnostic:
    """Safe non-content diagnostic for a degraded audio analysis run."""

    code: str
    message: str
    severity: ValidationSeverity = ValidationSeverity.WARNING

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or _CODE_PATTERN.fullmatch(self.code) is None:
            raise AudioAnalysisError("diagnostic code must be a bounded code")
        _text(self.message, "diagnostic message", 1024)
        if not isinstance(self.severity, ValidationSeverity):
            raise AudioAnalysisError("diagnostic severity must be a ValidationSeverity")

    def to_wire(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "severity": self.severity.value}


@dataclass(frozen=True, slots=True)
class AudioAnalysisBatch:
    """Validated audio-analysis output with explicit degraded states and finite collections."""

    batch_id: str
    schema: str
    status: AudioAnalysisStatus
    selected_asset_ids: tuple[str, ...]
    analyzed_sample_count: int = 0
    observations: tuple[AudioObservation, ...] = ()
    diagnostics: tuple[AudioAnalysisDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.batch_id, "batch_id")
        if self.schema != AUDIO_ANALYSIS_SCHEMA:
            raise AudioAnalysisError("unsupported audio analysis schema")
        if not isinstance(self.status, AudioAnalysisStatus):
            raise AudioAnalysisError("batch status must be an AudioAnalysisStatus")
        selected = self._selected_ids()
        if not selected:
            raise AudioAnalysisError("selected_asset_ids must not be empty")
        _non_negative_int(
            self.analyzed_sample_count, "analyzed_sample_count", MAX_AUDIO_SAMPLE_COUNT
        )
        if not isinstance(self.observations, tuple) or not all(
            isinstance(item, AudioObservation) for item in self.observations
        ):
            raise AudioAnalysisError("observations must contain AudioObservation values")
        if not isinstance(self.diagnostics, tuple) or len(self.diagnostics) > MAX_AUDIO_DIAGNOSTICS:
            raise AudioAnalysisError("diagnostics must be a bounded tuple")
        if not all(isinstance(item, AudioAnalysisDiagnostic) for item in self.diagnostics):
            raise AudioAnalysisError("diagnostics must contain AudioAnalysisDiagnostic values")
        if len(self.observations) > MAX_AUDIO_OBSERVATIONS:
            raise AudioAnalysisError("observations exceed the finite limit")
        identifiers = tuple(item.observation_id for item in self.observations)
        if len(identifiers) != len(set(identifiers)):
            raise AudioAnalysisError("audio analysis observation IDs must be unique")
        if self.status in {
            AudioAnalysisStatus.EMPTY,
            AudioAnalysisStatus.CORRUPT,
            AudioAnalysisStatus.UNSUPPORTED,
        } and (self.analyzed_sample_count or self.observations):
            raise AudioAnalysisError(
                "degraded empty/corrupt/unsupported batches cannot contain output"
            )
        if self.status is AudioAnalysisStatus.COMPLETE and self.analyzed_sample_count <= 0:
            raise AudioAnalysisError("complete audio analysis requires analyzed samples")

    def _selected_ids(self) -> tuple[str, ...]:
        if not isinstance(self.selected_asset_ids, tuple):
            raise AudioAnalysisError("selected_asset_ids must be a tuple")
        selected = tuple(
            _identifier(value, "selected asset ID") for value in self.selected_asset_ids
        )
        if len(selected) > MAX_AUDIO_SELECTIONS:
            raise AudioAnalysisError("selected_asset_ids exceed the finite limit")
        if len(selected) != len(set(selected)):
            raise AudioAnalysisError("selected_asset_ids must not contain duplicates")
        return selected

    @property
    def complete(self) -> bool:
        return self.status is AudioAnalysisStatus.COMPLETE

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "batch_id": self.batch_id,
            "status": self.status.value,
            "complete": self.complete,
            "selected_asset_ids": list(self.selected_asset_ids),
            "analyzed_sample_count": self.analyzed_sample_count,
            "observations": [item.to_wire() for item in self.observations],
            "diagnostics": [item.to_wire() for item in self.diagnostics],
        }


@runtime_checkable
class AudioAnalysisAdapter(Protocol):
    """Explicit local adapter seam for audio analysis."""

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        """Return a local perception descriptor supporting audio media."""

    def analyze(self, request: AudioAnalysisRequest, guard: LocalBudgetGuard) -> AudioAnalysisBatch:
        """Analyze selected audio without mutating hard constraints."""


class _AudioAdapterBridge:
    def __init__(self, adapter: AudioAnalysisAdapter) -> None:
        self._adapter = adapter

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._adapter.descriptor

    def run(
        self,
        request: LocalAdapterExecutionRequest,
        guard: LocalBudgetGuard,
    ) -> LocalAdapterResult:
        if not isinstance(request.input_value, AudioAnalysisRequest):
            raise AudioAnalysisError("audio adapter input is not an AudioAnalysisRequest")
        batch = self._adapter.analyze(request.input_value, guard)
        if not isinstance(batch, AudioAnalysisBatch):
            raise AudioAnalysisError("audio adapter returned an invalid analysis batch")
        if batch.schema != self.descriptor.output_schema:
            raise AudioAnalysisError("audio adapter returned an unexpected output schema")
        output_bytes = sum(
            len(item.claim.encode("utf-8"))
            + (0 if item.language is None else len(item.language.encode("utf-8")))
            + (0 if item.speaker_hint is None else len(item.speaker_hint.encode("utf-8")))
            for item in batch.observations
        ) + sum(len(item.message.encode("utf-8")) for item in batch.diagnostics)
        return LocalAdapterResult(
            adapter_id=self.descriptor.adapter_id,
            adapter_version=self.descriptor.adapter_version,
            device=request.device,
            value=batch,
            output_bytes=output_bytes,
            output_items=len(batch.observations),
        )


def _within(timestamp: TimePoint, selection: AudioSelection) -> bool:
    return (
        selection.declared_duration_seconds is None
        or timestamp.seconds <= selection.declared_duration_seconds
    )


def _validate_batch(batch: AudioAnalysisBatch, request: AudioAnalysisRequest) -> None:
    if batch.selected_asset_ids != request.selected_asset_ids:
        raise AudioAnalysisError("analysis batch selection does not match the request")
    if batch.analyzed_sample_count > MAX_AUDIO_SAMPLE_COUNT:
        raise AudioAnalysisError("analysis batch exceeds the sample limit")
    selections = {item.asset_id: item for item in request.selections}
    if any(
        selection.declared_sample_count is not None
        and batch.analyzed_sample_count > selection.declared_sample_count
        for selection in request.selections
    ):
        raise AudioAnalysisError("analyzed samples exceed the declared sample count")
    previous: dict[str, tuple[Decimal, Decimal, str]] = {}
    for observation in batch.observations:
        selection = selections.get(observation.asset_id)
        if selection is None or observation.source_id != selection.source_id:
            raise AudioAnalysisError("observation source identity does not match the request")
        if observation.kind not in request.sampling.enabled_kinds:
            raise AudioAnalysisError("adapter returned an unrequested observation kind")
        if (
            observation.kind is AudioObservationKind.TRANSCRIPT
            and len(observation.claim) > request.sampling.max_transcript_characters
        ):
            raise AudioAnalysisError("transcript exceeds the configured character limit")
        if not _within(observation.end, selection):
            raise AudioAnalysisError("observation exceeds the declared audio duration")
        key = (observation.start.seconds, observation.end.seconds, observation.observation_id)
        prior = previous.get(observation.asset_id)
        if prior is not None and key < prior:
            raise AudioAnalysisError(
                "audio observations must be deterministically ordered per source"
            )
        previous[observation.asset_id] = key


def execute_audio_analysis(
    adapter: AudioAnalysisAdapter,
    request: AudioAnalysisRequest,
    *,
    runtime: LocalAdapterRuntime | None = None,
    device: LocalDeviceSpec | None = None,
    deterministic_required: bool = False,
    seed: int | None = None,
    cancellation_probe: LocalCancellationProbe | None = None,
    clock: Callable[[], float] | None = None,
    memory_meter: Callable[[], int] | None = None,
) -> AudioAnalysisBatch:
    """Run one explicitly selected audio adapter through the M5-01 bounded runtime seam."""

    if not isinstance(adapter, AudioAnalysisAdapter):
        raise AudioAnalysisError("adapter must implement AudioAnalysisAdapter")
    if not isinstance(request, AudioAnalysisRequest):
        raise AudioAnalysisError("request must be an AudioAnalysisRequest")
    device_value = LocalDeviceSpec(LocalDeviceKind.AUTO) if device is None else device
    if not isinstance(device_value, LocalDeviceSpec):
        raise AudioAnalysisError("device must be a LocalDeviceSpec")
    seed_value = request.sampling.seed if seed is None else seed
    execution_request = LocalAdapterExecutionRequest(
        adapter_id=adapter.descriptor.adapter_id,
        task_mode=request.task_mode,
        media_kinds=(MediaKind.AUDIO,),
        reference_count=len(request.selections),
        device=device_value,
        estimated_memory_bytes=request.estimated_input_bytes,
        deterministic_required=deterministic_required,
        seed=seed_value,
        cancellation_required=cancellation_probe is not None,
        input_value=request,
    )
    bridge = _AudioAdapterBridge(adapter)
    result = run_local_adapter(
        bridge,
        execution_request,
        runtime=runtime,
        cancellation_probe=cancellation_probe,
        **({"clock": clock} if clock is not None else {}),
        memory_meter=memory_meter,
    )
    if not isinstance(result.value, AudioAnalysisBatch):
        raise AudioAnalysisError("audio adapter result does not contain an analysis batch")
    _validate_batch(result.value, request)
    if len(result.value.observations) > request.sampling.max_observations:
        raise AudioAnalysisError("analysis batch exceeds the observation limit")
    return result.value


__all__ = [
    "AUDIO_ANALYSIS_SCHEMA",
    "MAX_AUDIO_CHANNELS",
    "MAX_AUDIO_DIAGNOSTICS",
    "MAX_AUDIO_INPUT_BYTES",
    "MAX_AUDIO_LANGUAGES",
    "MAX_AUDIO_OBSERVATIONS",
    "MAX_AUDIO_SAMPLE_COUNT",
    "MAX_AUDIO_SAMPLE_RATE",
    "MAX_AUDIO_SELECTIONS",
    "MAX_AUDIO_SPEAKER_HINT_LENGTH",
    "MAX_AUDIO_TEXT_LENGTH",
    "AudioAnalysisAdapter",
    "AudioAnalysisBatch",
    "AudioAnalysisConfig",
    "AudioAnalysisDiagnostic",
    "AudioAnalysisRequest",
    "AudioAnalysisStatus",
    "AudioObservation",
    "AudioObservationKind",
    "AudioSelection",
    "execute_audio_analysis",
]
