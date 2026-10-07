"""Final cross-layer truth for optional assisted authoring.

The matrix is a generated report over existing authorities. It must never become a second provider
catalog or an authorization source, and it carries identifiers and bounds rather than prompts,
credentials, provider bodies, paths or URLs.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

import jsonschema

from comfyui_h3_context.core.capability_manifest import CapabilityMaturity
from scripts import m22_16_compatibility_matrix as generator

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "governance" / "contracts" / "assisted_authoring_compatibility_v2.json"
SCHEMA = ARTIFACT.with_name("assisted_authoring_compatibility_v2.schema.json")


class GeneratedCompatibilityMatrixTests(unittest.TestCase):
    document: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(ARTIFACT.read_text(encoding="utf-8"))

    def test_artifact_regenerates_byte_identically_and_passes_its_closed_schema(self) -> None:
        matrix = generator.build_matrix()
        self.assertEqual(generator.artifact_bytes(matrix), ARTIFACT.read_bytes())
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(self.document))
        self.assertEqual([error.message for error in errors], [])
        generator.validate_matrix(self.document)

    def test_exact_profile_truth_is_joined_without_turning_catalog_presence_into_authority(
        self,
    ) -> None:
        self.assertIsNone(self.document["manual_default"]["profile_id"])
        self.assertEqual(
            self.document["manual_default"]["assisted_authoring"],
            {
                "available": True,
                "selected": False,
                "ready": False,
                "authorized_for_this_action": False,
                "defaulted": False,
            },
        )
        self.assertEqual(self.document["manual_default"]["network_observation"], "none")
        self.assertEqual(self.document["manual_default"]["canonical_owner"], "manual_prompt")

        profiles = {row["profile_id"]: row for row in self.document["profiles"]}
        self.assertEqual(
            set(profiles),
            {
                "ollama.local",
                "openai.remote",
                "gemini.remote",
                "anthropic.remote",
            },
        )
        self.assertEqual(
            {profile_id: row["qualification_state"] for profile_id, row in profiles.items()},
            {
                "ollama.local": "qualified",
                "openai.remote": "catalog_only",
                "gemini.remote": "qualified",
                "anthropic.remote": "catalog_only",
            },
        )
        for profile_id, row in profiles.items():
            with self.subTest(profile=profile_id):
                qualified = row["qualification_state"] == "qualified"
                self.assertIs(row["executable"], False)
                self.assertIs(row["model_selection_required"], True)
                self.assertNotIn("model_id", row)
                self.assertIs(row["evidence_fingerprint"] is not None, qualified)
                self.assertEqual(row["manual_fallback"], "canonical")
                self.assertFalse(row["defaulted"])
        # The limitation is the honest half of the same fact the state above states: a row that
        # has not been activated says so, and a row that has says nothing of the kind. Deriving the
        # expectation rather than listing profile ids keeps the two from drifting apart the next
        # time one is promoted -- which is exactly how this assertion first went stale.
        for profile_id, row in profiles.items():
            with self.subTest(profile=profile_id):
                pending = "remote_activation_pending" in row["limitations"]
                remote = row["family"].startswith("remote_")
                self.assertIs(pending, remote and row["qualification_state"] == "catalog_only")

    def test_capability_and_installation_owners_match_each_profile(self) -> None:
        expected = {
            "ollama.local": (
                "provider.prompt_model_ollama",
                CapabilityMaturity.INJECTED_ONLY.value,
                "external_ollama",
                False,
            ),
            "openai.remote": (
                "provider.prompt_model_remote_openai_compatible",
                CapabilityMaturity.INJECTED_ONLY.value,
                "remote_openai_compatible_service",
                True,
            ),
            "gemini.remote": (
                "provider.prompt_model_remote_openai_compatible",
                CapabilityMaturity.INJECTED_ONLY.value,
                "remote_openai_compatible_service",
                True,
            ),
            "anthropic.remote": (
                "provider.prompt_model_remote_anthropic",
                CapabilityMaturity.INJECTED_ONLY.value,
                "remote_anthropic_service",
                True,
            ),
        }
        for row in self.document["profiles"]:
            with self.subTest(profile=row["profile_id"]):
                capability, maturity, installation, consent = expected[row["profile_id"]]
                self.assertEqual(row["capability_id"], capability)
                self.assertEqual(row["capability_maturity"], maturity)
                self.assertEqual(row["installation_profile_id"], installation)
                self.assertIs(row["requires_consent"], consent)

    def test_host_public_workflow_and_review_owners_are_exact_and_bounded(self) -> None:
        self.assertEqual(
            self.document["host_subject"],
            {
                "fixture_schema": "h3.context.host_seam_shape_fixture.v1",
                "fixture_profile": "comfyui_host_seams_v1",
                "comfyui_version": "0.38.0",
                "comfyui_revision": "8cfe5e1ecb97512dea8deaac15e1228d7e6feeb1",
                "frontend_version": "1.53.6",
                "seam_count": 29,
            },
        )
        public = self.document["public_contract"]
        self.assertEqual(public["node_count"], 28)
        self.assertEqual(public["workflow_modes"], ["t2va", "i2va", "fl2va", "l2va", "ref2va"])
        self.assertRegex(public["node_ids_fingerprint"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(public["workflow_surface_fingerprint"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(self.document["locales"], ["en", "zh-TW", "zh-CN"])
        self.assertEqual(
            self.document["proposal_review"],
            {
                "proposal_schema": "h3.context.assisted_prompt_proposal.v1",
                "max_attempts": 2,
                "canonical_mutations": ["accept", "validated_edit"],
                "non_mutating_outcomes": ["reject", "cancel", "failure"],
            },
        )

    def test_semantic_validator_rejects_overclaim_duplicate_extra_and_private_fields(self) -> None:
        cases: list[tuple[str, dict[str, Any]]] = []

        overclaim = copy.deepcopy(self.document)
        # A connection-only report never owns an executable model, even when qualified.
        overclaim["profiles"][0]["executable"] = True
        with self.assertRaises(generator.CompatibilityMatrixError) as caught:
            generator.validate_matrix(generator.reseal(copy.deepcopy(overclaim)))
        self.assertEqual("profile_qualification", str(caught.exception))
        cases.append(("catalog overclaim", generator.reseal(overclaim)))

        duplicate = copy.deepcopy(self.document)
        duplicate["profiles"].append(copy.deepcopy(duplicate["profiles"][0]))
        cases.append(("duplicate", generator.reseal(duplicate)))

        extra = copy.deepcopy(self.document)
        extra["profiles"][0]["endpoint"] = "https://example.invalid"
        cases.append(("extra field", generator.reseal(extra)))

        private = copy.deepcopy(self.document)
        private["profiles"][0]["limitations"] = ["token=private"]
        cases.append(("private text", generator.reseal(private)))

        for label, candidate in cases:
            with self.subTest(case=label):
                with self.assertRaises(generator.CompatibilityMatrixError):
                    generator.validate_matrix(candidate)

    def test_generator_check_command_is_green(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/m22_16_compatibility_matrix.py", "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
