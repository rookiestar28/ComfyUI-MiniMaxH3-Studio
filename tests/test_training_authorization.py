from __future__ import annotations

import ast
import importlib
import json
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from jsonschema import Draft202012Validator

from comfyui_h3_context.core import training_authorization as training

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "governance" / "contracts" / "training_authorization_v1.schema.json"
FIXTURE = ROOT / "tests" / "fixtures" / "m14_07_training_authorization.json"
GENERATOR = ROOT / "scripts" / "m14_07_training_authorization_fixture.py"
MODULE = ROOT / "comfyui_h3_context" / "core" / "training_authorization.py"


class ArmedString(str):
    equality_calls = 0

    def __eq__(self, other: object) -> bool:
        type(self).equality_calls += 1
        raise RuntimeError("caller-controlled equality executed")

    __hash__ = str.__hash__


class TrainingAuthorizationContractTests(unittest.TestCase):
    record: training.TrainingAuthorizationTerminalRecord
    wire: dict[str, object]

    @classmethod
    def setUpClass(cls) -> None:
        # The admitted golden is immutable; mutation tests construct their own fresh records.
        cls.record = training.build_training_authorization_terminal_record()
        cls.wire = cls.record.to_wire()

    def test_public_contract_symbols_exist(self) -> None:
        expected = {
            "TrainingAuthorizationError",
            "TrainingDecisionDisposition",
            "TrainingClaimCap",
            "TrainingMethod",
            "TrainingReviewDomain",
            "TrainingReviewStatus",
            "TrainingMethodStatus",
            "TrainingAuthorizationDomainReceipt",
            "TrainingAuthorizationMethodReceipt",
            "TrainingAuthorizationTerminalRecord",
            "build_training_authorization_terminal_record",
            "validate_training_authorization_terminal_wire",
            "decode_training_authorization_terminal_json",
        }
        self.assertEqual(expected - set(dir(training)), set())

    def test_exact_do_not_implement_disposition_and_predecessor_join(self) -> None:
        record = self.record
        self.assertEqual(record.schema, "h3.training_authorization.terminal.v1")
        self.assertEqual(record.record_date, "2026-08-09")
        self.assertIs(
            record.disposition,
            training.TrainingDecisionDisposition.DO_NOT_IMPLEMENT,
        )
        self.assertIs(
            record.claim_cap,
            training.TrainingClaimCap.NO_TRAINING_AUTHORIZATION_OR_EVIDENCE,
        )
        self.assertEqual(
            record.human_review_terminal_fingerprint,
            training.M14_06_HUMAN_REVIEW_TERMINAL_FINGERPRINT,
        )
        self.assertEqual(record.human_review_disposition, "HUMAN_REVIEW_UNAVAILABLE")
        self.assertEqual(record.human_review_claim_cap, "NO_HUMAN_QUALITY_CLAIM")
        self.assertEqual(record.official_oracle_terms_status, "do_not_implement")
        self.assertEqual(record.official_oracle_training_distillation, "deny")
        self.assertEqual(record.official_oracle_training_lane, "unavailable_prohibited")
        with patch.object(training, "build_human_review_terminal_record", return_value=object()):
            with self.assertRaises(training.TrainingAuthorizationError):
                training.TrainingAuthorizationTerminalRecord()

    def test_method_and_review_domain_inventories_are_closed_and_blocking(self) -> None:
        expected_methods = (
            "oracle_output_training",
            "synthetic_augmentation",
            "preference_optimization",
            "distillation",
            "local_fine_tuning",
        )
        expected_domains = (
            "legal",
            "terms",
            "privacy",
            "license",
            "compute",
            "marginal_value",
        )
        self.assertEqual(tuple(item.value for item in training.TrainingMethod), expected_methods)
        self.assertEqual(
            tuple(receipt.method.value for receipt in self.record.method_reviews),
            expected_methods,
        )
        self.assertEqual(
            tuple(item.value for item in training.TrainingReviewDomain), expected_domains
        )
        self.assertEqual(
            tuple(receipt.domain.value for receipt in self.record.domain_reviews),
            expected_domains,
        )
        self.assertTrue(
            all(
                receipt.status is training.TrainingMethodStatus.BLOCKED
                and not receipt.permitted
                and not receipt.necessary
                and receipt.authority_receipts == ()
                for receipt in self.record.method_reviews
            )
        )
        self.assertTrue(
            all(
                receipt.status is not training.TrainingReviewStatus.APPROVED
                and receipt.blocking
                and receipt.authority_receipts == ()
                for receipt in self.record.domain_reviews
            )
        )

    def test_affirmative_fields_are_null_and_every_activity_inventory_is_empty(self) -> None:
        for field_name in (
            "training_authorized",
            "implementation_authorized",
            "data_collection_authorized",
            "provider_output_use_authorized",
            "model_weight_use_authorized",
            "compute_spend_authorized",
            "publication_authorized",
        ):
            self.assertIs(getattr(self.record, field_name), False, field_name)
        for field_name in (
            "bounded_objective",
            "data_authority",
            "compute_envelope",
            "expected_gain",
            "rollback_plan",
            "required_new_roadmap_item",
        ):
            self.assertIsNone(getattr(self.record, field_name), field_name)
        for field_name in (
            "source_datasets",
            "training_examples",
            "oracle_outputs",
            "synthetic_outputs",
            "preference_records",
            "compute_receipts",
            "training_runs",
            "model_artifacts",
            "retained_artifacts",
            "publications",
        ):
            self.assertEqual(getattr(self.record, field_name), (), field_name)
        self.assertTrue(self.record.requires_separate_user_authorization)
        self.assertTrue(self.record.requires_new_roadmap_item)

    def test_future_approval_requirements_are_explicit_but_grant_nothing(self) -> None:
        self.assertEqual(
            self.record.future_approval_requirements,
            (
                "fresh_legal_terms_privacy_license_review",
                "admitted_data_authority",
                "bounded_objective",
                "bounded_compute_envelope",
                "measured_expected_gain",
                "rollback_plan",
                "separate_explicit_user_authorization",
                "dated_separate_roadmap_item_and_plan",
            ),
        )
        self.assertIn("no_training_authorization", self.record.limitations)
        self.assertIn("no_training_or_quality_evidence", self.record.limitations)

    def test_direct_construction_cannot_mint_authorization_or_evidence(self) -> None:
        self.assertEqual(training.TrainingAuthorizationTerminalRecord().to_wire(), self.wire)
        with self.assertRaises(TypeError):
            training.TrainingAuthorizationTerminalRecord(  # type: ignore[call-arg]
                training_authorized=True
            )
        with self.assertRaises((TypeError, ValueError)):
            replace(self.record, implementation_authorized=True)
        method = training.TrainingAuthorizationMethodReceipt(
            training.TrainingMethod.ORACLE_OUTPUT_TRAINING
        )
        with self.assertRaises((TypeError, ValueError)):
            replace(method, permitted=True)
        domain = training.TrainingAuthorizationDomainReceipt(training.TrainingReviewDomain.LEGAL)
        with self.assertRaises((TypeError, ValueError)):
            replace(domain, blocking=False)

    def test_production_validator_regenerates_and_rejects_semantic_drift(self) -> None:
        self.assertEqual(
            training.validate_training_authorization_terminal_wire(self.wire), self.record
        )
        attacks: list[dict[str, object]] = []
        for field_name, value in (
            ("unexpected", True),
            ("training_authorized", True),
            ("implementation_authorized", True),
            ("bounded_objective", "train"),
            ("source_datasets", ["dataset"]),
            ("training_runs", ["run"]),
            ("model_artifacts", ["model"]),
            ("human_review_terminal_fingerprint", "sha256:" + ("0" * 64)),
        ):
            mutated = dict(self.wire)
            mutated[field_name] = value
            attacks.append(mutated)

        method = json.loads(json.dumps(self.wire))
        method["method_reviews"][0]["permitted"] = True
        attacks.append(method)
        domain = json.loads(json.dumps(self.wire))
        domain["domain_reviews"][0]["status"] = "approved"
        attacks.append(domain)
        reordered = json.loads(json.dumps(self.wire))
        reordered["method_reviews"].reverse()
        attacks.append(reordered)

        for mutated in attacks:
            with self.subTest(keys=tuple(mutated)):
                with self.assertRaises(training.TrainingAuthorizationError):
                    training.validate_training_authorization_terminal_wire(mutated)

    def test_decoder_is_duplicate_aware_bounded_and_strict(self) -> None:
        encoded = json.dumps(self.wire, sort_keys=True, separators=(",", ":"))
        self.assertEqual(training.decode_training_authorization_terminal_json(encoded), self.record)
        self.assertEqual(
            training.decode_training_authorization_terminal_json(encoded.encode()), self.record
        )
        self.assertEqual(
            training.decode_training_authorization_terminal_json(bytearray(encoded.encode())),
            self.record,
        )
        duplicate = encoded.replace(
            '"schema":"h3.training_authorization.terminal.v1"',
            '"schema":"h3.training_authorization.terminal.v1",'
            '"schema":"h3.training_authorization.terminal.v1"',
            1,
        )
        for payload in (
            duplicate,
            "{" + ('"x":"' + ("a" * 40000) + '"}'),
            '{"schema":NaN}',
            b"\xff",
            7,
        ):
            with self.subTest(payload_type=type(payload).__name__):
                with self.assertRaises(training.TrainingAuthorizationError):
                    training.decode_training_authorization_terminal_json(
                        cast(str | bytes | bytearray, payload)
                    )

    def test_hostile_python_members_reject_before_caller_equality(self) -> None:
        ArmedString.equality_calls = 0
        hostile = dict(self.wire)
        schema = hostile.pop("schema")
        hostile[ArmedString("schema")] = schema
        with self.assertRaises(training.TrainingAuthorizationError):
            training.validate_training_authorization_terminal_wire(hostile)
        self.assertEqual(ArmedString.equality_calls, 0)
        for mutated in (
            {**self.wire, "training_authorized": 0},
            {**self.wire, "implementation_authorized": 0.0},
            {**self.wire, "limitations": tuple(cast(list[object], self.wire["limitations"]))},
            {**self.wire, "claim_cap": ArmedString(cast(str, self.wire["claim_cap"]))},
        ):
            with self.assertRaises(training.TrainingAuthorizationError):
                training.validate_training_authorization_terminal_wire(mutated)

    def test_public_projections_revalidate_after_mutation(self) -> None:
        record = training.build_training_authorization_terminal_record()
        object.__setattr__(record, "training_authorized", True)
        for projection in (record.to_wire, record.to_wire_bytes, lambda: record.fingerprint):
            with self.assertRaises(training.TrainingAuthorizationError):
                projection()

        method = training.TrainingAuthorizationMethodReceipt(
            training.TrainingMethod.ORACLE_OUTPUT_TRAINING
        )
        object.__setattr__(method, "permitted", True)
        with self.assertRaises(training.TrainingAuthorizationError):
            method.to_wire()

        domain = training.TrainingAuthorizationDomainReceipt(training.TrainingReviewDomain.LEGAL)
        object.__setattr__(domain, "blocking", False)
        with self.assertRaises(training.TrainingAuthorizationError):
            domain.to_wire()

    def test_separate_concurrent_and_reload_builders_are_isolated(self) -> None:
        first = training.build_training_authorization_terminal_record()
        second = training.build_training_authorization_terminal_record()
        self.assertTrue(
            all(
                left is not right
                for left, right in zip(first.method_reviews, second.method_reviews, strict=True)
            )
        )
        self.assertTrue(
            all(
                left is not right
                for left, right in zip(first.domain_reviews, second.domain_reviews, strict=True)
            )
        )
        expected = second.to_wire()
        object.__setattr__(first.method_reviews[0], "permitted", True)
        with self.assertRaises(training.TrainingAuthorizationError):
            first.to_wire()
        self.assertEqual(second.to_wire(), expected)
        self.assertEqual(
            training.build_training_authorization_terminal_record().to_wire(), expected
        )
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(
                pool.map(
                    lambda _: training.build_training_authorization_terminal_record(), range(8)
                )
            )
        self.assertTrue(all(record.to_wire() == expected for record in records))
        self.assertEqual(len({id(record.method_reviews[0]) for record in records}), 8)
        self.assertEqual(len({id(record.domain_reviews[0]) for record in records}), 8)
        reloaded = importlib.reload(training)
        self.assertEqual(
            reloaded.build_training_authorization_terminal_record().to_wire(), expected
        )

    def test_fingerprint_is_frozen_and_content_derived(self) -> None:
        self.assertEqual(
            self.record.fingerprint,
            training.FROZEN_TRAINING_AUTHORIZATION_TERMINAL_FINGERPRINT,
        )
        self.assertRegex(self.record.fingerprint, r"^sha256:[0-9a-f]{64}$")

    def test_fixture_schema_and_generator_are_exact(self) -> None:
        for path in (SCHEMA, FIXTURE, GENERATOR):
            self.assertTrue(path.is_file(), f"missing public artifact: {path}")
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(fixture, self.wire)
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        self.assertEqual(list(validator.iter_errors(fixture)), [])
        for mutated in (
            {**fixture, "unexpected": True},
            {**fixture, "training_authorized": True},
            {**fixture, "bounded_objective": "train"},
            {**fixture, "training_runs": ["run"]},
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

    def test_leaf_exports_are_narrow_and_no_execution_surface_exists(self) -> None:
        expected = {
            "TrainingDecisionDisposition",
            "TrainingClaimCap",
            "TrainingMethod",
            "TrainingReviewDomain",
            "TrainingReviewStatus",
            "TrainingMethodStatus",
            "TrainingAuthorizationDomainReceipt",
            "TrainingAuthorizationMethodReceipt",
            "TrainingAuthorizationTerminalRecord",
            "build_training_authorization_terminal_record",
            "validate_training_authorization_terminal_wire",
            "decode_training_authorization_terminal_json",
        }
        self.assertEqual(expected - set(training.__all__), set())
        for symbol in expected:
            self.assertTrue(hasattr(training, symbol), symbol)
        for forbidden in (
            "issue_training_authorization",
            "approve_training",
            "execute_training",
            "train_model",
            "fine_tune_model",
            "distill_model",
            "generate_synthetic_dataset",
        ):
            self.assertFalse(hasattr(training, forbidden))
        self.assertFalse(hasattr(training, "_DOMAIN_RESULTS"))
        self.assertFalse(hasattr(training, "_METHOD_REASONS"))

        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertTrue(
            imported_roots.isdisjoint(
                {
                    "torch",
                    "transformers",
                    "datasets",
                    "requests",
                    "httpx",
                    "aiohttp",
                    "subprocess",
                    "socket",
                    "urllib",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
