"""Provider-free canonical Request -> Plan -> Compiler -> Validator execution.

ComfyUI node classes are presentation wrappers around this module.  Other trusted adapters may
invoke the same deterministic pipeline without importing the node/UI layer or implementing a
second prompt path.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from .canonical import canonical_fingerprint
from .constraints import HardConstraintSet, TimePoint
from .context_reporting import (
    ContextPlan,
    ContextReport,
    Limitation,
    PlanStage,
    PlanStep,
    PlanStepStatus,
    PromptDocument,
    PromptRenderStatus,
    PromptSection,
    ProviderOutcome,
    ProviderReceipt,
    ValidationResult,
    ValidationStatus,
)
from .contracts import (
    PromptProfile,
    ProviderIdentity,
    SchemaVersion,
    TaskMode,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .errors import (
    ContextReportError,
    ContractValidationError,
    PromptLintError,
    PromptRenderingError,
    ReportLifecycleError,
)
from .intent_graph import IntentGraph, TimelineSegment, build_intent_graph
from .normalization import (
    NormalizedContextRequest,
    RawContextRequest,
    normalize_request,
    validate_request_controls,
)
from .reconstruction_stages import (
    _register_reconstruction_source_plan,
    _register_reconstruction_validation,
)
from .registry import ReferenceRegistry
from .validation_lifecycle import validate_context_report


class CanonicalContextPipelineError(ValueError):
    """Typed provider-free pipeline failure carrying its structured diagnostics."""

    def __init__(self, diagnostics: Iterable[ValidationDiagnostic]) -> None:
        self.diagnostics = tuple(diagnostics)
        codes = "; ".join(item.code for item in self.diagnostics)
        super().__init__("canonical context pipeline failed" + (f": {codes}" if codes else ""))


def has_error_diagnostics(diagnostics: tuple[ValidationDiagnostic, ...]) -> bool:
    return any(
        item.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
        for item in diagnostics
    )


def _pipeline_error(code: str, message: str) -> CanonicalContextPipelineError:
    return CanonicalContextPipelineError(
        (ValidationDiagnostic(ValidationSeverity.ERROR, code, message),)
    )


def pipeline_digest_id(prefix: str, value: object) -> str:
    """Return a bounded opaque ID; raw user/media/provider values never enter identifiers."""

    digest = canonical_fingerprint(value).split(":", 1)[1]
    return f"{prefix}_{digest[:24]}"


def build_draft_prompt_document(request: NormalizedContextRequest, plan_id: str) -> PromptDocument:
    """Create the inspectable pre-render document required by a draft planning report."""

    return PromptDocument(
        document_id=f"prompt_{plan_id}",
        schema_version=request.schema_version,
        profile=request.profile,
        task_mode=request.task_mode,
        plan_id=plan_id,
        text="Prompt compilation has not run.",
        sections=(
            PromptSection(
                section_id="draft_status",
                order=1,
                heading="draft_status",
                body="Prompt compilation has not run.",
            ),
        ),
        status=PromptRenderStatus.DRAFT,
    )


def _manual_provider_receipt(plan_id: str) -> ProviderReceipt:
    return ProviderReceipt(
        receipt_id=f"receipt_{plan_id}",
        provider=ProviderIdentity.MANUAL,
        outcome=ProviderOutcome.NOT_REQUESTED,
    )


def build_not_run_validation(schema_version: SchemaVersion, target_id: str) -> ValidationResult:
    """Build the explicit lifecycle state used until canonical validation runs."""

    return ValidationResult(
        validation_id=f"validation_{target_id}",
        schema_version=schema_version,
        target_id=target_id,
        status=ValidationStatus.NOT_RUN,
        validator_version="h3-context-m3-04",
    )


def build_pipeline_report(
    plan: ContextPlan,
    document: PromptDocument,
    validation: ValidationResult,
    diagnostics: tuple[ValidationDiagnostic, ...] = (),
) -> ContextReport:
    return ContextReport(
        report_id=f"report_{plan.plan_id}",
        schema_version=plan.schema_version,
        request=plan.request,
        plan=plan,
        prompt_document=document,
        validation=validation,
        evidence=plan.evidence,
        limitations=plan.limitations,
        diagnostics=diagnostics,
        provider_receipt=_manual_provider_receipt(plan.plan_id),
    )


def _plan_fingerprint(request: NormalizedContextRequest, graph: IntentGraph) -> dict[str, object]:
    """Build the canonical float-free plan identity projection."""

    return {
        "schema_version": str(request.schema_version),
        "profile": request.profile.to_wire(),
        "task_mode": request.task_mode.value,
        "model_variant": request.model_variant.value,
        "user_intent": request.user_intent,
        "requested_duration_seconds": (
            None
            if request.requested_duration_seconds is None
            else repr(request.requested_duration_seconds)
        ),
        "effective_frame_count": request.effective_frame_count,
        "duration_source": request.duration_source.value,
        "assets": [asset.to_wire() for asset in request.assets],
        "hard_constraints": request.hard_constraints.to_wire(),
        "reference_registry": request.reference_registry.to_wire(),
        "evidence": request.evidence.to_wire(),
        "intent_graph": graph.to_wire(),
    }


def build_canonical_context_request(
    task_mode: TaskMode | str,
    user_intent: str,
    duration_seconds: int | float | None = None,
    hard_constraints: HardConstraintSet | None = None,
) -> RawContextRequest:
    """Validate visible request controls and return their canonical typed envelope."""

    constraints = HardConstraintSet() if hard_constraints is None else hard_constraints
    raw = RawContextRequest(
        mode=task_mode,
        user_intent=user_intent,
        duration_seconds=duration_seconds,
        hard_constraints=constraints,
    )
    diagnostics = validate_request_controls(raw)
    if has_error_diagnostics(diagnostics):
        raise CanonicalContextPipelineError(diagnostics)
    try:
        canonical_mode = TaskMode(task_mode)
    except (TypeError, ValueError) as exc:
        raise CanonicalContextPipelineError(diagnostics) from exc
    return RawContextRequest(
        mode=canonical_mode,
        user_intent=user_intent,
        duration_seconds=duration_seconds,
        hard_constraints=constraints,
    )


def build_canonical_context_plan(
    request: object,
    reference_registry: object | None = None,
    intent_graph: object | None = None,
) -> tuple[ContextPlan, ContextReport]:
    """Normalize one request and assemble the canonical explicit or manual-skeleton plan."""

    if not isinstance(request, RawContextRequest):
        raise _pipeline_error("invalid_request", "request must be a RawContextRequest")
    if not isinstance(request.reference_registry, ReferenceRegistry):
        raise _pipeline_error(
            "invalid_reference_registry",
            "request reference_registry must be a ReferenceRegistry",
        )
    if reference_registry is not None and not isinstance(reference_registry, ReferenceRegistry):
        raise _pipeline_error(
            "invalid_reference_registry",
            "reference_registry must be a ReferenceRegistry",
        )

    selected_registry = request.reference_registry
    if reference_registry is not None:
        explicit_registry = reference_registry
        if selected_registry.assets and selected_registry != explicit_registry:
            raise _pipeline_error(
                "reference_registry_mismatch",
                "request and connected reference registries disagree",
            )
        selected_registry = explicit_registry
        if isinstance(intent_graph, IntentGraph) and intent_graph.registry != explicit_registry:
            raise _pipeline_error(
                "reference_registry_mismatch",
                "connected reference registry does not own the explicit intent graph",
            )

    try:
        normalized_result = normalize_request(
            replace(request, reference_registry=selected_registry)
        )
    except (ContextReportError, TypeError, ValueError) as exc:
        raise _pipeline_error(
            "normalization_failed", "request normalization failed closed"
        ) from exc
    if normalized_result.request is None:
        raise CanonicalContextPipelineError(normalized_result.diagnostics)
    normalized = normalized_result.request

    graph_diagnostics: tuple[ValidationDiagnostic, ...] = ()
    limitation: Limitation | None = None
    if intent_graph is not None:
        if not isinstance(intent_graph, IntentGraph):
            raise _pipeline_error("invalid_intent_graph", "intent_graph must be an IntentGraph")
        if intent_graph.registry != normalized.reference_registry:
            raise _pipeline_error(
                "intent_graph_registry_mismatch",
                "intent_graph registry does not match the normalized request",
            )
        duration = TimePoint.from_text(str(normalized.effective_duration_seconds))
        if intent_graph.effective_duration.seconds != duration.seconds:
            raise _pipeline_error(
                "intent_graph_duration_mismatch",
                "intent_graph duration does not match the normalized request",
            )
        graph_diagnostics = intent_graph.validate()
        if has_error_diagnostics(graph_diagnostics):
            raise CanonicalContextPipelineError(graph_diagnostics)
        graph = intent_graph
        assembly_description = "assemble explicit intent graph"
    else:
        duration = TimePoint.from_text(str(normalized.effective_duration_seconds))
        skeleton_result = build_intent_graph(
            effective_duration=duration,
            registry=normalized.reference_registry,
            segments=(
                TimelineSegment(
                    segment_id="segment_1",
                    start=TimePoint.from_text("0"),
                    end=duration,
                ),
            ),
        )
        if skeleton_result.graph is None:
            raise CanonicalContextPipelineError(skeleton_result.diagnostics)
        graph = skeleton_result.graph
        graph_diagnostics = skeleton_result.diagnostics
        limitation = Limitation(
            limitation_id="limitation_manual_plan",
            code="manual_plan_skeleton",
            message=(
                "No explicit semantic intent graph was supplied; user intent remains "
                "unstructured and no subjects, scenes, actions, camera, style, or audio "
                "were inferred."
            ),
        )
        assembly_description = "assemble manual timeline skeleton"

    plan_id = pipeline_digest_id("plan", _plan_fingerprint(normalized, graph))
    plan_limitations = () if limitation is None else (limitation,)
    plan_diagnostics = tuple(normalized_result.diagnostics) + graph_diagnostics
    try:
        plan = ContextPlan(
            plan_id=plan_id,
            schema_version=normalized.schema_version,
            request=normalized,
            intent_graph=graph,
            hard_constraints=normalized.hard_constraints,
            evidence=normalized.evidence,
            steps=(
                PlanStep(
                    step_id="step_normalize",
                    stage=PlanStage.NORMALIZE,
                    status=PlanStepStatus.COMPLETED,
                    description="normalize typed request",
                ),
                PlanStep(
                    step_id="step_bind_references",
                    stage=PlanStage.BIND_REFERENCES,
                    status=PlanStepStatus.COMPLETED,
                    description="bind explicit reference ownership",
                ),
                PlanStep(
                    step_id="step_assemble_intent",
                    stage=PlanStage.ASSEMBLE_INTENT,
                    status=PlanStepStatus.COMPLETED,
                    description=assembly_description,
                ),
            ),
            limitations=plan_limitations,
            diagnostics=plan_diagnostics,
        )
        document = build_draft_prompt_document(normalized, plan.plan_id)
        report = build_pipeline_report(
            plan,
            document,
            build_not_run_validation(normalized.schema_version, document.document_id),
            diagnostics=plan_diagnostics,
        )
        _register_reconstruction_source_plan(plan)
    except (ContextReportError, ContractValidationError) as exc:
        raise _pipeline_error("plan_contract_error", "plan report construction failed") from exc
    return plan, report


def compile_canonical_context_plan(
    plan: object,
) -> tuple[str, ContextReport, PromptDocument]:
    """Render a canonical plan through its declared pure-core prompt profile."""

    if not isinstance(plan, ContextPlan):
        raise _pipeline_error("invalid_plan", "plan must be a ContextPlan")
    try:
        if plan.request.profile.name is PromptProfile.BASE:
            from .rendering import render_base_prompt

            document = render_base_prompt(plan)
        elif plan.request.profile.name is PromptProfile.FULL_REFERENCE:
            from .rendering import render_full_reference_prompt

            document = render_full_reference_prompt(plan)
        else:
            raise _pipeline_error("unsupported_profile", "plan profile is not supported")
        validation = build_not_run_validation(plan.schema_version, document.document_id)
        report = build_pipeline_report(
            plan,
            document,
            validation,
            diagnostics=plan.diagnostics,
        )
    except CanonicalContextPipelineError:
        raise
    except (ContextReportError, PromptRenderingError, TypeError, ValueError) as exc:
        raise _pipeline_error("compile_failed", "selected prompt profile could not render") from exc
    return document.text, report, document


def validate_canonical_context_document(
    plan: object,
    prompt_document: object,
) -> tuple[ValidationResult, ContextReport]:
    """Audit a canonical plan/document pair without mutating or repairing prompt text."""

    if not isinstance(plan, ContextPlan):
        raise _pipeline_error("invalid_plan", "plan must be a ContextPlan")
    if not isinstance(prompt_document, PromptDocument):
        raise _pipeline_error(
            "invalid_prompt_document",
            "prompt_document must be a PromptDocument",
        )
    try:
        draft_report = build_pipeline_report(
            plan,
            prompt_document,
            build_not_run_validation(plan.schema_version, prompt_document.document_id),
            diagnostics=plan.diagnostics,
        )
        envelope = validate_context_report(draft_report)
        _register_reconstruction_validation(
            plan,
            prompt_document,
            envelope.report.validation,
            envelope.report,
        )
    except (
        ContractValidationError,
        PromptLintError,
        ContextReportError,
        ReportLifecycleError,
        TypeError,
        ValueError,
    ) as exc:
        raise _pipeline_error("validation_failed", "prompt validation failed closed") from exc
    return envelope.report.validation, envelope.report


__all__ = [
    "CanonicalContextPipelineError",
    "build_canonical_context_plan",
    "build_canonical_context_request",
    "build_draft_prompt_document",
    "build_not_run_validation",
    "build_pipeline_report",
    "compile_canonical_context_plan",
    "has_error_diagnostics",
    "pipeline_digest_id",
    "validate_canonical_context_document",
]
