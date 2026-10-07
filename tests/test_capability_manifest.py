"""M10-01 capability maturity and backend-owned binding manifest tests."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from comfyui_h3_context.core.capability_manifest import (
    CAPABILITY_MANIFEST_SCHEMA,
    BindingKind,
    CapabilityMaturity,
    HostCanaryOutcome,
    HostCanarySeam,
    build_default_binding_manifest,
    build_host_canary_report,
    default_capability_registry,
)
from comfyui_h3_context.core.errors import CapabilityManifestError
from comfyui_h3_context.core.node_contracts import default_node_contract_registry
from comfyui_h3_context.nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

ROOT = Path(__file__).resolve().parents[1]


class CapabilityManifestTests(unittest.TestCase):
    def test_json_schema_artifact_matches_manifest_version(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "capability_manifest_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["properties"]["schema"]["const"], CAPABILITY_MANIFEST_SCHEMA)
        self.assertIn("Binding", schema["$defs"])

    def test_all_twenty_eight_public_registrations_reconcile_in_one_manifest(self) -> None:
        manifest = build_default_binding_manifest(
            registration_ids=tuple(NODE_CLASS_MAPPINGS),
            display_names=NODE_DISPLAY_NAME_MAPPINGS,
        )
        self.assertEqual(manifest.schema, CAPABILITY_MANIFEST_SCHEMA)
        self.assertEqual(set(manifest.node_ids), set(NODE_CLASS_MAPPINGS))
        self.assertEqual(len(manifest.node_ids), 28)
        self.assertGreaterEqual(len(manifest.bindings), 40)
        self.assertTrue(all(item.field_id for item in manifest.bindings))
        self.assertEqual(
            {item.binding_kind for item in manifest.bindings},
            {BindingKind.INPUT, BindingKind.OUTPUT},
        )
        self.assertEqual(json.loads(json.dumps(manifest.to_wire())), manifest.to_wire())

    def test_manifest_rejects_registration_drift_before_mutation(self) -> None:
        registry = default_node_contract_registry()
        with self.assertRaises(CapabilityManifestError):
            build_default_binding_manifest(
                registry=registry,
                registration_ids=tuple(NODE_CLASS_MAPPINGS)[:-1],
                display_names=NODE_DISPLAY_NAME_MAPPINGS,
            )

    def test_capability_maturity_is_closed_and_explicit(self) -> None:
        registry = default_capability_registry()
        self.assertTrue(registry.get("node.request").maturity is CapabilityMaturity.DETERMINISTIC)
        semantic = registry.get("node.semanticproposalproducer")
        self.assertIs(semantic.maturity, CapabilityMaturity.CONCRETE_EXECUTABLE)
        self.assertEqual(semantic.provider_family, "ollama")
        self.assertTrue(semantic.requires_network)
        self.assertTrue(
            registry.get("provider.official_oracle").maturity is CapabilityMaturity.UNSUPPORTED
        )
        self.assertTrue(
            registry.get("perception.image_observation").maturity
            is CapabilityMaturity.INJECTED_ONLY
        )
        self.assertIs(
            registry.get("adapter.comfyui_native_generation").maturity,
            CapabilityMaturity.INJECTED_ONLY,
        )
        self.assertIs(
            registry.get("adapter.ollama_fallback").maturity,
            CapabilityMaturity.INJECTED_ONLY,
        )
        with self.assertRaises(CapabilityManifestError):
            registry.get("missing.capability")

    def test_host_canary_records_missing_public_seams_without_claiming_support(self) -> None:
        report = build_host_canary_report(
            "comfyui-0.32.0/frontend-1.48.7",
            {seam: (HostCanaryOutcome.UNSUPPORTED, "not_implemented") for seam in HostCanarySeam},
        )
        self.assertEqual(report.status, HostCanaryOutcome.UNSUPPORTED)
        self.assertEqual(len(report.findings), len(tuple(HostCanarySeam)))
        self.assertTrue(
            all(item.outcome is HostCanaryOutcome.UNSUPPORTED for item in report.findings)
        )

    def test_offline_canary_cli_is_deterministic_and_network_free(self) -> None:
        command = [
            sys.executable,
            str(ROOT / "scripts" / "m10_01_host_canary.py"),
            "--json",
        ]
        first = subprocess.run(command, check=True, capture_output=True, text=True)
        second = subprocess.run(command, check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(first.stdout), json.loads(second.stdout))
        payload = json.loads(first.stdout)
        self.assertEqual(payload["status"], "unsupported")
        self.assertEqual(payload["schema"], "h3.host.canary.v1")


if __name__ == "__main__":
    unittest.main()
