"""Speaker ownership survives rendering, semantic parsing and assisted adoption."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from jsonschema import Draft202012Validator
from test_full_rendering import full_plan
from test_rendering import make_plan

from comfyui_h3_context.adapters.assisted_draft_execution import build_assisted_guarantee
from comfyui_h3_context.core import (
    ContextPlan,
    DialogueDelivery,
    DialogueSpeaker,
    ExactTextConstraint,
    ExactTextKind,
    HardConstraintSet,
    TaskMode,
    assign_speaker_ids,
    audit_prompt_fidelity,
    render_base_prompt,
    render_full_reference_prompt,
)
from comfyui_h3_context.core.assisted_draft import (
    AdoptionCondition,
    DraftCandidate,
    evaluate_repair_adoption,
)
from comfyui_h3_context.core.canonical_context_pipeline import compile_canonical_context_plan
from comfyui_h3_context.core.dialogue_speakers import render_dialogue_line
from comfyui_h3_context.core.errors import ContractValidationError, PromptRenderingError
from comfyui_h3_context.core.production_semantics import (
    ProductionLocalShotV1,
    ProductionSemanticSliceV1,
)
from comfyui_h3_context.core.prompt_fidelity import PLAN_READINESS_IDS
from comfyui_h3_context.core.rendering import (
    _assert_constraints_preserved,
    render_production_segment_prompt,
)

# GUARD: consume the aggregation surface; direct layer imports break cohort privacy.
from comfyui_h3_context.core.semantic_graph_comparator import (
    SemanticSourceKind,
    parse_semantic_prompt,
)
from comfyui_h3_context.nodes import H3HardConstraintProducerNode

ROOT = Path(__file__).resolve().parents[1]


def line(
    identifier: str,
    *,
    speaker: DialogueSpeaker | None = None,
    segment: str | None = None,
    kind: ExactTextKind = ExactTextKind.DIALOGUE,
    delivery: DialogueDelivery = DialogueDelivery.ON_SCREEN,
) -> ExactTextConstraint:
    return ExactTextConstraint(
        identifier,
        kind,
        "Keep  this!",
        "English",
        speakers=() if speaker is None else (speaker,),
        delivery=delivery,
        segment_id=segment,
    )


def with_constraints(plan: ContextPlan, constraints: HardConstraintSet) -> ContextPlan:
    return replace(
        plan,
        hard_constraints=constraints,
        request=replace(plan.request, hard_constraints=constraints),
    )


def test_closed_wire_adds_speaker_fields_without_breaking_legacy_constructor() -> None:
    value = ExactTextConstraint("line", ExactTextKind.DIALOGUE, "Hello", "en", "here")
    wire = value.to_wire()
    assert set(wire) == {
        "constraint_id",
        "kind",
        "text",
        "language",
        "location",
        "speakers",
        "delivery",
        "segment_id",
    }
    assert wire["speakers"] == [] and wire["delivery"] == "on_screen" and wire["segment_id"] is None
    schema = json.loads((ROOT / "governance/contracts/h3_context_v1.schema.json").read_text())
    validator = Draft202012Validator(
        {"$ref": "#/$defs/ExactTextConstraint", "$defs": schema["$defs"]}
    )
    validator.validate(wire)
    assert list(
        validator.iter_errors({key: item for key, item in wire.items() if key != "speakers"})
    )
    assert list(validator.iter_errors({**wire, "unexpected": "value"}))
    speaker = DialogueSpeaker("person", "subject_1", "the guide")
    validator.validate(line("bound", speaker=speaker, segment="segment_1").to_wire())
    visible = ExactTextConstraint("visible", ExactTextKind.VISIBLE_TEXT, "OPEN").to_wire()
    assert list(validator.iter_errors({**visible, "speakers": [speaker.to_wire()]}))


@pytest.mark.parametrize(
    "change",
    [
        {"speakers": (DialogueSpeaker("speaker"),)},
        {"delivery": DialogueDelivery.VOICEOVER},
        {"segment_id": "segment_1"},
    ],
)
def test_visible_text_cannot_carry_dialogue_metadata(change: dict[str, Any]) -> None:
    with pytest.raises(ContractValidationError):
        ExactTextConstraint("visible", ExactTextKind.VISIBLE_TEXT, "OPEN", **change)


def test_one_speaker_key_cannot_change_identity_or_subject() -> None:
    for conflicting in [
        DialogueSpeaker("key", identity="the guest"),
        DialogueSpeaker("key", "subject_1", "the guide"),
    ]:
        with pytest.raises(ContractValidationError, match="speaker_identity_conflict"):
            HardConstraintSet(
                (
                    line("a", speaker=DialogueSpeaker("key", identity="the guide")),
                    line("b", speaker=conflicting),
                )
            )
    with pytest.raises(ContractValidationError):
        DialogueSpeaker("key", identity="a" * 257)
    with pytest.raises(ContractValidationError, match="duplicate"):
        replace(line("a"), speakers=(DialogueSpeaker("key"), DialogueSpeaker("key")))


def test_first_appearance_follows_segments_then_unanchored_constraint_order() -> None:
    a, b, c = (DialogueSpeaker(key, identity=key) for key in ("a", "b", "c"))
    constraints = HardConstraintSet(
        (
            line("fallback", speaker=c),
            line("later", speaker=b, segment="segment_2"),
            line("first", speaker=a, segment="segment_1"),
            line("repeat", speaker=b, segment="segment_1"),
        )
    )
    plan = make_plan(TaskMode.T2VA, constraints=constraints, segment_count=2)
    assert dict(assign_speaker_ids(plan)) == {"a": 1, "b": 2, "c": 3}
    assert "subject_1" not in assign_speaker_ids(plan)
    document = render_base_prompt(plan).text
    assert document.index("a (S1)") < document.index("b (S2)") < document.index("c (S3)")
    assert document.count("b (S2)") == 2


@given(count=st.integers(min_value=1, max_value=8))
def test_unbound_lines_never_share_an_id_or_alias_an_explicit_key(count: int) -> None:
    values = tuple(line(f"line_{index}") for index in range(count)) + (
        line("explicit", speaker=DialogueSpeaker("anonymous.line_0", identity="the guide")),
    )
    plan = make_plan(TaskMode.T2VA, constraints=HardConstraintSet(values))
    ids = assign_speaker_ids(plan)
    assert len(ids) == count + 1
    assert tuple(ids.values()) == tuple(range(1, count + 2))


@pytest.mark.parametrize(
    ("speaker", "delivery", "kind", "expected"),
    [
        (
            DialogueSpeaker("a", "subject_1"),
            DialogueDelivery.ON_SCREEN,
            ExactTextKind.DIALOGUE,
            "the baker (S1) says: <d>[English] Keep  this!</d>",
        ),
        (
            DialogueSpeaker("a", "subject_1", "the narrator"),
            DialogueDelivery.VOICEOVER,
            ExactTextKind.DIALOGUE,
            "the narrator (S1) says in an off-screen voiceover: <d>[English] Keep  this!</d>"
            " while the baker's lips remain completely closed.",
        ),
        (
            DialogueSpeaker("a", identity="the narrator"),
            DialogueDelivery.VOICEOVER,
            ExactTextKind.DIALOGUE,
            "the narrator (S1) says in an off-screen voiceover: <d>[English] Keep  this!</d>"
            " while the speaker's lips remain completely closed.",
        ),
        (
            DialogueSpeaker("a", identity="the singer"),
            DialogueDelivery.ON_SCREEN,
            ExactTextKind.LYRICS,
            "the singer (S1) sings: <d>[English] Keep  this!</d>",
        ),
        (
            DialogueSpeaker("a", identity="the singer"),
            DialogueDelivery.VOICEOVER,
            ExactTextKind.LYRICS,
            "the singer (S1) sings in an off-screen voiceover: <d>[English] Keep  this!</d>"
            " while the speaker's lips remain completely closed.",
        ),
    ],
)
def test_exact_delivery_grammar_and_gender_free_possessive(
    speaker: DialogueSpeaker, delivery: DialogueDelivery, kind: ExactTextKind, expected: str
) -> None:
    value = line("line", speaker=speaker, delivery=delivery, kind=kind)
    plan = make_plan(TaskMode.T2VA, constraints=HardConstraintSet((value,)))
    assert render_dialogue_line(plan, value) == expected
    assert expected in render_base_prompt(plan).text
    assert (
        expected
        in render_full_reference_prompt(with_constraints(full_plan(), plan.hard_constraints)).text
    )


def test_compound_ids_and_identity_phrases_follow_ascending_ordinals() -> None:
    a, b = DialogueSpeaker("a", identity="the guide"), DialogueSpeaker("b", identity="the guest")
    first = line("first", speaker=a, segment="segment_1")
    compound = replace(line("both"), speakers=(b, a))
    plan = make_plan(TaskMode.T2VA, constraints=HardConstraintSet((first, compound)))
    assert (
        render_dialogue_line(plan, compound)
        == "the guide and the guest (S1,S2) says: <d>[English] Keep  this!</d>"
    )


def test_anchored_lines_follow_action_and_precede_audio_without_fallback_duplication() -> None:
    value = line("anchored", speaker=DialogueSpeaker("a", "subject_1"), segment="segment_1")
    plan = make_plan(TaskMode.T2VA, constraints=HardConstraintSet((value,)))
    text = render_base_prompt(plan).text
    exact = render_dialogue_line(plan, value)
    assert (
        text.index("opens the wooden shutters")
        < text.index(exact)
        < text.index("soft morning street ambience")
    )
    assert text.count(exact) == 1


def test_binding_diagnostics_are_derived_again_when_a_plan_is_replaced() -> None:
    value = line("bad", speaker=DialogueSpeaker("a", "missing"), segment="missing_segment")
    plan = make_plan(TaskMode.T2VA, constraints=HardConstraintSet((value,)))
    assert {d.code for d in plan.diagnostics} == {
        "unknown_speaker_subject",
        "unknown_dialogue_segment",
    }
    fixed = replace(value, speakers=(DialogueSpeaker("a", "subject_1"),), segment_id="segment_1")
    assert with_constraints(plan, HardConstraintSet((fixed,))).diagnostics == ()


def test_unbound_speaker_is_readiness_and_authored_prose_stays_warning() -> None:
    plan = make_plan(TaskMode.T2VA, constraints=HardConstraintSet((line("line"),)))
    document = render_base_prompt(plan)
    audit = audit_prompt_fidelity(plan, document)
    finding = next(
        d for d in audit.diagnostics if d.diagnostic_id.value == "fidelity.dialogue.speaker_unbound"
    )
    assert finding.diagnostic_id in PLAN_READINESS_IDS and finding in audit.readiness_findings
    assert finding not in audit.prose_blocking and finding.severity.value == "warning"
    authored = replace(document, text="<d>[English] Keep  this!</d>")
    speaker_warning = next(
        d
        for d in audit_prompt_fidelity(plan, authored).diagnostics
        if d.diagnostic_id.value == "fidelity.dialogue.speaker_identity_missing"
    )
    assert speaker_warning.severity.value == "warning"


def test_canvas_speaker_delivery_and_legacy_optional_widget_order() -> None:
    shape = H3HardConstraintProducerNode.INPUT_TYPES()
    assert list(shape["optional"]) == ["dialogue_language", "dialogue_speaker", "dialogue_delivery"]
    constraints, _ = H3HardConstraintProducerNode().produce(
        dialogue="Hello",
        dialogue_language="English",
        dialogue_speaker="the guide",
        dialogue_delivery="voiceover",
    )
    assert constraints.exact_texts[0].speakers == (
        DialogueSpeaker("manual.speaker.1", identity="the guide"),
    )
    assert constraints.exact_texts[0].delivery is DialogueDelivery.VOICEOVER


def test_render_corpus_base_full_and_whole_video_has_no_dialogue_findings() -> None:
    constraints = HardConstraintSet(
        (line("line", speaker=DialogueSpeaker("speaker", identity="the guide")),)
    )
    base = make_plan(TaskMode.T2VA, constraints=constraints)
    full = with_constraints(full_plan(), constraints)
    rendered = render_base_prompt(base)
    exact = render_dialogue_line(base, constraints.exact_texts[0])
    semantic_slice = ProductionSemanticSliceV1(
        "segment_1", 0, 5167, TaskMode.T2VA, (), (), (), (), (), ()
    )
    production = render_production_segment_prompt(
        semantic_slice,
        (ProductionLocalShotV1("shot_1", 1, 0, "The guide waits.", (exact,)),),
        section_heading="integrated_multimodal_description",
        clip_duration_seconds=Decimal("5.167"),
    )
    for plan, document in [
        (base, rendered),
        (full, render_full_reference_prompt(full)),
        (base, replace(rendered, text=production.text)),
    ]:
        assert not [
            d
            for d in audit_prompt_fidelity(plan, document).diagnostics
            if d.diagnostic_id.value.startswith("fidelity.dialogue.")
        ]


def test_semantic_parser_tracks_any_dialogue_block_and_compound_lyric_identity() -> None:
    plan = make_plan(TaskMode.T2VA)
    document = render_base_prompt(plan)
    text = document.text + "\nThe pair (S1,S2) sings: <d>[English] Keep  this!</d>"
    graph = parse_semantic_prompt(
        text, plan.request.profile, TaskMode.T2VA, SemanticSourceKind.LOCAL, "local.dialogue"
    )
    node = next(n for n in graph.nodes if n.value == "Keep  this!")
    assert dict(node.attributes) == {"exact_kind": "lyrics", "speaker_ids": "S1,S2"}


@pytest.mark.parametrize("mutation", ["drop_id", "move_id", "drop_tag", "move_tag"])
def test_full_span_guarantee_refuses_dropped_or_detached_metadata(mutation: str) -> None:
    plan = make_plan(
        TaskMode.T2VA,
        constraints=HardConstraintSet(
            (line("line", speaker=DialogueSpeaker("a", identity="the guide")),)
        ),
    )
    _, report, original = compile_canonical_context_plan(plan)
    guarantee = build_assisted_guarantee(report)
    span = render_dialogue_line(plan, plan.hard_constraints.exact_texts[0])
    assert guarantee.user_dialogue == (span,)
    token = "(S1)" if mutation.endswith("id") else "[English]"
    text = original.text.replace(token, "", 1)
    if mutation.startswith("move"):
        text += "\n" + token
    with pytest.raises(PromptRenderingError):
        _assert_constraints_preserved(plan, text)
    adoption = evaluate_repair_adoption(
        guarantee=guarantee,
        original_text=original.text,
        repaired=DraftCandidate(text),
        reaudit=audit_prompt_fidelity(plan, replace(original, text=text)),
    )
    assert not adoption.adopted
    assert adoption.failed_condition in {
        AdoptionCondition.USER_TEXT_PRESERVED,
        AdoptionCondition.REAUDIT_PASSED,
    }
