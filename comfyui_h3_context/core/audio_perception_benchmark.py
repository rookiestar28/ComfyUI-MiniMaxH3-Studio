"""M12-01 source-clock audio extraction and normalization contracts.

Only redacted metadata and injected segment descriptors cross this seam.  Samples, decoders,
ComfyUI audio nodes, Ollama, and specialist runtimes remain outside the pure core.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from .canonical import canonical_fingerprint
from .errors import AudioPreprocessError
from .media_admission import PresentationTimestamp

AUDIO_PERCEPTION_BENCHMARK_SCHEMA = "h3.audio.perception_benchmark.v1"
MAX_AUDIO_BENCHMARK_FIXTURES = 8
MAX_AUDIO_BENCHMARK_SEGMENTS = 64
MAX_AUDIO_BENCHMARK_GAPS = 16
MAX_AUDIO_BENCHMARK_THRESHOLDS = 128
MAX_AUDIO_BENCHMARK_CANDIDATES = 8
MAX_AUDIO_PREPROCESS_OUTPUT_BYTES = 65_536
MAX_AUDIO_SAMPLE_RATE = 96_000
MAX_AUDIO_CHANNELS = 32
MAX_AUDIO_DURATION_TICKS = 120_000

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SENSITIVE = ("http://", "https://", "file://", "/", "\\", "token=", "authorization", "secret")


class AudioCapability(str, Enum):
    SOURCE_CLOCK = "source_clock"
    CHANNELS = "channels"
    SAMPLE_RATE = "sample_rate"
    GAPS = "gaps"
    OVERLAP = "overlap"
    VIDEO_AUDIO_OWNERSHIP = "video_audio_ownership"
    SPEECH = "speech"
    MULTILINGUAL_DIALOGUE = "multilingual_dialogue"
    SILENCE = "silence"
    NOISE = "noise"
    MUSIC = "music"
    AMBIENCE = "ambience"
    SFX = "sfx"
    CLIPPING = "clipping"
    CORRUPTION = "corruption"
    ADVERSARIAL_METADATA = "adversarial_metadata"


class AudioGapKind(str, Enum):
    SILENCE = "silence"
    MISSING = "missing"
    DISCONTINUITY = "discontinuity"


class AudioPreprocessStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"


class AudioRoute(str, Enum):
    STATIC_INJECTED = "static_injected"
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"
    SPECIALIST = "specialist"


class AudioCandidateFamily(str, Enum):
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"
    SPECIALIST = "specialist"


class AudioDisposition(str, Enum):
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


class AudioMetricKind(str, Enum):
    TIMESTAMP_PRESERVATION = "timestamp_preservation"
    SAMPLE_RATE_PRESERVATION = "sample_rate_preservation"
    CHANNEL_OWNERSHIP = "channel_ownership"
    GAP_RECALL = "gap_recall"
    OVERLAP_RECALL = "overlap_recall"
    CORRUPTION_REJECTION = "corruption_rejection"
    ADVERSARIAL_METADATA_REJECTION = "adversarial_metadata_rejection"


def _id(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise AudioPreprocessError(f"{field} must be a bounded identifier")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SENSITIVE):
        raise AudioPreprocessError(f"{field} contains locator or sensitive material")
    return value


def _code(value: object, field: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value.casefold()) is None:
        raise AudioPreprocessError(f"{field} must be a lower-case code")
    return value.casefold()


def _version(value: object, field: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise AudioPreprocessError(f"{field} must be a numeric version")
    return value


def _fp(value: object, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise AudioPreprocessError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _text(value: object, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AudioPreprocessError(f"{field} must be bounded non-empty text")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise AudioPreprocessError(f"{field} contains locator or sensitive material")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise AudioPreprocessError(f"{field} contains a control character")
    return value


def _positive(value: object, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise AudioPreprocessError(f"{field} must be between 1 and {maximum}")
    return value


def _non_negative(value: object, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise AudioPreprocessError(f"{field} must be between 0 and {maximum}")
    return value


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise AudioPreprocessError(f"{field} must be boolean")
    return value


def _span_valid(start: PresentationTimestamp, end: PresentationTimestamp, field: str) -> None:
    if not isinstance(start, PresentationTimestamp) or not isinstance(end, PresentationTimestamp):
        raise AudioPreprocessError(f"{field} endpoints must be PresentationTimestamp")
    if (start.time_base_num, start.time_base_den) != (end.time_base_num, end.time_base_den):
        raise AudioPreprocessError(f"{field} endpoints must share one time base")
    if end.ticks <= start.ticks:
        raise AudioPreprocessError(f"{field} end must be after start")


def _enum(value: object, expected: type[Enum], field: str) -> None:
    if not isinstance(value, expected):
        raise AudioPreprocessError(f"{field} must be a {expected.__name__}")


def _enum_tuple(values: object, expected: type[Enum], field: str, maximum: int) -> tuple[Enum, ...]:
    if not isinstance(values, tuple) or not values or len(values) > maximum:
        raise AudioPreprocessError(f"{field} must be a bounded non-empty tuple")
    if not all(isinstance(value, expected) for value in values):
        raise AudioPreprocessError(f"{field} contains an invalid value")
    if len(values) != len(set(values)):
        raise AudioPreprocessError(f"{field} must not contain duplicates")
    return values


@dataclass(frozen=True, slots=True)
class AudioSourceSpan:
    """Source-owned half-open interval with authoritative integer PTS."""

    asset_id: str
    source_id: str
    source_fingerprint: str
    start: PresentationTimestamp
    end: PresentationTimestamp
    schema: str = AUDIO_PERCEPTION_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.asset_id, "span asset_id")
        _id(self.source_id, "span source_id")
        _fp(self.source_fingerprint, "span source_fingerprint")
        _span_valid(self.start, self.end, "audio span")
        if self.schema != AUDIO_PERCEPTION_BENCHMARK_SCHEMA:
            raise AudioPreprocessError("unsupported audio benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
            "start": self.start.to_wire(),
            "end": self.end.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class AudioGap:
    """Explicit source gap; it is never silently converted to silence or filled."""

    gap_id: str
    span: AudioSourceSpan
    kind: AudioGapKind
    schema: str = AUDIO_PERCEPTION_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.gap_id, "gap_id")
        if not isinstance(self.span, AudioSourceSpan):
            raise AudioPreprocessError("gap span must be AudioSourceSpan")
        _enum(self.kind, AudioGapKind, "gap kind")
        if self.schema != AUDIO_PERCEPTION_BENCHMARK_SCHEMA:
            raise AudioPreprocessError("unsupported audio benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "gap_id": self.gap_id,
            "span": self.span.to_wire(),
            "kind": self.kind.value,
        }


@dataclass(frozen=True, slots=True)
class AudioSegment:
    """Normalized source-owned segment; audio payload remains runtime-owned."""

    segment_id: str
    span: AudioSourceSpan
    sample_rate: int
    channels: int
    channel_layout: str
    payload_fingerprint: str
    preprocessing_fingerprint: str
    overlap_group: str | None = None
    schema: str = AUDIO_PERCEPTION_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.segment_id, "segment_id")
        if not isinstance(self.span, AudioSourceSpan):
            raise AudioPreprocessError("segment span must be AudioSourceSpan")
        _positive(self.sample_rate, "segment sample_rate", MAX_AUDIO_SAMPLE_RATE)
        _positive(self.channels, "segment channels", MAX_AUDIO_CHANNELS)
        _code(self.channel_layout, "segment channel_layout")
        _fp(self.payload_fingerprint, "segment payload_fingerprint")
        _fp(self.preprocessing_fingerprint, "segment preprocessing_fingerprint")
        if self.overlap_group is not None:
            _id(self.overlap_group, "segment overlap_group")
        if self.schema != AUDIO_PERCEPTION_BENCHMARK_SCHEMA:
            raise AudioPreprocessError("unsupported audio benchmark schema")

    @property
    def asset_id(self) -> str:
        return self.span.asset_id

    @property
    def source_id(self) -> str:
        return self.span.source_id

    @property
    def source_fingerprint(self) -> str:
        return self.span.source_fingerprint

    @property
    def start(self) -> PresentationTimestamp:
        return self.span.start

    @property
    def end(self) -> PresentationTimestamp:
        return self.span.end

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "segment_id": self.segment_id,
            "span": self.span.to_wire(),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "channel_layout": self.channel_layout,
            "payload_fingerprint": self.payload_fingerprint,
            "preprocessing_fingerprint": self.preprocessing_fingerprint,
            "overlap_group": self.overlap_group,
        }


@dataclass(frozen=True, slots=True)
class AudioPreprocessRequest:
    """Explicit source metadata admitted to an injected preprocessing adapter."""

    asset_id: str
    source_id: str
    source_fingerprint: str
    duration_end: PresentationTimestamp
    sample_rate: int
    channels: int
    channel_layout: str
    paired_video_asset_id: str | None = None
    schema: str = AUDIO_PERCEPTION_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.asset_id, "request asset_id")
        _id(self.source_id, "request source_id")
        _fp(self.source_fingerprint, "request source_fingerprint")
        if not isinstance(self.duration_end, PresentationTimestamp):
            raise AudioPreprocessError("request duration_end must be PresentationTimestamp")
        if self.duration_end.ticks <= 0:
            raise AudioPreprocessError("request duration_end must be positive")
        _positive(self.sample_rate, "request sample_rate", MAX_AUDIO_SAMPLE_RATE)
        _positive(self.channels, "request channels", MAX_AUDIO_CHANNELS)
        _code(self.channel_layout, "request channel_layout")
        if self.paired_video_asset_id is not None:
            _id(self.paired_video_asset_id, "request paired_video_asset_id")
        if self.schema != AUDIO_PERCEPTION_BENCHMARK_SCHEMA:
            raise AudioPreprocessError("unsupported audio benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
            "duration_end": self.duration_end.to_wire(),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "channel_layout": self.channel_layout,
            "paired_video_asset_id": self.paired_video_asset_id,
        }


@dataclass(frozen=True, slots=True)
class AudioPreprocessReceipt:
    route: AudioRoute
    adapter_id: str
    adapter_version: str
    source_fingerprint: str
    preprocessing_fingerprint: str
    network_contacted: bool = False
    decoder_started: bool = False
    schema: str = AUDIO_PERCEPTION_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _enum(self.route, AudioRoute, "receipt route")
        _code(self.adapter_id, "receipt adapter_id")
        _version(self.adapter_version, "receipt adapter_version")
        _fp(self.source_fingerprint, "receipt source_fingerprint")
        _fp(self.preprocessing_fingerprint, "receipt preprocessing_fingerprint")
        _bool(self.network_contacted, "receipt network_contacted")
        _bool(self.decoder_started, "receipt decoder_started")
        if self.route is AudioRoute.STATIC_INJECTED and (
            self.network_contacted or self.decoder_started
        ):
            raise AudioPreprocessError(
                "static injected receipt cannot contact network or start decoder"
            )
        if self.schema != AUDIO_PERCEPTION_BENCHMARK_SCHEMA:
            raise AudioPreprocessError("unsupported audio benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "route": self.route.value,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "source_fingerprint": self.source_fingerprint,
            "preprocessing_fingerprint": self.preprocessing_fingerprint,
            "network_contacted": self.network_contacted,
            "decoder_started": self.decoder_started,
        }


@dataclass(frozen=True, slots=True)
class AudioPreprocessDocument:
    document_id: str
    status: AudioPreprocessStatus
    request: AudioPreprocessRequest
    segments: tuple[AudioSegment, ...] = ()
    gaps: tuple[AudioGap, ...] = ()
    receipt: AudioPreprocessReceipt | None = None
    diagnostics: tuple[str, ...] = ()
    schema: str = AUDIO_PERCEPTION_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.document_id, "document_id")
        _enum(self.status, AudioPreprocessStatus, "document status")
        if not isinstance(self.request, AudioPreprocessRequest):
            raise AudioPreprocessError("document request must be AudioPreprocessRequest")
        if (
            not isinstance(self.segments, tuple)
            or len(self.segments) > MAX_AUDIO_BENCHMARK_SEGMENTS
        ):
            raise AudioPreprocessError("document segments exceed the finite limit")
        if not isinstance(self.gaps, tuple) or len(self.gaps) > MAX_AUDIO_BENCHMARK_GAPS:
            raise AudioPreprocessError("document gaps exceed the finite limit")
        if not all(isinstance(item, AudioSegment) for item in self.segments):
            raise AudioPreprocessError("document segments contain an invalid value")
        if not all(isinstance(item, AudioGap) for item in self.gaps):
            raise AudioPreprocessError("document gaps contain an invalid value")
        if len({item.segment_id for item in self.segments}) != len(self.segments):
            raise AudioPreprocessError("document segment IDs must be unique")
        if len({item.gap_id for item in self.gaps}) != len(self.gaps):
            raise AudioPreprocessError("document gap IDs must be unique")
        for segment in self.segments:
            self._validate_span(segment.span, "segment")
            if (
                segment.sample_rate != self.request.sample_rate
                or segment.channels != self.request.channels
            ):
                raise AudioPreprocessError("normalized segment format differs from request")
            if segment.channel_layout != self.request.channel_layout:
                raise AudioPreprocessError("normalized channel layout differs from request")
        for gap in self.gaps:
            self._validate_span(gap.span, "gap")
        ordered = tuple(
            (item.span.start.ticks, item.span.end.ticks, item.segment_id) for item in self.segments
        )
        if ordered != tuple(sorted(ordered)):
            raise AudioPreprocessError("segments must be deterministically ordered by source PTS")
        for previous, current in zip(self.segments, self.segments[1:], strict=False):
            if current.span.start.ticks < previous.span.end.ticks and not (
                previous.overlap_group
                and current.overlap_group
                and previous.overlap_group == current.overlap_group
            ):
                raise AudioPreprocessError(
                    "overlapping segments require one explicit overlap group"
                )
        for gap in self.gaps:
            for segment in self.segments:
                if self._overlaps(gap.span, segment.span):
                    raise AudioPreprocessError("gap cannot overlap a normalized segment")
        terminal = {
            AudioPreprocessStatus.EMPTY,
            AudioPreprocessStatus.CORRUPT,
            AudioPreprocessStatus.UNSUPPORTED,
            AudioPreprocessStatus.CANCELLED,
        }
        if self.status in terminal and (self.segments or self.gaps or self.receipt is not None):
            raise AudioPreprocessError("terminal document cannot contain output or receipt")
        if self.status is AudioPreprocessStatus.COMPLETE:
            if not self.segments or self.receipt is None:
                raise AudioPreprocessError("complete document requires segments and receipt")
            if self.receipt.source_fingerprint != self.request.source_fingerprint:
                raise AudioPreprocessError("receipt source does not match request")
        if not isinstance(self.diagnostics, tuple) or len(self.diagnostics) > 32:
            raise AudioPreprocessError("diagnostics must be a bounded tuple")
        for diagnostic in self.diagnostics:
            _text(diagnostic, "diagnostic", 256)
        if self.schema != AUDIO_PERCEPTION_BENCHMARK_SCHEMA:
            raise AudioPreprocessError("unsupported audio benchmark schema")
        if len(self.to_wire_bytes()) > MAX_AUDIO_PREPROCESS_OUTPUT_BYTES:
            raise AudioPreprocessError("audio preprocess document exceeds output limit")

    def _validate_span(self, span: AudioSourceSpan, field: str) -> None:
        if span.asset_id != self.request.asset_id or span.source_id != self.request.source_id:
            raise AudioPreprocessError(f"{field} source ownership differs from request")
        if span.source_fingerprint != self.request.source_fingerprint:
            raise AudioPreprocessError(f"{field} fingerprint differs from request")
        if span.end.ticks > self.request.duration_end.ticks:
            raise AudioPreprocessError(f"{field} exceeds request duration")

    @staticmethod
    def _overlaps(left: AudioSourceSpan, right: AudioSourceSpan) -> bool:
        return left.start.ticks < right.end.ticks and right.start.ticks < left.end.ticks

    @property
    def complete(self) -> bool:
        return self.status is AudioPreprocessStatus.COMPLETE

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "document_id": self.document_id,
            "status": self.status.value,
            "request": self.request.to_wire(),
            "segments": [item.to_wire() for item in self.segments],
            "gaps": [item.to_wire() for item in self.gaps],
            "receipt": None if self.receipt is None else self.receipt.to_wire(),
            "diagnostics": list(self.diagnostics),
        }

    def to_wire_bytes(self) -> bytes:
        import json

        return json.dumps(
            self.to_wire(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


@runtime_checkable
class AudioCancellationProbe(Protocol):
    def is_cancelled(self) -> bool:
        """Return whether the caller requested cancellation."""


AudioProducer = Callable[[AudioPreprocessRequest], AudioPreprocessDocument]


def execute_audio_preprocess(
    producer: AudioProducer,
    request: AudioPreprocessRequest,
    *,
    cancellation_probe: AudioCancellationProbe | None = None,
) -> AudioPreprocessDocument:
    """Run one explicitly injected producer without process/network/provider discovery."""

    if not callable(producer):
        raise AudioPreprocessError("audio producer must be callable")
    if not isinstance(request, AudioPreprocessRequest):
        raise AudioPreprocessError("request must be AudioPreprocessRequest")
    if cancellation_probe is not None and (
        not isinstance(cancellation_probe, AudioCancellationProbe)
        or cancellation_probe.is_cancelled()
    ):
        raise AudioPreprocessError("audio preprocessing cancelled")
    document = producer(request)
    if not isinstance(document, AudioPreprocessDocument):
        raise AudioPreprocessError("producer returned an invalid audio document")
    if cancellation_probe is not None and cancellation_probe.is_cancelled():
        raise AudioPreprocessError("audio preprocessing cancelled")
    return document


def build_audio_preprocess_abstention(
    request: AudioPreprocessRequest,
    status: AudioPreprocessStatus,
    diagnostic: str,
) -> AudioPreprocessDocument:
    if status not in {
        AudioPreprocessStatus.EMPTY,
        AudioPreprocessStatus.CORRUPT,
        AudioPreprocessStatus.UNSUPPORTED,
        AudioPreprocessStatus.CANCELLED,
    }:
        raise AudioPreprocessError("abstention status must be terminal")
    return AudioPreprocessDocument(
        document_id=f"abstention.{status.value}",
        status=status,
        request=request,
        diagnostics=(diagnostic,),
    )


@dataclass(frozen=True, slots=True)
class AudioBenchmarkFixture:
    fixture_id: str
    modality: str
    capabilities: tuple[AudioCapability, ...]
    source_fingerprint: str
    annotation_fingerprint: str
    sample_rate: int
    channels: int
    segment_count: int
    gap_count: int
    overlap: bool = False
    corrupt: bool = False
    adversarial_metadata: bool = False
    paired_video_asset_id: str | None = None
    tags: tuple[str, ...] = ()
    schema: str = AUDIO_PERCEPTION_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.fixture_id, "fixture_id")
        _code(self.modality, "fixture modality")
        _enum_tuple(
            self.capabilities, AudioCapability, "fixture capabilities", len(AudioCapability)
        )
        _fp(self.source_fingerprint, "fixture source_fingerprint")
        _fp(self.annotation_fingerprint, "fixture annotation_fingerprint")
        _positive(self.sample_rate, "fixture sample_rate", MAX_AUDIO_SAMPLE_RATE)
        _positive(self.channels, "fixture channels", MAX_AUDIO_CHANNELS)
        _non_negative(self.segment_count, "fixture segment_count", MAX_AUDIO_BENCHMARK_SEGMENTS)
        _non_negative(self.gap_count, "fixture gap_count", MAX_AUDIO_BENCHMARK_GAPS)
        for value, field in (
            (self.overlap, "fixture overlap"),
            (self.corrupt, "fixture corrupt"),
            (self.adversarial_metadata, "fixture adversarial_metadata"),
        ):
            _bool(value, field)
        if self.paired_video_asset_id is not None:
            _id(self.paired_video_asset_id, "fixture paired_video_asset_id")
        if not isinstance(self.tags, tuple) or len(self.tags) > 16:
            raise AudioPreprocessError("fixture tags must be bounded")
        for tag in self.tags:
            _code(tag, "fixture tag")
        if self.schema != AUDIO_PERCEPTION_BENCHMARK_SCHEMA:
            raise AudioPreprocessError("unsupported audio benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "fixture_id": self.fixture_id,
            "modality": self.modality,
            "capabilities": [item.value for item in self.capabilities],
            "source_fingerprint": self.source_fingerprint,
            "annotation_fingerprint": self.annotation_fingerprint,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "segment_count": self.segment_count,
            "gap_count": self.gap_count,
            "overlap": self.overlap,
            "corrupt": self.corrupt,
            "adversarial_metadata": self.adversarial_metadata,
            "paired_video_asset_id": self.paired_video_asset_id,
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class AudioBenchmarkThreshold:
    metric_id: str
    metric: AudioMetricKind
    capability: AudioCapability
    minimum: int
    schema: str = AUDIO_PERCEPTION_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.metric_id, "threshold metric_id")
        _enum(self.metric, AudioMetricKind, "threshold metric")
        _enum(self.capability, AudioCapability, "threshold capability")
        _non_negative(self.minimum, "threshold minimum", 10_000)
        if self.schema != AUDIO_PERCEPTION_BENCHMARK_SCHEMA:
            raise AudioPreprocessError("unsupported audio benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "metric_id": self.metric_id,
            "metric": self.metric.value,
            "capability": self.capability.value,
            "minimum": self.minimum,
        }


@dataclass(frozen=True, slots=True)
class AudioCandidateProfile:
    candidate_id: str
    family: AudioCandidateFamily
    adapter_id: str
    adapter_version: str
    model_id: str
    capabilities: tuple[AudioCapability, ...]
    requires_network: bool
    supports_determinism: bool
    supports_cancellation_cleanup: bool
    disposition: AudioDisposition
    disposition_reason: str
    schema: str = AUDIO_PERCEPTION_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.candidate_id, "candidate_id")
        _enum(self.family, AudioCandidateFamily, "candidate family")
        _code(self.adapter_id, "candidate adapter_id")
        _version(self.adapter_version, "candidate adapter_version")
        _id(self.model_id, "candidate model_id")
        _enum_tuple(
            self.capabilities, AudioCapability, "candidate capabilities", len(AudioCapability)
        )
        for value, field in (
            (self.requires_network, "candidate requires_network"),
            (self.supports_determinism, "candidate supports_determinism"),
            (self.supports_cancellation_cleanup, "candidate supports_cancellation_cleanup"),
        ):
            _bool(value, field)
        _enum(self.disposition, AudioDisposition, "candidate disposition")
        _text(self.disposition_reason, "candidate disposition_reason")
        if self.family is AudioCandidateFamily.OLLAMA and not self.requires_network:
            raise AudioPreprocessError("Ollama candidate must disclose loopback transport")
        if self.schema != AUDIO_PERCEPTION_BENCHMARK_SCHEMA:
            raise AudioPreprocessError("unsupported audio benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "candidate_id": self.candidate_id,
            "family": self.family.value,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "model_id": self.model_id,
            "capabilities": [item.value for item in self.capabilities],
            "requires_network": self.requires_network,
            "supports_determinism": self.supports_determinism,
            "supports_cancellation_cleanup": self.supports_cancellation_cleanup,
            "disposition": self.disposition.value,
            "disposition_reason": self.disposition_reason,
        }


@dataclass(frozen=True, slots=True)
class AudioBenchmarkLimits:
    max_fixtures: int
    max_segments: int
    max_gaps: int
    max_wall_time_seconds: int
    max_total_compute_seconds: int
    max_peak_vram_mb: int
    max_peak_ram_mb: int
    max_output_bytes: int
    max_concurrency: int
    network_allowed: bool = False
    media_upload_allowed: bool = False
    schema: str = AUDIO_PERCEPTION_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        for value, field in (
            (self.max_fixtures, "limits max_fixtures"),
            (self.max_segments, "limits max_segments"),
            (self.max_gaps, "limits max_gaps"),
            (self.max_wall_time_seconds, "limits max_wall_time_seconds"),
            (self.max_total_compute_seconds, "limits max_total_compute_seconds"),
            (self.max_peak_vram_mb, "limits max_peak_vram_mb"),
            (self.max_peak_ram_mb, "limits max_peak_ram_mb"),
            (self.max_output_bytes, "limits max_output_bytes"),
            (self.max_concurrency, "limits max_concurrency"),
        ):
            _positive(value, field, 1_000_000_000)
        if self.network_allowed or self.media_upload_allowed:
            raise AudioPreprocessError("offline audio benchmark cannot allow network or upload")
        if self.schema != AUDIO_PERCEPTION_BENCHMARK_SCHEMA:
            raise AudioPreprocessError("unsupported audio benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "max_fixtures": self.max_fixtures,
            "max_segments": self.max_segments,
            "max_gaps": self.max_gaps,
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
class AudioRoutingPolicy:
    preference_order: tuple[AudioCandidateFamily, ...]
    automatic_fallback: bool = False
    explicit_selection_required: bool = True
    schema: str = AUDIO_PERCEPTION_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _enum_tuple(self.preference_order, AudioCandidateFamily, "routing preference_order", 3)
        if tuple(self.preference_order) != tuple(AudioCandidateFamily):
            raise AudioPreprocessError("routing must disclose native, Ollama, and specialist order")
        if self.automatic_fallback or not self.explicit_selection_required:
            raise AudioPreprocessError("audio routing requires explicit selection and no fallback")
        if self.schema != AUDIO_PERCEPTION_BENCHMARK_SCHEMA:
            raise AudioPreprocessError("unsupported audio benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "preference_order": [item.value for item in self.preference_order],
            "automatic_fallback": self.automatic_fallback,
            "explicit_selection_required": self.explicit_selection_required,
        }


@dataclass(frozen=True, slots=True)
class AudioBenchmarkPlan:
    plan_id: str
    plan_version: str
    fixtures: tuple[AudioBenchmarkFixture, ...]
    thresholds: tuple[AudioBenchmarkThreshold, ...]
    candidates: tuple[AudioCandidateProfile, ...]
    limits: AudioBenchmarkLimits
    routing: AudioRoutingPolicy
    schema: str = AUDIO_PERCEPTION_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.plan_id, "plan_id")
        _version(self.plan_version, "plan_version")
        if (
            not isinstance(self.fixtures, tuple)
            or not self.fixtures
            or len(self.fixtures) > MAX_AUDIO_BENCHMARK_FIXTURES
        ):
            raise AudioPreprocessError("plan fixtures must be bounded and non-empty")
        if (
            not isinstance(self.thresholds, tuple)
            or not self.thresholds
            or len(self.thresholds) > MAX_AUDIO_BENCHMARK_THRESHOLDS
        ):
            raise AudioPreprocessError("plan thresholds must be bounded and non-empty")
        if (
            not isinstance(self.candidates, tuple)
            or not self.candidates
            or len(self.candidates) > MAX_AUDIO_BENCHMARK_CANDIDATES
        ):
            raise AudioPreprocessError("plan candidates must be bounded and non-empty")
        if not all(isinstance(item, AudioBenchmarkFixture) for item in self.fixtures):
            raise AudioPreprocessError("plan fixtures contain an invalid value")
        if not all(isinstance(item, AudioBenchmarkThreshold) for item in self.thresholds):
            raise AudioPreprocessError("plan thresholds contain an invalid value")
        if not all(isinstance(item, AudioCandidateProfile) for item in self.candidates):
            raise AudioPreprocessError("plan candidates contain an invalid value")
        if not isinstance(self.limits, AudioBenchmarkLimits) or not isinstance(
            self.routing, AudioRoutingPolicy
        ):
            raise AudioPreprocessError("plan limits/routing have invalid types")
        if len({item.fixture_id for item in self.fixtures}) != len(self.fixtures):
            raise AudioPreprocessError("fixture IDs must be unique")
        if len({item.metric_id for item in self.thresholds}) != len(self.thresholds):
            raise AudioPreprocessError("threshold IDs must be unique")
        if len({item.candidate_id for item in self.candidates}) != len(self.candidates):
            raise AudioPreprocessError("candidate IDs must be unique")
        covered = {capability for fixture in self.fixtures for capability in fixture.capabilities}
        if covered != set(AudioCapability):
            raise AudioPreprocessError("audio fixture capability coverage is incomplete")
        threshold_caps = {item.capability for item in self.thresholds}
        if threshold_caps != set(AudioCapability):
            raise AudioPreprocessError("every audio capability requires a frozen threshold")
        if len(self.fixtures) > self.limits.max_fixtures:
            raise AudioPreprocessError("fixtures exceed frozen limits")
        families = {item.family for item in self.candidates}
        if families != set(AudioCandidateFamily):
            raise AudioPreprocessError(
                "plan must represent native, Ollama, and specialist families"
            )
        if self.schema != AUDIO_PERCEPTION_BENCHMARK_SCHEMA:
            raise AudioPreprocessError("unsupported audio benchmark schema")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    @property
    def executable_candidate_ids(self) -> tuple[str, ...]:
        return tuple(
            item.candidate_id
            for item in self.candidates
            if item.disposition is AudioDisposition.QUALIFIED
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "fixtures": [item.to_wire() for item in self.fixtures],
            "thresholds": [item.to_wire() for item in self.thresholds],
            "candidates": [item.to_wire() for item in self.candidates],
            "limits": self.limits.to_wire(),
            "routing": self.routing.to_wire(),
        }

    def to_public_summary(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "plan_fingerprint": self.fingerprint,
            "case_count": len(self.fixtures),
            "capability_count": len(AudioCapability),
            "threshold_count": len(self.thresholds),
            "candidate_count": len(self.candidates),
            "executable_candidate_ids": list(self.executable_candidate_ids),
            "dispositions": {
                disposition.value: sum(item.disposition is disposition for item in self.candidates)
                for disposition in AudioDisposition
            },
            "automatic_fallback": self.routing.automatic_fallback,
            "claim_ceiling": "structural_only",
        }


def _fp_seed(seed: str) -> str:
    return canonical_fingerprint({"audio_fixture": seed})


def _fixture(
    fixture_id: str,
    capabilities: tuple[AudioCapability, ...],
    *,
    modality: str = "audio",
    segment_count: int = 2,
    gap_count: int = 0,
    overlap: bool = False,
    corrupt: bool = False,
    adversarial_metadata: bool = False,
    paired_video_asset_id: str | None = None,
    tags: tuple[str, ...] = (),
) -> AudioBenchmarkFixture:
    return AudioBenchmarkFixture(
        fixture_id,
        modality,
        capabilities,
        _fp_seed(fixture_id + ".source"),
        _fp_seed(fixture_id + ".annotation"),
        48_000,
        2,
        segment_count,
        gap_count,
        overlap,
        corrupt,
        adversarial_metadata,
        paired_video_asset_id,
        tags,
    )


def build_default_audio_benchmark_plan() -> AudioBenchmarkPlan:
    """Build the frozen M12-01 metadata-only benchmark."""

    fixtures = (
        _fixture(
            "audio.dialogue.multilingual",
            (
                AudioCapability.SOURCE_CLOCK,
                AudioCapability.CHANNELS,
                AudioCapability.SAMPLE_RATE,
                AudioCapability.SPEECH,
                AudioCapability.MULTILINGUAL_DIALOGUE,
            ),
            tags=("speech", "en-zh", "clock"),
        ),
        _fixture(
            "audio.silence.noise.clipping",
            (AudioCapability.SILENCE, AudioCapability.NOISE, AudioCapability.CLIPPING),
            tags=("silence", "noise", "clipping"),
        ),
        _fixture(
            "audio.music.ambience.sfx",
            (AudioCapability.MUSIC, AudioCapability.AMBIENCE, AudioCapability.SFX),
            tags=("music", "ambience", "foley"),
        ),
        _fixture(
            "video.audio.gaps.overlap",
            (AudioCapability.GAPS, AudioCapability.OVERLAP, AudioCapability.VIDEO_AUDIO_OWNERSHIP),
            modality="video_audio",
            gap_count=1,
            overlap=True,
            paired_video_asset_id="video_fixture_a",
            tags=("gap", "overlap", "paired-video"),
        ),
        _fixture(
            "audio.corrupt.adversarial",
            (AudioCapability.CORRUPTION, AudioCapability.ADVERSARIAL_METADATA),
            segment_count=0,
            corrupt=True,
            adversarial_metadata=True,
            tags=("truncated", "metadata-injection"),
        ),
    )
    thresholds = tuple(
        AudioBenchmarkThreshold(
            f"structural.{capability.value}",
            AudioMetricKind.TIMESTAMP_PRESERVATION
            if capability is AudioCapability.SOURCE_CLOCK
            else AudioMetricKind.CHANNEL_OWNERSHIP
            if capability in {AudioCapability.CHANNELS, AudioCapability.VIDEO_AUDIO_OWNERSHIP}
            else AudioMetricKind.GAP_RECALL
            if capability is AudioCapability.GAPS
            else AudioMetricKind.OVERLAP_RECALL
            if capability is AudioCapability.OVERLAP
            else AudioMetricKind.CORRUPTION_REJECTION
            if capability is AudioCapability.CORRUPTION
            else AudioMetricKind.ADVERSARIAL_METADATA_REJECTION
            if capability is AudioCapability.ADVERSARIAL_METADATA
            else AudioMetricKind.SAMPLE_RATE_PRESERVATION
            if capability is AudioCapability.SAMPLE_RATE
            else AudioMetricKind.TIMESTAMP_PRESERVATION,
            capability,
            9_000,
        )
        for capability in AudioCapability
    )
    candidates = (
        AudioCandidateProfile(
            "native.comfyui.audio",
            AudioCandidateFamily.COMFYUI_NATIVE,
            "comfyui_native_audio",
            "1.0.0",
            "host-owned-audio-capability",
            tuple(AudioCapability),
            False,
            True,
            False,
            AudioDisposition.UNAVAILABLE,
            "supported ComfyUI audio model and media host lane were not admitted",
        ),
        AudioCandidateProfile(
            "fallback.ollama.audio",
            AudioCandidateFamily.OLLAMA,
            "ollama_native_api",
            "1.0.0",
            "explicitly-selected-loopback-audio-model",
            tuple(AudioCapability),
            True,
            False,
            False,
            AudioDisposition.UNAVAILABLE,
            "loopback Ollama server and explicitly selected audio model were not started",
        ),
        AudioCandidateProfile(
            "specialist.audio.placeholder",
            AudioCandidateFamily.SPECIALIST,
            "specialist_not_selected",
            "1.0.0",
            "explicit-selection-required",
            tuple(AudioCapability),
            False,
            True,
            True,
            AudioDisposition.UNSUPPORTED,
            "no specialist audio profile is selected in the native-first lane",
        ),
    )
    return AudioBenchmarkPlan(
        "m12-01.audio-perception-benchmark",
        "1.0.0",
        fixtures,
        thresholds,
        candidates,
        AudioBenchmarkLimits(5, 64, 16, 60, 180, 16_384, 32_768, 65_536, 1),
        AudioRoutingPolicy(tuple(AudioCandidateFamily)),
    )


__all__ = [
    "AUDIO_PERCEPTION_BENCHMARK_SCHEMA",
    "MAX_AUDIO_BENCHMARK_CANDIDATES",
    "MAX_AUDIO_BENCHMARK_FIXTURES",
    "MAX_AUDIO_BENCHMARK_GAPS",
    "MAX_AUDIO_BENCHMARK_SEGMENTS",
    "MAX_AUDIO_BENCHMARK_THRESHOLDS",
    "MAX_AUDIO_CHANNELS",
    "MAX_AUDIO_DURATION_TICKS",
    "MAX_AUDIO_PREPROCESS_OUTPUT_BYTES",
    "MAX_AUDIO_SAMPLE_RATE",
    "AudioBenchmarkFixture",
    "AudioBenchmarkLimits",
    "AudioBenchmarkPlan",
    "AudioBenchmarkThreshold",
    "AudioCandidateFamily",
    "AudioCandidateProfile",
    "AudioCapability",
    "AudioDisposition",
    "AudioGap",
    "AudioGapKind",
    "AudioMetricKind",
    "AudioPreprocessDocument",
    "AudioPreprocessReceipt",
    "AudioPreprocessRequest",
    "AudioPreprocessStatus",
    "AudioRoute",
    "AudioRoutingPolicy",
    "AudioSegment",
    "AudioSourceSpan",
    "AudioCancellationProbe",
    "build_audio_preprocess_abstention",
    "build_default_audio_benchmark_plan",
    "execute_audio_preprocess",
]
