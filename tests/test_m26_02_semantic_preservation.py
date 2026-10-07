"""Independent semantic-loss and reference-admission regressions for Production planning."""

from dataclasses import replace

import pytest

from comfyui_h3_context.core.contracts import AssetRole, TaskMode
from comfyui_h3_context.core.production_duration import (
    ProductionDurationIntentV1,
    SegmentationPolicyV1,
)
from comfyui_h3_context.core.production_semantics import (
    ProductionSemanticAuthorityV1,
    SemanticFieldV1,
    SemanticSplitPolicyV1,
    TimedSemanticSpanV1,
)
from comfyui_h3_context.core.production_storyboard import (
    PlanningAssetV1,
    ProductionPlanningContextV2,
    ProductionStoryboardAdmissionRequestV1,
    ProductionStoryboardAdmissionService,
    ProductionStoryboardAdmissionV2,
    SegmentationProposalRefusalV1,
    SegmentationProposalV2,
    StoryboardAdmissionRefusalV1,
    StoryboardShotV1,
    StoryboardSourceKindV1,
    build_segmentation_proposal,
    fingerprint_prompt_text,
)
from comfyui_h3_context.core.rendering import render_base_prompt

FP = "sha256:" + "a" * 64


def context(text: str, *, mode: TaskMode = TaskMode.T2VA) -> ProductionPlanningContextV2:
    return ProductionPlanningContextV2(
        planning_context_id="planning_semantics",
        revision=1,
        optimized_candidate_id="candidate_semantics",
        optimized_candidate_text_fingerprint=fingerprint_prompt_text(text),
        source_request_fingerprint=FP,
        source_intent_graph_fingerprint=FP,
        source_profile_fingerprint=FP,
        reference_registry_fingerprint=FP,
        source_context_duration_seconds=10,
        source_context_duration_provenance="context_report",
        production_duration_intent=ProductionDurationIntentV1(
            target_seconds=20, policy=SegmentationPolicyV1.FIXED_10
        ),
        global_task_mode=mode,
        subject_ids=("subject_known",),
        assets=(PlanningAssetV1("image_known", AssetRole.FIRST_FRAME),)
        if mode is TaskMode.I2VA
        else (),
    )


def admit(
    ctx: ProductionPlanningContextV2,
    *,
    text: str | None = None,
    rows: tuple[StoryboardShotV1, ...] = (),
) -> ProductionStoryboardAdmissionV2 | StoryboardAdmissionRefusalV1:
    return ProductionStoryboardAdmissionService().admit(
        ctx,
        ProductionStoryboardAdmissionRequestV1(
            request_id="request_semantics",
            planning_context_id=ctx.planning_context_id,
            planning_context_revision=ctx.revision,
            planning_context_fingerprint=ctx.fingerprint,
            optimized_candidate_id=ctx.optimized_candidate_id,
            optimized_candidate_text_fingerprint=ctx.optimized_candidate_text_fingerprint,
            production_duration_intent_fingerprint=ctx.duration_intent_fingerprint,
            source_kind=StoryboardSourceKindV1.CANONICAL_OPTIMIZED_PROMPT
            if text is not None
            else StoryboardSourceKindV1.USER_REVIEWED_TYPED_ROWS,
            candidate_text=text,
            typed_rows=rows,
            user_reviewed=text is None,
        ),
    )


def shots() -> tuple[StoryboardShotV1, ...]:
    return (
        StoryboardShotV1("opening", 1, 0, 10_000, "Opening.", subject_ids=("subject_known",)),
        StoryboardShotV1("ending", 2, 10_000, 20_000, "Ending.", subject_ids=("subject_known",)),
    )


@pytest.mark.parametrize("field", ["subject_ids", "asset_ids"])
def test_foreign_typed_reference_is_refused_before_admission(field: str) -> None:
    rows = shots()
    changed = (
        replace(rows[0], subject_ids=("foreign_reference",))
        if field == "subject_ids"
        else replace(rows[0], asset_ids=("foreign_reference",))
    )
    result = admit(context("Reviewed description."), rows=(changed, rows[1]))
    assert isinstance(result, StoryboardAdmissionRefusalV1)


def test_registered_but_unavailable_local_asset_blocks_proposal() -> None:
    ctx = context("Reviewed description.", mode=TaskMode.I2VA)
    rows = shots()
    result = admit(ctx, rows=(rows[0], replace(rows[1], asset_ids=("image_known",))))
    assert isinstance(result, ProductionStoryboardAdmissionV2)
    proposal = build_segmentation_proposal(ctx, result)
    assert isinstance(proposal, SegmentationProposalV2)
    assert not proposal.importable
    assert proposal.segments[1].task_mode is TaskMode.T2VA
    assert proposal.segments[1].asset_ids == ()


def test_real_canonical_renderer_audio_survives_local_proposal() -> None:
    from test_rendering import make_plan

    document = render_base_prompt(make_plan(TaskMode.T2VA))
    assert "\n\noverall_soundscape: " in document.text
    ctx = context(document.text)
    admitted = admit(ctx, text=document.text)
    assert isinstance(admitted, ProductionStoryboardAdmissionV2)
    proposal = build_segmentation_proposal(ctx, admitted)
    assert isinstance(proposal, SegmentationProposalV2)
    assert proposal.importable
    for segment in proposal.segments:
        for section in document.sections:
            if section.heading in {"overall_soundscape", "non_diegetic_music"}:
                assert f"{section.heading}: {section.body}" in segment.local_prompt


def test_unknown_structured_section_cannot_be_silently_dropped() -> None:
    text = (
        "integrated_multimodal_description: [Shot 1] Opening. "
        "[Shot 2] At 00:10.000, Ending.\n\n"
        "unknown_semantic_field: Preserve this declared meaning.\n\n"
        "overall_soundscape: Wind.\n\nnon_diegetic_music: N/A"
    )
    result = admit(context(text), text=text)
    assert isinstance(result, StoryboardAdmissionRefusalV1)


def test_unassigned_description_prefix_is_refused_instead_of_dropped() -> None:
    text = "integrated_multimodal_description: Keep the exact costume. [Shot 1] Opening."
    assert isinstance(admit(context(text), text=text), StoryboardAdmissionRefusalV1)


@pytest.mark.parametrize("token", ["<Subject 99>", "<Subject 0>", "<Picture 0>", "<Audio unknown>"])
def test_unknown_canonical_reference_tokens_are_refused(token: str) -> None:
    text = f"integrated_multimodal_description: [Shot 1] Follow {token}."
    assert isinstance(admit(context(text), text=text), StoryboardAdmissionRefusalV1)


def semantic_proposal() -> tuple[
    ProductionPlanningContextV2, ProductionStoryboardAdmissionV2, SegmentationProposalV2
]:
    ctx = replace(
        context("Reviewed description."),
        semantic_authority=ProductionSemanticAuthorityV1(
            global_fields=(SemanticFieldV1("audio", "overall_soundscape", "Continuous rain."),),
            timed_spans=(
                TimedSemanticSpanV1(
                    "bell",
                    "audio",
                    9_500,
                    10_500,
                    "A bell rings.",
                    split_policy=SemanticSplitPolicyV1.CLIP,
                ),
            ),
        ),
    )
    admission = admit(ctx, rows=shots())
    assert isinstance(admission, ProductionStoryboardAdmissionV2)
    proposal = build_segmentation_proposal(ctx, admission)
    assert isinstance(proposal, SegmentationProposalV2)
    return ctx, admission, proposal


def test_timed_audio_exact_rebase_and_complete_wire_roundtrip() -> None:
    ctx, admission, proposal = semantic_proposal()
    assert ProductionPlanningContextV2.from_wire(ctx.to_wire()) == ctx
    assert ProductionStoryboardAdmissionV2.from_wire(admission.to_wire()) == admission
    assert SegmentationProposalV2.from_wire(proposal.to_wire()) == proposal
    assert "From 00:09.500 to 00:10.000, A bell rings." in proposal.segments[0].local_prompt
    assert "From 00:00.000 to 00:00.500, A bell rings." in proposal.segments[1].local_prompt
    assert all("Continuous rain." in item.local_prompt for item in proposal.segments)
    assert build_segmentation_proposal(ctx, admission) == proposal


@pytest.mark.parametrize("source_length", [95, 96, 127, 128])
def test_maximum_timed_source_identity_remains_importable(source_length: int) -> None:
    ctx, _, _ = semantic_proposal()
    source_id = "T" + "x" * (source_length - 1)
    ctx = replace(
        ctx,
        semantic_authority=replace(
            ctx.semantic_authority,
            timed_spans=(replace(ctx.semantic_authority.timed_spans[0], span_id=source_id),),
        ),
    )
    admission = admit(ctx, rows=shots())
    assert isinstance(admission, ProductionStoryboardAdmissionV2)
    proposal = build_segmentation_proposal(ctx, admission)
    assert isinstance(proposal, SegmentationProposalV2)
    assert proposal.importable
    assert len(proposal.segments) == 2
    assert proposal.semantic_census.complete
    local_spans = tuple(item.timed_spans[0] for item in proposal.semantic_slices)
    assert {item.source_span_id for item in local_spans} == {source_id}
    assert len({item.span_id for item in local_spans}) == 2
    assert SegmentationProposalV2.from_wire(proposal.to_wire()) == proposal
    assert "From 00:09.500 to 00:10.000, A bell rings." in proposal.segments[0].local_prompt
    assert "From 00:00.000 to 00:00.500, A bell rings." in proposal.segments[1].local_prompt


@pytest.mark.parametrize("change", ["drop_global", "change_global", "drop_span"])
def test_recomposed_admission_cannot_replace_source_semantics(change: str) -> None:
    ctx, admission, _ = semantic_proposal()
    semantics = admission.semantic_authority
    if change == "drop_global":
        semantics = replace(semantics, global_fields=())
    elif change == "change_global":
        semantics = replace(
            semantics, global_fields=(replace(semantics.global_fields[0], text="Silence."),)
        )
    else:
        semantics = replace(semantics, timed_spans=())
    forged = replace(admission, semantic_authority=semantics)
    assert forged.fingerprint != admission.fingerprint
    assert isinstance(build_segmentation_proposal(ctx, forged), SegmentationProposalRefusalV1)


@pytest.mark.parametrize("change", ["slice_text", "slice_span", "prompt", "constraints"])
def test_self_consistent_proposal_recomposition_cannot_erase_semantics(change: str) -> None:
    _, _, proposal = semantic_proposal()
    with pytest.raises(ValueError):
        if change == "slice_text":
            first = proposal.semantic_slices[0]
            replace(
                proposal,
                semantic_slices=(replace(first, global_fields=()), *proposal.semantic_slices[1:]),
            )
        elif change == "slice_span":
            first = proposal.semantic_slices[0]
            replace(
                proposal,
                semantic_slices=(replace(first, timed_spans=()), *proposal.semantic_slices[1:]),
            )
        elif change == "prompt":
            first_segment = replace(proposal.segments[0], local_prompt="[Shot 1] Lost everything.")
            replace(proposal, segments=(first_segment, *proposal.segments[1:]))
        else:
            replace(
                proposal,
                cut_constraints=replace(
                    proposal.cut_constraints, required_cut_milliseconds=(8_000,)
                ),
            )


@pytest.mark.parametrize("count", range(16, 65))
def test_dense_shots_preserve_every_source_mapping_with_independent_output_bound(
    count: int,
) -> None:
    ctx = replace(
        context("Reviewed dense storyboard."),
        production_duration_intent=ProductionDurationIntentV1(
            target_seconds=60,
            policy=SegmentationPolicyV1.AUTO_STORYBOARD,
        ),
    )
    rows = tuple(
        StoryboardShotV1(
            f"dense_{index}",
            index + 1,
            60_000 * index // count,
            60_000 * (index + 1) // count,
            f"Scene {index}.",
        )
        for index in range(count)
    )
    admission = admit(ctx, rows=rows)
    assert isinstance(admission, ProductionStoryboardAdmissionV2)
    proposal = build_segmentation_proposal(ctx, admission)
    assert isinstance(proposal, SegmentationProposalV2)
    assert proposal.importable
    assert len(proposal.segments) <= 15
    assert sum(item.duration.requested_seconds for item in proposal.segments) == 60
    assert {shot.shot_id for shot in rows} == {
        mapping.global_shot_id for segment in proposal.segments for mapping in segment.shot_mappings
    }
    assert SegmentationProposalV2.from_wire(proposal.to_wire()) == proposal


def test_partial_timed_coverage_cannot_claim_complete_census() -> None:
    from comfyui_h3_context.core.production_semantics import assert_complete_semantic_census

    _, _, proposal = semantic_proposal()
    with pytest.raises(ValueError, match="semantic_timed_coverage"):
        assert_complete_semantic_census(proposal.semantic_authority, proposal.semantic_slices[:1])


@pytest.mark.parametrize("start,end", [(19_000, 21_000), (20_000, 21_000)])
def test_source_timed_span_cannot_exceed_production_clock(start: int, end: int) -> None:
    with pytest.raises(ValueError, match="planning_semantic_time_domain"):
        replace(
            context("Reviewed."),
            semantic_authority=ProductionSemanticAuthorityV1(
                timed_spans=(TimedSemanticSpanV1("late", "audio", start, end, "Late sound."),)
            ),
        )
