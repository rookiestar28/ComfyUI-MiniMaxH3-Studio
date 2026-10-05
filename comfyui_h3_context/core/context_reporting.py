"""Versioned plan, prompt-document, provider-receipt, and context-report contracts.

This module is the inspectable hand-off between the normalized request/intent graph and future
renderers or provider adapters.  It stores declarations and redacted outcomes only; it never calls
an external service, rewrites hard constraints, or treats an error as a successful result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar

from .constraints import HardConstraintSet
from .contracts import (
    CURRENT_SCHEMA_VERSION,
    ProfileIdentity,
    PromptProfile,
    ProviderIdentity,
    SchemaVersion,
    TaskMode,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .errors import ContextReportError
from .evidence import EvidenceSet
from .intent_graph import IntentGraph
from .normalization import NormalizedContextRequest

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_TEXT_LENGTH = 65_536
_MAX_SHORT_TEXT_LENGTH = 4096
_MAX_ITEMS = 256
_MAX_IDS = 64


class PlanStage(str, Enum):
    """Ordered pure-core stages represented in a context plan."""

    NORMALIZE = "normalize"
    BIND_REFERENCES = "bind_references"
    ASSEMBLE_INTENT = "assemble_intent"
    RENDER = "render"
    VALIDATE = "validate"
    PROVIDER = "provider"


class PlanStepStatus(str, Enum):
    """Execution state declared by a plan producer; no execution occurs here."""

    PLANNED = "planned"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class PromptRenderStatus(str, Enum):
    """Whether a prompt document is still a draft or contains rendered text."""

    DRAFT = "draft"
    RENDERED = "rendered"


class ValidationStatus(str, Enum):
    """Explicit validation lifecycle states."""

    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"


class ProviderOutcome(str, Enum):
    """Provider terminal outcomes kept distinct for audit and recovery."""

    NOT_REQUESTED = "not_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYABLE_TRANSPORT = "retryable_transport"
    MODERATED = "moderated"
    UNSUPPORTED_MEDIA = "unsupported_media"
    QUOTA = "quota"
    AUTHENTICATION = "authentication"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


_STAGE_ORDER = {stage: index for index, stage in enumerate(PlanStage)}


def _require_enum(value: object, expected: type[Enum], field: str) -> None:
    if not isinstance(value, expected):
        raise ContextReportError(f"{field} must be a {expected.__name__}")


def _require_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ContextReportError(f"{field} must be a bounded identifier")
    return value


def _require_code(value: object, field: str) -> str:
    if not isinstance(value, str) or _CODE_PATTERN.fullmatch(value) is None:
        raise ContextReportError(f"{field} must be a bounded lower-case code")
    return value


def _require_text(value: object, field: str, maximum: int = _MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ContextReportError(f"{field} must be a non-empty bounded string")
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ContextReportError(f"{field} contains an unsafe wire code point")
    return value


def _optional_text(value: object, field: str, maximum: int = _MAX_SHORT_TEXT_LENGTH) -> str | None:
    if value is None:
        return None
    return _require_text(value, field, maximum)


def _require_id_tuple(values: object, field: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > _MAX_IDS:
        raise ContextReportError(f"{field} must be a tuple of at most {_MAX_IDS} identifiers")
    result = tuple(_require_identifier(value, f"{field} item") for value in values)
    if len(result) != len(set(result)):
        raise ContextReportError(f"{field} must not contain duplicate identifiers")
    return result


T = TypeVar("T")


def _require_tuple(
    values: object, expected: type[T], field: str, maximum: int = _MAX_ITEMS
) -> tuple[T, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise ContextReportError(f"{field} must be a tuple of at most {maximum} values")
    if not all(isinstance(value, expected) for value in values):
        raise ContextReportError(f"{field} contains an invalid value")
    return values


def _require_schema_version(value: object, field: str) -> SchemaVersion:
    if not isinstance(value, SchemaVersion) or value != CURRENT_SCHEMA_VERSION:
        raise ContextReportError(f"{field} must use schema version {CURRENT_SCHEMA_VERSION}")
    return value


def _require_safe_metadata(value: str, field: str) -> str:
    """Reject values that could smuggle credentials, signed URLs, or raw private resources."""

    lowered = value.lower()
    forbidden = (
        "http://",
        "https://",
        "authorization",
        "bearer ",
        "api_key",
        "apikey",
        "password",
        "secret",
        "token=",
        "sig=",
        "x-amz-",
        "-----begin",
        "\\",
    )
    if any(marker in lowered for marker in forbidden) or "/" in value:
        raise ContextReportError(f"{field} must contain only redacted metadata")
    return value


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One ordered, inspectable planning stage with stable evidence references."""

    step_id: str
    stage: PlanStage
    status: PlanStepStatus
    description: str
    input_evidence_ids: tuple[str, ...] = ()
    output_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.step_id, "plan step_id")
        _require_enum(self.stage, PlanStage, "plan stage")
        _require_enum(self.status, PlanStepStatus, "plan step status")
        _require_text(self.description, "plan step description", _MAX_SHORT_TEXT_LENGTH)
        _require_id_tuple(self.input_evidence_ids, "plan step input_evidence_ids")
        _require_id_tuple(self.output_ids, "plan step output_ids")

    def to_wire(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "stage": self.stage.value,
            "status": self.status.value,
            "description": self.description,
            "input_evidence_ids": list(self.input_evidence_ids),
            "output_ids": list(self.output_ids),
        }


@dataclass(frozen=True, slots=True)
class Limitation:
    """A visible bounded limitation, distinct from a structural validation diagnostic."""

    limitation_id: str
    code: str
    message: str
    severity: ValidationSeverity = ValidationSeverity.WARNING
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.limitation_id, "limitation_id")
        _require_code(self.code, "limitation code")
        _require_text(self.message, "limitation message", _MAX_SHORT_TEXT_LENGTH)
        _require_enum(self.severity, ValidationSeverity, "limitation severity")
        _require_id_tuple(self.evidence_ids, "limitation evidence_ids")

    def to_wire(self) -> dict[str, object]:
        return {
            "limitation_id": self.limitation_id,
            "code": self.code,
            "message": self.message,
            "severity": self.severity.value,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class ContextPlan:
    """Validated pure-core plan joining request, intent graph, evidence, and stages."""

    plan_id: str
    schema_version: SchemaVersion
    request: NormalizedContextRequest
    intent_graph: IntentGraph
    hard_constraints: HardConstraintSet
    evidence: EvidenceSet
    steps: tuple[PlanStep, ...]
    limitations: tuple[Limitation, ...] = ()
    diagnostics: tuple[ValidationDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.plan_id, "plan_id")
        _require_schema_version(self.schema_version, "plan schema_version")
        if not isinstance(self.request, NormalizedContextRequest):
            raise ContextReportError("plan request must be a NormalizedContextRequest")
        if not isinstance(self.intent_graph, IntentGraph):
            raise ContextReportError("plan intent_graph must be an IntentGraph")
        if not isinstance(self.hard_constraints, HardConstraintSet):
            raise ContextReportError("plan hard_constraints must be a HardConstraintSet")
        if self.hard_constraints != self.request.hard_constraints:
            raise ContextReportError("plan hard_constraints do not match the normalized request")
        if not isinstance(self.evidence, EvidenceSet):
            raise ContextReportError("plan evidence must be an EvidenceSet")
        if self.evidence != self.request.evidence:
            raise ContextReportError("plan evidence does not match the normalized request")
        if self.intent_graph.registry.to_asset_descriptors() != self.request.assets:
            raise ContextReportError("plan graph registry does not match normalized request assets")
        _require_tuple(self.steps, PlanStep, "plan steps")
        _require_tuple(self.limitations, Limitation, "plan limitations")
        _require_tuple(self.diagnostics, ValidationDiagnostic, "plan diagnostics")
        from .dialogue_speakers import validate_dialogue_bindings

        binding_codes = {"unknown_speaker_subject", "unknown_dialogue_segment"}
        object.__setattr__(
            self,
            "diagnostics",
            tuple(value for value in self.diagnostics if value.code not in binding_codes)
            + validate_dialogue_bindings(self),
        )
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ContextReportError("plan step IDs must be unique")
        limitation_ids = [item.limitation_id for item in self.limitations]
        if len(limitation_ids) != len(set(limitation_ids)):
            raise ContextReportError("plan limitation IDs must be unique")
        previous_stage: int | None = None
        evidence_ids = {record.evidence_id for record in self.evidence.records}
        for step in self.steps:
            stage_index = _STAGE_ORDER[step.stage]
            if previous_stage is not None and stage_index < previous_stage:
                raise ContextReportError("plan steps must be ordered by stage")
            previous_stage = stage_index
            if not set(step.input_evidence_ids).issubset(evidence_ids):
                raise ContextReportError("plan step references unknown evidence")
        for limitation in self.limitations:
            if not set(limitation.evidence_ids).issubset(evidence_ids):
                raise ContextReportError("plan limitation references unknown evidence")

    def to_wire(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "schema_version": str(self.schema_version),
            "request": self.request.to_wire(),
            "intent_graph": self.intent_graph.to_wire(),
            "hard_constraints": self.hard_constraints.to_wire(),
            "evidence": self.evidence.to_wire(),
            "steps": [step.to_wire() for step in self.steps],
            "limitations": [item.to_wire() for item in self.limitations],
            "diagnostics": [diagnostic.to_wire() for diagnostic in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class PromptSection:
    """One ordered inspectable region of a prompt document."""

    section_id: str
    order: int
    heading: str
    body: str
    source_evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.section_id, "prompt section_id")
        if (
            isinstance(self.order, bool)
            or not isinstance(self.order, int)
            or not 1 <= self.order <= _MAX_ITEMS
        ):
            raise ContextReportError("prompt section order must be between 1 and 256")
        _require_text(self.heading, "prompt section heading", _MAX_SHORT_TEXT_LENGTH)
        _require_text(self.body, "prompt section body")
        _require_id_tuple(self.source_evidence_ids, "prompt section source_evidence_ids")

    def to_wire(self) -> dict[str, object]:
        return {
            "section_id": self.section_id,
            "order": self.order,
            "heading": self.heading,
            "body": self.body,
            "source_evidence_ids": list(self.source_evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class PromptDocument:
    """Versioned exact prompt text with an inspectable ordered section vector."""

    document_id: str
    schema_version: SchemaVersion
    profile: ProfileIdentity
    task_mode: TaskMode
    plan_id: str
    text: str
    sections: tuple[PromptSection, ...]
    status: PromptRenderStatus = PromptRenderStatus.DRAFT
    source_evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.document_id, "prompt document_id")
        _require_schema_version(self.schema_version, "prompt schema_version")
        if not isinstance(self.profile, ProfileIdentity):
            raise ContextReportError("prompt profile must be a ProfileIdentity")
        _require_enum(self.task_mode, TaskMode, "prompt task_mode")
        if self.profile.name is PromptProfile.BASE and self.task_mode is TaskMode.REF2VA:
            raise ContextReportError("the h3_base profile does not support ref2va")
        if (
            self.profile.name is PromptProfile.FULL_REFERENCE
            and self.task_mode is not TaskMode.REF2VA
        ):
            raise ContextReportError("the h3_full_reference profile only supports ref2va")
        _require_identifier(self.plan_id, "prompt plan_id")
        _require_text(self.text, "prompt text")
        _require_tuple(self.sections, PromptSection, "prompt sections")
        _require_enum(self.status, PromptRenderStatus, "prompt render status")
        _require_id_tuple(self.source_evidence_ids, "prompt source_evidence_ids")
        if not self.sections:
            raise ContextReportError("prompt document requires at least one section")
        section_ids = [section.section_id for section in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ContextReportError("prompt section IDs must be unique")
        if [section.order for section in self.sections] != list(range(1, len(self.sections) + 1)):
            raise ContextReportError("prompt sections must use contiguous declared order")

    def to_wire(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "schema_version": str(self.schema_version),
            "profile": self.profile.to_wire(),
            "task_mode": self.task_mode.value,
            "plan_id": self.plan_id,
            "text": self.text,
            "sections": [section.to_wire() for section in self.sections],
            "status": self.status.value,
            "source_evidence_ids": list(self.source_evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Validation state whose status cannot contradict its error diagnostics."""

    validation_id: str
    schema_version: SchemaVersion
    target_id: str
    status: ValidationStatus
    diagnostics: tuple[ValidationDiagnostic, ...] = ()
    validator_version: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.validation_id, "validation_id")
        _require_schema_version(self.schema_version, "validation schema_version")
        _require_identifier(self.target_id, "validation target_id")
        _require_enum(self.status, ValidationStatus, "validation status")
        _require_tuple(self.diagnostics, ValidationDiagnostic, "validation diagnostics")
        _optional_text(self.validator_version, "validator_version", 256)
        has_error = any(
            diagnostic.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
            for diagnostic in self.diagnostics
        )
        if self.status is ValidationStatus.PASSED and has_error:
            raise ContextReportError("passed validation cannot contain error diagnostics")
        if self.status is ValidationStatus.FAILED and not has_error:
            raise ContextReportError("failed validation requires an error diagnostic")
        if self.status is ValidationStatus.NOT_RUN and has_error:
            raise ContextReportError("not_run validation cannot contain error diagnostics")

    @property
    def is_valid(self) -> bool:
        return self.status is ValidationStatus.PASSED

    def to_wire(self) -> dict[str, object]:
        return {
            "validation_id": self.validation_id,
            "schema_version": str(self.schema_version),
            "target_id": self.target_id,
            "status": self.status.value,
            "diagnostics": [diagnostic.to_wire() for diagnostic in self.diagnostics],
            "validator_version": self.validator_version,
        }


@dataclass(frozen=True, slots=True)
class ProviderReceipt:
    """Redacted provider lifecycle receipt with no representable credential or URL fields."""

    receipt_id: str
    provider: ProviderIdentity
    outcome: ProviderOutcome
    provider_version: str | None = None
    endpoint_revision: str | None = None
    task_id: str | None = None
    input_fingerprint: str | None = None
    output_fingerprint: str | None = None
    redacted_message: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.receipt_id, "receipt_id")
        _require_enum(self.provider, ProviderIdentity, "receipt provider")
        _require_enum(self.outcome, ProviderOutcome, "provider outcome")
        for value, field in (
            (self.provider_version, "provider_version"),
            (self.endpoint_revision, "endpoint_revision"),
        ):
            if value is not None:
                _require_safe_metadata(_require_text(value, field, 256), field)
        if self.provider is not ProviderIdentity.MANUAL and (
            self.provider_version is None or self.endpoint_revision is None
        ):
            raise ContextReportError(
                "non-manual provider receipts require provider_version and endpoint_revision"
            )
        if self.task_id is not None:
            _require_identifier(self.task_id, "provider task_id")
        for value, field in (
            (self.input_fingerprint, "input_fingerprint"),
            (self.output_fingerprint, "output_fingerprint"),
        ):
            if value is not None and _SHA256_PATTERN.fullmatch(value) is None:
                raise ContextReportError(f"{field} must be a lowercase SHA-256 fingerprint")
        if self.redacted_message is not None:
            _require_safe_metadata(
                _require_text(self.redacted_message, "redacted_message", _MAX_SHORT_TEXT_LENGTH),
                "redacted_message",
            )
        if (
            self.provider is ProviderIdentity.MANUAL
            and self.outcome is not ProviderOutcome.NOT_REQUESTED
        ):
            raise ContextReportError("manual provider receipts may only be not_requested")

    @property
    def is_successful(self) -> bool:
        return self.outcome in {ProviderOutcome.SUCCEEDED, ProviderOutcome.NOT_REQUESTED}

    def to_wire(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "provider": self.provider.value,
            "outcome": self.outcome.value,
            "provider_version": self.provider_version,
            "endpoint_revision": self.endpoint_revision,
            "task_id": self.task_id,
            "input_fingerprint": self.input_fingerprint,
            "output_fingerprint": self.output_fingerprint,
            "redacted_message": self.redacted_message,
        }


@dataclass(frozen=True, slots=True)
class ContextReport:
    """Complete inspectable context envelope; success is never inferred from partial state."""

    report_id: str
    schema_version: SchemaVersion
    request: NormalizedContextRequest
    plan: ContextPlan
    prompt_document: PromptDocument
    validation: ValidationResult
    evidence: EvidenceSet
    limitations: tuple[Limitation, ...] = ()
    diagnostics: tuple[ValidationDiagnostic, ...] = ()
    provider_receipt: ProviderReceipt | None = None
    revision: int = 0

    def __post_init__(self) -> None:
        _require_identifier(self.report_id, "report_id")
        _require_schema_version(self.schema_version, "report schema_version")
        if not isinstance(self.request, NormalizedContextRequest):
            raise ContextReportError("report request must be a NormalizedContextRequest")
        if not isinstance(self.plan, ContextPlan):
            raise ContextReportError("report plan must be a ContextPlan")
        if not isinstance(self.prompt_document, PromptDocument):
            raise ContextReportError("report prompt_document must be a PromptDocument")
        if not isinstance(self.validation, ValidationResult):
            raise ContextReportError("report validation must be a ValidationResult")
        if not isinstance(self.evidence, EvidenceSet):
            raise ContextReportError("report evidence must be an EvidenceSet")
        if self.plan.request != self.request:
            raise ContextReportError("report request does not match plan request")
        if self.plan.evidence != self.evidence or self.request.evidence != self.evidence:
            raise ContextReportError("report evidence does not match request and plan evidence")
        if self.prompt_document.profile != self.request.profile:
            raise ContextReportError("prompt profile does not match request profile")
        if self.prompt_document.task_mode is not self.request.task_mode:
            raise ContextReportError("prompt task mode does not match request task mode")
        if self.prompt_document.plan_id != self.plan.plan_id:
            raise ContextReportError("prompt document does not belong to report plan")
        _require_tuple(self.limitations, Limitation, "report limitations")
        _require_tuple(self.diagnostics, ValidationDiagnostic, "report diagnostics")
        limitation_ids = [item.limitation_id for item in self.limitations]
        if len(limitation_ids) != len(set(limitation_ids)):
            raise ContextReportError("report limitation IDs must be unique")
        plan_limitation_ids = {item.limitation_id for item in self.plan.limitations}
        if not plan_limitation_ids.issubset(set(limitation_ids)):
            raise ContextReportError("report omits a plan limitation")
        evidence_ids = {record.evidence_id for record in self.evidence.records}
        prompt_ids = set(self.prompt_document.source_evidence_ids)
        for section in self.prompt_document.sections:
            prompt_ids.update(section.source_evidence_ids)
        if not prompt_ids.issubset(evidence_ids):
            raise ContextReportError("prompt document references unknown evidence")
        for limitation in self.limitations:
            if not set(limitation.evidence_ids).issubset(evidence_ids):
                raise ContextReportError("report limitation references unknown evidence")
        if self.provider_receipt is not None and not isinstance(
            self.provider_receipt, ProviderReceipt
        ):
            raise ContextReportError("provider_receipt must be a ProviderReceipt or None")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or not 0 <= self.revision <= 1_000_000
        ):
            raise ContextReportError("report revision must be an integer between 0 and 1000000")

    @property
    def has_errors(self) -> bool:
        report_errors = any(
            diagnostic.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
            for diagnostic in self.diagnostics
        )
        limitation_errors = any(
            limitation.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
            for limitation in self.limitations
        )
        validation_errors = self.validation.status is ValidationStatus.FAILED
        return report_errors or limitation_errors or validation_errors

    @property
    def is_successful(self) -> bool:
        return (
            not self.has_errors
            and self.prompt_document.status is PromptRenderStatus.RENDERED
            and (self.provider_receipt is None or self.provider_receipt.is_successful)
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "report_id": self.report_id,
            "schema_version": str(self.schema_version),
            "request": self.request.to_wire(),
            "plan": self.plan.to_wire(),
            "prompt_document": self.prompt_document.to_wire(),
            "validation": self.validation.to_wire(),
            "evidence": self.evidence.to_wire(),
            "limitations": [item.to_wire() for item in self.limitations],
            "diagnostics": [diagnostic.to_wire() for diagnostic in self.diagnostics],
            "provider_receipt": (
                None if self.provider_receipt is None else self.provider_receipt.to_wire()
            ),
        }


__all__ = [
    "ContextPlan",
    "ContextReport",
    "Limitation",
    "PlanStage",
    "PlanStep",
    "PlanStepStatus",
    "PromptDocument",
    "PromptRenderStatus",
    "PromptSection",
    "ProviderOutcome",
    "ProviderReceipt",
    "ValidationResult",
    "ValidationStatus",
]
