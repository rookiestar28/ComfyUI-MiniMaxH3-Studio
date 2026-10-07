"""Compiled Context variants crossing the real planning bridge.

Every source here is produced by the canonical compile pipeline. A hand-built planning context
can describe a source the product never emits, which is how a multi-shot Context was refused at
prepare while the storyboard suites stayed green.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from comfyui_h3_context.adapters.comfyui_production_workspace import (
    PRODUCTION_ACTION_SCHEMA,
    ProductionWorkbenchError,
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry
from comfyui_h3_context.adapters.production_planning_service import (
    PRODUCTION_PLANNING_ACTION_SCHEMA,
    ProductionPlanningService,
)
from comfyui_h3_context.adapters.production_planning_source import (
    build_production_planning_context,
    make_production_planning_source_snapshot,
)
from comfyui_h3_context.core.canonical_context_pipeline import (
    build_canonical_context_plan,
    build_canonical_context_request,
    compile_canonical_context_plan,
    validate_canonical_context_document,
)
from comfyui_h3_context.core.constraints import (
    ContentScope,
    ExactTextConstraint,
    ExactTextKind,
    ForbiddenContent,
    HardConstraintSet,
    TimePoint,
    TimingConstraint,
)
from comfyui_h3_context.core.context_reporting import ContextReport
from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.native_h3 import build_native_h3_wiring
from comfyui_h3_context.core.production_duration import (
    ProductionDurationIntentV1,
    SegmentationPolicyV1,
)
from comfyui_h3_context.core.production_semantics import (
    ProductionSemanticError,
    SemanticSplitPolicyV1,
    derive_production_semantic_authority,
)
from comfyui_h3_context.core.production_storyboard import (
    ProductionPlanningContextV2,
    ProductionStoryboardAdmissionRequestV1,
    ProductionStoryboardAdmissionService,
    ProductionStoryboardAdmissionV2,
    SegmentationProposalV2,
    StoryboardAdmissionRefusalCodeV1,
    StoryboardAdmissionRefusalV1,
    StoryboardShotV1,
    StoryboardSourceKindV1,
    build_segmentation_proposal,
)
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection
from comfyui_h3_context.core.sidebar_workspace import SidebarWorkspaceProjection
from comfyui_h3_context.core.ui_projection import ExecutionCorrelation
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
    H3ReferenceRegistryNode,
)

SINGLE = "A plain sphere crosses a quiet room while the light slowly changes."
MULTI = (
    "A plain sphere enters a quiet room. "
    "[Shot 2] At 00:05.000, the sphere stops beside a table. "
    "[Shot 3] At 00:10.000, the sphere leaves the room."
)
DIALOGUE = HardConstraintSet(
    (ExactTextConstraint("dialogue_1", ExactTextKind.DIALOGUE, "Hold position.", language="en"),)
)


def compiled(
    intent: str,
    seconds: int | None,
    constraints: HardConstraintSet | None = None,
) -> ContextReport:
    request = build_canonical_context_request(TaskMode.T2VA, intent, seconds, constraints)
    plan = build_canonical_context_plan(request)[0]
    _prompt, _report, document = compile_canonical_context_plan(plan)
    validation, report = validate_canonical_context_document(plan, document)
    assert validation.is_valid, [item.code for item in validation.diagnostics]
    return report


def planning_context(
    report: ContextReport,
    target: int,
    policy: SegmentationPolicyV1 = SegmentationPolicyV1.AUTO_STORYBOARD,
) -> ProductionPlanningContextV2:
    snapshot = make_production_planning_source_snapshot(
        workspace_id="ws_" + "a" * 32,
        report=report,
        wiring=build_native_h3_wiring(report),
        expires_at_monotonic=time.monotonic() + 600,
    )
    return build_production_planning_context(
        snapshot,
        planning_context_id="planning_" + "0" * 32,
        revision=1,
        duration_intent=ProductionDurationIntentV1(target_seconds=target, policy=policy),
    )


def admit(
    context: ProductionPlanningContextV2,
    *,
    text: str | None = None,
    rows: tuple[StoryboardShotV1, ...] = (),
) -> ProductionStoryboardAdmissionV2 | StoryboardAdmissionRefusalV1:
    return ProductionStoryboardAdmissionService().admit(
        context,
        ProductionStoryboardAdmissionRequestV1(
            request_id="request_bridge",
            planning_context_id=context.planning_context_id,
            planning_context_revision=context.revision,
            planning_context_fingerprint=context.fingerprint,
            optimized_candidate_id=context.optimized_candidate_id,
            optimized_candidate_text_fingerprint=context.optimized_candidate_text_fingerprint,
            production_duration_intent_fingerprint=context.duration_intent_fingerprint,
            source_kind=StoryboardSourceKindV1.CANONICAL_OPTIMIZED_PROMPT
            if text is not None
            else StoryboardSourceKindV1.USER_REVIEWED_TYPED_ROWS,
            candidate_text=text,
            typed_rows=rows,
            user_reviewed=text is None,
        ),
    )


def rows_at(cuts: tuple[int, ...], target: int) -> tuple[StoryboardShotV1, ...]:
    edges = (0, *cuts, target)
    return tuple(
        StoryboardShotV1(
            f"shot-{index}",
            index,
            edges[index - 1] * 1000,
            edges[index] * 1000,
            f"Reviewed beat {index} of the long story.",
        )
        for index in range(1, len(edges))
    )


def even_rows(count: int, target: int) -> tuple[StoryboardShotV1, ...]:
    """The even whole-second allocation the Sidebar script splitter emits."""

    base, extra = divmod(target, count)
    cuts: list[int] = []
    cursor = 0
    for index in range(count - 1):
        cursor += base + (1 if index < extra else 0)
        cuts.append(cursor)
    return rows_at(tuple(cuts), target)


def durations(proposal: object) -> tuple[int, ...]:
    assert isinstance(proposal, SegmentationProposalV2)
    return tuple(segment.duration.requested_seconds for segment in proposal.segments)


def test_multi_shot_context_prepares_and_plain_prose_is_not_timed_authority() -> None:
    report = compiled(MULTI, 15)
    assert len(report.plan.intent_graph.segments) == 1
    context = planning_context(report, 30)
    assert context.source_context_duration_seconds == 15
    assert context.semantic_authority.timed_spans == ()


def test_constrained_multi_shot_context_lifts_each_rendered_shot_on_the_source_clock() -> None:
    report = compiled(MULTI, 15, DIALOGUE)
    spans = planning_context(report, 15).semantic_authority.timed_spans
    assert tuple((span.start_milliseconds, span.end_milliseconds) for span in spans) == (
        (0, 5_000),
        (5_000, 10_000),
        (10_000, 15_000),
    )
    assert "enters a quiet room" in spans[0].text and "[Shot" not in spans[0].text
    assert "stops beside a table" in spans[1].text
    owners = [span for span in spans if "Hold position." in span.text]
    assert len(owners) == 1
    assert owners[0].split_policy is SemanticSplitPolicyV1.IMMUTABLE


def _description(report: ContextReport) -> str:
    return next(
        item.body
        for item in report.prompt_document.sections
        if item.heading == "integrated_multimodal_description"
    )


@pytest.mark.parametrize("constraints", [None, DIALOGUE])
def test_an_authored_first_shot_marker_is_not_rendered_twice(
    constraints: HardConstraintSet | None,
) -> None:
    report = compiled("[Shot 1] " + MULTI, 15, constraints)
    body = _description(report)
    assert body.startswith("[Shot 1] A plain sphere enters a quiet room.")
    assert body.count("[Shot 1]") == 1
    assert body == _description(compiled(MULTI, 15, constraints))
    spans = planning_context(report, 15).semantic_authority.timed_spans
    assert len(spans) == (0 if constraints is None else 3)


@pytest.mark.parametrize(
    "intent",
    [
        "A plain sphere enters a quiet room. [Shot 2] the sphere stops beside a table.",
        "A plain sphere enters a quiet room. [Shot 3] At 00:05.000, the sphere stops.",
    ],
)
def test_a_plain_source_is_planned_whatever_shape_its_shot_markers_have(intent: str) -> None:
    report = compiled(intent, 15)
    context = planning_context(report, 30)
    assert context.semantic_authority.timed_spans == ()
    refused = admit(context, text=report.prompt_document.text)
    assert isinstance(refused, StoryboardAdmissionRefusalV1)
    assert refused.code is StoryboardAdmissionRefusalCodeV1.STORYBOARD_UNAVAILABLE
    admission = admit(context, rows=rows_at((8, 20), 30))
    assert isinstance(admission, ProductionStoryboardAdmissionV2)
    assert durations(build_segmentation_proposal(context, admission)) == (8, 12, 10)


def test_forbidden_only_source_keeps_no_timed_description() -> None:
    constraints = HardConstraintSet(
        (ForbiddenContent("forbidden_1", ContentScope.VISUAL, "private emblem"),)
    )
    report = compiled(SINGLE, 15, constraints)
    assert planning_context(report, 60).semantic_authority.timed_spans == ()


@pytest.mark.parametrize(
    ("description", "reason"),
    [
        ("[Shot 1] Opening. [Shot 2] the marker lost its cut time.", "untimed later shot"),
        ("[Shot 1] Opening. [Shot 3] At 00:05.000, skipped ordinal.", "ordinal gap"),
        (
            "[Shot 1] Opening. [Shot 2] At 00:08.000, late. [Shot 3] At 00:04.000, early.",
            "not increasing",
        ),
        ("[Shot 1] Opening. [Shot 2] At 00:20.000, beyond the clip.", "outside the clip"),
    ],
)
def test_constrained_source_with_a_broken_storyboard_shape_is_refused(
    description: str, reason: str
) -> None:
    report = compiled(SINGLE, 15, DIALOGUE)
    document = report.prompt_document
    section = next(
        item for item in document.sections if item.heading == "integrated_multimodal_description"
    )
    from dataclasses import replace

    broken = replace(
        document,
        text=document.text.replace(section.body, description),
        sections=tuple(
            replace(item, body=description) if item is section else item
            for item in document.sections
        ),
    )
    with pytest.raises(ProductionSemanticError, match="semantic_source_storyboard_shape"):
        derive_production_semantic_authority(report.plan, broken)
    assert reason


@pytest.mark.parametrize(
    ("intent", "source", "target", "policy", "expected"),
    [
        (SINGLE, 10, 20, SegmentationPolicyV1.FIXED_10, ((0, 20_000),)),
        (SINGLE, 15, 15, SegmentationPolicyV1.AUTO_STORYBOARD, ((0, 15_000),)),
        (SINGLE, 15, 60, SegmentationPolicyV1.AUTO_STORYBOARD, None),
        (
            MULTI,
            15,
            15,
            SegmentationPolicyV1.AUTO_STORYBOARD,
            ((0, 5_000), (5_000, 10_000), (10_000, 15_000)),
        ),
        (MULTI, 15, 30, SegmentationPolicyV1.AUTO_STORYBOARD, None),
        (MULTI, 15, 30, SegmentationPolicyV1.FIXED_15, None),
        (
            MULTI,
            15,
            15,
            SegmentationPolicyV1.FIXED_5,
            ((0, 5_000), (5_000, 10_000), (10_000, 15_000)),
        ),
    ],
)
def test_canonical_storyboard_is_admitted_only_with_honest_cut_evidence(
    intent: str,
    source: int,
    target: int,
    policy: SegmentationPolicyV1,
    expected: tuple[tuple[int, int], ...] | None,
) -> None:
    report = compiled(intent, source)
    context = planning_context(report, target, policy)
    result = admit(context, text=report.prompt_document.text)
    if expected is None:
        assert isinstance(result, StoryboardAdmissionRefusalV1)
        assert result.code is StoryboardAdmissionRefusalCodeV1.STORYBOARD_UNAVAILABLE
        return
    assert isinstance(result, ProductionStoryboardAdmissionV2)
    assert (
        tuple((shot.start_milliseconds, shot.end_milliseconds) for shot in result.shots) == expected
    )
    proposal = build_segmentation_proposal(context, result)
    assert isinstance(proposal, SegmentationProposalV2)
    if policy is SegmentationPolicyV1.AUTO_STORYBOARD:
        assert durations(proposal) == (target,)


@pytest.mark.parametrize("intent", [SINGLE, MULTI])
def test_reviewed_rows_own_every_segment_prompt_of_a_plain_source(intent: str) -> None:
    report = compiled(intent, 15)
    context = planning_context(report, 60)
    admission = admit(context, rows=rows_at((8, 20, 33, 47), 60))
    assert isinstance(admission, ProductionStoryboardAdmissionV2)
    proposal = build_segmentation_proposal(context, admission)
    assert durations(proposal) == (8, 12, 13, 14, 13)
    assert isinstance(proposal, SegmentationProposalV2)
    assert proposal.blocker_codes == ()
    for ordinal, segment in enumerate(proposal.segments, start=1):
        assert f"Reviewed beat {ordinal} of the long story." in segment.local_prompt
        assert "From 00:" not in segment.local_prompt
        assert "quiet room" not in segment.local_prompt


def test_fixed_policy_repeats_a_single_shot_template_without_duplicating_it() -> None:
    report = compiled(SINGLE, 10)
    context = planning_context(report, 20, SegmentationPolicyV1.FIXED_10)
    admission = admit(context, text=report.prompt_document.text)
    assert isinstance(admission, ProductionStoryboardAdmissionV2)
    proposal = build_segmentation_proposal(context, admission)
    assert durations(proposal) == (10, 10)
    assert isinstance(proposal, SegmentationProposalV2)
    for segment in proposal.segments:
        assert segment.local_prompt.count(SINGLE) == 1
        assert "From 00:" not in segment.local_prompt


def test_constrained_source_ends_on_the_requested_second_not_the_frame_lattice() -> None:
    report = compiled(SINGLE, 10, DIALOGUE)
    context = planning_context(report, 20, SegmentationPolicyV1.FIXED_10)
    spans = context.semantic_authority.timed_spans
    assert tuple((span.start_milliseconds, span.end_milliseconds) for span in spans) == (
        (0, 10_000),
    )
    assert spans[0].split_policy is SemanticSplitPolicyV1.IMMUTABLE
    admission = admit(context, rows=rows_at((10,), 20))
    assert isinstance(admission, ProductionStoryboardAdmissionV2)
    proposal = build_segmentation_proposal(context, admission)
    assert durations(proposal) == (10, 10)
    assert isinstance(proposal, SegmentationProposalV2)
    assert "Hold position." in proposal.segments[0].local_prompt
    assert "From 00:" not in proposal.segments[1].local_prompt


@pytest.mark.parametrize(
    ("count", "expected", "shots_per_segment"),
    [(6, (10,) * 6, 1), (20, (15,) * 4, 5)],
)
def test_evenly_spaced_long_content_becomes_storyboard_aligned_segments(
    count: int, expected: tuple[int, ...], shots_per_segment: int
) -> None:
    report = compiled(SINGLE, 15)
    context = planning_context(report, 60)
    admission = admit(context, rows=even_rows(count, 60))
    assert isinstance(admission, ProductionStoryboardAdmissionV2)
    proposal = build_segmentation_proposal(context, admission)
    assert durations(proposal) == expected
    assert isinstance(proposal, SegmentationProposalV2)
    assert all(len(row.assigned_shot_ids) == shots_per_segment for row in proposal.segments)


def _service(
    intent: str,
    seconds: int | None,
    constraints: HardConstraintSet | None = None,
) -> tuple[ProductionPlanningService, dict[str, Any]]:
    report = compiled(intent, seconds, constraints)
    sidebar = SidebarWorkspaceRegistry(clock=lambda: 10.0, ttl_seconds=60)
    source = sidebar.publish(
        report, build_native_h3_wiring(report), ExecutionCorrelation("owned", "node")
    )
    production = ProductionWorkspaceRegistry(
        seed_claim=sidebar.claim_production_seed, clock=lambda: 10.0
    )
    created = production.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "create",
            "action": "create_workspace_from_context",
            "payload": {"context_workspace_handle": source.workspace_id},
        }
    ).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    service = ProductionPlanningService(
        sidebar_registry=sidebar, production_registry=production, clock=lambda: 10.0
    )
    prepare = {
        "workspace_handle": created.workspace_handle,
        "expected_workspace_revision": created.workspace_revision,
        "expected_workspace_fingerprint": created.workspace_fingerprint,
        "context_workspace_handle": source.workspace_id,
        "expected_report_revision": source.report_revision,
        "expected_report_fingerprint": source.report_fingerprint,
        "expected_planning_revision": 0,
        "target_seconds": 30,
        "policy": "auto_storyboard",
    }
    return service, prepare


def _action(kind: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {
        "schema": PRODUCTION_PLANNING_ACTION_SCHEMA,
        "request_id": request_id,
        "action": kind,
        "payload": payload,
    }


def _selectors(projection: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspace_handle": projection["workspace_handle"],
        "expected_workspace_revision": projection["workspace_revision"],
        "expected_workspace_fingerprint": projection["workspace_fingerprint"],
        "planning_context_id": projection["planning_context_id"],
        "expected_planning_revision": projection["planning_revision"],
    }


def test_service_prepares_a_multi_shot_context_and_refuses_its_stretched_storyboard() -> None:
    service, prepare = _service(MULTI, 15)
    result = service.dispatch(_action("prepare_context", prepare, "prepare"))
    assert result.status == 200
    prepared: dict[str, Any] = result.to_wire()
    assert prepared["source_duration_seconds"] == 15 and prepared["target_seconds"] == 30
    with pytest.raises(ProductionWorkbenchError, match="storyboard_review_required") as refused:
        service.dispatch(
            _action(
                "admit_storyboard",
                {
                    **_selectors(prepared),
                    "source_kind": "canonical_optimized_prompt",
                    "typed_rows": [],
                    "user_reviewed": False,
                },
                "admit.canonical",
            )
        )
    assert refused.value.status == 422
    admitted: dict[str, Any] = service.dispatch(
        _action(
            "admit_storyboard",
            {
                **_selectors(prepared),
                "source_kind": "user_reviewed_typed_rows",
                "typed_rows": [row.to_wire() for row in rows_at((8, 20), 30)],
                "user_reviewed": True,
            },
            "admit.rows",
        )
    ).to_wire()
    proposed: dict[str, Any] = service.dispatch(
        _action(
            "propose",
            {**_selectors(admitted), "admission_id": admitted["admission_id"]},
            "propose",
        )
    ).to_wire()
    assert [row["duration_seconds"] for row in proposed["proposal"]["segments"]] == [8, 12, 10]
    imported: dict[str, Any] = service.dispatch(
        _action(
            "import_plan",
            {**_selectors(proposed), "proposal_id": proposed["proposal"]["proposal_id"]},
            "import",
        )
    ).to_wire()
    assert len(imported["materialization_receipt_fingerprints"]) == 3


def test_service_names_an_unsupported_source_instead_of_a_malformed_request() -> None:
    point = HardConstraintSet((TimingConstraint("timing_1", TimePoint.from_text("2")),))
    service, prepare = _service(SINGLE, 15, point)
    with pytest.raises(ProductionWorkbenchError, match="planning_source_unsupported") as refused:
        service.dispatch(_action("prepare_context", prepare, "prepare"))
    assert refused.value.status == 422
    assert not service._entries and not service._requests


def test_service_names_a_context_without_a_whole_second_clip_length() -> None:
    # A Context left at its default duration is 125 frames, not a whole number of seconds.
    service, prepare = _service(SINGLE, None)
    with pytest.raises(ProductionWorkbenchError, match="planning_source_unsupported") as refused:
        service.dispatch(_action("prepare_context", prepare, "prepare"))
    assert refused.value.status == 422


# Sweep: the same bridge, reached by the other ways the product produces a Context.


def _edited_context(
    constraints: HardConstraintSet | None, target: int
) -> ProductionPlanningContextV2:
    """A single-shot Context whose prompt was rewritten as shots and validated again.

    This is the path of a manual prompt edit and of an accepted assisted candidate: the typed
    plan keeps its one segment while the prompt text gains shot markers.
    """

    report = compiled(SINGLE, 15, constraints)
    sidebar = SidebarWorkspaceRegistry(
        max_entries=4, ttl_seconds=60, clock=lambda: 1.0, source_binding_claim=lambda _value: None
    )
    published = sidebar.publish(
        report, build_native_h3_wiring(report), ExecutionCorrelation("owned", "node")
    )
    assert SINGLE in published.prompt_text
    identity = {
        "schema": "h3.context.sidebar.action.v2",
        "workspace_id": published.workspace_id,
    }
    staged = sidebar.dispatch(
        {
            **identity,
            "expected_revision": published.report_revision,
            "expected_report_fingerprint": published.report_fingerprint,
            "action": "stage_prompt",
            "payload": {
                "reason": "Write the shots",
                "prompt_text": published.prompt_text.replace(SINGLE, MULTI),
            },
        }
    )
    assert isinstance(staged, SidebarWorkspaceProjection)
    validated = sidebar.dispatch(
        {
            **identity,
            "expected_revision": staged.report_revision,
            "expected_report_fingerprint": staged.report_fingerprint,
            "action": "validate",
            "payload": {},
        }
    )
    assert isinstance(validated, SidebarWorkspaceProjection)
    assert validated.validation_status == "passed"
    snapshot = sidebar.snapshot_production_planning_source(
        validated.workspace_id,
        expected_report_revision=validated.report_revision,
        expected_report_fingerprint=validated.report_fingerprint,
    )
    assert len(snapshot.report.plan.intent_graph.segments) == 1
    return build_production_planning_context(
        snapshot,
        planning_context_id="planning_" + "0" * 32,
        revision=1,
        duration_intent=ProductionDurationIntentV1(target_seconds=target),
    )


@pytest.mark.parametrize(
    ("constraints", "spans"),
    [(None, ()), (DIALOGUE, ((0, 5_000), (5_000, 10_000), (10_000, 15_000)))],
)
def test_a_prompt_rewritten_as_shots_is_planned(
    constraints: HardConstraintSet | None, spans: tuple[tuple[int, int], ...]
) -> None:
    context = _edited_context(constraints, 30)
    assert (
        tuple(
            (span.start_milliseconds, span.end_milliseconds)
            for span in context.semantic_authority.timed_spans
        )
        == spans
    )
    admission = admit(context, rows=rows_at((8, 20), 30))
    assert isinstance(admission, ProductionStoryboardAdmissionV2)
    assert durations(build_segmentation_proposal(context, admission)) == (8, 12, 10)


def _reference_report(intent: str) -> ContextReport:
    request = H3ContextRequestNode().build_request(TaskMode.REF2VA, intent, duration_seconds=15.0)[
        0
    ]
    registry = H3ReferenceRegistryNode().build_registry(images=[object(), object()])[0]
    plan = H3ContextPlanNode().build_plan(request, registry)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    report: ContextReport = H3ContextValidatorNode().validate(plan, document)[1]
    assert report.validation.is_valid
    return report


@pytest.mark.parametrize("intent", [SINGLE, "[Shot 1] " + SINGLE])
def test_a_reference_context_is_planned_as_a_clip_and_as_a_longer_video(intent: str) -> None:
    report = _reference_report(intent)
    body = next(
        item.body
        for item in report.prompt_document.sections
        if item.heading == "detailed_description"
    )
    assert body == "[Shot 1] " + SINGLE
    # The clip's own length is a valid target: the frame-lattice surplus must not exceed it.
    clip = planning_context(report, 15)
    generated = admit(clip, text=report.prompt_document.text)
    assert isinstance(generated, ProductionStoryboardAdmissionV2)
    # Admission is not the outcome the user sees: the proposal is. Asserting only the admission
    # left a refused proposal for this exact path unnoticed.
    assert durations(build_segmentation_proposal(clip, generated)) == (15,)
    longer = planning_context(report, 30)
    admission = admit(longer, rows=rows_at((8, 20), 30))
    assert isinstance(admission, ProductionStoryboardAdmissionV2)
    proposal = build_segmentation_proposal(longer, admission)
    assert durations(proposal) == (8, 12, 10)
    assert isinstance(proposal, SegmentationProposalV2)
    assert {segment.task_mode for segment in proposal.segments} == {TaskMode.REF2VA}
