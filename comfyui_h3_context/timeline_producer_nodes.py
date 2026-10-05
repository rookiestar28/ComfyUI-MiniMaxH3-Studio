"""Node adapters that produce timelines and reconstructions.

Everything downstream of evidence: full-reference and feasible A/V timelines, hierarchical
reduction, constrained semantic planning, source-profiled rendering and local reconstruction.
"""

from __future__ import annotations

from .core.constraints import HardConstraintSet
from .core.context_reporting import (
    ContextPlan,
    ContextReport,
    PromptDocument,
    ValidationResult,
)
from .core.contracts import (
    PromptProfile,
)
from .core.cross_reference_graph import CrossReferenceGraph
from .core.downstream_producer import (
    DirectiveAuthorityBundle,
    DownstreamProducerReport,
    unavailable_full_reference_timeline,
)
from .core.errors import (
    ContractValidationError,
)
from .core.full_reference_timeline import FullReferenceTimelineResult
from .core.intent_graph import IntentGraph
from .core.local_reconstruction import (
    LocalReconstructionResult,
    ReconstructionRoute,
    ReconstructionRouteDisposition,
    issue_local_reconstruction_result,
    reconstruction_route_dispositions,
)
from .core.native_h3 import NativeH3Wiring
from .core.normalization import (
    RawContextRequest,
)
from .core.perception_execution import QualifiedPerceptionResult
from .core.perception_producer import (
    PerceptionProducerResult,
)
from .core.reconstruction_stages import (
    ReconstructionReductionStage,
    ReconstructionSemanticStage,
    ReconstructionTimelineStage,
    _register_reconstruction_profiled,
    assert_reconstruction_plan,
    build_reconstruction_reduction,
    build_reconstruction_semantic_disposition,
    build_reconstruction_timeline,
)
from .core.registry import (
    ReferenceRegistry,
)
from .core.source_profiled_prompt import (
    OFFICIAL_H3_BASE_GUIDE_DIGEST,
    OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST,
    OFFICIAL_H3_GUIDE_REVISION,
    SourceProfiledPromptResult,
    build_official_source_profile_binding,
    render_profiled_prompt,
)
from .core.unified_evidence_graph import UnifiedEvidenceGraph
from .node_capability import NodeCapability, NodeCapabilityReason
from .node_support import (
    CROSS_REFERENCE_GRAPH_SOCKET_TYPE,
    DIRECTIVE_AUTHORITY_SOCKET_TYPE,
    DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE,
    FULL_REFERENCE_TIMELINE_SOCKET_TYPE,
    INTENT_GRAPH_SOCKET_TYPE,
    MEDIA_PRODUCER_SOCKET_TYPE,
    PLAN_SOCKET_TYPE,
    PROMPT_DOCUMENT_SOCKET_TYPE,
    PROMPT_STRING_SOCKET_TYPE,
    REFERENCE_SOCKET_TYPE,
    REQUEST_SOCKET_TYPE,
    UNIFIED_EVIDENCE_GRAPH_SOCKET_TYPE,
    VALIDATION_SOCKET_TYPE,
    _pipeline_error,
)
from .product_shell_node import (
    NATIVE_H3_WIRING_SOCKET_TYPE,
    REPORT_SOCKET_TYPE,
)

FULL_REFERENCE_TIMELINE_PRODUCER_NODE_ID = (
    "comfyui_h3_context.H3Context.FullReferenceTimelineProducer"
)

FULL_REFERENCE_TIMELINE_PRODUCER_DISPLAY_NAME = "H3 Full Reference Timeline Producer"

SOURCE_PROFILED_RENDERER_NODE_ID = "comfyui_h3_context.H3Context.SourceProfiledRenderer"

SOURCE_PROFILED_RENDERER_DISPLAY_NAME = "H3 Source-Profiled Renderer"

SOURCE_PROFILED_PROMPT_SOCKET_TYPE = "H3_SOURCE_PROFILED_PROMPT"

LOCAL_RECONSTRUCTION_NODE_ID = "comfyui_h3_context.H3Context.LocalReconstruction"

LOCAL_RECONSTRUCTION_DISPLAY_NAME = "H3 Local Reconstruction Acceptance"

LOCAL_RECONSTRUCTION_SOCKET_TYPE = "H3_LOCAL_RECONSTRUCTION"

FEASIBLE_AV_TIMELINE_NODE_ID = "comfyui_h3_context.H3Context.FeasibleAVTimeline"

FEASIBLE_AV_TIMELINE_DISPLAY_NAME = "H3 Feasible AV Timeline"

FEASIBLE_AV_TIMELINE_SOCKET_TYPE = "H3_FEASIBLE_AV_TIMELINE"

HIERARCHICAL_REDUCTION_NODE_ID = "comfyui_h3_context.H3Context.HierarchicalEvidenceReduction"

HIERARCHICAL_REDUCTION_DISPLAY_NAME = "H3 Hierarchical Evidence Reduction"

HIERARCHICAL_REDUCTION_SOCKET_TYPE = "H3_HIERARCHICAL_EVIDENCE_REDUCTION"

CONSTRAINED_SEMANTIC_NODE_ID = "comfyui_h3_context.H3Context.ConstrainedSemanticPlanning"

CONSTRAINED_SEMANTIC_DISPLAY_NAME = "H3 Constrained Semantic Planning"

CONSTRAINED_SEMANTIC_SOCKET_TYPE = "H3_CONSTRAINED_SEMANTIC_PLANNING"


class H3FullReferenceTimelineProducerNode:
    """Build qualified partial timelines and retain the legacy unavailable fallback."""

    NODE_ID = FULL_REFERENCE_TIMELINE_PRODUCER_NODE_ID
    __h3_context_node_id__ = NODE_ID
    RETURN_TYPES = (FULL_REFERENCE_TIMELINE_SOCKET_TYPE, DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE)
    RETURN_NAMES = ("timeline", "producer_report")
    FUNCTION = "produce"
    CATEGORY = "h3_context/assembly"
    DESCRIPTION = (
        "Build a partial Full-Reference timeline from qualified decoded-CFR video observations. "
        "Connect visual_result; missing qualified perception remains unavailable."
    )
    OUTPUT_NODE = False

    @classmethod
    def capability(cls, *, qualified_input: bool = False) -> NodeCapability:
        return NodeCapability(
            cls.NODE_ID,
            NodeCapabilityReason.QUALIFIED_TIMELINE
            if qualified_input
            else NodeCapabilityReason.PERCEPTION_PROFILE_UNAVAILABLE,
        )

    @classmethod
    def VALIDATE_INPUTS(
        cls, request: object | None = None, visual_result: object | None = None
    ) -> bool | str:
        # CRITICAL: queue validation sees link placeholders. Permit the qualified input
        # structurally here; execution must verify its private authority and exact source.
        return cls.capability(qualified_input=visual_result is not None).validation_result()

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "request": (REQUEST_SOCKET_TYPE, {}),
                "reference_registry": (REFERENCE_SOCKET_TYPE, {}),
                "media": (MEDIA_PRODUCER_SOCKET_TYPE, {}),
                "evidence_graph": (UNIFIED_EVIDENCE_GRAPH_SOCKET_TYPE, {}),
                "evidence_report": (DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE, {}),
                "cross_reference_graph": (CROSS_REFERENCE_GRAPH_SOCKET_TYPE, {}),
                "cross_reference_report": (DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE, {}),
                "directive_authority": (DIRECTIVE_AUTHORITY_SOCKET_TYPE, {}),
                "directive_report": (DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE, {}),
                "intent_graph": (INTENT_GRAPH_SOCKET_TYPE, {}),
                "intent_report": (DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE, {}),
            },
            "optional": {"visual_result": ("H3_VISUAL_PRODUCER_RESULT", {})},
        }

    def produce(
        self,
        request: RawContextRequest,
        reference_registry: ReferenceRegistry,
        media: PerceptionProducerResult,
        evidence_graph: UnifiedEvidenceGraph,
        evidence_report: DownstreamProducerReport,
        cross_reference_graph: CrossReferenceGraph,
        cross_reference_report: DownstreamProducerReport,
        directive_authority: DirectiveAuthorityBundle,
        directive_report: DownstreamProducerReport,
        intent_graph: IntentGraph,
        intent_report: DownstreamProducerReport,
        visual_result: QualifiedPerceptionResult | None = None,
    ) -> tuple[FullReferenceTimelineResult, DownstreamProducerReport]:
        return unavailable_full_reference_timeline(
            request,
            reference_registry,
            media,
            evidence_graph,
            evidence_report,
            cross_reference_graph,
            cross_reference_report,
            directive_authority,
            directive_report,
            intent_graph,
            intent_report,
            visual_result=visual_result,
        )


class H3FeasibleAVTimelineNode:
    """Execute the accepted M13-06 planner over current public producer values."""

    NODE_ID = FEASIBLE_AV_TIMELINE_NODE_ID
    __h3_context_node_id__ = NODE_ID
    RETURN_TYPES = (FEASIBLE_AV_TIMELINE_SOCKET_TYPE,)
    RETURN_NAMES = ("feasible_timeline",)
    FUNCTION = "plan"
    CATEGORY = "h3_context/planning"
    DESCRIPTION = "Builds the deterministic feasible audiovisual timeline."

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "request": (REQUEST_SOCKET_TYPE, {}),
                "reference_registry": (REFERENCE_SOCKET_TYPE, {}),
                "intent_graph": (INTENT_GRAPH_SOCKET_TYPE, {}),
                "intent_report": (DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE, {}),
                "directive_authority": (DIRECTIVE_AUTHORITY_SOCKET_TYPE, {}),
                "directive_report": (DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE, {}),
            }
        }

    def plan(
        self,
        request: RawContextRequest,
        reference_registry: ReferenceRegistry,
        intent_graph: IntentGraph,
        intent_report: DownstreamProducerReport,
        directive_authority: DirectiveAuthorityBundle,
        directive_report: DownstreamProducerReport,
    ) -> tuple[ReconstructionTimelineStage]:
        try:
            return (
                build_reconstruction_timeline(
                    request,
                    reference_registry,
                    intent_graph,
                    intent_report,
                    directive_authority,
                    directive_report,
                ),
            )
        except (ContractValidationError, TypeError, ValueError) as exc:
            raise _pipeline_error(
                "feasible_timeline_failed", "feasible timeline planning failed closed"
            ) from exc


class H3HierarchicalEvidenceReductionNode:
    """Execute accepted M13-07 reduction without inventing observations."""

    NODE_ID = HIERARCHICAL_REDUCTION_NODE_ID
    __h3_context_node_id__ = NODE_ID
    RETURN_TYPES = (HIERARCHICAL_REDUCTION_SOCKET_TYPE,)
    RETURN_NAMES = ("reduction",)
    FUNCTION = "reduce"
    CATEGORY = "h3_context/planning"
    DESCRIPTION = "Reduces admitted evidence under explicit deterministic budgets."

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "media": (MEDIA_PRODUCER_SOCKET_TYPE, {}),
                "evidence_graph": (UNIFIED_EVIDENCE_GRAPH_SOCKET_TYPE, {}),
                "evidence_report": (DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE, {}),
                "feasible_timeline": (FEASIBLE_AV_TIMELINE_SOCKET_TYPE, {}),
            }
        }

    def reduce(
        self,
        media: PerceptionProducerResult,
        evidence_graph: UnifiedEvidenceGraph,
        evidence_report: DownstreamProducerReport,
        feasible_timeline: ReconstructionTimelineStage,
    ) -> tuple[ReconstructionReductionStage]:
        try:
            return (
                build_reconstruction_reduction(
                    media, evidence_graph, evidence_report, feasible_timeline
                ),
            )
        except (ContractValidationError, TypeError, ValueError) as exc:
            raise _pipeline_error(
                "hierarchical_reduction_failed",
                "hierarchical evidence reduction failed closed",
            ) from exc


class H3ConstrainedSemanticPlanningNode:
    """Execute the M13-08 boundary with an explicit model-free disposition."""

    NODE_ID = CONSTRAINED_SEMANTIC_NODE_ID
    __h3_context_node_id__ = NODE_ID
    RETURN_TYPES = (CONSTRAINED_SEMANTIC_SOCKET_TYPE, PLAN_SOCKET_TYPE)
    RETURN_NAMES = ("semantic_planning", "accepted_plan")
    FUNCTION = "plan"
    CATEGORY = "h3_context/planning"
    DESCRIPTION = "Records unavailable enrichment and materializes the accepted planning chain."

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "reduction": (HIERARCHICAL_REDUCTION_SOCKET_TYPE, {}),
                "plan": (PLAN_SOCKET_TYPE, {}),
            }
        }

    def plan(
        self, reduction: ReconstructionReductionStage, plan: ContextPlan
    ) -> tuple[ReconstructionSemanticStage, ContextPlan]:
        try:
            stage = build_reconstruction_semantic_disposition(reduction, plan)
        except (ContractValidationError, TypeError, ValueError) as exc:
            raise _pipeline_error(
                "semantic_planning_failed", "semantic planning boundary failed closed"
            ) from exc
        return stage, stage.accepted_plan


class H3SourceProfiledRendererNode:
    """Render through the accepted source-pinned M13-09 contract."""

    NODE_ID = SOURCE_PROFILED_RENDERER_NODE_ID
    __h3_context_node_id__ = NODE_ID
    RETURN_TYPES = (
        PROMPT_STRING_SOCKET_TYPE,
        SOURCE_PROFILED_PROMPT_SOCKET_TYPE,
        PROMPT_DOCUMENT_SOCKET_TYPE,
    )
    RETURN_NAMES = ("prompt", "profiled_result", "prompt_document")
    FUNCTION = "render"
    CATEGORY = "h3_context/compiler"
    DESCRIPTION = "Renders and semantically validates a plan against the pinned official guide."
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {"required": {"plan": (PLAN_SOCKET_TYPE, {})}}

    def render(self, plan: object) -> tuple[str, SourceProfiledPromptResult, PromptDocument]:
        if type(plan) is not ContextPlan:
            raise _pipeline_error("invalid_plan", "plan must be an exact ContextPlan")
        try:
            # SECURITY: validate the entire immutable graph before dereferencing nested values.
            current_plan = assert_reconstruction_plan(plan)
            digest = (
                OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST
                if current_plan.request.profile.name is PromptProfile.FULL_REFERENCE
                else OFFICIAL_H3_BASE_GUIDE_DIGEST
            )
            binding = build_official_source_profile_binding(
                current_plan.request.profile,
                observed_revision=OFFICIAL_H3_GUIDE_REVISION,
                observed_digest=digest,
            )
            result = render_profiled_prompt(current_plan, binding)
            if not result.is_valid:
                raise ContractValidationError("source-profiled prompt did not validate")
            _register_reconstruction_profiled(current_plan, result)
        except (ContractValidationError, TypeError, ValueError) as exc:
            raise _pipeline_error(
                "source_profiled_render_failed", "source-profiled rendering failed closed"
            ) from exc
        return result.document.text, result, result.document


class H3LocalReconstructionNode:
    """Join visible accepted stages without duplicating their behavior."""

    NODE_ID = LOCAL_RECONSTRUCTION_NODE_ID
    __h3_context_node_id__ = NODE_ID
    RETURN_TYPES = (LOCAL_RECONSTRUCTION_SOCKET_TYPE, PROMPT_STRING_SOCKET_TYPE, REPORT_SOCKET_TYPE)
    RETURN_NAMES = ("reconstruction", "accepted_prompt", "accepted_report")
    FUNCTION = "accept"
    CATEGORY = "h3_context/acceptance"
    DESCRIPTION = "Accepts only a current deterministic manual end-to-end reconstruction route."
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "request": (REQUEST_SOCKET_TYPE, {}),
                "reference_registry": (REFERENCE_SOCKET_TYPE, {}),
                "media": (MEDIA_PRODUCER_SOCKET_TYPE, {}),
                "hard_constraints": ("H3_HARD_CONSTRAINTS", {}),
                "hard_constraints_report": (DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE, {}),
                "intent_graph": (INTENT_GRAPH_SOCKET_TYPE, {}),
                "intent_report": (DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE, {}),
                "evidence_graph": (UNIFIED_EVIDENCE_GRAPH_SOCKET_TYPE, {}),
                "evidence_report": (DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE, {}),
                "cross_reference_graph": (CROSS_REFERENCE_GRAPH_SOCKET_TYPE, {}),
                "cross_reference_report": (DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE, {}),
                "directive_authority": (DIRECTIVE_AUTHORITY_SOCKET_TYPE, {}),
                "directive_report": (DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE, {}),
                "feasible_timeline": (FEASIBLE_AV_TIMELINE_SOCKET_TYPE, {}),
                "hierarchical_reduction": (HIERARCHICAL_REDUCTION_SOCKET_TYPE, {}),
                "semantic_planning": (CONSTRAINED_SEMANTIC_SOCKET_TYPE, {}),
                "plan": (PLAN_SOCKET_TYPE, {}),
                "profiled_prompt": (SOURCE_PROFILED_PROMPT_SOCKET_TYPE, {}),
                "validation": (VALIDATION_SOCKET_TYPE, {}),
                "validated_report": (REPORT_SOCKET_TYPE, {}),
                "native_h3_wiring": (NATIVE_H3_WIRING_SOCKET_TYPE, {}),
            }
        }

    @staticmethod
    def route_dispositions() -> dict[ReconstructionRoute, ReconstructionRouteDisposition]:
        return reconstruction_route_dispositions()

    def accept(
        self,
        request: RawContextRequest,
        reference_registry: ReferenceRegistry,
        media: PerceptionProducerResult,
        hard_constraints: HardConstraintSet,
        hard_constraints_report: DownstreamProducerReport,
        intent_graph: IntentGraph,
        intent_report: DownstreamProducerReport,
        evidence_graph: UnifiedEvidenceGraph,
        evidence_report: DownstreamProducerReport,
        cross_reference_graph: CrossReferenceGraph,
        cross_reference_report: DownstreamProducerReport,
        directive_authority: DirectiveAuthorityBundle,
        directive_report: DownstreamProducerReport,
        feasible_timeline: ReconstructionTimelineStage,
        hierarchical_reduction: ReconstructionReductionStage,
        semantic_planning: ReconstructionSemanticStage,
        plan: ContextPlan,
        profiled_prompt: SourceProfiledPromptResult,
        validation: ValidationResult,
        validated_report: ContextReport,
        native_h3_wiring: NativeH3Wiring,
    ) -> tuple[LocalReconstructionResult, str, ContextReport]:
        try:
            result = issue_local_reconstruction_result(
                request,
                reference_registry,
                media,
                hard_constraints,
                hard_constraints_report,
                intent_graph,
                intent_report,
                evidence_graph,
                evidence_report,
                cross_reference_graph,
                cross_reference_report,
                directive_authority,
                directive_report,
                feasible_timeline,
                hierarchical_reduction,
                semantic_planning,
                plan,
                profiled_prompt,
                validation,
                validated_report,
                native_h3_wiring,
            )
        except (ContractValidationError, TypeError, ValueError) as exc:
            raise _pipeline_error(
                "local_reconstruction_failed", "local reconstruction failed closed"
            ) from exc
        return result, profiled_prompt.document.text, validated_report
