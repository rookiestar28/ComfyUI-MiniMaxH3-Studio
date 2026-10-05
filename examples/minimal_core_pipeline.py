"""Executable provider-free pure-core example for the public API documentation."""

from __future__ import annotations

import json

from comfyui_h3_context.core import (
    CURRENT_SCHEMA_VERSION,
    AudioIntent,
    AudioLayer,
    CameraIntent,
    ContextPlan,
    IntentAction,
    IntentScene,
    IntentSubject,
    PlanStage,
    PlanStep,
    PlanStepStatus,
    RawContextRequest,
    StyleIntent,
    TaskMode,
    TimelineSegment,
    TimePoint,
    build_intent_graph,
    canonical_fingerprint,
    lint_prompt,
    normalize_request,
    parse_prompt,
    render_base_prompt,
)


def build_example_plan() -> ContextPlan:
    """Construct one deterministic synthetic T2V plan without optional dependencies."""

    normalized = normalize_request(
        RawContextRequest(
            TaskMode.T2VA,
            "A baker opens the shop at sunrise.",
            duration_seconds=5.167,
        )
    )
    if normalized.request is None:
        raise RuntimeError(f"request normalization failed: {normalized.diagnostics!r}")
    request = normalized.request
    duration = TimePoint.from_text(str(request.effective_duration_seconds))
    graph_result = build_intent_graph(
        effective_duration=duration,
        registry=request.reference_registry,
        subjects=(IntentSubject("subject_1", "the baker"),),
        scenes=(IntentScene("scene_1", "a quiet bakery", ("subject_1",), "style_1"),),
        actions=(IntentAction("action_1", "opens the shutters", ("subject_1",), "scene_1"),),
        cameras=(CameraIntent("camera_1", "a slow push in", "scene_1", ("subject_1",)),),
        styles=(StyleIntent("style_1", "cinematic live action"),),
        audios=(AudioIntent("audio_1", AudioLayer.AMBIENCE, "soft morning ambience"),),
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
                ("audio_1",),
                (),
            ),
        ),
    )
    if graph_result.graph is None:
        raise RuntimeError(f"intent graph construction failed: {graph_result.diagnostics!r}")
    return ContextPlan(
        "example_plan",
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


def main() -> None:
    plan = build_example_plan()
    document = render_base_prompt(plan)
    lint_result = lint_prompt(plan, document)
    parsed = parse_prompt(document.text, document.profile, document.task_mode)
    if not lint_result.is_valid or not parsed.is_valid:
        raise RuntimeError(
            f"example validation failed: lint={lint_result.to_wire()!r}, parse={parsed.to_wire()!r}"
        )
    fingerprint = canonical_fingerprint(document.to_wire())
    repeat_fingerprint = canonical_fingerprint(document.to_wire())
    if fingerprint != repeat_fingerprint:
        raise RuntimeError("canonical fingerprint is not deterministic")
    print(
        json.dumps(
            {
                "profile": document.profile.name.value,
                "task_mode": document.task_mode.value,
                "lint_valid": lint_result.is_valid,
                "parse_canonical": parsed.canonical,
                "document_fingerprint": fingerprint,
                "prompt": document.text,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
