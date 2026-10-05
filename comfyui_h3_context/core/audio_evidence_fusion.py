"""Provenance-preserving audio evidence fusion and confidence calibration for M12-06.

This pure-core module consumes bounded, already-produced ASR, speaker, audio-event, reference, and
audiovisual-grounding observations.  It does not decode media, discover a provider, run an LLM/VLM,
or perform network I/O.  Every disagreement remains visible and user-authored exact dialogue is
never silently replaced by observed ASR text.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from .canonical import canonical_fingerprint
from .contracts import MediaKind, TaskMode
from .errors import AudioEvidenceFusionError
from .evidence import Uncertainty, UncertaintyKind
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

AUDIO_EVIDENCE_FUSION_SCHEMA = "h3.audio.evidence_fusion.v1"
AUDIO_FUSION_BENCHMARK_SCHEMA = "h3.audio.evidence_fusion.benchmark.v1"
MAX_AUDIO_FUSION_CANDIDATES = 256
MAX_AUDIO_FUSION_GROUPS = 64
MAX_AUDIO_FUSION_RECEIPTS = 32
MAX_AUDIO_FUSION_UNCERTAINTIES = 16
MAX_AUDIO_FUSION_CASES = 32
MAX_AUDIO_FUSION_THRESHOLDS = 16
MAX_AUDIO_FUSION_BINS = 16
MAX_AUDIO_FUSION_PROFILES = 8
MAX_AUDIO_FUSION_DIAGNOSTICS = 32
MAX_AUDIO_FUSION_OUTPUT_BYTES = 65_536

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
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


class AudioFusionStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"


class AudioFusionRoute(str, Enum):
    STATIC_INJECTED = "static_injected"
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"
    SPECIALIST = "specialist"
    MIXED = "mixed"


class AudioFusionDisposition(str, Enum):
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


class AudioFusionAuthority(str, Enum):
    USER_AUTHORED = "user_authored"
    OBSERVED_ASR = "observed_asr"
    SPEAKER_HYPOTHESIS = "speaker_hypothesis"
    AUDIO_EVENT = "audio_event"
    AV_GROUNDING = "av_grounding"
    REFERENCE_SEMANTIC = "reference_semantic"
    INFERRED = "inferred"


class AudioFusionCategory(str, Enum):
    TRANSCRIPT = "transcript"
    SPEAKER_IDENTITY = "speaker_identity"
    AUDIO_EVENT = "audio_event"
    AV_ALIGNMENT = "av_alignment"
    REFERENCE_SEMANTIC = "reference_semantic"


class AudioFusionSupport(str, Enum):
    SUPPORTED = "supported"
    UNCERTAIN = "uncertain"
    UNSUPPORTED = "unsupported"


class AudioFusionResolution(str, Enum):
    CONSENSUS = "consensus"
    ALTERNATIVES = "alternatives"
    CONFLICT = "conflict"
    UNCERTAIN = "uncertain"
    ABSTAINED = "abstained"


class AudioFusionCaseKind(str, Enum):
    CLEAN_CONSENSUS = "clean_consensus"
    SOURCE_AUTHORITY_MISMATCH = "source_authority_mismatch"
    TRANSCRIPT_CONFLICT = "transcript_conflict"
    EVENT_AV_DISAGREEMENT = "event_av_disagreement"
    SPEAKER_AMBIGUITY = "speaker_ambiguity"
    UNSUPPORTED_UNKNOWN = "unsupported_unknown"
    HIGH_IMPACT_ABSTENTION = "high_impact_abstention"
    HELD_OUT_CALIBRATION = "held_out_calibration"
    TERMINAL = "terminal"


class AudioFusionCacheStatus(str, Enum):
    DISABLED = "disabled"
    NOT_USED = "not_used"
    MISS = "miss"
    HIT = "hit"


def _id(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise AudioEvidenceFusionError(f"{field} must be a bounded identifier")
    if (
        any(marker in value.casefold() for marker in _SENSITIVE_MARKERS)
        or "/" in value
        or "\\" in value
    ):
        raise AudioEvidenceFusionError(f"{field} contains locator or sensitive material")
    return value


def _code(value: object, field: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value.casefold()) is None:
        raise AudioEvidenceFusionError(f"{field} must be a lower-case bounded code")
    return value.casefold()


def _version(value: object, field: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise AudioEvidenceFusionError(f"{field} must be a numeric version")
    return value


def _fp(value: object, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise AudioEvidenceFusionError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _text(value: object, field: str, maximum: int = 2_048) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AudioEvidenceFusionError(f"{field} must be bounded non-empty text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise AudioEvidenceFusionError(f"{field} contains a control character")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise AudioEvidenceFusionError(f"{field} contains sensitive or locator material")
    return value


def _decimal(value: object, field: str, *, required: bool = True) -> Decimal | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or not Decimal("0") <= value <= Decimal("1")
    ):
        raise AudioEvidenceFusionError(f"{field} must be a finite Decimal between 0 and 1")
    return value


def _bounded_int(value: object, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise AudioEvidenceFusionError(f"{field} must be between 0 and {maximum}")
    return value


def _ids(values: object, field: str, maximum: int, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum or (required and not values):
        raise AudioEvidenceFusionError(f"{field} is outside the finite limit")
    result = tuple(_id(value, f"{field} item") for value in values)
    if len(result) != len(set(result)):
        raise AudioEvidenceFusionError(f"{field} must not contain duplicates")
    return result


def _codes(values: object, field: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise AudioEvidenceFusionError(f"{field} is outside the finite limit")
    result = tuple(_code(value, f"{field} item") for value in values)
    if len(result) != len(set(result)):
        raise AudioEvidenceFusionError(f"{field} must not contain duplicates")
    return result


def _uncertainties(values: object) -> tuple[Uncertainty, ...]:
    if (
        not isinstance(values, tuple)
        or len(values) > MAX_AUDIO_FUSION_UNCERTAINTIES
        or not all(isinstance(value, Uncertainty) for value in values)
    ):
        raise AudioEvidenceFusionError("uncertainties are outside the finite limit")
    return values


@dataclass(frozen=True, slots=True)
class AudioFusionSourceRef:
    """Redacted source/asset anchor for one audio or audiovisual observation."""

    asset_id: str
    source_id: str
    source_fingerprint: str
    anchor_kind: str | None = None
    anchor_id: str | None = None

    def __post_init__(self) -> None:
        _id(self.asset_id, "source asset_id")
        _id(self.source_id, "source source_id")
        _fp(self.source_fingerprint, "source fingerprint")
        if self.anchor_kind is not None:
            _code(self.anchor_kind, "source anchor_kind")
        if self.anchor_id is not None:
            _id(self.anchor_id, "source anchor_id")
        if (self.anchor_kind is None) != (self.anchor_id is None):
            raise AudioEvidenceFusionError("source anchor kind and ID must be paired")

    def to_wire(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
            "anchor_kind": self.anchor_kind,
            "anchor_id": self.anchor_id,
        }


@dataclass(frozen=True, slots=True)
class AudioFusionCandidateReceipt:
    route: AudioFusionRoute
    disposition: AudioFusionDisposition
    adapter_id: str
    adapter_version: str
    model_id: str
    model_digest: str
    output_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.route, AudioFusionRoute) or self.route is AudioFusionRoute.MIXED:
            raise AudioEvidenceFusionError("candidate receipt route must be concrete")
        if not isinstance(self.disposition, AudioFusionDisposition):
            raise AudioEvidenceFusionError("candidate receipt disposition is unsupported")
        _code(self.adapter_id, "candidate receipt adapter_id")
        _version(self.adapter_version, "candidate receipt adapter_version")
        _id(self.model_id, "candidate receipt model_id")
        _fp(self.model_digest, "candidate receipt model_digest")
        _fp(self.output_fingerprint, "candidate receipt output_fingerprint")

    def to_wire(self) -> dict[str, object]:
        return {
            "route": self.route.value,
            "disposition": self.disposition.value,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "output_fingerprint": self.output_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class AudioFusionCandidate:
    candidate_id: str
    group_id: str
    category: AudioFusionCategory
    authority: AudioFusionAuthority
    claim: str | None
    support: AudioFusionSupport
    confidence: Decimal | None
    source: AudioFusionSourceRef
    receipt: AudioFusionCandidateReceipt
    uncertainties: tuple[Uncertainty, ...] = ()
    exact: bool = False
    high_impact: bool = False

    def __post_init__(self) -> None:
        _id(self.candidate_id, "candidate_id")
        _id(self.group_id, "candidate group_id")
        if not isinstance(self.category, AudioFusionCategory):
            raise AudioEvidenceFusionError("candidate category is unsupported")
        if not isinstance(self.authority, AudioFusionAuthority):
            raise AudioEvidenceFusionError("candidate authority is unsupported")
        if not isinstance(self.support, AudioFusionSupport):
            raise AudioEvidenceFusionError("candidate support is unsupported")
        if not isinstance(self.source, AudioFusionSourceRef):
            raise AudioEvidenceFusionError("candidate source is invalid")
        if not isinstance(self.receipt, AudioFusionCandidateReceipt):
            raise AudioEvidenceFusionError("candidate receipt is invalid")
        _uncertainties(self.uncertainties)
        if not isinstance(self.exact, bool) or not isinstance(self.high_impact, bool):
            raise AudioEvidenceFusionError("candidate exact/high_impact flags must be bool")
        if self.support is AudioFusionSupport.UNSUPPORTED:
            if self.claim is not None or self.confidence is not None:
                raise AudioEvidenceFusionError(
                    "unsupported candidate cannot carry claim/confidence"
                )
            if not self.uncertainties:
                raise AudioEvidenceFusionError("unsupported candidate requires uncertainty")
        else:
            if self.claim is None:
                raise AudioEvidenceFusionError("supported candidate requires a claim")
            _text(self.claim, "candidate claim")
            _decimal(self.confidence, "candidate confidence")
            if self.support is AudioFusionSupport.UNCERTAIN and not self.uncertainties:
                raise AudioEvidenceFusionError("uncertain candidate requires uncertainty")
        if self.exact and self.authority is not AudioFusionAuthority.USER_AUTHORED:
            raise AudioEvidenceFusionError("only user-authored candidates may be exact")
        if self.exact and self.category is not AudioFusionCategory.TRANSCRIPT:
            raise AudioEvidenceFusionError("exact authority is reserved for transcript claims")

    def to_wire(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "group_id": self.group_id,
            "category": self.category.value,
            "authority": self.authority.value,
            "claim": self.claim,
            "support": self.support.value,
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "source": self.source.to_wire(),
            "receipt": self.receipt.to_wire(),
            "uncertainties": [item.to_wire() for item in self.uncertainties],
            "exact": self.exact,
            "high_impact": self.high_impact,
        }


@dataclass(frozen=True, slots=True)
class AudioFusionGroup:
    group_id: str
    category: AudioFusionCategory
    source: AudioFusionSourceRef
    candidate_ids: tuple[str, ...]
    resolution: AudioFusionResolution

    def __post_init__(self) -> None:
        _id(self.group_id, "group_id")
        if not isinstance(self.category, AudioFusionCategory):
            raise AudioEvidenceFusionError("group category is unsupported")
        if not isinstance(self.source, AudioFusionSourceRef):
            raise AudioEvidenceFusionError("group source is invalid")
        _ids(self.candidate_ids, "group candidate_ids", MAX_AUDIO_FUSION_CANDIDATES, required=True)
        if not isinstance(self.resolution, AudioFusionResolution):
            raise AudioEvidenceFusionError("group resolution is unsupported")

    def to_wire(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "category": self.category.value,
            "source": self.source.to_wire(),
            "candidate_ids": list(self.candidate_ids),
            "resolution": self.resolution.value,
        }


@dataclass(frozen=True, slots=True)
class AudioFusionDecision:
    decision_id: str
    group_id: str
    resolution: AudioFusionResolution
    selected_candidate_ids: tuple[str, ...]
    alternative_candidate_ids: tuple[str, ...]
    raw_confidence: Decimal | None
    calibrated_confidence: Decimal | None
    abstention_reason: str | None = None

    def __post_init__(self) -> None:
        _id(self.decision_id, "decision_id")
        _id(self.group_id, "decision group_id")
        if not isinstance(self.resolution, AudioFusionResolution):
            raise AudioEvidenceFusionError("decision resolution is unsupported")
        _ids(self.selected_candidate_ids, "selected candidate IDs", MAX_AUDIO_FUSION_CANDIDATES)
        _ids(
            self.alternative_candidate_ids, "alternative candidate IDs", MAX_AUDIO_FUSION_CANDIDATES
        )
        if set(self.selected_candidate_ids) & set(self.alternative_candidate_ids):
            raise AudioEvidenceFusionError("selected and alternative IDs must be disjoint")
        _decimal(self.raw_confidence, "decision raw confidence", required=False)
        _decimal(self.calibrated_confidence, "decision calibrated confidence", required=False)
        if self.resolution is AudioFusionResolution.ABSTAINED:
            if self.calibrated_confidence is not None or not self.abstention_reason:
                raise AudioEvidenceFusionError(
                    "abstained decision requires reason/no calibrated confidence"
                )
        elif self.abstention_reason is not None:
            _text(self.abstention_reason, "abstention reason", 512)

    def to_wire(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "group_id": self.group_id,
            "resolution": self.resolution.value,
            "selected_candidate_ids": list(self.selected_candidate_ids),
            "alternative_candidate_ids": list(self.alternative_candidate_ids),
            "raw_confidence": None
            if self.raw_confidence is None
            else format(self.raw_confidence, "f"),
            "calibrated_confidence": (
                None
                if self.calibrated_confidence is None
                else format(self.calibrated_confidence, "f")
            ),
            "abstention_reason": self.abstention_reason,
        }


@dataclass(frozen=True, slots=True)
class AudioCalibrationBin:
    lower: Decimal
    upper: Decimal
    calibrated: Decimal

    def __post_init__(self) -> None:
        _decimal(self.lower, "calibration lower")
        _decimal(self.upper, "calibration upper")
        _decimal(self.calibrated, "calibration calibrated")
        if self.lower >= self.upper:
            raise AudioEvidenceFusionError("calibration bin lower must be less than upper")

    def to_wire(self) -> dict[str, str]:
        return {
            "lower": format(self.lower, "f"),
            "upper": format(self.upper, "f"),
            "calibrated": format(self.calibrated, "f"),
        }


@dataclass(frozen=True, slots=True)
class AudioCalibrationProfile:
    profile_id: str
    version: str
    fit_case_ids: tuple[str, ...]
    held_out_case_ids: tuple[str, ...] = ()
    bins: tuple[AudioCalibrationBin, ...] = ()
    schema: str = AUDIO_EVIDENCE_FUSION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.profile_id, "calibration profile_id")
        _version(self.version, "calibration version")
        fit = _ids(
            self.fit_case_ids, "calibration fit_case_ids", MAX_AUDIO_FUSION_CASES, required=True
        )
        held = _ids(self.held_out_case_ids, "calibration held_out_case_ids", MAX_AUDIO_FUSION_CASES)
        if set(fit) & set(held):
            raise AudioEvidenceFusionError("calibration fit and held-out cases must be disjoint")
        if (
            not isinstance(self.bins, tuple)
            or not self.bins
            or len(self.bins) > MAX_AUDIO_FUSION_BINS
        ):
            raise AudioEvidenceFusionError("calibration bins are outside the finite limit")
        if not all(isinstance(item, AudioCalibrationBin) for item in self.bins):
            raise AudioEvidenceFusionError("calibration bins are invalid")
        previous: AudioCalibrationBin | None = None
        for item in self.bins:
            if previous is not None and (
                item.lower != previous.upper or item.calibrated < previous.calibrated
            ):
                raise AudioEvidenceFusionError("calibration bins must be contiguous and monotonic")
            previous = item
        if self.bins[0].lower != Decimal("0") or self.bins[-1].upper != Decimal("1"):
            raise AudioEvidenceFusionError("calibration bins must cover [0,1]")
        if self.schema != AUDIO_EVIDENCE_FUSION_SCHEMA:
            raise AudioEvidenceFusionError("unsupported calibration schema")

    def calibrate(self, confidence: Decimal) -> Decimal:
        _decimal(confidence, "calibration confidence")
        for index, item in enumerate(self.bins):
            if item.lower <= confidence < item.upper or (
                index == len(self.bins) - 1 and confidence <= item.upper
            ):
                return item.calibrated
        raise AudioEvidenceFusionError("confidence did not fit calibration bins")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "profile_id": self.profile_id,
            "version": self.version,
            "fit_case_ids": list(self.fit_case_ids),
            "held_out_case_ids": list(self.held_out_case_ids),
            "bins": [item.to_wire() for item in self.bins],
        }


def calibrate_audio_confidence(profile: AudioCalibrationProfile, confidence: Decimal) -> Decimal:
    if not isinstance(profile, AudioCalibrationProfile):
        raise AudioEvidenceFusionError("profile must be AudioCalibrationProfile")
    return profile.calibrate(confidence)


@dataclass(frozen=True, slots=True)
class AudioFusionThreshold:
    metric: str
    minimum: Decimal | None = None
    maximum: Decimal | None = None

    def __post_init__(self) -> None:
        _code(self.metric, "fusion metric")
        if self.minimum is None and self.maximum is None:
            raise AudioEvidenceFusionError("fusion threshold requires minimum or maximum")
        _decimal(self.minimum, "fusion minimum", required=False)
        _decimal(self.maximum, "fusion maximum", required=False)
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise AudioEvidenceFusionError("fusion threshold range is inverted")

    def to_wire(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "minimum": None if self.minimum is None else format(self.minimum, "f"),
            "maximum": None if self.maximum is None else format(self.maximum, "f"),
        }


@dataclass(frozen=True, slots=True)
class AudioFusionBenchmarkCase:
    case_id: str
    split: str
    kind: AudioFusionCaseKind
    high_impact: bool

    def __post_init__(self) -> None:
        _id(self.case_id, "fusion case_id")
        if self.split not in {"development", "held_out"}:
            raise AudioEvidenceFusionError("fusion case split is unsupported")
        if not isinstance(self.kind, AudioFusionCaseKind) or not isinstance(self.high_impact, bool):
            raise AudioEvidenceFusionError("fusion case kind/high_impact is invalid")

    def to_wire(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "split": self.split,
            "kind": self.kind.value,
            "high_impact": self.high_impact,
        }


@dataclass(frozen=True, slots=True)
class AudioFusionCandidateProfile:
    route: AudioFusionRoute
    disposition: AudioFusionDisposition
    capabilities: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.route, AudioFusionRoute) or self.route is AudioFusionRoute.MIXED:
            raise AudioEvidenceFusionError("candidate profile route must be concrete")
        if not isinstance(self.disposition, AudioFusionDisposition):
            raise AudioEvidenceFusionError("candidate profile disposition is invalid")
        _codes(self.capabilities, "candidate profile capabilities", 32)
        _text(self.reason, "candidate profile reason", 512)

    def to_wire(self) -> dict[str, object]:
        return {
            "route": self.route.value,
            "disposition": self.disposition.value,
            "capabilities": list(self.capabilities),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class AudioFusionRoutingPolicy:
    preferred_route: AudioFusionRoute = AudioFusionRoute.COMFYUI_NATIVE
    first_fallback: AudioFusionRoute = AudioFusionRoute.OLLAMA
    specialist_route: AudioFusionRoute = AudioFusionRoute.SPECIALIST
    automatic_fallback: bool = False

    def __post_init__(self) -> None:
        if self.preferred_route is not AudioFusionRoute.COMFYUI_NATIVE:
            raise AudioEvidenceFusionError("preferred route must be native ComfyUI")
        if self.first_fallback is not AudioFusionRoute.OLLAMA:
            raise AudioEvidenceFusionError("first fallback must be Ollama")
        if self.specialist_route is not AudioFusionRoute.SPECIALIST:
            raise AudioEvidenceFusionError("specialist route is unsupported")
        if not isinstance(self.automatic_fallback, bool) or self.automatic_fallback:
            raise AudioEvidenceFusionError("automatic provider fallback is forbidden")

    def to_wire(self) -> dict[str, object]:
        return {
            "preferred_route": self.preferred_route.value,
            "first_fallback": self.first_fallback.value,
            "specialist_route": self.specialist_route.value,
            "automatic_fallback": self.automatic_fallback,
        }


@dataclass(frozen=True, slots=True)
class AudioFusionBenchmarkPlan:
    cases: tuple[AudioFusionBenchmarkCase, ...]
    thresholds: tuple[AudioFusionThreshold, ...]
    candidate_profiles: tuple[AudioFusionCandidateProfile, ...]
    routing_policy: AudioFusionRoutingPolicy = AudioFusionRoutingPolicy()
    schema: str = AUDIO_FUSION_BENCHMARK_SCHEMA
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.cases, tuple)
            or not self.cases
            or len(self.cases) > MAX_AUDIO_FUSION_CASES
        ):
            raise AudioEvidenceFusionError("fusion cases are outside the finite limit")
        if not all(isinstance(item, AudioFusionBenchmarkCase) for item in self.cases):
            raise AudioEvidenceFusionError("fusion cases are invalid")
        if len({item.case_id for item in self.cases}) != len(self.cases):
            raise AudioEvidenceFusionError("fusion case IDs must be unique")
        if (
            not isinstance(self.thresholds, tuple)
            or not self.thresholds
            or len(self.thresholds) > MAX_AUDIO_FUSION_THRESHOLDS
        ):
            raise AudioEvidenceFusionError("fusion thresholds are outside the finite limit")
        if not all(isinstance(item, AudioFusionThreshold) for item in self.thresholds):
            raise AudioEvidenceFusionError("fusion thresholds are invalid")
        if len({item.metric for item in self.thresholds}) != len(self.thresholds):
            raise AudioEvidenceFusionError("fusion metrics must be unique")
        if (
            not isinstance(self.candidate_profiles, tuple)
            or not self.candidate_profiles
            or len(self.candidate_profiles) > MAX_AUDIO_FUSION_PROFILES
        ):
            raise AudioEvidenceFusionError("candidate profiles are outside the finite limit")
        if not all(
            isinstance(item, AudioFusionCandidateProfile) for item in self.candidate_profiles
        ):
            raise AudioEvidenceFusionError("candidate profiles are invalid")
        if len({item.route for item in self.candidate_profiles}) != len(self.candidate_profiles):
            raise AudioEvidenceFusionError("candidate profile routes must be unique")
        if not isinstance(self.routing_policy, AudioFusionRoutingPolicy):
            raise AudioEvidenceFusionError("routing policy is invalid")
        if self.schema != AUDIO_FUSION_BENCHMARK_SCHEMA:
            raise AudioEvidenceFusionError("unsupported benchmark schema")
        expected = canonical_fingerprint(
            {
                "schema": self.schema,
                "cases": [item.to_wire() for item in self.cases],
                "thresholds": [item.to_wire() for item in self.thresholds],
                "candidate_profiles": [item.to_wire() for item in self.candidate_profiles],
                "routing_policy": self.routing_policy.to_wire(),
            }
        )
        if self.fingerprint and self.fingerprint != expected:
            raise AudioEvidenceFusionError("benchmark fingerprint does not match contents")
        object.__setattr__(self, "fingerprint", expected)

    @property
    def development_case_ids(self) -> tuple[str, ...]:
        return tuple(item.case_id for item in self.cases if item.split == "development")

    @property
    def held_out_case_ids(self) -> tuple[str, ...]:
        return tuple(item.case_id for item in self.cases if item.split == "held_out")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "fingerprint": self.fingerprint,
            "cases": [item.to_wire() for item in self.cases],
            "thresholds": [item.to_wire() for item in self.thresholds],
            "candidate_profiles": [item.to_wire() for item in self.candidate_profiles],
            "routing_policy": self.routing_policy.to_wire(),
        }


def build_default_audio_fusion_benchmark_plan() -> AudioFusionBenchmarkPlan:
    cases = (
        AudioFusionBenchmarkCase(
            "audio.clean_consensus", "development", AudioFusionCaseKind.CLEAN_CONSENSUS, False
        ),
        AudioFusionBenchmarkCase(
            "audio.source_authority_mismatch",
            "development",
            AudioFusionCaseKind.SOURCE_AUTHORITY_MISMATCH,
            True,
        ),
        AudioFusionBenchmarkCase(
            "audio.transcript_conflict",
            "development",
            AudioFusionCaseKind.TRANSCRIPT_CONFLICT,
            True,
        ),
        AudioFusionBenchmarkCase(
            "audio.event_av_disagreement",
            "development",
            AudioFusionCaseKind.EVENT_AV_DISAGREEMENT,
            True,
        ),
        AudioFusionBenchmarkCase(
            "audio.speaker_ambiguity", "held_out", AudioFusionCaseKind.SPEAKER_AMBIGUITY, True
        ),
        AudioFusionBenchmarkCase(
            "audio.unsupported_unknown", "held_out", AudioFusionCaseKind.UNSUPPORTED_UNKNOWN, True
        ),
        AudioFusionBenchmarkCase(
            "audio.high_impact_abstention",
            "held_out",
            AudioFusionCaseKind.HIGH_IMPACT_ABSTENTION,
            True,
        ),
        AudioFusionBenchmarkCase(
            "audio.held_out_calibration",
            "held_out",
            AudioFusionCaseKind.HELD_OUT_CALIBRATION,
            False,
        ),
    )
    thresholds = (
        AudioFusionThreshold("expected_calibration_error", maximum=Decimal("0.25")),
        AudioFusionThreshold("brier_score", maximum=Decimal("0.25")),
        AudioFusionThreshold("disagreement_preservation", minimum=Decimal("1.0")),
        AudioFusionThreshold("exact_authority_violations", maximum=Decimal("0")),
        AudioFusionThreshold("source_ownership_violations", maximum=Decimal("0")),
        AudioFusionThreshold("high_impact_error_improvement", minimum=Decimal("0.20")),
    )
    profiles = (
        AudioFusionCandidateProfile(
            AudioFusionRoute.COMFYUI_NATIVE,
            AudioFusionDisposition.UNAVAILABLE,
            ("asr", "speaker", "event", "av_alignment"),
            "supported-host/model profile is not executed in M12-06",
        ),
        AudioFusionCandidateProfile(
            AudioFusionRoute.OLLAMA,
            AudioFusionDisposition.UNAVAILABLE,
            ("transcript", "event"),
            "explicit loopback fallback is not contacted in M12-06",
        ),
        AudioFusionCandidateProfile(
            AudioFusionRoute.SPECIALIST,
            AudioFusionDisposition.UNSUPPORTED,
            ("audio_fusion",),
            "no specialist audio-fusion profile is qualified in M12-06",
        ),
    )
    return AudioFusionBenchmarkPlan(
        cases=cases,
        thresholds=thresholds,
        candidate_profiles=profiles,
        routing_policy=AudioFusionRoutingPolicy(),
    )


@dataclass(frozen=True, slots=True)
class AudioFusionRequest:
    candidates: tuple[AudioFusionCandidate, ...]
    calibration_profile: AudioCalibrationProfile
    benchmark: AudioFusionBenchmarkPlan | None = None
    document_id: str = "audio_fusion_document"
    max_groups: int = MAX_AUDIO_FUSION_GROUPS

    def __post_init__(self) -> None:
        _id(self.document_id, "fusion document_id")
        if (
            not isinstance(self.candidates, tuple)
            or not self.candidates
            or len(self.candidates) > MAX_AUDIO_FUSION_CANDIDATES
        ):
            raise AudioEvidenceFusionError("fusion candidates are outside the finite limit")
        if not all(isinstance(item, AudioFusionCandidate) for item in self.candidates):
            raise AudioEvidenceFusionError("fusion candidates are invalid")
        if len({item.candidate_id for item in self.candidates}) != len(self.candidates):
            raise AudioEvidenceFusionError("fusion candidate IDs must be unique")
        if not isinstance(self.calibration_profile, AudioCalibrationProfile):
            raise AudioEvidenceFusionError("fusion calibration profile is invalid")
        if self.benchmark is not None and not isinstance(self.benchmark, AudioFusionBenchmarkPlan):
            raise AudioEvidenceFusionError("fusion benchmark is invalid")
        if (
            isinstance(self.max_groups, bool)
            or not isinstance(self.max_groups, int)
            or not 1 <= self.max_groups <= MAX_AUDIO_FUSION_GROUPS
        ):
            raise AudioEvidenceFusionError("fusion max_groups is outside the finite limit")
        if self.benchmark is not None:
            if set(self.calibration_profile.fit_case_ids) != set(
                self.benchmark.development_case_ids
            ) or set(self.calibration_profile.held_out_case_ids) != set(
                self.benchmark.held_out_case_ids
            ):
                raise AudioEvidenceFusionError("calibration splits must match benchmark splits")


@dataclass(frozen=True, slots=True)
class AudioFusionResourceReceipt:
    cache_status: AudioFusionCacheStatus
    cache_key_fingerprint: str | None
    memory_bytes: int
    wall_time_ms: int
    output_bytes: int
    output_items: int
    cancellation_checkpoints: int
    network_used: bool = False
    decoder_started: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.cache_status, AudioFusionCacheStatus):
            raise AudioEvidenceFusionError("cache status is unsupported")
        if self.cache_key_fingerprint is not None:
            _fp(self.cache_key_fingerprint, "cache key fingerprint")
        if (
            self.cache_status in {AudioFusionCacheStatus.HIT, AudioFusionCacheStatus.MISS}
            and self.cache_key_fingerprint is None
        ):
            raise AudioEvidenceFusionError(
                "cache hit/miss requires a redacted cache key fingerprint"
            )
        for value, field, maximum in (
            (self.memory_bytes, "memory_bytes", 2**63 - 1),
            (self.wall_time_ms, "wall_time_ms", 2**63 - 1),
            (self.output_bytes, "output_bytes", MAX_AUDIO_FUSION_OUTPUT_BYTES),
            (
                self.output_items,
                "output_items",
                MAX_AUDIO_FUSION_CANDIDATES + MAX_AUDIO_FUSION_GROUPS,
            ),
            (self.cancellation_checkpoints, "cancellation_checkpoints", 1_000_000),
        ):
            _bounded_int(value, field, maximum)
        if not isinstance(self.network_used, bool) or not isinstance(self.decoder_started, bool):
            raise AudioEvidenceFusionError("resource receipt flags must be bool")

    def to_wire(self) -> dict[str, object]:
        return {
            "cache_status": self.cache_status.value,
            "cache_key_fingerprint": self.cache_key_fingerprint,
            "memory_bytes": self.memory_bytes,
            "wall_time_ms": self.wall_time_ms,
            "output_bytes": self.output_bytes,
            "output_items": self.output_items,
            "cancellation_checkpoints": self.cancellation_checkpoints,
            "network_used": self.network_used,
            "decoder_started": self.decoder_started,
        }


@dataclass(frozen=True, slots=True)
class AudioFusionReceipt:
    route: AudioFusionRoute
    adapter_id: str
    adapter_version: str
    output_fingerprint: str
    source_fingerprints: tuple[str, ...]
    resource: AudioFusionResourceReceipt
    schema: str = AUDIO_EVIDENCE_FUSION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.route, AudioFusionRoute):
            raise AudioEvidenceFusionError("fusion receipt route is unsupported")
        _code(self.adapter_id, "fusion receipt adapter_id")
        _version(self.adapter_version, "fusion receipt adapter_version")
        _fp(self.output_fingerprint, "fusion receipt output_fingerprint")
        fps = self.source_fingerprints
        if not isinstance(fps, tuple) or not fps or len(fps) > MAX_AUDIO_FUSION_RECEIPTS:
            raise AudioEvidenceFusionError(
                "fusion source fingerprints are outside the finite limit"
            )
        for value in fps:
            _fp(value, "fusion source fingerprint")
        if len(set(fps)) != len(fps):
            raise AudioEvidenceFusionError("fusion source fingerprints must be unique")
        if not isinstance(self.resource, AudioFusionResourceReceipt):
            raise AudioEvidenceFusionError("fusion resource receipt is invalid")
        if self.schema != AUDIO_EVIDENCE_FUSION_SCHEMA:
            raise AudioEvidenceFusionError("unsupported fusion receipt schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "route": self.route.value,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "output_fingerprint": self.output_fingerprint,
            "source_fingerprints": list(self.source_fingerprints),
            "resource": self.resource.to_wire(),
        }

    def to_public_dict(self) -> dict[str, object]:
        result = self.to_wire()
        result["source_count"] = len(self.source_fingerprints)
        return result


@dataclass(frozen=True, slots=True)
class AudioFusionEvaluationSample:
    case_id: str
    confidence: Decimal
    correct: bool
    abstained: bool
    high_impact: bool

    def __post_init__(self) -> None:
        _id(self.case_id, "evaluation case_id")
        _decimal(self.confidence, "evaluation confidence")
        if (
            not isinstance(self.correct, bool)
            or not isinstance(self.abstained, bool)
            or not isinstance(self.high_impact, bool)
        ):
            raise AudioEvidenceFusionError("evaluation flags must be bool")


@dataclass(frozen=True, slots=True)
class AudioFusionEvaluation:
    split: str
    case_ids: tuple[str, ...]
    expected_calibration_error: Decimal
    brier_score: Decimal
    high_impact_error_rate_without_abstention: Decimal
    high_impact_error_rate_with_abstention: Decimal
    high_impact_error_improvement: Decimal
    abstention_improves_high_impact_error: bool

    def __post_init__(self) -> None:
        if self.split != "held_out":
            raise AudioEvidenceFusionError("fusion evaluation must use held_out split")
        _ids(self.case_ids, "evaluation case_ids", MAX_AUDIO_FUSION_CASES, required=True)
        for value, field in (
            (self.expected_calibration_error, "evaluation ECE"),
            (self.brier_score, "evaluation Brier score"),
            (self.high_impact_error_rate_without_abstention, "evaluation raw high-impact error"),
            (self.high_impact_error_rate_with_abstention, "evaluation abstained high-impact error"),
            (self.high_impact_error_improvement, "evaluation high-impact improvement"),
        ):
            _decimal(value, field)
        expected = (
            self.high_impact_error_rate_without_abstention
            - self.high_impact_error_rate_with_abstention
        )
        if expected != self.high_impact_error_improvement:
            raise AudioEvidenceFusionError("evaluation improvement does not match error rates")
        if self.abstention_improves_high_impact_error != (self.high_impact_error_improvement > 0):
            raise AudioEvidenceFusionError("evaluation improvement flag is inconsistent")

    def to_wire(self) -> dict[str, object]:
        return {
            "split": self.split,
            "case_ids": list(self.case_ids),
            "expected_calibration_error": format(self.expected_calibration_error, "f"),
            "brier_score": format(self.brier_score, "f"),
            "high_impact_error_rate_without_abstention": format(
                self.high_impact_error_rate_without_abstention, "f"
            ),
            "high_impact_error_rate_with_abstention": format(
                self.high_impact_error_rate_with_abstention, "f"
            ),
            "high_impact_error_improvement": format(self.high_impact_error_improvement, "f"),
            "abstention_improves_high_impact_error": self.abstention_improves_high_impact_error,
        }


def evaluate_audio_fusion_benchmark(
    plan: AudioFusionBenchmarkPlan,
    profile: AudioCalibrationProfile,
    samples: tuple[AudioFusionEvaluationSample, ...],
) -> AudioFusionEvaluation:
    if not isinstance(plan, AudioFusionBenchmarkPlan) or not isinstance(
        profile, AudioCalibrationProfile
    ):
        raise AudioEvidenceFusionError("evaluation requires benchmark plan and calibration profile")
    if not isinstance(samples, tuple) or not samples:
        raise AudioEvidenceFusionError("evaluation samples must be non-empty")
    held_out = set(plan.held_out_case_ids)
    if set(profile.held_out_case_ids) != held_out or any(
        item.case_id not in held_out for item in samples
    ):
        raise AudioEvidenceFusionError("evaluation sample is not in held-out split")
    if len({item.case_id for item in samples}) != len(samples):
        raise AudioEvidenceFusionError("evaluation sample IDs must be unique")
    ece = sum(
        (
            abs(item.confidence - (Decimal("1") if item.correct else Decimal("0")))
            for item in samples
        ),
        Decimal("0"),
    ) / Decimal(len(samples))
    brier = sum(
        (
            (item.confidence - (Decimal("1") if item.correct else Decimal("0"))) ** 2
            for item in samples
        ),
        Decimal("0"),
    ) / Decimal(len(samples))
    high_impact = tuple(item for item in samples if item.high_impact)
    if not high_impact:
        raise AudioEvidenceFusionError("evaluation requires high-impact samples")
    raw_rate = Decimal(sum(not item.correct for item in high_impact)) / Decimal(len(high_impact))
    abstained_rate = Decimal(
        sum(not item.correct and not item.abstained for item in high_impact)
    ) / Decimal(len(high_impact))
    return AudioFusionEvaluation(
        "held_out",
        tuple(item.case_id for item in samples),
        ece,
        brier,
        raw_rate,
        abstained_rate,
        raw_rate - abstained_rate,
        raw_rate > abstained_rate,
    )


@dataclass(frozen=True, slots=True)
class AudioFusionDocument:
    document_id: str
    status: AudioFusionStatus
    candidates: tuple[AudioFusionCandidate, ...]
    groups: tuple[AudioFusionGroup, ...]
    decisions: tuple[AudioFusionDecision, ...]
    calibration_profile: AudioCalibrationProfile
    receipt: AudioFusionReceipt | None
    benchmark: AudioFusionBenchmarkPlan | None = None
    evaluation: AudioFusionEvaluation | None = None
    diagnostics: tuple[str, ...] = ()
    schema: str = AUDIO_EVIDENCE_FUSION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.document_id, "fusion document_id")
        if not isinstance(self.status, AudioFusionStatus) or not isinstance(
            self.calibration_profile, AudioCalibrationProfile
        ):
            raise AudioEvidenceFusionError("fusion document status/profile is invalid")
        if (
            len(self.candidates) > MAX_AUDIO_FUSION_CANDIDATES
            or len(self.groups) > MAX_AUDIO_FUSION_GROUPS
        ):
            raise AudioEvidenceFusionError("fusion document exceeds finite limits")
        if not all(isinstance(item, AudioFusionCandidate) for item in self.candidates) or len(
            {item.candidate_id for item in self.candidates}
        ) != len(self.candidates):
            raise AudioEvidenceFusionError("fusion document candidates are invalid or duplicated")
        if not all(isinstance(item, AudioFusionGroup) for item in self.groups) or len(
            {item.group_id for item in self.groups}
        ) != len(self.groups):
            raise AudioEvidenceFusionError("fusion document groups are invalid or duplicated")
        if not all(isinstance(item, AudioFusionDecision) for item in self.decisions) or len(
            {item.decision_id for item in self.decisions}
        ) != len(self.decisions):
            raise AudioEvidenceFusionError("fusion document decisions are invalid or duplicated")
        candidate_map = {item.candidate_id: item for item in self.candidates}
        group_map = {item.group_id: item for item in self.groups}
        grouped_ids: list[str] = []
        for group in self.groups:
            grouped_ids.extend(group.candidate_ids)
            for candidate_id in group.candidate_ids:
                candidate = candidate_map.get(candidate_id)
                if (
                    candidate is None
                    or candidate.group_id != group.group_id
                    or candidate.category is not group.category
                    or candidate.source != group.source
                ):
                    raise AudioEvidenceFusionError("fusion group candidate ownership differs")
        if set(grouped_ids) != set(candidate_map) or len(grouped_ids) != len(set(grouped_ids)):
            raise AudioEvidenceFusionError("every candidate must belong to exactly one group")
        for decision in self.decisions:
            decision_group = group_map.get(decision.group_id)
            if decision_group is None:
                raise AudioEvidenceFusionError("fusion decision references unknown group")
            allowed = set(decision_group.candidate_ids)
            if (
                not set(decision.selected_candidate_ids) <= allowed
                or not set(decision.alternative_candidate_ids) <= allowed
            ):
                raise AudioEvidenceFusionError("fusion decision references unknown candidate")
            if decision.resolution is not decision_group.resolution:
                raise AudioEvidenceFusionError("fusion decision resolution differs from group")
        if self.receipt is not None and not isinstance(self.receipt, AudioFusionReceipt):
            raise AudioEvidenceFusionError("fusion document receipt is invalid")
        if self.benchmark is not None and not isinstance(self.benchmark, AudioFusionBenchmarkPlan):
            raise AudioEvidenceFusionError("fusion document benchmark is invalid")
        if self.evaluation is not None and not isinstance(self.evaluation, AudioFusionEvaluation):
            raise AudioEvidenceFusionError("fusion document evaluation is invalid")
        if (
            not isinstance(self.diagnostics, tuple)
            or len(self.diagnostics) > MAX_AUDIO_FUSION_DIAGNOSTICS
            or not all(isinstance(item, str) for item in self.diagnostics)
        ):
            raise AudioEvidenceFusionError("fusion diagnostics are invalid")
        if self.schema != AUDIO_EVIDENCE_FUSION_SCHEMA:
            raise AudioEvidenceFusionError("unsupported fusion document schema")
        if self.receipt is not None and set(self.receipt.source_fingerprints) != {
            item.source.source_fingerprint for item in self.candidates
        }:
            raise AudioEvidenceFusionError("fusion receipt does not cover all source fingerprints")
        if self.benchmark is not None and (
            set(self.calibration_profile.fit_case_ids) != set(self.benchmark.development_case_ids)
            or set(self.calibration_profile.held_out_case_ids)
            != set(self.benchmark.held_out_case_ids)
        ):
            raise AudioEvidenceFusionError("document calibration splits differ from benchmark")
        if self.status is AudioFusionStatus.COMPLETE:
            if self.receipt is None or not self.candidates or not self.groups or not self.decisions:
                raise AudioEvidenceFusionError(
                    "complete fusion document requires evidence and receipt"
                )
            if not any(
                item.resolution is not AudioFusionResolution.ABSTAINED for item in self.decisions
            ):
                raise AudioEvidenceFusionError(
                    "complete fusion document requires a non-abstained group"
                )
        elif (
            self.status
            in {
                AudioFusionStatus.CANCELLED,
                AudioFusionStatus.CORRUPT,
                AudioFusionStatus.UNSUPPORTED,
            }
            and self.receipt is not None
        ):
            raise AudioEvidenceFusionError("terminal fusion document cannot carry a receipt")
        if len(repr(self.to_wire()).encode("utf-8")) > MAX_AUDIO_FUSION_OUTPUT_BYTES:
            raise AudioEvidenceFusionError("fusion document exceeds portable output limit")

    @property
    def complete(self) -> bool:
        return self.status is AudioFusionStatus.COMPLETE

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "document_id": self.document_id,
            "status": self.status.value,
            "candidates": [item.to_wire() for item in self.candidates],
            "groups": [item.to_wire() for item in self.groups],
            "decisions": [item.to_wire() for item in self.decisions],
            "calibration_profile": self.calibration_profile.to_wire(),
            "receipt": None if self.receipt is None else self.receipt.to_wire(),
            "benchmark": None if self.benchmark is None else self.benchmark.to_wire(),
            "evaluation": None if self.evaluation is None else self.evaluation.to_wire(),
            "diagnostics": list(self.diagnostics),
        }

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "document_id": self.document_id,
            "status": self.status.value,
            "candidate_count": len(self.candidates),
            "group_count": len(self.groups),
            "decision_count": len(self.decisions),
            "groups": [
                {
                    "group_id": group.group_id,
                    "category": group.category.value,
                    "resolution": group.resolution.value,
                    "candidate_ids": list(group.candidate_ids),
                    "alternative_count": len(
                        next(
                            (
                                item.alternative_candidate_ids
                                for item in self.decisions
                                if item.group_id == group.group_id
                            ),
                            (),
                        )
                    ),
                }
                for group in self.groups
            ],
            "receipt": None if self.receipt is None else self.receipt.to_public_dict(),
            "evaluation": None if self.evaluation is None else self.evaluation.to_wire(),
        }


def _resolve_group(candidates: tuple[AudioFusionCandidate, ...]) -> AudioFusionResolution:
    supported = tuple(
        item for item in candidates if item.support is not AudioFusionSupport.UNSUPPORTED
    )
    labels = {item.claim for item in supported if item.claim is not None}
    if not labels:
        return AudioFusionResolution.ABSTAINED
    if len(labels) > 1:
        exact_conflict = any(item.exact for item in supported) and any(
            item.authority is not AudioFusionAuthority.USER_AUTHORED for item in supported
        )
        uncertain_conflict = any(
            uncertainty.kind is UncertaintyKind.CONFLICTING
            for item in supported
            for uncertainty in item.uncertainties
        )
        if exact_conflict or uncertain_conflict or any(item.high_impact for item in supported):
            return (
                AudioFusionResolution.ABSTAINED
                if any(item.high_impact for item in supported)
                else AudioFusionResolution.CONFLICT
            )
        return AudioFusionResolution.ALTERNATIVES
    if any(item.support is AudioFusionSupport.UNCERTAIN for item in supported):
        return AudioFusionResolution.UNCERTAIN
    return AudioFusionResolution.CONSENSUS


def fuse_audio_evidence(request: AudioFusionRequest) -> AudioFusionDocument:
    if not isinstance(request, AudioFusionRequest):
        raise AudioEvidenceFusionError("request must be AudioFusionRequest")
    grouped: dict[str, list[AudioFusionCandidate]] = {}
    order: list[str] = []
    for candidate in request.candidates:
        if candidate.group_id not in grouped:
            grouped[candidate.group_id] = []
            order.append(candidate.group_id)
        grouped[candidate.group_id].append(candidate)
    if len(order) > request.max_groups:
        raise AudioEvidenceFusionError("fusion group count exceeds request limit")
    groups: list[AudioFusionGroup] = []
    decisions: list[AudioFusionDecision] = []
    for group_id in order:
        members = tuple(grouped[group_id])
        first = members[0]
        if any(
            item.category is not first.category or item.source != first.source for item in members
        ):
            raise AudioEvidenceFusionError(
                "fusion group members must share category and source anchor"
            )
        resolution = _resolve_group(members)
        ids = tuple(item.candidate_id for item in members)
        groups.append(AudioFusionGroup(group_id, first.category, first.source, ids, resolution))
        confidences = tuple(item.confidence for item in members if item.confidence is not None)
        raw = sum(confidences, Decimal("0")) / Decimal(len(confidences)) if confidences else None
        calibrated = (
            None
            if raw is None or resolution is AudioFusionResolution.ABSTAINED
            else request.calibration_profile.calibrate(raw)
        )
        selected = (
            ids
            if resolution in {AudioFusionResolution.CONSENSUS, AudioFusionResolution.UNCERTAIN}
            else ()
        )
        alternatives = (
            ids
            if resolution
            in {
                AudioFusionResolution.ALTERNATIVES,
                AudioFusionResolution.CONFLICT,
                AudioFusionResolution.ABSTAINED,
            }
            and len({item.claim for item in members if item.claim is not None}) > 1
            else ()
        )
        reason = None
        if resolution is AudioFusionResolution.ABSTAINED:
            reason = (
                "unsupported_or_unknown"
                if not any(item.claim for item in members)
                else "high_impact_disagreement"
            )
        decisions.append(
            AudioFusionDecision(
                f"decision_{group_id}",
                group_id,
                resolution,
                selected,
                alternatives,
                raw,
                calibrated,
                reason,
            )
        )
    routes = {item.receipt.route for item in request.candidates}
    route = next(iter(routes)) if len(routes) == 1 else AudioFusionRoute.MIXED
    source_fingerprints = tuple(
        dict.fromkeys(item.source.source_fingerprint for item in request.candidates)
    )
    material = {
        "document_id": request.document_id,
        "candidates": [item.to_wire() for item in request.candidates],
        "groups": [item.to_wire() for item in groups],
        "decisions": [item.to_wire() for item in decisions],
    }
    output_fingerprint = canonical_fingerprint(material)
    resource = AudioFusionResourceReceipt(
        cache_status=AudioFusionCacheStatus.DISABLED,
        cache_key_fingerprint=None,
        memory_bytes=0,
        wall_time_ms=0,
        output_bytes=len(repr(material).encode("utf-8")),
        output_items=len(request.candidates) + len(groups),
        cancellation_checkpoints=0,
    )
    receipt = AudioFusionReceipt(
        route, "audio_evidence_fusion", "1.0.0", output_fingerprint, source_fingerprints, resource
    )
    return AudioFusionDocument(
        request.document_id,
        AudioFusionStatus.COMPLETE,
        request.candidates,
        tuple(groups),
        tuple(decisions),
        request.calibration_profile,
        receipt,
        request.benchmark,
    )


def build_default_audio_fusion_calibration_profile(
    plan: AudioFusionBenchmarkPlan,
) -> AudioCalibrationProfile:
    if not isinstance(plan, AudioFusionBenchmarkPlan):
        raise AudioEvidenceFusionError("plan must be AudioFusionBenchmarkPlan")
    return AudioCalibrationProfile(
        "fixture_audio_calibration",
        "1.0.0",
        plan.development_case_ids,
        plan.held_out_case_ids,
        (
            AudioCalibrationBin(Decimal("0"), Decimal("0.25"), Decimal("0.20")),
            AudioCalibrationBin(Decimal("0.25"), Decimal("0.50"), Decimal("0.45")),
            AudioCalibrationBin(Decimal("0.50"), Decimal("0.75"), Decimal("0.65")),
            AudioCalibrationBin(Decimal("0.75"), Decimal("1"), Decimal("0.85")),
        ),
    )


def build_audio_fusion_abstention(
    request: AudioFusionRequest, status: AudioFusionStatus, diagnostic: str
) -> AudioFusionDocument:
    if not isinstance(request, AudioFusionRequest):
        raise AudioEvidenceFusionError("request must be AudioFusionRequest")
    if status is AudioFusionStatus.COMPLETE:
        raise AudioEvidenceFusionError("complete is not an abstention status")
    _text(diagnostic, "fusion diagnostic", 512)
    return AudioFusionDocument(
        request.document_id,
        status,
        (),
        (),
        (),
        request.calibration_profile,
        None,
        request.benchmark,
        diagnostics=(diagnostic,),
    )


@runtime_checkable
class AudioEvidenceFusionAdapter(Protocol):
    @property
    def descriptor(self) -> LocalAdapterDescriptor: ...

    def analyze(
        self, request: AudioFusionRequest, guard: LocalBudgetGuard
    ) -> AudioFusionDocument: ...


class _AudioFusionBridge:
    def __init__(self, adapter: AudioEvidenceFusionAdapter) -> None:
        self._adapter = adapter

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._adapter.descriptor

    def run(
        self, request: LocalAdapterExecutionRequest, guard: LocalBudgetGuard
    ) -> LocalAdapterResult:
        if not isinstance(request.input_value, AudioFusionRequest):
            raise AudioEvidenceFusionError("audio fusion adapter input is not AudioFusionRequest")
        document = self._adapter.analyze(request.input_value, guard)
        if not isinstance(document, AudioFusionDocument):
            raise AudioEvidenceFusionError("audio fusion adapter returned an invalid document")
        if document.receipt is not None:
            resource = replace(
                document.receipt.resource,
                wall_time_ms=int(guard.elapsed_seconds * 1000),
                output_bytes=len(repr(document.to_wire()).encode("utf-8")),
                output_items=len(document.candidates) + len(document.groups),
                cancellation_checkpoints=1,
            )
            receipt = replace(document.receipt, resource=resource)
            document = replace(document, receipt=receipt)
        return LocalAdapterResult(
            adapter_id=self.descriptor.adapter_id,
            adapter_version=self.descriptor.adapter_version,
            device=request.device,
            value=document,
            output_bytes=len(repr(document.to_wire()).encode("utf-8")),
            output_items=len(document.candidates) + len(document.groups),
        )


def execute_audio_evidence_fusion(
    adapter: AudioEvidenceFusionAdapter,
    request: AudioFusionRequest,
    *,
    runtime: LocalAdapterRuntime | None = None,
    device: LocalDeviceSpec | None = None,
    cancellation_probe: LocalCancellationProbe | None = None,
    clock: Callable[[], float] | None = None,
    memory_meter: Callable[[], int] | None = None,
) -> AudioFusionDocument:
    if not isinstance(adapter, AudioEvidenceFusionAdapter):
        raise AudioEvidenceFusionError("adapter must implement AudioEvidenceFusionAdapter")
    if not isinstance(request, AudioFusionRequest):
        raise AudioEvidenceFusionError("request must be AudioFusionRequest")
    device_value = LocalDeviceSpec(LocalDeviceKind.AUTO) if device is None else device
    if not isinstance(device_value, LocalDeviceSpec):
        raise AudioEvidenceFusionError("device must be LocalDeviceSpec")
    execution_request = LocalAdapterExecutionRequest(
        adapter_id=adapter.descriptor.adapter_id,
        task_mode=TaskMode.T2VA,
        media_kinds=(MediaKind.AUDIO,),
        reference_count=len({candidate.source.asset_id for candidate in request.candidates}),
        device=device_value,
        estimated_memory_bytes=1,
        estimated_output_bytes=adapter.descriptor.limits.max_output_bytes,
        deterministic_required=True,
        seed=0,
        cancellation_required=cancellation_probe is not None,
        input_value=request,
    )
    bridge = _AudioFusionBridge(adapter)
    kwargs: dict[str, object] = {
        "runtime": runtime,
        "cancellation_probe": cancellation_probe,
        "memory_meter": memory_meter,
    }
    if clock is not None:
        kwargs["clock"] = clock
    result = run_local_adapter(bridge, execution_request, **kwargs)  # type: ignore[arg-type]
    if not isinstance(result.value, AudioFusionDocument):
        raise AudioEvidenceFusionError("audio fusion result did not contain a document")
    if result.value.document_id != request.document_id:
        raise AudioEvidenceFusionError("audio fusion document ID does not match request")
    return result.value


__all__ = [
    "AUDIO_EVIDENCE_FUSION_SCHEMA",
    "AUDIO_FUSION_BENCHMARK_SCHEMA",
    "MAX_AUDIO_FUSION_CANDIDATES",
    "MAX_AUDIO_FUSION_GROUPS",
    "MAX_AUDIO_FUSION_RECEIPTS",
    "MAX_AUDIO_FUSION_UNCERTAINTIES",
    "MAX_AUDIO_FUSION_CASES",
    "MAX_AUDIO_FUSION_THRESHOLDS",
    "MAX_AUDIO_FUSION_BINS",
    "MAX_AUDIO_FUSION_PROFILES",
    "MAX_AUDIO_FUSION_DIAGNOSTICS",
    "MAX_AUDIO_FUSION_OUTPUT_BYTES",
    "AudioFusionStatus",
    "AudioFusionRoute",
    "AudioFusionDisposition",
    "AudioFusionAuthority",
    "AudioFusionCategory",
    "AudioFusionSupport",
    "AudioFusionResolution",
    "AudioFusionCaseKind",
    "AudioFusionCacheStatus",
    "AudioFusionSourceRef",
    "AudioFusionCandidateReceipt",
    "AudioFusionCandidate",
    "AudioFusionGroup",
    "AudioFusionDecision",
    "AudioCalibrationBin",
    "AudioCalibrationProfile",
    "calibrate_audio_confidence",
    "AudioFusionThreshold",
    "AudioFusionBenchmarkCase",
    "AudioFusionCandidateProfile",
    "AudioFusionRoutingPolicy",
    "AudioFusionBenchmarkPlan",
    "build_default_audio_fusion_benchmark_plan",
    "AudioFusionRequest",
    "AudioFusionResourceReceipt",
    "AudioFusionReceipt",
    "AudioFusionEvaluationSample",
    "AudioFusionEvaluation",
    "evaluate_audio_fusion_benchmark",
    "AudioFusionDocument",
    "fuse_audio_evidence",
    "build_default_audio_fusion_calibration_profile",
    "build_audio_fusion_abstention",
    "AudioEvidenceFusionAdapter",
    "execute_audio_evidence_fusion",
]
