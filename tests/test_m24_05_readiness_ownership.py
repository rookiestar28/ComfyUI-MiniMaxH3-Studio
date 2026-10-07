"""M24-05 readiness is established only by explicit typed ownership."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from comfyui_h3_context.core.constraints import (
    ContentScope,
    DirectiveTarget,
    ExactTextConstraint,
    ExactTextKind,
    ForbiddenContent,
    HardConstraintSet,
    KeepChangeAction,
    KeepChangeDirective,
    RequiredContent,
    TimePoint,
)
from comfyui_h3_context.core.context_reporting import (
    ContextPlan,
    PlanStage,
    PlanStep,
    PlanStepStatus,
    PromptDocument,
    PromptRenderStatus,
    PromptSection,
)
from comfyui_h3_context.core.contracts import AssetRole, MediaKind, TaskMode
from comfyui_h3_context.core.errors import PromptRenderingError
from comfyui_h3_context.core.guide_conformance import GuideReadiness, evaluate_guide_conformance
from comfyui_h3_context.core.intent_graph import (
    AudioIntent,
    AudioLayer,
    AudioRetentionMarker,
    AudioScope,
    CameraIntent,
    IntentAction,
    IntentScene,
    IntentSubject,
    RetentionDomain,
    RetentionRelation,
    RetentionScope,
    SegmentDevelopment,
    SoundscapeDisposition,
    TimelineSegment,
    build_intent_graph,
)
from comfyui_h3_context.core.normalization import RawContextRequest, normalize_request
from comfyui_h3_context.core.prompt_fidelity import (
    PromptFidelityDiagnosticId,
    audit_prompt_fidelity,
)
from comfyui_h3_context.core.registry import MediaMetadata, ReferenceAsset, build_reference_registry
from comfyui_h3_context.core.rendering import render_full_reference_prompt


def _plan(
    mode: TaskMode,
    assets: tuple[ReferenceAsset, ...],
    *,
    subjects: tuple[IntentSubject, ...],
    actions: tuple[IntentAction, ...],
    cameras: tuple[CameraIntent, ...] = (),
    audios: tuple[AudioIntent, ...] = (),
    retention: tuple[RetentionRelation, ...] = (),
    segments: tuple[TimelineSegment, ...],
    constraints: HardConstraintSet | None = None,
) -> ContextPlan:
    registry = build_reference_registry(assets)
    constraints = HardConstraintSet() if constraints is None else constraints
    normalized = normalize_request(
        RawContextRequest(
            mode,
            "A caller-authored timeline.",
            duration_seconds=4.0,
            reference_registry=registry,
            hard_constraints=constraints,
        )
    )
    assert normalized.request is not None, normalized.diagnostics
    request = normalized.request
    effective = Decimal(str(request.effective_duration_seconds))
    segments = tuple(
        replace(
            segment,
            start=TimePoint.from_text(format(segment.start.seconds * effective / Decimal(4), "f")),
            end=TimePoint.from_text(format(segment.end.seconds * effective / Decimal(4), "f")),
        )
        for segment in segments
    )
    graph = build_intent_graph(
        effective_duration=TimePoint.from_text(format(effective, "f")),
        registry=registry,
        subjects=subjects,
        scenes=(IntentScene("scene", "a declared place", tuple(s.subject_id for s in subjects)),),
        actions=actions,
        cameras=cameras,
        audios=audios,
        retention=retention,
        segments=segments,
        soundscape=SoundscapeDisposition.EXPLICIT_COMPLETE_SILENCE
        if not audios
        else SoundscapeDisposition.UNSPECIFIED,
    )
    assert graph.graph is not None, graph.diagnostics
    return ContextPlan(
        "readiness_plan",
        request.schema_version,
        request,
        graph.graph,
        request.hard_constraints,
        request.evidence,
        (PlanStep("normalize", PlanStage.NORMALIZE, PlanStepStatus.COMPLETED, "normalized"),),
    )


def _document(plan: ContextPlan) -> PromptDocument:
    text = (
        "integrated_multimodal_description: [Shot 1] caller-authored content.\n\n"
        "overall_soundscape: N/A\n\nnon_diegetic_music: N/A"
    )
    return PromptDocument(
        "readiness_document",
        plan.request.schema_version,
        plan.request.profile,
        plan.request.task_mode,
        plan.plan_id,
        text,
        (PromptSection("description", 1, "description", text),),
        PromptRenderStatus.RENDERED,
    )


def _ids(plan: ContextPlan) -> list[PromptFidelityDiagnosticId]:
    return [item.diagnostic_id for item in audit_prompt_fidelity(plan, _document(plan)).diagnostics]


def _frame_assets(mode: TaskMode) -> tuple[ReferenceAsset, ...]:
    values: list[ReferenceAsset] = []
    if mode in {TaskMode.I2VA, TaskMode.FL2VA}:
        values.append(
            ReferenceAsset("first", MediaKind.IMAGE, AssetRole.FIRST_FRAME, len(values) + 1)
        )
    if mode in {TaskMode.FL2VA, TaskMode.L2VA}:
        values.append(
            ReferenceAsset("last", MediaKind.IMAGE, AssetRole.LAST_FRAME, len(values) + 1)
        )
    return tuple(values)


def test_empty_later_shot_is_reported_by_exact_segment_id() -> None:
    subject = IntentSubject("anchor", "anchor")
    plan = _plan(
        TaskMode.T2VA,
        (),
        subjects=(subject,),
        actions=(),
        segments=(
            TimelineSegment(
                "opening", TimePoint.from_text("0"), TimePoint.from_text("2"), "scene", ("anchor",)
            ),
            TimelineSegment("empty_later", TimePoint.from_text("2"), TimePoint.from_text("4")),
        ),
    )
    findings = [
        item
        for item in audit_prompt_fidelity(plan, _document(plan)).diagnostics
        if item.diagnostic_id is PromptFidelityDiagnosticId.DESCRIPTION_SEMANTIC_EMPTY
    ]
    assert [item.parameter_map for item in findings] == [{"segment_id": "empty_later"}]


def test_keyframe_path_and_development_require_anchor_owned_joins() -> None:
    anchor = IntentSubject("anchor", "anchor", ("first",))
    environment = IntentSubject("environment", "environment")
    unrelated = IntentAction("environment_moves", "clouds pass", ("environment",), "scene")
    plan = _plan(
        TaskMode.I2VA,
        _frame_assets(TaskMode.I2VA),
        subjects=(anchor, environment),
        actions=(unrelated,),
        segments=(
            TimelineSegment(
                "opening",
                TimePoint.from_text("0"),
                TimePoint.from_text("2"),
                "scene",
                ("anchor",),
                ("environment_moves",),
                development=SegmentDevelopment.ANCHOR_DEVELOPMENT,
            ),
            TimelineSegment(
                "unowned_path",
                TimePoint.from_text("2"),
                TimePoint.from_text("4"),
                "scene",
                ("environment",),
                ("environment_moves",),
                development=SegmentDevelopment.ANCHOR_DEVELOPMENT,
            ),
        ),
    )
    findings = audit_prompt_fidelity(plan, _document(plan)).diagnostics
    assert {
        (item.diagnostic_id, item.parameter_map.get("segment_id"))
        for item in findings
        if item.diagnostic_id
        in {
            PromptFidelityDiagnosticId.KEYFRAME_DEVELOPMENT_MISSING,
            PromptFidelityDiagnosticId.KEYFRAME_PATH_UNOWNED,
        }
    } == {
        (PromptFidelityDiagnosticId.KEYFRAME_DEVELOPMENT_MISSING, "opening"),
        (PromptFidelityDiagnosticId.KEYFRAME_PATH_UNOWNED, "unowned_path"),
    }


def test_anchor_camera_development_and_disjoint_action_static_hold_are_ready() -> None:
    anchor = IntentSubject("anchor", "anchor", ("first",))
    environment = IntentSubject("environment", "environment")
    action = IntentAction("clouds", "clouds pass", ("environment",), "scene")
    camera = CameraIntent("follow", "camera follows anchor", "scene", ("anchor",))
    plan = _plan(
        TaskMode.I2VA,
        _frame_assets(TaskMode.I2VA),
        subjects=(anchor, environment),
        actions=(action,),
        cameras=(camera,),
        segments=(
            TimelineSegment(
                "develop",
                TimePoint.from_text("0"),
                TimePoint.from_text("2"),
                "scene",
                ("anchor",),
                camera_id="follow",
                development=SegmentDevelopment.ANCHOR_DEVELOPMENT,
            ),
            TimelineSegment(
                "hold",
                TimePoint.from_text("2"),
                TimePoint.from_text("4"),
                "scene",
                ("anchor", "environment"),
                ("clouds",),
                development=SegmentDevelopment.ANCHOR_STATIC_HOLD,
            ),
        ),
    )
    blocked = {
        PromptFidelityDiagnosticId.KEYFRAME_ANCHOR_MISSING,
        PromptFidelityDiagnosticId.KEYFRAME_DEVELOPMENT_MISSING,
        PromptFidelityDiagnosticId.KEYFRAME_PATH_UNOWNED,
        PromptFidelityDiagnosticId.KEYFRAME_STATIC_HOLD_CONTRADICTED,
    }
    assert blocked.isdisjoint(_ids(plan))


def test_anchor_owned_action_contradicts_static_hold() -> None:
    anchor = IntentSubject("anchor", "anchor", ("last",))
    action = IntentAction("anchor_moves", "anchor moves", ("anchor",), "scene")
    plan = _plan(
        TaskMode.L2VA,
        _frame_assets(TaskMode.L2VA),
        subjects=(anchor,),
        actions=(action,),
        segments=(
            TimelineSegment(
                "hold",
                TimePoint.from_text("0"),
                TimePoint.from_text("4"),
                "scene",
                ("anchor",),
                ("anchor_moves",),
                development=SegmentDevelopment.ANCHOR_STATIC_HOLD,
            ),
        ),
    )
    assert PromptFidelityDiagnosticId.KEYFRAME_STATIC_HOLD_CONTRADICTED in _ids(plan)


def test_fl2va_requires_both_declared_endpoint_ownerships() -> None:
    first_only = IntentSubject("anchor", "anchor", ("first",))
    both = replace(first_only, source_asset_ids=("first", "last"))

    def make(subject: IntentSubject) -> ContextPlan:
        return _plan(
            TaskMode.FL2VA,
            _frame_assets(TaskMode.FL2VA),
            subjects=(subject,),
            actions=(),
            segments=(
                TimelineSegment(
                    "shot",
                    TimePoint.from_text("0"),
                    TimePoint.from_text("4"),
                    "scene",
                    ("anchor",),
                    development=SegmentDevelopment.ANCHOR_STATIC_HOLD,
                ),
            ),
        )

    first_plan = make(first_only)
    missing = [
        item
        for item in audit_prompt_fidelity(first_plan, _document(first_plan)).diagnostics
        if item.diagnostic_id is PromptFidelityDiagnosticId.KEYFRAME_ANCHOR_MISSING
    ]
    assert missing[0].parameter_map["roles"] == "last_frame"
    assert PromptFidelityDiagnosticId.KEYFRAME_ANCHOR_MISSING not in _ids(make(both))


def _audio_plan(
    *, duplicate: bool = False, timeline_gap: bool = False, source_duration: str | None = None
) -> ContextPlan:
    asset = ReferenceAsset(
        "source_audio",
        MediaKind.AUDIO,
        AssetRole.AUDIO_SOURCE,
        1,
        MediaMetadata(
            duration_seconds=None if source_duration is None else Decimal(source_duration)
        ),
    )
    carrier = AudioIntent(
        "carrier",
        AudioLayer.NON_DIEGETIC,
        "retained track",
        ("source_audio",),
        scope=AudioScope.TIMELINE,
    )
    extra = AudioIntent(
        "duplicate",
        AudioLayer.NON_DIEGETIC,
        "second carrier",
        ("source_audio",),
        scope=AudioScope.WHOLE_VIDEO,
    )
    relation = RetentionRelation(
        "final",
        RetentionDomain.AUDIO,
        ("source_audio",),
        "carrier",
        AudioRetentionMarker.FULLY_COPY,
        scope=RetentionScope.COMPLETE_FINAL_AUDIO_TRACK,
    )
    second_ids = () if timeline_gap else ("carrier",)
    return _plan(
        TaskMode.REF2VA,
        (asset,),
        subjects=(IntentSubject("subject", "subject"),),
        actions=(),
        audios=(carrier, extra) if duplicate else (carrier,),
        retention=(relation,),
        segments=(
            TimelineSegment(
                "one",
                TimePoint.from_text("0"),
                TimePoint.from_text("2"),
                "scene",
                ("subject",),
                audio_ids=("carrier",),
            ),
            TimelineSegment(
                "two",
                TimePoint.from_text("2"),
                TimePoint.from_text("4"),
                "scene",
                ("subject",),
                audio_ids=second_ids,
            ),
        ),
    )


def test_complete_final_track_requires_one_carrier_and_complete_coverage() -> None:
    assert PromptFidelityDiagnosticId.AUDIO_FINAL_TRACK_CONTRADICTED not in _ids(_audio_plan())
    assert PromptFidelityDiagnosticId.AUDIO_FINAL_TRACK_CONTRADICTED in _ids(
        _audio_plan(duplicate=True)
    )
    assert PromptFidelityDiagnosticId.AUDIO_FINAL_TRACK_CONTRADICTED in _ids(
        _audio_plan(timeline_gap=True)
    )
    assert PromptFidelityDiagnosticId.AUDIO_FINAL_TRACK_CONTRADICTED in _ids(
        _audio_plan(source_duration="3")
    )


def test_complete_final_track_rejects_a_second_audio_retention_relation() -> None:
    plan = _audio_plan()
    second = RetentionRelation(
        "layer",
        RetentionDomain.AUDIO,
        ("source_audio",),
        "carrier",
        AudioRetentionMarker.PARTIALLY_COPY,
        scope=RetentionScope.AUDIO_LAYER,
    )
    plan = replace(
        plan,
        intent_graph=replace(plan.intent_graph, retention=plan.intent_graph.retention + (second,)),
    )
    assert PromptFidelityDiagnosticId.AUDIO_FINAL_TRACK_CONTRADICTED in _ids(plan)


def test_unspecified_retention_scope_never_authorizes_readiness() -> None:
    asset = ReferenceAsset("source_audio", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE, 1)
    audio = AudioIntent(
        "carrier", AudioLayer.NON_DIEGETIC, "track", ("source_audio",), scope=AudioScope.WHOLE_VIDEO
    )
    relation = RetentionRelation(
        "legacy",
        RetentionDomain.AUDIO,
        ("source_audio",),
        "carrier",
        AudioRetentionMarker.FULLY_COPY,
    )
    plan = _plan(
        TaskMode.REF2VA,
        (asset,),
        subjects=(IntentSubject("subject", "subject"),),
        actions=(),
        audios=(audio,),
        retention=(relation,),
        segments=(
            TimelineSegment(
                "shot", TimePoint.from_text("0"), TimePoint.from_text("4"), "scene", ("subject",)
            ),
        ),
    )
    assert PromptFidelityDiagnosticId.RETENTION_SCOPE_UNSPECIFIED in _ids(plan)
    assert evaluate_guide_conformance(plan, _document(plan)).readiness is GuideReadiness.INCOMPLETE


def test_official_english_word_advisory_skips_editing_cjk_and_exact_dialogue() -> None:
    base = _plan(
        TaskMode.T2VA,
        (),
        subjects=(IntentSubject("subject", "subject"),),
        actions=(),
        segments=(
            TimelineSegment(
                "shot", TimePoint.from_text("0"), TimePoint.from_text("4"), "scene", ("subject",)
            ),
        ),
    )
    english = replace(
        _document(base),
        text="integrated_multimodal_description: "
        + "word " * 349
        + "\n\noverall_soundscape: N/A\n\nnon_diegetic_music: N/A",
    )
    finding = next(
        item
        for item in audit_prompt_fidelity(base, english).diagnostics
        if item.diagnostic_id is PromptFidelityDiagnosticId.DESCRIPTION_LENGTH_BAND
    )
    assert finding.parameter_map["words"] == 349
    assert finding.evidence_level.value == "official"

    cjk = replace(
        english,
        text=(
            "integrated_multimodal_description: 一段明確的中文描述。\n\n"
            "overall_soundscape: N/A\n\nnon_diegetic_music: N/A"
        ),
    )
    assert PromptFidelityDiagnosticId.DESCRIPTION_LENGTH_BAND not in {
        item.diagnostic_id for item in audit_prompt_fidelity(base, cjk).diagnostics
    }

    dialogue_constraints = HardConstraintSet(
        (ExactTextConstraint("dialogue", ExactTextKind.DIALOGUE, "Exact spoken line."),)
    )
    exact_dialogue = _plan(
        TaskMode.T2VA,
        (),
        subjects=(IntentSubject("subject", "subject"),),
        actions=(),
        constraints=dialogue_constraints,
        segments=(
            TimelineSegment(
                "shot", TimePoint.from_text("0"), TimePoint.from_text("4"), "scene", ("subject",)
            ),
        ),
    )
    dialogue_document = replace(_document(exact_dialogue), text=english.text)
    assert PromptFidelityDiagnosticId.DESCRIPTION_LENGTH_BAND not in {
        item.diagnostic_id
        for item in audit_prompt_fidelity(exact_dialogue, dialogue_document).diagnostics
    }

    editing_asset = ReferenceAsset("source_video", MediaKind.VIDEO, AssetRole.EDITING_SOURCE, 1)
    editing = _plan(
        TaskMode.REF2VA,
        (editing_asset,),
        subjects=(IntentSubject("subject", "subject"),),
        actions=(),
        segments=(
            TimelineSegment(
                "shot", TimePoint.from_text("0"), TimePoint.from_text("4"), "scene", ("subject",)
            ),
        ),
    )
    editing_document = replace(_document(editing), text=english.text)
    assert PromptFidelityDiagnosticId.DESCRIPTION_LENGTH_BAND not in {
        item.diagnostic_id for item in audit_prompt_fidelity(editing, editing_document).diagnostics
    }


def test_v2_fidelity_and_guide_wires_validate_the_new_identity_set() -> None:
    plan = _audio_plan(duplicate=True)
    audit = audit_prompt_fidelity(plan, _document(plan))
    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (root / "governance/contracts/prompt_fidelity_v2.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(audit.to_wire())
    conformance = evaluate_guide_conformance(plan, _document(plan))
    assert audit.to_wire()["schema"] == "h3.context.prompt_fidelity.v2"
    assert conformance.to_wire()["schema"] == "h3.context.guide_conformance.v2"


def _ready_final_track_plan(*, whole_video: bool = False) -> ContextPlan:
    plan = _audio_plan()
    carrier = replace(
        plan.intent_graph.audios[0],
        layer=AudioLayer.AMBIENCE,
        scope=AudioScope.WHOLE_VIDEO if whole_video else AudioScope.TIMELINE,
    )
    return replace(
        plan,
        intent_graph=replace(
            plan.intent_graph, audios=(carrier,), soundscape=SoundscapeDisposition.DESCRIBED
        ),
    )


def _with_audio_change(plan: ContextPlan, target: DirectiveTarget) -> ContextPlan:
    constraints = HardConstraintSet(
        (KeepChangeDirective("replace_audio", KeepChangeAction.CHANGE, target, "old", "new"),)
    )
    return replace(
        plan,
        hard_constraints=constraints,
        request=replace(plan.request, hard_constraints=constraints),
    )


@pytest.mark.parametrize("defect", ["timeline_gap", "source_duration", "wrong_target"])
def test_renderer_refuses_incomplete_final_track_declarations(defect: str) -> None:
    if defect == "timeline_gap":
        plan = _audio_plan(timeline_gap=True)
    elif defect == "source_duration":
        plan = _audio_plan(source_duration="3")
    else:
        plan = _audio_plan()
        unused = replace(plan.intent_graph.audios[0], audio_id="unused")
        plan = replace(
            plan,
            intent_graph=replace(
                plan.intent_graph,
                audios=(*plan.intent_graph.audios, unused),
                retention=(replace(plan.intent_graph.retention[0], target_id="unused"),),
            ),
        )
    assert PromptFidelityDiagnosticId.AUDIO_FINAL_TRACK_CONTRADICTED in _ids(plan)
    with pytest.raises(PromptRenderingError, match="complete final-track"):
        render_full_reference_prompt(plan)


@pytest.mark.parametrize(
    "target", [DirectiveTarget.AUDIO, DirectiveTarget.DIALOGUE, DirectiveTarget.LYRICS]
)
def test_audio_content_changes_cannot_keep_complete_track_ready(target: DirectiveTarget) -> None:
    valid = _ready_final_track_plan()
    document = render_full_reference_prompt(valid)
    assert evaluate_guide_conformance(valid, document).readiness is GuideReadiness.READY
    changed = _with_audio_change(valid, target)
    # A previously rendered declaration must lose authority when its typed hard facts change.
    assert PromptFidelityDiagnosticId.AUDIO_FINAL_TRACK_CONTRADICTED in {
        row.diagnostic_id for row in audit_prompt_fidelity(changed, document).diagnostics
    }
    assert evaluate_guide_conformance(changed, document).readiness is not GuideReadiness.READY


@pytest.mark.parametrize(
    "target", [DirectiveTarget.AUDIO, DirectiveTarget.DIALOGUE, DirectiveTarget.LYRICS]
)
def test_renderer_refuses_complete_track_with_audio_content_change(target: DirectiveTarget) -> None:
    changed = _with_audio_change(_ready_final_track_plan(), target)
    with pytest.raises(PromptRenderingError, match="complete final-track"):
        render_full_reference_prompt(changed)


def test_complete_track_accepts_known_matching_source_duration() -> None:
    duration = _audio_plan().intent_graph.effective_duration.seconds
    plan = _audio_plan(source_duration=format(duration, "f"))
    document = render_full_reference_prompt(plan)
    assert "<Audio 1>: fully_copy" in document.text
    assert PromptFidelityDiagnosticId.AUDIO_FINAL_TRACK_CONTRADICTED not in {
        row.diagnostic_id for row in audit_prompt_fidelity(plan, document).diagnostics
    }


@pytest.mark.parametrize("whole_video", [False, True])
@pytest.mark.parametrize(
    "constraints",
    [
        HardConstraintSet(),
        HardConstraintSet(
            (
                KeepChangeDirective(
                    "visual", KeepChangeAction.CHANGE, DirectiveTarget.CAMERA, "wide", "close"
                ),
            )
        ),
        HardConstraintSet(
            (
                KeepChangeDirective(
                    "keep", KeepChangeAction.KEEP, DirectiveTarget.AUDIO, "source soundtrack"
                ),
            )
        ),
        HardConstraintSet((RequiredContent("required", ContentScope.AUDIO, "a sound"),)),
        HardConstraintSet((ForbiddenContent("forbidden", ContentScope.AUDIO, "a sound"),)),
    ],
)
def test_compatible_complete_track_remains_renderable_and_ready(
    whole_video: bool, constraints: HardConstraintSet
) -> None:
    plan = _ready_final_track_plan(whole_video=whole_video)
    plan = replace(
        plan,
        hard_constraints=constraints,
        request=replace(plan.request, hard_constraints=constraints),
    )
    document = render_full_reference_prompt(plan)
    assert "<Audio 1>: fully_copy" in document.text
    assert "complete final audio track" in document.text
    assert PromptFidelityDiagnosticId.AUDIO_FINAL_TRACK_CONTRADICTED not in {
        row.diagnostic_id for row in audit_prompt_fidelity(plan, document).diagnostics
    }
    assert evaluate_guide_conformance(plan, document).readiness is GuideReadiness.READY
