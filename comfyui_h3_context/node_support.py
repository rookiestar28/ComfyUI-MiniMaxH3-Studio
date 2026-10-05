"""Shared plumbing for the ComfyUI node adapters: the errors they raise and the helpers
more than one of them needs.

Everything here is imported *by* the node modules and imports none of them back.  That one-way
direction is what keeps the adapter layer acyclic, and it is why this module exists at all rather
than the helpers living beside whichever caller happened to need them first.
"""

from __future__ import annotations

from collections.abc import Iterable

from .core import canonical_context_pipeline as _canonical_pipeline
from .core.contracts import (
    ValidationDiagnostic,
    ValidationSeverity,
)
from .core.errors import (
    OfficialContextIRError,
)

_draft_prompt_document = _canonical_pipeline.build_draft_prompt_document
_not_run_validation = _canonical_pipeline.build_not_run_validation
_pipeline_report = _canonical_pipeline.build_pipeline_report
_has_errors = _canonical_pipeline.has_error_diagnostics
_pipeline_digest_id = _canonical_pipeline.pipeline_digest_id

REQUEST_SOCKET_TYPE = "H3_CONTEXT_REQUEST"

REFERENCE_SOCKET_TYPE = "H3_REFERENCE_REGISTRY"

INTENT_GRAPH_SOCKET_TYPE = "H3_INTENT_GRAPH"

FULL_REFERENCE_TIMELINE_SOCKET_TYPE = "H3_FULL_REFERENCE_TIMELINE"

PLAN_SOCKET_TYPE = "H3_CONTEXT_PLAN"

REPORT_SOCKET_TYPE = "H3_CONTEXT_REPORT"

PROMPT_STRING_SOCKET_TYPE = "H3_PROMPT_STRING"

PROMPT_DOCUMENT_SOCKET_TYPE = "H3_PROMPT_DOCUMENT"

VALIDATION_SOCKET_TYPE = "H3_VALIDATION_RESULT"

NATIVE_H3_WIRING_SOCKET_TYPE = "H3_NATIVE_H3_WIRING"

MEDIA_PRODUCER_SOCKET_TYPE = "H3_MEDIA_PRODUCER_RESULT"

UNIFIED_EVIDENCE_GRAPH_SOCKET_TYPE = "H3_UNIFIED_EVIDENCE_GRAPH"

CROSS_REFERENCE_GRAPH_SOCKET_TYPE = "H3_CROSS_REFERENCE_GRAPH"

DIRECTIVE_AUTHORITY_SOCKET_TYPE = "H3_DIRECTIVE_AUTHORITY"

DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE = "H3_DOWNSTREAM_PRODUCER_REPORT"


class RequestNodeError(ValueError):
    """Actionable validation failure raised before a request output is emitted."""

    def __init__(self, diagnostics: Iterable[ValidationDiagnostic]) -> None:
        self.diagnostics = tuple(diagnostics)
        if not self.diagnostics:
            message = "H3 Context Request controls are invalid"
        else:
            details = "; ".join(f"{item.code}: {item.message}" for item in self.diagnostics)
            message = f"H3 Context Request controls are invalid: {details}"
        super().__init__(message)


class ReferenceRegistryNodeError(ValueError):
    """Actionable validation failure raised before a reference registry is emitted."""

    def __init__(self, diagnostics: Iterable[ValidationDiagnostic]) -> None:
        self.diagnostics = tuple(diagnostics)
        if not self.diagnostics:
            message = "H3 Reference Registry inputs are invalid"
        else:
            details = "; ".join(f"{item.code}: {item.message}" for item in self.diagnostics)
            message = f"H3 Reference Registry inputs are invalid: {details}"
        super().__init__(message)


class PipelineNodeError(ValueError):
    """Actionable typed failure shared by the M3-04 plan/compiler/validator nodes."""

    def __init__(self, diagnostics: Iterable[ValidationDiagnostic]) -> None:
        original = tuple(diagnostics)
        self.diagnostics = tuple(
            ValidationDiagnostic(
                severity=item.severity,
                code=item.code,
                message="H3 pipeline validation failed; inspect the diagnostic code.",
                location=None,
            )
            for item in original
        )
        if not self.diagnostics:
            message = "H3 pipeline node execution failed"
        else:
            details = "; ".join(f"{item.code}: {item.message}" for item in self.diagnostics)
            message = f"H3 pipeline node execution failed: {details}"
        super().__init__(message)


class PreviewNodeError(PipelineNodeError):
    """Actionable typed failure raised by the bounded preview adapter."""


class AuditOverrideNodeError(PipelineNodeError):
    """Actionable typed failure raised by the explicit audit override adapter."""


class ProviderTransparencyNodeError(PipelineNodeError):
    """Actionable typed failure raised by the provider disclosure adapter."""


class ReliabilityNodeError(PipelineNodeError):
    """Actionable typed failure raised by the reliability status adapter."""


class NativeH3AdapterNodeError(PipelineNodeError):
    """Actionable typed failure raised by the native-H3 wiring adapter."""


class OfficialContextIRNodeError(OfficialContextIRError):
    """Typed failure raised by the optional official provider node."""


def _pipeline_error(code: str, message: str) -> PipelineNodeError:
    return PipelineNodeError((ValidationDiagnostic(ValidationSeverity.ERROR, code, message),))
