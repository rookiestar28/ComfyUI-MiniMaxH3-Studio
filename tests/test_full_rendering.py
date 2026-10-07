"""M2-03 deterministic Full-Reference renderer tests."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from comfyui_h3_context.core import (
    CURRENT_SCHEMA_VERSION,
    AssetRole,
    AudioIntent,
    AudioLayer,
    AudioRetentionMarker,
    CameraIntent,
    ContextPlan,
    EventCopy,
    EventCopyMode,
    ExactTextConstraint,
    ExactTextKind,
    HardConstraintSet,
    IntentAction,
    IntentScene,
    IntentSubject,
    MediaKind,
    MediaMetadata,
    PlanStage,
    PlanStep,
    PlanStepStatus,
    PromptRenderingError,
    RawContextRequest,
    ReferenceAsset,
    RetentionDomain,
    RetentionRelation,
    RetentionScope,
    StyleIntent,
    TaskMode,
    TimelineSegment,
    TimePoint,
    VisualRetentionMarker,
    build_intent_graph,
    build_reference_registry,
    normalize_request,
    render_full_reference_prompt,
)

ROOT = Path(__file__).resolve().parents[1]


def full_plan() -> ContextPlan:
    registry = build_reference_registry(
        (
            ReferenceAsset(
                "picture_a",
                MediaKind.IMAGE,
                AssetRole.REFERENCE,
                1,
                MediaMetadata(width=640, height=480),
            ),
            ReferenceAsset(
                "picture_b",
                MediaKind.IMAGE,
                AssetRole.REFERENCE,
                2,
                MediaMetadata(width=640, height=480),
            ),
            ReferenceAsset(
                "audio_pair",
                MediaKind.AUDIO,
                AssetRole.AUDIO_SOURCE,
                3,
                paired_video_id="video_source",
            ),
            ReferenceAsset("video_source", MediaKind.VIDEO, AssetRole.EDITING_SOURCE, 4),
            ReferenceAsset("audio_score", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE, 5),
        )
    )
    constraints = HardConstraintSet(
        (
            ExactTextConstraint(
                "dialogue_1", ExactTextKind.DIALOGUE, "Keep the light on.", "English"
            ),
            ExactTextConstraint("visible_1", ExactTextKind.VISIBLE_TEXT, "營業中"),
        )
    )
    result = normalize_request(
        RawContextRequest(
            TaskMode.REF2VA,
            "Adapt the bakery reference into a new target video.",
            duration_seconds=5.167,
            reference_registry=registry,
            hard_constraints=constraints,
        )
    )
    assert result.request is not None
    request = result.request
    duration = TimePoint.from_text(str(request.effective_duration_seconds))
    graph_result = build_intent_graph(
        effective_duration=duration,
        registry=registry,
        subjects=(
            IntentSubject("subject_1", "the baker", ("picture_a",)),
            IntentSubject("subject_2", "the bakery interior", ("video_source",)),
        ),
        scenes=(
            IntentScene("scene_1", "the reference bakery", ("subject_1", "subject_2"), "style_1"),
        ),
        actions=(IntentAction("action_1", "lights the oven", ("subject_1",), "scene_1"),),
        cameras=(CameraIntent("camera_1", "a slow tracking shot", "scene_1", ("subject_1",)),),
        styles=(StyleIntent("style_1", "cinematic documentary"),),
        audios=(
            AudioIntent("audio_1", AudioLayer.DIEGETIC, "the oven hum", ("audio_pair",)),
            AudioIntent("audio_2", AudioLayer.MUSIC, "sparse piano", ("audio_score",)),
        ),
        events=(
            EventCopy(
                "event_1",
                RetentionDomain.VISUAL,
                "video_source",
                "segment_2",
                EventCopyMode.FULL_COPY,
            ),
        ),
        retention=(
            RetentionRelation(
                "retention_1",
                RetentionDomain.VISUAL,
                ("picture_a",),
                "subject_1",
                VisualRetentionMarker.FULLY_PRESERVED,
                RetentionScope.SUBJECT,
            ),
            RetentionRelation(
                "retention_2",
                RetentionDomain.AUDIO,
                ("audio_pair",),
                "audio_1",
                AudioRetentionMarker.PARTIALLY_COPY,
                RetentionScope.AUDIO_LAYER,
            ),
        ),
        segments=(
            TimelineSegment(
                "segment_1",
                TimePoint.from_text("0"),
                TimePoint.from_text("2"),
                "scene_1",
                ("subject_1",),
                ("action_1",),
                "camera_1",
                "style_1",
                ("audio_1", "audio_2"),
                (),
            ),
            TimelineSegment(
                "segment_2",
                TimePoint.from_text("2"),
                duration,
                "scene_1",
                ("subject_2",),
                (),
                "camera_1",
                "style_1",
                ("audio_1",),
                ("event_1",),
            ),
        ),
    )
    assert graph_result.graph is not None, graph_result.diagnostics
    return ContextPlan(
        "plan_full",
        CURRENT_SCHEMA_VERSION,
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


class FullRenderingTests(unittest.TestCase):
    def test_six_sections_and_explicit_reference_ownership_are_deterministic(self) -> None:
        document = render_full_reference_prompt(full_plan())
        expected_headings = [
            "subject_definitions",
            "summary",
            "retention_analysis",
            "detailed_description",
            "overall_soundscape",
            "non_diegetic_music",
        ]
        self.assertEqual([section.heading for section in document.sections], expected_headings)
        self.assertIn("<Picture 1>", document.text)
        # Presence alone is inspectable input metadata, not a declared standalone semantic role.
        self.assertNotIn("<Picture 2> is a supplied visual reference.", document.text)
        self.assertIn("<Video 1>", document.text)
        self.assertIn("<Audio 1>", document.text)
        self.assertIn("<Audio 2>", document.text)
        self.assertIn("<Subject 1>", document.text)
        self.assertIn("<Subject 2>", document.text)
        self.assertNotIn("<Subject subject_1>", document.text)
        self.assertIn(
            "<Subject 1>: fully_preserved - <Subject 1> retains its defined subject role",
            document.text,
        )
        self.assertIn(
            "<Audio 1>: partially_copy - the selected audio layer from <Audio 1>",
            document.text,
        )
        self.assertIn(
            "[video editing + reference generation + audio reuse + audio reference]",
            document.text,
        )
        self.assertIn("The target video is an edited version of <Video 1>.", document.text)
        for residue in (
            "fully_copy",
            "full_copy",
            "picture_a",
            "video_source",
            "retention_1",
            "event_1",
            "Declared style:",
            " -> ",
        ):
            self.assertNotIn(residue, document.text)
        self.assertIn("[Shot 2] At 00:02.000", document.text)
        self.assertIn("Keep the light on.", document.text)
        self.assertIn("營業中", document.text)
        self.assertEqual(document.text, render_full_reference_prompt(full_plan()).text)

    def test_full_renderer_rejects_base_plan_and_requires_typed_plan(self) -> None:
        from test_rendering import make_plan

        with self.assertRaises(PromptRenderingError):
            render_full_reference_prompt(make_plan(TaskMode.T2VA))
        with self.assertRaises(PromptRenderingError):
            render_full_reference_prompt("not a plan")  # type: ignore[arg-type]

    def test_full_rendering_module_has_no_optional_runtime_imports(self) -> None:
        module = ast.parse(
            (ROOT / "comfyui_h3_context" / "core" / "rendering.py").read_text(encoding="utf-8")
        )
        optional_roots = {"comfy", "torch", "torchaudio", "requests", "httpx", "PIL", "numpy"}
        imported: set[str] = set()
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue(optional_roots.isdisjoint(imported))


if __name__ == "__main__":
    unittest.main()
