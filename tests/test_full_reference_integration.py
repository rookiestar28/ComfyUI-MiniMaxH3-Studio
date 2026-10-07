"""M6-07 Full-Reference timeline hand-off and native composition tests."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from test_full_reference_timeline import audio_batch, directives, graph, normalized, video_batch

from comfyui_h3_context.core import (
    FullReferenceTimelineRequest,
    FullReferenceTimelineResult,
    PromptRenderStatus,
    ValidationDiagnostic,
    ValidationSeverity,
    plan_full_reference_timeline,
)
from comfyui_h3_context.nodes import (
    FULL_REFERENCE_NODE_ID,
    FULL_REFERENCE_TIMELINE_SOCKET_TYPE,
    H3ContextCompilerNode,
    H3ContextFullReferenceNode,
    H3ContextNativeH3AdapterNode,
    H3ContextValidatorNode,
    PipelineNodeError,
)

ROOT = Path(__file__).resolve().parents[1]


class FullReferenceIntegrationTests(unittest.TestCase):
    def test_timeline_handoff_reuses_compiler_validator_and_native_adapter(self) -> None:
        result = plan_full_reference_timeline(
            FullReferenceTimelineRequest(
                normalized(), video_batch(), audio_batch(), graph(), directives()
            )
        )
        self.assertTrue(result.is_valid)
        plan, report = H3ContextFullReferenceNode().build_plan(result)
        self.assertIs(plan, result.plan)
        self.assertEqual(report.plan, plan)
        self.assertEqual(report.prompt_document.status, PromptRenderStatus.DRAFT)

        prompt, compiled_report, document = H3ContextCompilerNode().compile(plan)
        self.assertEqual(document.status, PromptRenderStatus.RENDERED)
        self.assertIn("<Video 1>", prompt)
        self.assertIn("<Audio 1>", prompt)
        validation, validated_report = H3ContextValidatorNode().validate(plan, document)
        self.assertTrue(validation.is_valid)
        self.assertEqual(validated_report.prompt_document, compiled_report.prompt_document)
        native_prompt, wiring = H3ContextNativeH3AdapterNode().adapt(validated_report)
        self.assertEqual(native_prompt, prompt)
        self.assertEqual(wiring.native_node_id, "MiniMaxH3ReferenceToVideo")
        self.assertEqual(
            [item.label for item in wiring.bindings], ["<Picture 1>", "<Video 1>", "<Audio 1>"]
        )
        self.assertTrue(any(item.code == "missing_timeline_span" for item in plan.diagnostics))

    def test_failed_timeline_never_emits_plan_or_plausible_report(self) -> None:
        failed = FullReferenceTimelineResult(
            None,
            None,
            (
                ValidationDiagnostic(
                    ValidationSeverity.ERROR,
                    "directive_conflict",
                    "conflicting directives block the timeline",
                    "full_reference_timeline",
                ),
            ),
        )
        with self.assertRaises(PipelineNodeError) as context:
            H3ContextFullReferenceNode().build_plan(failed)
        self.assertIn("invalid_timeline", str(context.exception))
        self.assertNotIn("conflicting directives", str(context.exception))

    def test_node_contract_and_fixture_are_deterministic_and_safe(self) -> None:
        inputs = H3ContextFullReferenceNode.INPUT_TYPES()
        self.assertEqual(FULL_REFERENCE_NODE_ID, "comfyui_h3_context.H3Context.FullReference")
        self.assertEqual(tuple(inputs["required"]), ("timeline",))
        self.assertEqual(inputs["required"]["timeline"][0], FULL_REFERENCE_TIMELINE_SOCKET_TYPE)
        self.assertEqual(
            H3ContextFullReferenceNode.RETURN_TYPES,
            ("H3_CONTEXT_PLAN", "H3_CONTEXT_REPORT"),
        )
        fixture = json.loads(
            (ROOT / "workflows" / "m6_07_h3_context_full_reference.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(fixture["schema"], "h3-context-workflow-fixture/1")
        self.assertEqual(fixture["expected"]["task_mode"], "ref2va")
        self.assertEqual(
            fixture["expected"]["direct_media_links"],
            [
                {"source": "2", "source_output": 0, "target": "12", "target_input": "ref_images"},
                {"source": "3", "source_output": 0, "target": "12", "target_input": "ref_images"},
                {"source": "6", "source_output": 0, "target": "12", "target_input": "ref_videos"},
                {"source": "5", "source_output": 0, "target": "12", "target_input": "ref_audios"},
            ],
        )
        serialized = json.dumps(fixture, sort_keys=True).casefold()
        for marker in ("/home/", "c:\\", "https://", "authorization", "bearer ", "api_key"):
            self.assertNotIn(marker, serialized)

    def test_node_module_keeps_optional_runtime_boundary(self) -> None:
        tree = ast.parse((ROOT / "comfyui_h3_context" / "nodes.py").read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertTrue({"comfy", "torch", "av", "cv2", "requests"}.isdisjoint(imported))


if __name__ == "__main__":
    unittest.main()
