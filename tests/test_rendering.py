"""M2-02 deterministic Base renderer tests."""

from __future__ import annotations

import ast
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from comfyui_h3_context.core import (
    AssetRole,
    AudioIntent,
    AudioLayer,
    CameraIntent,
    ContentScope,
    ContextPlan,
    ExactTextConstraint,
    ExactTextKind,
    ForbiddenContent,
    HardConstraintSet,
    IntentAction,
    IntentScene,
    IntentSubject,
    MediaKind,
    MediaMetadata,
    PlanStage,
    PlanStep,
    PlanStepStatus,
    ProfileIdentity,
    PromptProfile,
    PromptRenderingError,
    PromptRenderStatus,
    RawContextRequest,
    ReferenceAsset,
    ReferenceRegistry,
    StyleIntent,
    TaskMode,
    TimelineSegment,
    TimePoint,
    build_intent_graph,
    build_reference_registry,
    normalize_request,
    render_base_prompt,
)

ROOT = Path(__file__).resolve().parents[1]


def test_undeclared_dialogue_never_invents_a_placeholder_language() -> None:
    constraints = HardConstraintSet(
        (ExactTextConstraint("line", ExactTextKind.DIALOGUE, "Open the door."),)
    )
    document = render_base_prompt(make_plan(TaskMode.T2VA, constraints=constraints))
    assert "<d>Open the door.</d>" in document.text
    assert "<d>[unspecified]" not in document.text


def test_renderer_assigns_an_identity_to_an_unbound_line() -> None:
    constraints = HardConstraintSet(
        (ExactTextConstraint("line", ExactTextKind.DIALOGUE, "Hello.", "English"),)
    )
    document = render_base_prompt(make_plan(TaskMode.T2VA, constraints=constraints))
    assert "A speaker (S1) says: <d>[English] Hello.</d>" in document.text
    assert "Exact dialogue:" not in document.text


def make_plan(
    mode: TaskMode,
    registry: ReferenceRegistry | None = None,
    constraints: HardConstraintSet | None = None,
    segment_count: int = 1,
) -> ContextPlan:
    if registry is None:
        registry = ReferenceRegistry.empty()
    if constraints is None:
        constraints = HardConstraintSet()
    result = normalize_request(
        RawContextRequest(
            mode,
            "A baker opens the bakery before sunrise.",
            duration_seconds=5.167,
            reference_registry=registry,
            hard_constraints=constraints,
        )
    )
    assert result.request is not None
    request = result.request
    duration = TimePoint.from_text(str(request.effective_duration_seconds))
    subjects = (IntentSubject("subject_1", "the baker"),)
    scenes = (IntentScene("scene_1", "a quiet street bakery", ("subject_1",), "style_1"),)
    actions = (IntentAction("action_1", "opens the wooden shutters", ("subject_1",), "scene_1"),)
    cameras = (CameraIntent("camera_1", "a slow push in", "scene_1", ("subject_1",)),)
    styles = (StyleIntent("style_1", "cinematic live-action"),)
    audios = (AudioIntent("audio_1", AudioLayer.AMBIENCE, "soft morning street ambience"),)
    segment_duration = duration.seconds / segment_count

    def time_text(value: Decimal) -> str:
        return "0" if value == 0 else format(value, "f")

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
            ("audio_1",),
            (),
        )
        for index in range(segment_count)
    )
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
    )
    assert graph_result.graph is not None, graph_result.diagnostics
    return ContextPlan(
        "plan_base",
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


class BaseRenderingTests(unittest.TestCase):
    def test_t2va_golden_structure_and_determinism(self) -> None:
        document = render_base_prompt(make_plan(TaskMode.T2VA))
        # M24-05 replaced the label stack and the repository-owned `Declared intent:` suffix with
        # natural shot prose composed from the same typed content, and stopped repeating a
        # soundscape-owned sound inside the integrated body. The outer three fields, their order,
        # and the timing contract are unchanged.
        expected = (
            "integrated_multimodal_description: [Shot 1] Cinematic live-action. "
            "The baker opens the wooden shutters in a quiet street bakery, "
            "filmed with a slow push in.\n\n"
            "overall_soundscape: soft morning street ambience\n\n"
            "non_diegetic_music: N/A"
        )
        self.assertEqual(document.text, expected)
        self.assertEqual(
            [section.heading for section in document.sections],
            [
                "integrated_multimodal_description",
                "overall_soundscape",
                "non_diegetic_music",
            ],
        )
        self.assertEqual(document.status, PromptRenderStatus.RENDERED)
        self.assertEqual(document.text, render_base_prompt(make_plan(TaskMode.T2VA)).text)

    def test_keyframe_modes_emit_explicit_guide_alignment_instructions(self) -> None:
        i2va = render_base_prompt(make_plan(TaskMode.I2VA, frame_registry(TaskMode.I2VA)))
        self.assertTrue(
            i2va.text.startswith(
                "For the target video, at 0.00 seconds into the target video, "
                "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
            )
        )
        fl2va = render_base_prompt(
            make_plan(TaskMode.FL2VA, frame_registry(TaskMode.FL2VA), segment_count=2)
        )
        self.assertIn(
            "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; "
            "Picture 2 (from Shot 2) aligns with the 5.17-second mark of the target video.",
            fl2va.text,
        )
        l2va = render_base_prompt(make_plan(TaskMode.L2VA, frame_registry(TaskMode.L2VA)))
        self.assertTrue(
            l2va.text.startswith(
                "How the reference pictures align with the target video — "
                "<Picture 1> (from [Shot 1]) aligns with the 5.17-second mark of the "
                "target video.\n\n"
            )
        )

    def test_exact_and_required_constraints_survive_without_forbidden_content(self) -> None:
        constraints = HardConstraintSet(
            (
                ExactTextConstraint(
                    "dialogue_1", ExactTextKind.DIALOGUE, "First batch!", "English"
                ),
                ExactTextConstraint("sign_1", ExactTextKind.VISIBLE_TEXT, "營業中"),
                ForbiddenContent("forbidden_1", scope=ContentScope.GENERAL, content="secret sign"),
            )
        )
        document = render_base_prompt(make_plan(TaskMode.T2VA, constraints=constraints))
        self.assertIn("First batch!", document.text)
        self.assertIn("營業中", document.text)
        self.assertNotIn("secret sign", document.text)

    def test_invalid_or_inconsistent_plans_fail_closed(self) -> None:
        reference_registry = build_reference_registry(
            (ReferenceAsset("reference", MediaKind.IMAGE, AssetRole.REFERENCE, 1),)
        )
        with self.assertRaises(PromptRenderingError):
            render_base_prompt(make_plan(TaskMode.REF2VA, reference_registry))
        with self.assertRaises(PromptRenderingError):
            plan = make_plan(TaskMode.T2VA)
            wrong_profile_request = replace(
                plan.request,
                profile=ProfileIdentity(PromptProfile.FULL_REFERENCE, plan.request.profile.version),
            )
            render_base_prompt(replace(plan, request=wrong_profile_request))

    def test_rendering_module_has_no_optional_runtime_imports(self) -> None:
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
