"""Provenance-preserving visual evidence fusion and calibration contracts for M11-07.

This pure-core module merges already produced, source-owned observations.  It does not discover
providers, run models, open media, or turn a transport label into a capability claim.  Conflicting
claims remain visible alternatives; calibration is an immutable, bounded mapping fitted only from
the declared development split.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from .canonical import canonical_fingerprint
from .contracts import MediaKind, TaskMode
from .errors import VisualEvidenceFusionError
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
from .temporal_visual_analysis import TemporalInterval

VISUAL_EVIDENCE_FUSION_SCHEMA = "h3.visual.evidence_fusion.v1"
VISUAL_FUSION_BENCHMARK_SCHEMA = "h3.visual.evidence_fusion.benchmark.v1"
MAX_FUSION_CANDIDATES = 256
MAX_FUSION_GROUPS = 64
MAX_FUSION_RECEIPTS = 32
MAX_FUSION_UNCERTAINTIES = 64
MAX_FUSION_CASES = 32
MAX_FUSION_THRESHOLDS = 16
MAX_FUSION_OUTPUT_BYTES = 65_536

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


class FusionStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"


class FusionRoute(str, Enum):
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"
    SPECIALIST = "specialist"
    MIXED = "mixed"


class FusionCategory(str, Enum):
    GLOBAL_DESCRIPTION = "global_description"
    OCR = "ocr"
    SUBJECT_OBJECT = "subject_object"
    IDENTITY = "identity"
    ACTION = "action"
    STATE = "state"
    OPTICAL_MOTION = "optical_motion"
    CAMERA_MOTION = "camera_motion"
    COMPOSITION = "composition"
    EDIT = "edit"
    STYLE = "style"


class FusionSupport(str, Enum):
    SUPPORTED = "supported"
    UNCERTAIN = "uncertain"
    UNSUPPORTED = "unsupported"


class FusionResolution(str, Enum):
    CONSENSUS = "consensus"
    ALTERNATIVES = "alternatives"
    CONFLICT = "conflict"
    UNCERTAIN = "uncertain"
    ABSTAINED = "abstained"


class FusionCaseKind(str, Enum):
    CLEAN_CONSENSUS = "clean_consensus"
    CORROBORATION = "corroboration"
    SOURCE_MISMATCH = "source_mismatch"
    LABEL_CONFLICT = "label_conflict"
    AMBIGUOUS_ALTERNATIVES = "ambiguous_alternatives"
    UNSUPPORTED = "unsupported"
    LOW_CONFIDENCE = "low_confidence"
    HIGH_IMPACT_ABSTENTION = "high_impact_abstention"
    HELD_OUT_CALIBRATION = "held_out_calibration"
    TERMINAL = "terminal"


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise VisualEvidenceFusionError(f"{field} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise VisualEvidenceFusionError(f"{field} contains sensitive or locator material")
    return value


def _code(value: object, field: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value.casefold()) is None:
        raise VisualEvidenceFusionError(f"{field} must be a lower-case bounded code")
    return value.casefold()


def _text(value: object, field: str, maximum: int = 2_048) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise VisualEvidenceFusionError(f"{field} must be bounded non-empty text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise VisualEvidenceFusionError(f"{field} contains a control character")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise VisualEvidenceFusionError(f"{field} contains sensitive or locator material")
    return value


def _fingerprint(value: object, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise VisualEvidenceFusionError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _version(value: object, field: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise VisualEvidenceFusionError(f"{field} must be a numeric version")
    return value


def _decimal(value: object, field: str, *, required: bool = True) -> Decimal | None:
    if value is None and not required:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or not Decimal("0") <= value <= 1:
        raise VisualEvidenceFusionError(f"{field} must be a finite Decimal between 0 and 1")
    return value


def _ids(values: object, field: str, maximum: int, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise VisualEvidenceFusionError(f"{field} is outside the finite limit")
    if required and not values:
        raise VisualEvidenceFusionError(f"{field} must not be empty")
    result = tuple(_identifier(value, f"{field} item") for value in values)
    if len(result) != len(set(result)):
        raise VisualEvidenceFusionError(f"{field} must not contain duplicates")
    return result


def _uncertainties(values: object) -> tuple[Uncertainty, ...]:
    if (
        not isinstance(values, tuple)
        or len(values) > MAX_FUSION_UNCERTAINTIES
        or not all(isinstance(value, Uncertainty) for value in values)
    ):
        raise VisualEvidenceFusionError("uncertainties are outside the finite limit")
    return values


@dataclass(frozen=True, slots=True)
class FusionSourceRef:
    """Stable source/asset anchor shared by every candidate in one fusion group."""

    asset_id: str
    source_id: str
    source_fingerprint: str
    interval: TemporalInterval | None = None

    def __post_init__(self) -> None:
        _identifier(self.asset_id, "source asset_id")
        _identifier(self.source_id, "source source_id")
        _fingerprint(self.source_fingerprint, "source fingerprint")
        if self.interval is not None:
            if not isinstance(self.interval, TemporalInterval):
                raise VisualEvidenceFusionError("source interval must be TemporalInterval")
            if self.interval.asset_id != self.asset_id or self.interval.source_id != self.source_id:
                raise VisualEvidenceFusionError("source interval ownership differs")

    def to_wire(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
            "interval": None if self.interval is None else self.interval.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class FusionCandidateReceipt:
    """Redacted identity of the explicitly selected specialist adapter output."""

    route: FusionRoute
    adapter_id: str
    adapter_version: str
    model_id: str
    model_digest: str
    output_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.route, FusionRoute) or self.route is FusionRoute.MIXED:
            raise VisualEvidenceFusionError("candidate receipt route must be a concrete route")
        _code(self.adapter_id, "receipt adapter_id")
        _version(self.adapter_version, "receipt adapter_version")
        _identifier(self.model_id, "receipt model_id")
        _fingerprint(self.model_digest, "receipt model_digest")
        _fingerprint(self.output_fingerprint, "receipt output_fingerprint")

    def to_wire(self) -> dict[str, object]:
        return {
            "route": self.route.value,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "output_fingerprint": self.output_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class FusionCandidate:
    """One source-owned observation retained as an input to a fusion group."""

    candidate_id: str
    group_id: str
    category: FusionCategory
    label: str | None
    support: FusionSupport
    confidence: Decimal | None
    source: FusionSourceRef
    receipt: FusionCandidateReceipt
    uncertainties: tuple[Uncertainty, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "candidate_id")
        _identifier(self.group_id, "candidate group_id")
        if not isinstance(self.category, FusionCategory):
            raise VisualEvidenceFusionError("candidate category is unsupported")
        if not isinstance(self.support, FusionSupport):
            raise VisualEvidenceFusionError("candidate support is unsupported")
        if not isinstance(self.source, FusionSourceRef):
            raise VisualEvidenceFusionError("candidate source must be FusionSourceRef")
        if not isinstance(self.receipt, FusionCandidateReceipt):
            raise VisualEvidenceFusionError("candidate receipt must be FusionCandidateReceipt")
        uncertainty_values = _uncertainties(self.uncertainties)
        object.__setattr__(self, "uncertainties", uncertainty_values)
        if self.support is FusionSupport.UNSUPPORTED:
            if self.label is not None or self.confidence is not None:
                raise VisualEvidenceFusionError(
                    "unsupported candidate cannot carry label/confidence"
                )
            if not uncertainty_values:
                raise VisualEvidenceFusionError("unsupported candidate requires uncertainty")
        else:
            if self.label is None:
                raise VisualEvidenceFusionError("supported candidate requires a label")
            _text(self.label, "candidate label")
            _decimal(self.confidence, "candidate confidence")
            if self.support is FusionSupport.UNCERTAIN and not uncertainty_values:
                raise VisualEvidenceFusionError("uncertain candidate requires uncertainty")

    def to_wire(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "group_id": self.group_id,
            "category": self.category.value,
            "label": self.label,
            "support": self.support.value,
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "source": self.source.to_wire(),
            "receipt": self.receipt.to_wire(),
            "uncertainties": [item.to_wire() for item in self.uncertainties],
        }


@dataclass(frozen=True, slots=True)
class FusionGroup:
    """A deterministic group of candidates anchored to one source observation."""

    group_id: str
    category: FusionCategory
    source: FusionSourceRef
    candidate_ids: tuple[str, ...]
    resolution: FusionResolution

    def __post_init__(self) -> None:
        _identifier(self.group_id, "group_id")
        if not isinstance(self.category, FusionCategory):
            raise VisualEvidenceFusionError("group category is unsupported")
        if not isinstance(self.source, FusionSourceRef):
            raise VisualEvidenceFusionError("group source must be FusionSourceRef")
        _ids(self.candidate_ids, "group candidate_ids", MAX_FUSION_CANDIDATES, required=True)
        if not isinstance(self.resolution, FusionResolution):
            raise VisualEvidenceFusionError("group resolution is unsupported")

    def to_wire(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "category": self.category.value,
            "source": self.source.to_wire(),
            "candidate_ids": list(self.candidate_ids),
            "resolution": self.resolution.value,
        }


@dataclass(frozen=True, slots=True)
class FusionDecision:
    """Decision metadata that never removes the underlying candidates."""

    decision_id: str
    group_id: str
    resolution: FusionResolution
    selected_candidate_ids: tuple[str, ...]
    alternative_candidate_ids: tuple[str, ...]
    raw_confidence: Decimal | None
    calibrated_confidence: Decimal | None
    abstention_reason: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.decision_id, "decision_id")
        _identifier(self.group_id, "decision group_id")
        if not isinstance(self.resolution, FusionResolution):
            raise VisualEvidenceFusionError("decision resolution is unsupported")
        _ids(self.selected_candidate_ids, "selected candidate IDs", MAX_FUSION_CANDIDATES)
        _ids(self.alternative_candidate_ids, "alternative candidate IDs", MAX_FUSION_CANDIDATES)
        if set(self.selected_candidate_ids) & set(self.alternative_candidate_ids):
            raise VisualEvidenceFusionError("selected and alternative IDs must be disjoint")
        _decimal(self.raw_confidence, "decision raw confidence", required=False)
        _decimal(self.calibrated_confidence, "decision calibrated confidence", required=False)
        if self.resolution is FusionResolution.ABSTAINED:
            if self.calibrated_confidence is not None or not self.abstention_reason:
                raise VisualEvidenceFusionError(
                    "abstained decision requires a reason and no confidence"
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
class CalibrationBin:
    lower: Decimal
    upper: Decimal
    calibrated: Decimal

    def __post_init__(self) -> None:
        for value, field in (
            (self.lower, "bin lower"),
            (self.upper, "bin upper"),
            (self.calibrated, "bin calibrated"),
        ):
            _decimal(value, field)
        if self.lower >= self.upper:
            raise VisualEvidenceFusionError("calibration bin lower must be less than upper")

    def to_wire(self) -> dict[str, str]:
        return {
            "lower": format(self.lower, "f"),
            "upper": format(self.upper, "f"),
            "calibrated": format(self.calibrated, "f"),
        }


@dataclass(frozen=True, slots=True)
class CalibrationProfile:
    """Immutable monotonic calibration profile fitted only on declared development cases."""

    profile_id: str
    version: str
    fit_case_ids: tuple[str, ...]
    held_out_case_ids: tuple[str, ...] = ()
    bins: tuple[CalibrationBin, ...] = ()
    schema: str = VISUAL_EVIDENCE_FUSION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.profile_id, "calibration profile_id")
        _version(self.version, "calibration version")
        fit_ids = _ids(
            self.fit_case_ids, "calibration fit_case_ids", MAX_FUSION_CASES, required=True
        )
        held_out_ids = _ids(
            self.held_out_case_ids, "calibration held_out_case_ids", MAX_FUSION_CASES
        )
        if set(fit_ids) & set(held_out_ids):
            raise VisualEvidenceFusionError("calibration fit and held-out cases must be disjoint")
        if not isinstance(self.bins, tuple) or not self.bins:
            raise VisualEvidenceFusionError("calibration bins must not be empty")
        if not all(isinstance(item, CalibrationBin) for item in self.bins):
            raise VisualEvidenceFusionError("calibration bins are invalid")
        previous: CalibrationBin | None = None
        for item in self.bins:
            if previous is not None and (
                item.lower != previous.upper or item.calibrated < previous.calibrated
            ):
                raise VisualEvidenceFusionError("calibration bins must be contiguous and monotonic")
            previous = item
        if self.bins[0].lower != Decimal("0") or self.bins[-1].upper != Decimal("1"):
            raise VisualEvidenceFusionError("calibration bins must cover [0,1]")
        if self.schema != VISUAL_EVIDENCE_FUSION_SCHEMA:
            raise VisualEvidenceFusionError("unsupported calibration schema")

    def calibrate(self, confidence: Decimal) -> Decimal:
        _decimal(confidence, "calibration confidence")
        for index, item in enumerate(self.bins):
            if item.lower <= confidence < item.upper or (
                index == len(self.bins) - 1 and confidence <= item.upper
            ):
                return item.calibrated
        raise VisualEvidenceFusionError("confidence did not fit calibration bins")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "profile_id": self.profile_id,
            "version": self.version,
            "fit_case_ids": list(self.fit_case_ids),
            "held_out_case_ids": list(self.held_out_case_ids),
            "bins": [item.to_wire() for item in self.bins],
        }


def calibrate_confidence(profile: CalibrationProfile, confidence: Decimal) -> Decimal:
    if not isinstance(profile, CalibrationProfile):
        raise VisualEvidenceFusionError("profile must be CalibrationProfile")
    return profile.calibrate(confidence)


@dataclass(frozen=True, slots=True)
class FusionThreshold:
    metric: str
    minimum: Decimal | None = None
    maximum: Decimal | None = None

    def __post_init__(self) -> None:
        _code(self.metric, "fusion metric")
        if self.minimum is None and self.maximum is None:
            raise VisualEvidenceFusionError("fusion threshold requires minimum or maximum")
        _decimal(self.minimum, "fusion minimum", required=False)
        _decimal(self.maximum, "fusion maximum", required=False)
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise VisualEvidenceFusionError("fusion threshold range is inverted")

    def to_wire(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "minimum": None if self.minimum is None else format(self.minimum, "f"),
            "maximum": None if self.maximum is None else format(self.maximum, "f"),
        }


@dataclass(frozen=True, slots=True)
class FusionBenchmarkCase:
    case_id: str
    split: str
    kind: FusionCaseKind
    high_impact: bool

    def __post_init__(self) -> None:
        _identifier(self.case_id, "fusion case_id")
        if self.split not in {"development", "held_out"}:
            raise VisualEvidenceFusionError("fusion case split is unsupported")
        if not isinstance(self.kind, FusionCaseKind):
            raise VisualEvidenceFusionError("fusion case kind is unsupported")
        if not isinstance(self.high_impact, bool):
            raise VisualEvidenceFusionError("fusion case high_impact must be bool")

    def to_wire(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "split": self.split,
            "kind": self.kind.value,
            "high_impact": self.high_impact,
        }


@dataclass(frozen=True, slots=True)
class FusionBenchmarkPlan:
    cases: tuple[FusionBenchmarkCase, ...]
    thresholds: tuple[FusionThreshold, ...]
    schema: str = VISUAL_FUSION_BENCHMARK_SCHEMA
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.cases, tuple)
            or not self.cases
            or len(self.cases) > MAX_FUSION_CASES
        ):
            raise VisualEvidenceFusionError("fusion cases are outside the finite limit")
        if not all(isinstance(item, FusionBenchmarkCase) for item in self.cases):
            raise VisualEvidenceFusionError("fusion cases are invalid")
        if len({item.case_id for item in self.cases}) != len(self.cases):
            raise VisualEvidenceFusionError("fusion case IDs must be unique")
        if (
            not isinstance(self.thresholds, tuple)
            or not self.thresholds
            or len(self.thresholds) > MAX_FUSION_THRESHOLDS
        ):
            raise VisualEvidenceFusionError("fusion thresholds are outside the finite limit")
        if not all(isinstance(item, FusionThreshold) for item in self.thresholds):
            raise VisualEvidenceFusionError("fusion thresholds are invalid")
        if len({item.metric for item in self.thresholds}) != len(self.thresholds):
            raise VisualEvidenceFusionError("fusion metrics must be unique")
        expected = canonical_fingerprint(
            {
                "schema": self.schema,
                "cases": [item.to_wire() for item in self.cases],
                "thresholds": [item.to_wire() for item in self.thresholds],
            }
        )
        if self.fingerprint and self.fingerprint != expected:
            raise VisualEvidenceFusionError("fusion benchmark fingerprint does not match contents")
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
        }


def build_default_visual_fusion_benchmark_plan() -> FusionBenchmarkPlan:
    cases = (
        FusionBenchmarkCase(
            "fusion.clean_consensus", "development", FusionCaseKind.CLEAN_CONSENSUS, False
        ),
        FusionBenchmarkCase(
            "fusion.corroboration", "development", FusionCaseKind.CORROBORATION, False
        ),
        FusionBenchmarkCase(
            "fusion.source_mismatch", "development", FusionCaseKind.SOURCE_MISMATCH, True
        ),
        FusionBenchmarkCase(
            "fusion.label_conflict", "development", FusionCaseKind.LABEL_CONFLICT, True
        ),
        FusionBenchmarkCase(
            "fusion.alternatives", "development", FusionCaseKind.AMBIGUOUS_ALTERNATIVES, True
        ),
        FusionBenchmarkCase("fusion.unsupported", "development", FusionCaseKind.UNSUPPORTED, True),
        FusionBenchmarkCase(
            "fusion.low_confidence", "held_out", FusionCaseKind.LOW_CONFIDENCE, True
        ),
        FusionBenchmarkCase(
            "fusion.high_impact_abstention", "held_out", FusionCaseKind.HIGH_IMPACT_ABSTENTION, True
        ),
        FusionBenchmarkCase(
            "fusion.held_out_calibration", "held_out", FusionCaseKind.HELD_OUT_CALIBRATION, False
        ),
        FusionBenchmarkCase("fusion.terminal", "held_out", FusionCaseKind.TERMINAL, False),
    )
    thresholds = (
        FusionThreshold("expected_calibration_error", maximum=Decimal("0.10")),
        FusionThreshold("brier_score", maximum=Decimal("0.15")),
        FusionThreshold("disagreement_preservation", minimum=Decimal("1.0")),
        FusionThreshold("source_ownership_violations", maximum=Decimal("0")),
        FusionThreshold("high_impact_error_improvement", minimum=Decimal("0.20")),
    )
    return FusionBenchmarkPlan(cases=cases, thresholds=thresholds)


@dataclass(frozen=True, slots=True)
class FusionRequest:
    candidates: tuple[FusionCandidate, ...]
    calibration_profile: CalibrationProfile
    benchmark: FusionBenchmarkPlan | None = None
    document_id: str = "fusion_document"
    max_groups: int = MAX_FUSION_GROUPS

    def __post_init__(self) -> None:
        _identifier(self.document_id, "fusion document_id")
        if not isinstance(self.candidates, tuple) or not self.candidates:
            raise VisualEvidenceFusionError("fusion candidates must be a non-empty tuple")
        if len(self.candidates) > MAX_FUSION_CANDIDATES:
            raise VisualEvidenceFusionError("fusion candidates exceed the finite limit")
        if not all(isinstance(item, FusionCandidate) for item in self.candidates):
            raise VisualEvidenceFusionError("fusion candidates are invalid")
        if len({item.candidate_id for item in self.candidates}) != len(self.candidates):
            raise VisualEvidenceFusionError("fusion candidate IDs must be unique")
        if not isinstance(self.calibration_profile, CalibrationProfile):
            raise VisualEvidenceFusionError("fusion calibration_profile is invalid")
        if self.benchmark is not None and not isinstance(self.benchmark, FusionBenchmarkPlan):
            raise VisualEvidenceFusionError("fusion benchmark is invalid")
        if (
            isinstance(self.max_groups, bool)
            or not isinstance(self.max_groups, int)
            or not 1 <= self.max_groups <= MAX_FUSION_GROUPS
        ):
            raise VisualEvidenceFusionError("fusion max_groups is outside the finite limit")
        if self.benchmark is not None:
            if set(self.calibration_profile.fit_case_ids) != set(
                self.benchmark.development_case_ids
            ):
                raise VisualEvidenceFusionError(
                    "calibration fit cases must equal benchmark development split"
                )
            if set(self.calibration_profile.held_out_case_ids) != set(
                self.benchmark.held_out_case_ids
            ):
                raise VisualEvidenceFusionError(
                    "calibration held-out cases must equal benchmark held-out split"
                )


@dataclass(frozen=True, slots=True)
class FusionReceipt:
    route: FusionRoute
    adapter_id: str
    adapter_version: str
    output_fingerprint: str
    source_fingerprints: tuple[str, ...]
    schema: str = VISUAL_EVIDENCE_FUSION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.route, FusionRoute):
            raise VisualEvidenceFusionError("fusion receipt route is unsupported")
        _code(self.adapter_id, "fusion receipt adapter_id")
        _version(self.adapter_version, "fusion receipt adapter_version")
        _fingerprint(self.output_fingerprint, "fusion receipt output_fingerprint")
        fingerprints = _ids(
            self.source_fingerprints,
            "fusion source_fingerprints",
            MAX_FUSION_RECEIPTS,
            required=True,
        )
        for value in fingerprints:
            _fingerprint(value, "fusion source fingerprint")
        if self.schema != VISUAL_EVIDENCE_FUSION_SCHEMA:
            raise VisualEvidenceFusionError("unsupported fusion receipt schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "route": self.route.value,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "output_fingerprint": self.output_fingerprint,
            "source_fingerprints": list(self.source_fingerprints),
        }

    def to_public_dict(self) -> dict[str, object]:
        result = self.to_wire()
        result["source_count"] = len(self.source_fingerprints)
        return result


@dataclass(frozen=True, slots=True)
class FusionEvaluationSample:
    case_id: str
    confidence: Decimal
    correct: bool
    abstained: bool
    high_impact: bool

    def __post_init__(self) -> None:
        _identifier(self.case_id, "evaluation case_id")
        _decimal(self.confidence, "evaluation confidence")
        if (
            not isinstance(self.correct, bool)
            or not isinstance(self.abstained, bool)
            or not isinstance(self.high_impact, bool)
        ):
            raise VisualEvidenceFusionError("evaluation flags must be bool")


@dataclass(frozen=True, slots=True)
class FusionEvaluation:
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
            raise VisualEvidenceFusionError("fusion evaluation must use held_out split")
        _ids(self.case_ids, "evaluation case_ids", MAX_FUSION_CASES, required=True)
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
            raise VisualEvidenceFusionError("evaluation improvement does not match error rates")
        if self.abstention_improves_high_impact_error != self.high_impact_error_improvement > 0:
            raise VisualEvidenceFusionError("evaluation improvement flag is inconsistent")

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


def evaluate_fusion_benchmark(
    plan: FusionBenchmarkPlan,
    profile: CalibrationProfile,
    samples: tuple[FusionEvaluationSample, ...],
) -> FusionEvaluation:
    if not isinstance(plan, FusionBenchmarkPlan) or not isinstance(profile, CalibrationProfile):
        raise VisualEvidenceFusionError(
            "evaluation requires a benchmark plan and calibration profile"
        )
    if not isinstance(samples, tuple) or not samples:
        raise VisualEvidenceFusionError("evaluation samples must be non-empty")
    held_out = set(plan.held_out_case_ids)
    if set(profile.held_out_case_ids) != held_out:
        raise VisualEvidenceFusionError("evaluation profile held-out split differs from benchmark")
    if any(item.case_id not in held_out for item in samples):
        raise VisualEvidenceFusionError("evaluation sample is not in held-out split")
    if len({item.case_id for item in samples}) != len(samples):
        raise VisualEvidenceFusionError("evaluation sample IDs must be unique")
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
        raise VisualEvidenceFusionError("evaluation requires high-impact samples")
    raw_errors = sum(not item.correct for item in high_impact)
    abstained_errors = sum(not item.correct and not item.abstained for item in high_impact)
    raw_rate = Decimal(raw_errors) / Decimal(len(high_impact))
    abstained_rate = Decimal(abstained_errors) / Decimal(len(high_impact))
    return FusionEvaluation(
        split="held_out",
        case_ids=tuple(item.case_id for item in samples),
        expected_calibration_error=ece,
        brier_score=brier,
        high_impact_error_rate_without_abstention=raw_rate,
        high_impact_error_rate_with_abstention=abstained_rate,
        high_impact_error_improvement=raw_rate - abstained_rate,
        abstention_improves_high_impact_error=raw_rate > abstained_rate,
    )


@dataclass(frozen=True, slots=True)
class FusionDocument:
    document_id: str
    status: FusionStatus
    candidates: tuple[FusionCandidate, ...]
    groups: tuple[FusionGroup, ...]
    decisions: tuple[FusionDecision, ...]
    calibration_profile: CalibrationProfile
    receipt: FusionReceipt | None
    benchmark: FusionBenchmarkPlan | None = None
    evaluation: FusionEvaluation | None = None
    diagnostics: tuple[str, ...] = ()
    schema: str = VISUAL_EVIDENCE_FUSION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.document_id, "fusion document_id")
        if not isinstance(self.status, FusionStatus):
            raise VisualEvidenceFusionError("fusion document status is unsupported")
        if not isinstance(self.calibration_profile, CalibrationProfile):
            raise VisualEvidenceFusionError("fusion document calibration profile is invalid")
        if len(self.candidates) > MAX_FUSION_CANDIDATES or len(self.groups) > MAX_FUSION_GROUPS:
            raise VisualEvidenceFusionError("fusion document exceeds finite limits")
        if not all(isinstance(item, FusionCandidate) for item in self.candidates):
            raise VisualEvidenceFusionError("fusion document candidates are invalid")
        if len({item.candidate_id for item in self.candidates}) != len(self.candidates):
            raise VisualEvidenceFusionError("fusion document candidate IDs are not unique")
        if not all(isinstance(item, FusionGroup) for item in self.groups):
            raise VisualEvidenceFusionError("fusion document groups are invalid")
        if len({item.group_id for item in self.groups}) != len(self.groups):
            raise VisualEvidenceFusionError("fusion document group IDs are not unique")
        if not all(isinstance(item, FusionDecision) for item in self.decisions):
            raise VisualEvidenceFusionError("fusion document decisions are invalid")
        candidate_map = {item.candidate_id: item for item in self.candidates}
        group_map = {item.group_id: item for item in self.groups}
        for group in self.groups:
            for candidate_id in group.candidate_ids:
                candidate = candidate_map.get(candidate_id)
                if (
                    candidate is None
                    or candidate.group_id != group.group_id
                    or candidate.category is not group.category
                    or candidate.source != group.source
                ):
                    raise VisualEvidenceFusionError("fusion group candidate ownership differs")
        grouped_ids = [
            candidate_id for group in self.groups for candidate_id in group.candidate_ids
        ]
        if set(grouped_ids) != set(candidate_map) or len(grouped_ids) != len(set(grouped_ids)):
            raise VisualEvidenceFusionError("every candidate must belong to exactly one group")
        if len({item.decision_id for item in self.decisions}) != len(self.decisions):
            raise VisualEvidenceFusionError("fusion decision IDs are not unique")
        for decision in self.decisions:
            decision_group = group_map.get(decision.group_id)
            if decision_group is None:
                raise VisualEvidenceFusionError("fusion decision references an unknown group")
            allowed = set(decision_group.candidate_ids)
            if (
                not set(decision.selected_candidate_ids) <= allowed
                or not set(decision.alternative_candidate_ids) <= allowed
            ):
                raise VisualEvidenceFusionError("fusion decision references an unknown candidate")
            if decision.resolution is not decision_group.resolution:
                raise VisualEvidenceFusionError("fusion decision resolution differs from group")
        if self.receipt is not None and not isinstance(self.receipt, FusionReceipt):
            raise VisualEvidenceFusionError("fusion document receipt is invalid")
        if self.benchmark is not None and not isinstance(self.benchmark, FusionBenchmarkPlan):
            raise VisualEvidenceFusionError("fusion document benchmark is invalid")
        if self.evaluation is not None and not isinstance(self.evaluation, FusionEvaluation):
            raise VisualEvidenceFusionError("fusion document evaluation is invalid")
        if (
            not isinstance(self.diagnostics, tuple)
            or len(self.diagnostics) > 32
            or not all(isinstance(item, str) for item in self.diagnostics)
        ):
            raise VisualEvidenceFusionError("fusion diagnostics are invalid")
        if self.schema != VISUAL_EVIDENCE_FUSION_SCHEMA:
            raise VisualEvidenceFusionError("unsupported fusion document schema")
        if self.receipt is not None:
            expected_sources = {
                candidate.source.source_fingerprint for candidate in self.candidates
            }
            if set(self.receipt.source_fingerprints) != expected_sources:
                raise VisualEvidenceFusionError(
                    "fusion receipt does not cover all source fingerprints"
                )
        if self.benchmark is not None:
            if set(self.calibration_profile.fit_case_ids) != set(
                self.benchmark.development_case_ids
            ):
                raise VisualEvidenceFusionError(
                    "document calibration fit split differs from benchmark"
                )
            if set(self.calibration_profile.held_out_case_ids) != set(
                self.benchmark.held_out_case_ids
            ):
                raise VisualEvidenceFusionError(
                    "document calibration held-out split differs from benchmark"
                )
            if self.evaluation is not None and not set(self.evaluation.case_ids) <= set(
                self.benchmark.held_out_case_ids
            ):
                raise VisualEvidenceFusionError("document evaluation contains a non-held-out case")
        if self.status is FusionStatus.COMPLETE:
            if self.receipt is None or not self.candidates or not self.groups or not self.decisions:
                raise VisualEvidenceFusionError(
                    "complete fusion document requires evidence and receipt"
                )
            if not any(
                item.resolution is not FusionResolution.ABSTAINED for item in self.decisions
            ):
                raise VisualEvidenceFusionError(
                    "complete fusion document requires a non-abstained group"
                )
        elif (
            self.status in {FusionStatus.CANCELLED, FusionStatus.CORRUPT, FusionStatus.UNSUPPORTED}
            and self.receipt is not None
        ):
            raise VisualEvidenceFusionError("terminal fusion document cannot carry a receipt")
        if len(repr(self.to_wire()).encode("utf-8")) > MAX_FUSION_OUTPUT_BYTES:
            raise VisualEvidenceFusionError("fusion document exceeds portable output limit")

    @property
    def complete(self) -> bool:
        return self.status is FusionStatus.COMPLETE

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
                    "route_count": len(
                        {
                            candidate.receipt.route
                            for candidate in self.candidates
                            if candidate.candidate_id in group.candidate_ids
                        }
                    ),
                    "alternative_count": len(
                        next(
                            (
                                decision.alternative_candidate_ids
                                for decision in self.decisions
                                if decision.group_id == group.group_id
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


def _resolve_group(candidates: tuple[FusionCandidate, ...]) -> FusionResolution:
    supported = tuple(item for item in candidates if item.support is not FusionSupport.UNSUPPORTED)
    labels = {item.label for item in supported if item.label is not None}
    if not labels:
        return FusionResolution.ABSTAINED
    if len(labels) > 1:
        if any(
            uncertainty.kind is UncertaintyKind.CONFLICTING
            for item in supported
            for uncertainty in item.uncertainties
        ):
            return FusionResolution.CONFLICT
        return FusionResolution.ALTERNATIVES
    if any(item.support is FusionSupport.UNCERTAIN for item in supported):
        return FusionResolution.UNCERTAIN
    return FusionResolution.CONSENSUS


def fuse_visual_evidence(request: FusionRequest) -> FusionDocument:
    if not isinstance(request, FusionRequest):
        raise VisualEvidenceFusionError("request must be FusionRequest")
    grouped: dict[str, list[FusionCandidate]] = {}
    group_order: list[str] = []
    for candidate in request.candidates:
        if candidate.group_id not in grouped:
            grouped[candidate.group_id] = []
            group_order.append(candidate.group_id)
        grouped[candidate.group_id].append(candidate)
    if len(group_order) > request.max_groups:
        raise VisualEvidenceFusionError("fusion group count exceeds request limit")
    groups: list[FusionGroup] = []
    decisions: list[FusionDecision] = []
    for group_id in group_order:
        members = tuple(grouped[group_id])
        first = members[0]
        if any(
            item.category is not first.category or item.source != first.source for item in members
        ):
            raise VisualEvidenceFusionError(
                "fusion group members must share category and source anchor"
            )
        resolution = _resolve_group(members)
        candidate_ids = tuple(item.candidate_id for item in members)
        groups.append(
            FusionGroup(group_id, first.category, first.source, candidate_ids, resolution)
        )
        confidences = tuple(item.confidence for item in members if item.confidence is not None)
        raw = sum(confidences, Decimal("0")) / Decimal(len(confidences)) if confidences else None
        calibrated = (
            None
            if raw is None or resolution is FusionResolution.ABSTAINED
            else request.calibration_profile.calibrate(raw)
        )
        selected = (
            candidate_ids
            if resolution in {FusionResolution.CONSENSUS, FusionResolution.UNCERTAIN}
            else ()
        )
        alternatives = (
            candidate_ids
            if resolution in {FusionResolution.ALTERNATIVES, FusionResolution.CONFLICT}
            else ()
        )
        reason = "no supported candidate" if resolution is FusionResolution.ABSTAINED else None
        decisions.append(
            FusionDecision(
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
    route = next(iter(routes)) if len(routes) == 1 else FusionRoute.MIXED
    source_fingerprints = tuple(
        dict.fromkeys(item.source.source_fingerprint for item in request.candidates)
    )
    material = {
        "document_id": request.document_id,
        "candidates": [item.to_wire() for item in request.candidates],
        "groups": [item.to_wire() for item in groups],
        "decisions": [item.to_wire() for item in decisions],
    }
    receipt = FusionReceipt(
        route=route,
        adapter_id="visual_evidence_fusion",
        adapter_version="1.0.0",
        output_fingerprint=canonical_fingerprint(material),
        source_fingerprints=source_fingerprints,
    )
    return FusionDocument(
        request.document_id,
        FusionStatus.COMPLETE,
        request.candidates,
        tuple(groups),
        tuple(decisions),
        request.calibration_profile,
        receipt,
        request.benchmark,
    )


def build_visual_fusion_abstention(
    request: FusionRequest, status: FusionStatus, diagnostic: str
) -> FusionDocument:
    if not isinstance(request, FusionRequest):
        raise VisualEvidenceFusionError("request must be FusionRequest")
    if status is FusionStatus.COMPLETE:
        raise VisualEvidenceFusionError("complete is not an abstention status")
    _text(diagnostic, "fusion diagnostic", 512)
    return FusionDocument(
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


def build_default_visual_fusion_calibration_profile(
    plan: FusionBenchmarkPlan,
) -> CalibrationProfile:
    if not isinstance(plan, FusionBenchmarkPlan):
        raise VisualEvidenceFusionError("plan must be FusionBenchmarkPlan")
    return CalibrationProfile(
        profile_id="fixture_calibration",
        version="1.0.0",
        fit_case_ids=plan.development_case_ids,
        held_out_case_ids=plan.held_out_case_ids,
        bins=(
            CalibrationBin(Decimal("0"), Decimal("0.25"), Decimal("0.05")),
            CalibrationBin(Decimal("0.25"), Decimal("0.50"), Decimal("0.25")),
            CalibrationBin(Decimal("0.50"), Decimal("0.75"), Decimal("0.60")),
            CalibrationBin(Decimal("0.75"), Decimal("1"), Decimal("0.80")),
        ),
    )


@runtime_checkable
class VisualFusionAdapter(Protocol):
    """Explicit adapter seam used by the bounded local execution bridge."""

    @property
    def descriptor(self) -> LocalAdapterDescriptor: ...

    def analyze(self, request: FusionRequest, guard: LocalBudgetGuard) -> FusionDocument: ...


class _FusionBridge:
    def __init__(self, adapter: VisualFusionAdapter) -> None:
        self._adapter = adapter

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._adapter.descriptor

    def run(
        self, request: LocalAdapterExecutionRequest, guard: LocalBudgetGuard
    ) -> LocalAdapterResult:
        if not isinstance(request.input_value, FusionRequest):
            raise VisualEvidenceFusionError("fusion adapter input is not FusionRequest")
        document = self._adapter.analyze(request.input_value, guard)
        if not isinstance(document, FusionDocument):
            raise VisualEvidenceFusionError("fusion adapter returned an invalid document")
        return LocalAdapterResult(
            adapter_id=self.descriptor.adapter_id,
            adapter_version=self.descriptor.adapter_version,
            device=request.device,
            value=document,
            output_bytes=len(repr(document.to_wire()).encode("utf-8")),
            output_items=len(document.candidates) + len(document.groups),
        )


def execute_visual_evidence_fusion(
    adapter: VisualFusionAdapter,
    request: FusionRequest,
    *,
    runtime: LocalAdapterRuntime | None = None,
    device: LocalDeviceSpec | None = None,
    cancellation_probe: LocalCancellationProbe | None = None,
    clock: Callable[[], float] | None = None,
    memory_meter: Callable[[], int] | None = None,
) -> FusionDocument:
    if not isinstance(adapter, VisualFusionAdapter):
        raise VisualEvidenceFusionError("adapter must be VisualFusionAdapter")
    if not isinstance(request, FusionRequest):
        raise VisualEvidenceFusionError("request must be FusionRequest")
    device_value = LocalDeviceSpec(LocalDeviceKind.AUTO) if device is None else device
    if not isinstance(device_value, LocalDeviceSpec):
        raise VisualEvidenceFusionError("device must be LocalDeviceSpec")
    execution_request = LocalAdapterExecutionRequest(
        adapter_id=adapter.descriptor.adapter_id,
        task_mode=TaskMode.REF2VA,
        media_kinds=(MediaKind.VIDEO,),
        reference_count=len({candidate.source.asset_id for candidate in request.candidates}),
        device=device_value,
        estimated_memory_bytes=1,
        estimated_output_bytes=adapter.descriptor.limits.max_output_bytes,
        deterministic_required=True,
        seed=0,
        cancellation_required=cancellation_probe is not None,
        input_value=request,
    )
    bridge = _FusionBridge(adapter)
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
    if not isinstance(result.value, FusionDocument):
        raise VisualEvidenceFusionError("fusion result did not contain a document")
    if result.value.document_id != request.document_id:
        raise VisualEvidenceFusionError("fusion document ID does not match request")
    if len(result.value.candidates) > MAX_FUSION_CANDIDATES:
        raise VisualEvidenceFusionError("fusion result exceeds candidate limit")
    return result.value


__all__ = [
    "VISUAL_EVIDENCE_FUSION_SCHEMA",
    "VISUAL_FUSION_BENCHMARK_SCHEMA",
    "MAX_FUSION_CANDIDATES",
    "MAX_FUSION_GROUPS",
    "MAX_FUSION_RECEIPTS",
    "MAX_FUSION_UNCERTAINTIES",
    "MAX_FUSION_CASES",
    "MAX_FUSION_THRESHOLDS",
    "MAX_FUSION_OUTPUT_BYTES",
    "FusionStatus",
    "FusionRoute",
    "FusionCategory",
    "FusionSupport",
    "FusionResolution",
    "FusionCaseKind",
    "FusionSourceRef",
    "FusionCandidateReceipt",
    "FusionCandidate",
    "FusionGroup",
    "FusionDecision",
    "CalibrationBin",
    "CalibrationProfile",
    "calibrate_confidence",
    "FusionThreshold",
    "FusionBenchmarkCase",
    "FusionBenchmarkPlan",
    "build_default_visual_fusion_benchmark_plan",
    "FusionRequest",
    "FusionReceipt",
    "FusionEvaluationSample",
    "FusionEvaluation",
    "evaluate_fusion_benchmark",
    "FusionDocument",
    "fuse_visual_evidence",
    "build_visual_fusion_abstention",
    "VisualFusionAdapter",
    "execute_visual_evidence_fusion",
    "build_default_visual_fusion_calibration_profile",
]
