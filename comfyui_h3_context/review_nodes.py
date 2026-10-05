"""Node adapters a person reads: the preview surface and the audit override.

Both exist so a human can inspect what the pipeline produced and, where the profile allows it,
deliberately overrule it.  An override is recorded as an override; it is never silent.
"""

from __future__ import annotations

from .core.audit_override import (
    AuditOverride,
    apply_audit_override,
    build_audit_override,
)
from .core.context_reporting import (
    ContextReport,
    PromptDocument,
)
from .core.contracts import (
    ValidationDiagnostic,
    ValidationSeverity,
)
from .core.errors import (
    AuditOverrideError,
    ContextReportError,
    ReportLifecycleError,
)
from .core.preview import ContextPreview, ContextPreviewError, build_context_preview
from .core.ui_projection import ExecutionCorrelation, UIEventState, build_ui_projection
from .core.validation_lifecycle import ValidatedReportEnvelope
from .node_support import (
    PROMPT_DOCUMENT_SOCKET_TYPE,
    PROMPT_STRING_SOCKET_TYPE,
    AuditOverrideNodeError,
    PreviewNodeError,
)
from .product_shell_node import (
    REPORT_SOCKET_TYPE,
)

PREVIEW_NODE_ID = "comfyui_h3_context.H3Context.Preview"

PREVIEW_DISPLAY_NAME = "H3 Context Preview"

PREVIEW_SOCKET_TYPE = "H3_CONTEXT_PREVIEW"

AUDIT_OVERRIDE_NODE_ID = "comfyui_h3_context.H3Context.AuditOverride"

AUDIT_OVERRIDE_DISPLAY_NAME = "H3 Context Audit Override"

AUDIT_OVERRIDE_SOCKET_TYPE = "H3_AUDIT_OVERRIDE"


class H3ContextPreviewNode:
    """Expose a bounded redacted audit projection without mutating the source report."""

    NODE_ID = PREVIEW_NODE_ID
    __h3_context_node_id__ = PREVIEW_NODE_ID
    RETURN_TYPES = (PROMPT_STRING_SOCKET_TYPE, PREVIEW_SOCKET_TYPE)
    RETURN_NAMES = ("prompt", "preview")
    FUNCTION = "emit"
    CATEGORY = "h3_context/audit"
    DESCRIPTION = "Shows a bounded redacted context audit projection and the source prompt."
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "report": (
                    REPORT_SOCKET_TYPE,
                    {"tooltip": "Inspectable typed context report from Plan or Compiler."},
                ),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, report: object) -> bool | str:
        # ComfyUI supplies ``None`` for a linked report during prompt pre-validation.
        if report is None:
            return True
        try:
            cls().preview(report)
        except PreviewNodeError as exc:
            return str(exc)
        return True

    def preview(self, report: object) -> tuple[str, ContextPreview]:
        if not isinstance(report, ContextReport):
            raise PreviewNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "invalid_report",
                        "report must be a ContextReport",
                    ),
                )
            )
        try:
            preview = build_context_preview(report)
        except (ContextPreviewError, ContextReportError, TypeError, ValueError) as exc:
            raise PreviewNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "preview_failed",
                        "context preview construction failed closed",
                    ),
                )
            ) from exc
        return report.prompt_document.text, preview

    def emit(
        self,
        report: object,
        prompt_id: object | None = None,
        execution_node_id: object | None = None,
        event_state: object = UIEventState.VALIDATED.value,
    ) -> dict[str, object]:
        """Emit ComfyUI's terminal ``ui``/``result`` response with a bounded projection."""

        source: ContextReport | ValidatedReportEnvelope
        if isinstance(report, (ContextReport, ValidatedReportEnvelope)):
            source = report
        else:
            raise PreviewNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "invalid_report",
                        "report must be a ContextReport",
                    ),
                )
            )
        if prompt_id is None or execution_node_id is None:
            try:
                from .adapters.comfyui_execution import current_execution_correlation

                host_correlation = current_execution_correlation()
            except (ImportError, TypeError, ValueError):
                host_correlation = None
            if host_correlation is not None:
                prompt_id, execution_node_id = host_correlation
        if not isinstance(prompt_id, str) or not isinstance(execution_node_id, str):
            raise PreviewNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "missing_correlation",
                        "terminal preview requires host prompt and execution-node IDs",
                    ),
                )
            )
        try:
            correlation = ExecutionCorrelation(prompt_id, execution_node_id)
            state = UIEventState(event_state)
            source_report = source.report if isinstance(source, ValidatedReportEnvelope) else source
            prompt, preview = self.preview(source_report)
            projection = build_ui_projection(source, correlation, state)
        except (
            ContextPreviewError,
            ContextReportError,
            ReportLifecycleError,
            TypeError,
            ValueError,
        ) as exc:
            code = exc.code if isinstance(exc, ReportLifecycleError) else "preview_failed"
            raise PreviewNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        code,
                        "terminal preview emission failed closed",
                    ),
                )
            ) from exc
        return {"ui": projection.to_ui(), "result": (prompt, preview)}


class H3ContextAuditOverrideNode:
    """Apply one explicit safe prompt edit and revalidate it against the source plan."""

    NODE_ID = AUDIT_OVERRIDE_NODE_ID
    __h3_context_node_id__ = AUDIT_OVERRIDE_NODE_ID
    RETURN_TYPES = (
        PROMPT_STRING_SOCKET_TYPE,
        REPORT_SOCKET_TYPE,
        AUDIT_OVERRIDE_SOCKET_TYPE,
        PROMPT_DOCUMENT_SOCKET_TYPE,
    )
    RETURN_NAMES = ("edited_prompt", "updated_report", "override", "prompt_document")
    FUNCTION = "apply"
    CATEGORY = "h3_context/audit"
    DESCRIPTION = (
        "Applies an explicit versioned prompt edit, preserves the typed plan, and re-runs "
        "validation without provider execution."
    )
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "report": (
                    REPORT_SOCKET_TYPE,
                    {"tooltip": "Source report shown by H3 Context Preview."},
                ),
                "base_report_fingerprint": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Exact report fingerprint from the audit preview; stale edits fail."
                        ),
                    },
                ),
                "revision": (
                    "INT",
                    {
                        "default": 1,
                        "min": 1,
                        "max": 1_000_000,
                        "step": 1,
                        "tooltip": "Positive operator revision for this manual edit.",
                    },
                ),
                "reason": (
                    "STRING",
                    {
                        "default": "Operator-approved audit edit",
                        "tooltip": "Why the manual edit is being made; no credentials or URLs.",
                    },
                ),
                "prompt_text": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": True,
                        "dynamicPrompts": False,
                        "tooltip": (
                            "Exact caller-authored prompt text; it is revalidated unchanged."
                        ),
                    },
                ),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(
        cls,
        report: object,
        base_report_fingerprint: object,
        revision: object,
        reason: object,
        prompt_text: object,
    ) -> bool | str:
        if report is None:
            return True
        try:
            cls().apply(report, base_report_fingerprint, revision, reason, prompt_text)
        except AuditOverrideNodeError as exc:
            return str(exc)
        return True

    def apply(
        self,
        report: object,
        base_report_fingerprint: object,
        revision: object,
        reason: object,
        prompt_text: object,
    ) -> tuple[str, ContextReport, AuditOverride, PromptDocument]:
        if not isinstance(report, ContextReport):
            raise AuditOverrideNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "invalid_report",
                        "report must be a ContextReport",
                    ),
                )
            )
        if not isinstance(base_report_fingerprint, str):
            raise AuditOverrideNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "invalid_override",
                        "base_report_fingerprint must be a string",
                    ),
                )
            )
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise AuditOverrideNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "invalid_override",
                        "revision must be an integer",
                    ),
                )
            )
        if not isinstance(reason, str) or not isinstance(prompt_text, str):
            raise AuditOverrideNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "invalid_override",
                        "reason and prompt_text must be strings",
                    ),
                )
            )
        try:
            override = build_audit_override(
                base_report_fingerprint,
                revision,
                reason,
                prompt_text,
            )
            updated = apply_audit_override(report, override)
        except AuditOverrideError as exc:
            raise AuditOverrideNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        exc.code,
                        "audit override failed closed",
                    ),
                )
            ) from exc
        except (ContextReportError, TypeError, ValueError) as exc:
            raise AuditOverrideNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "override_failed",
                        "audit override failed closed",
                    ),
                )
            ) from exc
        return updated.prompt_document.text, updated, override, updated.prompt_document
