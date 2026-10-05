"""Explicit, revalidated manual prompt overrides for the audit workflow."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from .canonical import canonical_fingerprint, fingerprint_context_report
from .context_reporting import (
    ContextReport,
    PromptDocument,
    PromptRenderStatus,
    PromptSection,
    ProviderOutcome,
    ProviderReceipt,
    ValidationResult,
    ValidationStatus,
)
from .contracts import ProviderIdentity, ValidationDiagnostic, ValidationSeverity
from .errors import (
    AuditOverrideError,
    ContextReportError,
    PromptParseError,
    SidebarWorkspaceError,
)
from .linting import lint_prompt
from .parsing import PromptParseResult, parse_prompt

AUDIT_OVERRIDE_SCHEMA = "h3-context-audit-override/1"
MAX_OVERRIDE_TEXT_LENGTH = 65_536
MAX_OVERRIDE_REASON_LENGTH = 1_024
MAX_OVERRIDE_REVISION = 1_000_000
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_UNSAFE_PATTERN = re.compile(
    r"https?://|file://|authorization|bearer\s+|api[_-]?key|apikey|password|secret|"
    r"token=|sig=|x-amz-|(?:[A-Za-z]:[\\/]|/(?:home|mnt|tmp|var|Users|private|workspace)/)|"
    r"\\\\|(?:^|[\\s])\.\.[\\/](?:[^\\s]*)",
    re.IGNORECASE,
)


def _require_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise AuditOverrideError("invalid_override", f"{field} must be a bounded identifier")
    return value


def _require_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise AuditOverrideError("invalid_override", f"{field} must be non-empty and bounded")
    if any(
        (ord(character) < 0x20 and character not in "\n\r\t")
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise AuditOverrideError("invalid_override", f"{field} contains an unsafe code point")
    return value


def _require_safe_text(value: str, field: str, maximum: int) -> str:
    value = _require_text(value, field, maximum)
    if _UNSAFE_PATTERN.search(value):
        raise AuditOverrideError(
            "unsafe_override",
            f"{field} contains a URL, credential, signed-resource, or private-path marker",
        )
    return value


@dataclass(frozen=True, slots=True)
class AuditOverride:
    """Versioned caller-authored prompt text tied to one exact source report identity."""

    override_id: str
    base_report_fingerprint: str
    revision: int
    reason: str
    prompt_text: str

    def __post_init__(self) -> None:
        _require_identifier(self.override_id, "override_id")
        if (
            not isinstance(self.base_report_fingerprint, str)
            or _FINGERPRINT_PATTERN.fullmatch(self.base_report_fingerprint) is None
        ):
            raise AuditOverrideError(
                "invalid_override", "base_report_fingerprint must be a SHA-256 fingerprint"
            )
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or not 1 <= self.revision <= MAX_OVERRIDE_REVISION
        ):
            raise AuditOverrideError("invalid_override", "revision is outside the supported bounds")
        _require_safe_text(self.reason, "reason", MAX_OVERRIDE_REASON_LENGTH)
        _require_safe_text(self.prompt_text, "prompt_text", MAX_OVERRIDE_TEXT_LENGTH)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": AUDIT_OVERRIDE_SCHEMA,
            "override_id": self.override_id,
            "base_report_fingerprint": self.base_report_fingerprint,
            "revision": self.revision,
            "reason": self.reason,
            "prompt_fingerprint": canonical_fingerprint(self.prompt_text),
        }


def build_audit_override(
    base_report_fingerprint: str,
    revision: int,
    reason: str,
    prompt_text: str,
) -> AuditOverride:
    """Create an opaque override identity from explicit bounded inputs."""

    # Validate all caller values before hashing so malformed inputs never leak a lower-level
    # canonicalization exception through this public contract.
    AuditOverride(
        override_id="override_pending",
        base_report_fingerprint=base_report_fingerprint,
        revision=revision,
        reason=reason,
        prompt_text=prompt_text,
    )
    identity = canonical_fingerprint(
        {
            "base_report_fingerprint": base_report_fingerprint,
            "revision": revision,
            "reason": reason,
            "prompt_text": prompt_text,
        }
    ).split(":", 1)[1][:24]
    return AuditOverride(
        override_id=f"override_{identity}",
        base_report_fingerprint=base_report_fingerprint,
        revision=revision,
        reason=reason,
        prompt_text=prompt_text,
    )


def _sections_from_parse(
    parsed: PromptParseResult,
    source: PromptDocument,
) -> tuple[PromptSection, ...]:
    sections: list[PromptSection] = []
    for index, field in enumerate(parsed.fields, 1):
        if not field.value:
            continue
        order = len(sections) + 1
        sections.append(
            PromptSection(
                section_id=f"manual_{index}_{field.name}",
                order=order,
                heading=field.name,
                body=field.value,
                source_evidence_ids=source.source_evidence_ids,
            )
        )
    if sections:
        return tuple(sections)
    return (
        PromptSection(
            section_id="manual_override_text",
            order=1,
            heading="manual_override_text",
            body=parsed.source_text,
            source_evidence_ids=source.source_evidence_ids,
        ),
    )


def _manual_document(report: ContextReport, override: AuditOverride) -> PromptDocument:
    source = report.prompt_document
    try:
        parsed = parse_prompt(override.prompt_text, source.profile, source.task_mode)
        sections = _sections_from_parse(parsed, source)
        return PromptDocument(
            document_id=f"{source.document_id}.override{override.revision}",
            schema_version=source.schema_version,
            profile=source.profile,
            task_mode=source.task_mode,
            plan_id=source.plan_id,
            text=override.prompt_text,
            sections=sections,
            status=PromptRenderStatus.RENDERED,
            source_evidence_ids=source.source_evidence_ids,
        )
    except (ContextReportError, PromptParseError, TypeError, ValueError) as exc:
        raise AuditOverrideError(
            "invalid_override", "manual prompt text could not be represented as a document"
        ) from exc


def apply_audit_override(report: ContextReport, override: AuditOverride) -> ContextReport:
    """Apply one safe edit and re-run validation without mutating the source report."""

    if not isinstance(report, ContextReport):
        raise AuditOverrideError("invalid_report", "report must be a ContextReport")
    if not isinstance(override, AuditOverride):
        raise AuditOverrideError("invalid_override", "override must be an AuditOverride")
    actual = fingerprint_context_report(report)
    if actual != override.base_report_fingerprint:
        raise AuditOverrideError(
            "stale_report",
            "base_report_fingerprint does not match the report being edited",
        )
    document = _manual_document(report, override)
    try:
        lint_result = lint_prompt(report.plan, document)
        diagnostics = tuple(
            ValidationDiagnostic(
                severity=item.severity,
                code=item.code,
                message=item.message,
                location=item.location,
            )
            for item in lint_result.diagnostics
        )
        validation = ValidationResult(
            validation_id=f"validation_{override.override_id}",
            schema_version=report.schema_version,
            target_id=document.document_id,
            status=ValidationStatus.FAILED if lint_result.has_errors else ValidationStatus.PASSED,
            diagnostics=diagnostics,
            validator_version="h3-context-m7-03",
        )
        # A prior non-manual receipt cannot describe a prompt that has not been sent anywhere.
        receipt = ProviderReceipt(
            receipt_id=f"receipt_{override.override_id}",
            provider=ProviderIdentity.MANUAL,
            outcome=ProviderOutcome.NOT_REQUESTED,
            redacted_message="manual override requires explicit downstream provider selection",
        )
        return replace(
            report,
            report_id=f"report_{override.override_id}",
            revision=override.revision,
            prompt_document=document,
            validation=validation,
            diagnostics=report.diagnostics
            + (
                ValidationDiagnostic(
                    ValidationSeverity.INFO,
                    "audit_override_applied",
                    "manual prompt override applied and revalidated",
                    "audit_override",
                ),
            ),
            provider_receipt=receipt,
        )
    except AuditOverrideError:
        raise
    except (ContextReportError, PromptParseError, TypeError, ValueError) as exc:
        raise AuditOverrideError(
            "override_failed", "manual override validation failed closed"
        ) from exc


def stage_audit_override(
    report: ContextReport,
    *,
    expected_revision: int,
    expected_report_fingerprint: str,
    reason: str,
    prompt_text: str,
) -> ContextReport:
    """Create one immutable unvalidated revision for an explicit sidebar edit/import."""

    # CRITICAL: reject subclasses before any attacker-overridable string operation or hashing.
    if type(report) is not ContextReport:
        raise SidebarWorkspaceError("invalid_report", "exact ContextReport authority is required")
    if type(expected_revision) is not int or not 0 <= expected_revision <= MAX_OVERRIDE_REVISION:
        raise SidebarWorkspaceError("invalid_revision", "expected revision is outside its bound")
    if type(expected_report_fingerprint) is not str:
        raise SidebarWorkspaceError("invalid_identity", "report fingerprint must be exact text")
    if type(reason) is not str or type(prompt_text) is not str:
        raise SidebarWorkspaceError("invalid_action", "edit fields must use exact text values")
    if report.revision != expected_revision:
        raise SidebarWorkspaceError("stale_revision", "report revision changed before the edit")
    actual_fingerprint = fingerprint_context_report(report)
    if actual_fingerprint != expected_report_fingerprint:
        raise SidebarWorkspaceError("stale_report", "report fingerprint changed before the edit")
    next_revision = report.revision + 1
    if next_revision > MAX_OVERRIDE_REVISION:
        raise SidebarWorkspaceError("revision_limit", "report revision limit is exhausted")
    try:
        override = build_audit_override(
            actual_fingerprint,
            next_revision,
            reason,
            prompt_text,
        )
        document = _manual_document(report, override)
        validation = ValidationResult(
            validation_id=f"validation_{override.override_id}",
            schema_version=report.schema_version,
            target_id=document.document_id,
            status=ValidationStatus.NOT_RUN,
            validator_version=None,
        )
        receipt = ProviderReceipt(
            receipt_id=f"receipt_{override.override_id}",
            provider=ProviderIdentity.MANUAL,
            outcome=ProviderOutcome.NOT_REQUESTED,
            redacted_message="manual revision requires explicit backend validation",
        )
        return replace(
            report,
            report_id=f"report_{override.override_id}",
            revision=next_revision,
            prompt_document=document,
            validation=validation,
            diagnostics=report.diagnostics
            + (
                ValidationDiagnostic(
                    ValidationSeverity.INFO,
                    "audit_override_staged",
                    "manual prompt revision staged; backend validation is required",
                    "audit_override",
                ),
            ),
            provider_receipt=receipt,
        )
    except SidebarWorkspaceError:
        raise
    except (AuditOverrideError, ContextReportError, PromptParseError, TypeError, ValueError) as exc:
        code = exc.code if isinstance(exc, AuditOverrideError) else "invalid_action"
        raise SidebarWorkspaceError(code, "manual revision could not be staged") from exc


__all__ = [
    "AUDIT_OVERRIDE_SCHEMA",
    "MAX_OVERRIDE_REASON_LENGTH",
    "MAX_OVERRIDE_REVISION",
    "MAX_OVERRIDE_TEXT_LENGTH",
    "AuditOverride",
    "apply_audit_override",
    "build_audit_override",
    "stage_audit_override",
]
