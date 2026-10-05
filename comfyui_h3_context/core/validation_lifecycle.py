"""Canonical compile-to-validate lifecycle and execution admission gate.

The compiler deliberately emits a ``NOT_RUN`` report.  This module is the only pure-core bridge
that turns that draft lifecycle state into a report carrying a concrete validation result.  Native
and provider adapters can consume the resulting report or envelope, but neither may infer
validation from a prompt string or from a stale compiler object.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum

from .canonical import canonical_fingerprint, fingerprint_context_report
from .context_reporting import (
    ContextReport,
    PromptRenderStatus,
    ValidationResult,
    ValidationStatus,
)
from .contracts import (
    CURRENT_SCHEMA_VERSION,
    SchemaVersion,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .errors import ContextReportError, PromptLintError, ReportLifecycleError
from .linting import lint_prompt
from .prompt_fidelity import audit_prompt_fidelity

VALIDATION_LIFECYCLE_SCHEMA = "h3.context.validation.lifecycle.v1"
VALIDATOR_VERSION = "h3-context-m10-05"
MAX_REPORT_REVISION = 1_000_000
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


class LifecycleState(str, Enum):
    """Terminal report/execution states exposed by the bounded lifecycle boundary."""

    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    STALE = "stale"


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ReportLifecycleError("invalid_identity", f"{field} must be a bounded identifier")
    return value


def _fingerprint(value: object, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ReportLifecycleError("invalid_identity", f"{field} must be a SHA-256 fingerprint")
    return value


def _revision(value: object, field: str = "report_revision") -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_REPORT_REVISION
    ):
        raise ReportLifecycleError(
            "invalid_revision", f"{field} must be between 0 and {MAX_REPORT_REVISION}"
        )
    return value


def _failed_validation(report: ContextReport, message: str) -> ValidationResult:
    diagnostic = ValidationDiagnostic(
        ValidationSeverity.ERROR,
        "validation_failed",
        message,
    )
    return ValidationResult(
        validation_id=f"validation_{report.prompt_document.document_id}",
        schema_version=report.schema_version,
        target_id=report.prompt_document.document_id,
        status=ValidationStatus.FAILED,
        diagnostics=(diagnostic,),
        validator_version=VALIDATOR_VERSION,
    )


@dataclass(frozen=True, slots=True)
class ValidatedReportEnvelope:
    """Versioned canonical report hand-off for every native/provider execution path."""

    report: ContextReport
    report_revision: int
    report_fingerprint: str
    prompt_fingerprint: str
    status: ValidationStatus
    schema: str = VALIDATION_LIFECYCLE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != VALIDATION_LIFECYCLE_SCHEMA:
            raise ReportLifecycleError(
                "unsupported_schema", "validation lifecycle schema is unsupported"
            )
        if not isinstance(self.report, ContextReport):
            raise ReportLifecycleError(
                "invalid_report", "validated envelope requires a ContextReport"
            )
        _revision(self.report_revision)
        if self.report_revision != self.report.revision:
            raise ReportLifecycleError(
                "revision_mismatch", "envelope revision does not match report"
            )
        _fingerprint(self.report_fingerprint, "report_fingerprint")
        _fingerprint(self.prompt_fingerprint, "prompt_fingerprint")
        if self.report_fingerprint != fingerprint_context_report(self.report):
            raise ReportLifecycleError(
                "fingerprint_mismatch", "envelope report fingerprint is stale"
            )
        if self.prompt_fingerprint != canonical_fingerprint(self.report.prompt_document.text):
            raise ReportLifecycleError(
                "fingerprint_mismatch", "envelope prompt fingerprint is stale"
            )
        if not isinstance(self.status, ValidationStatus) or self.status is ValidationStatus.NOT_RUN:
            raise ReportLifecycleError(
                "validation_not_run", "envelope requires a terminal validation status"
            )
        if self.status is not self.report.validation.status:
            raise ReportLifecycleError(
                "validation_mismatch", "envelope status does not match report"
            )

    @classmethod
    def from_report(cls, report: ContextReport) -> ValidatedReportEnvelope:
        if not isinstance(report, ContextReport):
            raise ReportLifecycleError("invalid_report", "validation requires a ContextReport")
        if report.validation.status is ValidationStatus.NOT_RUN:
            raise ReportLifecycleError(
                "validation_not_run", "report has not passed through Validator"
            )
        return cls(
            report=report,
            report_revision=report.revision,
            report_fingerprint=fingerprint_context_report(report),
            prompt_fingerprint=canonical_fingerprint(report.prompt_document.text),
            status=report.validation.status,
        )

    @property
    def is_execution_ready(self) -> bool:
        return self.status is ValidationStatus.PASSED and not self.report.has_errors

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "report_id": self.report.report_id,
            "report_revision": self.report_revision,
            "report_fingerprint": self.report_fingerprint,
            "prompt_fingerprint": self.prompt_fingerprint,
            "status": self.status.value,
            "report": self.report.to_wire(),
        }


def validate_context_report(
    report: ContextReport,
    *,
    validator_version: str = VALIDATOR_VERSION,
) -> ValidatedReportEnvelope:
    """Run the deterministic linter and return one canonical report/envelope."""

    if not isinstance(report, ContextReport):
        raise ReportLifecycleError("invalid_report", "validation requires a ContextReport")
    if not isinstance(validator_version, str) or not validator_version:
        raise ReportLifecycleError("invalid_validator", "validator version is required")
    try:
        lint_result = lint_prompt(report.plan, report.prompt_document)
        # M21-01. The fidelity audit is a pure function of the same pair, so the rich diagnostics --
        # stable identity plus typed parameters -- stay reachable by calling it directly. What joins
        # the validation stream here is the projection every existing consumer already understands.
        # Only non-informational findings are folded in: the description length band is a graded
        # quality signal that must not decide whether a prompt may run, and adding it would put an
        # advisory diagnostic on every report without changing what anyone may do with it.
        fidelity = audit_prompt_fidelity(report.plan, report.prompt_document)
        diagnostics = tuple(
            ValidationDiagnostic(
                severity=item.severity,
                code=item.code,
                message=item.message,
                location=item.location,
            )
            for item in lint_result.diagnostics
        ) + tuple(
            ValidationDiagnostic(
                severity=item.severity,
                code=item.diagnostic_id.value,
                message=item.message,
                location=item.location,
            )
            for item in fidelity.blocking
        )
        failed = lint_result.has_errors or any(
            item.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
            for item in fidelity.blocking
        )
        validation = ValidationResult(
            validation_id=f"validation_{report.prompt_document.document_id}",
            schema_version=report.schema_version,
            target_id=report.prompt_document.document_id,
            status=ValidationStatus.FAILED if failed else ValidationStatus.PASSED,
            diagnostics=diagnostics,
            validator_version=validator_version,
        )
    except (ContextReportError, PromptLintError, TypeError, ValueError):
        validation = _failed_validation(report, "prompt validation failed closed")
    validated = replace(report, validation=validation)
    return ValidatedReportEnvelope.from_report(validated)


def require_execution_ready(
    value: ContextReport | ValidatedReportEnvelope,
    *,
    expected_revision: int | None = None,
    expected_report_fingerprint: str | None = None,
    expected_schema: SchemaVersion = CURRENT_SCHEMA_VERSION,
) -> ContextReport:
    """Admit only a current, passed, error-free report to native/provider execution."""

    report = value.report if isinstance(value, ValidatedReportEnvelope) else value
    if not isinstance(report, ContextReport):
        raise ReportLifecycleError(
            "invalid_report", "execution requires a ContextReport or envelope"
        )
    if report.schema_version != expected_schema:
        raise ReportLifecycleError(
            "schema_mismatch", "report schema does not match the execution contract"
        )
    if isinstance(value, ValidatedReportEnvelope):
        if value.report is not report:
            raise ReportLifecycleError("stale_report", "envelope does not own the supplied report")
        if (
            value.report_revision != report.revision
            or value.report_fingerprint != fingerprint_context_report(report)
        ):
            raise ReportLifecycleError("stale_report", "validated envelope identity is stale")
        if not value.is_execution_ready:
            if value.status is ValidationStatus.FAILED:
                raise ReportLifecycleError(
                    "validation_failed", "validated envelope contains failed validation"
                )
            raise ReportLifecycleError(
                "validation_not_run", "validated envelope is not execution-ready"
            )
    if report.validation.status is ValidationStatus.NOT_RUN:
        raise ReportLifecycleError(
            "validation_not_run", "native/provider execution requires Validator output"
        )
    if report.validation.status is ValidationStatus.FAILED:
        raise ReportLifecycleError(
            "validation_failed", "native/provider execution refuses failed validation"
        )
    if report.validation.schema_version is not report.schema_version:
        raise ReportLifecycleError(
            "schema_mismatch", "validation schema does not match report schema"
        )
    if report.validation.target_id != report.prompt_document.document_id:
        raise ReportLifecycleError(
            "stale_report", "validation target does not match prompt document"
        )
    if report.prompt_document.status is not PromptRenderStatus.RENDERED:
        raise ReportLifecycleError("prompt_not_rendered", "execution requires a rendered prompt")
    if report.has_errors:
        raise ReportLifecycleError("report_has_errors", "execution refuses a report with errors")
    if report.provider_receipt is not None and not report.provider_receipt.is_successful:
        raise ReportLifecycleError(
            "provider_receipt_failed", "execution refuses an unsuccessful provider receipt"
        )
    if expected_revision is not None and report.revision != _revision(
        expected_revision, "expected_revision"
    ):
        raise ReportLifecycleError(
            "stale_revision", "report revision does not match the expected revision"
        )
    if expected_report_fingerprint is not None:
        if _fingerprint(
            expected_report_fingerprint, "expected_report_fingerprint"
        ) != fingerprint_context_report(report):
            raise ReportLifecycleError(
                "stale_report", "report fingerprint does not match the expected identity"
            )
    return report


__all__ = [
    "LifecycleState",
    "MAX_REPORT_REVISION",
    "VALIDATION_LIFECYCLE_SCHEMA",
    "VALIDATOR_VERSION",
    "ValidatedReportEnvelope",
    "require_execution_ready",
    "validate_context_report",
]
