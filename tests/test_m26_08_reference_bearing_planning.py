"""Reference-bearing Contexts planned as a whole video, judged by the real validator.

Every source is built through the Request, Registry, Plan, Compiler and Validator nodes and
published to a Sidebar registry, and every planned segment is materialized into its own Context.
A hand-built planning context can omit the sections the compile path derives per clip (the
keyframe alignment sentence, the subject definitions), which is how frame-anchored Contexts were
refused by planning while the storyboard suites stayed green.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pytest

from comfyui_h3_context.adapters.comfyui_production_workspace import (
    PRODUCTION_ACTION_SCHEMA,
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry
from comfyui_h3_context.adapters.production_context_materializer import (
    CanonicalProductionMaterializer,
)
from comfyui_h3_context.adapters.production_planning_service import (
    PRODUCTION_PLANNING_ACTION_SCHEMA,
    ProductionPlanningService,
)
from comfyui_h3_context.adapters.production_planning_source import (
    ProductionPlanningSourceSnapshot,
    build_production_planning_context,
)
from comfyui_h3_context.core.context_reporting import ContextReport
from comfyui_h3_context.core.contracts import AssetRole, TaskMode
from comfyui_h3_context.core.native_h3 import build_native_h3_wiring
from comfyui_h3_context.core.production_duration import (
    ProductionDurationIntentV1,
    SegmentationPolicyV1,
)
from comfyui_h3_context.core.production_storyboard import (
    ProductionPlanningContextV2,
    ProductionStoryboardAdmissionRequestV1,
    ProductionStoryboardAdmissionService,
    ProductionStoryboardAdmissionV2,
    SegmentationProposalV2,
    StoryboardShotV1,
    StoryboardSourceKindV1,
    build_segmentation_proposal,
)
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection
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
    "[Shot 1] A plain sphere enters a quiet room. "
    "[Shot 2] At 00:05.000, the sphere stops beside a table. "
    "[Shot 3] At 00:10.000, the sphere leaves the room."
)
FIRST_SENTENCE = (
    "For the target video, at 0.00 seconds into the target video, "
    "<Picture 1> (from [Shot 1]) is fully referenced."
)
_SECTION = re.compile(r"[a-z][a-z0-9_]{0,63}: ")
_MARK = re.compile(r"aligns with the (\d+\.\d\d)-second mark of the target video\.\Z")


def last_sentence(shot: int, mark: str) -> str:
    return (
        "How the reference pictures align with the target video — "
        f"<Picture 1> (from [Shot {shot}]) aligns with the {mark}-second mark of the target video."
    )


def both_sentence(shot: int, mark: str) -> str:
    return (
        "How the reference pictures align with the target video — "
        "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; "
        f"Picture 2 (from Shot {shot}) aligns with the {mark}-second mark of the target video."
    )


@dataclass(frozen=True)
class Source:
    sidebar: SidebarWorkspaceRegistry
    snapshot: ProductionPlanningSourceSnapshot
    report: ContextReport


def compile_report(mode: TaskMode, intent: str, seconds: int, **registry: object) -> ContextReport:
    request = H3ContextRequestNode().build_request(mode, intent, duration_seconds=seconds)[0]
    references = H3ReferenceRegistryNode().build_registry(**registry)[0]
    plan = H3ContextPlanNode().build_plan(request, references)[0]
    document = H3ContextCompilerNode().compile(plan)[2]
    report: ContextReport = H3ContextValidatorNode().validate(plan, document)[1]
    return report


def source(mode: TaskMode, intent: str, seconds: int, **registry: object) -> Source:
    report = compile_report(mode, intent, seconds, **registry)
    assert report.validation.is_valid, [item.code for item in report.validation.diagnostics]
    sidebar = SidebarWorkspaceRegistry(max_entries=8, ttl_seconds=600)
    projection = sidebar.publish(
        report, build_native_h3_wiring(report), ExecutionCorrelation("prompt.m2608", "node.m2608")
    )
    snapshot = sidebar.snapshot_production_planning_source(
        projection.workspace_id,
        expected_report_revision=projection.report_revision,
        expected_report_fingerprint=projection.report_fingerprint,
    )
    return Source(sidebar, snapshot, report)


def frames(mode: TaskMode) -> dict[str, object]:
    return {
        TaskMode.I2VA: {"first_frame": object()},
        TaskMode.L2VA: {"last_frame": object()},
        TaskMode.FL2VA: {"first_frame": object(), "last_frame": object()},
    }[mode]


def rows_at(cuts: tuple[int, ...], target: int) -> tuple[StoryboardShotV1, ...]:
    edges = (0, *cuts, target)
    return tuple(
        StoryboardShotV1(
            f"shot-{index}",
            index,
            edges[index - 1] * 1000,
            edges[index] * 1000,
            f"Reviewed beat {index} of the sphere's path.",
        )
        for index in range(1, len(edges))
    )


def plan(
    origin: Source,
    target: int,
    *,
    rows: tuple[StoryboardShotV1, ...] | None = None,
    policy: SegmentationPolicyV1 = SegmentationPolicyV1.AUTO_STORYBOARD,
) -> tuple[ProductionPlanningContextV2, SegmentationProposalV2]:
    """Prepare, admit and propose; `rows=None` uses the Context's own generated storyboard."""

    context = build_production_planning_context(
        origin.snapshot,
        planning_context_id="planning.m2608",
        revision=1,
        duration_intent=ProductionDurationIntentV1(target_seconds=target, policy=policy),
    )
    admission = ProductionStoryboardAdmissionService().admit(
        context,
        ProductionStoryboardAdmissionRequestV1(
            request_id="storyboard.m2608",
            planning_context_id=context.planning_context_id,
            planning_context_revision=context.revision,
            planning_context_fingerprint=context.fingerprint,
            optimized_candidate_id=context.optimized_candidate_id,
            optimized_candidate_text_fingerprint=context.optimized_candidate_text_fingerprint,
            production_duration_intent_fingerprint=context.duration_intent_fingerprint,
            source_kind=StoryboardSourceKindV1.CANONICAL_OPTIMIZED_PROMPT
            if rows is None
            else StoryboardSourceKindV1.USER_REVIEWED_TYPED_ROWS,
            candidate_text=origin.report.prompt_document.text if rows is None else None,
            typed_rows=() if rows is None else rows,
            user_reviewed=rows is not None,
        ),
    )
    assert isinstance(admission, ProductionStoryboardAdmissionV2), getattr(admission, "code", None)
    proposal = build_segmentation_proposal(context, admission)
    assert isinstance(proposal, SegmentationProposalV2), getattr(proposal, "code", None)
    assert proposal.blocker_codes == ()
    return context, proposal


def shape(
    proposal: SegmentationProposalV2,
) -> tuple[tuple[int, ...], tuple[str, ...], tuple[tuple[str, ...], ...]]:
    return (
        tuple(segment.duration.requested_seconds for segment in proposal.segments),
        tuple(segment.task_mode.value for segment in proposal.segments),
        tuple(segment.asset_ids for segment in proposal.segments),
    )


def preamble(prompt: str) -> str | None:
    """The block before the first canonical section, which is where an alignment sentence sits."""

    head = prompt.split("\n\n", 1)[0]
    return None if _SECTION.match(head) else head


def materialized(
    origin: Source, context: ProductionPlanningContextV2, proposal: SegmentationProposalV2
) -> tuple[ContextReport, ...]:
    """Each segment as its own Context, compiled and judged by the real validator."""

    materialize = CanonicalProductionMaterializer(
        origin.snapshot, workspace_registry=origin.sidebar
    )
    reports: list[ContextReport] = []
    for segment in proposal.segments:
        claim = materialize(context, proposal, segment)
        try:
            report = claim.report
            assert report.validation.is_valid, [d.code for d in report.validation.diagnostics]
            assert report.request.task_mode is segment.task_mode
            assert report.prompt_document.text == segment.local_prompt
            reports.append(report)
        finally:
            claim.release()
    return tuple(reports)


def two_places(seconds: float) -> str:
    return format(Decimal(str(seconds)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def test_first_frame_context_plans_with_the_frame_owned_by_the_opening_segment() -> None:
    origin = source(TaskMode.I2VA, SINGLE, 10, first_frame=object())
    context, proposal = plan(origin, 30, rows=rows_at((8, 20), 30))
    assert shape(proposal) == (
        (8, 12, 10),
        ("i2va", "t2va", "t2va"),
        (("first_frame_1",), (), ()),
    )
    first, *later = (segment.local_prompt for segment in proposal.segments)
    assert preamble(first) == FIRST_SENTENCE
    for prompt in later:
        assert preamble(prompt) is None
        assert "<Picture" not in prompt and "target video" not in prompt
    materialized(origin, context, proposal)


@pytest.mark.parametrize(
    ("target", "policy", "modes"),
    [
        (10, SegmentationPolicyV1.AUTO_STORYBOARD, ("i2va",)),
        (20, SegmentationPolicyV1.FIXED_10, ("i2va", "t2va")),
    ],
)
def test_first_frame_context_plans_from_its_own_generated_storyboard(
    target: int, policy: SegmentationPolicyV1, modes: tuple[str, ...]
) -> None:
    origin = source(TaskMode.I2VA, SINGLE, 10, first_frame=object())
    context, proposal = plan(origin, target, policy=policy)
    assert shape(proposal)[1] == modes
    prompts = tuple(segment.local_prompt for segment in proposal.segments)
    assert preamble(prompts[0]) == FIRST_SENTENCE
    assert all(preamble(prompt) is None for prompt in prompts[1:])
    materialized(origin, context, proposal)


def test_last_frame_context_plans_with_the_frame_owned_by_the_closing_segment() -> None:
    origin = source(TaskMode.L2VA, SINGLE, 15, last_frame=object())
    assert preamble(origin.report.prompt_document.text) == last_sentence(1, "15.08")
    context, proposal = plan(origin, 30, rows=rows_at((8, 20), 30))
    assert shape(proposal) == (
        (8, 12, 10),
        ("t2va", "t2va", "l2va"),
        ((), (), ("last_frame_1",)),
    )
    *earlier, last = (segment.local_prompt for segment in proposal.segments)
    assert all(preamble(prompt) is None and "<Picture" not in prompt for prompt in earlier)
    # The closing clip is ten seconds long: its mark is its own, not the source clip's 15.08.
    assert preamble(last) == last_sentence(1, "10.13")
    materialized(origin, context, proposal)


def test_closing_segment_names_its_own_last_shot() -> None:
    origin = source(TaskMode.L2VA, SINGLE, 15, last_frame=object())
    context, proposal = plan(
        origin, 30, rows=rows_at((8, 20, 25), 30), policy=SegmentationPolicyV1.FIXED_10
    )
    assert shape(proposal)[:2] == ((10, 10, 10), ("t2va", "t2va", "l2va"))
    closing = proposal.segments[-1]
    assert len(closing.shot_mappings) == 2
    assert preamble(closing.local_prompt) == last_sentence(2, "10.13")
    materialized(origin, context, proposal)


def test_two_frame_context_splits_its_frames_between_the_outer_segments() -> None:
    origin = source(TaskMode.FL2VA, SINGLE, 15, first_frame=object(), last_frame=object())
    assert preamble(origin.report.prompt_document.text) == both_sentence(1, "15.08")
    context, proposal = plan(origin, 30, rows=rows_at((8, 20), 30))
    assert shape(proposal) == (
        (8, 12, 10),
        ("i2va", "t2va", "l2va"),
        (("first_frame_1",), (), ("last_frame_1",)),
    )
    opening, middle, closing = (segment.local_prompt for segment in proposal.segments)
    assert preamble(opening) == FIRST_SENTENCE
    assert preamble(middle) is None and "Picture" not in middle
    # The final frame is the closing clip's only picture, so it is that clip's `<Picture 1>`.
    assert preamble(closing) == last_sentence(1, "10.13")
    assert all("15.08" not in segment.local_prompt for segment in proposal.segments)
    materialized(origin, context, proposal)


@pytest.mark.parametrize(("intent", "shots"), [(SINGLE, 1), (MULTI, 3)])
def test_two_frame_context_planned_as_one_clip_keeps_both_frames(intent: str, shots: int) -> None:
    origin = source(TaskMode.FL2VA, intent, 15, first_frame=object(), last_frame=object())
    context, proposal = plan(origin, 15)
    assert shape(proposal) == ((15,), ("fl2va",), (("first_frame_1", "last_frame_1"),))
    prompt = proposal.segments[0].local_prompt
    assert preamble(prompt) == both_sentence(shots, "15.08")
    assert preamble(prompt) == preamble(origin.report.prompt_document.text)
    materialized(origin, context, proposal)


@pytest.mark.parametrize("mode", [TaskMode.I2VA, TaskMode.L2VA, TaskMode.FL2VA])
def test_alignment_sentence_is_not_source_authority(mode: TaskMode) -> None:
    origin = source(mode, SINGLE, 10, **frames(mode))
    assert preamble(origin.report.prompt_document.text) is not None
    context = build_production_planning_context(
        origin.snapshot,
        planning_context_id="planning.m2608",
        revision=1,
        duration_intent=ProductionDurationIntentV1(target_seconds=30),
    )
    authority = context.semantic_authority
    assert "alignment_instruction" not in {field.heading for field in authority.global_fields}
    roles = {item.role for item in authority.reference_definitions}
    assert roles <= {AssetRole.FIRST_FRAME, AssetRole.LAST_FRAME} and roles


@pytest.mark.parametrize("mode", [TaskMode.I2VA, TaskMode.L2VA, TaskMode.FL2VA])
def test_a_derived_mark_is_the_materialized_clip_duration(mode: TaskMode) -> None:
    origin = source(mode, SINGLE, 15, **frames(mode))
    context, proposal = plan(origin, 30, rows=rows_at((9, 17), 30))
    marks = 0
    for segment, report in zip(
        proposal.segments, materialized(origin, context, proposal), strict=True
    ):
        found = _MARK.search(preamble(segment.local_prompt) or "")
        if found is not None:
            marks += 1
            assert found.group(1) == two_places(report.request.effective_duration_seconds)
    assert marks == (0 if mode is TaskMode.I2VA else 1)


@pytest.mark.parametrize(
    ("target", "policy", "durations"),
    [
        (10, SegmentationPolicyV1.AUTO_STORYBOARD, (10,)),
        (20, SegmentationPolicyV1.FIXED_10, (10, 10)),
    ],
)
def test_full_reference_context_plans_from_its_own_generated_storyboard(
    target: int, policy: SegmentationPolicyV1, durations: tuple[int, ...]
) -> None:
    origin = source(TaskMode.REF2VA, SINGLE, 10, images=[object(), object()])
    context, proposal = plan(origin, target, policy=policy)
    assert shape(proposal) == (
        durations,
        ("ref2va",) * len(durations),
        (("image_1", "image_2"),) * len(durations),
    )
    assert all(preamble(segment.local_prompt) is None for segment in proposal.segments)
    materialized(origin, context, proposal)


def _section(report: ContextReport, heading: str) -> str:
    return next(item.body for item in report.prompt_document.sections if item.heading == heading)


@pytest.mark.parametrize("intent", [MULTI, MULTI.removeprefix("[Shot 1] ")])
def test_multi_shot_full_reference_context_is_valid_and_planned(intent: str) -> None:
    report = compile_report(TaskMode.REF2VA, intent, 15, images=[object(), object()])
    assert report.validation.is_valid, [item.code for item in report.validation.diagnostics]
    summary = _section(report, "summary")
    assert "[Shot" not in summary and "00:05.000" not in summary
    assert summary.endswith(
        "A plain sphere enters a quiet room. The sphere stops beside a table. "
        "The sphere leaves the room."
    )
    assert _section(report, "detailed_description") == MULTI

    origin = source(TaskMode.REF2VA, intent, 15, images=[object(), object()])
    context, proposal = plan(origin, 15)
    assert sum(shape(proposal)[0]) == 15
    materialized(origin, context, proposal)
    context, proposal = plan(origin, 30, rows=rows_at((8, 20), 30))
    assert shape(proposal)[:2] == ((8, 12, 10), ("ref2va",) * 3)
    materialized(origin, context, proposal)


def test_a_full_reference_summary_without_shot_markers_is_unchanged() -> None:
    report = compile_report(TaskMode.REF2VA, SINGLE, 10, images=[object()])
    assert _section(report, "summary").endswith(" " + SINGLE)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (TaskMode.L2VA, last_sentence(3, "15.08")),
        (TaskMode.FL2VA, both_sentence(3, "15.08")),
    ],
)
def test_a_single_clip_final_frame_belongs_to_its_last_rendered_shot(
    mode: TaskMode, expected: str
) -> None:
    report = compile_report(mode, MULTI, 15, **frames(mode))
    assert report.validation.is_valid, [item.code for item in report.validation.diagnostics]
    assert len(report.plan.intent_graph.segments) == 1
    assert preamble(report.prompt_document.text) == expected


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


@pytest.mark.parametrize(
    ("mode", "registry", "kind", "rows", "modes"),
    [
        (
            TaskMode.I2VA,
            {"first_frame": object()},
            "user_reviewed_typed_rows",
            rows_at((8, 20), 30),
            ["i2va", "t2va", "t2va"],
        ),
        (
            TaskMode.L2VA,
            {"last_frame": object()},
            "user_reviewed_typed_rows",
            rows_at((8, 20), 30),
            ["t2va", "t2va", "l2va"],
        ),
        (
            TaskMode.REF2VA,
            {"images": [object(), object()]},
            "canonical_optimized_prompt",
            (),
            ["ref2va", "ref2va"],
        ),
    ],
)
def test_service_plans_and_imports_a_reference_bearing_context(
    mode: TaskMode,
    registry: dict[str, object],
    kind: str,
    rows: tuple[StoryboardShotV1, ...],
    modes: list[str],
) -> None:
    report = compile_report(mode, SINGLE, 10, **registry)
    sidebar = SidebarWorkspaceRegistry(clock=lambda: 10.0, ttl_seconds=60)
    published = sidebar.publish(
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
            "payload": {"context_workspace_handle": published.workspace_id},
        }
    ).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    service = ProductionPlanningService(
        sidebar_registry=sidebar, production_registry=production, clock=lambda: 10.0
    )
    reviewed = kind == "user_reviewed_typed_rows"
    prepared: dict[str, Any] = service.dispatch(
        _action(
            "prepare_context",
            {
                "workspace_handle": created.workspace_handle,
                "expected_workspace_revision": created.workspace_revision,
                "expected_workspace_fingerprint": created.workspace_fingerprint,
                "context_workspace_handle": published.workspace_id,
                "expected_report_revision": published.report_revision,
                "expected_report_fingerprint": published.report_fingerprint,
                "expected_planning_revision": 0,
                "target_seconds": 30 if reviewed else 20,
                "policy": "auto_storyboard" if reviewed else "fixed_10",
            },
            "prepare",
        )
    ).to_wire()
    admitted: dict[str, Any] = service.dispatch(
        _action(
            "admit_storyboard",
            {
                **_selectors(prepared),
                "source_kind": kind,
                "typed_rows": [row.to_wire() for row in rows],
                "user_reviewed": reviewed,
            },
            "admit",
        )
    ).to_wire()
    proposed: dict[str, Any] = service.dispatch(
        _action(
            "propose", {**_selectors(admitted), "admission_id": admitted["admission_id"]}, "propose"
        )
    ).to_wire()
    assert [row["task_mode"] for row in proposed["proposal"]["segments"]] == modes
    imported: dict[str, Any] = service.dispatch(
        _action(
            "import_plan",
            {**_selectors(proposed), "proposal_id": proposed["proposal"]["proposal_id"]},
            "import",
        )
    ).to_wire()
    assert len(imported["materialization_receipt_fingerprints"]) == len(modes)
