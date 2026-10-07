from __future__ import annotations

from decimal import Decimal

import pytest

from comfyui_h3_context.core.constraints import (
    ContentScope,
    HardConstraintSet,
    RequiredContent,
    TimePoint,
    TimingConstraint,
)
from comfyui_h3_context.core.context_reporting import PromptSection
from comfyui_h3_context.core.contracts import AssetRole, MediaKind, TaskMode
from comfyui_h3_context.core.production_semantics import (
    ProductionLocalShotV1,
    ProductionSemanticAuthorityV1,
    ProductionSemanticError,
    ProductionSemanticSliceV1,
    ReferenceDefinitionV1,
    SemanticDispositionV1,
    SemanticFieldV1,
    SemanticSplitPolicyV1,
    SubjectDefinitionV1,
    TimedSemanticSpanV1,
    assert_complete_semantic_census,
    bind_canonical_semantic_references,
    derive_production_semantic_authority,
    merge_semantic_authorities,
    parse_canonical_semantics,
    slice_production_semantics,
    validate_semantic_references,
)
from comfyui_h3_context.core.rendering import (
    render_production_segment_prompt,
    render_prompt_sections,
)

# A ten-second clip on the H3 frame lattice: 243 frames at 24 fps.
CLIP = Decimal("10.125")


def _authority(*, split_policy: SemanticSplitPolicyV1) -> ProductionSemanticAuthorityV1:
    return ProductionSemanticAuthorityV1(
        subject_definitions=(
            SubjectDefinitionV1(
                subject_id="subject_a",
                label="The fox",
                connection_order=1,
                description="wearing a blue coat",
                source_asset_ids=("picture_a",),
            ),
        ),
        reference_definitions=(
            ReferenceDefinitionV1(
                asset_id="picture_a",
                kind=MediaKind.IMAGE,
                role=AssetRole.SUBJECT_REFERENCE,
                connection_order=1,
                backend_label="<Picture 1>",
            ),
        ),
        global_fields=(
            SemanticFieldV1(
                field_id="soundscape",
                heading="overall_soundscape",
                text="Rain on glass.",
            ),
        ),
        timed_spans=(
            TimedSemanticSpanV1(
                span_id="dialogue_a",
                kind="exact_dialogue",
                start_milliseconds=2_000,
                end_milliseconds=8_000,
                text="<d>[en] Keep  two spaces.</d>",
                subject_ids=("subject_a",),
                asset_ids=("picture_a",),
                split_policy=split_policy,
            ),
        ),
        required_cut_milliseconds=(8_000,),
    )


def test_semantic_authority_is_closed_bounded_and_fingerprint_binds_content() -> None:
    authority = _authority(split_policy=SemanticSplitPolicyV1.CLIP)
    assert ProductionSemanticAuthorityV1.from_wire(authority.to_wire()) == authority
    assert authority.fingerprint.startswith("sha256:")

    tampered = authority.to_wire()
    tampered["extra"] = True
    with pytest.raises(ProductionSemanticError, match="semantic_authority_wire"):
        ProductionSemanticAuthorityV1.from_wire(tampered)

    with pytest.raises(ProductionSemanticError, match="semantic_total_text"):
        ProductionSemanticAuthorityV1(
            global_fields=(
                SemanticFieldV1(
                    field_id="oversized",
                    heading="summary",
                    text="x" * 65_536,
                ),
            ),
            timed_spans=(
                TimedSemanticSpanV1(
                    span_id="more",
                    kind="scene",
                    start_milliseconds=0,
                    end_milliseconds=1,
                    text="y",
                ),
            ),
        )


def test_slice_rebases_clip_content_and_requires_review_for_immutable_crossing() -> None:
    clip = slice_production_semantics(
        _authority(split_policy=SemanticSplitPolicyV1.CLIP),
        segment_id="segment_1",
        global_start_milliseconds=0,
        global_end_milliseconds=5_000,
        task_mode=TaskMode.REF2VA,
        asset_ids=("picture_a",),
    )
    assert clip.timed_spans[0].start_milliseconds == 2_000
    assert clip.timed_spans[0].end_milliseconds == 5_000
    assert clip.timed_spans[0].source_span_id == "dialogue_a"
    assert clip.census[-1].disposition is SemanticDispositionV1.ASSIGNED_REBASED

    immutable = slice_production_semantics(
        _authority(split_policy=SemanticSplitPolicyV1.IMMUTABLE),
        segment_id="segment_1",
        global_start_milliseconds=0,
        global_end_milliseconds=5_000,
        task_mode=TaskMode.REF2VA,
        asset_ids=("picture_a",),
    )
    assert immutable.timed_spans == ()
    assert immutable.census[-1].disposition is SemanticDispositionV1.REVIEW_REQUIRED
    with pytest.raises(ProductionSemanticError, match="semantic_preservation_incomplete"):
        assert_complete_semantic_census(
            _authority(split_policy=SemanticSplitPolicyV1.IMMUTABLE), (immutable,)
        )


@pytest.mark.parametrize("source_length", [1, 95, 96, 127, 128])
@pytest.mark.parametrize("segment_length", [1, 32, 127, 128])
def test_slice_identity_preserves_the_complete_identifier_domain(
    source_length: int, segment_length: int
) -> None:
    source_ids = tuple(prefix + "x" * (source_length - 1) for prefix in ("A", "B"))
    authority = ProductionSemanticAuthorityV1(
        timed_spans=tuple(
            TimedSemanticSpanV1(
                span_id=source_id,
                kind="audio",
                start_milliseconds=9_500,
                end_milliseconds=10_500,
                text="A sustained note.",
            )
            for source_id in source_ids
        )
    )
    slices = tuple(
        slice_production_semantics(
            authority,
            segment_id=prefix + "s" * (segment_length - 1),
            global_start_milliseconds=ordinal * 10_000,
            global_end_milliseconds=(ordinal + 1) * 10_000,
            task_mode=TaskMode.T2VA,
            asset_ids=(),
        )
        for ordinal, prefix in enumerate(("C", "D"))
    )
    local_spans = tuple(span for item in slices for span in item.timed_spans)
    assert len({span.span_id for span in local_spans}) == 4
    assert all(1 <= len(span.span_id) <= 128 for span in local_spans)
    assert {span.source_span_id for span in local_spans} == set(source_ids)
    assert [(span.start_milliseconds, span.end_milliseconds) for span in local_spans] == [
        (9_500, 10_000),
        (9_500, 10_000),
        (0, 500),
        (0, 500),
    ]
    assert assert_complete_semantic_census(authority, slices).complete
    for item in slices:
        assert ProductionSemanticSliceV1.from_wire(item.to_wire()) == item
        assert (
            slice_production_semantics(
                authority,
                segment_id=item.segment_id,
                global_start_milliseconds=item.global_start_milliseconds,
                global_end_milliseconds=item.global_end_milliseconds,
                task_mode=item.task_mode,
                asset_ids=item.asset_ids,
            )
            == item
        )


def test_complete_census_accounts_for_global_and_timed_source_content() -> None:
    authority = _authority(split_policy=SemanticSplitPolicyV1.CLIP)
    first = slice_production_semantics(
        authority,
        segment_id="segment_1",
        global_start_milliseconds=0,
        global_end_milliseconds=5_000,
        task_mode=TaskMode.REF2VA,
        asset_ids=("picture_a",),
    )
    second = slice_production_semantics(
        authority,
        segment_id="segment_2",
        global_start_milliseconds=5_000,
        global_end_milliseconds=10_000,
        task_mode=TaskMode.REF2VA,
        asset_ids=("picture_a",),
    )
    census = assert_complete_semantic_census(authority, (first, second))
    assert census.complete
    assert {entry.source_id for entry in census.entries} == {
        "subject_a",
        "picture_a",
        "soundscape",
        "dialogue_a",
    }


def test_membership_and_local_asset_availability_fail_closed() -> None:
    authority = _authority(split_policy=SemanticSplitPolicyV1.CLIP)
    with pytest.raises(ProductionSemanticError, match="semantic_subject_reference_unknown"):
        ProductionSemanticAuthorityV1(
            global_fields=(
                SemanticFieldV1(
                    field_id="foreign",
                    heading="summary",
                    text="Foreign subject content.",
                    subject_ids=("foreign_subject",),
                ),
            )
        )
    with pytest.raises(ProductionSemanticError, match="semantic_local_asset_unavailable"):
        validate_semantic_references(
            authority,
            asset_ids=("picture_a",),
            target_asset_ids=(),
        )

    unavailable = slice_production_semantics(
        authority,
        segment_id="segment_1",
        global_start_milliseconds=0,
        global_end_milliseconds=5_000,
        task_mode=TaskMode.T2VA,
        asset_ids=(),
    )
    assert unavailable.census[-1].disposition is SemanticDispositionV1.REJECTED


def test_subject_references_inherit_the_subjects_required_local_assets() -> None:
    authority = ProductionSemanticAuthorityV1(
        subject_definitions=(
            SubjectDefinitionV1("subject_a", "The fox", 1, source_asset_ids=("picture_a",)),
        ),
        reference_definitions=(
            ReferenceDefinitionV1(
                "picture_a",
                MediaKind.IMAGE,
                AssetRole.SUBJECT_REFERENCE,
                1,
                "<Picture 1>",
            ),
        ),
        global_fields=(
            SemanticFieldV1("summary_a", "summary", "The fox waits.", subject_ids=("subject_a",)),
        ),
        timed_spans=(
            TimedSemanticSpanV1(
                "span_a",
                "action",
                0,
                5_000,
                "The fox turns.",
                subject_ids=("subject_a",),
            ),
        ),
    )
    sliced = slice_production_semantics(
        authority,
        segment_id="segment_1",
        global_start_milliseconds=0,
        global_end_milliseconds=5_000,
        task_mode=TaskMode.T2VA,
        asset_ids=(),
    )
    assert sliced.subject_definitions == ()
    assert sliced.global_fields == ()
    assert sliced.timed_spans == ()
    assert {
        entry.source_id
        for entry in sliced.census
        if entry.disposition is SemanticDispositionV1.REJECTED
    } == {"summary_a", "span_a"}


def test_canonical_parser_preserves_globals_and_refuses_unknown_structured_sections() -> None:
    assert parse_canonical_semantics("[Shot 1] Reviewed exact prose.") == (
        ProductionSemanticAuthorityV1()
    )
    parsed = parse_canonical_semantics(
        "Align <Picture 1> at zero.\n\n"
        "integrated_multimodal_description: [Shot 1] A fox waits.\n\n"
        "overall_soundscape: Rain on glass.\n\n"
        "non_diegetic_music: N/A"
    )
    assert [field.heading for field in parsed.global_fields] == [
        "alignment_instruction",
        "overall_soundscape",
        "non_diegetic_music",
    ]
    with pytest.raises(ProductionSemanticError, match="canonical_semantic_unknown_section"):
        parse_canonical_semantics(
            "integrated_multimodal_description: [Shot 1] A fox waits.\n\n"
            "invented_structured_section: must not pass"
        )

    merged = merge_semantic_authorities(parsed, parsed)
    assert merged == parsed


def test_canonical_subject_tokens_bind_to_stable_subject_ids() -> None:
    authority = _authority(split_policy=SemanticSplitPolicyV1.CLIP)
    bound = bind_canonical_semantic_references(
        merge_semantic_authorities(
            authority,
            ProductionSemanticAuthorityV1(
                global_fields=(
                    SemanticFieldV1(
                        "canonical_summary",
                        "summary",
                        "<Subject 1> follows <Picture 1>.",
                    ),
                )
            ),
        )
    )
    field = next(item for item in bound.global_fields if item.field_id == "canonical_summary")
    assert field.subject_ids == ("subject_a",)
    assert field.asset_ids == ("picture_a",)

    with pytest.raises(ProductionSemanticError, match="canonical_subject_binding_missing"):
        bind_canonical_semantic_references(
            merge_semantic_authorities(
                authority,
                ProductionSemanticAuthorityV1(
                    global_fields=(
                        SemanticFieldV1("canonical_summary", "summary", "<Subject 2> is unknown."),
                    )
                ),
            )
        )


def test_segment_renderer_uses_canonical_sections_and_preserves_exact_text() -> None:
    authority = _authority(split_policy=SemanticSplitPolicyV1.CLIP)
    semantic_slice = slice_production_semantics(
        authority,
        segment_id="segment_1",
        global_start_milliseconds=0,
        global_end_milliseconds=10_000,
        task_mode=TaskMode.REF2VA,
        asset_ids=("picture_a",),
    )
    rendered = render_production_segment_prompt(
        semantic_slice,
        (
            ProductionLocalShotV1(
                shot_id="shot_1",
                ordinal=1,
                start_milliseconds=0,
                prose="The fox waits.",
                exact_dialogue=("<d>[en] Keep  two spaces.</d>",),
                visible_text=("OPEN  NOW",),
            ),
        ),
        section_heading="detailed_description",
        clip_duration_seconds=CLIP,
    )
    assert "production_segment:" not in rendered.text
    assert "global_span:" not in rendered.text
    assert "join_policy:" not in rendered.text
    assert "<d>[en] Keep  two spaces.</d>" in rendered.text
    assert "OPEN  NOW" in rendered.text
    assert [section.heading for section in rendered.sections] == [
        "subject_definitions",
        "detailed_description",
        "overall_soundscape",
    ]
    assert "wearing a blue coat" in rendered.text
    assert "<Picture 1>" in rendered.text
    assert "From 00:02.000 to 00:08.000," in rendered.text
    assert "<Subject 1> is The fox, wearing a blue coat from <Picture 1>." in rendered.text


def test_segment_renderer_emits_each_standalone_reference_once() -> None:
    authority = ProductionSemanticAuthorityV1(
        subject_definitions=(
            SubjectDefinitionV1(
                "subject_a",
                "The fox",
                1,
                source_asset_ids=("picture_a",),
            ),
        ),
        reference_definitions=(
            ReferenceDefinitionV1(
                "picture_a",
                MediaKind.IMAGE,
                AssetRole.SUBJECT_REFERENCE,
                1,
                "<Picture 1>",
            ),
            ReferenceDefinitionV1(
                "picture_b",
                MediaKind.IMAGE,
                AssetRole.STYLE_REFERENCE,
                2,
                "<Picture 2>",
            ),
        ),
    )
    sliced = slice_production_semantics(
        authority,
        segment_id="segment_1",
        global_start_milliseconds=0,
        global_end_milliseconds=5_000,
        task_mode=TaskMode.REF2VA,
        asset_ids=("picture_a", "picture_b"),
    )
    rendered = render_production_segment_prompt(
        sliced,
        (ProductionLocalShotV1("shot_1", 1, 0, "The fox waits."),),
        section_heading="detailed_description",
        clip_duration_seconds=CLIP,
    )
    definitions = rendered.sections[0]
    assert definitions.heading == "subject_definitions"
    assert definitions.body == (
        "<Subject 1> is The fox from <Picture 1>. <Picture 2> is the style reference."
    )
    assert rendered.text.count("<Picture 1>") == 1
    assert rendered.text.count("<Picture 2>") == 1


def test_equal_typed_and_parsed_subject_definitions_are_not_duplicated() -> None:
    exact = "<Subject 1> is The fox, wearing a blue coat from <Picture 1>."
    authority = _authority(split_policy=SemanticSplitPolicyV1.CLIP)
    authority = merge_semantic_authorities(
        authority,
        ProductionSemanticAuthorityV1(
            global_fields=(
                SemanticFieldV1("canonical_subject_definitions", "subject_definitions", exact),
            )
        ),
    )
    sliced = slice_production_semantics(
        authority,
        segment_id="segment_1",
        global_start_milliseconds=0,
        global_end_milliseconds=10_000,
        task_mode=TaskMode.REF2VA,
        asset_ids=("picture_a",),
    )
    rendered = render_production_segment_prompt(
        sliced,
        (ProductionLocalShotV1("shot_1", 1, 0, "The fox waits."),),
        section_heading="detailed_description",
        clip_duration_seconds=CLIP,
    )
    assert rendered.text.count(exact) == 1


def test_shared_section_serializer_keeps_exact_double_newline_boundary() -> None:
    sections = (
        PromptSection("section_a", 1, "summary", "Exact summary."),
        PromptSection("section_b", 2, "overall_soundscape", "N/A"),
    )
    assert render_prompt_sections(sections) == (
        "summary: Exact summary.\n\noverall_soundscape: N/A"
    )


def test_last_reference_maps_once_to_local_registry_and_protects_exact_text() -> None:
    from dataclasses import replace

    from comfyui_h3_context.core.errors import PromptRenderingError

    authority = ProductionSemanticAuthorityV1(
        reference_definitions=(
            ReferenceDefinitionV1(
                "first", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1, "<Picture 1>"
            ),
            ReferenceDefinitionV1("last", MediaKind.IMAGE, AssetRole.LAST_FRAME, 2, "<Picture 2>"),
        )
    )
    local = slice_production_semantics(
        authority,
        segment_id="last_segment",
        global_start_milliseconds=10_000,
        global_end_milliseconds=20_000,
        task_mode=TaskMode.L2VA,
        asset_ids=("last",),
    )
    shot = ProductionLocalShotV1("shot_1", 1, 0, "End at <Picture 2>.")
    rendered = render_production_segment_prompt(
        local,
        (shot,),
        section_heading="integrated_multimodal_description",
        clip_duration_seconds=CLIP,
    )
    assert "<Picture 2>" not in rendered.text
    assert "End at <Picture 1>." in rendered.text
    # The closing clip states its own alignment: its only picture, its own duration mark.
    assert rendered.text.startswith(
        "How the reference pictures align with the target video — <Picture 1> "
        "(from [Shot 1]) aligns with the 10.13-second mark of the target video.\n\n"
    )
    assert local.reference_definitions[0].asset_id == "last"
    assert local.reference_definitions[0].backend_label == "<Picture 2>"
    with pytest.raises(PromptRenderingError, match="lost exact text"):
        render_production_segment_prompt(
            local,
            (replace(shot, visible_text=("<Picture 2>",)),),
            section_heading="integrated_multimodal_description",
            clip_duration_seconds=CLIP,
        )


def test_typed_plan_and_exact_document_derive_source_authority_without_prose_timing() -> None:
    from test_rendering import make_plan

    from comfyui_h3_context.core.rendering import render_base_prompt

    plan = make_plan(TaskMode.T2VA)
    document = render_base_prompt(plan)
    authority = derive_production_semantic_authority(plan, document)
    # Plain prose is storyboard content, not timed authority.
    assert authority.timed_spans == ()
    assert [item.heading for item in authority.global_fields] == [
        "overall_soundscape",
        "non_diegetic_music",
    ]

    constrained = make_plan(
        TaskMode.T2VA,
        constraints=HardConstraintSet(
            (RequiredContent("required_door", ContentScope.SCENE, "A brass doorway"),)
        ),
    )
    constrained_document = render_base_prompt(constrained)
    carried = derive_production_semantic_authority(constrained, constrained_document)
    assert len(carried.timed_spans) == len(constrained.intent_graph.segments)
    assert all(item.text != constrained_document.sections[0].body for item in carried.timed_spans)


def test_typed_timing_range_becomes_a_forbidden_interval_not_required_cuts() -> None:
    from test_rendering import make_plan

    from comfyui_h3_context.core.rendering import render_base_prompt

    constraints = HardConstraintSet(
        (
            TimingConstraint(
                "timing_range",
                TimePoint.from_text("2.500"),
                TimePoint.from_text("4.000"),
                "dialogue window",
            ),
        )
    )
    plan = make_plan(TaskMode.T2VA, constraints=constraints)
    authority = derive_production_semantic_authority(plan, render_base_prompt(plan))
    assert authority.required_cut_milliseconds == ()
    assert authority.forbidden_cut_intervals_milliseconds == ((2_500, 4_000),)
    assert ProductionSemanticAuthorityV1.from_wire(authority.to_wire()) == authority


def test_typed_timing_point_requires_review_instead_of_inventing_a_cut() -> None:
    from test_rendering import make_plan

    from comfyui_h3_context.core.rendering import render_base_prompt

    constraints = HardConstraintSet(
        (TimingConstraint("timing_point", TimePoint.from_text("2.500")),)
    )
    plan = make_plan(TaskMode.T2VA, constraints=constraints)
    with pytest.raises(ProductionSemanticError, match="semantic_timing_point_requires_review"):
        derive_production_semantic_authority(plan, render_base_prompt(plan))


def test_dense_semantic_slice_census_round_trips_above_sixty_four_rows() -> None:
    subjects = tuple(
        SubjectDefinitionV1(
            f"subject_{index}",
            f"Subject {index}",
            index,
            source_asset_ids=(f"picture_{index}",),
        )
        for index in range(1, 41)
    )
    references = tuple(
        ReferenceDefinitionV1(
            f"picture_{index}",
            MediaKind.IMAGE,
            AssetRole.SUBJECT_REFERENCE,
            index,
            f"<Picture {index}>",
        )
        for index in range(1, 41)
    )
    authority = ProductionSemanticAuthorityV1(
        subject_definitions=subjects,
        reference_definitions=references,
    )
    sliced = slice_production_semantics(
        authority,
        segment_id="segment_dense",
        global_start_milliseconds=0,
        global_end_milliseconds=5_000,
        task_mode=TaskMode.REF2VA,
        asset_ids=tuple(item.asset_id for item in references),
    )
    assert len(sliced.census) == 80
    assert type(sliced).from_wire(sliced.to_wire()) == sliced


def test_segment_renderer_refuses_a_carried_alignment_sentence() -> None:
    from comfyui_h3_context.core.errors import PromptRenderingError

    authority = ProductionSemanticAuthorityV1(
        global_fields=(
            SemanticFieldV1(
                "alignment_instruction",
                "alignment_instruction",
                "Picture 2 aligns with the 15.08-second mark of the target video.",
            ),
        )
    )
    carried = slice_production_semantics(
        authority,
        segment_id="segment_1",
        global_start_milliseconds=0,
        global_end_milliseconds=10_000,
        task_mode=TaskMode.T2VA,
        asset_ids=(),
    )
    with pytest.raises(PromptRenderingError, match="derived per clip"):
        render_production_segment_prompt(
            carried,
            (ProductionLocalShotV1("shot_1", 1, 0, "The fox waits."),),
            section_heading="integrated_multimodal_description",
            clip_duration_seconds=CLIP,
        )


def test_segment_renderer_requires_the_clip_duration() -> None:
    from comfyui_h3_context.core.errors import PromptRenderingError

    sliced = slice_production_semantics(
        ProductionSemanticAuthorityV1(),
        segment_id="segment_1",
        global_start_milliseconds=0,
        global_end_milliseconds=10_000,
        task_mode=TaskMode.T2VA,
        asset_ids=(),
    )
    with pytest.raises(PromptRenderingError, match="delivered duration"):
        render_production_segment_prompt(
            sliced,
            (ProductionLocalShotV1("shot_1", 1, 0, "The fox waits."),),
            section_heading="integrated_multimodal_description",
            clip_duration_seconds=Decimal(0),
        )


def test_segment_renderer_states_no_alignment_for_a_frame_the_clip_does_not_hold() -> None:
    authority = ProductionSemanticAuthorityV1(
        reference_definitions=(
            ReferenceDefinitionV1(
                "first", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1, "<Picture 1>"
            ),
        )
    )
    shot = ProductionLocalShotV1("shot_1", 1, 0, "The fox waits.")
    for asset_ids, opening in (
        ((), "integrated_multimodal_description: [Shot 1] The fox waits."),
        (
            ("first",),
            "For the target video, at 0.00 seconds into the target video, <Picture 1> "
            "(from [Shot 1]) is fully referenced.",
        ),
    ):
        sliced = slice_production_semantics(
            authority,
            segment_id="segment_1",
            global_start_milliseconds=0,
            global_end_milliseconds=10_000,
            task_mode=TaskMode.I2VA,
            asset_ids=asset_ids,
        )
        rendered = render_production_segment_prompt(
            sliced,
            (shot,),
            section_heading="integrated_multimodal_description",
            clip_duration_seconds=CLIP,
        )
        assert rendered.text.split("\n\n")[0] == opening
