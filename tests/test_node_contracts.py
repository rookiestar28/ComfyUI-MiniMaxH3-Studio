"""M3-01 declarative ComfyUI node and socket contract tests."""

from __future__ import annotations

import ast
import copy
import json
import unittest
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import node_contracts as contracts
from comfyui_h3_context.core.errors import NodeContractError
from comfyui_h3_context.core.node_contracts import (
    NODE_CONTRACT_SCHEMA,
    NODE_NAMESPACE,
    HostApiFamily,
    HostCapabilities,
    HostVersion,
    NodeContract,
    NodeContractRegistry,
    NodeSocket,
    NodeSocketType,
    default_node_contract_registry,
)

ROOT = Path(__file__).resolve().parents[1]
NODE_CONTRACTS = ROOT / "comfyui_h3_context" / "core" / "node_contracts.py"


def socket(
    name: str = "value",
    socket_type: NodeSocketType = NodeSocketType.STRING,
    *,
    required: bool = True,
    default: str | int | float | bool | None = None,
    min_items: int | None = None,
    max_items: int = 1,
    choices: tuple[str, ...] = (),
    description: str = "test socket",
) -> NodeSocket:
    minimum = (1 if required else 0) if min_items is None else min_items
    return NodeSocket(
        name,
        socket_type,
        required,
        default,
        minimum,
        max_items,
        choices,
        description,
    )


def contract(
    node_id: str = "comfyui_h3_context.Test.Node",
    *,
    inputs: tuple[NodeSocket, ...] = (),
    outputs: tuple[NodeSocket, ...] | None = None,
) -> NodeContract:
    return NodeContract(
        node_id,
        "Test Node",
        "h3_context/test",
        inputs,
        outputs if outputs is not None else (socket("result"),),
        HostApiFamily.V1,
        HostVersion(0, 30, 0),
        "M3-01 test contract; future changes require migration.",
        "M3-01",
    )


class NodeContractTests(unittest.TestCase):
    def test_default_registry_declares_reviewed_surface(self) -> None:
        registry = default_node_contract_registry()
        expected_ids = (
            "comfyui_h3_context.H3Context.Request",
            "comfyui_h3_context.H3Context.ReferenceRegistry",
            "comfyui_h3_context.H3Context.Plan",
            "comfyui_h3_context.H3Context.Compiler",
            "comfyui_h3_context.H3Context.FullReference",
            "comfyui_h3_context.H3Context.Validator",
            "comfyui_h3_context.H3Context.Preview",
            "comfyui_h3_context.H3Context.AuditOverride",
            "comfyui_h3_context.H3Context.ProviderTransparency",
            "comfyui_h3_context.H3Context.Reliability",
            "comfyui_h3_context.H3Context.NativeH3Adapter",
            "comfyui_h3_context.H3Context.ProductShell",
            "comfyui_h3_context.H3Context.SemanticProposalProducer",
            "comfyui_h3_context.H3Context.OfficialContextIR",
            "comfyui_h3_context.H3Context.MediaAdmissionProducer",
            "comfyui_h3_context.H3Context.VisualPerceptionProducer",
            "comfyui_h3_context.H3Context.AudioPerceptionProducer",
            "comfyui_h3_context.H3Context.HardConstraintProducer",
            "comfyui_h3_context.H3Context.IntentGraphProducer",
            "comfyui_h3_context.H3Context.EvidenceFusionProducer",
            "comfyui_h3_context.H3Context.CrossReferenceProducer",
            "comfyui_h3_context.H3Context.DirectiveAuthorityProducer",
            "comfyui_h3_context.H3Context.FullReferenceTimelineProducer",
            "comfyui_h3_context.H3Context.FeasibleAVTimeline",
            "comfyui_h3_context.H3Context.HierarchicalEvidenceReduction",
            "comfyui_h3_context.H3Context.ConstrainedSemanticPlanning",
            "comfyui_h3_context.H3Context.SourceProfiledRenderer",
            "comfyui_h3_context.H3Context.LocalReconstruction",
        )
        self.assertEqual(tuple(item.node_id for item in registry.definitions), expected_ids)
        self.assertEqual(registry.schema, NODE_CONTRACT_SCHEMA)
        self.assertTrue(
            all(item.node_id.startswith(NODE_NAMESPACE) for item in registry.definitions)
        )
        self.assertTrue(all(not item.executable for item in registry.definitions))
        self.assertTrue(all(item.migration_note for item in registry.definitions))
        self.assertEqual(registry.get(expected_ids[0]).display_name, "H3 Context Request")
        self.assertEqual(registry.get(expected_ids[0]).category, "h3_context/contracts")

        product_shell = registry.get("comfyui_h3_context.H3Context.ProductShell")
        self.assertEqual(
            tuple(
                (item.name, item.socket_type.value, item.required) for item in product_shell.inputs
            ),
            (
                ("report", "H3_CONTEXT_REPORT", True),
                ("native_h3_wiring", "H3_NATIVE_H3_WIRING", True),
                ("recompute_plan", "H3_RECOMPUTE_PLAN", False),
                ("pipeline_transaction", "H3_PIPELINE_TRANSACTION", False),
                ("generation_sequence_state", "H3_GENERATION_SEQUENCE_STATE", False),
                (
                    "semantic_proposal_review_authority",
                    "H3_SEMANTIC_PROPOSAL_REVIEW_AUTHORITY",
                    False,
                ),
            ),
        )
        self.assertEqual(product_shell.roadmap_stage, "M17-06")

        request = registry.get(expected_ids[0])
        task_mode = request.inputs[0]
        self.assertEqual(task_mode.socket_type, NodeSocketType.STRING)
        self.assertEqual(task_mode.default, "t2va")
        self.assertEqual(task_mode.choices, ("t2va", "i2va", "fl2va", "l2va", "ref2va"))
        self.assertTrue(request.inputs[1].required)
        self.assertFalse(request.inputs[2].required)
        self.assertEqual(request.inputs[2].min_items, 0)
        self.assertEqual(request.inputs[2].max_items, 1)
        self.assertEqual(request.outputs[0].socket_type, NodeSocketType.H3_CONTEXT_REQUEST)

        references = registry.get(expected_ids[1])
        self.assertEqual(
            tuple(item.max_items for item in references.inputs),
            (1, 1, 9, 3, 3, 3),
        )
        self.assertEqual(
            tuple(item.socket_type for item in references.inputs),
            (
                NodeSocketType.IMAGE,
                NodeSocketType.IMAGE,
                NodeSocketType.IMAGE,
                NodeSocketType.VIDEO,
                NodeSocketType.AUDIO,
                NodeSocketType.AUDIO,
            ),
        )

        plan = registry.get(expected_ids[2])
        self.assertEqual(
            tuple(item.name for item in plan.inputs),
            ("request", "reference_registry", "intent_graph"),
        )
        self.assertFalse(plan.inputs[2].required)
        self.assertEqual(plan.inputs[2].socket_type, NodeSocketType.H3_INTENT_GRAPH)

        compiler = registry.get(expected_ids[3])
        self.assertEqual(
            tuple(item.socket_type for item in compiler.outputs),
            (
                NodeSocketType.H3_PROMPT_STRING,
                NodeSocketType.H3_CONTEXT_REPORT,
                NodeSocketType.H3_PROMPT_DOCUMENT,
            ),
        )
        self.assertIn("appended output", compiler.migration_note)

        full_reference = registry.get(expected_ids[4])
        self.assertEqual(tuple(item.name for item in full_reference.inputs), ("timeline",))
        self.assertEqual(
            full_reference.inputs[0].socket_type, NodeSocketType.H3_FULL_REFERENCE_TIMELINE
        )
        self.assertEqual(
            tuple(item.socket_type for item in full_reference.outputs),
            (NodeSocketType.H3_CONTEXT_PLAN, NodeSocketType.H3_CONTEXT_REPORT),
        )

        validator = registry.get(expected_ids[5])
        self.assertEqual(
            tuple(item.name for item in validator.outputs),
            ("validation", "validated_report"),
        )
        self.assertEqual(
            tuple(item.socket_type for item in validator.outputs),
            (NodeSocketType.H3_VALIDATION_RESULT, NodeSocketType.H3_CONTEXT_REPORT),
        )
        self.assertEqual(validator.roadmap_stage, "M10-05")

        preview = registry.get(expected_ids[6])
        self.assertEqual(
            tuple(item.socket_type for item in preview.outputs),
            (NodeSocketType.H3_PROMPT_STRING, NodeSocketType.H3_CONTEXT_PREVIEW),
        )
        self.assertEqual(tuple(item.name for item in preview.outputs), ("prompt", "preview"))
        self.assertIn("structured preview output", preview.migration_note)

        audit_override = registry.get(expected_ids[7])
        self.assertEqual(
            tuple(item.name for item in audit_override.inputs),
            ("report", "base_report_fingerprint", "revision", "reason", "prompt_text"),
        )
        self.assertEqual(
            tuple(item.socket_type for item in audit_override.outputs),
            (
                NodeSocketType.H3_PROMPT_STRING,
                NodeSocketType.H3_CONTEXT_REPORT,
                NodeSocketType.H3_AUDIT_OVERRIDE,
                NodeSocketType.H3_PROMPT_DOCUMENT,
            ),
        )
        self.assertIn("revalidated", audit_override.migration_note)

        transparency = registry.get(expected_ids[8])
        self.assertEqual(
            tuple(item.name for item in transparency.inputs),
            (
                "provider",
                "privacy_mode",
                "offline",
                "network_allowed",
                "upload_consent",
                "credential_reference",
                "fallback_provider",
                "local_backend",
            ),
        )
        self.assertEqual(
            tuple(item.socket_type for item in transparency.outputs),
            (
                NodeSocketType.H3_PROVIDER_TRANSPARENCY,
                NodeSocketType.H3_PROVIDER_CONSENT,
                NodeSocketType.STRING,
                NodeSocketType.H3_PROVIDER_SETUP,
            ),
        )
        self.assertIn("before execution", transparency.migration_note)

        reliability = registry.get(expected_ids[9])
        self.assertEqual(
            tuple(item.name for item in reliability.inputs),
            (
                "operation_id",
                "stage",
                "completed_units",
                "total_units",
                "run_state",
                "cancel_requested",
                "attempt",
                "max_attempts",
                "checkpoint_status",
                "resume_requested",
            ),
        )
        self.assertEqual(
            tuple(item.socket_type for item in reliability.outputs),
            (
                NodeSocketType.H3_EXECUTION_STATUS,
                NodeSocketType.H3_PROGRESS_EVENT,
                NodeSocketType.H3_RECOVERY_DECISION,
                NodeSocketType.STRING,
            ),
        )
        self.assertIn("never starts", reliability.migration_note)

        native_adapter = registry.get(expected_ids[10])
        self.assertEqual(tuple(item.name for item in native_adapter.inputs), ("report",))
        self.assertEqual(
            tuple(item.socket_type for item in native_adapter.outputs),
            (NodeSocketType.H3_PROMPT_STRING, NodeSocketType.H3_NATIVE_H3_WIRING),
        )
        self.assertIn("directly connected", native_adapter.migration_note)

        product_shell = registry.get(expected_ids[11])
        self.assertEqual(
            tuple(item.name for item in product_shell.inputs),
            (
                "report",
                "native_h3_wiring",
                "recompute_plan",
                "pipeline_transaction",
                "generation_sequence_state",
                "semantic_proposal_review_authority",
            ),
        )
        self.assertEqual(
            tuple(item.socket_type for item in product_shell.outputs),
            (NodeSocketType.STRING, NodeSocketType.H3_PRODUCT_SHELL),
        )
        self.assertEqual(product_shell.roadmap_stage, "M17-06")

        semantic_producer = registry.get(expected_ids[12])
        self.assertEqual(
            tuple(item.name for item in semantic_producer.inputs),
            ("report", "wiring", "provider_setup", "ollama_profile", "ollama_model"),
        )
        self.assertEqual(
            tuple(item.socket_type for item in semantic_producer.outputs),
            (NodeSocketType.H3_SEMANTIC_PROPOSAL_REVIEW_AUTHORITY,),
        )
        self.assertEqual(semantic_producer.roadmap_stage, "M17-16")

        official = registry.get(expected_ids[13])
        self.assertEqual(
            tuple(item.name for item in official.inputs),
            (
                "request",
                "ratio",
                "upload_consent",
                "network_allowed",
                "credential_reference",
                "reference_registry",
                "media",
            ),
        )
        self.assertEqual(
            tuple(item.socket_type for item in official.outputs),
            (
                NodeSocketType.H3_PROMPT_STRING,
                NodeSocketType.H3_PROVIDER_RECEIPT,
                NodeSocketType.H3_PROVIDER_CONSENT,
            ),
        )

    def test_wire_projection_is_json_safe_and_deterministic(self) -> None:
        first = default_node_contract_registry().to_wire()
        second = default_node_contract_registry().to_wire()
        self.assertEqual(first, second)
        self.assertEqual(json.loads(json.dumps(first)), first)
        self.assertEqual(first["schema"], NODE_CONTRACT_SCHEMA)
        nodes = cast(list[dict[str, object]], first["nodes"])
        self.assertEqual(len(nodes), 28)
        self.assertEqual(
            [item["node_id"] for item in nodes],
            [item.node_id for item in default_node_contract_registry().definitions],
        )

    def test_contract_registry_is_immutable_and_lookup_is_explicit(self) -> None:
        registry = default_node_contract_registry()
        snapshot = copy.deepcopy(registry.to_wire())
        added = registry.with_definition(contract())
        self.assertEqual(len(registry.definitions), 28)
        self.assertEqual(len(added.definitions), 29)
        self.assertEqual(registry.to_wire(), snapshot)
        self.assertIs(added.get("comfyui_h3_context.Test.Node"), added.definitions[-1])
        with self.assertRaises(NodeContractError):
            registry.get("comfyui_h3_context.Missing")
        with self.assertRaises(NodeContractError):
            registry.with_definition(contract("comfyui_h3_context.H3Context.Request"))
        with self.assertRaises(NodeContractError):
            NodeContractRegistry((contract(), contract()))
        with self.assertRaises(NodeContractError):
            NodeContractRegistry(registry.definitions, schema="h3-node-contract/999")

    def test_invalid_socket_defaults_metadata_and_cardinality_fail_closed(self) -> None:
        cases: tuple[Callable[[], NodeSocket], ...] = (
            lambda: socket("BadName"),
            lambda: socket("value", required=False, min_items=1),
            lambda: socket("value", min_items=2, max_items=1),
            lambda: socket("value", socket_type=NodeSocketType.INT, default=True),
            lambda: socket("value", socket_type=NodeSocketType.STRING, default=4),
            lambda: socket("value", socket_type=NodeSocketType.IMAGE, default="image"),
            lambda: socket("value", choices=("one", "one")),
            lambda: socket("value", default="other", choices=("one",)),
            lambda: socket("value", description="https://example.invalid/resource"),
            lambda: socket("value", description="line\nfeed"),
        )
        for make_socket in cases:
            with self.subTest(make_socket=make_socket):
                with self.assertRaises(NodeContractError):
                    make_socket()

    def test_invalid_node_identity_and_duplicate_socket_names_fail_closed(self) -> None:
        with self.assertRaises(NodeContractError):
            contract("foreign.Node")
        with self.assertRaises(NodeContractError):
            contract(inputs=(socket("same"),), outputs=(socket("same"),))
        with self.assertRaises(NodeContractError):
            NodeContract(
                "comfyui_h3_context.Test.Node",
                "Test Node",
                "H3 Context",
                (),
                (socket("result"),),
                HostApiFamily.V1,
                HostVersion(0, 30, 0),
                "migration",
                "M3-01",
            )
        with self.assertRaises(NodeContractError):
            NodeContract(
                "comfyui_h3_context.Test.Node",
                "Test Node",
                "h3_context/test",
                (),
                (socket("result"),),
                HostApiFamily.V1,
                HostVersion(0, 30, 0),
                "https://example.invalid/migration",
                "M3-01",
            )

    def test_foreign_collision_check_does_not_mutate_input(self) -> None:
        registry = default_node_contract_registry()
        foreign: dict[str, object] = {
            "Native.Sentinel": object(),
            registry.definitions[0].node_id: object(),
        }
        before = dict(foreign)
        with self.assertRaises(NodeContractError):
            registry.check_foreign_collisions(foreign)
        self.assertEqual(foreign, before)
        registry.check_foreign_collisions({"Native.Sentinel"})
        with self.assertRaises(NodeContractError):
            bad_ids: list[object] = ["Native.Sentinel", 4]
            registry.check_foreign_collisions(cast(Iterable[str], bad_ids))

    def test_host_capabilities_are_explicit_and_unsupported_values_fail(self) -> None:
        registry = default_node_contract_registry()
        all_types = tuple(NodeSocketType)
        supported = HostCapabilities(HostApiFamily.V1, HostVersion(0, 30, 0), all_types)
        registry.assert_host_compatible(supported)
        registry.assert_host_compatible(
            HostCapabilities(HostApiFamily.V1, HostVersion(999, 999, 999), all_types)
        )
        with self.assertRaises(NodeContractError):
            registry.assert_host_compatible(
                HostCapabilities(HostApiFamily.V3, HostVersion(0, 30, 0), all_types)
            )
        with self.assertRaises(NodeContractError):
            registry.assert_host_compatible(
                HostCapabilities(HostApiFamily.V1, HostVersion(0, 29, 9), all_types)
            )
        with self.assertRaises(NodeContractError):
            registry.assert_host_compatible(
                HostCapabilities(
                    HostApiFamily.V1,
                    HostVersion(0, 30, 0),
                    tuple(value for value in all_types if value is not NodeSocketType.AUDIO),
                )
            )
        with self.assertRaises(NodeContractError):
            registry.assert_host_compatible(object())  # type: ignore[arg-type]

    def test_host_version_wire_parser_is_strict_and_orderable(self) -> None:
        self.assertEqual(HostVersion.from_wire("0.30.0"), HostVersion(0, 30, 0))
        self.assertLess(HostVersion(0, 29, 9), HostVersion(0, 30, 0))
        self.assertEqual(str(HostVersion(1, 2, 3)), "1.2.3")
        for value in ("0.30", "v0.30.0", "0.30.0.dev1", "0.30.0 ", "-1.0.0"):
            with self.subTest(value=value), self.assertRaises(NodeContractError):
                HostVersion.from_wire(value)
        with self.assertRaises(NodeContractError):
            HostVersion(1000, 0, 0)

    def test_node_contract_module_has_no_optional_runtime_imports(self) -> None:
        tree = ast.parse(NODE_CONTRACTS.read_text(encoding="utf-8"), filename=str(NODE_CONTRACTS))
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
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".", 1)[0])
        self.assertTrue(forbidden.isdisjoint(imports), imports)

    def test_contract_types_are_leaf_module_exports(self) -> None:
        expected = {
            "NODE_CONTRACT_SCHEMA",
            "NODE_NAMESPACE",
            "HostApiFamily",
            "HostCapabilities",
            "HostVersion",
            "NodeContract",
            "NodeContractRegistry",
            "NodeSocket",
            "NodeSocketType",
            "default_node_contract_registry",
        }
        self.assertTrue(expected <= set(contracts.__all__))
        for name in expected:
            self.assertTrue(hasattr(contracts, name), name)


if __name__ == "__main__":
    unittest.main()
