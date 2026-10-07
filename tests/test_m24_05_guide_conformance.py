"""M24-05 official H3 prompt-guide semantic conformance tests.

The pinned authority is MiniMax-AI/MiniMax-H3 at `d21241f0a4b3acbb34c97dae47fa417b7065e438`,
canonical `skills/h3-prompt-writing/`. These tests encode the guide rules the repository actually
checks. They never claim equivalence with the private hosted H3-Context-IR workflow, and they
assert no prose document's content.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal

from test_full_rendering import full_plan

from comfyui_h3_context.core import (
    CURRENT_SCHEMA_VERSION,
    OFFICIAL_H3_BASE_GUIDE_DIGEST,
    OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST,
    OFFICIAL_H3_GUIDE_REVISION,
    UNSPECIFIED_SOUNDSCAPE_TEXT,
    AssetRole,
    AudioIntent,
    AudioLayer,
    AudioOwnership,
    AudioRetentionMarker,
    AudioScope,
    CameraIntent,
    ContentScope,
    ContextPlan,
    DialogueSpeaker,
    ExactTextConstraint,
    ExactTextKind,
    ForbiddenContent,
    GuideConformanceResult,
    GuideReadiness,
    HardConstraintSet,
    IntentAction,
    IntentGraphError,
    IntentScene,
    IntentSubject,
    MediaKind,
    MediaMetadata,
    PlanStage,
    PlanStep,
    PlanStepStatus,
    ProfileIdentity,
    PromptFidelityDiagnosticId,
    PromptLintError,
    PromptProfile,
    RawContextRequest,
    ReferenceAsset,
    ReferenceRegistry,
    RequiredContent,
    RetentionDomain,
    RetentionRelation,
    SegmentDevelopment,
    SoundscapeDisposition,
    SourceProfileBinding,
    StyleIntent,
    TaskMode,
    TimelineSegment,
    TimePoint,
    audit_prompt_fidelity,
    build_intent_graph,
    build_official_source_profile_binding,
    build_reference_registry,
    evaluate_guide_conformance,
    normalize_request,
    readiness_from_audit,
    render_base_prompt,
    render_full_reference_prompt,
    render_profiled_prompt,
    validate_profiled_prompt,
)
from comfyui_h3_context.core.prompt_fidelity import PromptFidelityAuditResult

INTENT = "A baker opens the bakery before sunrise."


def frame_registry(mode: TaskMode) -> ReferenceRegistry:
    assets: list[ReferenceAsset] = []
    if mode in {TaskMode.I2VA, TaskMode.FL2VA}:
        assets.append(
            ReferenceAsset(
                "first_frame",
                MediaKind.IMAGE,
                AssetRole.FIRST_FRAME,
                len(assets) + 1,
                MediaMetadata(width=640, height=480),
            )
        )
    if mode in {TaskMode.FL2VA, TaskMode.L2VA}:
        assets.append(
            ReferenceAsset(
                "last_frame",
                MediaKind.IMAGE,
                AssetRole.LAST_FRAME,
                len(assets) + 1,
                MediaMetadata(width=640, height=480),
            )
        )
    return build_reference_registry(assets)


def make_plan(
    mode: TaskMode = TaskMode.T2VA,
    *,
    registry: ReferenceRegistry | None = None,
    constraints: HardConstraintSet | None = None,
    audios: tuple[AudioIntent, ...] | None = None,
    segment_audio_ids: tuple[str, ...] | None = None,
    soundscape: SoundscapeDisposition = SoundscapeDisposition.UNSPECIFIED,
    semantic: bool = True,
    segment_count: int = 1,
) -> ContextPlan:
    """Build one plan. `semantic=False` reproduces the App Mode manual skeleton."""

    if registry is None:
        registry = ReferenceRegistry.empty()
    if constraints is None:
        constraints = HardConstraintSet()
    if audios is None:
        audios = (AudioIntent("audio_1", AudioLayer.AMBIENCE, "soft morning street ambience"),)
    if segment_audio_ids is None:
        segment_audio_ids = tuple(audio.audio_id for audio in audios)
    result = normalize_request(
        RawContextRequest(
            mode,
            INTENT,
            duration_seconds=5.167,
            reference_registry=registry,
            hard_constraints=constraints,
        )
    )
    assert result.request is not None, result.diagnostics
    request = result.request
    duration = TimePoint.from_text(str(request.effective_duration_seconds))
    segment_duration = duration.seconds / segment_count

    def time_text(value: Decimal) -> str:
        return "0" if value == 0 else format(value, "f")

    subjects: tuple[IntentSubject, ...] = ()
    scenes: tuple[IntentScene, ...] = ()
    actions: tuple[IntentAction, ...] = ()
    cameras: tuple[CameraIntent, ...] = ()
    styles: tuple[StyleIntent, ...] = ()
    if semantic:
        subjects = (IntentSubject("subject_1", "the baker", description="in a flour-dusted apron"),)
        scenes = (IntentScene("scene_1", "a quiet street bakery", ("subject_1",), "style_1"),)
        actions = (
            IntentAction("action_1", "opens the wooden shutters", ("subject_1",), "scene_1"),
        )
        cameras = (CameraIntent("camera_1", "a slow push in", "scene_1", ("subject_1",)),)
        styles = (StyleIntent("style_1", "cinematic live-action"),)
        segments = tuple(
            TimelineSegment(
                f"segment_{index + 1}",
                TimePoint.from_text(time_text(segment_duration * index)),
                TimePoint.from_text(time_text(segment_duration * (index + 1))),
                "scene_1",
                ("subject_1",),
                ("action_1",),
                "camera_1",
                "style_1",
                segment_audio_ids,
                (),
            )
            for index in range(segment_count)
        )
    else:
        audios = ()
        segments = (TimelineSegment("segment_1", TimePoint.from_text("0"), duration),)
    graph_result = build_intent_graph(
        effective_duration=duration,
        registry=registry,
        subjects=subjects,
        scenes=scenes,
        actions=actions,
        cameras=cameras,
        styles=styles,
        audios=audios,
        segments=segments,
        soundscape=soundscape,
    )
    assert graph_result.graph is not None, graph_result.diagnostics
    return ContextPlan(
        "plan_m24_05",
        request.schema_version,
        request,
        graph_result.graph,
        request.hard_constraints,
        request.evidence,
        (
            PlanStep("step_1", PlanStage.NORMALIZE, PlanStepStatus.COMPLETED, "normalize"),
            PlanStep("step_2", PlanStage.BIND_REFERENCES, PlanStepStatus.COMPLETED, "bind"),
            PlanStep("step_3", PlanStage.ASSEMBLE_INTENT, PlanStepStatus.COMPLETED, "assemble"),
        ),
    )


IMAGE_METADATA = MediaMetadata(width=640, height=480)


def ref2va_plan(
    assets: tuple[ReferenceAsset, ...],
    *,
    audios: tuple[AudioIntent, ...] = (),
    retention: tuple[RetentionRelation, ...] = (),
) -> ContextPlan:
    """One minimal Full-Reference plan whose only variable is which references it was given."""

    registry = build_reference_registry(assets)
    result = normalize_request(
        RawContextRequest(
            TaskMode.REF2VA,
            INTENT,
            duration_seconds=4.0,
            reference_registry=registry,
        )
    )
    assert result.request is not None, result.diagnostics
    request = result.request
    duration = TimePoint.from_text(str(request.effective_duration_seconds))
    graph_result = build_intent_graph(
        effective_duration=duration,
        registry=registry,
        subjects=(IntentSubject("subject_1", "the baker"),),
        scenes=(IntentScene("scene_1", "a quiet street bakery", ("subject_1",), "style_1"),),
        actions=(IntentAction("action_1", "opens the wooden shutters", ("subject_1",), "scene_1"),),
        cameras=(CameraIntent("camera_1", "a slow push in", "scene_1", ("subject_1",)),),
        styles=(StyleIntent("style_1", "cinematic live-action"),),
        audios=audios,
        retention=retention,
        segments=(
            TimelineSegment(
                "segment_1",
                TimePoint.from_text("0"),
                duration,
                "scene_1",
                ("subject_1",),
                ("action_1",),
                "camera_1",
                "style_1",
                tuple(item.audio_id for item in audios),
                (),
            ),
        ),
    )
    assert graph_result.graph is not None, graph_result.diagnostics
    return ContextPlan(
        "plan_m24_05_ref2va",
        request.schema_version,
        request,
        graph_result.graph,
        request.hard_constraints,
        request.evidence,
        (PlanStep("step_1", PlanStage.NORMALIZE, PlanStepStatus.COMPLETED, "normalize"),),
    )


def official_binding(profile: PromptProfile) -> SourceProfileBinding:
    digest = (
        OFFICIAL_H3_BASE_GUIDE_DIGEST
        if profile is PromptProfile.BASE
        else OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST
    )
    return build_official_source_profile_binding(
        ProfileIdentity(profile, CURRENT_SCHEMA_VERSION),
        observed_revision=OFFICIAL_H3_GUIDE_REVISION,
        observed_digest=digest,
    )


def section(document_text: str, field: str) -> str:
    """Return one rendered field body without re-implementing the parser."""

    marker = f"{field}: "
    start = document_text.find(f"\n\n{marker}")
    if start < 0:
        assert document_text.startswith(marker), f"{field} missing from prompt"
        start = 0
    else:
        start += 2
    body = document_text[start + len(marker) :]
    end = len(body)
    for candidate in (
        "\n\nintegrated_multimodal_description: ",
        "\n\noverall_soundscape: ",
        "\n\nnon_diegetic_music: ",
    ):
        found = body.find(candidate)
        if found >= 0:
            end = min(end, found)
    return body[:end].strip()


def conformance(plan: ContextPlan) -> GuideConformanceResult:
    return evaluate_guide_conformance(plan, render_base_prompt(plan))


def reason_ids(plan: ContextPlan) -> set[str]:
    return {item.value for item in conformance(plan).reasons}


def carries(haystack: str, fragment: str) -> bool:
    """Composition may capitalize a fragment that opens a sentence; the words must survive."""

    return fragment.casefold() in haystack.casefold()


class SoundscapeTruthTests(unittest.TestCase):
    """AC-05: `overall_soundscape: N/A` is a silence claim and needs explicit authority."""

    def test_unspecified_soundscape_never_renders_the_silence_marker(self) -> None:
        plan = make_plan(audios=(), segment_audio_ids=())
        text = render_base_prompt(plan).text
        self.assertEqual(section(text, "overall_soundscape"), UNSPECIFIED_SOUNDSCAPE_TEXT)
        self.assertNotEqual(section(text, "overall_soundscape"), "N/A")

    def test_unspecified_soundscape_is_incomplete_with_a_content_free_reason(self) -> None:
        plan = make_plan(audios=(), segment_audio_ids=())
        result = conformance(plan)
        self.assertIs(result.readiness, GuideReadiness.INCOMPLETE)
        self.assertIn(
            PromptFidelityDiagnosticId.SOUNDSCAPE_UNSPECIFIED.value,
            {item.value for item in result.reasons},
        )

    def test_explicit_complete_silence_is_the_only_source_of_the_silence_marker(self) -> None:
        plan = make_plan(
            audios=(),
            segment_audio_ids=(),
            soundscape=SoundscapeDisposition.EXPLICIT_COMPLETE_SILENCE,
        )
        text = render_base_prompt(plan).text
        self.assertEqual(section(text, "overall_soundscape"), "N/A")
        self.assertNotIn(PromptFidelityDiagnosticId.SOUNDSCAPE_UNSPECIFIED.value, reason_ids(plan))

    def test_explicit_silence_contradicting_audible_events_fails_closed(self) -> None:
        with self.assertRaises(IntentGraphError):
            make_plan(soundscape=SoundscapeDisposition.EXPLICIT_COMPLETE_SILENCE)

    def test_described_soundscape_requires_an_owned_audible_contribution(self) -> None:
        plan = make_plan(soundscape=SoundscapeDisposition.DESCRIBED)
        self.assertEqual(
            section(render_base_prompt(plan).text, "overall_soundscape"),
            "soft morning street ambience",
        )
        with self.assertRaises(IntentGraphError):
            make_plan(
                audios=(),
                segment_audio_ids=(),
                soundscape=SoundscapeDisposition.DESCRIBED,
            )


class AudioOwnershipTests(unittest.TestCase):
    """AC-06: every audio item enters only the official layer it actually owns."""

    def _plan(self, audio: AudioIntent) -> ContextPlan:
        return make_plan(audios=(audio,), segment_audio_ids=(audio.audio_id,))

    def test_dialogue_never_reaches_either_summary_field(self) -> None:
        plan = self._plan(
            AudioIntent("audio_1", AudioLayer.DIALOGUE, "the baker greets a customer")
        )
        text = render_base_prompt(plan).text
        self.assertTrue(
            carries(
                section(text, "integrated_multimodal_description"),
                "the baker greets a customer",
            )
        )
        self.assertFalse(carries(section(text, "overall_soundscape"), "greets a customer"))
        self.assertFalse(carries(section(text, "non_diegetic_music"), "greets a customer"))

    def test_ambience_and_sfx_summarize_into_the_soundscape(self) -> None:
        for layer in (AudioLayer.AMBIENCE, AudioLayer.SFX):
            with self.subTest(layer=layer):
                plan = self._plan(AudioIntent("audio_1", layer, "shutters rattle open"))
                text = render_base_prompt(plan).text
                self.assertIn("shutters rattle open", section(text, "overall_soundscape"))
                self.assertEqual(section(text, "non_diegetic_music"), "N/A")

    def test_non_diegetic_audio_summarizes_into_the_audience_only_field(self) -> None:
        plan = self._plan(
            AudioIntent("audio_1", AudioLayer.NON_DIEGETIC, "a warm piano score builds slowly")
        )
        text = render_base_prompt(plan).text
        self.assertIn("a warm piano score builds slowly", section(text, "non_diegetic_music"))
        self.assertNotIn("piano score", section(text, "overall_soundscape"))

    def test_generic_music_without_explicit_ownership_enters_no_output_section(self) -> None:
        plan = self._plan(AudioIntent("audio_1", AudioLayer.MUSIC, "a radio tune"))
        text = render_base_prompt(plan).text
        self.assertNotIn("a radio tune", section(text, "overall_soundscape"))
        self.assertNotIn("a radio tune", section(text, "non_diegetic_music"))
        self.assertIn(PromptFidelityDiagnosticId.AUDIO_OWNERSHIP_UNRESOLVED.value, reason_ids(plan))
        self.assertIs(conformance(plan).readiness, GuideReadiness.INCOMPLETE)

    def test_generic_music_reaches_a_section_once_ownership_is_declared(self) -> None:
        audience = self._plan(
            AudioIntent(
                "audio_1",
                AudioLayer.MUSIC,
                "a radio tune",
                ownership=AudioOwnership.AUDIENCE_ONLY,
            )
        )
        self.assertTrue(
            carries(
                section(render_base_prompt(audience).text, "non_diegetic_music"), "a radio tune"
            )
        )
        diegetic = self._plan(
            AudioIntent(
                "audio_1",
                AudioLayer.MUSIC,
                "a radio tune",
                ownership=AudioOwnership.INTEGRATED,
            )
        )
        text = render_base_prompt(diegetic).text
        self.assertTrue(carries(section(text, "integrated_multimodal_description"), "a radio tune"))
        self.assertEqual(section(text, "non_diegetic_music"), "N/A")

    def test_generic_diegetic_without_ownership_is_unresolved(self) -> None:
        plan = self._plan(AudioIntent("audio_1", AudioLayer.DIEGETIC, "an espresso machine hisses"))
        self.assertIn(PromptFidelityDiagnosticId.AUDIO_OWNERSHIP_UNRESOLVED.value, reason_ids(plan))

    def test_contradictory_ownership_fails_closed(self) -> None:
        contradictions = (
            (AudioLayer.DIALOGUE, AudioOwnership.AUDIENCE_ONLY),
            (AudioLayer.DIALOGUE, AudioOwnership.SOUNDSCAPE),
            (AudioLayer.NON_DIEGETIC, AudioOwnership.SOUNDSCAPE),
            (AudioLayer.AMBIENCE, AudioOwnership.AUDIENCE_ONLY),
            (AudioLayer.SFX, AudioOwnership.AUDIENCE_ONLY),
            (AudioLayer.DIEGETIC, AudioOwnership.AUDIENCE_ONLY),
            (AudioLayer.MUSIC, AudioOwnership.SOUNDSCAPE),
        )
        for layer, ownership in contradictions:
            with self.subTest(layer=layer, ownership=ownership):
                with self.assertRaises(IntentGraphError):
                    AudioIntent("audio_1", layer, "a sound", ownership=ownership)

    def test_unlinked_timeline_audio_is_not_silently_aggregated(self) -> None:
        plan = make_plan(
            audios=(AudioIntent("audio_1", AudioLayer.AMBIENCE, "distant traffic"),),
            segment_audio_ids=(),
        )
        text = render_base_prompt(plan).text
        self.assertNotIn("distant traffic", text)
        self.assertIn(PromptFidelityDiagnosticId.AUDIO_UNLINKED.value, reason_ids(plan))

    def test_whole_video_scope_contributes_without_a_timeline_link(self) -> None:
        plan = make_plan(
            audios=(
                AudioIntent(
                    "audio_1",
                    AudioLayer.AMBIENCE,
                    "distant traffic",
                    scope=AudioScope.WHOLE_VIDEO,
                ),
            ),
            segment_audio_ids=(),
        )
        text = render_base_prompt(plan).text
        self.assertIn("distant traffic", section(text, "overall_soundscape"))
        self.assertNotIn(PromptFidelityDiagnosticId.AUDIO_UNLINKED.value, reason_ids(plan))


class BaseProseTests(unittest.TestCase):
    """AC-04: natural shot prose, no repo-owned label stack."""

    def test_repo_owned_meta_labels_do_not_enter_the_prompt(self) -> None:
        text = render_base_prompt(make_plan()).text
        residues = ("Declared intent:", "subjects:", "scene:", "action:", "camera:", "ambience:")
        for residue in residues:
            self.assertNotIn(residue, text)

    def test_typed_intent_content_survives_as_prose(self) -> None:
        text = section(render_base_prompt(make_plan()).text, "integrated_multimodal_description")
        for fragment in (
            "the baker",
            "a quiet street bakery",
            "opens the wooden shutters",
            "a slow push in",
            "cinematic live-action",
        ):
            self.assertTrue(carries(text, fragment), fragment)

    def test_rendering_stays_deterministic(self) -> None:
        self.assertEqual(render_base_prompt(make_plan()).text, render_base_prompt(make_plan()).text)

    def test_hard_constraints_still_survive_verbatim(self) -> None:
        constraints = HardConstraintSet(
            (
                ExactTextConstraint("d1", ExactTextKind.DIALOGUE, "First batch!", "English"),
                ExactTextConstraint("s1", ExactTextKind.VISIBLE_TEXT, "營業中"),
                RequiredContent("r1", scope=ContentScope.GENERAL, content="a copper scale"),
                ForbiddenContent("f1", scope=ContentScope.GENERAL, content="secret sign"),
            )
        )
        text = render_base_prompt(make_plan(constraints=constraints)).text
        self.assertIn("First batch!", text)
        self.assertIn("營業中", text)
        self.assertIn("a copper scale", text)
        self.assertNotIn("secret sign", text)

    def test_semantic_empty_skeleton_still_represents_user_intent(self) -> None:
        text = render_base_prompt(make_plan(semantic=False)).text
        self.assertIn(INTENT, text)
        self.assertNotIn("Declared intent:", text)
        self.assertNotIn("the declared scene develops along the timeline", text)


class OuterCompatibilityTests(unittest.TestCase):
    """AC-02: the officially pinned outer shape does not move."""

    def test_base_keeps_exactly_three_fields_in_order(self) -> None:
        document = render_base_prompt(make_plan())
        self.assertEqual(
            [item.heading for item in document.sections],
            ["integrated_multimodal_description", "overall_soundscape", "non_diegetic_music"],
        )

    def test_keyframe_alignment_instructions_are_unchanged(self) -> None:
        i2va = render_base_prompt(make_plan(TaskMode.I2VA, registry=frame_registry(TaskMode.I2VA)))
        self.assertTrue(
            i2va.text.startswith(
                "For the target video, at 0.00 seconds into the target video, "
                "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
            )
        )
        l2va = render_base_prompt(make_plan(TaskMode.L2VA, registry=frame_registry(TaskMode.L2VA)))
        self.assertTrue(
            l2va.text.startswith(
                "How the reference pictures align with the target video — "
                "<Picture 1> (from [Shot 1]) aligns with the 5.17-second mark of the "
                "target video.\n\n"
            )
        )


class KeyframeReadinessTests(unittest.TestCase):
    """AC-03/AC-04: keyframe readiness is proven by owned joins, never by keyword presence."""

    def test_semantic_empty_i2va_is_incomplete_for_named_reasons(self) -> None:
        plan = make_plan(TaskMode.I2VA, registry=frame_registry(TaskMode.I2VA), semantic=False)
        result = conformance(plan)
        self.assertIs(result.readiness, GuideReadiness.INCOMPLETE)
        ids = {item.value for item in result.reasons}
        self.assertIn(PromptFidelityDiagnosticId.DESCRIPTION_SEMANTIC_EMPTY.value, ids)
        self.assertIn(PromptFidelityDiagnosticId.KEYFRAME_ANCHOR_MISSING.value, ids)
        self.assertIn(PromptFidelityDiagnosticId.SOUNDSCAPE_UNSPECIFIED.value, ids)

    def test_owned_anchor_and_forward_action_reach_ready(self) -> None:
        plan = make_plan(
            TaskMode.I2VA,
            registry=frame_registry(TaskMode.I2VA),
            soundscape=SoundscapeDisposition.DESCRIBED,
        )
        subjects = (replace(plan.intent_graph.subjects[0], source_asset_ids=("first_frame",)),)
        segments = tuple(
            replace(item, development=SegmentDevelopment.ANCHOR_DEVELOPMENT)
            for item in plan.intent_graph.segments
        )
        graph = replace(plan.intent_graph, subjects=subjects, segments=segments)
        ready = replace(plan, intent_graph=graph)
        result = evaluate_guide_conformance(ready, render_base_prompt(ready))
        self.assertIs(result.readiness, GuideReadiness.READY)
        self.assertEqual(result.reasons, ())

    def test_missing_forward_development_is_reported_separately(self) -> None:
        plan = make_plan(
            TaskMode.I2VA,
            registry=frame_registry(TaskMode.I2VA),
            soundscape=SoundscapeDisposition.DESCRIBED,
        )
        subjects = (replace(plan.intent_graph.subjects[0], source_asset_ids=("first_frame",)),)
        segments = tuple(replace(item, action_ids=()) for item in plan.intent_graph.segments)
        graph = replace(plan.intent_graph, subjects=subjects, segments=segments)
        candidate = replace(plan, intent_graph=graph)
        ids = {
            item.value
            for item in evaluate_guide_conformance(candidate, render_base_prompt(candidate)).reasons
        }
        self.assertIn(PromptFidelityDiagnosticId.KEYFRAME_DEVELOPMENT_MISSING.value, ids)
        self.assertNotIn(PromptFidelityDiagnosticId.KEYFRAME_ANCHOR_MISSING.value, ids)


class ConformanceResultContractTests(unittest.TestCase):
    """AC-08: one versioned readiness result, and no caller may assert it."""

    def test_result_is_versioned_and_serializable(self) -> None:
        wire = conformance(make_plan()).to_wire()
        self.assertEqual(wire["schema"], "h3.context.guide_conformance.v2")
        self.assertIn(wire["readiness"], {item.value for item in GuideReadiness})
        self.assertIsInstance(wire["reasons"], list)

    def test_every_reason_is_a_fidelity_identity(self) -> None:
        plan = make_plan(TaskMode.I2VA, registry=frame_registry(TaskMode.I2VA), semantic=False)
        for reason in conformance(plan).reasons:
            self.assertIsInstance(reason, PromptFidelityDiagnosticId)

    def test_readiness_cannot_be_asserted_by_a_caller(self) -> None:
        with self.assertRaises(PromptLintError):
            GuideConformanceResult(GuideReadiness.READY, ())


class WireMigrationTests(unittest.TestCase):
    """AC-09: a graph persisted before M24-05 loads with the explicit unspecified defaults."""

    def test_audio_defaults_are_unspecified_and_timeline_scoped(self) -> None:
        audio = AudioIntent("audio_1", AudioLayer.AMBIENCE, "soft morning street ambience")
        self.assertIs(audio.ownership, AudioOwnership.UNSPECIFIED)
        self.assertIs(audio.scope, AudioScope.TIMELINE)
        wire = audio.to_wire()
        self.assertEqual(wire["ownership"], AudioOwnership.UNSPECIFIED.value)
        self.assertEqual(wire["scope"], AudioScope.TIMELINE.value)

    def test_graph_soundscape_defaults_to_unspecified_never_to_silence(self) -> None:
        graph = make_plan().intent_graph
        self.assertIs(graph.soundscape, SoundscapeDisposition.UNSPECIFIED)
        self.assertEqual(graph.to_wire()["soundscape"], SoundscapeDisposition.UNSPECIFIED.value)


class FullReferenceSemanticsTests(unittest.TestCase):
    """AC-07: Ref2VA prose states real roles and carries no repository vocabulary."""

    def test_summary_task_prefix_follows_the_roles_actually_supplied(self) -> None:
        audio = ReferenceAsset("audio_a", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE, 1)
        oven = AudioIntent("audio_1", AudioLayer.AMBIENCE, "the oven hum", ("audio_a",))
        cases = (
            (
                "[video editing]",
                (ReferenceAsset("video_a", MediaKind.VIDEO, AssetRole.EDITING_SOURCE, 1),),
                (),
                (),
            ),
            (
                "[video continuation]",
                (ReferenceAsset("video_a", MediaKind.VIDEO, AssetRole.CONTINUATION_SOURCE, 1),),
                (),
                (),
            ),
            (
                "[keyframe completion]",
                (
                    ReferenceAsset(
                        "first_frame", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1, IMAGE_METADATA
                    ),
                    ReferenceAsset(
                        "last_frame", MediaKind.IMAGE, AssetRole.LAST_FRAME, 2, IMAGE_METADATA
                    ),
                ),
                (),
                (),
            ),
            ("[audio reference]", (audio,), (oven,), ()),
            (
                "[audio reuse]",
                (audio,),
                (oven,),
                (
                    RetentionRelation(
                        "retention_1",
                        RetentionDomain.AUDIO,
                        ("audio_a",),
                        "audio_1",
                        AudioRetentionMarker.FULLY_COPY,
                    ),
                ),
            ),
            (
                "The task relationship is not explicitly declared.",
                (
                    ReferenceAsset(
                        "picture_a", MediaKind.IMAGE, AssetRole.REFERENCE, 1, IMAGE_METADATA
                    ),
                ),
                (),
                (),
            ),
        )
        for prefix, assets, audios, retention in cases:
            with self.subTest(prefix=prefix):
                plan = ref2va_plan(assets, audios=audios, retention=retention)
                text = render_full_reference_prompt(plan).text
                self.assertIn(f"summary: {prefix} ", text)

    def test_standalone_definitions_state_the_role_each_reference_plays(self) -> None:
        plan = ref2va_plan(
            (
                ReferenceAsset(
                    "first_frame", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1, IMAGE_METADATA
                ),
                ReferenceAsset(
                    "last_frame", MediaKind.IMAGE, AssetRole.LAST_FRAME, 2, IMAGE_METADATA
                ),
                ReferenceAsset("video_a", MediaKind.VIDEO, AssetRole.STYLE_REFERENCE, 3),
            )
        )
        definitions = render_full_reference_prompt(plan).text
        self.assertIn("<Picture 1> is the first frame of the target video.", definitions)
        self.assertIn("<Picture 2> is the final frame of the target video.", definitions)
        self.assertIn("<Video 1> is the style reference.", definitions)

    def test_only_declared_references_are_named_and_dual_roles_survive(self) -> None:
        text = render_full_reference_prompt(full_plan()).text
        for label in ("<Picture 1>", "<Audio 1>", "<Video 1>", "<Audio 2>"):
            self.assertIn(label, text)
        self.assertNotIn("<Picture 2>", text)
        self.assertIn("<Video 1> is the footage being edited.", text)

    def test_retention_markers_stay_inside_their_own_domain(self) -> None:
        text = render_full_reference_prompt(full_plan()).text
        self.assertIn("<Subject 1>: fully_preserved -", text)
        self.assertIn("<Audio 1>: partially_copy -", text)
        self.assertIn("<Video 1> is copied in full into this shot.", text)

    def test_no_internal_identifier_enum_or_repo_label_survives(self) -> None:
        text = render_full_reference_prompt(full_plan()).text
        internal_ids = (
            "picture_a",
            "picture_b",
            "video_source",
            "audio_pair",
            "audio_score",
            "subject_1",
            "scene_1",
            "camera_1",
            "style_1",
            "action_1",
            "segment_1",
            "segment_2",
            "retention_1",
            "retention_2",
            "event_1",
            "audio_1",
            "audio_2",
        )
        enum_vocabulary = (
            "identifies asset",
            "with role",
            "media kind",
            "editing_source",
            "audio_source",
            "full_copy",
        )
        repo_labels = (
            "Declared style:",
            "Declared intent:",
            "References used:",
            "subjects:",
            "scene:",
            "action:",
            "camera:",
            "diegetic:",
            "references:",
            "events:",
            "->",
        )
        for residue in internal_ids + enum_vocabulary + repo_labels:
            with self.subTest(residue=residue):
                self.assertNotIn(residue, text)

    def test_six_official_fields_stay_in_order(self) -> None:
        text = render_full_reference_prompt(full_plan()).text
        headings = [block.split(":", 1)[0] for block in text.split("\n\n")]
        self.assertEqual(
            headings,
            [
                "subject_definitions",
                "summary",
                "retention_analysis",
                "detailed_description",
                "overall_soundscape",
                "non_diegetic_music",
            ],
        )


class ValidationTruthTests(unittest.TestCase):
    """AC-08: one readiness result, and canonical self-comparison cannot authorize it."""

    def test_unbound_speaker_is_readiness_until_identity_is_declared(self) -> None:
        original = make_plan(TaskMode.T2VA)
        value = ExactTextConstraint("line", ExactTextKind.DIALOGUE, "Hello.", "English")

        def audit(row: ExactTextConstraint) -> PromptFidelityAuditResult:
            constraints = HardConstraintSet((row,))
            plan = replace(
                original,
                hard_constraints=constraints,
                request=replace(original.request, hard_constraints=constraints),
            )
            return audit_prompt_fidelity(plan, render_base_prompt(plan))

        self.assertIn(
            PromptFidelityDiagnosticId.DIALOGUE_SPEAKER_UNBOUND,
            set(readiness_from_audit(audit(value)).reasons),
        )
        self.assertNotIn(
            PromptFidelityDiagnosticId.DIALOGUE_SPEAKER_UNBOUND,
            set(
                readiness_from_audit(
                    audit(
                        replace(value, speakers=(DialogueSpeaker("guide", identity="the guide"),))
                    )
                ).reasons
            ),
        )

    def test_every_consumer_reads_the_same_independent_result(self) -> None:
        plan = make_plan(TaskMode.I2VA, registry=frame_registry(TaskMode.I2VA))
        document = render_base_prompt(plan)
        from_audit = readiness_from_audit(audit_prompt_fidelity(plan, document))
        self.assertEqual(evaluate_guide_conformance(plan, document), from_audit)
        profiled = validate_profiled_prompt(plan, document, official_binding(PromptProfile.BASE))
        self.assertEqual(profiled.conformance, from_audit)

    def test_structural_validity_and_readiness_are_separate_facts(self) -> None:
        plan = make_plan(TaskMode.I2VA, registry=frame_registry(TaskMode.I2VA), semantic=False)
        result = render_profiled_prompt(plan, official_binding(PromptProfile.BASE))
        self.assertTrue(result.is_valid)
        self.assertIsNotNone(result.conformance)
        assert result.conformance is not None
        self.assertIs(result.conformance.readiness, GuideReadiness.INCOMPLETE)

    def test_a_canonical_document_alone_cannot_authorize_an_official_claim(self) -> None:
        plan = make_plan(TaskMode.I2VA, registry=frame_registry(TaskMode.I2VA), semantic=False)
        result = render_profiled_prompt(plan, official_binding(PromptProfile.BASE))
        # The document is byte-identical to what the canonical renderer produces, so the
        # self-comparison finds nothing; the claim is still withheld.
        self.assertEqual(result.document.text, render_base_prompt(plan).text)
        self.assertFalse(result.official_claim_admitted)

    def test_readiness_is_projected_into_the_wire_result(self) -> None:
        plan = make_plan(TaskMode.I2VA, registry=frame_registry(TaskMode.I2VA), semantic=False)
        wire = render_profiled_prompt(plan, official_binding(PromptProfile.BASE)).to_wire()
        conformance_wire = wire["conformance"]
        assert isinstance(conformance_wire, dict)
        self.assertEqual(conformance_wire["schema"], "h3.context.guide_conformance.v2")
        self.assertEqual(conformance_wire["readiness"], GuideReadiness.INCOMPLETE.value)
        self.assertTrue(conformance_wire["reasons"])


class PrivacyTests(unittest.TestCase):
    """AC-10: readiness reporting stays content-free."""

    def test_reasons_carry_no_user_content(self) -> None:
        plan = make_plan(TaskMode.I2VA, registry=frame_registry(TaskMode.I2VA), semantic=False)
        payload = repr(conformance(plan).to_wire())
        self.assertNotIn(INTENT, payload)
        self.assertNotIn("baker", payload)


if __name__ == "__main__":
    unittest.main()
