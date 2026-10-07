"""M13-10 public local-reconstruction end-to-end acceptance tests."""

from __future__ import annotations

import json
import pickle
import weakref
from copy import copy, deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from comfyui_h3_context.core import (
    LocalReconstructionResult,
    MediaAdmissionEvidence,
    ReconstructionRoute,
    ReconstructionRouteDisposition,
    canonical_fingerprint,
    reconstruction_stages,
    validate_local_reconstruction_wire,
)
from comfyui_h3_context.core.errors import ContractValidationError
from comfyui_h3_context.nodes import (
    LOCAL_RECONSTRUCTION_NODE_ID,
    NODE_CLASS_MAPPINGS,
    SOURCE_PROFILED_RENDERER_NODE_ID,
    H3ConstrainedSemanticPlanningNode,
    H3ContextNativeH3AdapterNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
    H3CrossReferenceProducerNode,
    H3DirectiveAuthorityProducerNode,
    H3EvidenceFusionProducerNode,
    H3FeasibleAVTimelineNode,
    H3HardConstraintProducerNode,
    H3HierarchicalEvidenceReductionNode,
    H3IntentGraphProducerNode,
    H3LocalReconstructionNode,
    H3MediaAdmissionProducerNode,
    H3ReferenceRegistryNode,
    H3SourceProfiledRendererNode,
    PipelineNodeError,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FINGERPRINT = "7" * 64


def _execute_manual_route() -> tuple[Any, ...]:
    opaque_image = object()
    registry = H3ReferenceRegistryNode().build_registry(images=[opaque_image])[0]
    constraints, constraints_report = H3HardConstraintProducerNode().produce(
        dialogue='Narrator: "Enter now."',
        visible_text="GATE 7",
        required_content="traveler",
        forbidden_content="watermark",
    )
    request = H3ContextRequestNode().build_request(
        "ref2va",
        "A traveler enters a quiet room.",
        duration_seconds=5.0,
        hard_constraints=constraints,
    )[0]
    intent, intent_report = H3IntentGraphProducerNode().produce(
        request,
        registry,
        subject_label="traveler",
        action_description="enters the quiet room",
    )
    media = H3MediaAdmissionProducerNode().admit(
        media_kind="image",
        asset_id="image_1",
        admission_evidence=MediaAdmissionEvidence(
            declared_source_fingerprint=SOURCE_FINGERPRINT,
            width_pixels=64,
            height_pixels=48,
            duration_seconds=0.0,
            sample_rate_hz=None,
            channel_count=None,
            reference_role="reference",
            reference_order=0,
        ),
        image=opaque_image,
    )[0]
    evidence, evidence_report = H3EvidenceFusionProducerNode().produce(media)
    cross, cross_report = H3CrossReferenceProducerNode().produce(registry, media)
    directives, directive_report = H3DirectiveAuthorityProducerNode().produce(
        request,
        registry,
        constraints,
        constraints_report,
        intent,
        intent_report,
    )
    feasible = H3FeasibleAVTimelineNode().plan(
        request, registry, intent, intent_report, directives, directive_report
    )[0]
    reduction = H3HierarchicalEvidenceReductionNode().reduce(
        media, evidence, evidence_report, feasible
    )[0]
    initial_plan = H3ContextPlanNode().build_plan(request, registry, intent)[0]
    semantic, plan = H3ConstrainedSemanticPlanningNode().plan(reduction, initial_plan)
    prompt, profiled, document = H3SourceProfiledRendererNode().render(plan)
    validation, report = H3ContextValidatorNode().validate(plan, document)
    native_prompt, wiring = H3ContextNativeH3AdapterNode().adapt(report)
    acceptance_inputs = (
        request,
        registry,
        media,
        constraints,
        constraints_report,
        intent,
        intent_report,
        evidence,
        evidence_report,
        cross,
        cross_report,
        directives,
        directive_report,
        feasible,
        reduction,
        semantic,
        plan,
        profiled,
        validation,
        report,
        wiring,
    )
    result = H3LocalReconstructionNode().accept(*acceptance_inputs)[0]
    return (
        result,
        prompt,
        native_prompt,
        plan,
        profiled,
        validation,
        report,
        wiring,
        acceptance_inputs,
    )


def _execute_scenario_route(scenario: str) -> tuple[Any, str, Any]:
    fixture = json.loads(
        (ROOT / f"workflows/m15_02_downstream_{scenario}.json").read_text(encoding="utf-8")
    )
    nodes = fixture["prompt"]
    opaque_image = object()
    registry = H3ReferenceRegistryNode().build_registry(images=[opaque_image])[0]
    constraints, constraints_report = H3HardConstraintProducerNode().produce(**nodes["4"]["inputs"])
    request_inputs = nodes["1"]["inputs"]
    request = H3ContextRequestNode().build_request(
        request_inputs["task_mode"],
        request_inputs["user_intent"],
        duration_seconds=request_inputs["duration_seconds"],
        hard_constraints=constraints,
    )[0]
    intent_inputs = dict(nodes["5"]["inputs"])
    intent_inputs.pop("request")
    intent_inputs.pop("reference_registry")
    intent, intent_report = H3IntentGraphProducerNode().produce(request, registry, **intent_inputs)
    media_inputs = dict(nodes["6"]["inputs"])
    media_inputs.pop("image")
    media = H3MediaAdmissionProducerNode().admit(**media_inputs, image=opaque_image)[0]
    evidence, evidence_report = H3EvidenceFusionProducerNode().produce(media)
    cross_inputs = dict(nodes["8"]["inputs"])
    cross_inputs.pop("reference_registry")
    cross_inputs.pop("media")
    cross, cross_report = H3CrossReferenceProducerNode().produce(registry, media, **cross_inputs)
    directive_inputs = dict(nodes["9"]["inputs"])
    for link_name in (
        "request",
        "reference_registry",
        "hard_constraints",
        "hard_constraints_report",
        "intent_graph",
        "intent_report",
    ):
        directive_inputs.pop(link_name)
    directives, directive_report = H3DirectiveAuthorityProducerNode().produce(
        request,
        registry,
        constraints,
        constraints_report,
        intent,
        intent_report,
        **directive_inputs,
    )
    feasible = H3FeasibleAVTimelineNode().plan(
        request, registry, intent, intent_report, directives, directive_report
    )[0]
    reduction = H3HierarchicalEvidenceReductionNode().reduce(
        media, evidence, evidence_report, feasible
    )[0]
    initial_plan = H3ContextPlanNode().build_plan(request, registry, intent)[0]
    semantic, plan = H3ConstrainedSemanticPlanningNode().plan(reduction, initial_plan)
    prompt, profiled, document = H3SourceProfiledRendererNode().render(plan)
    validation, report = H3ContextValidatorNode().validate(plan, document)
    wiring = H3ContextNativeH3AdapterNode().adapt(report)[1]
    result = H3LocalReconstructionNode().accept(
        request,
        registry,
        media,
        constraints,
        constraints_report,
        intent,
        intent_report,
        evidence,
        evidence_report,
        cross,
        cross_report,
        directives,
        directive_report,
        feasible,
        reduction,
        semantic,
        plan,
        profiled,
        validation,
        report,
        wiring,
    )[0]
    return result, prompt, cross


def test_public_manual_route_completes_with_exact_constraints_and_native_wiring() -> None:
    result, prompt, native_prompt, plan, profiled, validation, report, wiring = (
        _execute_manual_route()[:8]
    )

    assert isinstance(result, LocalReconstructionResult)
    assert result.is_complete
    assert weakref.ref(result)() is result
    assert result.route is ReconstructionRoute.DETERMINISTIC_MANUAL
    assert result.disposition is ReconstructionRouteDisposition.COMPLETE
    assert prompt == native_prompt == report.prompt_document.text
    assert profiled.document == report.prompt_document
    assert validation.status.value == "passed"
    assert wiring.prompt_fingerprint == canonical_fingerprint(report.prompt_document.text)
    assert 'Narrator: "Enter now."' in prompt
    assert "GATE 7" in prompt
    assert result.to_wire()["schema"] == "h3-local-reconstruction/1"


def test_result_wire_has_structural_and_semantic_validation() -> None:
    result = _execute_manual_route()[0]
    wire = result.to_wire()
    schema = json.loads(
        (ROOT / "governance/contracts/local_reconstruction_v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(wire)) == []
    validate_local_reconstruction_wire(wire)

    stale = json.loads(json.dumps(wire))
    stale["prompt_fingerprint"] = "sha256:" + "0" * 64
    contradictory = json.loads(json.dumps(wire))
    contradictory["disposition"] = "unavailable"
    for mutation in (stale, contradictory):
        with pytest.raises(ContractValidationError):
            validate_local_reconstruction_wire(mutation)


def test_direct_copy_and_mutated_results_cannot_authorize_success() -> None:
    result = _execute_manual_route()[0]
    direct = LocalReconstructionResult(
        result.route,
        result.disposition,
        result.stage_fingerprints,
        result.prompt_fingerprint,
        result.report_fingerprint,
        result.wiring_fingerprint,
        result.limitations,
        result.run_fingerprint,
    )
    serialized_copy = pickle.loads(pickle.dumps(result))  # noqa: S301 - trusted test object
    for forged in (direct, copy(result), deepcopy(result), serialized_copy):
        with pytest.raises(ContractValidationError):
            forged.to_wire()
    object.__setattr__(result, "route", ReconstructionRoute.OLLAMA)
    with pytest.raises(ContractValidationError):
        result.to_wire()


def test_malformed_result_and_wire_fail_closed_before_caller_dispatch() -> None:
    result = _execute_manual_route()[0]
    with pytest.raises(ContractValidationError):
        LocalReconstructionResult(
            result.route,
            result.disposition,
            (object(),),  # type: ignore[arg-type]
            result.prompt_fingerprint,
            result.report_fingerprint,
            result.wiring_fingerprint,
            result.limitations,
            result.run_fingerprint,
        )

    class HostileString(str):
        def __eq__(self, other: object) -> bool:
            raise RuntimeError("caller equality must not run")

    wire = result.to_wire()
    route_matrix = wire["route_dispositions"]
    assert isinstance(route_matrix, list)
    route_matrix[0]["route"] = HostileString("deterministic_manual")
    with pytest.raises(ContractValidationError):
        validate_local_reconstruction_wire(wire)

    hostile_schema = result.to_wire()
    hostile_schema["schema"] = HostileString("h3-local-reconstruction/1")
    with pytest.raises(ContractValidationError):
        validate_local_reconstruction_wire(hostile_schema)


def test_source_profiled_node_rejects_malformed_nested_plan_with_project_error() -> None:
    plan = _execute_manual_route()[3]
    object.__setattr__(plan, "request", object())
    with pytest.raises(PipelineNodeError):
        H3SourceProfiledRendererNode().render(plan)


def test_acceptance_rejects_hostile_report_without_calling_it() -> None:
    acceptance_inputs = list(_execute_manual_route()[8])

    class HostileReport:
        called = False

        def to_wire(self) -> dict[str, object]:
            self.called = True
            raise RuntimeError("caller report method must not run")

    hostile = HostileReport()
    acceptance_inputs[4] = hostile
    with pytest.raises(PipelineNodeError):
        H3LocalReconstructionNode().accept(*acceptance_inputs)
    assert hostile.called is False


def test_result_authority_binds_each_producer_report_after_issuance() -> None:
    route = _execute_manual_route()
    result = route[0]
    acceptance_inputs = route[8]
    stages = [item["stage"] for item in result.to_wire()["stage_fingerprints"]]
    assert "hard_constraints_report" in stages
    assert "intent_report" in stages
    assert "evidence_report" in stages
    assert "cross_reference_report" in stages
    assert "directive_report" in stages
    assert stages[13:16] == [
        "feasible_timeline",
        "hierarchical_reduction",
        "semantic_planning",
    ]

    object.__setattr__(acceptance_inputs[4], "provenance", "stale")
    with pytest.raises(ContractValidationError):
        result.to_wire()


def test_acceptance_rejects_same_fingerprint_report_from_another_execution() -> None:
    for report_index in (4, 6, 8, 10, 12):
        first = list(_execute_manual_route()[8])
        second = _execute_manual_route()[8]
        first[report_index] = second[report_index]
        with pytest.raises(PipelineNodeError):
            H3LocalReconstructionNode().accept(*first)


def test_acceptance_rejects_deepcopied_late_stage() -> None:
    for stage_index in range(13, 21):
        inputs = list(_execute_manual_route()[8])
        inputs[stage_index] = deepcopy(inputs[stage_index])
        with pytest.raises(PipelineNodeError):
            H3LocalReconstructionNode().accept(*inputs)


def test_acceptance_rejects_planning_stage_from_another_execution() -> None:
    for stage_index in (13, 14, 15):
        first = list(_execute_manual_route()[8])
        second = _execute_manual_route()[8]
        first[stage_index] = second[stage_index]
        with pytest.raises(PipelineNodeError):
            H3LocalReconstructionNode().accept(*first)


def test_manual_planning_chain_is_executed_but_model_enrichment_is_not_claimed() -> None:
    inputs = _execute_manual_route()[8]
    feasible, reduction, semantic, plan = inputs[13:17]
    assert feasible.result.plan is not None
    assert reduction.result.plan is not None
    assert reduction.result.plan.timeline_fingerprint == feasible.result.plan.fingerprint
    assert semantic.result.status.value == "unavailable"
    assert semantic.result.document is None
    assert semantic.accepted_plan is plan


def test_manual_planning_chain_materializes_the_renderer_plan() -> None:
    inputs = _execute_manual_route()[8]
    request, registry, intent = inputs[0], inputs[1], inputs[5]
    feasible, reduction, semantic, plan = inputs[13:17]
    legacy = H3ContextPlanNode().build_plan(request, registry, intent)[0]
    assert plan is semantic.accepted_plan
    assert plan.plan_id != legacy.plan_id
    assert tuple(step.step_id for step in plan.steps[-3:]) == (
        "step_feasible_timeline",
        "step_hierarchical_reduction",
        "step_semantic_disposition",
    )
    assert len(plan.request.evidence.records) >= 3
    assert plan.evidence is plan.request.evidence
    assert feasible.result.plan is not None
    assert reduction.result.plan is not None
    assert plan.steps[-2].output_ids == (
        "reduction." + reduction.result.plan.fingerprint.removeprefix("sha256:"),
    )
    profiled = inputs[17]
    assert {
        "reconstruction.timeline",
        "reconstruction.reduction",
        "reconstruction.semantic",
    } <= set(profiled.document.source_evidence_ids)


def test_acceptance_rejects_shallow_profiled_copy_and_reconstructed_late_chain() -> None:
    inputs = list(_execute_manual_route()[8])
    inputs[17] = copy(inputs[17])
    with pytest.raises(PipelineNodeError):
        H3LocalReconstructionNode().accept(*inputs)

    inputs = list(_execute_manual_route()[8])
    validation = replace(inputs[18])
    report = replace(inputs[19], validation=validation)
    wiring = H3ContextNativeH3AdapterNode().adapt(report)[1]
    inputs[18:21] = (validation, report, wiring)
    with pytest.raises(PipelineNodeError):
        H3LocalReconstructionNode().accept(*inputs)


def test_reconstructed_plan_cannot_reissue_a_complete_chain() -> None:
    inputs = list(_execute_manual_route()[8])
    source_plan = H3ContextPlanNode().build_plan(inputs[0], inputs[1], inputs[5])[0]
    reconstructed = replace(source_plan)
    with pytest.raises(PipelineNodeError):
        H3ConstrainedSemanticPlanningNode().plan(inputs[14], reconstructed)

    reconstructed_accepted = replace(inputs[16])
    with pytest.raises(PipelineNodeError):
        H3ConstrainedSemanticPlanningNode().plan(inputs[14], reconstructed_accepted)


def test_reconstruction_authority_issuers_are_not_public() -> None:
    for name in (
        "register_reconstruction_source_plan",
        "register_reconstruction_profiled",
        "register_reconstruction_validation",
    ):
        assert not hasattr(reconstruction_stages, name)


def test_same_reduction_cannot_issue_a_second_late_stage_chain() -> None:
    inputs = list(_execute_manual_route()[8])
    second_source = H3ContextPlanNode().build_plan(inputs[0], inputs[1], inputs[5])[0]
    with pytest.raises(PipelineNodeError):
        H3ConstrainedSemanticPlanningNode().plan(inputs[14], second_source)


def test_validation_and_native_wiring_are_one_shot_for_reconstruction() -> None:
    inputs = list(_execute_manual_route()[8])
    with pytest.raises(PipelineNodeError):
        H3ContextValidatorNode().validate(inputs[16], inputs[17].document)
    with pytest.raises(PipelineNodeError):
        H3ContextNativeH3AdapterNode().adapt(inputs[19])


def test_evicted_complete_result_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reconstruction_stages, "_MAX_LIVE_RUNS", 1)
    old_route = _execute_manual_route()
    old_result = old_route[0]
    old_inputs = old_route[8]
    _execute_manual_route()
    with pytest.raises(ContractValidationError):
        old_result.to_wire()
    replacement_source = H3ContextPlanNode().build_plan(
        old_inputs[0], old_inputs[1], old_inputs[5]
    )[0]
    with pytest.raises(PipelineNodeError):
        H3ConstrainedSemanticPlanningNode().plan(old_inputs[14], replacement_source)


def test_mutated_timeline_result_fails_with_project_errors() -> None:
    inputs = list(_execute_manual_route()[8])
    timeline = inputs[13]
    object.__setattr__(timeline, "result", object())
    with pytest.raises(PipelineNodeError):
        H3HierarchicalEvidenceReductionNode().reduce(inputs[2], inputs[7], inputs[8], timeline)
    with pytest.raises(PipelineNodeError):
        H3LocalReconstructionNode().accept(*inputs)


def test_unqualified_optional_routes_are_explicit_and_never_fallback() -> None:
    dispositions = H3LocalReconstructionNode.route_dispositions()
    assert dispositions[ReconstructionRoute.DETERMINISTIC_MANUAL] is (
        ReconstructionRouteDisposition.COMPLETE
    )
    for route in (
        ReconstructionRoute.FULL_REFERENCE_PERCEPTION,
        ReconstructionRoute.COMFYUI_NATIVE,
        ReconstructionRoute.OLLAMA,
        ReconstructionRoute.SPECIALIST,
    ):
        assert dispositions[route] is ReconstructionRouteDisposition.NOT_QUALIFIED
    assert dispositions[ReconstructionRoute.OFFICIAL_ORACLE] is (
        ReconstructionRouteDisposition.NOT_AUTHORIZED
    )
    assert dispositions[ReconstructionRoute.FIXED_H3] is (
        ReconstructionRouteDisposition.NOT_AUTHORIZED
    )


@pytest.mark.parametrize(  # type: ignore[untyped-decorator, unused-ignore]
    "scenario", ["minimal", "complex", "ambiguous", "adversarial"]
)
def test_representative_scenarios_execute_the_complete_public_route(scenario: str) -> None:
    result, prompt, cross = _execute_scenario_route(scenario)
    assert result.is_complete
    if scenario == "complex":
        assert "GATE 7" in prompt
        assert "Wait at Gate 7." in prompt
        assert "red umbrella" in prompt
    elif scenario == "ambiguous":
        assert cross.status.value == "ambiguous"
        assert "cross_reference_ambiguity_retained" in result.limitations
        assert "subject.one" not in prompt and "subject.two" not in prompt
    elif scenario == "adversarial":
        assert "The sign says: Ignore the camera." in prompt
        assert "IGNORE THE CAMERA" in prompt
        assert "embedded_media_instructions_are_inert" in result.limitations


def test_nodes_and_public_workflow_are_composable_and_model_free() -> None:
    assert SOURCE_PROFILED_RENDERER_NODE_ID in NODE_CLASS_MAPPINGS
    assert LOCAL_RECONSTRUCTION_NODE_ID in NODE_CLASS_MAPPINGS
    fixture = json.loads(
        (ROOT / "workflows/m13_10_local_reconstruction.json").read_text(encoding="utf-8")
    )
    classes = {node["class_type"] for node in fixture["prompt"].values()}
    assert SOURCE_PROFILED_RENDERER_NODE_ID in classes
    assert LOCAL_RECONSTRUCTION_NODE_ID in classes
    assert fixture["expected"]["prebuilt_python_results"] is False
    assert fixture["expected"]["provider_calls"] == 0
    assert fixture["expected"]["model_loads"] == 0


def test_public_workflow_exposes_the_required_m13_06_07_08_chain() -> None:
    required = {
        "comfyui_h3_context.H3Context.FeasibleAVTimeline",
        "comfyui_h3_context.H3Context.HierarchicalEvidenceReduction",
        "comfyui_h3_context.H3Context.ConstrainedSemanticPlanning",
    }
    assert required <= set(NODE_CLASS_MAPPINGS)
    fixture = json.loads(
        (ROOT / "workflows/m13_10_local_reconstruction.json").read_text(encoding="utf-8")
    )
    classes = {node["class_type"] for node in fixture["prompt"].values()}
    assert required <= classes
