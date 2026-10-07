"""M0-07 namespaced ComfyUI registration contract tests."""

from __future__ import annotations

import ast
import unittest
from collections.abc import MutableMapping
from pathlib import Path

import comfyui_h3_context as package
from comfyui_h3_context.core.errors import RegistrationConflictError, RegistrationProbeError
from comfyui_h3_context.nodes import (
    AUDIO_PERCEPTION_NODE_ID,
    AUDIT_OVERRIDE_NODE_ID,
    COMPILER_NODE_ID,
    CONSTRAINED_SEMANTIC_NODE_ID,
    CROSS_REFERENCE_PRODUCER_NODE_ID,
    DIRECTIVE_AUTHORITY_PRODUCER_NODE_ID,
    EVIDENCE_FUSION_PRODUCER_NODE_ID,
    FEASIBLE_AV_TIMELINE_NODE_ID,
    FULL_REFERENCE_NODE_ID,
    FULL_REFERENCE_TIMELINE_PRODUCER_NODE_ID,
    HARD_CONSTRAINT_PRODUCER_NODE_ID,
    HIERARCHICAL_REDUCTION_NODE_ID,
    INTENT_GRAPH_PRODUCER_NODE_ID,
    LOCAL_RECONSTRUCTION_NODE_ID,
    MEDIA_ADMISSION_NODE_ID,
    NATIVE_H3_ADAPTER_NODE_ID,
    OFFICIAL_CONTEXT_IR_NODE_ID,
    PLAN_NODE_ID,
    PREVIEW_NODE_ID,
    PRODUCT_SHELL_NODE_ID,
    PROVIDER_TRANSPARENCY_NODE_ID,
    REFERENCE_NODE_ID,
    RELIABILITY_NODE_ID,
    REQUEST_NODE_ID,
    SEMANTIC_PROPOSAL_PRODUCER_NODE_ID,
    SOURCE_PROFILED_RENDERER_NODE_ID,
    VALIDATOR_NODE_ID,
    VISUAL_PERCEPTION_NODE_ID,
    H3ContextAuditOverrideNode,
    H3ContextCompilerNode,
    H3ContextFullReferenceNode,
    H3ContextNativeH3AdapterNode,
    H3ContextPlanNode,
    H3ContextPreviewNode,
    H3ContextProductShellNode,
    H3ContextProviderTransparencyNode,
    H3ContextReliabilityNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
    H3OfficialContextIRNode,
    H3ReferenceRegistryNode,
    H3SemanticProposalProducerNode,
)
from comfyui_h3_context.registration import (
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
    RegistrationProbe,
    register_nodes,
)

ROOT = Path(__file__).resolve().parents[1]


class RegistrationHarnessTests(unittest.TestCase):
    def test_root_exposes_explicit_namespaced_product_mappings(self) -> None:
        self.assertEqual(package.NODE_CLASS_MAPPINGS, NODE_CLASS_MAPPINGS)
        self.assertEqual(package.NODE_DISPLAY_NAME_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS)
        expected_ids = {
            REQUEST_NODE_ID,
            REFERENCE_NODE_ID,
            PLAN_NODE_ID,
            COMPILER_NODE_ID,
            FULL_REFERENCE_NODE_ID,
            VALIDATOR_NODE_ID,
            PREVIEW_NODE_ID,
            AUDIT_OVERRIDE_NODE_ID,
            NATIVE_H3_ADAPTER_NODE_ID,
            PRODUCT_SHELL_NODE_ID,
            SEMANTIC_PROPOSAL_PRODUCER_NODE_ID,
            OFFICIAL_CONTEXT_IR_NODE_ID,
            PROVIDER_TRANSPARENCY_NODE_ID,
            RELIABILITY_NODE_ID,
            MEDIA_ADMISSION_NODE_ID,
            VISUAL_PERCEPTION_NODE_ID,
            AUDIO_PERCEPTION_NODE_ID,
            HARD_CONSTRAINT_PRODUCER_NODE_ID,
            INTENT_GRAPH_PRODUCER_NODE_ID,
            EVIDENCE_FUSION_PRODUCER_NODE_ID,
            CROSS_REFERENCE_PRODUCER_NODE_ID,
            DIRECTIVE_AUTHORITY_PRODUCER_NODE_ID,
            FULL_REFERENCE_TIMELINE_PRODUCER_NODE_ID,
            FEASIBLE_AV_TIMELINE_NODE_ID,
            HIERARCHICAL_REDUCTION_NODE_ID,
            CONSTRAINED_SEMANTIC_NODE_ID,
            SOURCE_PROFILED_RENDERER_NODE_ID,
            LOCAL_RECONSTRUCTION_NODE_ID,
        }
        self.assertEqual(set(NODE_CLASS_MAPPINGS), expected_ids)
        self.assertTrue(REQUEST_NODE_ID.startswith("comfyui_h3_context."))
        self.assertIs(NODE_CLASS_MAPPINGS[REQUEST_NODE_ID], H3ContextRequestNode)
        self.assertIs(NODE_CLASS_MAPPINGS[REFERENCE_NODE_ID], H3ReferenceRegistryNode)
        self.assertIs(NODE_CLASS_MAPPINGS[PLAN_NODE_ID], H3ContextPlanNode)
        self.assertIs(NODE_CLASS_MAPPINGS[COMPILER_NODE_ID], H3ContextCompilerNode)
        self.assertIs(NODE_CLASS_MAPPINGS[FULL_REFERENCE_NODE_ID], H3ContextFullReferenceNode)
        self.assertIs(NODE_CLASS_MAPPINGS[VALIDATOR_NODE_ID], H3ContextValidatorNode)
        self.assertIs(NODE_CLASS_MAPPINGS[PREVIEW_NODE_ID], H3ContextPreviewNode)
        self.assertIs(NODE_CLASS_MAPPINGS[AUDIT_OVERRIDE_NODE_ID], H3ContextAuditOverrideNode)
        self.assertIs(
            NODE_CLASS_MAPPINGS[PROVIDER_TRANSPARENCY_NODE_ID],
            H3ContextProviderTransparencyNode,
        )
        self.assertIs(NODE_CLASS_MAPPINGS[RELIABILITY_NODE_ID], H3ContextReliabilityNode)
        self.assertIs(NODE_CLASS_MAPPINGS[NATIVE_H3_ADAPTER_NODE_ID], H3ContextNativeH3AdapterNode)
        self.assertIs(NODE_CLASS_MAPPINGS[PRODUCT_SHELL_NODE_ID], H3ContextProductShellNode)
        self.assertIs(
            NODE_CLASS_MAPPINGS[SEMANTIC_PROPOSAL_PRODUCER_NODE_ID],
            H3SemanticProposalProducerNode,
        )
        self.assertIs(NODE_CLASS_MAPPINGS[OFFICIAL_CONTEXT_IR_NODE_ID], H3OfficialContextIRNode)
        self.assertEqual(
            set(NODE_DISPLAY_NAME_MAPPINGS),
            expected_ids,
        )

    def test_registration_is_idempotent_and_preserves_unrelated_entries(self) -> None:
        unrelated = object()
        host_nodes: MutableMapping[str, object] = {"Native.Sentinel": unrelated}
        host_display: MutableMapping[str, str] = {"Native.Sentinel": "Native Sentinel"}

        register_nodes(host_nodes, host_display)
        first_node = host_nodes[REQUEST_NODE_ID]
        register_nodes(host_nodes, host_display)

        self.assertIs(host_nodes["Native.Sentinel"], unrelated)
        self.assertIs(host_nodes[REQUEST_NODE_ID], first_node)
        self.assertEqual(host_display[REQUEST_NODE_ID], "H3 Context Request")
        self.assertEqual(host_display[REFERENCE_NODE_ID], "H3 Reference Registry")
        self.assertEqual(
            set(host_nodes),
            {
                "Native.Sentinel",
                REQUEST_NODE_ID,
                REFERENCE_NODE_ID,
                PLAN_NODE_ID,
                COMPILER_NODE_ID,
                FULL_REFERENCE_NODE_ID,
                VALIDATOR_NODE_ID,
                PREVIEW_NODE_ID,
                AUDIT_OVERRIDE_NODE_ID,
                PROVIDER_TRANSPARENCY_NODE_ID,
                RELIABILITY_NODE_ID,
                NATIVE_H3_ADAPTER_NODE_ID,
                PRODUCT_SHELL_NODE_ID,
                SEMANTIC_PROPOSAL_PRODUCER_NODE_ID,
                OFFICIAL_CONTEXT_IR_NODE_ID,
                MEDIA_ADMISSION_NODE_ID,
                VISUAL_PERCEPTION_NODE_ID,
                AUDIO_PERCEPTION_NODE_ID,
                HARD_CONSTRAINT_PRODUCER_NODE_ID,
                INTENT_GRAPH_PRODUCER_NODE_ID,
                EVIDENCE_FUSION_PRODUCER_NODE_ID,
                CROSS_REFERENCE_PRODUCER_NODE_ID,
                DIRECTIVE_AUTHORITY_PRODUCER_NODE_ID,
                FULL_REFERENCE_TIMELINE_PRODUCER_NODE_ID,
                FEASIBLE_AV_TIMELINE_NODE_ID,
                HIERARCHICAL_REDUCTION_NODE_ID,
                CONSTRAINED_SEMANTIC_NODE_ID,
                SOURCE_PROFILED_RENDERER_NODE_ID,
                LOCAL_RECONSTRUCTION_NODE_ID,
            },
        )

        reloaded_node = type(
            "ReloadedH3ContextRequestNode",
            (),
            {"__h3_context_node_id__": REQUEST_NODE_ID},
        )
        host_nodes[REQUEST_NODE_ID] = reloaded_node
        register_nodes(host_nodes, host_display)
        self.assertIs(host_nodes[REQUEST_NODE_ID], reloaded_node)

    def test_foreign_node_or_display_collision_fails_before_mutation(self) -> None:
        foreign = object()
        host_nodes: MutableMapping[str, object] = {REQUEST_NODE_ID: foreign}
        host_display: MutableMapping[str, str] = {}
        with self.assertRaises(RegistrationConflictError):
            register_nodes(host_nodes, host_display)
        self.assertIs(host_nodes[REQUEST_NODE_ID], foreign)
        self.assertEqual(host_display, {})

        host_nodes = {REQUEST_NODE_ID: None}
        with self.assertRaises(RegistrationConflictError):
            register_nodes(host_nodes, {})
        self.assertIsNone(host_nodes[REQUEST_NODE_ID])

        host_nodes = {}
        host_display = {REQUEST_NODE_ID: "Foreign display"}
        with self.assertRaises(RegistrationConflictError):
            register_nodes(host_nodes, host_display)
        self.assertEqual(host_nodes, {})
        self.assertEqual(host_display[REQUEST_NODE_ID], "Foreign display")

    def test_registration_probe_never_returns_dummy_success(self) -> None:
        with self.assertRaises(RegistrationProbeError):
            RegistrationProbe().execute()
        self.assertEqual(RegistrationProbe.INPUT_TYPES(), {"required": {}})
        self.assertEqual(RegistrationProbe.RETURN_TYPES, ())

    def test_registration_module_has_no_optional_host_or_runtime_imports(self) -> None:
        path = ROOT / "comfyui_h3_context" / "registration.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        forbidden = {
            "aiohttp",
            "comfy",
            "comfy_api",
            "cv2",
            "diffusers",
            "httpx",
            "moviepy",
            "requests",
            "torch",
            "transformers",
        }
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imports.append((node.module or "").split(".", 1)[0])
        self.assertTrue(forbidden.isdisjoint(imports), imports)


if __name__ == "__main__":
    unittest.main()
