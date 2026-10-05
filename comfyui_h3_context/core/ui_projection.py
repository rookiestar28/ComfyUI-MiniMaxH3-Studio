"""Bounded native ComfyUI ``ui`` projection for the terminal Preview node.

Only a small redacted summary crosses this boundary.  The canonical report remains available as a
typed result for the graph, while browser/native-text consumers receive no evidence collections,
provider payloads, locators, credentials, or raw private report sections.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum

from .canonical import canonical_fingerprint, fingerprint_context_report
from .context_reporting import ContextReport, ValidationStatus
from .contracts import ValidationSeverity
from .errors import ReportLifecycleError
from .validation_lifecycle import ValidatedReportEnvelope

UI_PROJECTION_SCHEMA = "h3.context.ui.projection.v1"
MAX_UI_PROJECTION_BYTES = 32_768
MAX_UI_PROJECTION_TEXT = 4_096
MAX_UI_PROJECTION_ITEMS = 32
_SEVERITY_VALUES = frozenset(member.value for member in ValidationSeverity)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_REDACTION = re.compile(
    r"https?://[^\s<>\"']+"
    r"|file://[^\s<>\"']+"
    r"|(?i:\b(?:authorization\s*[:=]\s*)?bearer\s+[^\s,;]+)"
    r"|(?i:\b(?:authorization|bearer|api[_-]?key|apikey|password|secret|token|sig|"
    r"x-amz-[a-z0-9-]*)\s*[:=]\s*[^\s,;]+)"
    r"|(?i:(?:[A-Za-z]:[\\/]|/(?:home|mnt|tmp|var|Users|private|workspace)/)[^\s,;]+)"
)


class UIEventState(str, Enum):
    """Distinct states carried by an ``executed`` projection."""

    VALIDATED = "validated"
    CACHED = "cached"
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ReportLifecycleError("invalid_correlation", f"{field} must be a bounded identifier")
    return value


def _redact(value: str) -> tuple[str, bool]:
    changed = False

    def replace(match: re.Match[str]) -> str:
        nonlocal changed
        changed = True
        return "[REDACTED]"

    return _REDACTION.sub(replace, value), changed


def _bounded_text(
    value: object, field: str, maximum: int = MAX_UI_PROJECTION_TEXT
) -> tuple[str, bool]:
    if not isinstance(value, str):
        raise ReportLifecycleError("invalid_projection", f"{field} must be text")
    redacted, changed = _redact(value)
    marker = "…[TRUNCATED]"
    if len(redacted) > maximum:
        return redacted[: max(0, maximum - len(marker))] + marker, True
    return redacted, changed


def _safe_field(value: object, field: str, maximum: int = 256) -> str:
    """Validate a scalar projection field after applying the same redaction policy."""

    bounded, changed = _bounded_text(value, field, maximum)
    if changed or _REDACTION.search(bounded):
        raise ReportLifecycleError("unsafe_projection", f"{field} is not redacted or canonical")
    return bounded


@dataclass(frozen=True, slots=True)
class ExecutionCorrelation:
    """The two public identifiers required to join a UI payload to host execution events."""

    prompt_id: str
    execution_node_id: str

    def __post_init__(self) -> None:
        _identifier(self.prompt_id, "prompt_id")
        _identifier(self.execution_node_id, "execution_node_id")

    def to_wire(self) -> dict[str, str]:
        return {
            "prompt_id": self.prompt_id,
            "execution_node_id": self.execution_node_id,
        }


@dataclass(frozen=True, slots=True)
class BoundedUIProjection:
    """JSON-safe, bounded summary deliberately smaller than a full private report."""

    report_id: str
    report_revision: int
    report_fingerprint: str
    prompt_fingerprint: str
    correlation: ExecutionCorrelation
    state: UIEventState
    validation_status: ValidationStatus
    prompt_text: str
    diagnostics: tuple[dict[str, str], ...] = ()
    limitations: tuple[dict[str, str], ...] = ()
    provider: str = "manual"
    provider_outcome: str = "not_requested"
    redacted_fields: tuple[str, ...] = ()
    omitted_fields: tuple[str, ...] = ()
    schema: str = UI_PROJECTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != UI_PROJECTION_SCHEMA:
            raise ReportLifecycleError("unsupported_schema", "UI projection schema is unsupported")
        _identifier(self.report_id, "report_id")
        if (
            isinstance(self.report_revision, bool)
            or not isinstance(self.report_revision, int)
            or not 0 <= self.report_revision <= 1_000_000
        ):
            raise ReportLifecycleError("invalid_revision", "UI report revision is out of bounds")
        for value, field in (
            (self.report_fingerprint, "report_fingerprint"),
            (self.prompt_fingerprint, "prompt_fingerprint"),
        ):
            if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}\Z", value):
                raise ReportLifecycleError(
                    "invalid_projection", f"{field} is not a SHA-256 fingerprint"
                )
        if not isinstance(self.correlation, ExecutionCorrelation):
            raise ReportLifecycleError(
                "invalid_correlation", "UI projection correlation is invalid"
            )
        if not isinstance(self.state, UIEventState) or not isinstance(
            self.validation_status, ValidationStatus
        ):
            raise ReportLifecycleError("invalid_projection", "UI projection state is invalid")
        safe_prompt, _ = _bounded_text(self.prompt_text, "prompt_text")
        if safe_prompt != self.prompt_text:
            raise ReportLifecycleError(
                "unsafe_projection", "prompt_text is not redacted or bounded"
            )
        for summary_values, field in (
            (self.diagnostics, "diagnostics"),
            (self.limitations, "limitations"),
        ):
            if (
                not isinstance(summary_values, tuple)
                or len(summary_values) > MAX_UI_PROJECTION_ITEMS
            ):
                raise ReportLifecycleError("projection_limit", f"{field} exceeds the item bound")
            for item in summary_values:
                if not isinstance(item, dict) or set(item) != {"code", "severity", "message"}:
                    raise ReportLifecycleError(
                        "invalid_projection", f"{field} contains an invalid item"
                    )
                _safe_field(item["code"], f"{field}.code", 64)
                _safe_field(item["severity"], f"{field}.severity", 32)
                # Membership, not just length. The UI-projection JSON Schema M18-05 retired
                # stated this as an enum and the typed authority did not, so a bounded string
                # of any content crossed the UI boundary calling itself a severity. Checked
                # against the live `ValidationSeverity` rather than a copied literal list, so
                # the two cannot drift apart the way the schema and the module already had.
                if item["severity"] not in _SEVERITY_VALUES:
                    raise ReportLifecycleError(
                        "invalid_projection", f"{field}.severity is not a known severity"
                    )
                message, _ = _bounded_text(item["message"], f"{field}.message", 512)
                if message != item["message"]:
                    raise ReportLifecycleError(
                        "unsafe_projection", f"{field}.message is not redacted or bounded"
                    )
        for redaction_values, field in (
            (self.redacted_fields, "redacted_fields"),
            (self.omitted_fields, "omitted_fields"),
        ):
            if (
                not isinstance(redaction_values, tuple)
                or len(redaction_values) > MAX_UI_PROJECTION_ITEMS
                or not all(isinstance(item, str) and item for item in redaction_values)
            ):
                raise ReportLifecycleError("invalid_projection", f"{field} is outside its bound")
            for field_name in redaction_values:
                _safe_field(field_name, field, 128)
        _safe_field(self.provider, "provider", 64)
        _safe_field(self.provider_outcome, "provider_outcome", 64)
        wire = self.to_wire()
        try:
            encoded = json.dumps(wire, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ReportLifecycleError(
                "invalid_projection", "UI projection is not JSON serializable"
            ) from exc
        if len(encoded.encode("utf-8")) > MAX_UI_PROJECTION_BYTES:
            raise ReportLifecycleError("projection_limit", "UI projection exceeds its byte bound")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "report_id": self.report_id,
            "report_revision": self.report_revision,
            "report_fingerprint": self.report_fingerprint,
            "prompt_fingerprint": self.prompt_fingerprint,
            "correlation": self.correlation.to_wire(),
            "state": self.state.value,
            "validation_status": self.validation_status.value,
            "prompt_text": self.prompt_text,
            "diagnostics": list(self.diagnostics),
            "limitations": list(self.limitations),
            "provider": self.provider,
            "provider_outcome": self.provider_outcome,
            "redacted_fields": list(self.redacted_fields),
            "omitted_fields": list(self.omitted_fields),
        }

    def to_ui(self) -> dict[str, tuple[object, ...]]:
        """Adapt the scalar wire projection to ComfyUI's list-valued UI protocol."""

        return {key: (value,) for key, value in self.to_wire().items()}


def _report(value: ContextReport | ValidatedReportEnvelope) -> ContextReport:
    report = value.report if isinstance(value, ValidatedReportEnvelope) else value
    if not isinstance(report, ContextReport):
        raise ReportLifecycleError("invalid_report", "UI projection requires a ContextReport")
    if isinstance(value, ValidatedReportEnvelope) and value.report is not report:
        raise ReportLifecycleError("stale_report", "UI envelope does not own the report")
    return report


def _diagnostic_items(report: ContextReport) -> tuple[dict[str, str], ...]:
    diagnostics = tuple(report.validation.diagnostics) + tuple(report.diagnostics)
    result: list[dict[str, str]] = []
    for item in diagnostics[:MAX_UI_PROJECTION_ITEMS]:
        message, _ = _bounded_text(item.message, "diagnostic.message", 512)
        result.append({"code": item.code, "severity": item.severity.value, "message": message})
    return tuple(result)


def _limitation_items(report: ContextReport) -> tuple[dict[str, str], ...]:
    result: list[dict[str, str]] = []
    for item in report.limitations[:MAX_UI_PROJECTION_ITEMS]:
        message, _ = _bounded_text(item.message, "limitation.message", 512)
        result.append({"code": item.code, "severity": item.severity.value, "message": message})
    return tuple(result)


def build_ui_projection(
    value: ContextReport | ValidatedReportEnvelope,
    correlation: ExecutionCorrelation,
    state: UIEventState,
) -> BoundedUIProjection:
    """Build a small redacted projection without copying the canonical report."""

    if not isinstance(correlation, ExecutionCorrelation):
        raise ReportLifecycleError(
            "invalid_correlation", "UI projection requires execution correlation"
        )
    if not isinstance(state, UIEventState):
        raise ReportLifecycleError("invalid_projection", "UI projection state is invalid")
    report = _report(value)
    if state in {UIEventState.VALIDATED, UIEventState.CACHED, UIEventState.SUCCESS}:
        if report.validation.status is ValidationStatus.NOT_RUN:
            raise ReportLifecycleError(
                "validation_not_run", "successful UI state requires Validator output"
            )
        if report.validation.status is ValidationStatus.FAILED or report.has_errors:
            raise ReportLifecycleError(
                "validation_failed", "successful UI state cannot contain validation errors"
            )
    prompt_text, prompt_redacted_or_truncated = _bounded_text(
        report.prompt_document.text, "prompt_text"
    )
    redacted_fields = ("prompt_text",) if prompt_redacted_or_truncated else ()
    provider = (
        report.provider_receipt.provider.value if report.provider_receipt is not None else "manual"
    )
    outcome = (
        report.provider_receipt.outcome.value
        if report.provider_receipt is not None
        else "not_requested"
    )
    projection = BoundedUIProjection(
        report_id=report.report_id,
        report_revision=report.revision,
        report_fingerprint=fingerprint_context_report(report),
        prompt_fingerprint=canonical_fingerprint(report.prompt_document.text),
        correlation=correlation,
        state=state,
        validation_status=report.validation.status,
        prompt_text=prompt_text,
        diagnostics=_diagnostic_items(report),
        limitations=_limitation_items(report),
        provider=provider,
        provider_outcome=outcome,
        redacted_fields=redacted_fields,
        omitted_fields=(),
    )
    return projection


__all__ = [
    "BoundedUIProjection",
    "ExecutionCorrelation",
    "MAX_UI_PROJECTION_BYTES",
    "MAX_UI_PROJECTION_ITEMS",
    "MAX_UI_PROJECTION_TEXT",
    "UIEventState",
    "UI_PROJECTION_SCHEMA",
    "build_ui_projection",
]
