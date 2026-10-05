"""Bounded local perception/fusion/planner qualification contracts.

The module is pure: it does not discover models, open media, start ComfyUI/Ollama, contact a
network, or execute a provider. This release activates only its accepted manual-only plan and has
no public evidence issuer; declared observations cannot create an assisted product claim.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import cast

from .canonical import canonical_fingerprint

LOCAL_QUALIFICATION_SCHEMA = "h3.local.qualification.v1"
MAX_QUALIFICATION_CASES = 32
MAX_QUALIFICATION_CANDIDATES = 16
MAX_QUALIFICATION_LIMITATIONS = 32
MAX_QUALIFICATION_WIRE_BYTES = 65_536

_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}\Z")
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FORBIDDEN_PORTABLE_MARKERS = (
    "http://",
    "https://",
    "token=",
    "secret",
    "password",
    "authorization",
    "bearer ",
    "/mnt/",
    "\\\\",
    ":\\",
)


class LocalQualificationError(ValueError):
    """Raised when qualification input or evidence is unsafe or contradictory."""


class QualificationBackend(str, Enum):
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"


class CandidateTopology(str, Enum):
    MODULAR_ONLY = "modular_only"
    OMNI_ONLY = "omni_only"
    MODULAR_PLUS_PLANNER = "modular_plus_planner"


class AblationFactor(str, Enum):
    ADAPTER = "adapter"
    FUSION_RULE = "fusion_rule"
    PROMPT_STRATEGY = "prompt_strategy"
    ENRICHMENT_MODEL = "enrichment_model"
    PLANNER = "planner"
    BACKEND = "backend"


class AblationStatus(str, Enum):
    COMPLETE = "complete"
    MISSING = "missing"


class CandidateAvailability(str, Enum):
    ELIGIBLE = "eligible"
    UNAVAILABLE = "unavailable"


class ExecutionLevel(str, Enum):
    UNMOCKED_RAW_MEDIA = "unmocked_raw_media"
    BOUNDARY_MOCKED = "boundary_mocked"
    SYNTHETIC = "synthetic"


class CandidateDisposition(str, Enum):
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class ProductScopeDisposition(str, Enum):
    ASSISTED_PROFILE_QUALIFIED = "ASSISTED_PROFILE_QUALIFIED"
    MANUAL_ONLY_SCOPED = "MANUAL_ONLY_SCOPED"


def _exact_type(value: object, expected: type[object], field: str) -> None:
    if type(value) is not expected:
        raise LocalQualificationError(f"{field} must be exact {expected.__name__}")


def _code(value: object, field: str) -> str:
    if type(value) is not str or _CODE.fullmatch(value) is None:
        raise LocalQualificationError(f"{field} must be a bounded lower-case code")
    lowered = value.casefold()
    if any(marker in lowered for marker in _FORBIDDEN_PORTABLE_MARKERS):
        raise LocalQualificationError(f"{field} contains non-portable or sensitive material")
    return value


def _optional_code(value: object, field: str) -> str:
    if value == "":
        return ""
    return _code(value, field)


def _version(value: object, field: str) -> str:
    if type(value) is not str or _VERSION.fullmatch(value) is None:
        raise LocalQualificationError(f"{field} must be a numeric version")
    return value


def _sha(value: object, field: str) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise LocalQualificationError(f"{field} must be a canonical SHA-256 fingerprint")
    return value


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise LocalQualificationError(f"{field} must be a boolean")
    return value


def _integer(value: object, field: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise LocalQualificationError(f"{field} is outside its finite range")
    return value


def _codes(values: object, field: str, maximum: int) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > maximum:
        raise LocalQualificationError(f"{field} must be a bounded tuple")
    result = tuple(_code(value, f"{field} item") for value in values)
    if len(result) != len(set(result)):
        raise LocalQualificationError(f"{field} contains duplicate values")
    return result


@dataclass(frozen=True, slots=True)
class QualificationBudget:
    max_wall_time_ms: int
    max_peak_vram_mb: int
    max_peak_ram_mb: int
    max_failures: int
    max_runs: int

    def __post_init__(self) -> None:
        _integer(self.max_wall_time_ms, "budget max_wall_time_ms", minimum=1, maximum=3_600_000)
        _integer(self.max_peak_vram_mb, "budget max_peak_vram_mb", minimum=1, maximum=1_048_576)
        _integer(self.max_peak_ram_mb, "budget max_peak_ram_mb", minimum=1, maximum=1_048_576)
        _integer(self.max_failures, "budget max_failures", minimum=0, maximum=1_000)
        _integer(self.max_runs, "budget max_runs", minimum=1, maximum=10_000)

    def to_wire(self) -> dict[str, int]:
        return {
            "max_wall_time_ms": self.max_wall_time_ms,
            "max_peak_vram_mb": self.max_peak_vram_mb,
            "max_peak_ram_mb": self.max_peak_ram_mb,
            "max_failures": self.max_failures,
            "max_runs": self.max_runs,
        }


@dataclass(frozen=True, slots=True)
class QualificationThresholds:
    min_source_grounding_bp: int
    max_unsupported_fact_bp: int

    def __post_init__(self) -> None:
        _integer(
            self.min_source_grounding_bp,
            "threshold min_source_grounding_bp",
            minimum=0,
            maximum=10_000,
        )
        _integer(
            self.max_unsupported_fact_bp,
            "threshold max_unsupported_fact_bp",
            minimum=0,
            maximum=10_000,
        )

    def to_wire(self) -> dict[str, int]:
        return {
            "min_source_grounding_bp": self.min_source_grounding_bp,
            "max_unsupported_fact_bp": self.max_unsupported_fact_bp,
        }


@dataclass(frozen=True, slots=True)
class QualificationCase:
    case_id: str
    source_cluster_id: str
    task_mode: str
    media_fingerprint: str
    admission_receipt_fingerprint: str

    def __post_init__(self) -> None:
        _code(self.case_id, "case_id")
        _code(self.source_cluster_id, "source_cluster_id")
        if self.task_mode not in {"i2va", "fl2va", "ref2va"}:
            raise LocalQualificationError("qualification case task_mode is unsupported")
        _sha(self.media_fingerprint, "case media_fingerprint")
        _sha(self.admission_receipt_fingerprint, "case admission_receipt_fingerprint")

    def to_wire(self) -> dict[str, str]:
        return {
            "case_id": self.case_id,
            "source_cluster_id": self.source_cluster_id,
            "task_mode": self.task_mode,
            "media_fingerprint": self.media_fingerprint,
            "admission_receipt_fingerprint": self.admission_receipt_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class QualificationCandidate:
    candidate_id: str
    topology: CandidateTopology
    backend: QualificationBackend
    profile_id: str
    adapter_id: str
    fusion_rule_id: str
    prompt_strategy_id: str
    enrichment_model_id: str
    planner_id: str
    availability: CandidateAvailability
    unavailable_reason: str = ""

    def __post_init__(self) -> None:
        _code(self.candidate_id, "candidate_id")
        _exact_type(self.topology, CandidateTopology, "candidate topology")
        _exact_type(self.backend, QualificationBackend, "candidate backend")
        for value, field in (
            (self.profile_id, "candidate profile_id"),
            (self.adapter_id, "candidate adapter_id"),
            (self.fusion_rule_id, "candidate fusion_rule_id"),
            (self.prompt_strategy_id, "candidate prompt_strategy_id"),
            (self.enrichment_model_id, "candidate enrichment_model_id"),
            (self.planner_id, "candidate planner_id"),
        ):
            _code(value, field)
        _exact_type(self.availability, CandidateAvailability, "candidate availability")
        _optional_code(self.unavailable_reason, "candidate unavailable_reason")
        if self.availability is CandidateAvailability.UNAVAILABLE and not self.unavailable_reason:
            raise LocalQualificationError("unavailable candidate requires a reason")
        if self.availability is CandidateAvailability.ELIGIBLE and self.unavailable_reason:
            raise LocalQualificationError("eligible candidate cannot carry an unavailable reason")

    def to_wire(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "topology": self.topology.value,
            "backend": self.backend.value,
            "profile_id": self.profile_id,
            "adapter_id": self.adapter_id,
            "fusion_rule_id": self.fusion_rule_id,
            "prompt_strategy_id": self.prompt_strategy_id,
            "enrichment_model_id": self.enrichment_model_id,
            "planner_id": self.planner_id,
            "availability": self.availability.value,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True, slots=True)
class QualificationAblation:
    ablation_id: str
    factor: AblationFactor
    baseline_candidate_id: str
    variant_candidate_id: str

    def __post_init__(self) -> None:
        _code(self.ablation_id, "ablation_id")
        _exact_type(self.factor, AblationFactor, "ablation factor")
        _code(self.baseline_candidate_id, "ablation baseline_candidate_id")
        _code(self.variant_candidate_id, "ablation variant_candidate_id")
        if self.baseline_candidate_id == self.variant_candidate_id:
            raise LocalQualificationError("ablation candidates must be distinct")

    def to_wire(self) -> dict[str, str]:
        return {
            "ablation_id": self.ablation_id,
            "factor": self.factor.value,
            "baseline_candidate_id": self.baseline_candidate_id,
            "variant_candidate_id": self.variant_candidate_id,
        }


@dataclass(frozen=True, slots=True)
class QualificationPlan:
    plan_id: str
    cases: tuple[QualificationCase, ...]
    candidates: tuple[QualificationCandidate, ...]
    ablations: tuple[QualificationAblation, ...]
    seeds: tuple[int, ...]
    prompt_profile_id: str
    prompt_schema_version: str
    settings_fingerprint: str
    predecessor_evidence_fingerprints: tuple[str, ...]
    budget: QualificationBudget
    thresholds: QualificationThresholds
    privacy_mode: str
    license_scope: str
    schema: str = LOCAL_QUALIFICATION_SCHEMA

    def __post_init__(self) -> None:
        _code(self.plan_id, "plan_id")
        if type(self.cases) is not tuple or not 1 <= len(self.cases) <= MAX_QUALIFICATION_CASES:
            raise LocalQualificationError("plan cases must be a non-empty bounded tuple")
        if not all(type(item) is QualificationCase for item in self.cases):
            raise LocalQualificationError("plan cases contain a non-exact value")
        case_ids = tuple(item.case_id for item in self.cases)
        if len(case_ids) != len(set(case_ids)):
            raise LocalQualificationError("plan case IDs must be unique")
        if type(self.candidates) is not tuple or not 1 <= len(self.candidates) <= (
            MAX_QUALIFICATION_CANDIDATES
        ):
            raise LocalQualificationError("plan candidates must be a non-empty bounded tuple")
        if not all(type(item) is QualificationCandidate for item in self.candidates):
            raise LocalQualificationError("plan candidates contain a non-exact value")
        candidate_ids = tuple(item.candidate_id for item in self.candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise LocalQualificationError("plan candidate IDs must be unique")
        if {item.topology for item in self.candidates} != set(CandidateTopology):
            raise LocalQualificationError("plan must cover every candidate topology")
        if {item.backend for item in self.candidates} != set(QualificationBackend):
            raise LocalQualificationError("plan must cover every backend family")
        if type(self.ablations) is not tuple or not 1 <= len(self.ablations) <= 16:
            raise LocalQualificationError("plan ablations must be a non-empty bounded tuple")
        if not all(type(item) is QualificationAblation for item in self.ablations):
            raise LocalQualificationError("plan ablations contain a non-exact value")
        ablation_ids = tuple(item.ablation_id for item in self.ablations)
        if len(ablation_ids) != len(set(ablation_ids)):
            raise LocalQualificationError("plan ablation IDs must be unique")
        if {item.factor for item in self.ablations} != set(AblationFactor):
            raise LocalQualificationError("plan must isolate every required ablation factor")
        by_id = {item.candidate_id: item for item in self.candidates}
        factor_field = {
            AblationFactor.ADAPTER: "adapter_id",
            AblationFactor.FUSION_RULE: "fusion_rule_id",
            AblationFactor.PROMPT_STRATEGY: "prompt_strategy_id",
            AblationFactor.ENRICHMENT_MODEL: "enrichment_model_id",
            AblationFactor.PLANNER: "planner_id",
            AblationFactor.BACKEND: "backend",
        }
        compared_fields = (
            "topology",
            "backend",
            "profile_id",
            "adapter_id",
            "fusion_rule_id",
            "prompt_strategy_id",
            "enrichment_model_id",
            "planner_id",
        )
        for ablation in self.ablations:
            try:
                baseline = by_id[ablation.baseline_candidate_id]
                variant = by_id[ablation.variant_candidate_id]
            except KeyError as exc:
                raise LocalQualificationError("ablation references an unknown candidate") from exc
            changed = {
                field
                for field in compared_fields
                if getattr(baseline, field) != getattr(variant, field)
            }
            if changed != {factor_field[ablation.factor]}:
                raise LocalQualificationError("ablation must change exactly its declared factor")
        if type(self.seeds) is not tuple or not 1 <= len(self.seeds) <= 32:
            raise LocalQualificationError("plan seeds must be a non-empty bounded tuple")
        for seed in self.seeds:
            _integer(seed, "plan seed", minimum=0, maximum=2**31 - 1)
        if len(self.seeds) != len(set(self.seeds)):
            raise LocalQualificationError("plan seeds must be unique")
        _exact_type(self.budget, QualificationBudget, "plan budget")
        planned_runs = (
            sum(item.availability is CandidateAvailability.ELIGIBLE for item in self.candidates)
            * len(self.cases)
            * len(self.seeds)
        )
        if planned_runs > self.budget.max_runs:
            raise LocalQualificationError("eligible candidate search exceeds the run budget")
        _code(self.prompt_profile_id, "plan prompt_profile_id")
        _version(self.prompt_schema_version, "plan prompt_schema_version")
        _sha(self.settings_fingerprint, "plan settings_fingerprint")
        if (
            type(self.predecessor_evidence_fingerprints) is not tuple
            or not 1 <= len(self.predecessor_evidence_fingerprints) <= 16
        ):
            raise LocalQualificationError(
                "plan predecessor evidence must be a non-empty bounded tuple"
            )
        for fingerprint in self.predecessor_evidence_fingerprints:
            _sha(fingerprint, "plan predecessor evidence fingerprint")
        if len(self.predecessor_evidence_fingerprints) != len(
            set(self.predecessor_evidence_fingerprints)
        ):
            raise LocalQualificationError("plan predecessor evidence contains duplicates")
        _exact_type(self.thresholds, QualificationThresholds, "plan thresholds")
        if self.privacy_mode != "local_no_retention":
            raise LocalQualificationError("plan privacy_mode must be local_no_retention")
        if self.license_scope != "approved_local_evaluation":
            raise LocalQualificationError("plan license_scope is unsupported")
        if self.schema != LOCAL_QUALIFICATION_SCHEMA:
            raise LocalQualificationError("unsupported local qualification schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "cases": [item.to_wire() for item in sorted(self.cases, key=lambda item: item.case_id)],
            "candidates": [
                item.to_wire()
                for item in sorted(self.candidates, key=lambda item: item.candidate_id)
            ],
            "ablations": [
                item.to_wire() for item in sorted(self.ablations, key=lambda item: item.ablation_id)
            ],
            "seeds": sorted(self.seeds),
            "prompt_profile_id": self.prompt_profile_id,
            "prompt_schema_version": self.prompt_schema_version,
            "settings_fingerprint": self.settings_fingerprint,
            "predecessor_evidence_fingerprints": sorted(self.predecessor_evidence_fingerprints),
            "budget": self.budget.to_wire(),
            "thresholds": self.thresholds.to_wire(),
            "privacy_mode": self.privacy_mode,
            "license_scope": self.license_scope,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


@dataclass(frozen=True, slots=True)
class CandidateCaseEvidence:
    case_id: str
    media_fingerprint: str
    admission_receipt_fingerprint: str
    seed_inventory: tuple[int, ...]
    source_grounding_bp: int
    unsupported_fact_bp: int
    hard_constraints_preserved: bool
    latency_ms: int
    peak_vram_mb: int
    peak_ram_mb: int
    deterministic: bool
    validation_passed: bool
    prompt_fingerprint: str
    report_fingerprint: str
    failure_count: int = 0

    def __post_init__(self) -> None:
        _code(self.case_id, "case evidence case_id")
        _sha(self.media_fingerprint, "case evidence media_fingerprint")
        _sha(
            self.admission_receipt_fingerprint,
            "case evidence admission_receipt_fingerprint",
        )
        if type(self.seed_inventory) is not tuple or not 1 <= len(self.seed_inventory) <= 32:
            raise LocalQualificationError(
                "case evidence seed_inventory must be a non-empty bounded tuple"
            )
        for seed in self.seed_inventory:
            _integer(seed, "case evidence seed", minimum=0, maximum=2**31 - 1)
        if len(self.seed_inventory) != len(set(self.seed_inventory)):
            raise LocalQualificationError("case evidence seed_inventory contains duplicates")
        _integer(
            self.source_grounding_bp,
            "case evidence source_grounding_bp",
            minimum=0,
            maximum=10_000,
        )
        _integer(
            self.unsupported_fact_bp,
            "case evidence unsupported_fact_bp",
            minimum=0,
            maximum=10_000,
        )
        _bool(self.hard_constraints_preserved, "case evidence hard_constraints_preserved")
        _integer(self.latency_ms, "case evidence latency_ms", minimum=0, maximum=3_600_000)
        _integer(self.peak_vram_mb, "case evidence peak_vram_mb", minimum=0, maximum=1_048_576)
        _integer(self.peak_ram_mb, "case evidence peak_ram_mb", minimum=0, maximum=1_048_576)
        _bool(self.deterministic, "case evidence deterministic")
        _bool(self.validation_passed, "case evidence validation_passed")
        _sha(self.prompt_fingerprint, "case evidence prompt_fingerprint")
        _sha(self.report_fingerprint, "case evidence report_fingerprint")
        _integer(self.failure_count, "case evidence failure_count", minimum=0, maximum=1_000)

    def to_wire(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "media_fingerprint": self.media_fingerprint,
            "admission_receipt_fingerprint": self.admission_receipt_fingerprint,
            "seed_inventory": sorted(self.seed_inventory),
            "source_grounding_bp": self.source_grounding_bp,
            "unsupported_fact_bp": self.unsupported_fact_bp,
            "hard_constraints_preserved": self.hard_constraints_preserved,
            "latency_ms": self.latency_ms,
            "peak_vram_mb": self.peak_vram_mb,
            "peak_ram_mb": self.peak_ram_mb,
            "deterministic": self.deterministic,
            "validation_passed": self.validation_passed,
            "prompt_fingerprint": self.prompt_fingerprint,
            "report_fingerprint": self.report_fingerprint,
            "failure_count": self.failure_count,
        }


@dataclass(frozen=True, eq=False)
class QualificationCandidateEvidence:
    candidate_id: str
    plan_fingerprint: str
    backend: QualificationBackend
    topology: CandidateTopology
    profile_id: str
    execution_level: ExecutionLevel
    settings_fingerprint: str
    cases: tuple[CandidateCaseEvidence, ...]
    fallback_used: bool
    cleanup_verified: bool
    private_content_retained: bool
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _code(self.candidate_id, "candidate evidence candidate_id")
        _sha(self.plan_fingerprint, "candidate evidence plan_fingerprint")
        _exact_type(self.backend, QualificationBackend, "candidate evidence backend")
        _exact_type(self.topology, CandidateTopology, "candidate evidence topology")
        _code(self.profile_id, "candidate evidence profile_id")
        _exact_type(self.execution_level, ExecutionLevel, "candidate evidence execution_level")
        _sha(self.settings_fingerprint, "candidate evidence settings_fingerprint")
        if type(self.cases) is not tuple or len(self.cases) > MAX_QUALIFICATION_CASES:
            raise LocalQualificationError("candidate evidence cases must be a bounded tuple")
        if not all(type(item) is CandidateCaseEvidence for item in self.cases):
            raise LocalQualificationError("candidate evidence cases contain a non-exact value")
        case_ids = tuple(item.case_id for item in self.cases)
        if len(case_ids) != len(set(case_ids)):
            raise LocalQualificationError("candidate evidence case IDs must be unique")
        _bool(self.fallback_used, "candidate evidence fallback_used")
        _bool(self.cleanup_verified, "candidate evidence cleanup_verified")
        _bool(self.private_content_retained, "candidate evidence private_content_retained")
        _codes(self.limitations, "candidate evidence limitations", MAX_QUALIFICATION_LIMITATIONS)

    def to_wire(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "plan_fingerprint": self.plan_fingerprint,
            "backend": self.backend.value,
            "topology": self.topology.value,
            "profile_id": self.profile_id,
            "execution_level": self.execution_level.value,
            "settings_fingerprint": self.settings_fingerprint,
            "cases": [item.to_wire() for item in sorted(self.cases, key=lambda item: item.case_id)],
            "fallback_used": self.fallback_used,
            "cleanup_verified": self.cleanup_verified,
            "private_content_retained": self.private_content_retained,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class QualificationCandidateReceipt:
    candidate_id: str
    backend: QualificationBackend
    topology: CandidateTopology
    profile_id: str
    disposition: CandidateDisposition
    failed_checks: tuple[str, ...]
    limitations: tuple[str, ...]
    evaluated_case_count: int
    mean_source_grounding_bp: int | None
    max_unsupported_fact_bp: int | None
    max_latency_ms: int | None
    max_peak_vram_mb: int | None
    max_peak_ram_mb: int | None
    total_failures: int | None

    def __post_init__(self) -> None:
        _code(self.candidate_id, "receipt candidate_id")
        _exact_type(self.backend, QualificationBackend, "receipt backend")
        _exact_type(self.topology, CandidateTopology, "receipt topology")
        _code(self.profile_id, "receipt profile_id")
        _exact_type(self.disposition, CandidateDisposition, "receipt disposition")
        _codes(self.failed_checks, "receipt failed_checks", MAX_QUALIFICATION_LIMITATIONS)
        _codes(self.limitations, "receipt limitations", MAX_QUALIFICATION_LIMITATIONS)
        _integer(
            self.evaluated_case_count,
            "receipt evaluated_case_count",
            minimum=0,
            maximum=MAX_QUALIFICATION_CASES,
        )
        for value, field, maximum in (
            (self.mean_source_grounding_bp, "receipt mean_source_grounding_bp", 10_000),
            (self.max_unsupported_fact_bp, "receipt max_unsupported_fact_bp", 10_000),
            (self.max_latency_ms, "receipt max_latency_ms", 3_600_000),
            (self.max_peak_vram_mb, "receipt max_peak_vram_mb", 1_048_576),
            (self.max_peak_ram_mb, "receipt max_peak_ram_mb", 1_048_576),
            (self.total_failures, "receipt total_failures", 1_000),
        ):
            if value is not None:
                _integer(value, field, minimum=0, maximum=maximum)
        metrics = (
            self.mean_source_grounding_bp,
            self.max_unsupported_fact_bp,
            self.max_latency_ms,
            self.max_peak_vram_mb,
            self.max_peak_ram_mb,
            self.total_failures,
        )
        if self.evaluated_case_count == 0 and any(value is not None for value in metrics):
            raise LocalQualificationError("receipt without cases cannot carry aggregate metrics")
        if self.disposition is CandidateDisposition.QUALIFIED and (
            self.failed_checks or any(value is None for value in metrics)
        ):
            raise LocalQualificationError("qualified receipt must be complete and failure-free")
        if self.disposition is CandidateDisposition.UNAVAILABLE and (
            self.evaluated_case_count != 0
            or any(value is not None for value in metrics)
            or self.failed_checks
            or not self.limitations
        ):
            raise LocalQualificationError(
                "unavailable receipt must be unevaluated, metric-free, and explained"
            )

    @property
    def executable(self) -> bool:
        return self.disposition is CandidateDisposition.QUALIFIED

    def to_wire(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "backend": self.backend.value,
            "topology": self.topology.value,
            "profile_id": self.profile_id,
            "disposition": self.disposition.value,
            "failed_checks": list(self.failed_checks),
            "limitations": list(self.limitations),
            "evaluated_case_count": self.evaluated_case_count,
            "mean_source_grounding_bp": self.mean_source_grounding_bp,
            "max_unsupported_fact_bp": self.max_unsupported_fact_bp,
            "max_latency_ms": self.max_latency_ms,
            "max_peak_vram_mb": self.max_peak_vram_mb,
            "max_peak_ram_mb": self.max_peak_ram_mb,
            "total_failures": self.total_failures,
            "executable": self.executable,
        }


@dataclass(frozen=True, slots=True)
class BackendQualificationSummary:
    backend: QualificationBackend
    candidate_ids: tuple[str, ...]
    qualified_candidate_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _exact_type(self.backend, QualificationBackend, "backend summary backend")
        _codes(self.candidate_ids, "backend summary candidate_ids", MAX_QUALIFICATION_CANDIDATES)
        _codes(
            self.qualified_candidate_ids,
            "backend summary qualified_candidate_ids",
            MAX_QUALIFICATION_CANDIDATES,
        )
        if not set(self.qualified_candidate_ids) <= set(self.candidate_ids):
            raise LocalQualificationError("backend summary qualified IDs are not candidates")

    def to_wire(self) -> dict[str, object]:
        return {
            "backend": self.backend.value,
            "candidate_ids": list(self.candidate_ids),
            "qualified_candidate_ids": list(self.qualified_candidate_ids),
        }


@dataclass(frozen=True, slots=True)
class QualificationAblationReceipt:
    ablation_id: str
    factor: AblationFactor
    baseline_candidate_id: str
    variant_candidate_id: str
    status: AblationStatus
    source_grounding_delta_bp: int | None
    unsupported_fact_delta_bp: int | None
    latency_delta_ms: int | None
    peak_vram_delta_mb: int | None
    peak_ram_delta_mb: int | None
    failure_delta: int | None
    limitation: str = ""

    def __post_init__(self) -> None:
        _code(self.ablation_id, "ablation receipt ablation_id")
        _exact_type(self.factor, AblationFactor, "ablation receipt factor")
        _code(self.baseline_candidate_id, "ablation receipt baseline_candidate_id")
        _code(self.variant_candidate_id, "ablation receipt variant_candidate_id")
        _exact_type(self.status, AblationStatus, "ablation receipt status")
        values = (
            self.source_grounding_delta_bp,
            self.unsupported_fact_delta_bp,
            self.latency_delta_ms,
            self.peak_vram_delta_mb,
            self.peak_ram_delta_mb,
            self.failure_delta,
        )
        for value in values:
            if value is not None:
                _integer(value, "ablation receipt delta", minimum=-1_048_576, maximum=1_048_576)
        _optional_code(self.limitation, "ablation receipt limitation")
        if self.status is AblationStatus.COMPLETE and (
            any(value is None for value in values) or self.limitation
        ):
            raise LocalQualificationError("complete ablation receipt must contain every delta")
        if self.status is AblationStatus.MISSING and (
            any(value is not None for value in values) or not self.limitation
        ):
            raise LocalQualificationError("missing ablation receipt requires one limitation")

    def to_wire(self) -> dict[str, object]:
        return {
            "ablation_id": self.ablation_id,
            "factor": self.factor.value,
            "baseline_candidate_id": self.baseline_candidate_id,
            "variant_candidate_id": self.variant_candidate_id,
            "status": self.status.value,
            "source_grounding_delta_bp": self.source_grounding_delta_bp,
            "unsupported_fact_delta_bp": self.unsupported_fact_delta_bp,
            "latency_delta_ms": self.latency_delta_ms,
            "peak_vram_delta_mb": self.peak_vram_delta_mb,
            "peak_ram_delta_mb": self.peak_ram_delta_mb,
            "failure_delta": self.failure_delta,
            "limitation": self.limitation,
        }


@dataclass(frozen=True, slots=True)
class LocalQualificationReport:
    plan_id: str
    plan_fingerprint: str
    product_scope: ProductScopeDisposition
    candidate_receipts: tuple[QualificationCandidateReceipt, ...]
    backend_summaries: tuple[BackendQualificationSummary, ...]
    ablation_receipts: tuple[QualificationAblationReceipt, ...]
    qualified_candidate_ids: tuple[str, ...]
    pareto_candidate_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    schema: str = LOCAL_QUALIFICATION_SCHEMA

    def __post_init__(self) -> None:
        _code(self.plan_id, "report plan_id")
        _sha(self.plan_fingerprint, "report plan_fingerprint")
        _exact_type(self.product_scope, ProductScopeDisposition, "report product_scope")
        if type(self.candidate_receipts) is not tuple or not self.candidate_receipts:
            raise LocalQualificationError("report candidate receipts must be non-empty")
        if not all(type(item) is QualificationCandidateReceipt for item in self.candidate_receipts):
            raise LocalQualificationError("report candidate receipts contain a non-exact value")
        receipt_ids = tuple(item.candidate_id for item in self.candidate_receipts)
        if len(receipt_ids) != len(set(receipt_ids)):
            raise LocalQualificationError("report candidate receipt IDs must be unique")
        if type(self.backend_summaries) is not tuple or len(self.backend_summaries) != len(
            QualificationBackend
        ):
            raise LocalQualificationError("report backend summaries must cover every backend")
        if {item.backend for item in self.backend_summaries} != set(QualificationBackend):
            raise LocalQualificationError("report backend summaries are incomplete")
        if type(self.ablation_receipts) is not tuple or not self.ablation_receipts:
            raise LocalQualificationError("report ablation receipts must be non-empty")
        if not all(type(item) is QualificationAblationReceipt for item in self.ablation_receipts):
            raise LocalQualificationError("report ablation receipts contain a non-exact value")
        ablation_ids = tuple(item.ablation_id for item in self.ablation_receipts)
        if len(ablation_ids) != len(set(ablation_ids)):
            raise LocalQualificationError("report ablation receipt IDs must be unique")
        if {item.factor for item in self.ablation_receipts} != set(AblationFactor):
            raise LocalQualificationError("report ablation factor inventory is incomplete")
        _codes(
            self.qualified_candidate_ids,
            "report qualified_candidate_ids",
            MAX_QUALIFICATION_CANDIDATES,
        )
        _codes(
            self.pareto_candidate_ids,
            "report pareto_candidate_ids",
            MAX_QUALIFICATION_CANDIDATES,
        )
        _codes(self.limitations, "report limitations", MAX_QUALIFICATION_LIMITATIONS)
        executable = tuple(
            sorted(item.candidate_id for item in self.candidate_receipts if item.executable)
        )
        if tuple(sorted(self.qualified_candidate_ids)) != executable:
            raise LocalQualificationError("report qualified inventory does not match receipts")
        if not set(self.pareto_candidate_ids) <= set(self.qualified_candidate_ids):
            raise LocalQualificationError("Pareto inventory contains an unqualified candidate")
        expected_limitations = tuple(
            sorted(
                {
                    limitation
                    for receipt in self.candidate_receipts
                    for limitation in receipt.limitations
                }
            )
        )
        if tuple(sorted(self.limitations)) != expected_limitations:
            raise LocalQualificationError("report limitations do not match candidate receipts")
        expected_scope = (
            ProductScopeDisposition.ASSISTED_PROFILE_QUALIFIED
            if executable
            else ProductScopeDisposition.MANUAL_ONLY_SCOPED
        )
        if self.product_scope is not expected_scope:
            raise LocalQualificationError("product scope contradicts candidate receipts")
        # CRITICAL: no raw-media runtime owner is activated in this release; declarations alone
        # must never turn a local Python object into an assisted product claim.
        if executable:
            raise LocalQualificationError(
                "no assisted qualification profile is activated in this release"
            )
        if self.schema != LOCAL_QUALIFICATION_SCHEMA:
            raise LocalQualificationError("unsupported local qualification report schema")
        if len(self.to_wire_bytes()) > MAX_QUALIFICATION_WIRE_BYTES:
            raise LocalQualificationError("local qualification report exceeds its portable bound")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "plan_fingerprint": self.plan_fingerprint,
            "product_scope": self.product_scope.value,
            "candidate_receipts": [
                item.to_wire()
                for item in sorted(self.candidate_receipts, key=lambda item: item.candidate_id)
            ],
            "backend_summaries": [
                item.to_wire()
                for item in sorted(self.backend_summaries, key=lambda item: item.backend.value)
            ],
            "ablation_receipts": [
                item.to_wire()
                for item in sorted(self.ablation_receipts, key=lambda item: item.ablation_id)
            ],
            "qualified_candidate_ids": list(sorted(self.qualified_candidate_ids)),
            "pareto_candidate_ids": list(sorted(self.pareto_candidate_ids)),
            "limitations": list(self.limitations),
        }

    def to_wire_bytes(self) -> bytes:
        return json.dumps(
            self.to_wire(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


def validate_local_qualification_wire(
    value: object,
) -> LocalQualificationReport:
    """Validate one portable report against the activated plan and Python invariants."""

    def exact_object(item: object, keys: set[str], field: str) -> dict[str, object]:
        if type(item) is not dict or set(item) != keys:
            raise LocalQualificationError(f"{field} must be a closed object")
        return cast(dict[str, object], item)

    def bounded_list(item: object, field: str, maximum: int) -> list[object]:
        if type(item) is not list or len(item) > maximum:
            raise LocalQualificationError(f"{field} must be a bounded list")
        return cast(list[object], item)

    def code_tuple(item: object, field: str, maximum: int) -> tuple[str, ...]:
        return _codes(tuple(bounded_list(item, field, maximum)), field, maximum)

    def nullable_integer(item: object, field: str, maximum: int) -> int | None:
        if item is None:
            return None
        return _integer(item, field, minimum=0, maximum=maximum)

    def signed_nullable_integer(item: object, field: str) -> int | None:
        if item is None:
            return None
        return _integer(item, field, minimum=-1_048_576, maximum=1_048_576)

    def enum_value(item: object, enum: type[Enum], field: str) -> Enum:
        if type(item) is not str:
            raise LocalQualificationError(f"{field} must be an enum string")
        try:
            return enum(item)
        except ValueError as exc:
            raise LocalQualificationError(f"{field} is unsupported") from exc

    root_keys = {
        "schema",
        "plan_id",
        "plan_fingerprint",
        "product_scope",
        "candidate_receipts",
        "backend_summaries",
        "ablation_receipts",
        "qualified_candidate_ids",
        "pareto_candidate_ids",
        "limitations",
    }
    root = exact_object(value, root_keys, "local qualification wire")
    try:
        encoded = json.dumps(root, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise LocalQualificationError("local qualification wire is not canonical JSON") from exc
    if len(encoded) > MAX_QUALIFICATION_WIRE_BYTES:
        raise LocalQualificationError("local qualification wire exceeds its portable bound")
    if root["schema"] != LOCAL_QUALIFICATION_SCHEMA:
        raise LocalQualificationError("unsupported local qualification wire schema")

    receipt_keys = {
        "candidate_id",
        "backend",
        "topology",
        "profile_id",
        "disposition",
        "failed_checks",
        "limitations",
        "evaluated_case_count",
        "mean_source_grounding_bp",
        "max_unsupported_fact_bp",
        "max_latency_ms",
        "max_peak_vram_mb",
        "max_peak_ram_mb",
        "total_failures",
        "executable",
    }
    receipts: list[QualificationCandidateReceipt] = []
    for index, raw_receipt in enumerate(
        bounded_list(root["candidate_receipts"], "candidate_receipts", MAX_QUALIFICATION_CANDIDATES)
    ):
        item = exact_object(raw_receipt, receipt_keys, f"candidate_receipts[{index}]")
        disposition = cast(
            CandidateDisposition,
            enum_value(item["disposition"], CandidateDisposition, "receipt disposition"),
        )
        executable = _bool(item["executable"], "receipt executable")
        if executable is not (disposition is CandidateDisposition.QUALIFIED):
            raise LocalQualificationError("receipt executable contradicts disposition")
        receipts.append(
            QualificationCandidateReceipt(
                candidate_id=_code(item["candidate_id"], "receipt candidate_id"),
                backend=cast(
                    QualificationBackend,
                    enum_value(item["backend"], QualificationBackend, "receipt backend"),
                ),
                topology=cast(
                    CandidateTopology,
                    enum_value(item["topology"], CandidateTopology, "receipt topology"),
                ),
                profile_id=_code(item["profile_id"], "receipt profile_id"),
                disposition=disposition,
                failed_checks=code_tuple(
                    item["failed_checks"], "receipt failed_checks", MAX_QUALIFICATION_LIMITATIONS
                ),
                limitations=code_tuple(
                    item["limitations"], "receipt limitations", MAX_QUALIFICATION_LIMITATIONS
                ),
                evaluated_case_count=_integer(
                    item["evaluated_case_count"],
                    "receipt evaluated_case_count",
                    minimum=0,
                    maximum=MAX_QUALIFICATION_CASES,
                ),
                mean_source_grounding_bp=nullable_integer(
                    item["mean_source_grounding_bp"], "receipt mean_source_grounding_bp", 10_000
                ),
                max_unsupported_fact_bp=nullable_integer(
                    item["max_unsupported_fact_bp"], "receipt max_unsupported_fact_bp", 10_000
                ),
                max_latency_ms=nullable_integer(
                    item["max_latency_ms"], "receipt max_latency_ms", 3_600_000
                ),
                max_peak_vram_mb=nullable_integer(
                    item["max_peak_vram_mb"], "receipt max_peak_vram_mb", 1_048_576
                ),
                max_peak_ram_mb=nullable_integer(
                    item["max_peak_ram_mb"], "receipt max_peak_ram_mb", 1_048_576
                ),
                total_failures=nullable_integer(
                    item["total_failures"], "receipt total_failures", 1_000
                ),
            )
        )

    backend_keys = {"backend", "candidate_ids", "qualified_candidate_ids"}
    summaries: list[BackendQualificationSummary] = []
    for index, raw_summary in enumerate(
        bounded_list(root["backend_summaries"], "backend_summaries", len(QualificationBackend))
    ):
        item = exact_object(raw_summary, backend_keys, f"backend_summaries[{index}]")
        summaries.append(
            BackendQualificationSummary(
                backend=cast(
                    QualificationBackend,
                    enum_value(item["backend"], QualificationBackend, "summary backend"),
                ),
                candidate_ids=code_tuple(
                    item["candidate_ids"], "summary candidate_ids", MAX_QUALIFICATION_CANDIDATES
                ),
                qualified_candidate_ids=code_tuple(
                    item["qualified_candidate_ids"],
                    "summary qualified_candidate_ids",
                    MAX_QUALIFICATION_CANDIDATES,
                ),
            )
        )

    ablation_keys = {
        "ablation_id",
        "factor",
        "baseline_candidate_id",
        "variant_candidate_id",
        "status",
        "source_grounding_delta_bp",
        "unsupported_fact_delta_bp",
        "latency_delta_ms",
        "peak_vram_delta_mb",
        "peak_ram_delta_mb",
        "failure_delta",
        "limitation",
    }
    ablations: list[QualificationAblationReceipt] = []
    for index, raw_ablation in enumerate(
        bounded_list(root["ablation_receipts"], "ablation_receipts", 16)
    ):
        item = exact_object(raw_ablation, ablation_keys, f"ablation_receipts[{index}]")

        ablations.append(
            QualificationAblationReceipt(
                ablation_id=_code(item["ablation_id"], "ablation id"),
                factor=cast(
                    AblationFactor,
                    enum_value(item["factor"], AblationFactor, "ablation factor"),
                ),
                baseline_candidate_id=_code(
                    item["baseline_candidate_id"], "ablation baseline candidate"
                ),
                variant_candidate_id=_code(
                    item["variant_candidate_id"], "ablation variant candidate"
                ),
                status=cast(
                    AblationStatus,
                    enum_value(item["status"], AblationStatus, "ablation status"),
                ),
                source_grounding_delta_bp=signed_nullable_integer(
                    item["source_grounding_delta_bp"], "ablation source_grounding_delta_bp"
                ),
                unsupported_fact_delta_bp=signed_nullable_integer(
                    item["unsupported_fact_delta_bp"], "ablation unsupported_fact_delta_bp"
                ),
                latency_delta_ms=signed_nullable_integer(
                    item["latency_delta_ms"], "ablation latency_delta_ms"
                ),
                peak_vram_delta_mb=signed_nullable_integer(
                    item["peak_vram_delta_mb"], "ablation peak_vram_delta_mb"
                ),
                peak_ram_delta_mb=signed_nullable_integer(
                    item["peak_ram_delta_mb"], "ablation peak_ram_delta_mb"
                ),
                failure_delta=signed_nullable_integer(
                    item["failure_delta"], "ablation failure_delta"
                ),
                limitation=_optional_code(item["limitation"], "ablation limitation"),
            )
        )

    report = LocalQualificationReport(
        plan_id=_code(root["plan_id"], "report plan_id"),
        plan_fingerprint=_sha(root["plan_fingerprint"], "report plan_fingerprint"),
        product_scope=cast(
            ProductScopeDisposition,
            enum_value(root["product_scope"], ProductScopeDisposition, "report product_scope"),
        ),
        candidate_receipts=tuple(receipts),
        backend_summaries=tuple(summaries),
        ablation_receipts=tuple(ablations),
        qualified_candidate_ids=code_tuple(
            root["qualified_candidate_ids"],
            "report qualified_candidate_ids",
            MAX_QUALIFICATION_CANDIDATES,
        ),
        pareto_candidate_ids=code_tuple(
            root["pareto_candidate_ids"],
            "report pareto_candidate_ids",
            MAX_QUALIFICATION_CANDIDATES,
        ),
        limitations=code_tuple(
            root["limitations"], "report limitations", MAX_QUALIFICATION_LIMITATIONS
        ),
    )

    plan = build_default_local_qualification_plan()
    _exact_type(plan, QualificationPlan, "expected qualification plan")
    if report.plan_id != plan.plan_id or report.plan_fingerprint != plan.fingerprint:
        raise LocalQualificationError("qualification wire does not match the activated plan")
    candidates = {candidate.candidate_id: candidate for candidate in plan.candidates}
    receipts_by_id = {receipt.candidate_id: receipt for receipt in report.candidate_receipts}
    if set(receipts_by_id) != set(candidates):
        raise LocalQualificationError("qualification wire candidate inventory is not exact")
    for candidate_id, candidate in candidates.items():
        receipt = receipts_by_id[candidate_id]
        if (
            receipt.backend is not candidate.backend
            or receipt.topology is not candidate.topology
            or receipt.profile_id != candidate.profile_id
        ):
            raise LocalQualificationError("qualification wire candidate identity drifted")
        expected_receipt = _receipt(plan, candidate, None)
        if receipt != expected_receipt:
            raise LocalQualificationError(
                "qualification wire candidate receipt is not plan-derived"
            )
    summaries_by_backend = {summary.backend: summary for summary in report.backend_summaries}
    for backend in QualificationBackend:
        summary = summaries_by_backend[backend]
        expected_ids = tuple(
            sorted(
                receipt.candidate_id
                for receipt in report.candidate_receipts
                if receipt.backend is backend
            )
        )
        expected_qualified = tuple(
            sorted(
                receipt.candidate_id
                for receipt in report.candidate_receipts
                if receipt.backend is backend and receipt.executable
            )
        )
        if (
            tuple(sorted(summary.candidate_ids)) != expected_ids
            or tuple(sorted(summary.qualified_candidate_ids)) != expected_qualified
        ):
            raise LocalQualificationError("qualification wire backend inventory is not exact")
    plan_ablations = {ablation.ablation_id: ablation for ablation in plan.ablations}
    wire_ablations = {ablation.ablation_id: ablation for ablation in report.ablation_receipts}
    if set(plan_ablations) != set(wire_ablations):
        raise LocalQualificationError("qualification wire ablation inventory is not exact")
    for ablation_id, planned in plan_ablations.items():
        observed = wire_ablations[ablation_id]
        if (
            observed.factor is not planned.factor
            or observed.baseline_candidate_id != planned.baseline_candidate_id
            or observed.variant_candidate_id != planned.variant_candidate_id
        ):
            raise LocalQualificationError("qualification wire ablation identity drifted")
        expected_ablation = _ablation_receipt(planned, receipts_by_id)
        if observed != expected_ablation:
            raise LocalQualificationError(
                "qualification wire ablation receipt is not evaluator-derived"
            )
    return report


def _aggregate(
    cases: tuple[CandidateCaseEvidence, ...],
) -> tuple[int, int, int, int, int, int] | None:
    if not cases:
        return None
    return (
        sum(item.source_grounding_bp for item in cases) // len(cases),
        max(item.unsupported_fact_bp for item in cases),
        max(item.latency_ms for item in cases),
        max(item.peak_vram_mb for item in cases),
        max(item.peak_ram_mb for item in cases),
        sum(item.failure_count for item in cases),
    )


def _receipt(
    plan: QualificationPlan,
    candidate: QualificationCandidate,
    evidence: QualificationCandidateEvidence | None,
) -> QualificationCandidateReceipt:
    if candidate.availability is CandidateAvailability.UNAVAILABLE:
        if evidence is not None:
            raise LocalQualificationError("unavailable candidate cannot carry runtime evidence")
        return QualificationCandidateReceipt(
            candidate_id=candidate.candidate_id,
            backend=candidate.backend,
            topology=candidate.topology,
            profile_id=candidate.profile_id,
            disposition=CandidateDisposition.UNAVAILABLE,
            failed_checks=(),
            limitations=(candidate.unavailable_reason,),
            evaluated_case_count=0,
            mean_source_grounding_bp=None,
            max_unsupported_fact_bp=None,
            max_latency_ms=None,
            max_peak_vram_mb=None,
            max_peak_ram_mb=None,
            total_failures=None,
        )
    if evidence is None:
        return QualificationCandidateReceipt(
            candidate_id=candidate.candidate_id,
            backend=candidate.backend,
            topology=candidate.topology,
            profile_id=candidate.profile_id,
            disposition=CandidateDisposition.REJECTED,
            failed_checks=("runtime_evidence_missing",),
            limitations=(),
            evaluated_case_count=0,
            mean_source_grounding_bp=None,
            max_unsupported_fact_bp=None,
            max_latency_ms=None,
            max_peak_vram_mb=None,
            max_peak_ram_mb=None,
            total_failures=None,
        )
    if evidence.plan_fingerprint != plan.fingerprint:
        raise LocalQualificationError("candidate evidence belongs to another plan")
    if evidence.settings_fingerprint != plan.settings_fingerprint:
        raise LocalQualificationError("candidate evidence settings do not match the plan")
    if (
        evidence.backend is not candidate.backend
        or evidence.topology is not candidate.topology
        or evidence.profile_id != candidate.profile_id
    ):
        raise LocalQualificationError("candidate evidence identity does not match the plan")
    expected_cases = {item.case_id: item for item in plan.cases}
    observed_cases = {item.case_id: item for item in evidence.cases}
    failures: set[str] = set()
    if set(observed_cases) != set(expected_cases):
        failures.add("case_inventory_mismatch")
    for case_id in set(observed_cases) & set(expected_cases):
        if observed_cases[case_id].media_fingerprint != expected_cases[case_id].media_fingerprint:
            raise LocalQualificationError("candidate evidence media does not match the frozen case")
        if observed_cases[case_id].admission_receipt_fingerprint != (
            expected_cases[case_id].admission_receipt_fingerprint
        ):
            raise LocalQualificationError(
                "candidate evidence admission receipt does not match the frozen case"
            )
    if evidence.execution_level is not ExecutionLevel.UNMOCKED_RAW_MEDIA:
        failures.add("unmocked_raw_media_required")
    if evidence.fallback_used:
        failures.add("fallback_forbidden")
    if not evidence.cleanup_verified:
        failures.add("cleanup_not_verified")
    if evidence.private_content_retained:
        failures.add("private_content_retained")
    for case in evidence.cases:
        if tuple(sorted(case.seed_inventory)) != tuple(sorted(plan.seeds)):
            failures.add("seed_inventory_mismatch")
        if case.source_grounding_bp < plan.thresholds.min_source_grounding_bp:
            failures.add("source_grounding_below_minimum")
        if case.unsupported_fact_bp > plan.thresholds.max_unsupported_fact_bp:
            failures.add("unsupported_fact_above_maximum")
        if not case.hard_constraints_preserved:
            failures.add("hard_constraint_mutation")
        if case.latency_ms > plan.budget.max_wall_time_ms:
            failures.add("latency_limit_exceeded")
        if case.peak_vram_mb > plan.budget.max_peak_vram_mb:
            failures.add("vram_limit_exceeded")
        if case.peak_ram_mb > plan.budget.max_peak_ram_mb:
            failures.add("ram_limit_exceeded")
        if not case.deterministic:
            failures.add("determinism_failed")
        if not case.validation_passed:
            failures.add("validation_not_passed")
    aggregate = _aggregate(evidence.cases)
    if aggregate is not None and aggregate[-1] > plan.budget.max_failures:
        failures.add("failure_budget_exceeded")
    disposition = CandidateDisposition.REJECTED if failures else CandidateDisposition.QUALIFIED
    metrics: tuple[int | None, ...] = (
        (None, None, None, None, None, None) if aggregate is None else aggregate
    )
    return QualificationCandidateReceipt(
        candidate_id=candidate.candidate_id,
        backend=candidate.backend,
        topology=candidate.topology,
        profile_id=candidate.profile_id,
        disposition=disposition,
        failed_checks=tuple(sorted(failures)),
        limitations=evidence.limitations,
        evaluated_case_count=len(evidence.cases),
        mean_source_grounding_bp=metrics[0],
        max_unsupported_fact_bp=metrics[1],
        max_latency_ms=metrics[2],
        max_peak_vram_mb=metrics[3],
        max_peak_ram_mb=metrics[4],
        total_failures=metrics[5],
    )


def _dominates(left: QualificationCandidateReceipt, right: QualificationCandidateReceipt) -> bool:
    left_values = (
        left.mean_source_grounding_bp,
        left.max_unsupported_fact_bp,
        left.max_latency_ms,
        left.max_peak_vram_mb,
        left.max_peak_ram_mb,
        left.total_failures,
    )
    right_values = (
        right.mean_source_grounding_bp,
        right.max_unsupported_fact_bp,
        right.max_latency_ms,
        right.max_peak_vram_mb,
        right.max_peak_ram_mb,
        right.total_failures,
    )
    if any(value is None for value in (*left_values, *right_values)):
        return False
    left_grounding = cast(int, left_values[0])
    right_grounding = cast(int, right_values[0])
    left_costs = tuple(cast(int, value) for value in left_values[1:])
    right_costs = tuple(cast(int, value) for value in right_values[1:])
    no_worse = left_grounding >= right_grounding and all(
        left_value <= right_value
        for left_value, right_value in zip(left_costs, right_costs, strict=True)
    )
    strictly_better = left_grounding > right_grounding or any(
        left_value < right_value
        for left_value, right_value in zip(left_costs, right_costs, strict=True)
    )
    return no_worse and strictly_better


def _ablation_receipt(
    ablation: QualificationAblation,
    receipts: dict[str, QualificationCandidateReceipt],
) -> QualificationAblationReceipt:
    baseline = receipts[ablation.baseline_candidate_id]
    variant = receipts[ablation.variant_candidate_id]
    baseline_values = (
        baseline.mean_source_grounding_bp,
        baseline.max_unsupported_fact_bp,
        baseline.max_latency_ms,
        baseline.max_peak_vram_mb,
        baseline.max_peak_ram_mb,
        baseline.total_failures,
    )
    variant_values = (
        variant.mean_source_grounding_bp,
        variant.max_unsupported_fact_bp,
        variant.max_latency_ms,
        variant.max_peak_vram_mb,
        variant.max_peak_ram_mb,
        variant.total_failures,
    )
    if (
        not baseline.executable
        or not variant.executable
        or any(value is None for value in (*baseline_values, *variant_values))
    ):
        return QualificationAblationReceipt(
            ablation_id=ablation.ablation_id,
            factor=ablation.factor,
            baseline_candidate_id=ablation.baseline_candidate_id,
            variant_candidate_id=ablation.variant_candidate_id,
            status=AblationStatus.MISSING,
            source_grounding_delta_bp=None,
            unsupported_fact_delta_bp=None,
            latency_delta_ms=None,
            peak_vram_delta_mb=None,
            peak_ram_delta_mb=None,
            failure_delta=None,
            limitation="baseline.or.variant.not.qualified",
        )
    deltas = tuple(
        cast(int, variant_value) - cast(int, baseline_value)
        for baseline_value, variant_value in zip(baseline_values, variant_values, strict=True)
    )
    return QualificationAblationReceipt(
        ablation_id=ablation.ablation_id,
        factor=ablation.factor,
        baseline_candidate_id=ablation.baseline_candidate_id,
        variant_candidate_id=ablation.variant_candidate_id,
        status=AblationStatus.COMPLETE,
        source_grounding_delta_bp=deltas[0],
        unsupported_fact_delta_bp=deltas[1],
        latency_delta_ms=deltas[2],
        peak_vram_delta_mb=deltas[3],
        peak_ram_delta_mb=deltas[4],
        failure_delta=deltas[5],
    )


def _evaluate_local_qualification_plan(
    plan: QualificationPlan,
    *,
    evidence: tuple[QualificationCandidateEvidence, ...] = (),
) -> LocalQualificationReport:
    """Evaluate plan mechanics after an owning boundary has established authority."""

    _exact_type(plan, QualificationPlan, "qualification plan")
    plan.__post_init__()
    if type(evidence) is not tuple or len(evidence) > MAX_QUALIFICATION_CANDIDATES:
        raise LocalQualificationError("qualification evidence must be a bounded tuple")
    if not all(type(item) is QualificationCandidateEvidence for item in evidence):
        raise LocalQualificationError("qualification evidence contains a non-exact value")
    evidence_ids = tuple(item.candidate_id for item in evidence)
    if len(evidence_ids) != len(set(evidence_ids)):
        raise LocalQualificationError("qualification evidence contains duplicate candidates")
    candidates = {item.candidate_id: item for item in plan.candidates}
    unknown = set(evidence_ids) - set(candidates)
    if unknown:
        raise LocalQualificationError("qualification evidence references an unknown candidate")
    evidence_by_id = {item.candidate_id: item for item in evidence}
    receipts = tuple(
        _receipt(plan, candidate, evidence_by_id.get(candidate.candidate_id))
        for candidate in sorted(plan.candidates, key=lambda item: item.candidate_id)
    )
    qualified = tuple(item for item in receipts if item.executable)
    qualified_ids = tuple(item.candidate_id for item in qualified)
    pareto_ids = tuple(
        item.candidate_id
        for item in qualified
        if not any(_dominates(other, item) for other in qualified if other is not item)
    )
    backend_summaries = tuple(
        BackendQualificationSummary(
            backend=backend,
            candidate_ids=tuple(item.candidate_id for item in receipts if item.backend is backend),
            qualified_candidate_ids=tuple(
                item.candidate_id for item in qualified if item.backend is backend
            ),
        )
        for backend in QualificationBackend
    )
    receipt_by_id = {item.candidate_id: item for item in receipts}
    ablation_receipts = tuple(
        _ablation_receipt(ablation, receipt_by_id)
        for ablation in sorted(plan.ablations, key=lambda item: item.ablation_id)
    )
    product_scope = (
        ProductScopeDisposition.ASSISTED_PROFILE_QUALIFIED
        if qualified
        else ProductScopeDisposition.MANUAL_ONLY_SCOPED
    )
    limitations = tuple(
        sorted(
            {limitation for receipt in receipts for limitation in receipt.limitations if limitation}
        )
    )
    return LocalQualificationReport(
        plan_id=plan.plan_id,
        plan_fingerprint=plan.fingerprint,
        product_scope=product_scope,
        candidate_receipts=receipts,
        backend_summaries=backend_summaries,
        ablation_receipts=ablation_receipts,
        qualified_candidate_ids=qualified_ids,
        pareto_candidate_ids=pareto_ids,
        limitations=limitations,
    )


def evaluate_local_qualification(
    plan: QualificationPlan,
    *,
    evidence: tuple[QualificationCandidateEvidence, ...] = (),
) -> LocalQualificationReport:
    """Evaluate only this release's accepted manual-only plan."""

    _exact_type(plan, QualificationPlan, "qualification plan")
    plan.__post_init__()
    accepted_plan = build_default_local_qualification_plan()
    if plan.fingerprint != accepted_plan.fingerprint:
        raise LocalQualificationError("qualification plan is not the accepted release plan")
    # CRITICAL: an evidence-bearing route must be added only with its actual runtime owner.
    if evidence:
        raise LocalQualificationError("no runtime qualification evidence owner is activated")
    return _evaluate_local_qualification_plan(plan)


def build_default_local_qualification_plan() -> QualificationPlan:
    """Return the preregistered M14-04 plan reflecting currently unqualified live profiles."""

    from .audio_acceptance import evaluate_audio_acceptance
    from .audio_perception_benchmark import build_default_audio_benchmark_plan
    from .local_reconstruction import reconstruction_route_dispositions
    from .visual_acceptance import evaluate_visual_acceptance
    from .visual_benchmark import build_default_visual_benchmark_plan

    predecessor_evidence = (
        evaluate_visual_acceptance(build_default_visual_benchmark_plan()).fingerprint,
        evaluate_audio_acceptance(build_default_audio_benchmark_plan()).fingerprint,
        canonical_fingerprint(
            {
                route.value: disposition.value
                for route, disposition in reconstruction_route_dispositions().items()
            }
        ),
    )
    candidates = (
        QualificationCandidate(
            "native.modular",
            CandidateTopology.MODULAR_ONLY,
            QualificationBackend.COMFYUI_NATIVE,
            "profile.modular.v1",
            "adapter.modular.primary.v1",
            "fusion.provenance.weighted.v1",
            "prompt.source.profiled.v1",
            "enrichment.none.v1",
            "planner.deterministic.v1",
            CandidateAvailability.UNAVAILABLE,
            "visual.audio.live.profiles.unqualified",
        ),
        QualificationCandidate(
            "native.modular.adapter_alt",
            CandidateTopology.MODULAR_ONLY,
            QualificationBackend.COMFYUI_NATIVE,
            "profile.modular.v1",
            "adapter.modular.alternate.v1",
            "fusion.provenance.weighted.v1",
            "prompt.source.profiled.v1",
            "enrichment.none.v1",
            "planner.deterministic.v1",
            CandidateAvailability.UNAVAILABLE,
            "alternate.adapter.profile.unqualified",
        ),
        QualificationCandidate(
            "native.modular.fusion_alt",
            CandidateTopology.MODULAR_ONLY,
            QualificationBackend.COMFYUI_NATIVE,
            "profile.modular.v1",
            "adapter.modular.primary.v1",
            "fusion.conservative.vote.v1",
            "prompt.source.profiled.v1",
            "enrichment.none.v1",
            "planner.deterministic.v1",
            CandidateAvailability.UNAVAILABLE,
            "alternate.fusion.profile.unqualified",
        ),
        QualificationCandidate(
            "native.modular.prompt_alt",
            CandidateTopology.MODULAR_ONLY,
            QualificationBackend.COMFYUI_NATIVE,
            "profile.modular.v1",
            "adapter.modular.primary.v1",
            "fusion.provenance.weighted.v1",
            "prompt.compact.profiled.v1",
            "enrichment.none.v1",
            "planner.deterministic.v1",
            CandidateAvailability.UNAVAILABLE,
            "alternate.prompt.profile.unqualified",
        ),
        QualificationCandidate(
            "native.modular.enrichment_alt",
            CandidateTopology.MODULAR_ONLY,
            QualificationBackend.COMFYUI_NATIVE,
            "profile.modular.v1",
            "adapter.modular.primary.v1",
            "fusion.provenance.weighted.v1",
            "prompt.source.profiled.v1",
            "enrichment.local.text.v1",
            "planner.deterministic.v1",
            CandidateAvailability.UNAVAILABLE,
            "enrichment.profile.unqualified",
        ),
        QualificationCandidate(
            "native.omni",
            CandidateTopology.OMNI_ONLY,
            QualificationBackend.COMFYUI_NATIVE,
            "profile.native.omni.v1",
            "adapter.native.omni.v1",
            "fusion.omni.direct.v1",
            "prompt.source.profiled.v1",
            "enrichment.none.v1",
            "planner.omni.direct.v1",
            CandidateAvailability.UNAVAILABLE,
            "native.omni.profile.unavailable",
        ),
        QualificationCandidate(
            "native.hybrid",
            CandidateTopology.MODULAR_PLUS_PLANNER,
            QualificationBackend.COMFYUI_NATIVE,
            "profile.hybrid.v1",
            "adapter.modular.primary.v1",
            "fusion.provenance.weighted.v1",
            "prompt.source.profiled.v1",
            "enrichment.local.text.v1",
            "planner.constrained.semantic.v1",
            CandidateAvailability.UNAVAILABLE,
            "native.planner.profile.unqualified",
        ),
        QualificationCandidate(
            "native.hybrid.planner_alt",
            CandidateTopology.MODULAR_PLUS_PLANNER,
            QualificationBackend.COMFYUI_NATIVE,
            "profile.hybrid.v1",
            "adapter.modular.primary.v1",
            "fusion.provenance.weighted.v1",
            "prompt.source.profiled.v1",
            "enrichment.local.text.v1",
            "planner.hierarchical.alternate.v1",
            CandidateAvailability.UNAVAILABLE,
            "alternate.planner.profile.unqualified",
        ),
        QualificationCandidate(
            "ollama.modular",
            CandidateTopology.MODULAR_ONLY,
            QualificationBackend.OLLAMA,
            "profile.modular.v1",
            "adapter.modular.primary.v1",
            "fusion.provenance.weighted.v1",
            "prompt.source.profiled.v1",
            "enrichment.none.v1",
            "planner.deterministic.v1",
            CandidateAvailability.UNAVAILABLE,
            "ollama.server.model.profile.unqualified",
        ),
        QualificationCandidate(
            "ollama.omni",
            CandidateTopology.OMNI_ONLY,
            QualificationBackend.OLLAMA,
            "profile.ollama.omni.v1",
            "adapter.ollama.omni.v1",
            "fusion.omni.direct.v1",
            "prompt.source.profiled.v1",
            "enrichment.ollama.text.v1",
            "planner.omni.direct.v1",
            CandidateAvailability.UNAVAILABLE,
            "ollama.server.model.profile.unqualified",
        ),
    )
    cases = (
        QualificationCase(
            "case.image.roles",
            "cluster.image",
            "i2va",
            "sha256:" + "1" * 64,
            "sha256:" + "5" * 64,
        ),
        QualificationCase(
            "case.video.timeline",
            "cluster.video",
            "fl2va",
            "sha256:" + "2" * 64,
            "sha256:" + "6" * 64,
        ),
        QualificationCase(
            "case.reference.identity",
            "cluster.reference",
            "ref2va",
            "sha256:" + "3" * 64,
            "sha256:" + "7" * 64,
        ),
    )
    ablations = (
        QualificationAblation(
            "ablation.adapter",
            AblationFactor.ADAPTER,
            "native.modular",
            "native.modular.adapter_alt",
        ),
        QualificationAblation(
            "ablation.fusion",
            AblationFactor.FUSION_RULE,
            "native.modular",
            "native.modular.fusion_alt",
        ),
        QualificationAblation(
            "ablation.prompt",
            AblationFactor.PROMPT_STRATEGY,
            "native.modular",
            "native.modular.prompt_alt",
        ),
        QualificationAblation(
            "ablation.enrichment",
            AblationFactor.ENRICHMENT_MODEL,
            "native.modular",
            "native.modular.enrichment_alt",
        ),
        QualificationAblation(
            "ablation.planner",
            AblationFactor.PLANNER,
            "native.hybrid",
            "native.hybrid.planner_alt",
        ),
        QualificationAblation(
            "ablation.backend",
            AblationFactor.BACKEND,
            "native.modular",
            "ollama.modular",
        ),
    )
    return QualificationPlan(
        plan_id="m14.04.local.qualification.v1",
        cases=cases,
        candidates=candidates,
        ablations=ablations,
        seeds=(17, 29),
        prompt_profile_id="h3.full.reference.v1",
        prompt_schema_version="1.0",
        settings_fingerprint="sha256:" + "4" * 64,
        predecessor_evidence_fingerprints=predecessor_evidence,
        budget=QualificationBudget(
            max_wall_time_ms=120_000,
            max_peak_vram_mb=24_576,
            max_peak_ram_mb=65_536,
            max_failures=0,
            max_runs=60,
        ),
        thresholds=QualificationThresholds(
            min_source_grounding_bp=8_000,
            max_unsupported_fact_bp=500,
        ),
        privacy_mode="local_no_retention",
        license_scope="approved_local_evaluation",
    )


__all__ = [
    "LOCAL_QUALIFICATION_SCHEMA",
    "MAX_QUALIFICATION_CANDIDATES",
    "MAX_QUALIFICATION_CASES",
    "MAX_QUALIFICATION_LIMITATIONS",
    "MAX_QUALIFICATION_WIRE_BYTES",
    "AblationFactor",
    "AblationStatus",
    "BackendQualificationSummary",
    "CandidateAvailability",
    "CandidateCaseEvidence",
    "CandidateDisposition",
    "CandidateTopology",
    "ExecutionLevel",
    "LocalQualificationError",
    "LocalQualificationReport",
    "ProductScopeDisposition",
    "QualificationBackend",
    "QualificationAblation",
    "QualificationAblationReceipt",
    "QualificationBudget",
    "QualificationCandidate",
    "QualificationCandidateEvidence",
    "QualificationCandidateReceipt",
    "QualificationCase",
    "QualificationPlan",
    "QualificationThresholds",
    "build_default_local_qualification_plan",
    "evaluate_local_qualification",
    "validate_local_qualification_wire",
]
