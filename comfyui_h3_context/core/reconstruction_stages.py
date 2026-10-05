"""Visible, model-free M13-06/07/08 adapters for local reconstruction."""

from __future__ import annotations

import hashlib
import json
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TypeVar, cast

from .canonical import canonical_fingerprint
from .constrained_semantic_planning import (
    SemanticPlanningBudget,
    SemanticPlanningPolicy,
    SemanticPlanningProfile,
    SemanticPlanningRequest,
    SemanticPlanningResult,
    unavailable_semantic_planning,
)
from .constraints import TimePoint
from .context_reporting import (
    ContextPlan,
    ContextReport,
    PlanStage,
    PlanStep,
    PlanStepStatus,
    PromptDocument,
    ValidationResult,
)
from .contracts import EvidenceLevel, MediaKind, ProviderIdentity, TaskMode
from .downstream_producer import (
    DirectiveAuthorityBundle,
    DownstreamProducerReport,
    _validate_exact_graph,
    assert_downstream_pair,
)
from .errors import ContractValidationError
from .evidence import (
    EvidenceOrigin,
    EvidenceRecord,
    EvidenceSet,
    EvidenceSource,
    EvidenceSourceKind,
    Provenance,
    SupportStatus,
)
from .feasible_av_timeline_planner import (
    FeasibleAVTimelineRequest,
    FeasibleAVTimelineResult,
    FrameGridPolicy,
    TimelinePlannerStatus,
    build_feasible_av_timeline,
)
from .hierarchical_evidence_reduction import (
    HierarchicalEvidenceReductionRequest,
    HierarchicalEvidenceReductionResult,
    ReductionBudget,
    ReductionEvidence,
    ReductionLevel,
    ReductionModality,
    ReductionTarget,
    build_hierarchical_evidence_reduction,
)
from .intent_graph import IntentGraph
from .model_manifest import ModelBackendFamily
from .native_h3 import NativeH3Wiring
from .normalization import RawContextRequest
from .perception_producer import PerceptionProducerResult
from .registry import ReferenceRegistry
from .source_profiled_prompt import SourceProfiledPromptResult
from .task_mode_retention_classifier import (
    TaskModeRetentionRequest,
    build_task_mode_retention_report,
)
from .unified_evidence_graph import UnifiedEvidenceGraph

RECONSTRUCTION_STAGE_SCHEMA = "h3.reconstruction-stage.v1"
_IdentityT = TypeVar("_IdentityT")


@dataclass(frozen=True)
class _Authority:
    digest: str
    upstream: tuple[object, ...]


# CRITICAL: keep the standard layout; Python 3.10 has no weakref_slot and these values are
# retained by weak-reference registries.
@dataclass(frozen=True, eq=False)
class ReconstructionTimelineStage:
    result: FeasibleAVTimelineResult
    schema: str = RECONSTRUCTION_STAGE_SCHEMA

    def __post_init__(self) -> None:
        if type(self) is not ReconstructionTimelineStage:
            raise ContractValidationError("timeline stage must be exact")
        if type(self.result) is not FeasibleAVTimelineResult or not self.result.is_valid:
            raise ContractValidationError("timeline stage requires a feasible result")
        if self.schema != RECONSTRUCTION_STAGE_SCHEMA:
            raise ContractValidationError("timeline stage schema is unsupported")

    def to_wire(self) -> dict[str, object]:
        assert_reconstruction_timeline(self)
        return _timeline_payload(self)


@dataclass(frozen=True, eq=False)
class ReconstructionReductionStage:
    result: HierarchicalEvidenceReductionResult
    timeline: ReconstructionTimelineStage
    schema: str = RECONSTRUCTION_STAGE_SCHEMA

    def __post_init__(self) -> None:
        if type(self) is not ReconstructionReductionStage:
            raise ContractValidationError("reduction stage must be exact")
        if type(self.result) is not HierarchicalEvidenceReductionResult or self.result.plan is None:
            raise ContractValidationError("reduction stage requires a renderable result")
        if type(self.timeline) is not ReconstructionTimelineStage:
            raise ContractValidationError("reduction stage timeline must be exact")
        if self.schema != RECONSTRUCTION_STAGE_SCHEMA:
            raise ContractValidationError("reduction stage schema is unsupported")

    def to_wire(self) -> dict[str, object]:
        assert_reconstruction_reduction(self)
        return _reduction_payload(self)


@dataclass(frozen=True, eq=False)
class ReconstructionSemanticStage:
    result: SemanticPlanningResult
    reduction: ReconstructionReductionStage
    accepted_plan: ContextPlan
    schema: str = RECONSTRUCTION_STAGE_SCHEMA

    def __post_init__(self) -> None:
        if type(self) is not ReconstructionSemanticStage:
            raise ContractValidationError("semantic stage must be exact")
        if type(self.result) is not SemanticPlanningResult:
            raise ContractValidationError("semantic stage result must be exact")
        if type(self.reduction) is not ReconstructionReductionStage:
            raise ContractValidationError("semantic stage reduction must be exact")
        if type(self.accepted_plan) is not ContextPlan:
            raise ContractValidationError("semantic stage plan must be exact")
        if self.result.is_valid:
            raise ContractValidationError("manual reconstruction cannot claim model enrichment")
        if self.schema != RECONSTRUCTION_STAGE_SCHEMA:
            raise ContractValidationError("semantic stage schema is unsupported")

    def to_wire(self) -> dict[str, object]:
        assert_reconstruction_semantic(self, self.accepted_plan)
        return _semantic_payload(self)


_TIMELINE_AUTH: weakref.WeakKeyDictionary[ReconstructionTimelineStage, _Authority] = (
    weakref.WeakKeyDictionary()
)
_REDUCTION_AUTH: weakref.WeakKeyDictionary[ReconstructionReductionStage, _Authority] = (
    weakref.WeakKeyDictionary()
)
_SEMANTIC_AUTH: weakref.WeakKeyDictionary[ReconstructionSemanticStage, _Authority] = (
    weakref.WeakKeyDictionary()
)
_MAX_LIVE_RUNS = 4096
_SOURCE_PLAN_AUTH: dict[int, ContextPlan] = {}
_PLAN_AUTH: dict[int, tuple[ContextPlan, ReconstructionSemanticStage]] = {}
_CONSUMED_REDUCTIONS: weakref.WeakKeyDictionary[ReconstructionReductionStage, bool] = (
    weakref.WeakKeyDictionary()
)
_PROFILED_AUTH: dict[int, tuple[SourceProfiledPromptResult, ContextPlan, PromptDocument]] = {}
_PROFILED_BY_PLAN: dict[int, tuple[ContextPlan, SourceProfiledPromptResult]] = {}
_DOCUMENT_AUTH: dict[int, tuple[PromptDocument, ContextPlan]] = {}
_VALIDATION_AUTH: dict[
    int, tuple[ContextReport, ValidationResult, ContextPlan, PromptDocument]
] = {}
_VALIDATION_BY_DOCUMENT: dict[int, tuple[PromptDocument, ContextReport]] = {}
_NATIVE_BY_REPORT: dict[int, tuple[ContextReport, NativeH3Wiring]] = {}


def _retain_identity(store: dict[int, _IdentityT], key: int, value: _IdentityT) -> None:
    # SECURITY: strong identity prevents CPython id reuse from authorizing a reconstructed value.
    # The bounded FIFO makes old runs fail closed instead of retaining unbounded process state.
    store[key] = value
    while len(store) > _MAX_LIVE_RUNS:
        del store[next(iter(store))]


def _exact_wire(value: object, expected: type[_StageT], field: str) -> dict[str, object]:
    if type(value) is not expected:
        raise ContractValidationError(f"{field} must be an exact concrete value")
    try:
        post_init = expected.__dict__.get("__post_init__")
        if post_init is not None:
            post_init(value)
        method = expected.__dict__.get("to_wire")
        if method is None:
            raise ContractValidationError(f"{field} has no module-owned wire method")
        wire = method(value)
    except ContractValidationError:
        raise
    except Exception as exc:
        raise ContractValidationError(f"{field} current-value validation failed") from exc
    if type(wire) is not dict:
        raise ContractValidationError(f"{field} wire value is malformed")
    return cast(dict[str, object], wire)


def _timeline_payload(stage: ReconstructionTimelineStage) -> dict[str, object]:
    return {
        "schema": stage.schema,
        "kind": "feasible_timeline",
        "result": _exact_wire(stage.result, FeasibleAVTimelineResult, "timeline result"),
    }


def _reduction_payload(stage: ReconstructionReductionStage) -> dict[str, object]:
    return {
        "schema": stage.schema,
        "kind": "hierarchical_reduction",
        "timeline_fingerprint": canonical_fingerprint(_timeline_payload(stage.timeline)),
        "result": _exact_wire(
            stage.result,
            HierarchicalEvidenceReductionResult,
            "reduction result",
        ),
    }


def _semantic_payload(stage: ReconstructionSemanticStage) -> dict[str, object]:
    _validate_exact_graph(stage.accepted_plan)
    return {
        "schema": stage.schema,
        "kind": "constrained_semantic_planning",
        "reduction_fingerprint": canonical_fingerprint(_reduction_payload(stage.reduction)),
        "plan_fingerprint": "sha256:"
        + hashlib.sha256(
            json.dumps(
                stage.accepted_plan.to_wire(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
        "result": _exact_wire(stage.result, SemanticPlanningResult, "semantic result"),
    }


_StageT = TypeVar("_StageT")


def _assert_authority(
    value: object, expected: type[_StageT], store: Mapping[_StageT, _Authority]
) -> _Authority:
    if type(value) is not expected:
        raise ContractValidationError("reconstruction stage must be an exact concrete value")
    authority = store.get(value)
    if authority is None:
        raise ContractValidationError("reconstruction stage is forged, copied, or stale")
    return authority


def assert_reconstruction_timeline(
    value: object,
    request: RawContextRequest | None = None,
    registry: ReferenceRegistry | None = None,
    intent: IntentGraph | None = None,
    directives: DirectiveAuthorityBundle | None = None,
) -> ReconstructionTimelineStage:
    authority = _assert_authority(value, ReconstructionTimelineStage, _TIMELINE_AUTH)
    stage = cast(ReconstructionTimelineStage, value)
    if authority.digest != canonical_fingerprint(_timeline_payload(stage)):
        raise ContractValidationError("timeline stage changed after issuance")
    supplied = (request, registry, intent, directives)
    if any(item is not None for item in supplied) and any(
        left is not right for left, right in zip(authority.upstream, supplied, strict=True)
    ):
        raise ContractValidationError("timeline stage does not authorize these exact upstreams")
    return stage


def assert_reconstruction_reduction(
    value: object,
    evidence: UnifiedEvidenceGraph | None = None,
    timeline: ReconstructionTimelineStage | None = None,
) -> ReconstructionReductionStage:
    authority = _assert_authority(value, ReconstructionReductionStage, _REDUCTION_AUTH)
    stage = cast(ReconstructionReductionStage, value)
    assert_reconstruction_timeline(stage.timeline)
    if authority.digest != canonical_fingerprint(_reduction_payload(stage)):
        raise ContractValidationError("reduction stage changed after issuance")
    supplied = (evidence, timeline)
    if any(item is not None for item in supplied) and any(
        left is not right for left, right in zip(authority.upstream, supplied, strict=True)
    ):
        raise ContractValidationError("reduction stage does not authorize these exact upstreams")
    return stage


def assert_reconstruction_semantic(
    value: object, plan: ContextPlan | None = None
) -> ReconstructionSemanticStage:
    authority = _assert_authority(value, ReconstructionSemanticStage, _SEMANTIC_AUTH)
    stage = cast(ReconstructionSemanticStage, value)
    assert_reconstruction_reduction(stage.reduction)
    if authority.digest != canonical_fingerprint(_semantic_payload(stage)):
        raise ContractValidationError("semantic stage changed after issuance")
    if plan is not None and (stage.accepted_plan is not plan or authority.upstream[2] is not plan):
        raise ContractValidationError("semantic stage does not authorize this exact plan")
    return stage


def assert_reconstruction_plan(value: object) -> ContextPlan:
    if type(value) is not ContextPlan:
        raise ContractValidationError("reconstruction plan must be an exact ContextPlan")
    retained = _PLAN_AUTH.get(id(value))
    if retained is None or retained[0] is not value:
        raise ContractValidationError("reconstruction plan is reconstructed, copied, or stale")
    _validate_exact_graph(value)
    assert_reconstruction_semantic(retained[1], value)
    return value


def _register_reconstruction_source_plan(plan: ContextPlan) -> None:
    if type(plan) is not ContextPlan:
        raise ContractValidationError("source plan must be an exact ContextPlan")
    _validate_exact_graph(plan)
    _retain_identity(_SOURCE_PLAN_AUTH, id(plan), plan)


def assert_reconstruction_source_plan(value: object) -> ContextPlan:
    if type(value) is not ContextPlan:
        raise ContractValidationError("source plan must be an exact ContextPlan")
    retained = _SOURCE_PLAN_AUTH.get(id(value))
    if retained is not value:
        raise ContractValidationError("source plan is reconstructed, copied, or stale")
    _validate_exact_graph(value)
    return value


def _register_reconstruction_profiled(
    plan: ContextPlan, result: SourceProfiledPromptResult
) -> None:
    current_plan = assert_reconstruction_plan(plan)
    if type(result) is not SourceProfiledPromptResult or result.document.plan_id != plan.plan_id:
        raise ContractValidationError("profiled result does not belong to the reconstruction plan")
    _exact_wire(result, SourceProfiledPromptResult, "profiled reconstruction result")
    prior = _PROFILED_BY_PLAN.get(id(current_plan))
    if prior is not None and (prior[0] is not current_plan or prior[1] is not result):
        raise ContractValidationError("reconstruction plan already issued a profiled result")
    retained = (result, current_plan, result.document)
    _retain_identity(_PROFILED_AUTH, id(result), retained)
    _retain_identity(_PROFILED_BY_PLAN, id(current_plan), (current_plan, result))
    _retain_identity(_DOCUMENT_AUTH, id(result.document), (result.document, current_plan))


def assert_reconstruction_profiled(value: object, plan: ContextPlan) -> SourceProfiledPromptResult:
    current_plan = assert_reconstruction_plan(plan)
    if type(value) is not SourceProfiledPromptResult:
        raise ContractValidationError("profiled reconstruction result must be exact")
    retained = _PROFILED_AUTH.get(id(value))
    if (
        retained is None
        or retained[0] is not value
        or retained[1] is not current_plan
        or retained[2] is not value.document
    ):
        raise ContractValidationError("profiled reconstruction result is copied or stale")
    _exact_wire(value, SourceProfiledPromptResult, "profiled reconstruction result")
    return value


def _register_reconstruction_validation(
    plan: object,
    document: object,
    validation: ValidationResult,
    report: ContextReport,
) -> bool:
    retained_plan = _PLAN_AUTH.get(id(plan))
    if retained_plan is None or retained_plan[0] is not plan:
        return False
    current_plan = assert_reconstruction_plan(plan)
    retained_document = _DOCUMENT_AUTH.get(id(document))
    if (
        type(document) is not PromptDocument
        or retained_document is None
        or retained_document[0] is not document
        or retained_document[1] is not current_plan
    ):
        raise ContractValidationError("reconstruction document is copied or stale")
    if (
        type(validation) is not ValidationResult
        or type(report) is not ContextReport
        or report.plan is not current_plan
        or report.prompt_document is not document
        or report.validation is not validation
    ):
        raise ContractValidationError("reconstruction validation/report join is not exact")
    _exact_wire(validation, ValidationResult, "reconstruction validation")
    _exact_wire(report, ContextReport, "reconstruction report")
    prior = _VALIDATION_BY_DOCUMENT.get(id(document))
    if prior is not None and (prior[0] is not document or prior[1] is not report):
        raise ContractValidationError("reconstruction document already issued validation")
    _retain_identity(
        _VALIDATION_AUTH,
        id(report),
        (report, validation, current_plan, document),
    )
    _retain_identity(_VALIDATION_BY_DOCUMENT, id(document), (document, report))
    return True


def _register_reconstruction_native(report: object, wiring: NativeH3Wiring) -> bool:
    retained = _VALIDATION_AUTH.get(id(report))
    if retained is None:
        return False
    if retained[0] is not report or type(wiring) is not NativeH3Wiring:
        raise ContractValidationError("reconstruction native inputs are not exact")
    prior = _NATIVE_BY_REPORT.get(id(report))
    if prior is not None and (prior[0] is not report or prior[1] is not wiring):
        raise ContractValidationError("reconstruction report already issued native wiring")
    _retain_identity(_NATIVE_BY_REPORT, id(report), (report, wiring))
    return True


def assert_reconstruction_validation(
    plan: ContextPlan,
    profiled: SourceProfiledPromptResult,
    validation: object,
    report: object,
) -> tuple[ValidationResult, ContextReport]:
    current_plan = assert_reconstruction_plan(plan)
    current_profiled = assert_reconstruction_profiled(profiled, current_plan)
    if type(validation) is not ValidationResult or type(report) is not ContextReport:
        raise ContractValidationError("reconstruction validation/report values must be exact")
    retained = _VALIDATION_AUTH.get(id(report))
    if (
        retained is None
        or retained[0] is not report
        or retained[1] is not validation
        or retained[2] is not current_plan
        or retained[3] is not current_profiled.document
    ):
        raise ContractValidationError("reconstruction validation/report is reconstructed or stale")
    _exact_wire(validation, ValidationResult, "reconstruction validation")
    _exact_wire(report, ContextReport, "reconstruction report")
    return validation, report


def build_reconstruction_timeline(
    request: RawContextRequest,
    registry: ReferenceRegistry,
    intent: IntentGraph,
    intent_report: DownstreamProducerReport,
    directives: DirectiveAuthorityBundle,
    directive_report: DownstreamProducerReport,
) -> ReconstructionTimelineStage:
    for root in (request, registry, intent, directives):
        _validate_exact_graph(root)
    assert_downstream_pair(intent_report, intent, (request, registry))
    assert_downstream_pair(
        directive_report, directives, (request, registry, request.hard_constraints, intent)
    )
    if type(request.mode) is not TaskMode or intent.registry is not registry:
        raise ContractValidationError("timeline inputs do not share exact mode/registry authority")
    mode_report = build_task_mode_retention_report(
        TaskModeRetentionRequest.from_user_mode(request.mode, registry)
    )
    result = build_feasible_av_timeline(
        FeasibleAVTimelineRequest(
            mode_report,
            TimePoint.from_text(
                str(request.duration_seconds)
                if request.duration_seconds is not None
                else str(max(1, int(intent.effective_duration.seconds)))
            ),
            FrameGridPolicy(24, 1),
            registry,
        )
    )
    if result.status not in {TimelinePlannerStatus.COMPLETE, TimelinePlannerStatus.PARTIAL}:
        raise ContractValidationError("feasible timeline did not produce a renderable plan")
    stage = ReconstructionTimelineStage(result)
    upstream = (request, registry, intent, directives)
    _TIMELINE_AUTH[stage] = _Authority(canonical_fingerprint(_timeline_payload(stage)), upstream)
    return stage


def _reduction_modality(kind: MediaKind) -> ReductionModality:
    return ReductionModality.AUDIO if kind is MediaKind.AUDIO else ReductionModality.VISUAL


def build_reconstruction_reduction(
    media: PerceptionProducerResult,
    evidence: UnifiedEvidenceGraph,
    evidence_report: DownstreamProducerReport,
    timeline: ReconstructionTimelineStage,
) -> ReconstructionReductionStage:
    stage_timeline = assert_reconstruction_timeline(timeline)
    assert_downstream_pair(evidence_report, evidence, (media,))
    _validate_exact_graph(evidence)
    if stage_timeline.result.plan is None or not evidence.observations:
        raise ContractValidationError("reduction requires a feasible timeline and evidence")
    items = tuple(
        ReductionEvidence(
            f"evidence.{index}",
            ReductionLevel.ASSET,
            _reduction_modality(observation.modality),
            observation.label,
            (observation.observation_id, observation.asset_id),
            token_estimate=max(1, min(256, len(observation.label) // 4 + 1)),
            fidelity_weight=1,
            salience=Decimal("0.5"),
            ambiguity_ids=observation.uncertainty_ids,
        )
        for index, observation in enumerate(evidence.observations, start=1)
    )
    focus_ids = tuple(source_id for item in items for source_id in item.source_ids)
    ambiguity_ids = tuple(uncertainty for item in items for uncertainty in item.ambiguity_ids)
    result = build_hierarchical_evidence_reduction(
        HierarchicalEvidenceReductionRequest(
            evidence,
            stage_timeline.result.plan,
            items,
            ReductionTarget(
                "target.reconstruction",
                focus_source_ids=focus_ids,
                required_item_ids=tuple(item.item_id for item in items),
                high_risk_ids=ambiguity_ids,
            ),
            ReductionBudget(64, 65_536, Decimal("0"), Decimal("0"), Decimal("0")),
        )
    )
    if result.plan is None:
        raise ContractValidationError("hierarchical reduction did not produce a plan")
    stage = ReconstructionReductionStage(result, stage_timeline)
    upstream = (evidence, stage_timeline)
    _REDUCTION_AUTH[stage] = _Authority(canonical_fingerprint(_reduction_payload(stage)), upstream)
    return stage


def build_reconstruction_semantic_disposition(
    reduction: ReconstructionReductionStage,
    plan: ContextPlan,
) -> ReconstructionSemanticStage:
    stage_reduction = assert_reconstruction_reduction(reduction)
    plan = assert_reconstruction_source_plan(plan)
    if _CONSUMED_REDUCTIONS.get(stage_reduction) is True:
        raise ContractValidationError("reduction already issued a semantic reconstruction stage")
    if stage_reduction.result.plan is None:
        raise ContractValidationError("semantic planning requires a reduction plan")
    timeline_plan = stage_reduction.timeline.result.plan
    if timeline_plan is None:
        raise ContractValidationError("semantic planning requires a timeline plan")
    request = SemanticPlanningRequest(
        stage_reduction.result.plan,
        timeline_plan,
        SemanticPlanningProfile(
            "manual.unqualified",
            ModelBackendFamily.COMFYUI_NATIVE,
            "unqualified-model",
            "sha256:" + "0" * 64,
            seed=0,
        ),
        SemanticPlanningPolicy.STRICT,
        budget=SemanticPlanningBudget(4, 128, 1024, 16),
    )
    result = unavailable_semantic_planning(
        request, "deterministic manual route does not authorize model enrichment"
    )
    timeline_fingerprint = timeline_plan.fingerprint
    reduction_fingerprint = stage_reduction.result.plan.fingerprint
    semantic_fingerprint = canonical_fingerprint(result.to_wire())
    source = EvidenceSource(EvidenceSourceKind.SYSTEM_DERIVED, "m13-10.reconstruction")
    provenance = Provenance(
        source,
        ProviderIdentity.MANUAL,
        EvidenceLevel.FRAMEWORK_REFERENCE,
        source_revision=RECONSTRUCTION_STAGE_SCHEMA,
    )
    chain_evidence = (
        EvidenceRecord(
            "reconstruction.timeline",
            f"Feasible audiovisual timeline accepted {len(timeline_plan.shots)} planned shots.",
            EvidenceOrigin.DERIVED,
            SupportStatus.SUPPORTED,
            provenance,
        ),
        EvidenceRecord(
            "reconstruction.reduction",
            (
                "Hierarchical evidence reduction retained "
                f"{len(stage_reduction.result.plan.retained_items)} bounded evidence items."
            ),
            EvidenceOrigin.DERIVED,
            SupportStatus.SUPPORTED,
            provenance,
        ),
        EvidenceRecord(
            "reconstruction.semantic",
            "Semantic model enrichment was explicitly unavailable and contributed no content.",
            EvidenceOrigin.DERIVED,
            SupportStatus.SUPPORTED,
            provenance,
        ),
    )
    evidence = EvidenceSet(plan.evidence.records + chain_evidence)
    accepted_request = replace(plan.request, evidence=evidence)
    chain_steps = (
        PlanStep(
            "step_feasible_timeline",
            PlanStage.ASSEMBLE_INTENT,
            PlanStepStatus.COMPLETED,
            "consume the exact feasible audiovisual timeline",
            output_ids=("timeline." + timeline_fingerprint.removeprefix("sha256:"),),
        ),
        PlanStep(
            "step_hierarchical_reduction",
            PlanStage.ASSEMBLE_INTENT,
            PlanStepStatus.COMPLETED,
            "consume the exact hierarchical evidence reduction",
            input_evidence_ids=("reconstruction.timeline",),
            output_ids=("reduction." + reduction_fingerprint.removeprefix("sha256:"),),
        ),
        PlanStep(
            "step_semantic_disposition",
            PlanStage.ASSEMBLE_INTENT,
            PlanStepStatus.COMPLETED,
            "retain the explicit model-free semantic planning disposition",
            input_evidence_ids=("reconstruction.reduction",),
            output_ids=("semantic." + semantic_fingerprint.removeprefix("sha256:"),),
        ),
    )
    plan_payload = {
        "source_plan": plan.to_wire(),
        "timeline": timeline_fingerprint,
        "reduction": reduction_fingerprint,
        "semantic": semantic_fingerprint,
    }
    plan_digest = hashlib.sha256(
        json.dumps(
            plan_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    accepted_plan = ContextPlan(
        plan_id="reconstruction_" + plan_digest[:24],
        schema_version=plan.schema_version,
        request=accepted_request,
        intent_graph=plan.intent_graph,
        hard_constraints=plan.hard_constraints,
        evidence=evidence,
        steps=plan.steps + chain_steps,
        limitations=plan.limitations,
        diagnostics=plan.diagnostics,
    )
    stage = ReconstructionSemanticStage(result, stage_reduction, accepted_plan)
    upstream = (stage_reduction, plan, accepted_plan)
    _SEMANTIC_AUTH[stage] = _Authority(canonical_fingerprint(_semantic_payload(stage)), upstream)
    # SECURITY: a weak-key tombstone survives live-registry eviction while the caller still owns
    # the reduction, preventing an old execution graph from being reissued as a new success.
    _CONSUMED_REDUCTIONS[stage_reduction] = True
    _retain_identity(_PLAN_AUTH, id(accepted_plan), (accepted_plan, stage))
    return stage


__all__ = [
    "RECONSTRUCTION_STAGE_SCHEMA",
    "ReconstructionReductionStage",
    "ReconstructionSemanticStage",
    "ReconstructionTimelineStage",
    "assert_reconstruction_reduction",
    "assert_reconstruction_plan",
    "assert_reconstruction_profiled",
    "assert_reconstruction_semantic",
    "assert_reconstruction_source_plan",
    "assert_reconstruction_timeline",
    "assert_reconstruction_validation",
    "build_reconstruction_reduction",
    "build_reconstruction_semantic_disposition",
    "build_reconstruction_timeline",
]
