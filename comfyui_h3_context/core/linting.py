"""Structural and semantic prompt auditing without auto-correction or runtime dependencies."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from .context_reporting import ContextPlan, PromptDocument, PromptRenderStatus
from .contracts import (
    AssetRole,
    PromptProfile,
    TaskMode,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .errors import ProfileRegistryError, PromptLintError
from .profiles import PromptProfileRegistry, default_profile_registry

_LABEL_PATTERN = re.compile(r"<(Subject|Picture|Video|Audio) ([1-9][0-9]*)>")
_LABEL_CANDIDATE_PATTERN = re.compile(r"<([A-Za-z][A-Za-z ]*) ([0-9]+)>")
_RESIDUE_PATTERN = re.compile(
    r"https?://|authorization|bearer\s+|api[_-]?key|password|secret|token=|sig=|x-amz-",
    re.IGNORECASE,
)
_MAX_DIAGNOSTICS = 512


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 65_536:
        raise PromptLintError(f"{field} must be a bounded non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class PromptLintDiagnostic:
    """One actionable machine-readable lint finding."""

    severity: ValidationSeverity
    code: str
    message: str
    location: str
    remediation: str

    def __post_init__(self) -> None:
        ValidationDiagnostic(self.severity, self.code, self.message, self.location)
        _require_text(self.remediation, "lint remediation")

    def to_wire(self) -> dict[str, str]:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "location": self.location,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class PromptLintResult:
    """Immutable audit result; it never contains a corrected prompt."""

    diagnostics: tuple[PromptLintDiagnostic, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.diagnostics, tuple) or len(self.diagnostics) > _MAX_DIAGNOSTICS:
            raise PromptLintError("lint diagnostics exceed the bounded result limit")
        if not all(isinstance(value, PromptLintDiagnostic) for value in self.diagnostics):
            raise PromptLintError("lint diagnostics contain an invalid value")

    @property
    def has_errors(self) -> bool:
        return any(
            value.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
            for value in self.diagnostics
        )

    @property
    def is_valid(self) -> bool:
        return not self.has_errors

    def to_wire(self) -> dict[str, object]:
        return {
            "is_valid": self.is_valid,
            "diagnostics": [value.to_wire() for value in self.diagnostics],
        }


def _add(
    diagnostics: list[PromptLintDiagnostic],
    severity: ValidationSeverity,
    code: str,
    message: str,
    location: str,
    remediation: str,
) -> None:
    if len(diagnostics) >= _MAX_DIAGNOSTICS:
        return
    diagnostics.append(PromptLintDiagnostic(severity, code, message, location, remediation))


def _expected_labels(plan: ContextPlan) -> set[str]:
    """Every label the prompt is *allowed* to contain."""

    labels = {label.label for label in plan.request.reference_registry.labels}
    if plan.request.profile.name is PromptProfile.FULL_REFERENCE:
        labels.update(f"<Subject {index}>" for index, _ in enumerate(plan.intent_graph.subjects, 1))
    return labels


def _required_labels(plan: ContextPlan) -> set[str]:
    """Every label the prompt is *obliged* to contain.

    GUARD: this is deliberately narrower than `_expected_labels`. A reference label reaches the
    prompt because the plan binds the asset to something -- a subject's source, a sound's source, a
    retained element, a copied event -- or because the mode instruction names it as a keyframe.
    An asset the author attached and never bound earns no invented role, so its absence is not a
    renderer defect; M24-05 reports it as `fidelity.reference.unused` instead. Widening this back
    to every registry label turns that honest omission into a blocking lint error and refuses the
    report.
    """

    graph = plan.intent_graph
    bound: set[str] = {
        asset_id for subject in graph.subjects for asset_id in subject.source_asset_ids
    }
    bound |= {asset_id for audio in graph.audios for asset_id in audio.source_asset_ids}
    bound |= {event.source_asset_id for event in graph.events}
    bound |= {asset_id for relation in graph.retention for asset_id in relation.source_asset_ids}
    bound |= {
        asset.asset_id
        for asset in plan.request.reference_registry.assets
        if asset.role in {AssetRole.FIRST_FRAME, AssetRole.LAST_FRAME}
    }
    labels = {
        label.label for label in plan.request.reference_registry.labels if label.asset_id in bound
    }
    if plan.request.profile.name is PromptProfile.FULL_REFERENCE:
        labels.update(f"<Subject {index}>" for index, _ in enumerate(graph.subjects, 1))
    return labels


def _check_profile_and_document(
    plan: ContextPlan,
    document: PromptDocument,
    registry: PromptProfileRegistry,
    diagnostics: list[PromptLintDiagnostic],
) -> None:
    try:
        profile = registry.get(plan.request.profile)
    except ProfileRegistryError:
        _add(
            diagnostics,
            ValidationSeverity.ERROR,
            "profile.unknown",
            "the plan profile is not registered",
            "plan.request.profile",
            "Register an explicit supported profile/version before rendering.",
        )
        return
    if not profile.supports(plan.request.task_mode):
        _add(
            diagnostics,
            ValidationSeverity.ERROR,
            "profile.unsupported_mode",
            "the selected profile does not support the plan task mode",
            "plan.request.task_mode",
            "Choose a compatible profile explicitly or change the typed task mode.",
        )
    if document.profile != plan.request.profile:
        _add(
            diagnostics,
            ValidationSeverity.ERROR,
            "document.profile_mismatch",
            "prompt document profile differs from the plan profile",
            "prompt_document.profile",
            "Re-render the document from the same typed plan and profile.",
        )
    if document.task_mode is not plan.request.task_mode:
        _add(
            diagnostics,
            ValidationSeverity.ERROR,
            "document.mode_mismatch",
            "prompt document task mode differs from the plan task mode",
            "prompt_document.task_mode",
            "Re-render the document from the same typed plan and task mode.",
        )
    if document.plan_id != plan.plan_id:
        _add(
            diagnostics,
            ValidationSeverity.ERROR,
            "document.plan_mismatch",
            "prompt document does not belong to the audited plan",
            "prompt_document.plan_id",
            "Use the document produced for this plan or re-render it.",
        )
    headings = tuple(section.heading for section in document.sections)
    if len(headings) != len(set(headings)):
        _add(
            diagnostics,
            ValidationSeverity.ERROR,
            "prompt.duplicate_section",
            "prompt contains a duplicate profile section heading",
            "prompt_document.sections",
            "Emit each profile section exactly once in the declared render order.",
        )
    for field in profile.required_fields:
        if field not in headings:
            _add(
                diagnostics,
                ValidationSeverity.ERROR,
                "prompt.missing_section",
                f"required profile section {field!r} is missing",
                "prompt_document.sections",
                f"Render the required {field} section in the profile-declared order.",
            )
    if headings != profile.render_order:
        _add(
            diagnostics,
            ValidationSeverity.ERROR,
            "prompt.section_order",
            "prompt sections do not match the profile-declared render order",
            "prompt_document.sections",
            "Re-render with the versioned profile render order; do not reorder silently.",
        )
    if document.status is not PromptRenderStatus.RENDERED:
        _add(
            diagnostics,
            ValidationSeverity.ERROR,
            "prompt.not_rendered",
            "prompt document is not marked rendered",
            "prompt_document.status",
            "Complete the render stage or return an explicit draft state to the caller.",
        )
    for section in document.sections:
        if section.body not in document.text:
            _add(
                diagnostics,
                ValidationSeverity.ERROR,
                "prompt.section_body_missing",
                "prompt text does not contain the declared section body",
                f"prompt_document.sections.{section.section_id}",
                "Re-render or parse the prompt without changing declared section content.",
            )


def _check_labels_and_assets(
    plan: ContextPlan,
    document: PromptDocument,
    diagnostics: list[PromptLintDiagnostic],
) -> None:
    expected = _expected_labels(plan)
    observed = {match.group(0) for match in _LABEL_PATTERN.finditer(document.text)}
    # FL2VA's first/last-frame alignment prose intentionally uses the guide's
    # ``Picture N`` spelling without angle brackets.  Those labels still refer
    # to the typed registry assets and must satisfy the same ownership check.
    if plan.request.task_mode is TaskMode.FL2VA:
        observed.update(
            f"<{match.group(0)}>"
            for match in re.finditer(r"\bPicture [1-9][0-9]*\b", document.text)
        )
    candidates = {match.group(0) for match in _LABEL_CANDIDATE_PATTERN.finditer(document.text)}
    invalid = {label for label in candidates if _LABEL_PATTERN.fullmatch(label) is None}
    invalid.update(observed - expected)
    for label in sorted(invalid):
        _add(
            diagnostics,
            ValidationSeverity.ERROR,
            "reference.invalid_label",
            f"prompt contains unsupported reference label {label!r}",
            "prompt_document.text",
            "Use only deterministic labels emitted by the reference registry and subject graph.",
        )
    for label in sorted(_required_labels(plan) - observed):
        _add(
            diagnostics,
            ValidationSeverity.ERROR,
            "reference.orphan_asset",
            f"typed reference label {label!r} is not used by the prompt",
            "prompt_document.text",
            "Include the owned reference in the appropriate definition/shot or remove it "
            "from the plan.",
        )


def _check_graph_and_duration(plan: ContextPlan, diagnostics: list[PromptLintDiagnostic]) -> None:
    for diagnostic in plan.intent_graph.validate():
        if diagnostic.code.startswith("timeline_"):
            lint_code = f"timeline.{diagnostic.code.removeprefix('timeline_')}"
        else:
            lint_code = f"graph.{diagnostic.code}"
        _add(
            diagnostics,
            diagnostic.severity,
            lint_code,
            diagnostic.message,
            diagnostic.location or "intent_graph",
            "Repair the typed timeline/ownership references and re-run validation.",
        )
    if plan.intent_graph.effective_duration.seconds != Decimal(
        str(plan.request.effective_duration_seconds)
    ):
        _add(
            diagnostics,
            ValidationSeverity.ERROR,
            "timeline.duration_mismatch",
            "intent graph duration differs from normalized request duration",
            "intent_graph.effective_duration",
            "Rebuild the graph from the normalized effective duration.",
        )


def _check_security_and_constraints(
    plan: ContextPlan,
    document: PromptDocument,
    diagnostics: list[PromptLintDiagnostic],
) -> None:
    if _RESIDUE_PATTERN.search(document.text):
        _add(
            diagnostics,
            ValidationSeverity.FATAL,
            "security.provider_residue",
            "prompt contains provider credential, signed-resource, or URL residue",
            "prompt_document.text",
            "Remove provider residue and re-render from redacted typed contracts.",
        )
    for exact_value in plan.hard_constraints.exact_texts:
        if exact_value.text not in document.text:
            _add(
                diagnostics,
                ValidationSeverity.ERROR,
                "constraint.exact_text_lost",
                "declared exact dialogue/lyrics/visible text is absent from the prompt",
                f"hard_constraints.{exact_value.constraint_id}",
                "Restore the exact caller-declared value or record an authorized transformation.",
            )
    for required_value in plan.hard_constraints.required_content:
        if required_value.content not in document.text:
            _add(
                diagnostics,
                ValidationSeverity.ERROR,
                "constraint.required_content_lost",
                "declared required content is absent from the prompt",
                f"hard_constraints.{required_value.constraint_id}",
                "Restore the required content in the owned prompt section.",
            )
    for forbidden_value in plan.hard_constraints.forbidden_content:
        if forbidden_value.content in document.text:
            _add(
                diagnostics,
                ValidationSeverity.ERROR,
                "constraint.forbidden_content_present",
                "forbidden content appears in the prompt",
                f"hard_constraints.{forbidden_value.constraint_id}",
                "Remove the forbidden value and re-render; do not auto-rewrite it.",
            )


def lint_prompt(
    plan: ContextPlan,
    document: PromptDocument,
    registry: PromptProfileRegistry | None = None,
) -> PromptLintResult:
    """Audit a typed plan/document pair and return diagnostics without mutation."""

    if not isinstance(plan, ContextPlan):
        raise PromptLintError("prompt linting requires a ContextPlan")
    if not isinstance(document, PromptDocument):
        raise PromptLintError("prompt linting requires a PromptDocument")
    profiles = default_profile_registry() if registry is None else registry
    diagnostics: list[PromptLintDiagnostic] = []
    _check_profile_and_document(plan, document, profiles, diagnostics)
    _check_labels_and_assets(plan, document, diagnostics)
    _check_graph_and_duration(plan, diagnostics)
    _check_security_and_constraints(plan, document, diagnostics)
    return PromptLintResult(tuple(diagnostics))


__all__ = ["PromptLintDiagnostic", "PromptLintResult", "lint_prompt"]
