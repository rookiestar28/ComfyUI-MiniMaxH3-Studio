"""HC-11 package compatibility and deprecation policy contract tests.

The Markdown policy is intentionally outside the test contract.  These tests pin only the shipped
machine record, its closed schema, and joins to the live Python surface authorities.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

import comfyui_h3_context.core as core
from comfyui_h3_context import public_api
from scripts import package_compatibility_policy as policy_validator

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "governance" / "contracts"
ARTIFACT = CONTRACTS / "package_compatibility_policy_v1.json"
SCHEMA = CONTRACTS / "package_compatibility_policy_v1.schema.json"
PUBLIC_SURFACE = CONTRACTS / "public_surface_v1.json"
ACCEPTANCE_BASELINE = ROOT / "tests" / "acceptance_baseline.json"
FINGERPRINT_DOMAINS = CONTRACTS / "fingerprint_domain_v1.json"


class ShippedPackageCompatibilityPolicyTests(unittest.TestCase):
    document: dict[str, Any]
    schema: dict[str, Any]
    validator: Draft202012Validator

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def _errors(self, candidate: dict[str, Any]) -> list[str]:
        return [error.message for error in self.validator.iter_errors(candidate)]

    def test_the_shipped_record_passes_its_closed_schema(self) -> None:
        self.assertEqual(self._errors(self.document), [])
        self.assertEqual(self.document["schema"], "h3.context.package_compatibility_policy.v1")
        self.assertEqual(self.document["version"], "1.0.0")

    def test_exactly_one_consumer_authority_matches_the_live_package_facade(self) -> None:
        authority = self.document["consumer_authority"]
        self.assertEqual(
            {
                "authority_id": authority["authority_id"],
                "path": authority["path"],
                "selector": authority["selector"],
                "relation": authority["relation"],
            },
            {
                "authority_id": "package",
                "path": "comfyui_h3_context/public_api.py",
                "selector": "__all__",
                "relation": "supported_python_consumer_surface",
            },
        )
        self.assertEqual(authority["names"], sorted(public_api.__all__))
        self.assertEqual(len(authority["names"]), len(set(authority["names"])))
        self.assertEqual([name for name in authority["names"] if not hasattr(public_api, name)], [])

    def test_supporting_authorities_retain_the_accepted_three_level_model(self) -> None:
        surface = json.loads(PUBLIC_SURFACE.read_text(encoding="utf-8"))
        surface_by_id = {row["authority_id"]: row for row in surface["authorities"]}
        support_by_id = {
            row["authority_id"]: row for row in self.document["supporting_authorities"]
        }
        self.assertEqual(set(support_by_id), {"acceptance_abi", "pure_core"})
        self.assertEqual(
            support_by_id["acceptance_abi"],
            {
                "authority_id": "acceptance_abi",
                "path": "tests/acceptance_baseline.json",
                "selector": "public_python_abi[*].export",
                "relation": "gate_enforced_pure_core_subset",
            },
        )
        self.assertEqual(
            support_by_id["pure_core"],
            {
                "authority_id": "pure_core",
                "path": "comfyui_h3_context/core/__init__.py",
                "selector": "__all__",
                "relation": "implementation_export_census",
            },
        )
        self.assertEqual(surface_by_id["package"]["level"], "package")
        self.assertEqual(surface_by_id["pure_core"]["level"], "pure_core")
        self.assertEqual(surface_by_id["acceptance_abi"]["level"], "gate_enforced")

    def test_acceptance_abi_remains_a_subset_of_the_pure_core_census(self) -> None:
        baseline = json.loads(ACCEPTANCE_BASELINE.read_text(encoding="utf-8"))
        abi_names = {row["export"] for row in baseline["public_python_abi"]}
        self.assertLessEqual(abi_names, set(core.__all__))
        self.assertEqual([name for name in abi_names if not hasattr(core, name)], [])

    def test_protected_boundaries_have_one_symbolic_semver_classification(self) -> None:
        classifications = self.document["change_classification"]
        self.assertEqual(
            set(classifications),
            {
                "contract_schema",
                "emitted_prompt_shape",
                "emitted_report_shape",
                "node_identifier_and_socket",
                "python_consumer_api",
            },
        )
        for boundary, rule in classifications.items():
            with self.subTest(boundary=boundary):
                self.assertEqual(
                    rule,
                    {
                        "breaking": "major",
                        "backward_compatible_addition": "minor",
                        "compatible_fix": "patch",
                    },
                )

    def test_deprecation_window_is_explicit_and_starts_empty(self) -> None:
        policy = self.document["deprecation_policy"]
        self.assertEqual(policy["minimum_successor_releases"], 2)
        self.assertIs(policy["removal_requires_major"], True)
        self.assertEqual(policy["announcement_channels"], ["in_product", "release_notes"])
        self.assertEqual(policy["deprecations"], [])

    def test_the_policy_record_is_release_integrity_not_a_runtime_identity(self) -> None:
        assignments = json.loads(FINGERPRINT_DOMAINS.read_text(encoding="utf-8"))["assignments"]
        by_id = {row["consumer_id"]: row for row in assignments}
        policy = by_id["h3.context.package_compatibility_policy.v1"]
        self.assertEqual(policy["domain"], "release_integrity")
        self.assertEqual(policy["rule"], "declared_contract_domain")

    def test_unknown_fields_and_malformed_versions_are_rejected(self) -> None:
        extra = copy.deepcopy(self.document)
        extra["consumer_authority"]["second_public_surface"] = True
        malformed = copy.deepcopy(self.document)
        malformed["version"] = "1.0"
        for label, candidate in (("unknown", extra), ("version", malformed)):
            with self.subTest(case=label):
                self.assertTrue(self._errors(candidate))

    def test_deprecation_state_and_removal_window_fail_closed(self) -> None:
        template = {
            "notice_code": "H3-DEPRECATION-001",
            "boundary": "python_consumer_api",
            "identifier": "example_name",
            "state": "active",
            "announced_in": "1.2.0",
            "announcement_channels": ["in_product", "release_notes"],
            "replacement": "replacement_name",
            "no_replacement_reason": None,
            "successor_releases": [],
            "removal_version": None,
        }

        invalid_state = copy.deepcopy(self.document)
        invalid_state["deprecation_policy"]["deprecations"] = [template | {"state": "retired"}]

        active_with_removal = copy.deepcopy(self.document)
        active_with_removal["deprecation_policy"]["deprecations"] = [
            template | {"removal_version": "2.0.0"}
        ]

        removed_too_soon = copy.deepcopy(self.document)
        removed_too_soon["deprecation_policy"]["deprecations"] = [
            template
            | {
                "state": "removed",
                "successor_releases": ["1.3.0"],
                "removal_version": "2.0.0",
            }
        ]

        duplicate = copy.deepcopy(self.document)
        duplicate["deprecation_policy"]["deprecations"] = [template, copy.deepcopy(template)]

        for label, candidate in (
            ("invalid state", invalid_state),
            ("active removal", active_with_removal),
            ("under-window removal", removed_too_soon),
            ("duplicate entry", duplicate),
        ):
            with self.subTest(case=label):
                self.assertTrue(self._errors(candidate))

    def test_semantic_validator_accepts_a_chronological_later_major_removal(self) -> None:
        candidate = copy.deepcopy(self.document)
        candidate["deprecation_policy"]["deprecations"] = [
            {
                "notice_code": "H3-DEPRECATION-001",
                "boundary": "python_consumer_api",
                "identifier": "example_name",
                "state": "removed",
                "announced_in": "1.2.0",
                "announcement_channels": ["in_product", "release_notes"],
                "replacement": "replacement_name",
                "no_replacement_reason": None,
                "successor_releases": ["1.3.0", "1.4.0"],
                "removal_version": "2.0.0",
            }
        ]
        policy_validator.validate_policy(candidate, schema=self.schema)

    def test_semantic_validator_rejects_chronology_major_and_notice_code_gaps(self) -> None:
        entry = {
            "notice_code": "H3-DEPRECATION-001",
            "boundary": "python_consumer_api",
            "identifier": "example_name",
            "state": "removed",
            "announced_in": "1.2.0",
            "announcement_channels": ["in_product", "release_notes"],
            "replacement": "replacement_name",
            "no_replacement_reason": None,
            "successor_releases": ["1.3.0", "1.4.0"],
            "removal_version": "2.0.0",
        }
        cases: list[tuple[str, list[dict[str, Any]]]] = [
            ("reversed successors", [entry | {"successor_releases": ["1.4.0", "1.3.0"]}]),
            ("successor before announcement", [entry | {"successor_releases": ["1.1.0", "1.3.0"]}]),
            (
                "removal not after successors",
                [entry | {"successor_releases": ["1.3.0", "2.0.0"]}],
            ),
            ("same-major removal", [entry | {"removal_version": "1.5.0"}]),
            ("non-boundary major version", [entry | {"removal_version": "2.1.0"}]),
            (
                "duplicate notice code",
                [entry, entry | {"identifier": "another_name"}],
            ),
        ]
        for label, entries in cases:
            candidate = copy.deepcopy(self.document)
            candidate["deprecation_policy"]["deprecations"] = entries
            with self.subTest(case=label):
                with self.assertRaises(policy_validator.PackageCompatibilityPolicyError):
                    policy_validator.validate_policy(candidate, schema=self.schema)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
