"""Governed human-review terminal contract tests."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator

from comfyui_h3_context.core import human_review as review

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "governance" / "contracts" / "human_review_v1.schema.json"
FIXTURE = ROOT / "tests" / "fixtures" / "m14_06_human_review.json"
GENERATOR = ROOT / "scripts" / "m14_06_human_review_fixture.py"


class ArmedString(str):
    equality_calls = 0

    def __eq__(self, other: object) -> bool:
        type(self).equality_calls += 1
        raise RuntimeError("attacker equality must not execute")

    __hash__ = str.__hash__


class HumanReviewContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = review.build_human_review_terminal_record()
        self.wire = self.record.to_wire()

    def test_governed_human_review_contract_module_exists(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("comfyui_h3_context.core.human_review"),
            "the governed human-review terminal contract is missing",
        )

    def test_public_contract_symbols_exist(self) -> None:
        expected = {
            "HUMAN_REVIEW_SCHEMA",
            "HUMAN_REVIEW_RECORD_DATE",
            "FROZEN_HUMAN_REVIEW_TERMINAL_FINGERPRINT",
            "M14_05_FIXED_H3_TERMINAL_FINGERPRINT",
            "HumanReviewError",
            "HumanReviewDisposition",
            "HumanReviewEvidenceClass",
            "HumanReviewClaimCap",
            "HumanReviewEvidenceStatus",
            "HumanReviewGovernanceRequirement",
            "HumanReviewFailureCategory",
            "HumanReviewGovernanceReceipt",
            "HumanReviewProtocol",
            "HumanReviewAdjudicationRule",
            "HumanReviewTerminalRecord",
            "build_human_review_terminal_record",
            "validate_human_review_terminal_wire",
            "decode_human_review_terminal_json",
        }
        self.assertEqual(expected - set(dir(review)), set())

    def test_exact_unavailable_disposition_and_predecessor_join(self) -> None:
        record = self.record
        self.assertEqual(record.schema, "h3.human_review.terminal.v1")
        self.assertEqual(record.record_date, "2026-08-09")
        self.assertIs(record.disposition, review.HumanReviewDisposition.UNAVAILABLE)
        self.assertIs(record.evidence_class, review.HumanReviewEvidenceClass.NONE)
        self.assertIs(record.claim_cap, review.HumanReviewClaimCap.NO_HUMAN_QUALITY_CLAIM)
        self.assertEqual(
            record.fixed_h3_terminal_fingerprint,
            review.M14_05_FIXED_H3_TERMINAL_FINGERPRINT,
        )
        self.assertEqual(record.fixed_h3_disposition, "UNAVAILABLE")
        self.assertEqual(record.fixed_h3_claim_cap, "NO_FIXED_H3_GENERATION_EVIDENCE")

    def test_governance_inventory_is_closed_and_missing(self) -> None:
        expected = (
            "informed_consent",
            "compensation_or_volunteer_terms",
            "reviewer_pii_retention_deletion",
            "response_retention_deletion",
            "language_qualification",
            "task_qualification",
            "restricted_media_access",
            "conflicts",
            "exclusion",
            "withdrawal",
            "accessibility",
            "fatigue_session_limits",
            "order_learning_controls",
            "human_subjects_ethics_review",
        )
        self.assertEqual(
            tuple(item.value for item in review.HumanReviewGovernanceRequirement), expected
        )
        self.assertEqual(
            tuple(receipt.requirement.value for receipt in self.record.governance_receipts),
            expected,
        )
        for receipt in self.record.governance_receipts:
            self.assertIs(receipt.status, review.HumanReviewEvidenceStatus.MISSING)
            self.assertFalse(receipt.authority_granted)
            self.assertEqual(
                receipt.reason,
                f"{receipt.requirement.value}_authority_unavailable",
            )
            self.assertEqual(receipt.evidence_receipts, ())

    def test_protocol_freezes_review_design_and_threshold_floors_without_results(self) -> None:
        protocol = self.record.protocol
        self.assertEqual(protocol.schema, "h3.human_review.protocol.v1")
        self.assertEqual(protocol.protocol_date, "2026-08-09")
        self.assertFalse(protocol.study_authorized)
        self.assertTrue(protocol.blinded_required)
        self.assertTrue(protocol.randomized_order_required)
        self.assertTrue(protocol.pairwise_tie_required)
        self.assertTrue(protocol.balanced_candidate_position_required)
        self.assertTrue(protocol.swapped_order_controls_required)
        self.assertEqual(protocol.qualification_anchor_class, "deterministic_p0")
        self.assertTrue(protocol.reviewer_source_uncertainty_required)
        self.assertEqual(protocol.planned_power_floor_basis_points, 8000)
        self.assertEqual(protocol.qualification_score_floor_basis_points, 9000)
        self.assertEqual(protocol.swapped_order_consistency_floor_basis_points, 9500)
        self.assertTrue(protocol.tie_adjusted_noninferiority_required)
        self.assertTrue(protocol.two_sided_equivalence_required)
        self.assertTrue(protocol.separate_comparison_reporting_required)
        self.assertIsNone(protocol.noninferiority_margin)
        self.assertIsNone(protocol.equivalence_margin)
        self.assertIsNone(protocol.observed_power)
        self.assertIsNone(protocol.observed_qualification_score)
        self.assertIsNone(protocol.observed_swapped_order_consistency)

    def test_protocol_contains_a_closed_versioned_rubric_and_uncertainty_method(self) -> None:
        protocol = self.record.protocol
        self.assertEqual(protocol.rubric_schema, "h3.human_review.rubric.v1")
        self.assertEqual(protocol.rubric_version, "1")
        self.assertEqual(
            protocol.rubric_outcomes,
            ("left_preferred", "tie", "right_preferred"),
        )
        self.assertEqual(
            protocol.rubric_dimensions,
            (
                "instruction_adherence",
                "identity_consistency",
                "temporal_coherence",
                "visual_fidelity",
                "audio_alignment",
                "hard_constraint_preservation",
            ),
        )
        self.assertEqual(protocol.uncertainty_method, "reviewer_source_clustered")
        self.assertEqual(protocol.candidate_position_target_basis_points, 5000)

    def test_failure_adjudication_inventory_is_closed_without_false_fixtures(self) -> None:
        expected = ("perception", "fusion", "planning", "rendering", "provider", "h3")
        self.assertEqual(tuple(item.value for item in review.HumanReviewFailureCategory), expected)
        self.assertEqual(
            tuple(rule.category.value for rule in self.record.adjudication_rules), expected
        )
        for rule in self.record.adjudication_rules:
            self.assertIs(rule.status, review.HumanReviewEvidenceStatus.MISSING)
            self.assertTrue(rule.privacy_safe_fixture_required)
            self.assertEqual(rule.failure_count, 0)
            self.assertEqual(rule.fixture_count, 0)
            self.assertEqual(rule.fixture_fingerprints, ())

    def test_unavailable_record_has_zero_side_effects_results_overrides_and_promotion(self) -> None:
        record = self.record
        false_fields = (
            "lawful_review_authority_granted",
            "qualified_reviewer_cohort_available",
            "ethics_review_complete",
            "restricted_media_access_granted",
            "recruitment_started",
            "reviewer_contacted",
            "study_started",
            "model_judge_used",
            "consent_collected",
            "pii_collected",
            "responses_collected",
            "compensation_paid",
            "media_accessed",
            "retention_started",
            "privacy_safe_fixture_created",
            "structural_failure_override_authorized",
            "security_failure_override_authorized",
            "promotion_authorized",
            "publication_authorized",
        )
        for field_name in false_fields:
            self.assertFalse(getattr(record, field_name), field_name)
        zero_fields = (
            "invited_reviewer_count",
            "enrolled_reviewer_count",
            "admitted_reviewer_count",
            "response_count",
            "pairwise_comparison_count",
            "qualification_anchor_response_count",
            "observed_failure_count",
            "privacy_safe_fixture_count",
            "retained_pii_count",
            "retained_response_count",
        )
        for field_name in zero_fields:
            self.assertEqual(getattr(record, field_name), 0, field_name)
        empty_fields = (
            "reviewer_ids",
            "consent_receipts",
            "response_receipts",
            "payment_receipts",
            "pii_receipts",
            "media_receipts",
            "fixture_fingerprints",
            "locators",
        )
        for field_name in empty_fields:
            self.assertEqual(getattr(record, field_name), (), field_name)
        none_fields = (
            "preference_estimate",
            "reviewer_source_variance",
            "achieved_power",
            "qualification_score",
            "swapped_order_consistency",
            "noninferiority_estimate",
            "noninferiority_interval",
            "noninferiority_conclusion",
            "equivalence_estimate",
            "equivalence_interval",
            "equivalence_conclusion",
        )
        for field_name in none_fields:
            self.assertIsNone(getattr(record, field_name), field_name)
        self.assertIn("no_human_quality_claim", record.limitations)
        self.assertIn("no_regression_promotion", record.limitations)

    def test_direct_construction_cannot_mint_derived_study_state(self) -> None:
        self.assertEqual(review.HumanReviewTerminalRecord().to_wire(), self.wire)
        with self.assertRaises(TypeError):
            review.HumanReviewTerminalRecord(study_started=True)  # type: ignore[call-arg]
        with self.assertRaises((TypeError, ValueError)):
            replace(self.record, promotion_authorized=True)
        receipt = review.HumanReviewGovernanceReceipt(
            review.HumanReviewGovernanceRequirement.INFORMED_CONSENT
        )
        with self.assertRaises((TypeError, ValueError)):
            replace(receipt, authority_granted=True)
        rule = review.HumanReviewAdjudicationRule(review.HumanReviewFailureCategory.PERCEPTION)
        with self.assertRaises((TypeError, ValueError)):
            replace(rule, failure_count=1)
        self.assertFalse(hasattr(review, "issue_human_review_evidence"))
        self.assertFalse(hasattr(review, "execute_human_review_study"))

    def test_production_validator_regenerates_exact_wire_and_rejects_semantic_drift(self) -> None:
        self.assertEqual(review.validate_human_review_terminal_wire(self.wire), self.record)
        attacks: list[dict[str, object]] = []
        for field_name, value in (
            ("unexpected", True),
            ("study_started", True),
            ("response_count", 1),
            ("preference_estimate", 0),
            ("promotion_authorized", True),
            ("security_failure_override_authorized", True),
            ("fixed_h3_terminal_fingerprint", "sha256:" + ("0" * 64)),
        ):
            mutated = dict(self.wire)
            mutated[field_name] = value
            attacks.append(mutated)

        governance = json.loads(json.dumps(self.wire))
        governance["governance_receipts"][0]["authority_granted"] = True
        attacks.append(governance)
        protocol = json.loads(json.dumps(self.wire))
        protocol["protocol"]["study_authorized"] = True
        attacks.append(protocol)
        threshold = json.loads(json.dumps(self.wire))
        threshold["protocol"]["planned_power_floor_basis_points"] = 7999
        attacks.append(threshold)
        adjudication = json.loads(json.dumps(self.wire))
        adjudication["adjudication_rules"][0]["failure_count"] = 1
        attacks.append(adjudication)

        for mutated in attacks:
            with self.subTest(mutated=mutated):
                with self.assertRaises(review.HumanReviewError):
                    review.validate_human_review_terminal_wire(mutated)

    def test_decoder_is_duplicate_aware_bounded_and_strict(self) -> None:
        encoded = json.dumps(self.wire, sort_keys=True, separators=(",", ":"))
        self.assertEqual(review.decode_human_review_terminal_json(encoded), self.record)
        self.assertEqual(review.decode_human_review_terminal_json(encoded.encode()), self.record)
        self.assertEqual(
            review.decode_human_review_terminal_json(bytearray(encoded.encode())), self.record
        )

        duplicate = encoded.replace(
            '"schema":"h3.human_review.terminal.v1"',
            '"schema":"h3.human_review.terminal.v1","schema":"h3.human_review.terminal.v1"',
            1,
        )
        payloads: tuple[object, ...] = (
            duplicate,
            "{" + ('"x":' + ('"' + ("a" * 40000) + '"')) + "}",
            '{"schema":NaN}',
            b"\xff",
            7,
        )
        for payload in payloads:
            with self.subTest(payload_type=type(payload).__name__):
                with self.assertRaises(review.HumanReviewError):
                    review.decode_human_review_terminal_json(payload)  # type: ignore[arg-type]

    def test_hostile_python_members_reject_before_caller_equality(self) -> None:
        ArmedString.equality_calls = 0
        hostile = dict(self.wire)
        schema = hostile.pop("schema")
        hostile[ArmedString("schema")] = schema
        with self.assertRaises(review.HumanReviewError):
            review.validate_human_review_terminal_wire(hostile)
        self.assertEqual(ArmedString.equality_calls, 0)

        for mutated in (
            {**self.wire, "response_count": True},
            {**self.wire, "response_count": 0.5},
            {**self.wire, "promotion_authorized": 0.0},
            {**self.wire, "limitations": tuple(cast(list[object], self.wire["limitations"]))},
        ):
            with self.assertRaises(review.HumanReviewError):
                review.validate_human_review_terminal_wire(mutated)

    def test_public_projections_revalidate_after_mutation(self) -> None:
        record = review.build_human_review_terminal_record()
        object.__setattr__(record, "study_started", True)
        for projection in (record.to_wire, record.to_wire_bytes, lambda: record.fingerprint):
            with self.assertRaises(review.HumanReviewError):
                projection()

        governance = review.HumanReviewGovernanceReceipt(
            review.HumanReviewGovernanceRequirement.INFORMED_CONSENT
        )
        object.__setattr__(governance, "authority_granted", True)
        with self.assertRaises(review.HumanReviewError):
            governance.to_wire()

        protocol = review.HumanReviewProtocol()
        object.__setattr__(protocol, "study_authorized", True)
        with self.assertRaises(review.HumanReviewError):
            protocol.to_wire()

        rule = review.HumanReviewAdjudicationRule(review.HumanReviewFailureCategory.PERCEPTION)
        object.__setattr__(rule, "failure_count", 1)
        with self.assertRaises(review.HumanReviewError):
            rule.to_wire()

    def test_separate_and_concurrent_builders_are_isolated_and_deterministic(self) -> None:
        first = review.build_human_review_terminal_record()
        second = review.build_human_review_terminal_record()
        self.assertIsNot(first.protocol, second.protocol)
        self.assertTrue(
            all(
                left is not right
                for left, right in zip(
                    first.governance_receipts, second.governance_receipts, strict=True
                )
            )
        )
        self.assertTrue(
            all(
                left is not right
                for left, right in zip(
                    first.adjudication_rules, second.adjudication_rules, strict=True
                )
            )
        )
        expected = second.to_wire()
        object.__setattr__(first.governance_receipts[0], "authority_granted", True)
        with self.assertRaises(review.HumanReviewError):
            first.to_wire()
        self.assertEqual(second.to_wire(), expected)
        self.assertEqual(review.build_human_review_terminal_record().to_wire(), expected)

        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(
                pool.map(lambda _: review.build_human_review_terminal_record(), range(32))
            )
        self.assertTrue(all(record.to_wire() == expected for record in records))
        self.assertEqual(len({id(record.protocol) for record in records}), 32)
        self.assertEqual(len({id(record.governance_receipts[0]) for record in records}), 32)
        self.assertEqual(len({id(record.adjudication_rules[0]) for record in records}), 32)

    def test_fingerprint_is_frozen_and_content_derived(self) -> None:
        self.assertEqual(self.record.fingerprint, review.FROZEN_HUMAN_REVIEW_TERMINAL_FINGERPRINT)
        self.assertRegex(self.record.fingerprint, r"^sha256:[0-9a-f]{64}$")

    def test_portable_assets_and_public_claim_ceiling_exist(self) -> None:
        for path in (SCHEMA, FIXTURE, GENERATOR):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), f"missing public artifact: {path}")

    def test_fixture_schema_and_generator_are_exact(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(fixture, self.wire)
        self.assertEqual(review.validate_human_review_terminal_wire(fixture), self.record)
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        self.assertEqual(list(validator.iter_errors(fixture)), [])
        for mutated in (
            {**fixture, "unexpected": True},
            {**fixture, "study_started": True},
            {**fixture, "promotion_authorized": True},
        ):
            self.assertNotEqual(list(validator.iter_errors(mutated)), [])
        completed = subprocess.run(
            [sys.executable, str(GENERATOR), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_schema_admitted_integral_decimals_canonicalize_at_every_integer_leaf(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        integer_paths: list[tuple[str | int, ...]] = []

        def collect(value: object, path: tuple[str | int, ...] = ()) -> None:
            if type(value) is int:
                integer_paths.append(path)
            elif type(value) is dict:
                for key, member in cast(dict[str, object], value).items():
                    collect(member, (*path, key))
            elif type(value) is list:
                for index, member in enumerate(cast(list[object], value)):
                    collect(member, (*path, index))

        collect(fixture)
        self.assertEqual(len(integer_paths), 26)
        for path in integer_paths:
            mutated = json.loads(json.dumps(fixture))
            cursor: object = mutated
            for component in path[:-1]:
                cursor = cursor[component]  # type: ignore[index]
            final = path[-1]
            original = cursor[final]  # type: ignore[index]
            cursor[final] = float(original)  # type: ignore[index]
            with self.subTest(path=path):
                self.assertEqual(list(validator.iter_errors(mutated)), [])
                self.assertEqual(review.validate_human_review_terminal_wire(mutated), self.record)
                encoded = json.dumps(mutated, sort_keys=True, separators=(",", ":"))
                self.assertEqual(review.decode_human_review_terminal_json(encoded), self.record)

        invalid_numbers = (
            {**fixture, "response_count": 0.5},
            {**fixture, "response_count": float("inf")},
            {**fixture, "promotion_authorized": 0},
            {**fixture, "promotion_authorized": 0.0},
            {**fixture, "claim_cap": 0},
        )
        for mutated in invalid_numbers:
            with self.subTest(invalid_number=mutated):
                self.assertNotEqual(list(validator.iter_errors(mutated)), [])
                with self.assertRaises(review.HumanReviewError):
                    review.validate_human_review_terminal_wire(mutated)

    def test_raw_json_decimal_tokens_are_exact_and_resource_bounded(self) -> None:
        integer_paths: list[tuple[tuple[str | int, ...], int]] = []

        def collect(value: object, path: tuple[str | int, ...] = ()) -> None:
            if type(value) is int:
                integer_paths.append((path, value))
            elif type(value) is dict:
                for key, member in cast(dict[str, object], value).items():
                    collect(member, (*path, key))
            elif type(value) is list:
                for index, member in enumerate(cast(list[object], value)):
                    collect(member, (*path, index))

        def payload(path: tuple[str | int, ...], token: str) -> str:
            mutated = json.loads(json.dumps(self.wire))
            cursor: object = mutated
            for component in path[:-1]:
                cursor = cursor[component]  # type: ignore[index]
            cursor[path[-1]] = "__RAW_NUMBER__"  # type: ignore[index]
            encoded = json.dumps(mutated, sort_keys=True, separators=(",", ":"))
            return encoded.replace('"__RAW_NUMBER__"', token, 1)

        collect(self.wire)
        self.assertEqual(len(integer_paths), 26)
        for index, (path, expected) in enumerate(integer_paths):
            exact_token = f"{expected}.000"
            unequal_token = (
                "1e-400"
                if expected == 0 and index % 2 == 0
                else "-1e-400"
                if expected == 0
                else f"{expected}.0000000000001"
            )
            with self.subTest(path=path, token=exact_token):
                self.assertEqual(
                    review.decode_human_review_terminal_json(payload(path, exact_token)),
                    self.record,
                )
            with self.subTest(path=path, token=unequal_token):
                with self.assertRaises(review.HumanReviewError):
                    review.decode_human_review_terminal_json(payload(path, unequal_token))

        protocol_path = ("protocol", "planned_power_floor_basis_points")
        position_path = ("protocol", "candidate_position_target_basis_points")
        zero_path = ("response_count",)
        accepted_tokens = (
            (zero_path, "-0.0"),
            (zero_path, "0e99"),
            (position_path, "5e3"),
            (protocol_path, "8e3"),
            (protocol_path, "8000.000"),
        )
        for path, token in accepted_tokens:
            with self.subTest(accepted_token=token):
                self.assertEqual(
                    review.decode_human_review_terminal_json(payload(path, token)), self.record
                )

        rejected_tokens = (
            (protocol_path, "8000.0000000000001"),
            (protocol_path, "7999.9999999999999"),
            (position_path, "5000.0000000000001"),
            (position_path, "4999.9999999999999"),
            (("protocol", "qualification_score_floor_basis_points"), "8999.9999999999999"),
            (
                ("protocol", "swapped_order_consistency_floor_basis_points"),
                "9499.9999999999999",
            ),
            (zero_path, "1e-400"),
            (zero_path, "-1e-400"),
            (zero_path, "0e129"),
            (zero_path, "0e-129"),
            (zero_path, "0." + ("0" * 40)),
            (protocol_path, "8.0000000000001e3"),
        )
        for path, token in rejected_tokens:
            with self.subTest(rejected_token=token):
                with self.assertRaises(review.HumanReviewError):
                    review.decode_human_review_terminal_json(payload(path, token))

    def test_leaf_exports_are_narrow_and_no_study_issuer_exists(self) -> None:
        expected = {
            "HumanReviewDisposition",
            "HumanReviewEvidenceClass",
            "HumanReviewClaimCap",
            "HumanReviewTerminalRecord",
            "build_human_review_terminal_record",
            "validate_human_review_terminal_wire",
            "decode_human_review_terminal_json",
        }
        self.assertEqual(expected - set(review.__all__), set())
        for symbol in expected:
            self.assertTrue(hasattr(review, symbol), symbol)
        for forbidden in (
            "issue_human_review_evidence",
            "execute_human_review_study",
            "promote_human_review_regression",
        ):
            self.assertFalse(hasattr(review, forbidden))


if __name__ == "__main__":
    unittest.main()
