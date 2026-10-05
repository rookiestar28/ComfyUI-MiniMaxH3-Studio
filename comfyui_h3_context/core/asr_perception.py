"""M12-02 source-owned ASR, language, timing, and uncertainty contracts.

Only bounded, redacted observations and injected descriptors cross this seam.  Audio samples,
decoders, ComfyUI model objects, Ollama, specialist runtimes, and network clients remain outside
the pure core.  ASR text is explicitly observed evidence and can never become user-authored exact
dialogue through this API.
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
from .errors import ASRPerceptionError
from .media_admission import PresentationTimestamp

ASR_PERCEPTION_SCHEMA = "h3.audio.asr.v1"
ASR_BENCHMARK_SCHEMA = "h3.audio.asr_benchmark.v1"
MAX_ASR_SEGMENTS = 64
MAX_ASR_WORDS = 1024
MAX_ASR_ALTERNATIVES = 4
MAX_ASR_UNCERTAINTIES = 8
MAX_ASR_DIAGNOSTICS = 32
MAX_ASR_OUTPUT_BYTES = 65_536
MAX_ASR_TEXT_LENGTH = 2048
MAX_ASR_LANGUAGE_HINTS = 8
MAX_ASR_BENCHMARK_CASES = 8
MAX_ASR_BENCHMARK_THRESHOLDS = 64
MAX_ASR_BENCHMARK_CANDIDATES = 8

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_LANGUAGE = re.compile(r"[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{2,8}){0,2}\Z")
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
_UNCLEAR_ALLOWED = frozenset(
    {
        "low_confidence",
        "ambiguous",
        "unintelligible",
        "noise",
        "music_masking",
        "overlap",
        "language_unknown",
    }
)


class ASRStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"


class ASRRoute(str, Enum):
    STATIC_INJECTED = "static_injected"
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"
    SPECIALIST = "specialist"


class ASRCandidateFamily(str, Enum):
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"
    SPECIALIST = "specialist"


class ASRDisposition(str, Enum):
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


class ASRTextAuthority(str, Enum):
    OBSERVED_ASR = "observed_asr"


class ASRTextStatus(str, Enum):
    TRANSCRIBED = "transcribed"
    UNCLEAR = "unclear"


class ASRUncertaintyKind(str, Enum):
    LOW_CONFIDENCE = "low_confidence"
    AMBIGUOUS = "ambiguous"
    UNINTELLIGIBLE = "unintelligible"
    NOISE = "noise"
    MUSIC_MASKING = "music_masking"
    OVERLAP = "overlap"
    LANGUAGE_UNKNOWN = "language_unknown"
    TIMING_UNCERTAIN = "timing_uncertain"
    CORRUPT = "corrupt"
    TRUNCATED = "truncated"
    NO_SPEECH = "no_speech"


class ASRCapability(str, Enum):
    SEGMENT_TIMING = "segment_timing"
    WORD_TIMING = "word_timing"
    LANGUAGE_ID = "language_id"
    LANGUAGE_CONFIDENCE = "language_confidence"
    WORD_CONFIDENCE = "word_confidence"
    ALTERNATIVES = "alternatives"
    UNCLEAR_ABSTENTION = "unclear_abstention"
    EXACT_DIALOGUE_SEPARATION = "exact_dialogue_separation"
    MULTILINGUAL = "multilingual"
    NOISY_SPEECH = "noisy_speech"
    OVERLAP_SPEECH = "overlap_speech"
    MUSIC_MASKING = "music_masking"
    SILENCE_ABSTENTION = "silence_abstention"
    CORRUPTION = "corruption"
    ADVERSARIAL_METADATA = "adversarial_metadata"


class ASRCaseKind(str, Enum):
    MULTILINGUAL_DIALOGUE = "multilingual_dialogue"
    NOISY_SPEECH = "noisy_speech"
    OVERLAP_SPEECH = "overlap_speech"
    MUSIC_MASKING = "music_masking"
    SILENCE = "silence"
    CORRUPT_ADVERSARIAL = "corrupt_adversarial"


class ASRMetricKind(str, Enum):
    WER = "wer"
    TIMESTAMP_ERROR = "timestamp_error"
    LANGUAGE_ACCURACY = "language_accuracy"
    CONFIDENCE_CALIBRATION = "confidence_calibration"
    ALTERNATIVE_RECALL = "alternative_recall"
    UNCLEAR_ABSTENTION = "unclear_abstention"
    EXACT_AUTHORITY_VIOLATIONS = "exact_authority_violations"


class ASRMetricUnit(str, Enum):
    BASIS_POINTS = "basis_points"
    MILLISECONDS = "milliseconds"
    COUNT = "count"


def _id(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ASRPerceptionError(f"{field} must be a bounded identifier")
    if any(marker in value.casefold() for marker in ("/", "\\", *_SENSITIVE)):
        raise ASRPerceptionError(f"{field} contains locator or sensitive material")
    return value


def _code(value: object, field: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value.casefold()) is None:
        raise ASRPerceptionError(f"{field} must be a lower-case bounded code")
    return value.casefold()


def _version(value: object, field: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise ASRPerceptionError(f"{field} must be a numeric version")
    return value


def _fp(value: object, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ASRPerceptionError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _text(value: object, field: str, maximum: int = MAX_ASR_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ASRPerceptionError(f"{field} must be bounded non-empty text")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ASRPerceptionError(f"{field} contains a control character")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise ASRPerceptionError(f"{field} contains locator or sensitive material")
    return value


def _language(value: object, field: str) -> str:
    if not isinstance(value, str) or _LANGUAGE.fullmatch(value) is None:
        raise ASRPerceptionError(f"{field} must be a bounded language tag")
    return value


def _optional_language(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _language(value, field)


def _confidence(value: object, field: str) -> object:
    if value is None:
        return None
    from decimal import Decimal

    if not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 1:
        raise ASRPerceptionError(f"{field} must be a Decimal between 0 and 1")
    return value


def _positive(value: object, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise ASRPerceptionError(f"{field} must be between 1 and {maximum}")
    return value


def _non_negative(value: object, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ASRPerceptionError(f"{field} must be between 0 and {maximum}")
    return value


def _enum(value: object, expected: type[Enum], field: str) -> Enum:
    try:
        return value if isinstance(value, expected) else expected(value)
    except (TypeError, ValueError):
        raise ASRPerceptionError(f"{field} is unsupported") from None


def _timestamp_span(start: PresentationTimestamp, end: PresentationTimestamp, field: str) -> None:
    if not isinstance(start, PresentationTimestamp) or not isinstance(end, PresentationTimestamp):
        raise ASRPerceptionError(f"{field} endpoints must be PresentationTimestamp")
    if (start.time_base_num, start.time_base_den) != (end.time_base_num, end.time_base_den):
        raise ASRPerceptionError(f"{field} endpoints must share one time base")
    if end.ticks <= start.ticks:
        raise ASRPerceptionError(f"{field} end must be after start")


def _uncertainties(values: object, field: str) -> tuple[ASRUncertainty, ...]:
    if not isinstance(values, tuple) or len(values) > MAX_ASR_UNCERTAINTIES:
        raise ASRPerceptionError(f"{field} must be a bounded tuple")
    if not all(isinstance(value, ASRUncertainty) for value in values):
        raise ASRPerceptionError(f"{field} contains an invalid value")
    kinds = tuple(value.kind for value in values)
    if len(kinds) != len(set(kinds)):
        raise ASRPerceptionError(f"{field} must not duplicate uncertainty kinds")
    return values


def _alternatives(values: object, field: str) -> tuple[ASRAlternative, ...]:
    if not isinstance(values, tuple) or len(values) > MAX_ASR_ALTERNATIVES:
        raise ASRPerceptionError(f"{field} must be a bounded tuple")
    if not all(isinstance(value, ASRAlternative) for value in values):
        raise ASRPerceptionError(f"{field} contains an invalid value")
    ranks = tuple(value.rank for value in values)
    texts = tuple(value.text.casefold() for value in values)
    if len(ranks) != len(set(ranks)) or len(texts) != len(set(texts)):
        raise ASRPerceptionError(f"{field} must not contain duplicate ranks or text")
    return values


@dataclass(frozen=True, slots=True)
class ASRRequest:
    """Explicit source and preprocessing identity admitted to an injected ASR producer."""

    asset_id: str
    source_id: str
    source_fingerprint: str
    preprocessing_fingerprint: str
    duration_end: PresentationTimestamp
    language_hints: tuple[str, ...] = ()
    route: ASRRoute | str = ASRRoute.STATIC_INJECTED
    max_segments: int = MAX_ASR_SEGMENTS
    max_words: int = MAX_ASR_WORDS
    schema: str = ASR_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.asset_id, "request asset_id")
        _id(self.source_id, "request source_id")
        _fp(self.source_fingerprint, "request source_fingerprint")
        _fp(self.preprocessing_fingerprint, "request preprocessing_fingerprint")
        if not isinstance(self.duration_end, PresentationTimestamp) or self.duration_end.ticks <= 0:
            raise ASRPerceptionError(
                "request duration_end must be a positive PresentationTimestamp"
            )
        if (
            not isinstance(self.language_hints, tuple)
            or len(self.language_hints) > MAX_ASR_LANGUAGE_HINTS
        ):
            raise ASRPerceptionError("language_hints must be a bounded tuple")
        languages = tuple(_language(value, "language hint") for value in self.language_hints)
        if len(languages) != len(set(value.casefold() for value in languages)):
            raise ASRPerceptionError("language_hints must not contain duplicates")
        object.__setattr__(self, "language_hints", languages)
        object.__setattr__(self, "route", _enum(self.route, ASRRoute, "request route"))
        _positive(self.max_segments, "request max_segments", MAX_ASR_SEGMENTS)
        _positive(self.max_words, "request max_words", MAX_ASR_WORDS)
        if self.schema != ASR_PERCEPTION_SCHEMA:
            raise ASRPerceptionError("unsupported ASR perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
            "preprocessing_fingerprint": self.preprocessing_fingerprint,
            "duration_end": self.duration_end.to_wire(),
            "language_hints": list(self.language_hints),
            "route": cast(ASRRoute, self.route).value,
            "max_segments": self.max_segments,
            "max_words": self.max_words,
        }


@dataclass(frozen=True, slots=True)
class ASRAlternative:
    """Observed hypothesis alternative; never a user-authored exact constraint."""

    text: str
    confidence: object
    rank: int
    authority: ASRTextAuthority | str = ASRTextAuthority.OBSERVED_ASR
    schema: str = ASR_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _text(self.text, "alternative text")
        if self.text == "[unclear]":
            raise ASRPerceptionError("alternative text cannot be the abstention marker")
        _confidence(self.confidence, "alternative confidence")
        _positive(self.rank, "alternative rank", MAX_ASR_ALTERNATIVES)
        authority = _enum(self.authority, ASRTextAuthority, "alternative authority")
        if authority is not ASRTextAuthority.OBSERVED_ASR:
            raise ASRPerceptionError("alternative authority must remain observed_asr")
        object.__setattr__(self, "authority", authority)
        if self.schema != ASR_PERCEPTION_SCHEMA:
            raise ASRPerceptionError("unsupported ASR perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "text": self.text,
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "rank": self.rank,
            "authority": cast(ASRTextAuthority, self.authority).value,
        }


@dataclass(frozen=True, slots=True)
class ASRUncertainty:
    """Bounded reason why an observed ASR claim may be incomplete or ambiguous."""

    kind: ASRUncertaintyKind | str
    detail: str
    schema: str = ASR_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _enum(self.kind, ASRUncertaintyKind, "uncertainty kind"))
        _text(self.detail, "uncertainty detail", 512)
        if self.schema != ASR_PERCEPTION_SCHEMA:
            raise ASRPerceptionError("unsupported ASR perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "kind": cast(ASRUncertaintyKind, self.kind).value,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ASRWord:
    """One source-timestamped observed word hypothesis."""

    word_id: str
    span: AudioSourceSpan
    text: str
    confidence: object = None
    alternatives: tuple[ASRAlternative, ...] = ()
    uncertainties: tuple[ASRUncertainty, ...] = ()
    authority: ASRTextAuthority | str = ASRTextAuthority.OBSERVED_ASR
    schema: str = ASR_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.word_id, "word_id")
        if not isinstance(self.span, AudioSourceSpan):
            raise ASRPerceptionError("word span must be AudioSourceSpan")
        _text(self.text, "word text", 256)
        if self.text == "[unclear]":
            raise ASRPerceptionError("word text cannot be the abstention marker")
        _confidence(self.confidence, "word confidence")
        object.__setattr__(
            self, "alternatives", _alternatives(self.alternatives, "word alternatives")
        )
        object.__setattr__(
            self, "uncertainties", _uncertainties(self.uncertainties, "word uncertainties")
        )
        authority = _enum(self.authority, ASRTextAuthority, "word authority")
        if authority is not ASRTextAuthority.OBSERVED_ASR:
            raise ASRPerceptionError("word authority must remain observed_asr")
        object.__setattr__(self, "authority", authority)
        if self.schema != ASR_PERCEPTION_SCHEMA:
            raise ASRPerceptionError("unsupported ASR perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "word_id": self.word_id,
            "span": self.span.to_wire(),
            "text": self.text,
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "alternatives": [item.to_wire() for item in self.alternatives],
            "uncertainties": [item.to_wire() for item in self.uncertainties],
            "authority": cast(ASRTextAuthority, self.authority).value,
        }


@dataclass(frozen=True, slots=True)
class ASRSegment:
    """A source-owned transcript segment with explicit uncertainty and authority."""

    segment_id: str
    span: AudioSourceSpan
    text: str
    text_status: ASRTextStatus | str
    language: str | None
    language_confidence: object
    confidence: object
    words: tuple[ASRWord, ...] = ()
    alternatives: tuple[ASRAlternative, ...] = ()
    uncertainties: tuple[ASRUncertainty, ...] = ()
    overlap_group: str | None = None
    authority: ASRTextAuthority | str = ASRTextAuthority.OBSERVED_ASR
    schema: str = ASR_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.segment_id, "segment_id")
        if not isinstance(self.span, AudioSourceSpan):
            raise ASRPerceptionError("segment span must be AudioSourceSpan")
        _text(self.text, "segment text")
        status = _enum(self.text_status, ASRTextStatus, "segment text_status")
        object.__setattr__(self, "text_status", status)
        if status is ASRTextStatus.UNCLEAR and self.text != "[unclear]":
            raise ASRPerceptionError("unclear segment must use the exact [unclear] marker")
        if status is ASRTextStatus.TRANSCRIBED and self.text == "[unclear]":
            raise ASRPerceptionError("transcribed segment cannot use the abstention marker")
        object.__setattr__(self, "language", _optional_language(self.language, "segment language"))
        _confidence(self.language_confidence, "language confidence")
        _confidence(self.confidence, "segment confidence")
        if not isinstance(self.words, tuple) or len(self.words) > MAX_ASR_WORDS:
            raise ASRPerceptionError("segment words exceed the finite limit")
        if not all(isinstance(value, ASRWord) for value in self.words):
            raise ASRPerceptionError("segment words contain an invalid value")
        word_ids = tuple(value.word_id for value in self.words)
        if len(word_ids) != len(set(word_ids)):
            raise ASRPerceptionError("segment word IDs must be unique")
        if status is ASRTextStatus.UNCLEAR and self.words:
            raise ASRPerceptionError("unclear segment cannot carry fabricated word timing")
        if status is ASRTextStatus.TRANSCRIBED and not self.words:
            raise ASRPerceptionError("transcribed segment requires word timing")
        previous: tuple[int, int, str] | None = None
        for word in self.words:
            if (
                word.span.asset_id != self.span.asset_id
                or word.span.source_id != self.span.source_id
                or word.span.source_fingerprint != self.span.source_fingerprint
            ):
                raise ASRPerceptionError("word source ownership differs from segment")
            if (word.span.start.time_base_num, word.span.start.time_base_den) != (
                self.span.start.time_base_num,
                self.span.start.time_base_den,
            ):
                raise ASRPerceptionError("word and segment must use one source time base")
            if (
                word.span.start.ticks < self.span.start.ticks
                or word.span.end.ticks > self.span.end.ticks
            ):
                raise ASRPerceptionError("word timing exceeds its segment span")
            current = (word.span.start.ticks, word.span.end.ticks, word.word_id)
            if previous is not None and current < previous:
                raise ASRPerceptionError("word timing must be deterministically ordered")
            previous = current
        object.__setattr__(
            self, "alternatives", _alternatives(self.alternatives, "segment alternatives")
        )
        object.__setattr__(
            self, "uncertainties", _uncertainties(self.uncertainties, "segment uncertainties")
        )
        if status is ASRTextStatus.UNCLEAR and not any(
            cast(ASRUncertaintyKind, item.kind).value in _UNCLEAR_ALLOWED
            for item in self.uncertainties
        ):
            raise ASRPerceptionError("unclear segment requires an explicit uncertainty reason")
        if (
            self.language is None
            and status is ASRTextStatus.TRANSCRIBED
            and not any(
                item.kind is ASRUncertaintyKind.LANGUAGE_UNKNOWN for item in self.uncertainties
            )
        ):
            raise ASRPerceptionError(
                "transcribed segment without language requires language_unknown"
            )
        if self.overlap_group is not None:
            _id(self.overlap_group, "segment overlap_group")
        authority = _enum(self.authority, ASRTextAuthority, "segment authority")
        if authority is not ASRTextAuthority.OBSERVED_ASR:
            raise ASRPerceptionError("segment authority must remain observed_asr")
        object.__setattr__(self, "authority", authority)
        if self.schema != ASR_PERCEPTION_SCHEMA:
            raise ASRPerceptionError("unsupported ASR perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "segment_id": self.segment_id,
            "span": self.span.to_wire(),
            "text": self.text,
            "text_status": cast(ASRTextStatus, self.text_status).value,
            "language": self.language,
            "language_confidence": (
                None if self.language_confidence is None else format(self.language_confidence, "f")
            ),
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "words": [item.to_wire() for item in self.words],
            "alternatives": [item.to_wire() for item in self.alternatives],
            "uncertainties": [item.to_wire() for item in self.uncertainties],
            "overlap_group": self.overlap_group,
            "authority": cast(ASRTextAuthority, self.authority).value,
        }


@dataclass(frozen=True, slots=True)
class ASRReceipt:
    """Redacted adapter/model receipt; credentials and raw provider payloads are excluded."""

    route: ASRRoute | str
    adapter_id: str
    adapter_version: str
    model_id: str
    model_fingerprint: str
    source_fingerprint: str
    preprocessing_fingerprint: str
    network_contacted: bool = False
    decoder_started: bool = False
    schema: str = ASR_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "route", _enum(self.route, ASRRoute, "receipt route"))
        _code(self.adapter_id, "receipt adapter_id")
        _version(self.adapter_version, "receipt adapter_version")
        _id(self.model_id, "receipt model_id")
        _fp(self.model_fingerprint, "receipt model_fingerprint")
        _fp(self.source_fingerprint, "receipt source_fingerprint")
        _fp(self.preprocessing_fingerprint, "receipt preprocessing_fingerprint")
        if not isinstance(self.network_contacted, bool) or not isinstance(
            self.decoder_started, bool
        ):
            raise ASRPerceptionError("receipt runtime flags must be boolean")
        if self.route is ASRRoute.STATIC_INJECTED and (
            self.network_contacted or self.decoder_started
        ):
            raise ASRPerceptionError(
                "static injected receipt cannot contact network or start decoder"
            )
        if self.schema != ASR_PERCEPTION_SCHEMA:
            raise ASRPerceptionError("unsupported ASR perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "route": cast(ASRRoute, self.route).value,
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
class ASRDocument:
    """Validated ASR output whose authority is permanently observed, never exact dialogue."""

    document_id: str
    status: ASRStatus | str
    request: ASRRequest
    segments: tuple[ASRSegment, ...] = ()
    receipt: ASRReceipt | None = None
    diagnostics: tuple[str, ...] = ()
    schema: str = ASR_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.document_id, "document_id")
        object.__setattr__(self, "status", _enum(self.status, ASRStatus, "document status"))
        if not isinstance(self.request, ASRRequest):
            raise ASRPerceptionError("document request must be ASRRequest")
        if not isinstance(self.segments, tuple) or len(self.segments) > self.request.max_segments:
            raise ASRPerceptionError("document segments exceed the request limit")
        if not all(isinstance(value, ASRSegment) for value in self.segments):
            raise ASRPerceptionError("document segments contain an invalid value")
        segment_ids = tuple(value.segment_id for value in self.segments)
        if len(segment_ids) != len(set(segment_ids)):
            raise ASRPerceptionError("document segment IDs must be unique")
        total_words = sum(len(value.words) for value in self.segments)
        if total_words > self.request.max_words:
            raise ASRPerceptionError("document words exceed the request limit")
        previous: tuple[int, int, str] | None = None
        previous_segment: ASRSegment | None = None
        for segment in self.segments:
            self._validate_span(segment.span, "segment")
            current = (segment.span.start.ticks, segment.span.end.ticks, segment.segment_id)
            if previous is not None and current < previous:
                raise ASRPerceptionError("segments must be deterministically ordered by source PTS")
            if (
                previous_segment is not None
                and segment.span.start.ticks < previous_segment.span.end.ticks
            ):
                if (
                    not previous_segment.overlap_group
                    or previous_segment.overlap_group != segment.overlap_group
                ):
                    raise ASRPerceptionError("overlapping ASR segments require one explicit group")
            previous = current
            previous_segment = segment
        if not isinstance(self.diagnostics, tuple) or len(self.diagnostics) > MAX_ASR_DIAGNOSTICS:
            raise ASRPerceptionError("document diagnostics exceed the finite limit")
        for diagnostic in self.diagnostics:
            _text(diagnostic, "diagnostic", 512)
        if self.receipt is not None and not isinstance(self.receipt, ASRReceipt):
            raise ASRPerceptionError("document receipt is invalid")
        terminal = {ASRStatus.EMPTY, ASRStatus.CORRUPT, ASRStatus.UNSUPPORTED, ASRStatus.CANCELLED}
        if self.status in terminal and (self.segments or self.receipt is not None):
            raise ASRPerceptionError("terminal document cannot contain hypotheses or receipt")
        if self.status is ASRStatus.COMPLETE:
            if not self.segments or self.receipt is None:
                raise ASRPerceptionError("complete ASR document requires segments and receipt")
            if self.receipt.route is not self.request.route:
                raise ASRPerceptionError("receipt route does not match request")
            if self.receipt.source_fingerprint != self.request.source_fingerprint:
                raise ASRPerceptionError("receipt source fingerprint does not match request")
            if self.receipt.preprocessing_fingerprint != self.request.preprocessing_fingerprint:
                raise ASRPerceptionError("receipt preprocessing fingerprint does not match request")
        if self.schema != ASR_PERCEPTION_SCHEMA:
            raise ASRPerceptionError("unsupported ASR perception schema")
        if len(self.to_wire_bytes()) > MAX_ASR_OUTPUT_BYTES:
            raise ASRPerceptionError("ASR document exceeds the output limit")

    def _validate_span(self, span: AudioSourceSpan, field: str) -> None:
        if span.asset_id != self.request.asset_id or span.source_id != self.request.source_id:
            raise ASRPerceptionError(f"{field} source ownership differs from request")
        if span.source_fingerprint != self.request.source_fingerprint:
            raise ASRPerceptionError(f"{field} source fingerprint differs from request")
        if span.end.ticks > self.request.duration_end.ticks:
            raise ASRPerceptionError(f"{field} exceeds request duration")

    @property
    def complete(self) -> bool:
        return self.status is ASRStatus.COMPLETE

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "document_id": self.document_id,
            "status": cast(ASRStatus, self.status).value,
            "authority_policy": "observed_asr_not_exact_dialogue",
            "request": self.request.to_wire(),
            "segments": [item.to_wire() for item in self.segments],
            "receipt": None if self.receipt is None else self.receipt.to_wire(),
            "diagnostics": list(self.diagnostics),
        }

    def to_wire_bytes(self) -> bytes:
        return json.dumps(
            self.to_wire(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()


@runtime_checkable
class ASRCancellationProbe(Protocol):
    def is_cancelled(self) -> bool:
        """Return whether the caller requested cancellation."""


ASRProducer = Callable[[ASRRequest], ASRDocument]


def execute_asr(
    producer: ASRProducer,
    request: ASRRequest,
    *,
    cancellation_probe: ASRCancellationProbe | None = None,
) -> ASRDocument:
    """Run one explicitly injected ASR producer without process/network/provider discovery."""

    if not callable(producer):
        raise ASRPerceptionError("ASR producer must be callable")
    if not isinstance(request, ASRRequest):
        raise ASRPerceptionError("request must be ASRRequest")
    if cancellation_probe is not None and (
        not isinstance(cancellation_probe, ASRCancellationProbe)
        or cancellation_probe.is_cancelled()
    ):
        raise ASRPerceptionError("ASR execution cancelled")
    document = producer(request)
    if not isinstance(document, ASRDocument):
        raise ASRPerceptionError("producer returned an invalid ASR document")
    if cancellation_probe is not None and cancellation_probe.is_cancelled():
        raise ASRPerceptionError("ASR execution cancelled")
    return document


def build_asr_abstention(
    request: ASRRequest, status: ASRStatus | str, diagnostic: str
) -> ASRDocument:
    """Build a terminal no-speech/corrupt/unsupported/cancelled document without hypotheses."""

    status_value = _enum(status, ASRStatus, "abstention status")
    if status_value not in {
        ASRStatus.EMPTY,
        ASRStatus.CORRUPT,
        ASRStatus.UNSUPPORTED,
        ASRStatus.CANCELLED,
    }:
        raise ASRPerceptionError("abstention status must be terminal")
    if not isinstance(request, ASRRequest):
        raise ASRPerceptionError("request must be ASRRequest")
    return ASRDocument(
        document_id=f"abstention.{status_value.value}",
        status=status_value,
        request=request,
        diagnostics=(diagnostic,),
    )


@dataclass(frozen=True, slots=True)
class ASRBenchmarkFixture:
    case_id: str
    kind: ASRCaseKind | str
    capabilities: tuple[ASRCapability, ...]
    source_fingerprint: str
    annotation_fingerprint: str
    expected_status: ASRStatus | str
    should_abstain: bool
    overlap: bool = False
    tags: tuple[str, ...] = ()
    schema: str = ASR_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.case_id, "fixture case_id")
        object.__setattr__(self, "kind", _enum(self.kind, ASRCaseKind, "fixture kind"))
        if not isinstance(self.capabilities, tuple) or not self.capabilities:
            raise ASRPerceptionError("fixture capabilities must be non-empty")
        if not all(isinstance(value, ASRCapability) for value in self.capabilities):
            raise ASRPerceptionError("fixture capabilities contain an invalid value")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ASRPerceptionError("fixture capabilities must not duplicate values")
        _fp(self.source_fingerprint, "fixture source_fingerprint")
        _fp(self.annotation_fingerprint, "fixture annotation_fingerprint")
        object.__setattr__(
            self,
            "expected_status",
            _enum(self.expected_status, ASRStatus, "fixture expected_status"),
        )
        if not isinstance(self.should_abstain, bool) or not isinstance(self.overlap, bool):
            raise ASRPerceptionError("fixture boolean fields must be boolean")
        if not isinstance(self.tags, tuple) or len(self.tags) > 16:
            raise ASRPerceptionError("fixture tags must be bounded")
        for tag in self.tags:
            _code(tag, "fixture tag")
        if self.schema != ASR_BENCHMARK_SCHEMA:
            raise ASRPerceptionError("unsupported ASR benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "case_id": self.case_id,
            "kind": cast(ASRCaseKind, self.kind).value,
            "capabilities": [item.value for item in self.capabilities],
            "source_fingerprint": self.source_fingerprint,
            "annotation_fingerprint": self.annotation_fingerprint,
            "expected_status": cast(ASRStatus, self.expected_status).value,
            "should_abstain": self.should_abstain,
            "overlap": self.overlap,
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class ASRBenchmarkThreshold:
    metric_id: str
    metric: ASRMetricKind | str
    capability: ASRCapability
    unit: ASRMetricUnit | str
    minimum: int | None = None
    maximum: int | None = None
    schema: str = ASR_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.metric_id, "threshold metric_id")
        object.__setattr__(self, "metric", _enum(self.metric, ASRMetricKind, "threshold metric"))
        if not isinstance(self.capability, ASRCapability):
            raise ASRPerceptionError("threshold capability must be ASRCapability")
        object.__setattr__(self, "unit", _enum(self.unit, ASRMetricUnit, "threshold unit"))
        if (self.minimum is None) == (self.maximum is None):
            raise ASRPerceptionError("threshold requires exactly one bound")
        value = self.minimum if self.minimum is not None else self.maximum
        if value is None or isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ASRPerceptionError("threshold bound must be a non-negative integer")
        if self.schema != ASR_BENCHMARK_SCHEMA:
            raise ASRPerceptionError("unsupported ASR benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "metric_id": self.metric_id,
            "metric": cast(ASRMetricKind, self.metric).value,
            "capability": self.capability.value,
            "unit": cast(ASRMetricUnit, self.unit).value,
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


@dataclass(frozen=True, slots=True)
class ASRCandidateProfile:
    candidate_id: str
    family: ASRCandidateFamily | str
    adapter_id: str
    adapter_version: str
    model_id: str
    capabilities: tuple[ASRCapability, ...]
    requires_network: bool
    supports_determinism: bool
    supports_cancellation_cleanup: bool
    disposition: ASRDisposition | str
    disposition_reason: str
    schema: str = ASR_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.candidate_id, "candidate_id")
        object.__setattr__(
            self, "family", _enum(self.family, ASRCandidateFamily, "candidate family")
        )
        _code(self.adapter_id, "candidate adapter_id")
        _version(self.adapter_version, "candidate adapter_version")
        _id(self.model_id, "candidate model_id")
        if not isinstance(self.capabilities, tuple) or not self.capabilities:
            raise ASRPerceptionError("candidate capabilities must be non-empty")
        if not all(isinstance(value, ASRCapability) for value in self.capabilities):
            raise ASRPerceptionError("candidate capabilities contain an invalid value")
        if not all(
            isinstance(value, bool)
            for value in (
                self.requires_network,
                self.supports_determinism,
                self.supports_cancellation_cleanup,
            )
        ):
            raise ASRPerceptionError("candidate capability flags must be boolean")
        object.__setattr__(
            self, "disposition", _enum(self.disposition, ASRDisposition, "candidate disposition")
        )
        _text(self.disposition_reason, "candidate disposition_reason", 512)
        if self.family is ASRCandidateFamily.OLLAMA and not self.requires_network:
            raise ASRPerceptionError("Ollama candidate must disclose loopback transport")
        if self.schema != ASR_BENCHMARK_SCHEMA:
            raise ASRPerceptionError("unsupported ASR benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "candidate_id": self.candidate_id,
            "family": cast(ASRCandidateFamily, self.family).value,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "model_id": self.model_id,
            "capabilities": [item.value for item in self.capabilities],
            "requires_network": self.requires_network,
            "supports_determinism": self.supports_determinism,
            "supports_cancellation_cleanup": self.supports_cancellation_cleanup,
            "disposition": cast(ASRDisposition, self.disposition).value,
            "disposition_reason": self.disposition_reason,
        }


@dataclass(frozen=True, slots=True)
class ASRBenchmarkLimits:
    max_cases: int
    max_segments: int
    max_words: int
    max_wall_time_seconds: int
    max_total_compute_seconds: int
    max_peak_vram_mb: int
    max_peak_ram_mb: int
    max_output_bytes: int
    max_concurrency: int
    network_allowed: bool = False
    media_upload_allowed: bool = False
    schema: str = ASR_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        for value, field in (
            (self.max_cases, "limits max_cases"),
            (self.max_segments, "limits max_segments"),
            (self.max_words, "limits max_words"),
            (self.max_wall_time_seconds, "limits max_wall_time_seconds"),
            (self.max_total_compute_seconds, "limits max_total_compute_seconds"),
            (self.max_peak_vram_mb, "limits max_peak_vram_mb"),
            (self.max_peak_ram_mb, "limits max_peak_ram_mb"),
            (self.max_output_bytes, "limits max_output_bytes"),
            (self.max_concurrency, "limits max_concurrency"),
        ):
            _positive(value, field, 1_000_000_000)
        if self.network_allowed or self.media_upload_allowed:
            raise ASRPerceptionError("offline ASR benchmark cannot allow network or upload")
        if self.schema != ASR_BENCHMARK_SCHEMA:
            raise ASRPerceptionError("unsupported ASR benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "max_cases": self.max_cases,
            "max_segments": self.max_segments,
            "max_words": self.max_words,
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
class ASRRoutingPolicy:
    preference_order: tuple[ASRCandidateFamily, ...]
    automatic_fallback: bool = False
    explicit_selection_required: bool = True
    schema: str = ASR_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.preference_order, tuple) or tuple(self.preference_order) != tuple(
            ASRCandidateFamily
        ):
            raise ASRPerceptionError("routing must disclose native, Ollama, and specialist order")
        if self.automatic_fallback or not self.explicit_selection_required:
            raise ASRPerceptionError("ASR routing requires explicit selection and no fallback")
        if self.schema != ASR_BENCHMARK_SCHEMA:
            raise ASRPerceptionError("unsupported ASR benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "preference_order": [item.value for item in self.preference_order],
            "automatic_fallback": self.automatic_fallback,
            "explicit_selection_required": self.explicit_selection_required,
        }


@dataclass(frozen=True, slots=True)
class ASRBenchmarkPlan:
    plan_id: str
    plan_version: str
    fixtures: tuple[ASRBenchmarkFixture, ...]
    thresholds: tuple[ASRBenchmarkThreshold, ...]
    candidates: tuple[ASRCandidateProfile, ...]
    limits: ASRBenchmarkLimits
    routing: ASRRoutingPolicy
    schema: str = ASR_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.plan_id, "plan_id")
        _version(self.plan_version, "plan_version")
        if (
            not isinstance(self.fixtures, tuple)
            or not self.fixtures
            or len(self.fixtures) > MAX_ASR_BENCHMARK_CASES
        ):
            raise ASRPerceptionError("plan fixtures must be bounded and non-empty")
        if (
            not isinstance(self.thresholds, tuple)
            or not self.thresholds
            or len(self.thresholds) > MAX_ASR_BENCHMARK_THRESHOLDS
        ):
            raise ASRPerceptionError("plan thresholds must be bounded and non-empty")
        if (
            not isinstance(self.candidates, tuple)
            or not self.candidates
            or len(self.candidates) > MAX_ASR_BENCHMARK_CANDIDATES
        ):
            raise ASRPerceptionError("plan candidates must be bounded and non-empty")
        if not all(isinstance(value, ASRBenchmarkFixture) for value in self.fixtures):
            raise ASRPerceptionError("plan fixtures contain an invalid value")
        if not all(isinstance(value, ASRBenchmarkThreshold) for value in self.thresholds):
            raise ASRPerceptionError("plan thresholds contain an invalid value")
        if not all(isinstance(value, ASRCandidateProfile) for value in self.candidates):
            raise ASRPerceptionError("plan candidates contain an invalid value")
        if len({value.case_id for value in self.fixtures}) != len(self.fixtures):
            raise ASRPerceptionError("fixture IDs must be unique")
        if len({value.metric_id for value in self.thresholds}) != len(self.thresholds):
            raise ASRPerceptionError("threshold IDs must be unique")
        if len({value.candidate_id for value in self.candidates}) != len(self.candidates):
            raise ASRPerceptionError("candidate IDs must be unique")
        covered = {capability for fixture in self.fixtures for capability in fixture.capabilities}
        if covered != set(ASRCapability):
            raise ASRPerceptionError("ASR fixture capability coverage is incomplete")
        threshold_capabilities = {value.capability for value in self.thresholds}
        if threshold_capabilities != set(ASRCapability):
            raise ASRPerceptionError("every ASR capability requires a frozen threshold")
        if len(self.fixtures) > self.limits.max_cases:
            raise ASRPerceptionError("fixtures exceed frozen limits")
        if {value.family for value in self.candidates} != set(ASRCandidateFamily):
            raise ASRPerceptionError("plan must represent native, Ollama, and specialist families")
        if self.schema != ASR_BENCHMARK_SCHEMA:
            raise ASRPerceptionError("unsupported ASR benchmark schema")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    @property
    def executable_candidate_ids(self) -> tuple[str, ...]:
        return tuple(
            value.candidate_id
            for value in self.candidates
            if value.disposition is ASRDisposition.QUALIFIED
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
            "capability_count": len(ASRCapability),
            "threshold_count": len(self.thresholds),
            "candidate_count": len(self.candidates),
            "executable_candidate_ids": list(self.executable_candidate_ids),
            "dispositions": {
                disposition.value: sum(
                    value.disposition is disposition for value in self.candidates
                )
                for disposition in ASRDisposition
            },
            "automatic_fallback": self.routing.automatic_fallback,
            "claim_ceiling": "structural_only",
        }


def _seed(value: str) -> str:
    return canonical_fingerprint({"asr_fixture": value})


def build_default_asr_benchmark_plan() -> ASRBenchmarkPlan:
    """Build the frozen M12-02 metadata-only ASR benchmark."""

    fixtures = (
        ASRBenchmarkFixture(
            "asr.multilingual.dialogue",
            ASRCaseKind.MULTILINGUAL_DIALOGUE,
            (
                ASRCapability.SEGMENT_TIMING,
                ASRCapability.WORD_TIMING,
                ASRCapability.LANGUAGE_ID,
                ASRCapability.LANGUAGE_CONFIDENCE,
                ASRCapability.WORD_CONFIDENCE,
                ASRCapability.ALTERNATIVES,
                ASRCapability.EXACT_DIALOGUE_SEPARATION,
                ASRCapability.MULTILINGUAL,
            ),
            _seed("asr.multilingual.dialogue.source"),
            _seed("asr.multilingual.dialogue.annotation"),
            ASRStatus.COMPLETE,
            False,
            tags=("en-zh", "timing", "confidence"),
        ),
        ASRBenchmarkFixture(
            "asr.noisy.speech",
            ASRCaseKind.NOISY_SPEECH,
            (ASRCapability.NOISY_SPEECH, ASRCapability.UNCLEAR_ABSTENTION),
            _seed("asr.noisy.speech.source"),
            _seed("asr.noisy.speech.annotation"),
            ASRStatus.PARTIAL,
            True,
            tags=("noise", "abstain"),
        ),
        ASRBenchmarkFixture(
            "asr.overlap.speech",
            ASRCaseKind.OVERLAP_SPEECH,
            (ASRCapability.OVERLAP_SPEECH, ASRCapability.ALTERNATIVES),
            _seed("asr.overlap.speech.source"),
            _seed("asr.overlap.speech.annotation"),
            ASRStatus.PARTIAL,
            True,
            overlap=True,
            tags=("overlap", "separate-speakers"),
        ),
        ASRBenchmarkFixture(
            "asr.music.masking",
            ASRCaseKind.MUSIC_MASKING,
            (ASRCapability.MUSIC_MASKING, ASRCapability.UNCLEAR_ABSTENTION),
            _seed("asr.music.masking.source"),
            _seed("asr.music.masking.annotation"),
            ASRStatus.PARTIAL,
            True,
            tags=("music", "masked"),
        ),
        ASRBenchmarkFixture(
            "asr.silence.no_speech",
            ASRCaseKind.SILENCE,
            (ASRCapability.SILENCE_ABSTENTION,),
            _seed("asr.silence.no_speech.source"),
            _seed("asr.silence.no_speech.annotation"),
            ASRStatus.EMPTY,
            True,
            tags=("silence", "no-speech"),
        ),
        ASRBenchmarkFixture(
            "asr.corrupt.adversarial",
            ASRCaseKind.CORRUPT_ADVERSARIAL,
            (ASRCapability.CORRUPTION, ASRCapability.ADVERSARIAL_METADATA),
            _seed("asr.corrupt.adversarial.source"),
            _seed("asr.corrupt.adversarial.annotation"),
            ASRStatus.CORRUPT,
            True,
            tags=("corrupt", "metadata-injection"),
        ),
    )
    threshold_specs: dict[
        ASRCapability, tuple[ASRMetricKind, ASRMetricUnit, int | None, int | None]
    ] = {
        ASRCapability.SEGMENT_TIMING: (
            ASRMetricKind.TIMESTAMP_ERROR,
            ASRMetricUnit.MILLISECONDS,
            None,
            80,
        ),
        ASRCapability.WORD_TIMING: (
            ASRMetricKind.TIMESTAMP_ERROR,
            ASRMetricUnit.MILLISECONDS,
            None,
            120,
        ),
        ASRCapability.LANGUAGE_ID: (
            ASRMetricKind.LANGUAGE_ACCURACY,
            ASRMetricUnit.BASIS_POINTS,
            9_000,
            None,
        ),
        ASRCapability.LANGUAGE_CONFIDENCE: (
            ASRMetricKind.CONFIDENCE_CALIBRATION,
            ASRMetricUnit.BASIS_POINTS,
            None,
            1_000,
        ),
        ASRCapability.WORD_CONFIDENCE: (
            ASRMetricKind.CONFIDENCE_CALIBRATION,
            ASRMetricUnit.BASIS_POINTS,
            None,
            1_500,
        ),
        ASRCapability.ALTERNATIVES: (
            ASRMetricKind.ALTERNATIVE_RECALL,
            ASRMetricUnit.BASIS_POINTS,
            8_000,
            None,
        ),
        ASRCapability.UNCLEAR_ABSTENTION: (
            ASRMetricKind.UNCLEAR_ABSTENTION,
            ASRMetricUnit.BASIS_POINTS,
            9_000,
            None,
        ),
        ASRCapability.EXACT_DIALOGUE_SEPARATION: (
            ASRMetricKind.EXACT_AUTHORITY_VIOLATIONS,
            ASRMetricUnit.COUNT,
            None,
            0,
        ),
        ASRCapability.MULTILINGUAL: (
            ASRMetricKind.LANGUAGE_ACCURACY,
            ASRMetricUnit.BASIS_POINTS,
            8_500,
            None,
        ),
        ASRCapability.NOISY_SPEECH: (ASRMetricKind.WER, ASRMetricUnit.BASIS_POINTS, None, 3_500),
        ASRCapability.OVERLAP_SPEECH: (
            ASRMetricKind.ALTERNATIVE_RECALL,
            ASRMetricUnit.BASIS_POINTS,
            7_000,
            None,
        ),
        ASRCapability.MUSIC_MASKING: (
            ASRMetricKind.UNCLEAR_ABSTENTION,
            ASRMetricUnit.BASIS_POINTS,
            9_000,
            None,
        ),
        ASRCapability.SILENCE_ABSTENTION: (
            ASRMetricKind.UNCLEAR_ABSTENTION,
            ASRMetricUnit.BASIS_POINTS,
            10_000,
            None,
        ),
        ASRCapability.CORRUPTION: (
            ASRMetricKind.UNCLEAR_ABSTENTION,
            ASRMetricUnit.BASIS_POINTS,
            10_000,
            None,
        ),
        ASRCapability.ADVERSARIAL_METADATA: (
            ASRMetricKind.EXACT_AUTHORITY_VIOLATIONS,
            ASRMetricUnit.COUNT,
            None,
            0,
        ),
    }
    thresholds = tuple(
        ASRBenchmarkThreshold(
            f"asr.{capability.value}",
            metric,
            capability,
            unit,
            minimum,
            maximum,
        )
        for capability, (metric, unit, minimum, maximum) in threshold_specs.items()
    )
    candidates = (
        ASRCandidateProfile(
            "native.comfyui.asr",
            ASRCandidateFamily.COMFYUI_NATIVE,
            "comfyui_native_asr",
            "1.0.0",
            "host-owned-audio-asr-capability",
            tuple(ASRCapability),
            False,
            True,
            False,
            ASRDisposition.UNAVAILABLE,
            "pinned ComfyUI audio ASR model and host lane were not admitted",
        ),
        ASRCandidateProfile(
            "fallback.ollama.asr",
            ASRCandidateFamily.OLLAMA,
            "ollama_native_api",
            "1.0.0",
            "explicitly-selected-loopback-asr-model",
            tuple(ASRCapability),
            True,
            False,
            False,
            ASRDisposition.UNAVAILABLE,
            "loopback Ollama server and explicitly selected ASR model were not started",
        ),
        ASRCandidateProfile(
            "specialist.asr.placeholder",
            ASRCandidateFamily.SPECIALIST,
            "specialist_not_selected",
            "1.0.0",
            "explicit-selection-required",
            tuple(ASRCapability),
            False,
            True,
            True,
            ASRDisposition.UNSUPPORTED,
            "no specialist ASR profile is selected in the native-first lane",
        ),
    )
    return ASRBenchmarkPlan(
        "m12-02.asr-perception-benchmark",
        "1.0.0",
        fixtures,
        thresholds,
        candidates,
        ASRBenchmarkLimits(
            6, MAX_ASR_SEGMENTS, MAX_ASR_WORDS, 60, 180, 16_384, 32_768, MAX_ASR_OUTPUT_BYTES, 1
        ),
        ASRRoutingPolicy(tuple(ASRCandidateFamily)),
    )


__all__ = [
    "ASR_BENCHMARK_SCHEMA",
    "ASR_PERCEPTION_SCHEMA",
    "MAX_ASR_ALTERNATIVES",
    "MAX_ASR_BENCHMARK_CANDIDATES",
    "MAX_ASR_BENCHMARK_CASES",
    "MAX_ASR_BENCHMARK_THRESHOLDS",
    "MAX_ASR_DIAGNOSTICS",
    "MAX_ASR_OUTPUT_BYTES",
    "MAX_ASR_SEGMENTS",
    "MAX_ASR_TEXT_LENGTH",
    "MAX_ASR_UNCERTAINTIES",
    "MAX_ASR_WORDS",
    "ASRAlternative",
    "ASRBenchmarkFixture",
    "ASRBenchmarkLimits",
    "ASRBenchmarkPlan",
    "ASRBenchmarkThreshold",
    "ASRCaseKind",
    "ASRCandidateFamily",
    "ASRCandidateProfile",
    "ASRCancellationProbe",
    "ASRCapability",
    "ASRDisposition",
    "ASRDocument",
    "ASRMetricKind",
    "ASRMetricUnit",
    "ASRReceipt",
    "ASRRequest",
    "ASRRoute",
    "ASRRoutingPolicy",
    "ASRSegment",
    "ASRStatus",
    "ASRTextAuthority",
    "ASRTextStatus",
    "ASRUncertainty",
    "ASRUncertaintyKind",
    "ASRWord",
    "build_asr_abstention",
    "build_default_asr_benchmark_plan",
    "execute_asr",
]
