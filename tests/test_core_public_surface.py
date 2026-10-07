"""HC-12 exact pure-core hub derivation and package-containment tests."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "scripts" / "core_public_surface.py"
POLICY_PATH = ROOT / "governance" / "contracts" / "package_compatibility_policy_v1.json"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_hc12_core_public_surface", TOOL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the core-public-surface tool")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()


class CorePublicSurfaceDerivationTests(unittest.TestCase):
    def test_the_core_hub_is_exactly_the_derived_retained_set(self) -> None:
        current = TOOL.current_core_export_names()
        expected = TOOL.derive_required_core_exports()
        self.assertEqual(current, expected)
        self.assertEqual(len(current), 987)
        self.assertEqual(TOOL.unexplained_core_exports(), set())

    def test_every_typing_binding_is_declared_and_lazy_map_cannot_restore_removed_names(
        self,
    ) -> None:
        tree = ast.parse(TOOL.CORE_INIT.read_text(encoding="utf-8"))
        imported = {binding.bound for binding in TOOL.current_core_bindings()}
        self.assertEqual(imported, TOOL.current_core_export_names())
        self.assertTrue(
            any(
                isinstance(node, ast.FunctionDef) and node.name == "__getattr__"
                for node in tree.body
            )
        )
        import comfyui_h3_context.core as core

        self.assertEqual(set(core._EXPORTS), imported)
        self.assertNotIn("MAX_ASR_ALTERNATIVES", core._EXPORTS)

    def test_package_names_partition_into_pure_values_and_six_justified_facades(self) -> None:
        pure, facades = TOOL.package_surface_partition()
        policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        package_names = set(policy["consumer_authority"]["names"])
        self.assertEqual(pure | set(facades), package_names)
        self.assertEqual(pure & set(facades), set())
        self.assertEqual(len(pure), 9)
        self.assertEqual(len(facades), 6)
        self.assertEqual(facades, TOOL.PACKAGE_FACADE_REASONS)
        self.assertEqual(
            set(facades.values()),
            {"requires_node_registration_metadata_forbidden_to_pure_core"},
        )

    def test_gate_enforced_names_remain_a_pure_core_subset(self) -> None:
        self.assertLessEqual(TOOL.acceptance_abi_names(), TOOL.current_core_export_names())

    def test_removed_name_stays_at_its_leaf_and_not_on_the_hub(self) -> None:
        import comfyui_h3_context.core as core
        from comfyui_h3_context.core.asr_perception import MAX_ASR_ALTERNATIVES

        self.assertIsInstance(MAX_ASR_ALTERNATIVES, int)
        self.assertNotIn("MAX_ASR_ALTERNATIVES", core.__all__)
        self.assertFalse(hasattr(core, "MAX_ASR_ALTERNATIVES"))

    def test_retained_package_values_are_the_same_leaf_objects(self) -> None:
        import comfyui_h3_context.core as core
        from comfyui_h3_context.core.public_manifest_v2 import (
            PUBLIC_MANIFEST_V2_SCHEMA,
            PublicManifestV2,
            RuntimeNodeFacts,
            project_public_manifest_v1,
        )

        expected: dict[str, Any] = {
            "PUBLIC_MANIFEST_V2_SCHEMA": PUBLIC_MANIFEST_V2_SCHEMA,
            "PublicManifestV2": PublicManifestV2,
            "RuntimeNodeFacts": RuntimeNodeFacts,
            "project_public_manifest_v1": project_public_manifest_v1,
        }
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertIn(name, core.__all__)
                self.assertIs(getattr(core, name), value)

    def test_the_rendered_core_init_is_byte_identical(self) -> None:
        self.assertEqual(
            TOOL.render_core_init(), TOOL.CORE_INIT.read_bytes().replace(b"\r\n", b"\n")
        )

    def test_only_the_genuinely_outstanding_cohort_remains(self) -> None:
        from scripts import architecture_inventory

        cohort_ids = tuple(item[0] for item in architecture_inventory.COHORTS)
        self.assertEqual(cohort_ids, ("frontend_contract_owners",))


if __name__ == "__main__":
    unittest.main()
