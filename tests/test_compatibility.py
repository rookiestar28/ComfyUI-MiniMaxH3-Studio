"""M7-06 compatibility, unsupported-profile, and co-installation contract tests."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from comfyui_h3_context.core import (
    COMPATIBILITY_MATRIX_SCHEMA,
    CompatibilityAssessment,
    CompatibilityMatrix,
    CompatibilityObservation,
    CompatibilityStatus,
    CompatibilityVersion,
    PlatformFamily,
    assess_compatibility,
    default_compatibility_matrix,
)
from comfyui_h3_context.core.errors import CompatibilityError, RegistrationConflictError
from comfyui_h3_context.registration import register_nodes

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "governance" / "contracts" / "compatibility_matrix_v1.schema.json"
PACKAGE_MATRIX_PATH = ROOT / "governance" / "contracts" / "compatibility_matrix_v1.json"
MATRIX_PATH = ROOT / "compatibility" / "compatibility_matrix_v1.json"
SCRIPT_PATH = ROOT / "scripts" / "m7_06_compatibility.py"


def observation_for(profile_id: str) -> CompatibilityObservation:
    return CompatibilityObservation(
        profile_id=profile_id,
        platform=PlatformFamily.LINUX_WSL,
        python_version=CompatibilityVersion(3, 10, 12),
        host_version=CompatibilityVersion(0, 32, 0),
        host_revision="b323a345bbbfb2f3a95b5b73b68eb7919a26515e",  # pragma: allowlist secret
        dependencies=(
            ("comfy-aimdo", CompatibilityVersion(0, 4, 11)),
            ("comfy-kitchen", CompatibilityVersion(0, 2, 26)),
            ("torch", CompatibilityVersion(2, 5, 1)),
            ("transformers", CompatibilityVersion(4, 50, 3)),
        ),
        native_node_ids=("MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"),
        optional_imports=(),
        foreign_registration_collisions=(),
    )


class CompatibilityContractTests(unittest.TestCase):
    def test_default_matrix_is_versioned_and_fingerprinted(self) -> None:
        matrix = default_compatibility_matrix()
        self.assertIsInstance(matrix, CompatibilityMatrix)
        self.assertEqual(matrix.schema, COMPATIBILITY_MATRIX_SCHEMA)
        self.assertGreaterEqual(len(matrix.profiles), 2)
        self.assertEqual(
            {profile.platform for profile in matrix.profiles},
            {PlatformFamily.LINUX_WSL, PlatformFamily.WINDOWS, PlatformFamily.ANY},
        )
        self.assertEqual(matrix.fingerprint, default_compatibility_matrix().fingerprint)
        encoded = json.dumps(matrix.to_wire(), sort_keys=True)
        self.assertNotIn("/mnt/", encoded)
        self.assertNotIn("authorization", encoded.casefold())

    def test_exact_pinned_profile_passes_and_wire_is_json_safe(self) -> None:
        assessment = assess_compatibility(
            default_compatibility_matrix(), observation_for("comfyui-reference-0.32.0")
        )
        self.assertIsInstance(assessment, CompatibilityAssessment)
        self.assertEqual(assessment.status, CompatibilityStatus.SUPPORTED)
        self.assertEqual(assessment.diagnostics, ())
        self.assertEqual(assessment.to_wire()["status"], "supported")
        self.assertRegex(assessment.fingerprint, r"^sha256:[0-9a-f]{64}$")

    def test_host_identity_is_provenance_but_missing_capabilities_fail_closed(self) -> None:
        observation = observation_for("comfyui-reference-0.32.0")
        mismatched = CompatibilityObservation(
            profile_id=observation.profile_id,
            platform=observation.platform,
            python_version=observation.python_version,
            host_version=observation.host_version,
            host_revision="0" * 40,
            dependencies=observation.dependencies,
            native_node_ids=("MiniMaxH3ImageToVideo",),
            optional_imports=("torch",),
            foreign_registration_collisions=("Native.Foreign",),
        )
        assessment = assess_compatibility(default_compatibility_matrix(), mismatched)
        self.assertEqual(assessment.status, CompatibilityStatus.UNSUPPORTED)
        codes = {diagnostic.code for diagnostic in assessment.diagnostics}
        self.assertEqual(
            codes,
            {
                "native_node_missing",
                "optional_import_leak",
                "foreign_registration_collision",
            },
        )
        self.assertTrue(all("/" not in diagnostic.message for diagnostic in assessment.diagnostics))

    def test_different_host_identity_does_not_refuse_observed_capabilities(self) -> None:
        observation = observation_for("comfyui-reference-0.32.0")
        changed = CompatibilityObservation(
            profile_id=observation.profile_id,
            platform=observation.platform,
            python_version=observation.python_version,
            host_version=CompatibilityVersion(0, 33, 0),
            host_revision="0" * 40,
            dependencies=observation.dependencies,
            native_node_ids=observation.native_node_ids,
            optional_imports=observation.optional_imports,
            foreign_registration_collisions=observation.foreign_registration_collisions,
        )
        assessment = assess_compatibility(default_compatibility_matrix(), changed)
        self.assertEqual(assessment.status, CompatibilityStatus.SUPPORTED)
        self.assertEqual(assessment.diagnostics, ())

    def test_python_and_torch_dependency_constraints_fail_closed(self) -> None:
        observation = CompatibilityObservation(
            profile_id="comfyui-reference-0.32.0",
            platform=PlatformFamily.LINUX_WSL,
            python_version=CompatibilityVersion(3, 9, 18),
            host_version=CompatibilityVersion(0, 32, 0),
            host_revision="b323a345bbbfb2f3a95b5b73b68eb7919a26515e",  # pragma: allowlist secret
            dependencies=(
                ("comfy-aimdo", CompatibilityVersion(0, 4, 10)),
                ("comfy-kitchen", CompatibilityVersion(0, 2, 26)),
                ("torch", CompatibilityVersion(2, 3, 1)),
                ("transformers", CompatibilityVersion(4, 50, 3)),
            ),
            native_node_ids=("MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"),
            optional_imports=(),
            foreign_registration_collisions=(),
        )
        assessment = assess_compatibility(default_compatibility_matrix(), observation)
        self.assertEqual(assessment.status, CompatibilityStatus.UNSUPPORTED)
        self.assertEqual(
            {diagnostic.code for diagnostic in assessment.diagnostics},
            {"python_below_minimum", "dependency_exact_mismatch", "dependency_below_minimum"},
        )

    def test_malformed_contract_and_secret_like_values_are_rejected(self) -> None:
        with self.assertRaises(CompatibilityError):
            CompatibilityVersion.from_wire("v3.10.0")
        with self.assertRaises(CompatibilityError):
            CompatibilityObservation(
                profile_id="bad/path",
                platform=PlatformFamily.LINUX_WSL,
                python_version=CompatibilityVersion(3, 10, 0),
                host_version=None,
                host_revision=None,
                dependencies=(),
                native_node_ids=(),
                optional_imports=(),
                foreign_registration_collisions=(),
            )
        with self.assertRaises(CompatibilityError):
            CompatibilityObservation(
                profile_id="safe",
                platform=PlatformFamily.LINUX_WSL,
                python_version=CompatibilityVersion(3, 10, 0),
                host_version=None,
                host_revision=None,
                dependencies=(("api_key", CompatibilityVersion(1, 0, 0)),),
                native_node_ids=(),
                optional_imports=(),
                foreign_registration_collisions=(),
            )

    def test_schema_and_public_matrix_match_python_contract(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        packaged_matrix = json.loads(PACKAGE_MATRIX_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/compatibility_matrix_v1.schema.json"
        )
        self.assertEqual(schema["properties"]["schema"]["const"], COMPATIBILITY_MATRIX_SCHEMA)
        self.assertEqual(matrix["schema"], COMPATIBILITY_MATRIX_SCHEMA)
        self.assertGreaterEqual(len(matrix["profiles"]), 2)
        self.assertEqual(matrix, default_compatibility_matrix().to_wire())
        self.assertEqual(packaged_matrix, matrix)

    def test_core_compatibility_module_has_no_optional_runtime_imports(self) -> None:
        path = ROOT / "comfyui_h3_context" / "core" / "compatibility.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        forbidden = {"aiohttp", "comfy", "comfy_api", "requests", "torch", "transformers"}
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".", 1)[0])
        self.assertTrue(forbidden.isdisjoint(imports), imports)

    def test_coinstalled_foreign_nodes_survive_and_collision_fails_atomically(self) -> None:
        foreign = object()
        nodes: dict[str, object] = {"Foreign.Node": foreign}
        displays = {"Foreign.Node": "Foreign"}
        register_nodes(nodes, displays)
        self.assertIs(nodes["Foreign.Node"], foreign)
        before_nodes = dict(nodes)
        before_displays = dict(displays)
        nodes["comfyui_h3_context.H3Context.Request"] = foreign
        with self.assertRaises(RegistrationConflictError):
            register_nodes(nodes, displays)
        self.assertIs(nodes["Foreign.Node"], foreign)
        self.assertEqual(displays, before_displays)
        self.assertEqual(set(nodes) - set(before_nodes), set())

    def test_read_only_probe_script_has_explicit_negative_lane(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--profile", "unsupported-host-drift", "--json"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "unsupported")
        self.assertIn("diagnostics", payload)
        self.assertIn("unsupported_profile", {item["code"] for item in payload["diagnostics"]})

    @unittest.skipUnless(
        os.environ.get("H3_CONTEXT_HOST_ROOT"),
        "requires an explicitly supplied pinned ComfyUI host",
    )
    def test_pinned_registration_smoke_exercises_product_and_probe_mappings(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "registration_smoke.py")],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "PASS")
        self.assertGreaterEqual(len(payload["registered_ids"]), 10)
        self.assertTrue(payload["optional_dependencies_absent"])
        self.assertTrue(payload["unrelated_entry_preserved"])


if __name__ == "__main__":
    unittest.main()
