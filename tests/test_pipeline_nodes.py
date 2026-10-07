"""M3-04 plan, compiler, and validator V1 node adapter tests."""

from __future__ import annotations

import ast
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from comfyui_h3_context.core import (
    AssetRole,
    PromptRenderStatus,
    RawContextRequest,
    ReferenceRegistry,
    TaskMode,
)
from comfyui_h3_context.core import canonical_context_pipeline as canonical_pipeline
from comfyui_h3_context.core.canonical_context_pipeline import (
    CanonicalContextPipelineError,
    build_canonical_context_plan,
    build_canonical_context_request,
    compile_canonical_context_plan,
    validate_canonical_context_document,
)
from comfyui_h3_context.core.reconstruction_stages import (
    _register_reconstruction_source_plan,
    _register_reconstruction_validation,
)
from comfyui_h3_context.nodes import (
    COMPILER_NODE_ID,
    INTENT_GRAPH_SOCKET_TYPE,
    PLAN_NODE_ID,
    PROMPT_DOCUMENT_SOCKET_TYPE,
    PROMPT_STRING_SOCKET_TYPE,
    REPORT_SOCKET_TYPE,
    VALIDATION_SOCKET_TYPE,
    VALIDATOR_NODE_ID,
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
    H3ReferenceRegistryNode,
    PipelineNodeError,
    RequestNodeError,
)

ROOT = Path(__file__).resolve().parents[1]
NODE_MODULE = ROOT / "comfyui_h3_context" / "nodes.py"


def request(
    mode: TaskMode = TaskMode.T2VA,
    *,
    duration_seconds: float | None = None,
) -> RawContextRequest:
    return H3ContextRequestNode().build_request(
        mode,
        "Preserve the exact declared intent.",
        duration_seconds=duration_seconds,
    )[0]


def registry() -> ReferenceRegistry:
    return H3ReferenceRegistryNode().build_registry(images=[object()])[0]


class PipelineNodeTests(unittest.TestCase):
    def test_node_wrappers_are_exact_views_of_the_shared_canonical_pipeline(self) -> None:
        with mock.patch.object(
            canonical_pipeline,
            "_register_reconstruction_source_plan",
            wraps=_register_reconstruction_source_plan,
        ) as register_source:
            direct_request = build_canonical_context_request(
                TaskMode.T2VA,
                "Preserve the exact declared intent.",
                duration_seconds=5.0,
            )
            node_request = request(duration_seconds=5.0)
            direct_plan, direct_draft = build_canonical_context_plan(direct_request)
            node_plan, node_draft = H3ContextPlanNode().build_plan(node_request)
        self.assertEqual(node_request, direct_request)
        self.assertEqual((node_plan, node_draft), (direct_plan, direct_draft))
        self.assertEqual(register_source.call_count, 2)

        direct_compiled = compile_canonical_context_plan(direct_plan)
        node_compiled = H3ContextCompilerNode().compile(node_plan)
        self.assertEqual(node_compiled, direct_compiled)

        with mock.patch.object(
            canonical_pipeline,
            "_register_reconstruction_validation",
            wraps=_register_reconstruction_validation,
        ) as register_validation:
            direct_validated = validate_canonical_context_document(direct_plan, direct_compiled[2])
            node_validated = H3ContextValidatorNode().validate(node_plan, node_compiled[2])
        self.assertEqual(node_validated, direct_validated)
        self.assertEqual(register_validation.call_count, 2)

    def test_shared_pipeline_and_node_wrappers_refuse_the_same_invalid_boundaries(self) -> None:
        private_intent = "private-intent-marker-must-not-enter-an-exception"
        with self.assertRaises(CanonicalContextPipelineError) as direct_request_error:
            build_canonical_context_request("invalid", private_intent)
        with self.assertRaises(RequestNodeError) as node_request_error:
            H3ContextRequestNode().build_request("invalid", private_intent)
        self.assertEqual(
            tuple(row.code for row in node_request_error.exception.diagnostics),
            tuple(row.code for row in direct_request_error.exception.diagnostics),
        )
        self.assertNotIn(private_intent, str(direct_request_error.exception))
        self.assertNotIn(private_intent, str(node_request_error.exception))

        with self.assertRaises(CanonicalContextPipelineError) as direct_plan_error:
            build_canonical_context_plan(object())
        with self.assertRaises(PipelineNodeError) as node_plan_error:
            H3ContextPlanNode().build_plan(object())
        self.assertEqual(
            tuple(row.code for row in node_plan_error.exception.diagnostics),
            tuple(row.code for row in direct_plan_error.exception.diagnostics),
        )

        plan = build_canonical_context_plan(
            build_canonical_context_request(TaskMode.T2VA, "intent")
        )[0]
        with self.assertRaises(CanonicalContextPipelineError) as direct_compile_error:
            compile_canonical_context_plan(object())
        with self.assertRaises(PipelineNodeError) as node_compile_error:
            H3ContextCompilerNode().compile(object())
        self.assertEqual(
            tuple(row.code for row in node_compile_error.exception.diagnostics),
            tuple(row.code for row in direct_compile_error.exception.diagnostics),
        )

        with self.assertRaises(CanonicalContextPipelineError) as direct_document_error:
            validate_canonical_context_document(plan, object())
        with self.assertRaises(PipelineNodeError) as node_document_error:
            H3ContextValidatorNode().validate(plan, object())
        self.assertEqual(
            tuple(row.code for row in node_document_error.exception.diagnostics),
            tuple(row.code for row in direct_document_error.exception.diagnostics),
        )

    def test_frame_registry_reaches_plan_without_losing_typed_role_ownership(self) -> None:
        cases = (
            (TaskMode.I2VA, {"first_frame": object()}, (AssetRole.FIRST_FRAME,)),
            (TaskMode.L2VA, {"last_frame": object()}, (AssetRole.LAST_FRAME,)),
            (
                TaskMode.FL2VA,
                {"first_frame": object(), "last_frame": object()},
                (AssetRole.FIRST_FRAME, AssetRole.LAST_FRAME),
            ),
        )
        for mode, sockets, expected_roles in cases:
            with self.subTest(mode=mode.value):
                owned = H3ReferenceRegistryNode().build_registry(**sockets)[0]
                plan, _report = H3ContextPlanNode().build_plan(
                    request(mode, duration_seconds=5.167), owned
                )
                self.assertEqual(
                    tuple(asset.role for asset in plan.request.reference_registry.assets),
                    expected_roles,
                )
                self.assertEqual(plan.intent_graph.registry, owned)

    def test_plan_compiler_validator_metadata_matches_m3_contract(self) -> None:
        plan_inputs = H3ContextPlanNode.INPUT_TYPES()
        self.assertEqual(H3ContextPlanNode.NODE_ID, PLAN_NODE_ID)
        self.assertEqual(H3ContextPlanNode.RETURN_TYPES, ("H3_CONTEXT_PLAN", REPORT_SOCKET_TYPE))
        self.assertEqual(tuple(plan_inputs), ("required", "optional"))
        self.assertEqual(tuple(plan_inputs["required"]), ("request",))
        self.assertEqual(tuple(plan_inputs["optional"]), ("reference_registry", "intent_graph"))
        self.assertEqual(plan_inputs["optional"]["intent_graph"][0], INTENT_GRAPH_SOCKET_TYPE)

        compiler_inputs = H3ContextCompilerNode.INPUT_TYPES()
        self.assertEqual(H3ContextCompilerNode.NODE_ID, COMPILER_NODE_ID)
        self.assertEqual(
            H3ContextCompilerNode.RETURN_TYPES,
            (PROMPT_STRING_SOCKET_TYPE, REPORT_SOCKET_TYPE, PROMPT_DOCUMENT_SOCKET_TYPE),
        )
        self.assertEqual(tuple(compiler_inputs["required"]), ("plan",))

        validator_inputs = H3ContextValidatorNode.INPUT_TYPES()
        self.assertEqual(H3ContextValidatorNode.NODE_ID, VALIDATOR_NODE_ID)
        self.assertEqual(
            H3ContextValidatorNode.RETURN_TYPES,
            (VALIDATION_SOCKET_TYPE, REPORT_SOCKET_TYPE),
        )
        self.assertEqual(H3ContextValidatorNode.RETURN_NAMES, ("validation", "validated_report"))
        self.assertEqual(tuple(validator_inputs["required"]), ("plan", "prompt_document"))

    def test_plan_normalizes_and_returns_explicit_manual_skeleton_report(self) -> None:
        raw = request(TaskMode.REF2VA, duration_seconds=5.0)
        plan, report = H3ContextPlanNode().build_plan(raw, registry())[0:2]
        self.assertEqual(plan.request.task_mode, TaskMode.REF2VA)
        self.assertEqual(plan.intent_graph.registry, plan.request.reference_registry)
        self.assertEqual(plan.intent_graph.segments[0].start.raw, "0")
        self.assertAlmostEqual(
            float(plan.intent_graph.segments[0].end.seconds),
            plan.request.effective_duration_seconds,
        )
        self.assertEqual(plan.limitations[0].code, "manual_plan_skeleton")
        self.assertEqual(report.prompt_document.status, PromptRenderStatus.DRAFT)
        self.assertFalse(report.is_successful)
        self.assertEqual(report.validation.status.value, "not_run")
        encoded = json.dumps(report.to_wire(), ensure_ascii=False, sort_keys=True)
        self.assertIn("manual_plan_skeleton", encoded)
        self.assertIn("Preserve the exact declared intent.", encoded)

    def test_explicit_graph_is_preserved_and_request_registry_mismatch_fails(self) -> None:
        raw = request(TaskMode.REF2VA)
        owned_registry = registry()
        normalized = H3ContextPlanNode().build_plan(raw, owned_registry)[0]
        graph = normalized.intent_graph
        plan, _ = H3ContextPlanNode().build_plan(raw, owned_registry, graph)
        self.assertIs(plan.intent_graph, graph)
        other_registry = H3ReferenceRegistryNode().build_registry(images=[object(), object()])[0]
        with self.assertRaises(PipelineNodeError) as context:
            H3ContextPlanNode().build_plan(raw, other_registry, graph)
        self.assertIn("reference_registry_mismatch", str(context.exception))

    def test_plan_preserves_normalization_warning_without_fake_failure(self) -> None:
        plan, report = H3ContextPlanNode().build_plan(request(duration_seconds=5.0))
        self.assertTrue(any(item.code == "duration_snapped" for item in report.diagnostics))
        self.assertFalse(report.has_errors)
        self.assertEqual(plan.request.requested_duration_seconds, 5.0)

    def test_compiler_selects_explicit_profile_and_is_deterministic(self) -> None:
        base_plan = H3ContextPlanNode().build_plan(request())[0]
        first = H3ContextCompilerNode().compile(base_plan)
        second = H3ContextCompilerNode().compile(base_plan)
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[2].text, first[0])
        self.assertEqual(first[2].status, PromptRenderStatus.RENDERED)
        self.assertEqual(first[1].validation.status.value, "not_run")
        self.assertIn("Preserve the exact declared intent.", first[0])

        full_plan = H3ContextPlanNode().build_plan(request(TaskMode.REF2VA), registry())[0]
        full_prompt, _, full_document = H3ContextCompilerNode().compile(full_plan)
        self.assertNotIn("<Picture 1>", full_prompt)
        self.assertEqual(len(full_document.sections), 6)
        self.assertIn("Preserve the exact declared intent.", full_prompt)
        self.assertEqual(
            full_plan.request.reference_registry.label_for("image_1").label,
            "<Picture 1>",
        )
        self.assertEqual(full_document.profile, full_plan.request.profile)

    def test_validator_delegates_lint_and_never_repairs_prompt_text(self) -> None:
        plan = H3ContextPlanNode().build_plan(request())[0]
        prompt, _, document = H3ContextCompilerNode().compile(plan)
        passed = H3ContextValidatorNode().validate(plan, document)[0]
        self.assertTrue(passed.is_valid)
        self.assertEqual(passed.target_id, document.document_id)

        tampered = replace(document, text="tampered prompt")
        failed = H3ContextValidatorNode().validate(plan, tampered)[0]
        self.assertFalse(failed.is_valid)
        self.assertTrue(failed.diagnostics)
        self.assertEqual(tampered.text, "tampered prompt")
        self.assertEqual(prompt, document.text)

    def test_invalid_nodes_fail_closed_without_plausible_fallback(self) -> None:
        with self.assertRaises(PipelineNodeError) as plan_error:
            H3ContextPlanNode().build_plan(object())
        self.assertIn("invalid_request", str(plan_error.exception))

        with self.assertRaises(PipelineNodeError) as compiler_error:
            H3ContextCompilerNode().compile(object())
        self.assertIn("invalid_plan", str(compiler_error.exception))

        with self.assertRaises(PipelineNodeError) as validator_error:
            H3ContextValidatorNode().validate(object(), object())
        self.assertIn("invalid_plan", str(validator_error.exception))

    def test_pipeline_errors_do_not_echo_untrusted_request_values(self) -> None:
        unsafe = replace(request(), mode="mode-with-sensitive-value")
        with self.assertRaises(PipelineNodeError) as context:
            H3ContextPlanNode().build_plan(unsafe)
        self.assertNotIn("mode-with-sensitive-value", str(context.exception))
        self.assertNotIn("mode-with-sensitive-value", context.exception.diagnostics[0].message)

    def test_node_module_has_no_optional_runtime_imports(self) -> None:
        tree = ast.parse(NODE_MODULE.read_text(encoding="utf-8"), filename=str(NODE_MODULE))
        forbidden = {
            "aiohttp",
            "comfy",
            "comfy_api",
            "cv2",
            "diffusers",
            "httpx",
            "moviepy",
            "numpy",
            "requests",
            "torch",
            "transformers",
        }
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".")[0])
        self.assertTrue(forbidden.isdisjoint(imports), imports)


if __name__ == "__main__":
    unittest.main()
