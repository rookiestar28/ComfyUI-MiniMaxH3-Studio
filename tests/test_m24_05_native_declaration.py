"""M24-05 native keyframe/development declarations stay explicit and fail closed."""

from __future__ import annotations

from typing import cast

import pytest

from comfyui_h3_context.context_pipeline_nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextValidatorNode,
)
from comfyui_h3_context.context_request_nodes import H3ContextRequestNode, H3ReferenceRegistryNode
from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.context_reporting import PromptDocument
from comfyui_h3_context.core.contracts import PromptProfile, TaskMode
from comfyui_h3_context.core.downstream_producer import ManualKeyframeBinding
from comfyui_h3_context.core.errors import ContractValidationError
from comfyui_h3_context.core.guide_conformance import GuideReadiness
from comfyui_h3_context.core.intent_graph import IntentGraph, SegmentDevelopment
from comfyui_h3_context.core.native_h3 import build_native_h3_wiring
from comfyui_h3_context.core.normalization import RawContextRequest
from comfyui_h3_context.core.prompt_fidelity import PromptFidelityDiagnosticId
from comfyui_h3_context.core.registry import ReferenceRegistry
from comfyui_h3_context.core.sidebar_workspace import (
    SidebarWorkspaceProjection,
    build_sidebar_workspace_projection,
)
from comfyui_h3_context.core.source_profiled_prompt import (
    OFFICIAL_H3_BASE_GUIDE_DIGEST,
    OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST,
    OFFICIAL_H3_GUIDE_REVISION,
    SourceProfiledPromptResult,
    build_official_source_profile_binding,
    validate_profiled_prompt,
)
from comfyui_h3_context.core.ui_projection import ExecutionCorrelation
from comfyui_h3_context.evidence_producer_nodes import H3IntentGraphProducerNode


def _registry(*, first: bool = False, last: bool = False) -> ReferenceRegistry:
    return H3ReferenceRegistryNode().build_registry(
        first_frame=object() if first else None,
        last_frame=object() if last else None,
    )[0]


def _request(mode: TaskMode) -> RawContextRequest:
    return RawContextRequest(mode, "A declared subject.", duration_seconds=4.0)


def _mode_registry(mode: TaskMode) -> ReferenceRegistry:
    if mode is TaskMode.I2VA:
        return _registry(first=True)
    if mode is TaskMode.FL2VA:
        return _registry(first=True, last=True)
    if mode is TaskMode.L2VA:
        return _registry(last=True)
    if mode is TaskMode.REF2VA:
        # A declared first-frame role is self-evident in Full-Reference mode. A generic reference
        # must remain unused until the caller supplies a real subject/asset ownership seam.
        return _registry(first=True)
    return _registry()


def _native_readiness_chain(
    mode: TaskMode,
    *,
    subject_label: str = "",
    action_description: str = "",
    complete_silence: bool = False,
    keyframe_binding: str = ManualKeyframeBinding.UNBOUND.value,
    segment_development: str = SegmentDevelopment.UNSPECIFIED.value,
) -> tuple[IntentGraph, PromptDocument, SourceProfiledPromptResult, SidebarWorkspaceProjection]:
    registry = _mode_registry(mode)
    request = H3ContextRequestNode().build_request(
        mode,
        "A declared subject acts in a declared scene.",
        duration_seconds=4.0,
    )[0]
    graph = H3IntentGraphProducerNode().produce(
        request,
        registry,
        subject_label=subject_label,
        action_description=action_description,
        complete_silence=complete_silence,
        keyframe_binding=keyframe_binding,
        segment_development=segment_development,
    )[0]
    plan = H3ContextPlanNode().build_plan(request, registry, graph)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    digest = (
        OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST
        if plan.request.profile.name is PromptProfile.FULL_REFERENCE
        else OFFICIAL_H3_BASE_GUIDE_DIGEST
    )
    binding = build_official_source_profile_binding(
        plan.request.profile,
        observed_revision=OFFICIAL_H3_GUIDE_REVISION,
        observed_digest=digest,
    )
    profiled = validate_profiled_prompt(plan, document, binding)
    _, report = H3ContextValidatorNode().validate(plan, document)
    wiring = build_native_h3_wiring(report)
    sidebar = build_sidebar_workspace_projection(
        report,
        wiring,
        ExecutionCorrelation(f"native-{mode.value}", "1"),
        workspace_id="ws_0123456789abcdefghijklmnopqrstuv",
        base_prompt_fingerprint=canonical_fingerprint(document.text),
    )
    return graph, document, profiled, sidebar


def test_native_widgets_default_to_no_new_authority() -> None:
    optional = H3IntentGraphProducerNode.INPUT_TYPES()["optional"]
    assert optional["keyframe_binding"] == (
        "COMBO",
        {
            "default": ManualKeyframeBinding.UNBOUND.value,
            "options": [item.value for item in ManualKeyframeBinding],
        },
    )
    assert optional["segment_development"] == (
        "COMBO",
        {
            "default": SegmentDevelopment.UNSPECIFIED.value,
            "options": [item.value for item in SegmentDevelopment],
        },
    )
    graph = H3IntentGraphProducerNode().produce(
        _request(TaskMode.I2VA), _registry(first=True), subject_label="subject"
    )[0]
    assert graph.subjects[0].source_asset_ids == ()
    assert graph.segments[0].development is SegmentDevelopment.UNSPECIFIED


@pytest.mark.parametrize(
    ("mode", "expected_sections"),
    (
        (TaskMode.T2VA, 3),
        (TaskMode.I2VA, 3),
        (TaskMode.FL2VA, 3),
        (TaskMode.L2VA, 3),
        (TaskMode.REF2VA, 6),
    ),
)
def test_native_default_chain_keeps_profile_shape_and_truthful_incomplete_readiness(
    mode: TaskMode, expected_sections: int
) -> None:
    graph, document, profiled, sidebar = _native_readiness_chain(mode)

    assert graph.subjects == ()
    assert graph.actions == ()
    assert len(document.sections) == expected_sections
    assert profiled.is_valid
    assert profiled.conformance is not None
    assert profiled.conformance.readiness is GuideReadiness.INCOMPLETE
    assert not profiled.official_claim_admitted
    assert sidebar.guide_conformance == profiled.conformance
    assert sidebar.guide_conformance.readiness is GuideReadiness.INCOMPLETE
    reason_ids = set(profiled.conformance.reasons)
    assert PromptFidelityDiagnosticId.SOUNDSCAPE_UNSPECIFIED in reason_ids
    if mode in (TaskMode.I2VA, TaskMode.FL2VA, TaskMode.L2VA):
        assert PromptFidelityDiagnosticId.KEYFRAME_ANCHOR_MISSING in reason_ids


@pytest.mark.parametrize("mode", (TaskMode.T2VA, TaskMode.REF2VA))
def test_native_non_keyframe_request_rejects_missing_content_before_graph(mode: TaskMode) -> None:
    result = H3ContextRequestNode.VALIDATE_INPUTS(
        task_mode=mode,
        user_intent="   ",
        duration_seconds=4.0,
    )

    assert isinstance(result, str)
    assert "missing_user_intent" in result


@pytest.mark.parametrize(
    ("mode", "binding", "development", "expected_sections"),
    (
        (TaskMode.T2VA, "unbound", "unspecified", 3),
        (TaskMode.I2VA, "first_frame", "anchor_development", 3),
        (TaskMode.FL2VA, "both", "anchor_development", 3),
        (TaskMode.L2VA, "last_frame", "anchor_development", 3),
        (TaskMode.REF2VA, "unbound", "unspecified", 6),
    ),
)
def test_native_explicit_semantics_reach_the_same_ready_result_in_every_consumer(
    mode: TaskMode,
    binding: str,
    development: str,
    expected_sections: int,
) -> None:
    graph, document, profiled, sidebar = _native_readiness_chain(
        mode,
        subject_label="declared subject",
        action_description="the declared subject crosses the scene",
        complete_silence=True,
        keyframe_binding=binding,
        segment_development=development,
    )

    assert graph.subjects
    assert graph.actions
    assert len(document.sections) == expected_sections
    assert profiled.is_valid
    assert profiled.conformance is not None
    assert profiled.conformance.readiness is GuideReadiness.READY
    assert profiled.conformance.reasons == ()
    assert profiled.official_claim_admitted
    assert sidebar.guide_conformance == profiled.conformance
    assert sidebar.guide_conformance.readiness is GuideReadiness.READY
    assert sidebar.lifecycle == "ready"


@pytest.mark.parametrize("mode", (TaskMode.I2VA, TaskMode.FL2VA, TaskMode.L2VA))
def test_native_keyframe_chain_with_development_but_no_binding_stays_incomplete(
    mode: TaskMode,
) -> None:
    graph, _, profiled, sidebar = _native_readiness_chain(
        mode,
        subject_label="declared subject",
        action_description="the declared subject crosses the scene",
        complete_silence=True,
        segment_development="anchor_development",
    )

    assert graph.subjects[0].source_asset_ids == ()
    assert profiled.conformance is not None
    assert profiled.conformance.readiness is GuideReadiness.INCOMPLETE
    assert PromptFidelityDiagnosticId.KEYFRAME_ANCHOR_MISSING in profiled.conformance.reasons
    assert sidebar.guide_conformance == profiled.conformance
    assert sidebar.guide_conformance.readiness is GuideReadiness.INCOMPLETE


@pytest.mark.parametrize(
    ("mode", "binding", "first", "last", "expected"),
    (
        (TaskMode.I2VA, "first_frame", True, False, ("first_frame_1",)),
        (TaskMode.L2VA, "last_frame", False, True, ("last_frame_1",)),
        (TaskMode.FL2VA, "first_frame", True, True, ("first_frame_1",)),
        (TaskMode.FL2VA, "last_frame", True, True, ("last_frame_1",)),
        (
            TaskMode.FL2VA,
            "both",
            True,
            True,
            ("first_frame_1", "last_frame_1"),
        ),
    ),
)
def test_native_binding_selects_only_declared_roles_in_registry_order(
    mode: TaskMode,
    binding: str,
    first: bool,
    last: bool,
    expected: tuple[str, ...],
) -> None:
    graph, report = H3IntentGraphProducerNode().produce(
        _request(mode),
        _registry(first=first, last=last),
        subject_label="subject",
        action_description="subject changes",
        keyframe_binding=binding,
        segment_development="anchor_development",
    )
    assert graph.subjects[0].source_asset_ids == expected
    assert graph.segments[0].development is SegmentDevelopment.ANCHOR_DEVELOPMENT
    assert report.to_wire()["component_fingerprint"] is not None


@pytest.mark.parametrize(
    ("mode", "binding", "first", "last"),
    (
        (TaskMode.T2VA, "first_frame", False, False),
        (TaskMode.REF2VA, "last_frame", False, False),
        (TaskMode.I2VA, "last_frame", True, False),
        (TaskMode.L2VA, "first_frame", False, True),
        (TaskMode.I2VA, "first_frame", False, False),
        (TaskMode.FL2VA, "both", True, False),
    ),
)
def test_native_binding_rejects_incompatible_or_absent_roles(
    mode: TaskMode, binding: str, first: bool, last: bool
) -> None:
    with pytest.raises(ContractValidationError):
        H3IntentGraphProducerNode().produce(
            _request(mode),
            _registry(first=first, last=last),
            subject_label="subject",
            keyframe_binding=binding,
        )


def test_native_binding_requires_the_primary_subject() -> None:
    with pytest.raises(ContractValidationError):
        H3IntentGraphProducerNode().produce(
            _request(TaskMode.I2VA),
            _registry(first=True),
            keyframe_binding="first_frame",
        )


def test_development_without_binding_stays_explicit_but_unowned() -> None:
    graph = H3IntentGraphProducerNode().produce(
        _request(TaskMode.I2VA),
        _registry(first=True),
        subject_label="subject",
        action_description="subject changes",
        segment_development="anchor_development",
    )[0]
    assert graph.subjects[0].source_asset_ids == ()
    assert graph.segments[0].development is SegmentDevelopment.ANCHOR_DEVELOPMENT


@pytest.mark.parametrize("mode", (TaskMode.T2VA, TaskMode.REF2VA))
def test_non_keyframe_modes_reject_anchor_development(mode: TaskMode) -> None:
    with pytest.raises(ContractValidationError):
        H3IntentGraphProducerNode().produce(
            _request(mode),
            _registry(),
            subject_label="subject",
            segment_development="anchor_static_hold",
        )


def test_native_widget_values_are_closed_exact_strings() -> None:
    with pytest.raises(ContractValidationError):
        H3IntentGraphProducerNode().produce(
            _request(TaskMode.I2VA),
            _registry(first=True),
            subject_label="subject",
            keyframe_binding="all_assets",
        )
    with pytest.raises(ContractValidationError):
        H3IntentGraphProducerNode().produce(
            _request(TaskMode.I2VA),
            _registry(first=True),
            subject_label="subject",
            segment_development=cast(str, SegmentDevelopment.ANCHOR_DEVELOPMENT),
        )
