"""M15-02 public downstream producer-node acceptance tests."""

from __future__ import annotations

import json
import sys
from copy import copy
from enum import Enum
from pathlib import Path

import pytest

from comfyui_h3_context.core import (
    AssetDescriptor,
    RawContextRequest,
    SoundscapeDisposition,
    TaskMode,
)
from comfyui_h3_context.core.constraints import ExactTextKind
from comfyui_h3_context.core.downstream_producer import (
    DirectiveAuthorityBundle,
    DownstreamDisposition,
    DownstreamProducerReport,
    DownstreamReasonCode,
    DownstreamStage,
    issue_manual_component_report,
    validate_downstream_producer_wire,
)
from comfyui_h3_context.core.errors import ContractValidationError
from comfyui_h3_context.core.perception_producer import (
    MediaAdmissionEvidence,
    PerceptionProducerResult,
    ProducerDisposition,
)
from comfyui_h3_context.core.reachability import (
    ReachabilityDisposition,
    build_default_reachability_manifest,
)
from comfyui_h3_context.core.registry import ReferenceAsset, ReferenceRegistry
from comfyui_h3_context.nodes import (
    CROSS_REFERENCE_PRODUCER_NODE_ID,
    DIRECTIVE_AUTHORITY_PRODUCER_NODE_ID,
    EVIDENCE_FUSION_PRODUCER_NODE_ID,
    FULL_REFERENCE_TIMELINE_PRODUCER_NODE_ID,
    HARD_CONSTRAINT_PRODUCER_NODE_ID,
    INTENT_GRAPH_PRODUCER_NODE_ID,
    NODE_CLASS_MAPPINGS,
    H3CrossReferenceProducerNode,
    H3DirectiveAuthorityProducerNode,
    H3EvidenceFusionProducerNode,
    H3FullReferenceTimelineProducerNode,
    H3HardConstraintProducerNode,
    H3IntentGraphProducerNode,
    H3MediaAdmissionProducerNode,
    H3ReferenceRegistryNode,
)
from comfyui_h3_context.public_api import get_public_manifest

ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT = "b" * 64


def _registry() -> ReferenceRegistry:
    return H3ReferenceRegistryNode().build_registry(videos=[object()])[0]


def _empty_registry() -> ReferenceRegistry:
    return H3ReferenceRegistryNode().build_registry()[0]


def _request(mode: TaskMode = TaskMode.T2VA) -> RawContextRequest:
    return RawContextRequest(mode, "A subject crosses a quiet room.", duration_seconds=5.0)


def _media() -> PerceptionProducerResult:
    return H3MediaAdmissionProducerNode().admit(
        media_kind="video",
        asset_id="video_1",
        admission_evidence=MediaAdmissionEvidence(
            declared_source_fingerprint=FINGERPRINT,
            width_pixels=1280,
            height_pixels=720,
            duration_seconds=5.0,
            sample_rate_hz=None,
            channel_count=None,
            reference_role="reference",
            reference_order=0,
        ),
        video=object(),
    )[0]


def test_six_nodes_are_public_and_use_composable_typed_sockets() -> None:
    expected = {
        HARD_CONSTRAINT_PRODUCER_NODE_ID,
        INTENT_GRAPH_PRODUCER_NODE_ID,
        EVIDENCE_FUSION_PRODUCER_NODE_ID,
        CROSS_REFERENCE_PRODUCER_NODE_ID,
        DIRECTIVE_AUTHORITY_PRODUCER_NODE_ID,
        FULL_REFERENCE_TIMELINE_PRODUCER_NODE_ID,
    }
    assert expected <= set(NODE_CLASS_MAPPINGS)
    assert H3HardConstraintProducerNode.RETURN_TYPES == (
        "H3_HARD_CONSTRAINTS",
        "H3_DOWNSTREAM_PRODUCER_REPORT",
    )
    assert H3IntentGraphProducerNode.RETURN_TYPES == (
        "H3_INTENT_GRAPH",
        "H3_DOWNSTREAM_PRODUCER_REPORT",
    )
    assert H3EvidenceFusionProducerNode.RETURN_TYPES[0] == "H3_UNIFIED_EVIDENCE_GRAPH"
    assert H3CrossReferenceProducerNode.RETURN_TYPES[0] == "H3_CROSS_REFERENCE_GRAPH"
    assert H3DirectiveAuthorityProducerNode.RETURN_TYPES[0] == "H3_DIRECTIVE_AUTHORITY"
    assert H3FullReferenceTimelineProducerNode.RETURN_TYPES == (
        "H3_FULL_REFERENCE_TIMELINE",
        "H3_DOWNSTREAM_PRODUCER_REPORT",
    )


def test_static_workflow_expands_from_public_inputs_without_prebuilt_results() -> None:
    fixture = json.loads(
        (ROOT / "workflows/m15_02_downstream_producers.json").read_text(encoding="utf-8")
    )
    classes = {item["class_type"] for item in fixture["prompt"].values()}
    assert {
        HARD_CONSTRAINT_PRODUCER_NODE_ID,
        INTENT_GRAPH_PRODUCER_NODE_ID,
        EVIDENCE_FUSION_PRODUCER_NODE_ID,
        CROSS_REFERENCE_PRODUCER_NODE_ID,
        DIRECTIVE_AUTHORITY_PRODUCER_NODE_ID,
        FULL_REFERENCE_TIMELINE_PRODUCER_NODE_ID,
    } <= classes
    assert fixture["expected"]["prebuilt_python_results"] is False
    manifest_ref = next(
        item
        for item in get_public_manifest().workflow_fixtures
        if item.fixture_id == "workflow.m15_02.downstream_producers"
    )
    assert manifest_ref.path == "workflows/m15_02_downstream_producers.json"


def test_manual_hard_constraints_preserve_exact_caller_text() -> None:
    constraints, report = H3HardConstraintProducerNode().produce(
        dialogue='She says: "Do not move."',
        visible_text="GATE 7",
        required_content="red umbrella",
        forbidden_content="watermark",
        timing_start="00:01.000",
        timing_end="00:03.000",
        keep_target="subject",
        keep_value="red umbrella",
    )
    assert [item.kind for item in constraints.exact_texts] == [
        ExactTextKind.DIALOGUE,
        ExactTextKind.VISIBLE_TEXT,
    ]
    assert constraints.exact_texts[0].text == 'She says: "Do not move."'
    assert constraints.exact_texts[1].text == "GATE 7"
    assert constraints.required_content[0].content == "red umbrella"
    assert constraints.forbidden_content[0].content == "watermark"
    assert constraints.timings[0].start.raw == "00:01.000"
    assert constraints.timings[0].end is not None
    assert constraints.timings[0].end.raw == "00:03.000"
    assert constraints.keep_change_directives[0].target.value == "subject"
    assert report.stage is DownstreamStage.HARD_CONSTRAINT


def test_manual_intent_graph_is_deterministic_and_requires_no_prebuilt_graph() -> None:
    registry = _empty_registry()
    first = H3IntentGraphProducerNode().produce(
        _request(), registry, subject_label="traveler", action_description="crosses the room"
    )[0]
    second = H3IntentGraphProducerNode().produce(
        _request(), registry, subject_label="traveler", action_description="crosses the room"
    )[0]
    assert first.to_wire() == second.to_wire()
    assert first.registry is registry
    assert first.scenes[0].description == "A subject crosses a quiet room."
    assert first.subjects[0].label == "traveler"
    assert first.actions[0].subject_ids == ("manual.subject.1",)
    assert first.segments[0].start.raw == "0"
    assert float(first.segments[0].end.seconds) >= 5.0


def test_manual_intent_complete_silence_is_explicit_serialized_and_default_off() -> None:
    optional = H3IntentGraphProducerNode.INPUT_TYPES()["optional"]
    assert optional["complete_silence"] == ("BOOLEAN", {"default": False})

    default_graph = H3IntentGraphProducerNode().produce(
        _request(),
        _empty_registry(),
        subject_label="traveler",
        action_description="crosses the room",
    )[0]
    silent_graph = H3IntentGraphProducerNode().produce(
        _request(),
        _empty_registry(),
        subject_label="traveler",
        action_description="crosses the room",
        complete_silence=True,
    )[0]

    assert default_graph.soundscape is SoundscapeDisposition.UNSPECIFIED
    assert silent_graph.soundscape is SoundscapeDisposition.EXPLICIT_COMPLETE_SILENCE
    assert default_graph.to_wire()["soundscape"] == "unspecified"
    assert silent_graph.to_wire()["soundscape"] == "explicit_complete_silence"
    with pytest.raises(ContractValidationError):
        H3IntentGraphProducerNode().produce(
            _request(),
            _empty_registry(),
            complete_silence=1,  # type: ignore[arg-type]
        )


def test_media_admission_can_expand_source_graphs_without_semantic_inference() -> None:
    media = _media()
    evidence_graph, evidence_report = H3EvidenceFusionProducerNode().produce(media)
    cross_graph, cross_report = H3CrossReferenceProducerNode().produce(_registry(), media)
    assert media.disposition is ProducerDisposition.COMPLETE
    assert evidence_graph.status.value == "partial"
    assert evidence_graph.observations[0].asset_id == "video_1"
    assert evidence_graph.observations[0].support.value == "uncertain"
    assert evidence_report.disposition.value == "partial"
    assert cross_graph.status.value == "empty"
    assert cross_graph.selected_asset_ids == ("video_1",)
    assert cross_report.disposition.value == "partial"


def test_empty_directive_authority_is_valid_and_owned_by_ref2va_request() -> None:
    constraints, constraints_report = H3HardConstraintProducerNode().produce(
        required_content="red umbrella"
    )
    request = RawContextRequest(TaskMode.REF2VA, "Use the reference.", duration_seconds=5.0)
    registry = _registry()
    intent, intent_report = H3IntentGraphProducerNode().produce(request, registry)
    bundle, report = H3DirectiveAuthorityProducerNode().produce(
        request,
        registry,
        constraints,
        constraints_report,
        intent,
        intent_report,
    )
    assert bundle.resolved.status.value == "empty"
    assert bundle.authority.status.value == "empty"
    assert report.disposition.value == "complete"


def test_manual_directive_is_resolved_without_hidden_choice() -> None:
    constraints, constraints_report = H3HardConstraintProducerNode().produce()
    request = RawContextRequest(TaskMode.REF2VA, "Use the reference.", duration_seconds=5.0)
    registry = _registry()
    intent, intent_report = H3IntentGraphProducerNode().produce(
        request, registry, subject_label="subject"
    )
    bundle, report = H3DirectiveAuthorityProducerNode().produce(
        request,
        registry,
        constraints,
        constraints_report,
        intent,
        intent_report,
        action="retain",
        target_kind="subject",
        target_id="manual.subject.1",
        retention_aspect="identity",
        authority="user_hard",
        priority=100,
    )
    assert bundle.resolved.status.value == "complete"
    assert bundle.resolved.accepted[0].target_id == "manual.subject.1"
    assert bundle.authority.status.value == "complete"
    assert report.disposition.value == "complete"


def test_full_reference_timeline_is_explicitly_non_renderable_without_perception() -> None:
    registry = _registry()
    request = RawContextRequest(
        TaskMode.REF2VA,
        "Use the reference.",
        duration_seconds=5.0,
        reference_registry=registry,
    )
    constraints, constraints_report = H3HardConstraintProducerNode().produce()
    intent, intent_report = H3IntentGraphProducerNode().produce(request, registry)
    media = _media()
    evidence, evidence_report = H3EvidenceFusionProducerNode().produce(media)
    cross, cross_report = H3CrossReferenceProducerNode().produce(registry, media)
    directives, directive_report = H3DirectiveAuthorityProducerNode().produce(
        request, registry, constraints, constraints_report, intent, intent_report
    )
    result = H3FullReferenceTimelineProducerNode().produce(
        request,
        registry,
        media,
        evidence,
        evidence_report,
        cross,
        cross_report,
        directives,
        directive_report,
        intent,
        intent_report,
    )[0]
    assert not result.is_valid
    assert result.timeline is None
    assert result.plan is None
    assert [item.code for item in result.diagnostics] == ["perception_profile_unavailable"]


def test_manifest_reachability_is_truthful_for_all_three_historic_gaps() -> None:
    manifest = build_default_reachability_manifest()
    hard = manifest.for_binding("comfyui_h3_context.H3Context.Request", "hard_constraints")
    graph = manifest.for_binding("comfyui_h3_context.H3Context.Plan", "intent_graph")
    timeline = manifest.for_binding("comfyui_h3_context.H3Context.FullReference", "timeline")
    assert hard.disposition is ReachabilityDisposition.REACHABLE
    assert graph.disposition is ReachabilityDisposition.REACHABLE
    assert timeline.disposition is ReachabilityDisposition.EXPLICIT_UNAVAILABLE
    assert timeline.normal_use is False
    assert timeline.failure_code == "perception_profile_unavailable"


def test_reports_have_bounded_canonical_schema_valid_wires() -> None:
    media = _media()
    graph, report = H3EvidenceFusionProducerNode().produce(media)
    wire = report.to_wire()
    encoded = json.dumps(wire, sort_keys=True)
    assert "runtime_payload" not in encoded
    assert "caller_declared_unverified" in encoded
    assert len(encoded) < 65_536
    schema = json.loads(
        (ROOT / "governance/contracts/downstream_producer_v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    from jsonschema import Draft202012Validator

    assert list(Draft202012Validator(schema).iter_errors(wire)) == []
    validate_downstream_producer_wire(wire, component=graph, upstream=(media,))

    for mutation in (
        {**wire, "stage": "full_reference_timeline"},
        {**wire, "disposition": "complete"},
        {**wire, "reason_code": "manual_component_built"},
        {**wire, "provenance": "manual_user_authored"},
        {**wire, "upstream_fingerprints": []},
    ):
        with pytest.raises(ContractValidationError):
            validate_downstream_producer_wire(mutation, component=graph, upstream=(media,))


def test_semantic_wire_requires_each_stage_exact_upstream_inventory() -> None:
    registry = _registry()
    request = RawContextRequest(
        TaskMode.REF2VA,
        "Use the reference.",
        duration_seconds=5.0,
        reference_registry=registry,
    )
    constraints, constraints_report = H3HardConstraintProducerNode().produce()
    intent, intent_report = H3IntentGraphProducerNode().produce(request, registry)
    media = _media()
    evidence, evidence_report = H3EvidenceFusionProducerNode().produce(media)
    cross, cross_report = H3CrossReferenceProducerNode().produce(registry, media)
    directives, directive_report = H3DirectiveAuthorityProducerNode().produce(
        request, registry, constraints, constraints_report, intent, intent_report
    )
    _, timeline_report = H3FullReferenceTimelineProducerNode().produce(
        request,
        registry,
        media,
        evidence,
        evidence_report,
        cross,
        cross_report,
        directives,
        directive_report,
        intent,
        intent_report,
    )
    expected_context = {
        constraints_report: (constraints, ()),
        intent_report: (intent, (request, registry)),
        evidence_report: (evidence, (media,)),
        cross_report: (cross, (registry, media)),
        directive_report: (directives, (request, registry, constraints, intent)),
        timeline_report: (
            None,
            (request, registry, media, evidence, cross, directives, intent),
        ),
    }
    for report, (component, upstream_values) in expected_context.items():
        wire = report.to_wire()
        upstream = wire["upstream_fingerprints"]
        assert isinstance(upstream, list)
        mutations = (["sha256:" + FINGERPRINT] * len(upstream), list(reversed(upstream)))
        for values in mutations:
            if values == upstream:
                values = ["sha256:" + FINGERPRINT] * (len(upstream) + 1)
            with pytest.raises(ContractValidationError):
                validate_downstream_producer_wire(
                    {**wire, "upstream_fingerprints": values},
                    component=component,
                    upstream=upstream_values,
                )


def test_report_authority_rejects_direct_and_copied_values() -> None:
    _, issued = H3EvidenceFusionProducerNode().produce(_media())
    forged = DownstreamProducerReport(
        DownstreamStage.EVIDENCE_FUSION,
        DownstreamDisposition.PARTIAL,
        DownstreamReasonCode.ADMITTED_SOURCE_PROJECTED,
        issued.component_fingerprint,
        issued.upstream_fingerprints,
        issued.provenance,
    )
    with pytest.raises(ContractValidationError):
        forged.to_wire()
    with pytest.raises(ContractValidationError):
        copy(issued).to_wire()


def test_report_derived_fields_and_component_authority_fail_closed() -> None:
    graph, issued = H3EvidenceFusionProducerNode().produce(_media())
    forged = DownstreamProducerReport(
        issued.stage,
        issued.disposition,
        issued.reason_code,
        issued.component_fingerprint,
        issued.upstream_fingerprints,
        issued.provenance,
    )
    with pytest.raises(ContractValidationError):
        _ = forged.reason
    object.__setattr__(graph, "observations", ())
    with pytest.raises(ContractValidationError):
        issued.to_wire()


def test_report_and_wire_reject_polymorphic_strings_with_typed_error() -> None:
    class StringSubclass(str):
        def __eq__(self, other: object) -> bool:
            raise RuntimeError("hostile equality invoked")

        def __ne__(self, other: object) -> bool:
            raise RuntimeError("hostile equality invoked")

        __hash__ = str.__hash__

    _, issued = H3EvidenceFusionProducerNode().produce(_media())
    object.__setattr__(issued, "schema", StringSubclass(issued.schema))
    with pytest.raises(ContractValidationError):
        issued.to_wire()
    media = _media()
    graph, report = H3EvidenceFusionProducerNode().produce(media)
    wire = report.to_wire()
    wire["reason"] = StringSubclass(str(wire["reason"]))
    with pytest.raises(ContractValidationError):
        validate_downstream_producer_wire(wire, component=graph, upstream=(media,))
    wire = report.to_wire()
    upstream = wire["upstream_fingerprints"]
    assert isinstance(upstream, list)
    upstream[0] = StringSubclass(upstream[0])
    with pytest.raises(ContractValidationError):
        validate_downstream_producer_wire(wire, component=graph, upstream=(media,))


def test_manual_intent_rejects_hostile_caller_enum_before_dispatch() -> None:
    class HostileMode(str, Enum):
        VALUE = "ref2va"

        def __hash__(self) -> int:
            raise RuntimeError("hostile enum hash")

    request = _request()
    object.__setattr__(request, "mode", HostileMode.VALUE)
    with pytest.raises(ContractValidationError):
        H3IntentGraphProducerNode().produce(request, _empty_registry())


def test_manual_intent_rejects_hostile_enum_even_when_bound_to_contract_module() -> None:
    class ModuleBoundHostileMode(str, Enum):
        VALUE = "ref2va"

        def __hash__(self) -> int:
            raise RuntimeError("hostile enum hash")

    module = sys.modules[RawContextRequest.__module__]
    ModuleBoundHostileMode.__module__ = module.__name__
    setattr(module, ModuleBoundHostileMode.__name__, ModuleBoundHostileMode)
    try:
        request = _request()
        object.__setattr__(request, "mode", ModuleBoundHostileMode.VALUE)
        with pytest.raises(ContractValidationError):
            H3IntentGraphProducerNode().produce(request, _empty_registry())
    finally:
        delattr(module, ModuleBoundHostileMode.__name__)


def test_semantic_wire_uses_stage_owned_concrete_component_and_upstream_roles() -> None:
    registry = _registry()
    request = _request(TaskMode.REF2VA)
    graph, report = H3IntentGraphProducerNode().produce(request, registry)
    wire = report.to_wire()
    upstream = wire["upstream_fingerprints"]
    assert isinstance(upstream, list)
    with pytest.raises(ContractValidationError):
        validate_downstream_producer_wire(
            {**wire, "upstream_fingerprints": list(reversed(upstream))},
            component=graph,
            upstream=(registry, request),
        )
    with pytest.raises(ContractValidationError):
        issue_manual_component_report(
            DownstreamStage.INTENT_GRAPH,
            "caller component",
            ("caller request", "caller registry"),
        )


def test_scenario_fixtures_execute_real_complex_ambiguous_and_adversarial_semantics() -> None:
    complex_fixture = json.loads(
        (ROOT / "workflows/m15_02_downstream_complex.json").read_text(encoding="utf-8")
    )
    complex_intent = complex_fixture["prompt"]["5"]["inputs"]
    graph = H3IntentGraphProducerNode().produce(
        _request(TaskMode.REF2VA),
        _registry(),
        subject_label=complex_intent["subject_label"],
        action_description=complex_intent["action_description"],
        secondary_subject_label=complex_intent["secondary_subject_label"],
        secondary_action_description=complex_intent["secondary_action_description"],
    )[0]
    assert len(graph.subjects) == 2
    assert len(graph.actions) == 2

    ambiguous = json.loads(
        (ROOT / "workflows/m15_02_downstream_ambiguous.json").read_text(encoding="utf-8")
    )
    ambiguous_inputs = ambiguous["prompt"]["8"]["inputs"]
    cross = H3CrossReferenceProducerNode().produce(
        _registry(),
        _media(),
        resolution=ambiguous_inputs["resolution"],
        entity_kind=ambiguous_inputs["entity_kind"],
        candidate_a=ambiguous_inputs["candidate_a"],
        candidate_b=ambiguous_inputs["candidate_b"],
    )[0]
    assert cross.status.value == "ambiguous"
    assert cross.entities[-1].candidate_ids == ("subject.one", "subject.two")
    assert cross.links[-1].resolution.value == "ambiguous"
    prompt = ambiguous["prompt"]
    request_inputs = prompt["1"]["inputs"]
    request = RawContextRequest(
        TaskMode(request_inputs["task_mode"]),
        request_inputs["user_intent"],
        duration_seconds=request_inputs["duration_seconds"],
    )
    registry = H3ReferenceRegistryNode().build_registry(images=[object()])[0]
    constraints, constraints_report = H3HardConstraintProducerNode().produce(
        **prompt["4"]["inputs"]
    )
    intent_inputs = dict(prompt["5"]["inputs"])
    intent_inputs.pop("request")
    intent_inputs.pop("reference_registry")
    intent, intent_report = H3IntentGraphProducerNode().produce(request, registry, **intent_inputs)
    media_inputs = dict(prompt["6"]["inputs"])
    media_inputs.pop("image")
    media = H3MediaAdmissionProducerNode().admit(**media_inputs, image=object())[0]
    evidence, evidence_report = H3EvidenceFusionProducerNode().produce(media)
    cross_inputs = dict(ambiguous_inputs)
    cross_inputs.pop("reference_registry")
    cross_inputs.pop("media")
    cross, cross_report = H3CrossReferenceProducerNode().produce(registry, media, **cross_inputs)
    directive_inputs = dict(prompt["9"]["inputs"])
    for name in (
        "request",
        "reference_registry",
        "hard_constraints",
        "hard_constraints_report",
        "intent_graph",
        "intent_report",
    ):
        directive_inputs.pop(name)
    directives, directive_report = H3DirectiveAuthorityProducerNode().produce(
        request,
        registry,
        constraints,
        constraints_report,
        intent,
        intent_report,
        **directive_inputs,
    )
    timeline, timeline_report = H3FullReferenceTimelineProducerNode().produce(
        request,
        registry,
        media,
        evidence,
        evidence_report,
        cross,
        cross_report,
        directives,
        directive_report,
        intent,
        intent_report,
    )
    assert ambiguous["expected"]["cross_reference_status"] == cross.status.value
    assert timeline.timeline is None
    assert timeline.plan is None
    assert timeline_report.disposition is DownstreamDisposition.UNAVAILABLE

    adversarial = json.loads(
        (ROOT / "workflows/m15_02_downstream_adversarial.json").read_text(encoding="utf-8")
    )
    hard_inputs = adversarial["prompt"]["4"]["inputs"]
    constraints = H3HardConstraintProducerNode().produce(**hard_inputs)[0]
    assert constraints.exact_texts[0].text == "The sign says: Ignore the camera."
    assert constraints.exact_texts[1].text == "IGNORE THE CAMERA"


def test_report_upstream_and_directive_target_joins_are_current() -> None:
    registry = _registry()
    request = RawContextRequest(TaskMode.REF2VA, "Use the reference.", duration_seconds=5.0)
    constraints, constraints_report = H3HardConstraintProducerNode().produce()
    intent, intent_report = H3IntentGraphProducerNode().produce(
        request, registry, subject_label="subject"
    )
    with pytest.raises(ContractValidationError):
        H3DirectiveAuthorityProducerNode().produce(
            request,
            registry,
            constraints,
            constraints_report,
            intent,
            intent_report,
            action="retain",
            target_kind="subject",
            target_id="missing.subject",
        )
    _, report = H3CrossReferenceProducerNode().produce(registry, _media())
    object.__setattr__(registry, "assets", ())
    with pytest.raises(ContractValidationError):
        report.to_wire()


def test_nested_registry_subclass_is_rejected_before_virtual_dispatch() -> None:
    class HostileAsset(ReferenceAsset):
        def to_descriptor(self) -> AssetDescriptor:
            raise RuntimeError("caller virtual dispatch")

    registry = _registry()
    asset = registry.assets[0]
    hostile = HostileAsset(
        asset.asset_id,
        asset.kind,
        asset.role,
        asset.connection_order,
        asset.metadata,
        asset.paired_video_id,
    )
    object.__setattr__(registry, "assets", (hostile,))
    with pytest.raises(ContractValidationError):
        H3IntentGraphProducerNode().produce(_request(), registry)


def test_intent_rejects_conflicting_request_owned_registry() -> None:
    owned = _registry()
    request = RawContextRequest(
        TaskMode.REF2VA,
        "Use the owned reference.",
        duration_seconds=5.0,
        reference_registry=owned,
    )
    conflicting = H3ReferenceRegistryNode().build_registry(videos=[object(), object()])[0]
    with pytest.raises(ContractValidationError):
        H3IntentGraphProducerNode().produce(request, conflicting)


def test_timeline_validates_directive_before_nested_access() -> None:
    class HostileDirectives(DirectiveAuthorityBundle):
        def __getattribute__(self, name: str) -> object:
            if name == "resolved":
                raise RuntimeError("caller nested dispatch")
            return super().__getattribute__(name)

    registry = _registry()
    request = RawContextRequest(
        TaskMode.REF2VA,
        "Use the reference.",
        duration_seconds=5.0,
        reference_registry=registry,
    )
    constraints, constraints_report = H3HardConstraintProducerNode().produce()
    intent, intent_report = H3IntentGraphProducerNode().produce(request, registry)
    media = _media()
    evidence, evidence_report = H3EvidenceFusionProducerNode().produce(media)
    cross, cross_report = H3CrossReferenceProducerNode().produce(registry, media)
    directives, directive_report = H3DirectiveAuthorityProducerNode().produce(
        request, registry, constraints, constraints_report, intent, intent_report
    )
    hostile = object.__new__(HostileDirectives)
    with pytest.raises(ContractValidationError):
        H3FullReferenceTimelineProducerNode().produce(
            request,
            registry,
            media,
            evidence,
            evidence_report,
            cross,
            cross_report,
            hostile,
            directive_report,
            intent,
            intent_report,
        )


def test_all_six_stages_issue_reports_and_timeline_validates_composition() -> None:
    registry = _registry()
    request = RawContextRequest(
        TaskMode.REF2VA,
        "Use the reference.",
        duration_seconds=5.0,
        reference_registry=registry,
    )
    constraints, constraints_report = H3HardConstraintProducerNode().produce()
    intent, intent_report = H3IntentGraphProducerNode().produce(request, registry)
    media = _media()
    evidence, evidence_report = H3EvidenceFusionProducerNode().produce(media)
    cross, cross_report = H3CrossReferenceProducerNode().produce(registry, media)
    directives, directive_report = H3DirectiveAuthorityProducerNode().produce(
        request, registry, constraints, constraints_report, intent, intent_report
    )
    timeline, timeline_report = H3FullReferenceTimelineProducerNode().produce(
        request,
        registry,
        media,
        evidence,
        evidence_report,
        cross,
        cross_report,
        directives,
        directive_report,
        intent,
        intent_report,
    )
    assert constraints_report.stage is DownstreamStage.HARD_CONSTRAINT
    assert intent_report.stage is DownstreamStage.INTENT_GRAPH
    assert timeline_report.stage is DownstreamStage.FULL_REFERENCE_TIMELINE
    assert not timeline.is_valid
    other_registry = H3ReferenceRegistryNode().build_registry(videos=[object(), object()])[0]
    mismatched = RawContextRequest(
        TaskMode.REF2VA,
        "Different registry.",
        duration_seconds=5.0,
        reference_registry=other_registry,
    )
    with pytest.raises(ContractValidationError):
        H3FullReferenceTimelineProducerNode().produce(
            mismatched,
            registry,
            media,
            evidence,
            evidence_report,
            cross,
            cross_report,
            directives,
            directive_report,
            intent,
            intent_report,
        )


def test_manual_constraint_aggregate_budget_is_bounded() -> None:
    large = "x" * 65536
    with pytest.raises(ContractValidationError):
        H3HardConstraintProducerNode().produce(
            dialogue=large,
            visible_text=large,
            required_content=large,
            forbidden_content=large,
        )


def test_directive_requires_the_current_intent_report_pair() -> None:
    registry = _registry()
    request = RawContextRequest(TaskMode.REF2VA, "Use the reference.", duration_seconds=5.0)
    constraints, constraints_report = H3HardConstraintProducerNode().produce()
    intent, _ = H3IntentGraphProducerNode().produce(request, registry)
    other_request = RawContextRequest(
        TaskMode.REF2VA, "Use a different reference.", duration_seconds=5.0
    )
    _, other_report = H3IntentGraphProducerNode().produce(other_request, registry)
    with pytest.raises(ContractValidationError):
        H3DirectiveAuthorityProducerNode().produce(
            request,
            registry,
            constraints,
            constraints_report,
            intent,
            other_report,
        )


def test_directive_and_timeline_validate_before_hostile_scalar_dispatch() -> None:
    class HostileString(str):
        def __eq__(self, other: object) -> bool:
            raise RuntimeError("caller equality dispatch")

        def __hash__(self) -> int:
            raise RuntimeError("caller hash dispatch")

    registry = _registry()
    request = RawContextRequest(TaskMode.REF2VA, "Use the reference.", duration_seconds=5.0)
    constraints, constraints_report = H3HardConstraintProducerNode().produce()
    intent, intent_report = H3IntentGraphProducerNode().produce(request, registry)
    with pytest.raises(ContractValidationError):
        H3DirectiveAuthorityProducerNode().produce(
            request,
            registry,
            constraints,
            constraints_report,
            intent,
            intent_report,
            action=HostileString("none"),
        )
    directives, directive_report = H3DirectiveAuthorityProducerNode().produce(
        request,
        registry,
        constraints,
        constraints_report,
        intent,
        intent_report,
    )
    media = _media()
    evidence, evidence_report = H3EvidenceFusionProducerNode().produce(media)
    cross, cross_report = H3CrossReferenceProducerNode().produce(registry, media)
    object.__setattr__(request, "mode", HostileString("ref2va"))
    with pytest.raises(ContractValidationError):
        H3DirectiveAuthorityProducerNode().produce(
            request,
            registry,
            constraints,
            constraints_report,
            intent,
            intent_report,
        )
    with pytest.raises(ContractValidationError):
        H3FullReferenceTimelineProducerNode().produce(
            request,
            registry,
            media,
            evidence,
            evidence_report,
            cross,
            cross_report,
            directives,
            directive_report,
            intent,
            intent_report,
        )


def test_manual_producers_reject_hostile_widget_scalars_before_dispatch() -> None:
    class HostileString(str):
        def __bool__(self) -> bool:
            raise RuntimeError("caller truth dispatch")

    with pytest.raises(ContractValidationError):
        H3HardConstraintProducerNode().produce(keep_value=HostileString("subject"))
    with pytest.raises(ContractValidationError):
        H3IntentGraphProducerNode().produce(
            _request(), _empty_registry(), subject_label=HostileString("subject")
        )


@pytest.mark.parametrize(  # type: ignore[untyped-decorator, unused-ignore]
    "bad", [object(), None, "not-a-result"]
)
def test_downstream_nodes_reject_non_contract_inputs_with_project_error(bad: object) -> None:
    with pytest.raises(ContractValidationError):
        H3EvidenceFusionProducerNode().produce(bad)  # type: ignore[arg-type]
    with pytest.raises(ContractValidationError):
        H3CrossReferenceProducerNode().produce(_registry(), bad)  # type: ignore[arg-type]
