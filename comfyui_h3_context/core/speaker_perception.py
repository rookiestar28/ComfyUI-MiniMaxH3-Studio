"""M12-03 source-owned speaker diarization and voice-reference contracts.

Only bounded, redacted turns, hypotheses, and association evidence cross this seam.  Raw audio,
embeddings, voiceprints, model runtimes, ComfyUI, Ollama, specialist processes, and network clients
remain outside the pure core.  Speaker labels are source-local hypotheses, not legal identity.
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
from .errors import SpeakerPerceptionError
from .media_admission import PresentationTimestamp

SPEAKER_PERCEPTION_SCHEMA = "h3.audio.speaker.v1"
SPEAKER_BENCHMARK_SCHEMA = "h3.audio.speaker_benchmark.v1"
MAX_SPEAKER_TURNS = 64
MAX_SPEAKER_HYPOTHESES = 64
MAX_SPEAKER_ASSOCIATIONS = 32
MAX_SPEAKER_UNCERTAINTIES = 8
MAX_SPEAKER_DIAGNOSTICS = 32
MAX_SPEAKER_OUTPUT_BYTES = 65_536
MAX_SPEAKER_BENCHMARK_CASES = 8
MAX_SPEAKER_BENCHMARK_THRESHOLDS = 64
MAX_SPEAKER_BENCHMARK_CANDIDATES = 8

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


class SpeakerStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"


class SpeakerRoute(str, Enum):
    STATIC_INJECTED = "static_injected"
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"
    SPECIALIST = "specialist"


class SpeakerCandidateFamily(str, Enum):
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"
    SPECIALIST = "specialist"


class SpeakerDisposition(str, Enum):
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


class SpeakerRetention(str, Enum):
    EPHEMERAL = "ephemeral"
    SESSION = "session"
    EXPLICIT = "explicit"


class SpeakerAssociationEvidence(str, Enum):
    AV_EVIDENCE = "av_evidence"
    USER_SELECTION = "user_selection"


class SpeakerUncertaintyKind(str, Enum):
    UNKNOWN_SPEAKER = "unknown_speaker"
    LOW_CONFIDENCE = "low_confidence"
    OVERLAP = "overlap"
    AMBIGUOUS = "ambiguous"
    FALSE_LINK_RISK = "false_link_risk"
    TIMING_UNCERTAIN = "timing_uncertain"
    AV_ASSOCIATION_MISSING = "av_association_missing"
    PRIVACY_REDACTED = "privacy_redacted"
    CORRUPT = "corrupt"
    NO_SPEECH = "no_speech"


class SpeakerCapability(str, Enum):
    TURN_TIMING = "turn_timing"
    OVERLAP = "overlap"
    UNKNOWN_SPEAKER = "unknown_speaker"
    SPEAKER_COUNT = "speaker_count"
    CROSS_ASSET_HYPOTHESIS = "cross_asset_hypothesis"
    EMBEDDING_OPT_IN = "embedding_opt_in"
    RETENTION_CONTROL = "retention_control"
    AV_ASSOCIATION_GATE = "av_association_gate"
    USER_SELECTION_GATE = "user_selection_gate"
    AMBIGUITY = "ambiguity"
    DER = "der"
    FALSE_LINK = "false_link"
    PRIVACY_NO_LOG = "privacy_no_log"
    CORRUPTION = "corruption"
    ADVERSARIAL_METADATA = "adversarial_metadata"


class SpeakerCaseKind(str, Enum):
    CLEAN_TURNS = "clean_turns"
    OVERLAP_TURNS = "overlap_turns"
    CROSS_ASSET_AMBIGUITY = "cross_asset_ambiguity"
    ASSOCIATION_GATE = "association_gate"
    CORRUPT_PRIVACY = "corrupt_privacy"


class SpeakerMetricKind(str, Enum):
    DER = "der"
    OVERLAP_RECALL = "overlap_recall"
    FALSE_LINK_RATE = "false_link_rate"
    PRIVACY_VIOLATIONS = "privacy_violations"
    ASSOCIATION_GATE = "association_gate"
    ABSTENTION = "abstention"


class SpeakerMetricUnit(str, Enum):
    BASIS_POINTS = "basis_points"
    COUNT = "count"


def _id(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise SpeakerPerceptionError(f"{field} must be a bounded identifier")
    if any(marker in value.casefold() for marker in ("/", "\\", *_SENSITIVE)):
        raise SpeakerPerceptionError(f"{field} contains locator or sensitive material")
    return value


def _code(value: object, field: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value.casefold()) is None:
        raise SpeakerPerceptionError(f"{field} must be a lower-case bounded code")
    return value.casefold()


def _version(value: object, field: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise SpeakerPerceptionError(f"{field} must be a numeric version")
    return value


def _fp(value: object, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise SpeakerPerceptionError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _text(value: object, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise SpeakerPerceptionError(f"{field} must be bounded non-empty text")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise SpeakerPerceptionError(f"{field} contains a control character")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise SpeakerPerceptionError(f"{field} contains locator or sensitive material")
    return value


def _confidence(value: object, field: str) -> object:
    from decimal import Decimal

    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 1:
        raise SpeakerPerceptionError(f"{field} must be a Decimal between 0 and 1")
    return value


def _non_negative(value: object, field: str, maximum: int = 10_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise SpeakerPerceptionError(f"{field} must be between 0 and {maximum}")
    return value


def _positive(value: object, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise SpeakerPerceptionError(f"{field} must be between 1 and {maximum}")
    return value


def _enum(value: object, expected: type[Enum], field: str) -> Enum:
    try:
        return value if isinstance(value, expected) else expected(value)
    except (TypeError, ValueError):
        raise SpeakerPerceptionError(f"{field} is unsupported") from None


def _uncertainties(values: object, field: str) -> tuple[SpeakerUncertainty, ...]:
    if not isinstance(values, tuple) or len(values) > MAX_SPEAKER_UNCERTAINTIES:
        raise SpeakerPerceptionError(f"{field} must be a bounded tuple")
    if not all(isinstance(value, SpeakerUncertainty) for value in values):
        raise SpeakerPerceptionError(f"{field} contains an invalid value")
    kinds = tuple(value.kind for value in values)
    if len(kinds) != len(set(kinds)):
        raise SpeakerPerceptionError(f"{field} must not duplicate uncertainty kinds")
    return values


@dataclass(frozen=True, slots=True)
class SpeakerReference:
    """Canonical cross-asset voice-reference identity without retaining media or vectors."""

    asset_id: str
    source_id: str
    source_fingerprint: str
    schema: str = SPEAKER_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.asset_id, "reference asset_id")
        _id(self.source_id, "reference source_id")
        _fp(self.source_fingerprint, "reference source_fingerprint")
        if self.schema != SPEAKER_PERCEPTION_SCHEMA:
            raise SpeakerPerceptionError("unsupported speaker perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class SpeakerRequest:
    """Explicit source, reference, privacy, and retention policy for speaker analysis."""

    asset_id: str
    source_id: str
    source_fingerprint: str
    preprocessing_fingerprint: str
    duration_end: PresentationTimestamp
    references: tuple[SpeakerReference, ...] = ()
    embedding_requested: bool = False
    biometric_opt_in: bool = False
    retention: SpeakerRetention | str = SpeakerRetention.EPHEMERAL
    user_selected_speaker_ids: tuple[str, ...] = ()
    route: SpeakerRoute | str = SpeakerRoute.STATIC_INJECTED
    max_turns: int = MAX_SPEAKER_TURNS
    max_hypotheses: int = MAX_SPEAKER_HYPOTHESES
    max_associations: int = MAX_SPEAKER_ASSOCIATIONS
    schema: str = SPEAKER_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.asset_id, "request asset_id")
        _id(self.source_id, "request source_id")
        _fp(self.source_fingerprint, "request source_fingerprint")
        _fp(self.preprocessing_fingerprint, "request preprocessing_fingerprint")
        if not isinstance(self.duration_end, PresentationTimestamp) or self.duration_end.ticks <= 0:
            raise SpeakerPerceptionError("request duration_end must be positive")
        if not isinstance(self.references, tuple) or len(self.references) > 16:
            raise SpeakerPerceptionError("request references must be bounded")
        if not all(isinstance(value, SpeakerReference) for value in self.references):
            raise SpeakerPerceptionError("request references contain an invalid value")
        reference_ids = tuple(value.asset_id for value in self.references)
        if len(reference_ids) != len(set(reference_ids)):
            raise SpeakerPerceptionError("request reference asset IDs must be unique")
        if not isinstance(self.embedding_requested, bool) or not isinstance(
            self.biometric_opt_in, bool
        ):
            raise SpeakerPerceptionError("request embedding flags must be boolean")
        if self.embedding_requested and not self.biometric_opt_in:
            raise SpeakerPerceptionError("embedding output requires explicit biometric opt-in")
        object.__setattr__(
            self, "retention", _enum(self.retention, SpeakerRetention, "request retention")
        )
        if (
            not isinstance(self.user_selected_speaker_ids, tuple)
            or len(self.user_selected_speaker_ids) > MAX_SPEAKER_TURNS
        ):
            raise SpeakerPerceptionError("user_selected_speaker_ids must be bounded")
        selected = tuple(
            _id(value, "selected speaker ID") for value in self.user_selected_speaker_ids
        )
        if len(selected) != len(set(selected)):
            raise SpeakerPerceptionError("user_selected_speaker_ids must be unique")
        object.__setattr__(self, "user_selected_speaker_ids", selected)
        object.__setattr__(self, "route", _enum(self.route, SpeakerRoute, "request route"))
        _positive(self.max_turns, "request max_turns", MAX_SPEAKER_TURNS)
        _positive(self.max_hypotheses, "request max_hypotheses", MAX_SPEAKER_HYPOTHESES)
        _positive(self.max_associations, "request max_associations", MAX_SPEAKER_ASSOCIATIONS)
        if self.schema != SPEAKER_PERCEPTION_SCHEMA:
            raise SpeakerPerceptionError("unsupported speaker perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
            "preprocessing_fingerprint": self.preprocessing_fingerprint,
            "duration_end": self.duration_end.to_wire(),
            "references": [value.to_wire() for value in self.references],
            "embedding_requested": self.embedding_requested,
            "biometric_opt_in": self.biometric_opt_in,
            "retention": cast(SpeakerRetention, self.retention).value,
            "user_selected_speaker_ids": list(self.user_selected_speaker_ids),
            "route": cast(SpeakerRoute, self.route).value,
            "max_turns": self.max_turns,
            "max_hypotheses": self.max_hypotheses,
            "max_associations": self.max_associations,
        }


@dataclass(frozen=True, slots=True)
class SpeakerUncertainty:
    kind: SpeakerUncertaintyKind | str
    detail: str
    schema: str = SPEAKER_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "kind", _enum(self.kind, SpeakerUncertaintyKind, "uncertainty kind")
        )
        _text(self.detail, "uncertainty detail")
        if self.schema != SPEAKER_PERCEPTION_SCHEMA:
            raise SpeakerPerceptionError("unsupported speaker perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "kind": cast(SpeakerUncertaintyKind, self.kind).value,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    """Source-owned diarized turn; labels are local hypotheses, never legal identity."""

    turn_id: str
    span: AudioSourceSpan
    speaker_label: str | None
    confidence: object
    uncertainties: tuple[SpeakerUncertainty, ...] = ()
    overlap_group: str | None = None
    embedding_fingerprint: str | None = None
    embedding_retention: SpeakerRetention | str | None = None
    schema: str = SPEAKER_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.turn_id, "turn_id")
        if not isinstance(self.span, AudioSourceSpan):
            raise SpeakerPerceptionError("turn span must be AudioSourceSpan")
        if self.speaker_label is not None:
            _id(self.speaker_label, "speaker_label")
        _confidence(self.confidence, "turn confidence")
        object.__setattr__(
            self, "uncertainties", _uncertainties(self.uncertainties, "turn uncertainties")
        )
        if self.speaker_label is None and not any(
            value.kind is SpeakerUncertaintyKind.UNKNOWN_SPEAKER for value in self.uncertainties
        ):
            raise SpeakerPerceptionError(
                "unknown speaker turns require unknown_speaker uncertainty"
            )
        if self.overlap_group is not None:
            _id(self.overlap_group, "turn overlap_group")
        if self.embedding_fingerprint is not None:
            _fp(self.embedding_fingerprint, "turn embedding_fingerprint")
            if self.embedding_retention is None:
                raise SpeakerPerceptionError("embedding fingerprint requires retention policy")
        elif self.embedding_retention is not None:
            raise SpeakerPerceptionError("retention policy requires an embedding fingerprint")
        if self.embedding_retention is not None:
            object.__setattr__(
                self,
                "embedding_retention",
                _enum(self.embedding_retention, SpeakerRetention, "embedding retention"),
            )
        if self.schema != SPEAKER_PERCEPTION_SCHEMA:
            raise SpeakerPerceptionError("unsupported speaker perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "turn_id": self.turn_id,
            "span": self.span.to_wire(),
            "speaker_label": self.speaker_label,
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "uncertainties": [value.to_wire() for value in self.uncertainties],
            "overlap_group": self.overlap_group,
            "embedding_fingerprint": self.embedding_fingerprint,
            "embedding_retention": (
                None
                if self.embedding_retention is None
                else cast(SpeakerRetention, self.embedding_retention).value
            ),
        }


@dataclass(frozen=True, slots=True)
class SpeakerHypothesis:
    """Ambiguous cross-asset voice hypothesis with bounded false-link risk."""

    hypothesis_id: str
    speaker_label: str
    reference: SpeakerReference
    confidence: object
    false_link_risk_bps: int
    ambiguous: bool
    user_confirmed: bool
    uncertainties: tuple[SpeakerUncertainty, ...] = ()
    schema: str = SPEAKER_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.hypothesis_id, "hypothesis_id")
        _id(self.speaker_label, "hypothesis speaker_label")
        if not isinstance(self.reference, SpeakerReference):
            raise SpeakerPerceptionError("hypothesis reference is invalid")
        _confidence(self.confidence, "hypothesis confidence")
        _non_negative(self.false_link_risk_bps, "false_link_risk_bps")
        if not isinstance(self.ambiguous, bool) or not isinstance(self.user_confirmed, bool):
            raise SpeakerPerceptionError("hypothesis boolean fields must be boolean")
        if self.user_confirmed and self.ambiguous:
            raise SpeakerPerceptionError("user-confirmed hypothesis cannot remain ambiguous")
        object.__setattr__(
            self, "uncertainties", _uncertainties(self.uncertainties, "hypothesis uncertainties")
        )
        if self.schema != SPEAKER_PERCEPTION_SCHEMA:
            raise SpeakerPerceptionError("unsupported speaker perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "hypothesis_id": self.hypothesis_id,
            "speaker_label": self.speaker_label,
            "reference": self.reference.to_wire(),
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "false_link_risk_bps": self.false_link_risk_bps,
            "ambiguous": self.ambiguous,
            "user_confirmed": self.user_confirmed,
            "uncertainties": [value.to_wire() for value in self.uncertainties],
        }


@dataclass(frozen=True, slots=True)
class SpeakerAssociation:
    """Visible-person association admitted only through AV evidence or explicit user selection."""

    association_id: str
    speaker_label: str
    video_asset_id: str
    evidence_kind: SpeakerAssociationEvidence | str
    confidence: object
    video_source_fingerprint: str | None = None
    evidence_fingerprint: str | None = None
    user_selected: bool = False
    video_source_id: str | None = None
    schema: str = SPEAKER_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.association_id, "association_id")
        _id(self.speaker_label, "association speaker_label")
        _id(self.video_asset_id, "association video_asset_id")
        object.__setattr__(
            self,
            "evidence_kind",
            _enum(self.evidence_kind, SpeakerAssociationEvidence, "association evidence_kind"),
        )
        _confidence(self.confidence, "association confidence")
        if self.video_source_id is not None:
            _id(self.video_source_id, "association video_source_id")
        if self.video_source_fingerprint is not None:
            _fp(self.video_source_fingerprint, "association video_source_fingerprint")
        if self.evidence_fingerprint is not None:
            _fp(self.evidence_fingerprint, "association evidence_fingerprint")
        if not isinstance(self.user_selected, bool):
            raise SpeakerPerceptionError("association user_selected must be boolean")
        evidence = cast(SpeakerAssociationEvidence, self.evidence_kind)
        if evidence is SpeakerAssociationEvidence.AV_EVIDENCE:
            if self.video_source_fingerprint is None or self.evidence_fingerprint is None:
                raise SpeakerPerceptionError(
                    "AV association requires video and evidence fingerprints"
                )
        elif not self.user_selected:
            raise SpeakerPerceptionError("user-selection association requires explicit selection")
        if (
            evidence is SpeakerAssociationEvidence.USER_SELECTION
            and self.evidence_fingerprint is not None
        ):
            raise SpeakerPerceptionError(
                "user-selection association cannot carry hidden AV evidence"
            )
        if self.schema != SPEAKER_PERCEPTION_SCHEMA:
            raise SpeakerPerceptionError("unsupported speaker perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "association_id": self.association_id,
            "speaker_label": self.speaker_label,
            "video_asset_id": self.video_asset_id,
            "video_source_id": self.video_source_id,
            "evidence_kind": cast(SpeakerAssociationEvidence, self.evidence_kind).value,
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "video_source_fingerprint": self.video_source_fingerprint,
            "evidence_fingerprint": self.evidence_fingerprint,
            "user_selected": self.user_selected,
        }


@dataclass(frozen=True, slots=True)
class SpeakerReceipt:
    route: SpeakerRoute | str
    adapter_id: str
    adapter_version: str
    model_id: str
    model_fingerprint: str
    source_fingerprint: str
    preprocessing_fingerprint: str
    embedding_generated: bool = False
    embedding_logged: bool = False
    network_contacted: bool = False
    decoder_started: bool = False
    schema: str = SPEAKER_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "route", _enum(self.route, SpeakerRoute, "receipt route"))
        _code(self.adapter_id, "receipt adapter_id")
        _version(self.adapter_version, "receipt adapter_version")
        _id(self.model_id, "receipt model_id")
        _fp(self.model_fingerprint, "receipt model_fingerprint")
        _fp(self.source_fingerprint, "receipt source_fingerprint")
        _fp(self.preprocessing_fingerprint, "receipt preprocessing_fingerprint")
        if not all(
            isinstance(value, bool)
            for value in (
                self.embedding_generated,
                self.embedding_logged,
                self.network_contacted,
                self.decoder_started,
            )
        ):
            raise SpeakerPerceptionError("receipt flags must be boolean")
        if self.embedding_logged:
            raise SpeakerPerceptionError("biometric-like data must never be logged")
        if cast(SpeakerRoute, self.route) is SpeakerRoute.STATIC_INJECTED and (
            self.network_contacted or self.decoder_started
        ):
            raise SpeakerPerceptionError(
                "static injected receipt cannot contact network or start decoder"
            )
        if self.schema != SPEAKER_PERCEPTION_SCHEMA:
            raise SpeakerPerceptionError("unsupported speaker perception schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "route": cast(SpeakerRoute, self.route).value,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "model_id": self.model_id,
            "model_fingerprint": self.model_fingerprint,
            "source_fingerprint": self.source_fingerprint,
            "preprocessing_fingerprint": self.preprocessing_fingerprint,
            "embedding_generated": self.embedding_generated,
            "embedding_logged": self.embedding_logged,
            "network_contacted": self.network_contacted,
            "decoder_started": self.decoder_started,
        }


@dataclass(frozen=True, slots=True)
class SpeakerDocument:
    """Validated diarization/voice evidence with explicit privacy and association boundaries."""

    document_id: str
    status: SpeakerStatus | str
    request: SpeakerRequest
    turns: tuple[SpeakerTurn, ...] = ()
    hypotheses: tuple[SpeakerHypothesis, ...] = ()
    associations: tuple[SpeakerAssociation, ...] = ()
    receipt: SpeakerReceipt | None = None
    diagnostics: tuple[str, ...] = ()
    schema: str = SPEAKER_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.document_id, "document_id")
        object.__setattr__(self, "status", _enum(self.status, SpeakerStatus, "document status"))
        if not isinstance(self.request, SpeakerRequest):
            raise SpeakerPerceptionError("document request must be SpeakerRequest")
        if not isinstance(self.turns, tuple) or len(self.turns) > self.request.max_turns:
            raise SpeakerPerceptionError("document turns exceed the request limit")
        if not all(isinstance(value, SpeakerTurn) for value in self.turns):
            raise SpeakerPerceptionError("document turns contain an invalid value")
        if (
            not isinstance(self.hypotheses, tuple)
            or len(self.hypotheses) > self.request.max_hypotheses
        ):
            raise SpeakerPerceptionError("document hypotheses exceed the request limit")
        if not all(isinstance(value, SpeakerHypothesis) for value in self.hypotheses):
            raise SpeakerPerceptionError("document hypotheses contain an invalid value")
        if (
            not isinstance(self.associations, tuple)
            or len(self.associations) > self.request.max_associations
        ):
            raise SpeakerPerceptionError("document associations exceed the request limit")
        if not all(isinstance(value, SpeakerAssociation) for value in self.associations):
            raise SpeakerPerceptionError("document associations contain an invalid value")
        if len({value.turn_id for value in self.turns}) != len(self.turns):
            raise SpeakerPerceptionError("turn IDs must be unique")
        if len({value.hypothesis_id for value in self.hypotheses}) != len(self.hypotheses):
            raise SpeakerPerceptionError("hypothesis IDs must be unique")
        if len({value.association_id for value in self.associations}) != len(self.associations):
            raise SpeakerPerceptionError("association IDs must be unique")
        previous: tuple[int, int, str] | None = None
        previous_turn: SpeakerTurn | None = None
        for turn in self.turns:
            self._validate_span(turn.span, "turn")
            if turn.embedding_fingerprint is not None:
                if not self.request.embedding_requested or not self.request.biometric_opt_in:
                    raise SpeakerPerceptionError("embedding output lacks explicit biometric opt-in")
                if turn.embedding_retention is None:
                    raise SpeakerPerceptionError("embedding output lacks retention policy")
            current = (turn.span.start.ticks, turn.span.end.ticks, turn.turn_id)
            if previous is not None and current < previous:
                raise SpeakerPerceptionError(
                    "turns must be deterministically ordered by source PTS"
                )
            if previous_turn is not None and turn.span.start.ticks < previous_turn.span.end.ticks:
                if (
                    not previous_turn.overlap_group
                    or previous_turn.overlap_group != turn.overlap_group
                ):
                    raise SpeakerPerceptionError("overlapping turns require one explicit group")
            previous = current
            previous_turn = turn
        references = {value.asset_id: value for value in self.request.references}
        selected = set(self.request.user_selected_speaker_ids)
        for hypothesis in self.hypotheses:
            reference = references.get(hypothesis.reference.asset_id)
            if reference is None or reference != hypothesis.reference:
                raise SpeakerPerceptionError("hypothesis reference ownership differs from request")
            if hypothesis.user_confirmed and hypothesis.speaker_label not in selected:
                raise SpeakerPerceptionError("user-confirmed hypothesis lacks user selection")
        turn_labels = {
            value.speaker_label for value in self.turns if value.speaker_label is not None
        }
        for association in self.associations:
            if association.speaker_label not in turn_labels:
                raise SpeakerPerceptionError("association speaker label is not source-owned")
            evidence = cast(SpeakerAssociationEvidence, association.evidence_kind)
            if evidence is SpeakerAssociationEvidence.USER_SELECTION and (
                not association.user_selected or association.speaker_label not in selected
            ):
                raise SpeakerPerceptionError("association lacks explicit user selection")
            if evidence is SpeakerAssociationEvidence.AV_EVIDENCE and (
                association.video_source_fingerprint is None
                or association.evidence_fingerprint is None
            ):
                raise SpeakerPerceptionError("association lacks audiovisual evidence ownership")
        if (
            not isinstance(self.diagnostics, tuple)
            or len(self.diagnostics) > MAX_SPEAKER_DIAGNOSTICS
        ):
            raise SpeakerPerceptionError("document diagnostics exceed the finite limit")
        for diagnostic in self.diagnostics:
            _text(diagnostic, "diagnostic")
        if self.receipt is not None and not isinstance(self.receipt, SpeakerReceipt):
            raise SpeakerPerceptionError("document receipt is invalid")
        terminal = {
            SpeakerStatus.EMPTY,
            SpeakerStatus.CORRUPT,
            SpeakerStatus.UNSUPPORTED,
            SpeakerStatus.CANCELLED,
        }
        if self.status in terminal and (
            self.turns or self.hypotheses or self.associations or self.receipt is not None
        ):
            raise SpeakerPerceptionError(
                "terminal document cannot contain speaker output or receipt"
            )
        if self.status is SpeakerStatus.COMPLETE:
            if not self.turns or self.receipt is None:
                raise SpeakerPerceptionError("complete speaker document requires turns and receipt")
            if cast(SpeakerRoute, self.receipt.route) is not cast(SpeakerRoute, self.request.route):
                raise SpeakerPerceptionError("receipt route does not match request")
            if self.receipt.source_fingerprint != self.request.source_fingerprint:
                raise SpeakerPerceptionError("receipt source fingerprint does not match request")
            if self.receipt.preprocessing_fingerprint != self.request.preprocessing_fingerprint:
                raise SpeakerPerceptionError(
                    "receipt preprocessing fingerprint does not match request"
                )
            if self.receipt.embedding_generated != any(
                value.embedding_fingerprint is not None for value in self.turns
            ):
                raise SpeakerPerceptionError("receipt embedding flag does not match turn output")
        if self.schema != SPEAKER_PERCEPTION_SCHEMA:
            raise SpeakerPerceptionError("unsupported speaker perception schema")
        if len(self.to_wire_bytes()) > MAX_SPEAKER_OUTPUT_BYTES:
            raise SpeakerPerceptionError("speaker document exceeds the output limit")

    def _validate_span(self, span: AudioSourceSpan, field: str) -> None:
        if span.asset_id != self.request.asset_id or span.source_id != self.request.source_id:
            raise SpeakerPerceptionError(f"{field} source ownership differs from request")
        if span.source_fingerprint != self.request.source_fingerprint:
            raise SpeakerPerceptionError(f"{field} source fingerprint differs from request")
        if (span.start.time_base_num, span.start.time_base_den) != (
            self.request.duration_end.time_base_num,
            self.request.duration_end.time_base_den,
        ):
            raise SpeakerPerceptionError(f"{field} time base differs from request")
        if span.end.ticks > self.request.duration_end.ticks:
            raise SpeakerPerceptionError(f"{field} exceeds request duration")

    @property
    def complete(self) -> bool:
        return self.status is SpeakerStatus.COMPLETE

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "document_id": self.document_id,
            "status": cast(SpeakerStatus, self.status).value,
            "privacy_policy": "biometric_opt_in_fingerprint_only_never_logged",
            "request": self.request.to_wire(),
            "turns": [value.to_wire() for value in self.turns],
            "hypotheses": [value.to_wire() for value in self.hypotheses],
            "associations": [value.to_wire() for value in self.associations],
            "receipt": None if self.receipt is None else self.receipt.to_wire(),
            "diagnostics": list(self.diagnostics),
        }

    def to_wire_bytes(self) -> bytes:
        return json.dumps(
            self.to_wire(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()


@runtime_checkable
class SpeakerCancellationProbe(Protocol):
    def is_cancelled(self) -> bool:
        """Return whether the caller requested cancellation."""


SpeakerProducer = Callable[[SpeakerRequest], SpeakerDocument]


def execute_speaker(
    producer: SpeakerProducer,
    request: SpeakerRequest,
    *,
    cancellation_probe: SpeakerCancellationProbe | None = None,
) -> SpeakerDocument:
    """Run one explicitly injected speaker producer without process/network discovery."""

    if not callable(producer):
        raise SpeakerPerceptionError("speaker producer must be callable")
    if not isinstance(request, SpeakerRequest):
        raise SpeakerPerceptionError("request must be SpeakerRequest")
    if cancellation_probe is not None and (
        not isinstance(cancellation_probe, SpeakerCancellationProbe)
        or cancellation_probe.is_cancelled()
    ):
        raise SpeakerPerceptionError("speaker execution cancelled")
    document = producer(request)
    if not isinstance(document, SpeakerDocument):
        raise SpeakerPerceptionError("producer returned an invalid speaker document")
    if cancellation_probe is not None and cancellation_probe.is_cancelled():
        raise SpeakerPerceptionError("speaker execution cancelled")
    return document


def build_speaker_abstention(
    request: SpeakerRequest, status: SpeakerStatus | str, diagnostic: str
) -> SpeakerDocument:
    status_value = _enum(status, SpeakerStatus, "abstention status")
    if status_value not in {
        SpeakerStatus.EMPTY,
        SpeakerStatus.CORRUPT,
        SpeakerStatus.UNSUPPORTED,
        SpeakerStatus.CANCELLED,
    }:
        raise SpeakerPerceptionError("abstention status must be terminal")
    if not isinstance(request, SpeakerRequest):
        raise SpeakerPerceptionError("request must be SpeakerRequest")
    return SpeakerDocument(
        f"abstention.{cast(SpeakerStatus, status_value).value}",
        cast(SpeakerStatus, status_value),
        request,
        diagnostics=(diagnostic,),
    )


@dataclass(frozen=True, slots=True)
class SpeakerBenchmarkFixture:
    case_id: str
    kind: SpeakerCaseKind | str
    capabilities: tuple[SpeakerCapability, ...]
    source_fingerprint: str
    annotation_fingerprint: str
    expected_status: SpeakerStatus | str
    should_abstain: bool
    overlap: bool = False
    biometric_case: bool = False
    tags: tuple[str, ...] = ()
    schema: str = SPEAKER_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.case_id, "fixture case_id")
        object.__setattr__(self, "kind", _enum(self.kind, SpeakerCaseKind, "fixture kind"))
        if not isinstance(self.capabilities, tuple) or not self.capabilities:
            raise SpeakerPerceptionError("fixture capabilities must be non-empty")
        if not all(isinstance(value, SpeakerCapability) for value in self.capabilities):
            raise SpeakerPerceptionError("fixture capabilities contain an invalid value")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise SpeakerPerceptionError("fixture capabilities must be unique")
        _fp(self.source_fingerprint, "fixture source_fingerprint")
        _fp(self.annotation_fingerprint, "fixture annotation_fingerprint")
        object.__setattr__(
            self,
            "expected_status",
            _enum(self.expected_status, SpeakerStatus, "fixture expected_status"),
        )
        if (
            not isinstance(self.should_abstain, bool)
            or not isinstance(self.overlap, bool)
            or not isinstance(self.biometric_case, bool)
        ):
            raise SpeakerPerceptionError("fixture boolean fields must be boolean")
        if not isinstance(self.tags, tuple) or len(self.tags) > 16:
            raise SpeakerPerceptionError("fixture tags must be bounded")
        for tag in self.tags:
            _code(tag, "fixture tag")
        if self.schema != SPEAKER_BENCHMARK_SCHEMA:
            raise SpeakerPerceptionError("unsupported speaker benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "case_id": self.case_id,
            "kind": cast(SpeakerCaseKind, self.kind).value,
            "capabilities": [value.value for value in self.capabilities],
            "source_fingerprint": self.source_fingerprint,
            "annotation_fingerprint": self.annotation_fingerprint,
            "expected_status": cast(SpeakerStatus, self.expected_status).value,
            "should_abstain": self.should_abstain,
            "overlap": self.overlap,
            "biometric_case": self.biometric_case,
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class SpeakerBenchmarkThreshold:
    metric_id: str
    metric: SpeakerMetricKind | str
    capability: SpeakerCapability
    unit: SpeakerMetricUnit | str
    minimum: int | None = None
    maximum: int | None = None
    schema: str = SPEAKER_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.metric_id, "threshold metric_id")
        object.__setattr__(
            self, "metric", _enum(self.metric, SpeakerMetricKind, "threshold metric")
        )
        if not isinstance(self.capability, SpeakerCapability):
            raise SpeakerPerceptionError("threshold capability must be SpeakerCapability")
        object.__setattr__(self, "unit", _enum(self.unit, SpeakerMetricUnit, "threshold unit"))
        if (self.minimum is None) == (self.maximum is None):
            raise SpeakerPerceptionError("threshold requires exactly one bound")
        value = self.minimum if self.minimum is not None else self.maximum
        if value is None or isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SpeakerPerceptionError("threshold bound must be a non-negative integer")
        if self.schema != SPEAKER_BENCHMARK_SCHEMA:
            raise SpeakerPerceptionError("unsupported speaker benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "metric_id": self.metric_id,
            "metric": cast(SpeakerMetricKind, self.metric).value,
            "capability": self.capability.value,
            "unit": cast(SpeakerMetricUnit, self.unit).value,
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


@dataclass(frozen=True, slots=True)
class SpeakerCandidateProfile:
    candidate_id: str
    family: SpeakerCandidateFamily | str
    adapter_id: str
    adapter_version: str
    model_id: str
    capabilities: tuple[SpeakerCapability, ...]
    requires_network: bool
    supports_determinism: bool
    supports_cancellation_cleanup: bool
    disposition: SpeakerDisposition | str
    disposition_reason: str
    schema: str = SPEAKER_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.candidate_id, "candidate_id")
        object.__setattr__(
            self, "family", _enum(self.family, SpeakerCandidateFamily, "candidate family")
        )
        _code(self.adapter_id, "candidate adapter_id")
        _version(self.adapter_version, "candidate adapter_version")
        _id(self.model_id, "candidate model_id")
        if not isinstance(self.capabilities, tuple) or not self.capabilities:
            raise SpeakerPerceptionError("candidate capabilities must be non-empty")
        if not all(isinstance(value, SpeakerCapability) for value in self.capabilities):
            raise SpeakerPerceptionError("candidate capabilities contain an invalid value")
        if not all(
            isinstance(value, bool)
            for value in (
                self.requires_network,
                self.supports_determinism,
                self.supports_cancellation_cleanup,
            )
        ):
            raise SpeakerPerceptionError("candidate flags must be boolean")
        object.__setattr__(
            self,
            "disposition",
            _enum(self.disposition, SpeakerDisposition, "candidate disposition"),
        )
        _text(self.disposition_reason, "candidate disposition_reason")
        if self.family is SpeakerCandidateFamily.OLLAMA and not self.requires_network:
            raise SpeakerPerceptionError("Ollama candidate must disclose loopback transport")
        if self.schema != SPEAKER_BENCHMARK_SCHEMA:
            raise SpeakerPerceptionError("unsupported speaker benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "candidate_id": self.candidate_id,
            "family": cast(SpeakerCandidateFamily, self.family).value,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "model_id": self.model_id,
            "capabilities": [value.value for value in self.capabilities],
            "requires_network": self.requires_network,
            "supports_determinism": self.supports_determinism,
            "supports_cancellation_cleanup": self.supports_cancellation_cleanup,
            "disposition": cast(SpeakerDisposition, self.disposition).value,
            "disposition_reason": self.disposition_reason,
        }


@dataclass(frozen=True, slots=True)
class SpeakerBenchmarkLimits:
    max_cases: int
    max_turns: int
    max_hypotheses: int
    max_associations: int
    max_wall_time_seconds: int
    max_total_compute_seconds: int
    max_peak_vram_mb: int
    max_peak_ram_mb: int
    max_output_bytes: int
    max_concurrency: int
    network_allowed: bool = False
    media_upload_allowed: bool = False
    schema: str = SPEAKER_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        for value, field in (
            (self.max_cases, "limits max_cases"),
            (self.max_turns, "limits max_turns"),
            (self.max_hypotheses, "limits max_hypotheses"),
            (self.max_associations, "limits max_associations"),
            (self.max_wall_time_seconds, "limits max_wall_time_seconds"),
            (self.max_total_compute_seconds, "limits max_total_compute_seconds"),
            (self.max_peak_vram_mb, "limits max_peak_vram_mb"),
            (self.max_peak_ram_mb, "limits max_peak_ram_mb"),
            (self.max_output_bytes, "limits max_output_bytes"),
            (self.max_concurrency, "limits max_concurrency"),
        ):
            _positive(value, field, 1_000_000_000)
        if self.network_allowed or self.media_upload_allowed:
            raise SpeakerPerceptionError("offline speaker benchmark cannot allow network or upload")
        if self.schema != SPEAKER_BENCHMARK_SCHEMA:
            raise SpeakerPerceptionError("unsupported speaker benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "max_cases": self.max_cases,
            "max_turns": self.max_turns,
            "max_hypotheses": self.max_hypotheses,
            "max_associations": self.max_associations,
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
class SpeakerRoutingPolicy:
    preference_order: tuple[SpeakerCandidateFamily, ...]
    automatic_fallback: bool = False
    explicit_selection_required: bool = True
    schema: str = SPEAKER_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.preference_order, tuple) or tuple(self.preference_order) != tuple(
            SpeakerCandidateFamily
        ):
            raise SpeakerPerceptionError(
                "routing must disclose native, Ollama, and specialist order"
            )
        if self.automatic_fallback or not self.explicit_selection_required:
            raise SpeakerPerceptionError(
                "speaker routing requires explicit selection and no fallback"
            )
        if self.schema != SPEAKER_BENCHMARK_SCHEMA:
            raise SpeakerPerceptionError("unsupported speaker benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "preference_order": [value.value for value in self.preference_order],
            "automatic_fallback": self.automatic_fallback,
            "explicit_selection_required": self.explicit_selection_required,
        }


@dataclass(frozen=True, slots=True)
class SpeakerBenchmarkPlan:
    plan_id: str
    plan_version: str
    fixtures: tuple[SpeakerBenchmarkFixture, ...]
    thresholds: tuple[SpeakerBenchmarkThreshold, ...]
    candidates: tuple[SpeakerCandidateProfile, ...]
    limits: SpeakerBenchmarkLimits
    routing: SpeakerRoutingPolicy
    schema: str = SPEAKER_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.plan_id, "plan_id")
        _version(self.plan_version, "plan_version")
        if (
            not isinstance(self.fixtures, tuple)
            or not self.fixtures
            or len(self.fixtures) > MAX_SPEAKER_BENCHMARK_CASES
        ):
            raise SpeakerPerceptionError("plan fixtures must be bounded and non-empty")
        if (
            not isinstance(self.thresholds, tuple)
            or not self.thresholds
            or len(self.thresholds) > MAX_SPEAKER_BENCHMARK_THRESHOLDS
        ):
            raise SpeakerPerceptionError("plan thresholds must be bounded and non-empty")
        if (
            not isinstance(self.candidates, tuple)
            or not self.candidates
            or len(self.candidates) > MAX_SPEAKER_BENCHMARK_CANDIDATES
        ):
            raise SpeakerPerceptionError("plan candidates must be bounded and non-empty")
        if not all(isinstance(value, SpeakerBenchmarkFixture) for value in self.fixtures):
            raise SpeakerPerceptionError("plan fixtures contain an invalid value")
        if not all(isinstance(value, SpeakerBenchmarkThreshold) for value in self.thresholds):
            raise SpeakerPerceptionError("plan thresholds contain an invalid value")
        if not all(isinstance(value, SpeakerCandidateProfile) for value in self.candidates):
            raise SpeakerPerceptionError("plan candidates contain an invalid value")
        if len({value.case_id for value in self.fixtures}) != len(self.fixtures):
            raise SpeakerPerceptionError("fixture IDs must be unique")
        if len({value.metric_id for value in self.thresholds}) != len(self.thresholds):
            raise SpeakerPerceptionError("threshold IDs must be unique")
        if len({value.candidate_id for value in self.candidates}) != len(self.candidates):
            raise SpeakerPerceptionError("candidate IDs must be unique")
        covered = {capability for fixture in self.fixtures for capability in fixture.capabilities}
        if covered != set(SpeakerCapability):
            raise SpeakerPerceptionError("speaker fixture capability coverage is incomplete")
        threshold_capabilities = {value.capability for value in self.thresholds}
        if threshold_capabilities != set(SpeakerCapability):
            raise SpeakerPerceptionError("every speaker capability requires a frozen threshold")
        if len(self.fixtures) > self.limits.max_cases:
            raise SpeakerPerceptionError("fixtures exceed frozen limits")
        if {value.family for value in self.candidates} != set(SpeakerCandidateFamily):
            raise SpeakerPerceptionError(
                "plan must represent native, Ollama, and specialist families"
            )
        if self.schema != SPEAKER_BENCHMARK_SCHEMA:
            raise SpeakerPerceptionError("unsupported speaker benchmark schema")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    @property
    def executable_candidate_ids(self) -> tuple[str, ...]:
        return tuple(
            value.candidate_id
            for value in self.candidates
            if value.disposition is SpeakerDisposition.QUALIFIED
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
            "capability_count": len(SpeakerCapability),
            "threshold_count": len(self.thresholds),
            "candidate_count": len(self.candidates),
            "executable_candidate_ids": list(self.executable_candidate_ids),
            "dispositions": {
                disposition.value: sum(
                    value.disposition is disposition for value in self.candidates
                )
                for disposition in SpeakerDisposition
            },
            "automatic_fallback": self.routing.automatic_fallback,
            "claim_ceiling": "structural_only",
        }


def _seed(value: str) -> str:
    return canonical_fingerprint({"speaker_fixture": value})


def build_default_speaker_benchmark_plan() -> SpeakerBenchmarkPlan:
    """Build the frozen M12-03 metadata-only speaker benchmark."""

    fixtures = (
        SpeakerBenchmarkFixture(
            "speaker.clean.turns",
            SpeakerCaseKind.CLEAN_TURNS,
            (
                SpeakerCapability.TURN_TIMING,
                SpeakerCapability.SPEAKER_COUNT,
                SpeakerCapability.PRIVACY_NO_LOG,
            ),
            _seed("speaker.clean.turns.source"),
            _seed("speaker.clean.turns.annotation"),
            SpeakerStatus.COMPLETE,
            False,
            tags=("clean", "turns"),
        ),
        SpeakerBenchmarkFixture(
            "speaker.overlap.turns",
            SpeakerCaseKind.OVERLAP_TURNS,
            (SpeakerCapability.OVERLAP, SpeakerCapability.DER),
            _seed("speaker.overlap.turns.source"),
            _seed("speaker.overlap.turns.annotation"),
            SpeakerStatus.PARTIAL,
            True,
            overlap=True,
            tags=("overlap", "der"),
        ),
        SpeakerBenchmarkFixture(
            "speaker.cross.asset.ambiguity",
            SpeakerCaseKind.CROSS_ASSET_AMBIGUITY,
            (
                SpeakerCapability.CROSS_ASSET_HYPOTHESIS,
                SpeakerCapability.AMBIGUITY,
                SpeakerCapability.FALSE_LINK,
            ),
            _seed("speaker.cross.asset.ambiguity.source"),
            _seed("speaker.cross.asset.ambiguity.annotation"),
            SpeakerStatus.PARTIAL,
            True,
            biometric_case=True,
            tags=("reference", "ambiguous"),
        ),
        SpeakerBenchmarkFixture(
            "speaker.association.gate",
            SpeakerCaseKind.ASSOCIATION_GATE,
            (
                SpeakerCapability.EMBEDDING_OPT_IN,
                SpeakerCapability.RETENTION_CONTROL,
                SpeakerCapability.AV_ASSOCIATION_GATE,
                SpeakerCapability.USER_SELECTION_GATE,
            ),
            _seed("speaker.association.gate.source"),
            _seed("speaker.association.gate.annotation"),
            SpeakerStatus.COMPLETE,
            False,
            biometric_case=True,
            tags=("av", "user-selection", "opt-in"),
        ),
        SpeakerBenchmarkFixture(
            "speaker.corrupt.privacy",
            SpeakerCaseKind.CORRUPT_PRIVACY,
            (
                SpeakerCapability.UNKNOWN_SPEAKER,
                SpeakerCapability.PRIVACY_NO_LOG,
                SpeakerCapability.CORRUPTION,
                SpeakerCapability.ADVERSARIAL_METADATA,
            ),
            _seed("speaker.corrupt.privacy.source"),
            _seed("speaker.corrupt.privacy.annotation"),
            SpeakerStatus.CORRUPT,
            True,
            tags=("corrupt", "privacy"),
        ),
    )
    threshold_specs: dict[
        SpeakerCapability, tuple[SpeakerMetricKind, SpeakerMetricUnit, int | None, int | None]
    ] = {
        SpeakerCapability.TURN_TIMING: (
            SpeakerMetricKind.DER,
            SpeakerMetricUnit.BASIS_POINTS,
            None,
            1_500,
        ),
        SpeakerCapability.OVERLAP: (
            SpeakerMetricKind.OVERLAP_RECALL,
            SpeakerMetricUnit.BASIS_POINTS,
            9_000,
            None,
        ),
        SpeakerCapability.UNKNOWN_SPEAKER: (
            SpeakerMetricKind.ABSTENTION,
            SpeakerMetricUnit.BASIS_POINTS,
            10_000,
            None,
        ),
        SpeakerCapability.SPEAKER_COUNT: (
            SpeakerMetricKind.DER,
            SpeakerMetricUnit.BASIS_POINTS,
            None,
            2_000,
        ),
        SpeakerCapability.CROSS_ASSET_HYPOTHESIS: (
            SpeakerMetricKind.FALSE_LINK_RATE,
            SpeakerMetricUnit.BASIS_POINTS,
            None,
            5_000,
        ),
        SpeakerCapability.EMBEDDING_OPT_IN: (
            SpeakerMetricKind.PRIVACY_VIOLATIONS,
            SpeakerMetricUnit.COUNT,
            None,
            0,
        ),
        SpeakerCapability.RETENTION_CONTROL: (
            SpeakerMetricKind.PRIVACY_VIOLATIONS,
            SpeakerMetricUnit.COUNT,
            None,
            0,
        ),
        SpeakerCapability.AV_ASSOCIATION_GATE: (
            SpeakerMetricKind.ASSOCIATION_GATE,
            SpeakerMetricUnit.BASIS_POINTS,
            10_000,
            None,
        ),
        SpeakerCapability.USER_SELECTION_GATE: (
            SpeakerMetricKind.ASSOCIATION_GATE,
            SpeakerMetricUnit.BASIS_POINTS,
            10_000,
            None,
        ),
        SpeakerCapability.AMBIGUITY: (
            SpeakerMetricKind.FALSE_LINK_RATE,
            SpeakerMetricUnit.BASIS_POINTS,
            None,
            5_000,
        ),
        SpeakerCapability.DER: (SpeakerMetricKind.DER, SpeakerMetricUnit.BASIS_POINTS, None, 2_000),
        SpeakerCapability.FALSE_LINK: (
            SpeakerMetricKind.FALSE_LINK_RATE,
            SpeakerMetricUnit.BASIS_POINTS,
            None,
            5_000,
        ),
        SpeakerCapability.PRIVACY_NO_LOG: (
            SpeakerMetricKind.PRIVACY_VIOLATIONS,
            SpeakerMetricUnit.COUNT,
            None,
            0,
        ),
        SpeakerCapability.CORRUPTION: (
            SpeakerMetricKind.ABSTENTION,
            SpeakerMetricUnit.BASIS_POINTS,
            10_000,
            None,
        ),
        SpeakerCapability.ADVERSARIAL_METADATA: (
            SpeakerMetricKind.PRIVACY_VIOLATIONS,
            SpeakerMetricUnit.COUNT,
            None,
            0,
        ),
    }
    thresholds = tuple(
        SpeakerBenchmarkThreshold(
            f"speaker.{capability.value}",
            metric,
            capability,
            unit,
            minimum,
            maximum,
        )
        for capability, (metric, unit, minimum, maximum) in threshold_specs.items()
    )
    candidates = (
        SpeakerCandidateProfile(
            "native.comfyui.speaker",
            SpeakerCandidateFamily.COMFYUI_NATIVE,
            "comfyui_native_speaker",
            "1.0.0",
            "host-owned-speaker-capability",
            tuple(SpeakerCapability),
            False,
            True,
            False,
            SpeakerDisposition.UNAVAILABLE,
            "pinned ComfyUI audio speaker model and host lane were not admitted",
        ),
        SpeakerCandidateProfile(
            "fallback.ollama.speaker",
            SpeakerCandidateFamily.OLLAMA,
            "ollama_native_api",
            "1.0.0",
            "explicitly-selected-loopback-speaker-model",
            tuple(SpeakerCapability),
            True,
            False,
            False,
            SpeakerDisposition.UNAVAILABLE,
            "loopback Ollama server and explicitly selected speaker model were not started",
        ),
        SpeakerCandidateProfile(
            "specialist.speaker.placeholder",
            SpeakerCandidateFamily.SPECIALIST,
            "specialist_not_selected",
            "1.0.0",
            "explicit-selection-required",
            tuple(SpeakerCapability),
            False,
            True,
            True,
            SpeakerDisposition.UNSUPPORTED,
            "no specialist speaker profile is selected in the native-first lane",
        ),
    )
    return SpeakerBenchmarkPlan(
        "m12-03.speaker-perception-benchmark",
        "1.0.0",
        fixtures,
        thresholds,
        candidates,
        SpeakerBenchmarkLimits(
            5,
            MAX_SPEAKER_TURNS,
            MAX_SPEAKER_HYPOTHESES,
            MAX_SPEAKER_ASSOCIATIONS,
            60,
            180,
            16_384,
            32_768,
            MAX_SPEAKER_OUTPUT_BYTES,
            1,
        ),
        SpeakerRoutingPolicy(tuple(SpeakerCandidateFamily)),
    )


__all__ = [
    "SPEAKER_BENCHMARK_SCHEMA",
    "SPEAKER_PERCEPTION_SCHEMA",
    "MAX_SPEAKER_ASSOCIATIONS",
    "MAX_SPEAKER_BENCHMARK_CANDIDATES",
    "MAX_SPEAKER_BENCHMARK_CASES",
    "MAX_SPEAKER_BENCHMARK_THRESHOLDS",
    "MAX_SPEAKER_DIAGNOSTICS",
    "MAX_SPEAKER_HYPOTHESES",
    "MAX_SPEAKER_OUTPUT_BYTES",
    "MAX_SPEAKER_TURNS",
    "MAX_SPEAKER_UNCERTAINTIES",
    "SpeakerAssociation",
    "SpeakerAssociationEvidence",
    "SpeakerBenchmarkFixture",
    "SpeakerBenchmarkLimits",
    "SpeakerBenchmarkPlan",
    "SpeakerBenchmarkThreshold",
    "SpeakerCandidateFamily",
    "SpeakerCandidateProfile",
    "SpeakerCancellationProbe",
    "SpeakerCapability",
    "SpeakerCaseKind",
    "SpeakerDisposition",
    "SpeakerDocument",
    "SpeakerHypothesis",
    "SpeakerMetricKind",
    "SpeakerMetricUnit",
    "SpeakerReceipt",
    "SpeakerReference",
    "SpeakerRequest",
    "SpeakerRetention",
    "SpeakerRoute",
    "SpeakerRoutingPolicy",
    "SpeakerStatus",
    "SpeakerTurn",
    "SpeakerUncertainty",
    "SpeakerUncertaintyKind",
    "build_default_speaker_benchmark_plan",
    "build_speaker_abstention",
    "execute_speaker",
]
