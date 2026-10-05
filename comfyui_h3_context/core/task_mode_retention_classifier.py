"""Fail-closed H3 task-mode, full-reference task, and retention classification.

This module is a deterministic pre-planning boundary.  It validates an explicit ``TaskMode``
against the canonical reference registry and typed evidence; it never infers a mode or a retention
relationship from media presence, filenames, prose, model output, or provider state.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import TypeAlias

from .canonical import canonical_fingerprint
from .contracts import AssetRole, MediaKind, TaskMode, ValidationDiagnostic, ValidationSeverity
from .directive_authority_engine import (
    DirectiveAuthorityEngineStatus,
    DirectiveAuthorityReport,
)
from .errors import TaskModeRetentionClassifierError
from .intent_graph import (
    AudioRetentionMarker,
    RetentionDomain,
    RetentionRelation,
    RetentionScope,
    VisualRetentionMarker,
)
from .reference_role_resolution import (
    ReferenceRoleGraphStatus,
    ReferenceRoleResolutionGraph,
)
from .registry import ReferenceRegistry

TASK_MODE_RETENTION_SCHEMA = "h3.task_mode_retention_classifier.v2"
MAX_CLASSIFIER_EVIDENCE = 512
MAX_CLASSIFIER_DIAGNOSTICS = 512
MAX_CLASSIFIER_ASSETS = 256
MAX_CLASSIFIER_IDS = 256
MAX_CLASSIFIER_TEXT = 2048
MAX_CLASSIFIER_OUTPUT_BYTES = 131_072

# These are structural admission thresholds, not model-calibration or quality claims.
CLASSIFIER_ACCEPT_THRESHOLD = Decimal("0.90")
CLASSIFIER_ABSTENTION_THRESHOLD = Decimal("0.60")
ACCEPTANCE_THRESHOLD = CLASSIFIER_ACCEPT_THRESHOLD
ABSTENTION_THRESHOLD = CLASSIFIER_ABSTENTION_THRESHOLD

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SENSITIVE = (
    "http://",
    "https://",
    "file://",
    "/mnt/",
    "c:\\",
    "api_key",
    "authorization",
    "bearer ",
    "password",
    "secret",
    "token=",
)


class TaskModeRetentionStatus(str, Enum):
    """Aggregate claim ceiling for one pre-planning classification."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    ABSTAINED = "abstained"
    BLOCKED = "blocked"
    CONFLICTING = "conflicting"


class ClassifierDisposition(str, Enum):
    """Disposition of one mode, task-type, or retention decision."""

    ACCEPTED = "accepted"
    ABSTAINED = "abstained"
    REJECTED = "rejected"
    CONFLICTING = "conflicting"


class ClassifierEvidenceSource(str, Enum):
    """Allowed sources; untrusted model/prose output has no source value here."""

    USER_DIRECTIVE = "user_directive"
    SUPPORTED_EVIDENCE = "supported_evidence"
    ROLE_RESOLUTION = "role_resolution"
    DIRECTIVE_AUTHORITY = "directive_authority"


class FullReferenceTaskType(str, Enum):
    """Closed task-type labels from the pinned Full-Reference guide."""

    KEYFRAME_COMPLETION = "keyframe_completion"
    REFERENCE_GENERATION = "reference_generation"
    VIDEO_EDITING = "video_editing"
    VIDEO_CONTINUATION = "video_continuation"
    AUDIO_REUSE = "audio_reuse"
    AUDIO_REFERENCE = "audio_reference"


class OracleComparisonStatus(str, Enum):
    """Explicit boundary for the separate official-oracle evidence layer."""

    NOT_REQUESTED = "not_requested"
    SEPARATE = "separate"
    UNAVAILABLE = "unavailable"


RetentionMarker: TypeAlias = VisualRetentionMarker | AudioRetentionMarker


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise TaskModeRetentionClassifierError(f"{field_name} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise TaskModeRetentionClassifierError(
            f"{field_name} contains sensitive or locator material"
        )
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_CLASSIFIER_TEXT:
        raise TaskModeRetentionClassifierError(f"{field_name} must be bounded non-empty text")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise TaskModeRetentionClassifierError(
            f"{field_name} contains sensitive or locator material"
        )
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise TaskModeRetentionClassifierError(f"{field_name} contains an unsafe wire code point")
    return value


def _ids(values: object, field_name: str, maximum: int = MAX_CLASSIFIER_IDS) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise TaskModeRetentionClassifierError(f"{field_name} is outside the bounded envelope")
    result = tuple(_identifier(value, f"{field_name} item") for value in values)
    if len(result) != len(set(result)):
        raise TaskModeRetentionClassifierError(f"{field_name} must not contain duplicates")
    return result


def _confidence(value: object, field_name: str) -> Decimal:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or not Decimal("0") <= value <= Decimal("1")
    ):
        raise TaskModeRetentionClassifierError(
            f"{field_name} must be a finite Decimal between 0 and 1"
        )
    return value


def _enum(value: object, expected: type[Enum], field_name: str) -> None:
    if not isinstance(value, expected):
        raise TaskModeRetentionClassifierError(f"{field_name} must be a {expected.__name__}")


def _marker(value: object) -> None:
    if not isinstance(value, (VisualRetentionMarker, AudioRetentionMarker)):
        raise TaskModeRetentionClassifierError("retention marker is outside the closed vocabulary")


def _confidence_disposition(confidence: Decimal) -> ClassifierDisposition:
    if confidence >= CLASSIFIER_ACCEPT_THRESHOLD:
        return ClassifierDisposition.ACCEPTED
    if confidence >= CLASSIFIER_ABSTENTION_THRESHOLD:
        return ClassifierDisposition.ABSTAINED
    return ClassifierDisposition.REJECTED


@dataclass(frozen=True, slots=True)
class ModeEvidence:
    """One explicit source supporting the already selected task mode."""

    mode: TaskMode
    source: ClassifierEvidenceSource
    confidence: Decimal
    evidence_ids: tuple[str, ...] = ()
    reason: str | None = None
    schema: str = TASK_MODE_RETENTION_SCHEMA

    def __post_init__(self) -> None:
        _enum(self.mode, TaskMode, "mode evidence mode")
        _enum(self.source, ClassifierEvidenceSource, "mode evidence source")
        _confidence(self.confidence, "mode evidence confidence")
        _ids(self.evidence_ids, "mode evidence IDs")
        if self.reason is not None:
            _text(self.reason, "mode evidence reason")
        if self.schema != TASK_MODE_RETENTION_SCHEMA:
            raise TaskModeRetentionClassifierError("unsupported mode evidence schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "mode": self.mode.value,
            "source": self.source.value,
            "confidence": format(self.confidence, "f"),
            "evidence_ids": list(self.evidence_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class TaskTypeEvidence:
    """One typed full-reference task-type proposal."""

    task_type: FullReferenceTaskType
    source: ClassifierEvidenceSource
    confidence: Decimal
    source_asset_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    reason: str | None = None
    schema: str = TASK_MODE_RETENTION_SCHEMA

    def __post_init__(self) -> None:
        _enum(self.task_type, FullReferenceTaskType, "task-type evidence task_type")
        _enum(self.source, ClassifierEvidenceSource, "task-type evidence source")
        _confidence(self.confidence, "task-type evidence confidence")
        _ids(self.source_asset_ids, "task-type evidence source_asset_ids")
        _ids(self.evidence_ids, "task-type evidence IDs")
        if self.reason is not None:
            _text(self.reason, "task-type evidence reason")
        if self.schema != TASK_MODE_RETENTION_SCHEMA:
            raise TaskModeRetentionClassifierError("unsupported task-type evidence schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "task_type": self.task_type.value,
            "source": self.source.value,
            "confidence": format(self.confidence, "f"),
            "source_asset_ids": list(self.source_asset_ids),
            "evidence_ids": list(self.evidence_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RetentionEvidence:
    """One typed visual/audio retention proposal."""

    domain: RetentionDomain
    marker: RetentionMarker
    source_asset_ids: tuple[str, ...]
    target_id: str
    source: ClassifierEvidenceSource
    confidence: Decimal
    evidence_ids: tuple[str, ...] = ()
    reason: str | None = None
    schema: str = TASK_MODE_RETENTION_SCHEMA
    scope: RetentionScope = RetentionScope.UNSPECIFIED

    def __post_init__(self) -> None:
        _enum(self.scope, RetentionScope, "retention scope")
        _enum(self.domain, RetentionDomain, "retention evidence domain")
        _marker(self.marker)
        _ids(self.source_asset_ids, "retention evidence source_asset_ids")
        if not self.source_asset_ids:
            raise TaskModeRetentionClassifierError(
                "retention evidence requires source asset ownership"
            )
        _identifier(self.target_id, "retention evidence target_id")
        _enum(self.source, ClassifierEvidenceSource, "retention evidence source")
        _confidence(self.confidence, "retention evidence confidence")
        _ids(self.evidence_ids, "retention evidence IDs")
        if self.reason is not None:
            _text(self.reason, "retention evidence reason")
        if self.schema != TASK_MODE_RETENTION_SCHEMA:
            raise TaskModeRetentionClassifierError("unsupported retention evidence schema")
        if self.domain is RetentionDomain.VISUAL and not isinstance(
            self.marker, VisualRetentionMarker
        ):
            raise TaskModeRetentionClassifierError("visual retention requires a visual marker")
        if self.domain is RetentionDomain.AUDIO and not isinstance(
            self.marker, AudioRetentionMarker
        ):
            raise TaskModeRetentionClassifierError("audio retention requires an audio marker")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "domain": self.domain.value,
            "marker": self.marker.value,
            "scope": self.scope.value,
            "source_asset_ids": list(self.source_asset_ids),
            "target_id": self.target_id,
            "source": self.source.value,
            "confidence": format(self.confidence, "f"),
            "evidence_ids": list(self.evidence_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class OracleComparisonBoundary:
    """A receipt that keeps oracle comparison outside classifier evidence."""

    status: OracleComparisonStatus = OracleComparisonStatus.NOT_REQUESTED
    note: str = "official-oracle comparison is a separate evidence layer"
    schema: str = TASK_MODE_RETENTION_SCHEMA

    def __post_init__(self) -> None:
        _enum(self.status, OracleComparisonStatus, "oracle comparison status")
        _text(self.note, "oracle comparison note")
        if self.schema != TASK_MODE_RETENTION_SCHEMA:
            raise TaskModeRetentionClassifierError("unsupported oracle boundary schema")

    def to_wire(self) -> dict[str, str]:
        return {"schema": self.schema, "status": self.status.value, "note": self.note}


@dataclass(frozen=True, slots=True)
class ClassifierCalibrationPolicy:
    """Frozen structural thresholds; not a model-calibration result."""

    accept_threshold: Decimal = CLASSIFIER_ACCEPT_THRESHOLD
    abstention_threshold: Decimal = CLASSIFIER_ABSTENTION_THRESHOLD
    schema: str = TASK_MODE_RETENTION_SCHEMA

    def __post_init__(self) -> None:
        _confidence(self.accept_threshold, "accept threshold")
        _confidence(self.abstention_threshold, "abstention threshold")
        if self.accept_threshold <= self.abstention_threshold:
            raise TaskModeRetentionClassifierError(
                "accept threshold must be greater than abstention threshold"
            )
        if self.schema != TASK_MODE_RETENTION_SCHEMA:
            raise TaskModeRetentionClassifierError("unsupported calibration policy schema")

    def to_wire(self) -> dict[str, str]:
        return {
            "schema": self.schema,
            "accept_threshold": format(self.accept_threshold, "f"),
            "abstention_threshold": format(self.abstention_threshold, "f"),
        }


@dataclass(frozen=True, slots=True)
class TaskModeRetentionRequest:
    """Immutable pre-planning input with explicit typed evidence only."""

    task_mode: TaskMode
    reference_registry: ReferenceRegistry = field(default_factory=ReferenceRegistry.empty)
    mode_evidence: tuple[ModeEvidence, ...] = ()
    task_type_evidence: tuple[TaskTypeEvidence, ...] = ()
    retention_evidence: tuple[RetentionEvidence, ...] = ()
    retention_relations: tuple[RetentionRelation, ...] = ()
    role_graph: ReferenceRoleResolutionGraph | None = None
    authority_report: DirectiveAuthorityReport | None = None
    oracle_comparison: OracleComparisonBoundary = field(default_factory=OracleComparisonBoundary)
    schema: str = TASK_MODE_RETENTION_SCHEMA

    def __post_init__(self) -> None:
        _enum(self.task_mode, TaskMode, "task_mode")
        if not isinstance(self.reference_registry, ReferenceRegistry):
            raise TaskModeRetentionClassifierError("reference_registry must be a ReferenceRegistry")
        for values, expected, field_name in (
            (self.mode_evidence, ModeEvidence, "mode_evidence"),
            (self.task_type_evidence, TaskTypeEvidence, "task_type_evidence"),
            (self.retention_evidence, RetentionEvidence, "retention_evidence"),
            (self.retention_relations, RetentionRelation, "retention_relations"),
        ):
            if (
                not isinstance(values, tuple)
                or len(values) > MAX_CLASSIFIER_EVIDENCE
                or not all(isinstance(item, expected) for item in values)
            ):
                raise TaskModeRetentionClassifierError(
                    f"{field_name} is outside the bounded envelope"
                )
        if self.role_graph is not None and not isinstance(
            self.role_graph, ReferenceRoleResolutionGraph
        ):
            raise TaskModeRetentionClassifierError("role_graph must be a typed role graph")
        if self.authority_report is not None and not isinstance(
            self.authority_report, DirectiveAuthorityReport
        ):
            raise TaskModeRetentionClassifierError("authority_report must be a typed report")
        if not isinstance(self.oracle_comparison, OracleComparisonBoundary):
            raise TaskModeRetentionClassifierError(
                "oracle_comparison must be an OracleComparisonBoundary"
            )
        if self.schema != TASK_MODE_RETENTION_SCHEMA:
            raise TaskModeRetentionClassifierError("unsupported classifier request schema")

    @classmethod
    def from_user_mode(
        cls,
        task_mode: TaskMode,
        reference_registry: ReferenceRegistry | None = None,
    ) -> TaskModeRetentionRequest:
        """Build the explicit user-mode-only request without reading prose or media."""

        if not isinstance(task_mode, TaskMode):
            raise TaskModeRetentionClassifierError("task_mode must be a TaskMode")
        return cls(
            task_mode,
            ReferenceRegistry.empty() if reference_registry is None else reference_registry,
            (
                ModeEvidence(
                    task_mode, ClassifierEvidenceSource.USER_DIRECTIVE, Decimal("1"), ("mode.user",)
                ),
            ),
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "task_mode": self.task_mode.value,
            "reference_registry": self.reference_registry.to_wire(),
            "mode_evidence": [item.to_wire() for item in self.mode_evidence],
            "task_type_evidence": [item.to_wire() for item in self.task_type_evidence],
            "retention_evidence": [item.to_wire() for item in self.retention_evidence],
            "retention_relations": [item.to_wire() for item in self.retention_relations],
            "role_graph": None if self.role_graph is None else self.role_graph.to_wire(),
            "authority_report": (
                None if self.authority_report is None else self.authority_report.to_wire()
            ),
            "oracle_comparison": self.oracle_comparison.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class TaskModeDecision:
    mode: TaskMode
    disposition: ClassifierDisposition
    source: ClassifierEvidenceSource
    confidence: Decimal
    evidence_ids: tuple[str, ...]
    reason: str
    schema: str = TASK_MODE_RETENTION_SCHEMA

    def __post_init__(self) -> None:
        _enum(self.mode, TaskMode, "mode decision mode")
        _enum(self.disposition, ClassifierDisposition, "mode decision disposition")
        _enum(self.source, ClassifierEvidenceSource, "mode decision source")
        _confidence(self.confidence, "mode decision confidence")
        _ids(self.evidence_ids, "mode decision evidence_ids")
        _text(self.reason, "mode decision reason")
        if self.schema != TASK_MODE_RETENTION_SCHEMA:
            raise TaskModeRetentionClassifierError("unsupported mode decision schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "mode": self.mode.value,
            "disposition": self.disposition.value,
            "source": self.source.value,
            "confidence": format(self.confidence, "f"),
            "evidence_ids": list(self.evidence_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class TaskTypeDecision:
    task_type: FullReferenceTaskType
    disposition: ClassifierDisposition
    source: ClassifierEvidenceSource
    confidence: Decimal
    source_asset_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    reason: str
    schema: str = TASK_MODE_RETENTION_SCHEMA

    def __post_init__(self) -> None:
        _enum(self.task_type, FullReferenceTaskType, "task-type decision task_type")
        _enum(self.disposition, ClassifierDisposition, "task-type decision disposition")
        _enum(self.source, ClassifierEvidenceSource, "task-type decision source")
        _confidence(self.confidence, "task-type decision confidence")
        _ids(self.source_asset_ids, "task-type decision source_asset_ids")
        _ids(self.evidence_ids, "task-type decision evidence_ids")
        _text(self.reason, "task-type decision reason")
        if self.schema != TASK_MODE_RETENTION_SCHEMA:
            raise TaskModeRetentionClassifierError("unsupported task-type decision schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "task_type": self.task_type.value,
            "disposition": self.disposition.value,
            "source": self.source.value,
            "confidence": format(self.confidence, "f"),
            "source_asset_ids": list(self.source_asset_ids),
            "evidence_ids": list(self.evidence_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RetentionDecision:
    domain: RetentionDomain
    marker: RetentionMarker
    disposition: ClassifierDisposition
    source: ClassifierEvidenceSource
    confidence: Decimal
    source_asset_ids: tuple[str, ...]
    target_id: str
    evidence_ids: tuple[str, ...]
    reason: str
    schema: str = TASK_MODE_RETENTION_SCHEMA
    scope: RetentionScope = RetentionScope.UNSPECIFIED

    def __post_init__(self) -> None:
        _enum(self.scope, RetentionScope, "retention scope")
        _enum(self.domain, RetentionDomain, "retention decision domain")
        _marker(self.marker)
        _enum(self.disposition, ClassifierDisposition, "retention decision disposition")
        _enum(self.source, ClassifierEvidenceSource, "retention decision source")
        _confidence(self.confidence, "retention decision confidence")
        _ids(self.source_asset_ids, "retention decision source_asset_ids")
        _identifier(self.target_id, "retention decision target_id")
        _ids(self.evidence_ids, "retention decision evidence_ids")
        _text(self.reason, "retention decision reason")
        if self.schema != TASK_MODE_RETENTION_SCHEMA:
            raise TaskModeRetentionClassifierError("unsupported retention decision schema")
        if self.domain is RetentionDomain.VISUAL and not isinstance(
            self.marker, VisualRetentionMarker
        ):
            raise TaskModeRetentionClassifierError("visual retention requires a visual marker")
        if self.domain is RetentionDomain.AUDIO and not isinstance(
            self.marker, AudioRetentionMarker
        ):
            raise TaskModeRetentionClassifierError("audio retention requires an audio marker")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "domain": self.domain.value,
            "marker": self.marker.value,
            "scope": self.scope.value,
            "disposition": self.disposition.value,
            "source": self.source.value,
            "confidence": format(self.confidence, "f"),
            "source_asset_ids": list(self.source_asset_ids),
            "target_id": self.target_id,
            "evidence_ids": list(self.evidence_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class TaskModeRetentionReport:
    status: TaskModeRetentionStatus
    mode: TaskModeDecision
    task_types: tuple[TaskTypeDecision, ...]
    retention: tuple[RetentionDecision, ...]
    diagnostics: tuple[ValidationDiagnostic, ...]
    calibration: ClassifierCalibrationPolicy
    oracle_comparison: OracleComparisonBoundary
    input_fingerprint: str
    registry_fingerprint: str
    role_graph_fingerprint: str | None = None
    authority_report_fingerprint: str | None = None
    report_fingerprint: str | None = None
    schema: str = TASK_MODE_RETENTION_SCHEMA

    def __post_init__(self) -> None:
        _enum(self.status, TaskModeRetentionStatus, "report status")
        if not isinstance(self.mode, TaskModeDecision):
            raise TaskModeRetentionClassifierError("report mode must be a TaskModeDecision")
        for values, expected_type, field_name, maximum in (
            (self.task_types, TaskTypeDecision, "task_types", MAX_CLASSIFIER_EVIDENCE),
            (self.retention, RetentionDecision, "retention", MAX_CLASSIFIER_EVIDENCE),
            (
                self.diagnostics,
                ValidationDiagnostic,
                "diagnostics",
                MAX_CLASSIFIER_DIAGNOSTICS,
            ),
        ):
            if (
                not isinstance(values, tuple)
                or len(values) > maximum
                or not all(isinstance(item, expected_type) for item in values)
            ):
                raise TaskModeRetentionClassifierError(f"report {field_name} is invalid")
        if not isinstance(self.calibration, ClassifierCalibrationPolicy):
            raise TaskModeRetentionClassifierError("report calibration is invalid")
        if not isinstance(self.oracle_comparison, OracleComparisonBoundary):
            raise TaskModeRetentionClassifierError("report oracle comparison is invalid")
        for value, field_name in (
            (self.input_fingerprint, "input_fingerprint"),
            (self.registry_fingerprint, "registry_fingerprint"),
        ):
            if _FINGERPRINT.fullmatch(value) is None:
                raise TaskModeRetentionClassifierError(
                    f"{field_name} must be a SHA-256 fingerprint"
                )
        for fingerprint_value, field_name in (
            (self.role_graph_fingerprint, "role_graph_fingerprint"),
            (self.authority_report_fingerprint, "authority_report_fingerprint"),
        ):
            if fingerprint_value is not None and _FINGERPRINT.fullmatch(fingerprint_value) is None:
                raise TaskModeRetentionClassifierError(
                    f"{field_name} must be a SHA-256 fingerprint or None"
                )
        if self.schema != TASK_MODE_RETENTION_SCHEMA:
            raise TaskModeRetentionClassifierError("unsupported classifier report schema")
        expected = canonical_fingerprint(self.to_wire(include_fingerprint=False))
        if self.report_fingerprint is None:
            object.__setattr__(self, "report_fingerprint", expected)
        elif self.report_fingerprint != expected:
            raise TaskModeRetentionClassifierError("report fingerprint does not match contents")
        if len(self.to_wire_bytes()) > MAX_CLASSIFIER_OUTPUT_BYTES:
            raise TaskModeRetentionClassifierError("classifier report exceeds output limit")

    @property
    def fingerprint(self) -> str:
        if self.report_fingerprint is None:  # pragma: no cover - set in __post_init__
            raise TaskModeRetentionClassifierError("report fingerprint is not initialized")
        return self.report_fingerprint

    @property
    def task_type_decisions(self) -> tuple[TaskTypeDecision, ...]:
        """Compatibility/readability alias for the ordered task-type decisions."""

        return self.task_types

    @property
    def retention_decisions(self) -> tuple[RetentionDecision, ...]:
        """Compatibility/readability alias for the ordered retention decisions."""

        return self.retention

    def to_wire(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.schema,
            "status": self.status.value,
            "mode": self.mode.to_wire(),
            "task_types": [item.to_wire() for item in self.task_types],
            "retention": [item.to_wire() for item in self.retention],
            "diagnostics": [item.to_wire() for item in self.diagnostics],
            "calibration": self.calibration.to_wire(),
            "oracle_comparison": self.oracle_comparison.to_wire(),
            "input_fingerprint": self.input_fingerprint,
            "registry_fingerprint": self.registry_fingerprint,
            "role_graph_fingerprint": self.role_graph_fingerprint,
            "authority_report_fingerprint": self.authority_report_fingerprint,
        }
        if include_fingerprint:
            value["report_fingerprint"] = self.fingerprint
        return value

    def to_wire_bytes(self) -> bytes:
        import json

        return json.dumps(
            self.to_wire(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")


def _diagnostic(
    severity: ValidationSeverity,
    code: str,
    message: str,
    location: str = "task_mode_retention_classifier",
) -> ValidationDiagnostic:
    return ValidationDiagnostic(severity, code, message, location)


def _mode_decision(
    request: TaskModeRetentionRequest,
) -> tuple[TaskModeDecision, list[ValidationDiagnostic]]:
    diagnostics: list[ValidationDiagnostic] = []
    evidence = request.mode_evidence
    if not evidence:
        return (
            TaskModeDecision(
                request.task_mode,
                ClassifierDisposition.ACCEPTED,
                ClassifierEvidenceSource.USER_DIRECTIVE,
                Decimal("1"),
                ("mode.request",),
                "task mode is an explicit typed request value",
            ),
            diagnostics,
        )
    mismatched = tuple(item for item in evidence if item.mode is not request.task_mode)
    if mismatched:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "conflicting_mode_evidence",
                "mode evidence contains a task mode different from the selected request mode",
            )
        )
        ids = tuple(sorted({item_id for item in evidence for item_id in item.evidence_ids}))
        return (
            TaskModeDecision(
                request.task_mode,
                ClassifierDisposition.CONFLICTING,
                mismatched[0].source,
                max(item.confidence for item in evidence),
                ids,
                "conflicting typed mode evidence requires an explicit caller decision",
            ),
            diagnostics,
        )
    selected = max(
        evidence,
        key=lambda item: (item.confidence, item.source.value, item.evidence_ids),
    )
    disposition = _confidence_disposition(selected.confidence)
    if disposition is not ClassifierDisposition.ACCEPTED:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.WARNING,
                "mode_confidence_abstention",
                "mode evidence is below the frozen acceptance threshold",
            )
        )
    return (
        TaskModeDecision(
            request.task_mode,
            disposition,
            selected.source,
            selected.confidence,
            tuple(sorted({item_id for item in evidence for item_id in item.evidence_ids})),
            selected.reason or "typed mode evidence was evaluated without prose inference",
        ),
        diagnostics,
    )


def _validate_mode_assets(
    mode: TaskMode,
    registry: ReferenceRegistry,
) -> list[ValidationDiagnostic]:
    assets = registry.assets
    diagnostics: list[ValidationDiagnostic] = []
    first = tuple(item for item in assets if item.role is AssetRole.FIRST_FRAME)
    last = tuple(item for item in assets if item.role is AssetRole.LAST_FRAME)
    if mode is TaskMode.T2VA:
        if assets:
            diagnostics.append(
                _diagnostic(
                    ValidationSeverity.ERROR,
                    "impossible_role_mixture",
                    "T2VA is text-only and cannot carry reference or frame-anchor assets",
                )
            )
    elif mode is TaskMode.I2VA:
        if len(assets) != 1 or len(first) != 1 or assets[0].kind is not MediaKind.IMAGE:
            diagnostics.append(
                _diagnostic(
                    ValidationSeverity.ERROR,
                    "impossible_role_mixture",
                    "I2VA requires exactly one first-frame image and no other assets",
                )
            )
    elif mode is TaskMode.FL2VA:
        if (
            len(assets) != 2
            or len(first) != 1
            or len(last) != 1
            or any(item.kind is not MediaKind.IMAGE for item in assets)
        ):
            diagnostics.append(
                _diagnostic(
                    ValidationSeverity.ERROR,
                    "impossible_role_mixture",
                    "FL2VA requires exactly one first-frame and one last-frame image",
                )
            )
    elif mode is TaskMode.L2VA:
        if len(assets) != 1 or len(last) != 1 or assets[0].kind is not MediaKind.IMAGE:
            diagnostics.append(
                _diagnostic(
                    ValidationSeverity.ERROR,
                    "impossible_role_mixture",
                    "L2VA requires exactly one last-frame image and no other assets",
                )
            )
    else:
        # IMPORTANT: classification describes owned inputs, not backend execution capability;
        # requiring a visual reference here rejects valid text plus reference-audio requests.
        non_anchor = tuple(
            item
            for item in assets
            if item.role not in {AssetRole.FIRST_FRAME, AssetRole.LAST_FRAME}
        )
        if not assets:
            diagnostics.append(
                _diagnostic(
                    ValidationSeverity.ERROR,
                    "missing_reference_assets",
                    "Ref2VA requires at least one explicitly owned reference asset",
                )
            )
        elif not non_anchor:
            diagnostics.append(
                _diagnostic(
                    ValidationSeverity.ERROR,
                    "impossible_role_mixture",
                    "Ref2VA requires a non-anchor reference role in addition to frame anchors",
                )
            )
    return diagnostics


def _validate_role_graph(
    request: TaskModeRetentionRequest,
) -> tuple[list[ValidationDiagnostic], str | None, bool, bool]:
    graph = request.role_graph
    if graph is None:
        return [], None, False, False
    if graph.labels != request.reference_registry.labels:
        raise TaskModeRetentionClassifierError(
            "role graph labels do not match the canonical reference registry"
        )
    registry_ids = {asset.asset_id for asset in request.reference_registry.assets}
    for assignment in graph.assignments:
        if not set(assignment.source_asset_ids).issubset(registry_ids):
            raise TaskModeRetentionClassifierError(
                "role graph assignment references an asset outside the canonical registry"
            )
        if assignment.candidate_id not in registry_ids and assignment.role.value in {
            "picture",
            "video",
            "audio",
        }:
            raise TaskModeRetentionClassifierError(
                "asset role assignment candidate is outside the canonical registry"
            )
    diagnostics: list[ValidationDiagnostic] = []
    conflict = graph.status is ReferenceRoleGraphStatus.CONFLICTING
    abstain = graph.status in {
        ReferenceRoleGraphStatus.AMBIGUOUS,
        ReferenceRoleGraphStatus.PARTIAL,
    }
    if conflict:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "conflicting_role_evidence",
                "role-resolution evidence contains an explicit conflict",
            )
        )
    elif abstain:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.WARNING,
                "ambiguous_role_evidence",
                "role-resolution evidence is incomplete or ambiguous; classification abstains",
            )
        )
    return diagnostics, graph.fingerprint, conflict, abstain


def _validate_authority(
    report: DirectiveAuthorityReport | None,
) -> tuple[list[ValidationDiagnostic], str | None, bool, bool]:
    if report is None:
        return [], None, False, False
    conflict = report.status in {
        DirectiveAuthorityEngineStatus.BLOCKED,
        DirectiveAuthorityEngineStatus.CONFLICTING,
    }
    abstain = report.status is DirectiveAuthorityEngineStatus.PARTIAL
    diagnostics: list[ValidationDiagnostic] = []
    if conflict:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "conflicting_directive_authority",
                "directive authority is blocked or conflicting; no lower-authority winner is used",
            )
        )
    elif abstain:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.WARNING,
                "partial_directive_authority",
                "directive authority is partial; dependent classification remains bounded",
            )
        )
    return diagnostics, report.fingerprint, conflict, abstain


def _matching_asset_ids(
    evidence_ids: tuple[str, ...], registry: ReferenceRegistry
) -> tuple[str, ...]:
    if evidence_ids:
        return evidence_ids
    return tuple(asset.asset_id for asset in registry.assets)


def _task_type_supported(
    evidence: TaskTypeEvidence,
    mode: TaskMode,
    registry: ReferenceRegistry,
) -> tuple[bool, tuple[str, ...], str]:
    if mode is not TaskMode.REF2VA:
        return False, (), "full-reference task types are valid only for Ref2VA"
    registry_map = {asset.asset_id: asset for asset in registry.assets}
    source_ids = _matching_asset_ids(evidence.source_asset_ids, registry)
    if any(asset_id not in registry_map for asset_id in source_ids):
        return False, source_ids, "task-type evidence references an unknown asset"
    source_assets = tuple(registry_map[asset_id] for asset_id in source_ids)
    if not source_assets:
        return False, source_ids, "task-type evidence requires owned reference assets"
    if evidence.task_type is FullReferenceTaskType.KEYFRAME_COMPLETION:
        valid = any(
            asset.kind is MediaKind.IMAGE
            and asset.role in {AssetRole.FIRST_FRAME, AssetRole.LAST_FRAME}
            for asset in source_assets
        )
    elif evidence.task_type is FullReferenceTaskType.REFERENCE_GENERATION:
        valid = any(
            asset.role not in {AssetRole.FIRST_FRAME, AssetRole.LAST_FRAME}
            for asset in source_assets
        )
    elif evidence.task_type in {
        FullReferenceTaskType.VIDEO_EDITING,
        FullReferenceTaskType.VIDEO_CONTINUATION,
    }:
        valid = any(asset.kind is MediaKind.VIDEO for asset in source_assets)
    else:
        valid = any(asset.kind is MediaKind.AUDIO for asset in source_assets)
    if not valid:
        return False, source_ids, "task-type evidence is incompatible with its source asset roles"
    return True, source_ids, "typed full-reference task evidence is supported"


def _task_type_decisions(
    request: TaskModeRetentionRequest,
    diagnostics: list[ValidationDiagnostic],
) -> tuple[TaskTypeDecision, ...]:
    if not request.task_type_evidence:
        if request.task_mode is TaskMode.REF2VA:
            diagnostics.append(
                _diagnostic(
                    ValidationSeverity.WARNING,
                    "missing_task_type_evidence",
                    "Ref2VA task type is not guessed without a typed directive or evidence record",
                )
            )
        return ()
    grouped: dict[FullReferenceTaskType, list[TaskTypeEvidence]] = {}
    for item in request.task_type_evidence:
        grouped.setdefault(item.task_type, []).append(item)
    decisions: list[TaskTypeDecision] = []
    for task_type in sorted(grouped, key=lambda value: value.value):
        values = grouped[task_type]
        compatible: list[tuple[TaskTypeEvidence, tuple[str, ...]]] = []
        for item in values:
            supported, source_ids, reason = _task_type_supported(
                item, request.task_mode, request.reference_registry
            )
            if not supported:
                diagnostics.append(
                    _diagnostic(ValidationSeverity.ERROR, "unsupported_task_type_evidence", reason)
                )
            else:
                compatible.append((item, source_ids))
        if not compatible:
            selected = max(values, key=lambda item: (item.confidence, item.evidence_ids))
            decisions.append(
                TaskTypeDecision(
                    task_type,
                    ClassifierDisposition.REJECTED,
                    selected.source,
                    selected.confidence,
                    selected.source_asset_ids,
                    selected.evidence_ids,
                    "task type was rejected before planning because typed source roles are "
                    "incompatible",
                )
            )
            continue
        selected, selected_ids = max(
            compatible,
            key=lambda item: (item[0].confidence, item[0].source.value, item[0].evidence_ids),
        )
        disposition = _confidence_disposition(selected.confidence)
        if disposition is ClassifierDisposition.ABSTAINED:
            diagnostics.append(
                _diagnostic(
                    ValidationSeverity.WARNING,
                    "task_type_confidence_abstention",
                    f"task type {task_type.value!r} is below the frozen acceptance threshold",
                )
            )
        decisions.append(
            TaskTypeDecision(
                task_type,
                disposition,
                selected.source,
                selected.confidence,
                selected_ids,
                tuple(
                    sorted(
                        {evidence_id for item, _ in compatible for evidence_id in item.evidence_ids}
                    )
                ),
                selected.reason or "typed full-reference task evidence was evaluated",
            )
        )
    return tuple(decisions)


def _retention_inputs(request: TaskModeRetentionRequest) -> tuple[RetentionEvidence, ...]:
    derived = tuple(
        RetentionEvidence(
            relation.domain,
            relation.marker,
            relation.source_asset_ids,
            relation.target_id,
            ClassifierEvidenceSource.SUPPORTED_EVIDENCE,
            Decimal("1"),
            (relation.relation_id,),
            "retention relation supplied by the typed intent graph",
            scope=relation.scope,
        )
        for relation in request.retention_relations
    )
    return request.retention_evidence + derived


def _retention_supported(
    evidence: RetentionEvidence,
    mode: TaskMode,
    registry: ReferenceRegistry,
) -> tuple[bool, str]:
    if mode is not TaskMode.REF2VA:
        return False, "retention markers are valid only for Ref2VA"
    visual_scopes = {RetentionScope.SUBJECT, RetentionScope.PICTURE, RetentionScope.VIDEO_STRUCTURE}
    audio_scopes = {RetentionScope.AUDIO_LAYER, RetentionScope.COMPLETE_FINAL_AUDIO_TRACK}
    if (evidence.domain is RetentionDomain.VISUAL and evidence.scope in audio_scopes) or (
        evidence.domain is RetentionDomain.AUDIO and evidence.scope in visual_scopes
    ):
        return False, "retention scope contradicts its domain"
    if evidence.marker is AudioRetentionMarker.FULLY_COPY and evidence.scope not in {
        RetentionScope.UNSPECIFIED,
        RetentionScope.COMPLETE_FINAL_AUDIO_TRACK,
    }:
        return False, "full audio copy requires explicit complete final-track scope"
    registry_map = {asset.asset_id: asset for asset in registry.assets}
    # GUARD: confidence cannot invent a standalone denotation absent from the typed registry.
    # Keep these role requirements aligned with IntentGraph retention target validation.
    if evidence.scope in {RetentionScope.PICTURE, RetentionScope.VIDEO_STRUCTURE}:
        target = registry_map.get(evidence.target_id)
        roles = (
            {AssetRole.FIRST_FRAME, AssetRole.LAST_FRAME}
            if evidence.scope is RetentionScope.PICTURE
            else {
                AssetRole.EDITING_SOURCE,
                AssetRole.CONTINUATION_SOURCE,
                AssetRole.MOTION_REFERENCE,
                AssetRole.CAMERA_REFERENCE,
            }
        )
        kind = MediaKind.IMAGE if evidence.scope is RetentionScope.PICTURE else MediaKind.VIDEO
        if target is None or target.kind is not kind or target.role not in roles:
            return False, "retention target lacks the declared picture/video role"
    assets = tuple(registry_map.get(asset_id) for asset_id in evidence.source_asset_ids)
    if any(asset is None for asset in assets):
        return False, "retention evidence references an unknown asset"
    known_assets = tuple(asset for asset in assets if asset is not None)
    if evidence.domain is RetentionDomain.VISUAL:
        if not all(asset.kind in {MediaKind.IMAGE, MediaKind.VIDEO} for asset in known_assets):
            return False, "visual retention evidence requires an image or video source"
    elif not all(asset.kind is MediaKind.AUDIO for asset in known_assets):
        return False, "audio retention evidence requires audio source assets"
    return True, "typed retention evidence is supported"


def _retention_decisions(
    request: TaskModeRetentionRequest,
    diagnostics: list[ValidationDiagnostic],
) -> tuple[RetentionDecision, ...]:
    values = _retention_inputs(request)
    if not values:
        if request.task_mode is TaskMode.REF2VA:
            diagnostics.append(
                _diagnostic(
                    ValidationSeverity.WARNING,
                    "missing_retention_evidence",
                    "Ref2VA retention is not guessed without a typed relation or directive",
                )
            )
        return ()
    if request.task_mode is not TaskMode.REF2VA:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "retention_mode_mismatch",
                "full-reference retention evidence cannot be attached to a base task mode",
            )
        )
    grouped: dict[tuple[RetentionDomain, str, tuple[str, ...]], list[RetentionEvidence]] = {}
    for item in values:
        supported, reason = _retention_supported(
            item, request.task_mode, request.reference_registry
        )
        if not supported:
            diagnostics.append(
                _diagnostic(ValidationSeverity.ERROR, "unsupported_retention_evidence", reason)
            )
        grouped.setdefault((item.domain, item.target_id, item.source_asset_ids), []).append(item)

    decisions: list[RetentionDecision] = []
    for key in sorted(grouped, key=lambda value: (value[0].value, value[1], value[2])):
        group = grouped[key]
        # Re-evaluate per item so one bad group cannot make a valid sibling disappear.
        supported_values = [
            item
            for item in group
            if _retention_supported(item, request.task_mode, request.reference_registry)[0]
        ]
        if not supported_values:
            selected = max(group, key=lambda item: (item.confidence, item.evidence_ids))
            decisions.append(
                RetentionDecision(
                    selected.domain,
                    selected.marker,
                    ClassifierDisposition.REJECTED,
                    selected.source,
                    selected.confidence,
                    selected.source_asset_ids,
                    selected.target_id,
                    selected.evidence_ids,
                    "retention evidence was rejected before planning because source roles are "
                    "incompatible",
                    scope=selected.scope,
                )
            )
            continue
        highest_confidence = max(item.confidence for item in supported_values)
        winners = [item for item in supported_values if item.confidence == highest_confidence]
        marker_values = {(item.marker, item.scope) for item in winners}
        if len(marker_values) > 1:
            for item in sorted(winners, key=lambda value: (value.marker.value, value.evidence_ids)):
                decisions.append(
                    RetentionDecision(
                        item.domain,
                        item.marker,
                        ClassifierDisposition.CONFLICTING,
                        item.source,
                        item.confidence,
                        item.source_asset_ids,
                        item.target_id,
                        item.evidence_ids,
                        "equal-confidence retention markers require an explicit caller decision",
                        scope=item.scope,
                    )
                )
            diagnostics.append(
                _diagnostic(
                    ValidationSeverity.ERROR,
                    "conflicting_retention_evidence",
                    "equal-confidence retention markers disagree for one target",
                )
            )
            continue
        selected = max(winners, key=lambda item: (item.source.value, item.evidence_ids))
        disposition = _confidence_disposition(selected.confidence)
        # GUARD: legacy source-to-target evidence has no declared denotation. Confidence
        # cannot upgrade missing copy scope into current official retention authority.
        if selected.scope is RetentionScope.UNSPECIFIED:
            disposition = ClassifierDisposition.ABSTAINED
        if disposition is ClassifierDisposition.ABSTAINED:
            diagnostics.append(
                _diagnostic(
                    ValidationSeverity.WARNING,
                    "retention_confidence_abstention",
                    "retention evidence is below the frozen acceptance threshold",
                )
            )
        decisions.append(
            RetentionDecision(
                selected.domain,
                selected.marker,
                disposition,
                selected.source,
                selected.confidence,
                selected.source_asset_ids,
                selected.target_id,
                tuple(
                    sorted(
                        {
                            evidence_id
                            for item in supported_values
                            for evidence_id in item.evidence_ids
                        }
                    )
                ),
                selected.reason or "typed retention evidence was evaluated",
                scope=selected.scope,
            )
        )
    return tuple(decisions)


def _aggregate_status(
    mode: TaskModeDecision,
    task_types: tuple[TaskTypeDecision, ...],
    retention: tuple[RetentionDecision, ...],
    diagnostics: Iterable[ValidationDiagnostic],
    *,
    role_conflict: bool,
    authority_conflict: bool,
    role_abstain: bool,
    authority_abstain: bool,
) -> TaskModeRetentionStatus:
    diagnostic_values = tuple(diagnostics)
    if (
        mode.disposition is ClassifierDisposition.CONFLICTING
        or role_conflict
        or authority_conflict
        or any(
            item.severity is ValidationSeverity.ERROR and item.code.startswith("conflicting_")
            for item in diagnostic_values
        )
        or any(
            item.disposition is ClassifierDisposition.CONFLICTING for item in task_types + retention
        )
    ):
        return TaskModeRetentionStatus.CONFLICTING
    if mode.disposition is ClassifierDisposition.REJECTED or any(
        item.severity is ValidationSeverity.ERROR for item in diagnostic_values
    ):
        return TaskModeRetentionStatus.BLOCKED
    if mode.disposition is ClassifierDisposition.ABSTAINED:
        return TaskModeRetentionStatus.ABSTAINED
    decisions = task_types + retention
    if any(item.disposition is ClassifierDisposition.REJECTED for item in decisions):
        return TaskModeRetentionStatus.ABSTAINED
    if any(item.disposition is ClassifierDisposition.ABSTAINED for item in decisions):
        return TaskModeRetentionStatus.PARTIAL
    if role_abstain or authority_abstain:
        return TaskModeRetentionStatus.PARTIAL
    if any(item.code.startswith("missing_") for item in diagnostic_values):
        return TaskModeRetentionStatus.PARTIAL
    return TaskModeRetentionStatus.COMPLETE


def build_task_mode_retention_report(
    request: TaskModeRetentionRequest,
) -> TaskModeRetentionReport:
    """Validate explicit mode/role/task/retention evidence before planning."""

    if not isinstance(request, TaskModeRetentionRequest):
        raise TaskModeRetentionClassifierError("request must be a TaskModeRetentionRequest")
    input_fingerprint = canonical_fingerprint(request.to_wire())
    registry_fingerprint = canonical_fingerprint(request.reference_registry.to_wire())
    diagnostics: list[ValidationDiagnostic] = []
    mode, mode_diagnostics = _mode_decision(request)
    diagnostics.extend(mode_diagnostics)
    diagnostics.extend(_validate_mode_assets(request.task_mode, request.reference_registry))
    role_diagnostics, role_fingerprint, role_conflict, role_abstain = _validate_role_graph(request)
    authority_diagnostics, authority_fingerprint, authority_conflict, authority_abstain = (
        _validate_authority(request.authority_report)
    )
    diagnostics.extend(role_diagnostics)
    diagnostics.extend(authority_diagnostics)
    task_types = _task_type_decisions(request, diagnostics)
    retention = _retention_decisions(request, diagnostics)
    diagnostics_tuple = tuple(
        sorted(
            {item.code: item for item in diagnostics}.values(),
            key=lambda item: (item.severity.value, item.code, item.location or ""),
        )
    )
    status = _aggregate_status(
        mode,
        task_types,
        retention,
        diagnostics_tuple,
        role_conflict=role_conflict,
        authority_conflict=authority_conflict,
        role_abstain=role_abstain,
        authority_abstain=authority_abstain,
    )
    provisional = TaskModeRetentionReport(
        status,
        mode,
        task_types,
        retention,
        diagnostics_tuple,
        ClassifierCalibrationPolicy(),
        request.oracle_comparison,
        input_fingerprint,
        registry_fingerprint,
        role_fingerprint,
        authority_fingerprint,
        None,
    )
    return provisional


# Short aliases make the contract discoverable without weakening the canonical names.
TaskModeClassifierRequest = TaskModeRetentionRequest
TaskModeClassifierReport = TaskModeRetentionReport
TaskModeClassifierStatus = TaskModeRetentionStatus
FullReferenceTask = FullReferenceTaskType
classify_task_mode_retention = build_task_mode_retention_report
classify_task_mode_and_retention = build_task_mode_retention_report


__all__ = [
    "ACCEPTANCE_THRESHOLD",
    "ABSTENTION_THRESHOLD",
    "CLASSIFIER_ABSTENTION_THRESHOLD",
    "CLASSIFIER_ACCEPT_THRESHOLD",
    "ClassifierCalibrationPolicy",
    "ClassifierDisposition",
    "ClassifierEvidenceSource",
    "FullReferenceTaskType",
    "FullReferenceTask",
    "MAX_CLASSIFIER_ASSETS",
    "MAX_CLASSIFIER_DIAGNOSTICS",
    "MAX_CLASSIFIER_EVIDENCE",
    "MAX_CLASSIFIER_OUTPUT_BYTES",
    "ModeEvidence",
    "OracleComparisonBoundary",
    "OracleComparisonStatus",
    "RetentionDomain",
    "RetentionEvidence",
    "RetentionMarker",
    "RetentionRelation",
    "AudioRetentionMarker",
    "VisualRetentionMarker",
    "TaskModeClassifierReport",
    "TaskModeClassifierRequest",
    "TaskModeClassifierStatus",
    "TaskModeDecision",
    "TaskModeRetentionReport",
    "TaskModeRetentionRequest",
    "TaskModeRetentionStatus",
    "TaskTypeDecision",
    "TaskTypeEvidence",
    "TASK_MODE_RETENTION_SCHEMA",
    "build_task_mode_retention_report",
    "classify_task_mode_retention",
    "classify_task_mode_and_retention",
]
