"""M14-05 terminal fixed-H3 generation disposition contract tests."""

from __future__ import annotations

import ast
import importlib
import json
import unittest
from dataclasses import replace
from pathlib import Path

from jsonschema import Draft202012Validator

from comfyui_h3_context.core import (
    ContractValidationError,
    FixedH3Receipt,
    FixedH3Settings,
    FixedH3Status,
)
from comfyui_h3_context.core import fixed_h3_generation as fixed

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "governance" / "contracts" / "fixed_h3_generation_v1.schema.json"
FIXTURE = ROOT / "tests" / "fixtures" / "m14_05_fixed_h3_generation.json"


class ArmedString(str):
    equality_calls = 0

    def __eq__(self, other: object) -> bool:
        type(self).equality_calls += 1
        raise RuntimeError("attacker equality must not run")

    __hash__ = str.__hash__


class FixedH3GenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = fixed.build_fixed_h3_terminal_record()
        self.wire = self.record.to_wire()

    def test_exact_terminal_disposition_and_zero_execution_authority(self) -> None:
        record = self.record
        self.assertEqual(record.schema, "h3.fixed_generation.terminal.v1")
        self.assertEqual(record.record_date, "2026-08-09")
        self.assertIs(record.disposition, fixed.FixedH3Disposition.UNAVAILABLE)
        self.assertIs(
            record.claim_cap,
            fixed.FixedH3ClaimCap.NO_FIXED_H3_GENERATION_EVIDENCE,
        )
        self.assertFalse(record.authorization_granted)
        self.assertFalse(record.host_started)
        self.assertFalse(record.model_loaded)
        self.assertFalse(record.media_opened)
        self.assertFalse(record.network_contacted)
        self.assertFalse(record.provider_selected)
        self.assertFalse(record.gpu_used)
        self.assertFalse(record.output_artifact_created)
        self.assertFalse(record.reference_code_executed)
        self.assertIs(record.execution_status, fixed.FixedH3EvidenceStatus.MISSING)
        self.assertEqual(
            (
                record.generation_attempt_count,
                record.generation_failure_count,
                record.retained_output_count,
                record.scored_output_count,
                record.published_output_count,
            ),
            (0, 0, 0, 0, 0),
        )
        self.assertEqual(record.run_receipts, ())
        self.assertEqual(record.output_receipts, ())
        self.assertEqual(record.score_receipts, ())
        self.assertEqual(record.locators, ())
        self.assertIsNone(record.primary_margin)
        self.assertIsNone(record.confidence_interval)
        self.assertIsNone(record.aggregate_score)
        self.assertIsNone(record.official_noninferiority)
        self.assertFalse(record.promotion_authorized)
        self.assertEqual(record.fingerprint, fixed.FROZEN_FIXED_H3_TERMINAL_FINGERPRINT)

    def test_prompt_families_and_predecessor_authorities_are_exact(self) -> None:
        expected_reasons = {
            fixed.FixedH3PromptFamily.RAW_MANUAL: "execution_envelope_unavailable",
            fixed.FixedH3PromptFamily.OFFICIAL: "official_prompt_unavailable",
            fixed.FixedH3PromptFamily.LOCAL: "assisted_profile_unqualified",
            fixed.FixedH3PromptFamily.ABLATED: "ablation_prompts_unavailable",
        }
        self.assertEqual(
            [receipt.family for receipt in self.record.prompt_families],
            list(fixed.FixedH3PromptFamily),
        )
        for receipt in self.record.prompt_families:
            self.assertIs(receipt.status, fixed.FixedH3EvidenceStatus.MISSING)
            self.assertEqual(receipt.reason, expected_reasons[receipt.family])
            self.assertEqual(receipt.prompt_count, 0)
            self.assertEqual(receipt.prompt_fingerprints, ())

        self.assertEqual(
            self.record.official_terminal_disposition_sha256,
            fixed.M14_01_TERMINAL_DISPOSITION_SHA256,
        )
        self.assertEqual(
            self.record.official_behavior_profile_fingerprint,
            fixed.M14_03_BEHAVIOR_PROFILE_FINGERPRINT,
        )
        self.assertEqual(self.record.official_behavior_profile_disposition, "UNAVAILABLE")
        self.assertEqual(
            self.record.local_qualification_fingerprint,
            fixed.M14_04_LOCAL_QUALIFICATION_FINGERPRINT,
        )
        self.assertEqual(self.record.local_product_scope, "MANUAL_ONLY_SCOPED")

    def test_all_eleven_metrics_remain_missing_without_analysis_claims(self) -> None:
        self.assertEqual(
            [receipt.metric for receipt in self.record.metrics],
            list(fixed.FixedH3Metric),
        )
        self.assertEqual(len(self.record.metrics), 11)
        for receipt in self.record.metrics:
            self.assertIs(receipt.status, fixed.FixedH3EvidenceStatus.MISSING)
            self.assertEqual(receipt.direction, "MISSING")
            self.assertEqual(receipt.scale, "MISSING")
            self.assertIsNone(receipt.noninferiority_margin)
            self.assertEqual(receipt.observation_count, 0)

    def test_native_requirements_are_explicit_but_runtime_evidence_is_missing(self) -> None:
        native = self.record.native_prerequisites
        self.assertIs(native.status, fixed.FixedH3EvidenceStatus.MISSING)
        self.assertEqual(native.host_version, "0.30.0")
        self.assertEqual(len(native.host_revision), 40)
        self.assertEqual(native.schema, "h3-native-h3-wiring/1")
        self.assertEqual(
            native.classes,
            (
                "EmptyMiniMaxH3LatentAV",
                "MiniMaxH3ImageToVideo",
                "MiniMaxH3ReferenceToVideo",
                "MiniMaxH3SigmaShift",
            ),
        )
        self.assertEqual(native.prompt_socket, "STRING")
        self.assertEqual(native.media_socket, "V3_MEDIA")
        self.assertEqual(native.task_modes, ("t2va", "i2va", "fl2va", "ref2va"))
        self.assertIn("clip", native.required_model_inputs)
        self.assertIn("audio_vae", native.required_model_inputs)
        self.assertFalse(native.executable_workflow_admitted)

    def test_public_construction_cannot_mint_execution_or_derived_state(self) -> None:
        self.assertEqual(fixed.FixedH3TerminalRecord().to_wire(), self.wire)
        with self.assertRaises(TypeError):
            fixed.FixedH3TerminalRecord(disposition="EXECUTED")  # type: ignore[call-arg]
        with self.assertRaises((TypeError, ValueError)):
            replace(self.record, authorization_granted=True)
        with self.assertRaises((TypeError, ValueError)):
            replace(self.record, generation_attempt_count=1)
        with self.assertRaises((TypeError, ValueError)):
            replace(self.record, promotion_authorized=True)
        with self.assertRaises((TypeError, ValueError)):
            replace(self.record, disposition=fixed.FixedH3Disposition.UNAVAILABLE)

        family = fixed.FixedH3FamilyReceipt(fixed.FixedH3PromptFamily.OFFICIAL)
        with self.assertRaises((TypeError, ValueError)):
            replace(family, status=fixed.FixedH3EvidenceStatus.MISSING)
        metric = fixed.FixedH3MetricReceipt(fixed.FixedH3Metric.INSTRUCTION)
        with self.assertRaises((TypeError, ValueError)):
            replace(metric, observation_count=1)

        self.assertFalse(hasattr(fixed, "issue_fixed_h3_evidence"))
        self.assertFalse(hasattr(fixed, "execute_fixed_h3_generation"))

    def test_production_validator_regenerates_and_rejects_semantic_drift(self) -> None:
        self.assertEqual(fixed.validate_fixed_h3_terminal_wire(self.wire), self.record)
        attacks: list[tuple[str, dict[str, object]]] = []

        opened = dict(self.wire)
        opened["unexpected"] = True
        attacks.append(("open root", opened))

        executed = dict(self.wire)
        executed["generation_attempt_count"] = 1
        attacks.append(("attempt", executed))

        authorized = dict(self.wire)
        authorized["authorization_granted"] = True
        attacks.append(("authorization", authorized))

        promoted = dict(self.wire)
        promoted["promotion_authorized"] = True
        attacks.append(("promotion", promoted))

        predecessor = dict(self.wire)
        predecessor["local_qualification_fingerprint"] = "sha256:" + ("f" * 64)
        attacks.append(("predecessor", predecessor))

        family_drift = json.loads(json.dumps(self.wire))
        family_drift["prompt_families"][0]["status"] = "AVAILABLE"
        attacks.append(("family", family_drift))

        metric_drift = json.loads(json.dumps(self.wire))
        metric_drift["metrics"][0]["noninferiority_margin"] = "0.01"
        attacks.append(("metric", metric_drift))

        native_drift = json.loads(json.dumps(self.wire))
        native_drift["native_prerequisites"]["status"] = "PASS"
        attacks.append(("native", native_drift))

        locator = dict(self.wire)
        locator["locators"] = ["https://example.invalid/private"]
        attacks.append(("locator", locator))

        for name, attack in attacks:
            with self.subTest(name=name), self.assertRaises(fixed.FixedH3Error) as captured:
                fixed.validate_fixed_h3_terminal_wire(attack)
            self.assertNotIn("example.invalid", str(captured.exception))

    def test_python_mapping_types_fail_before_attacker_equality(self) -> None:
        ArmedString.equality_calls = 0
        armed = dict(self.wire)
        value = armed.pop("schema")
        armed[ArmedString("schema")] = value
        with self.assertRaises(fixed.FixedH3Error):
            fixed.validate_fixed_h3_terminal_wire(armed)
        self.assertEqual(ArmedString.equality_calls, 0)

        list_subclass = dict(self.wire)
        list_subclass["metrics"] = type("SpoofList", (list,), {})(self.wire["metrics"])
        with self.assertRaises(fixed.FixedH3Error):
            fixed.validate_fixed_h3_terminal_wire(list_subclass)

        class SpoofInt(int):
            pass

        int_subclass = dict(self.wire)
        int_subclass["generation_attempt_count"] = SpoofInt(0)
        with self.assertRaises(fixed.FixedH3Error):
            fixed.validate_fixed_h3_terminal_wire(int_subclass)

    def test_decoder_is_duplicate_aware_bounded_and_content_free(self) -> None:
        text = json.dumps(self.wire, sort_keys=True, separators=(",", ":"))
        self.assertEqual(fixed.decode_fixed_h3_terminal_json(text), self.record)

        duplicate = text.replace(
            '"schema":"h3.fixed_generation.terminal.v1"',
            '"schema":"h3.fixed_generation.terminal.v1","schema":"h3.fixed_generation.terminal.v1"',
            1,
        )
        with self.assertRaisesRegex(fixed.FixedH3Error, "duplicate"):
            fixed.decode_fixed_h3_terminal_json(duplicate)

        for raw in ("NaN", "Infinity", "-Infinity"):
            poisoned = text.replace('"aggregate_score":null', f'"aggregate_score":{raw}')
            with self.subTest(raw=raw), self.assertRaises(fixed.FixedH3Error):
                fixed.decode_fixed_h3_terminal_json(poisoned)

        for unsafe in ("unsafe\u0001value", "unsafe\ud800value"):
            poisoned = json.loads(text)
            poisoned["limitations"] = [unsafe]
            with self.subTest(unsafe=ascii(unsafe)), self.assertRaises(fixed.FixedH3Error):
                fixed.validate_fixed_h3_terminal_wire(poisoned)

        with self.assertRaises(fixed.FixedH3Error):
            fixed.decode_fixed_h3_terminal_json("[" * 1000 + "0" + "]" * 1000)
        with self.assertRaises(fixed.FixedH3Error):
            fixed.decode_fixed_h3_terminal_json("9" * 5000)
        with self.assertRaises(fixed.FixedH3Error):
            fixed.decode_fixed_h3_terminal_json("x" * (fixed.MAX_FIXED_H3_WIRE_BYTES + 1))

    def test_schema_is_closed_and_rejects_every_contract_drift(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        self.assertEqual(list(validator.iter_errors(self.wire)), [])

        attacks = []
        for key, value in (
            ("disposition", "EXECUTED"),
            ("authorization_granted", True),
            ("generation_attempt_count", 1),
            ("promotion_authorized", True),
            ("primary_margin", "0.05"),
        ):
            attack = json.loads(json.dumps(self.wire))
            attack[key] = value
            attacks.append(attack)
        open_wire = dict(self.wire)
        open_wire["extra"] = None
        attacks.append(open_wire)
        for attack in attacks:
            self.assertTrue(list(validator.iter_errors(attack)))

    def test_fixture_generator_contract_is_exact(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(fixture, self.wire)
        self.assertEqual(fixed.validate_fixed_h3_terminal_wire(fixture), self.record)

    def test_module_is_pure_and_has_no_execution_dependencies(self) -> None:
        path = ROOT / "comfyui_h3_context" / "core" / "fixed_h3_generation.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue(
            {
                "comfy",
                "torch",
                "numpy",
                "requests",
                "httpx",
                "openai",
                "subprocess",
                "socket",
            }.isdisjoint(imported)
        )
        source = path.read_text(encoding="utf-8").casefold()
        for forbidden in ("cuda", "model.generate", "provider.execute", "media.open"):
            self.assertNotIn(forbidden, source)

    def test_leaf_module_exports_are_present_and_narrow(self) -> None:
        module = importlib.import_module("comfyui_h3_context.core.fixed_h3_generation")
        for name in (
            "FixedH3Disposition",
            "FixedH3ClaimCap",
            "FixedH3EvidenceStatus",
            "FixedH3PromptFamily",
            "FixedH3Metric",
            "FixedH3TerminalRecord",
            "FROZEN_FIXED_H3_TERMINAL_FINGERPRINT",
            "build_fixed_h3_terminal_record",
            "validate_fixed_h3_terminal_wire",
            "decode_fixed_h3_terminal_json",
        ):
            self.assertIn(name, module.__all__)
            self.assertTrue(hasattr(module, name), name)
        for forbidden in ("issue_fixed_h3_evidence", "execute_fixed_h3_generation"):
            self.assertFalse(hasattr(module, forbidden))

    def test_legacy_receipt_cannot_mint_an_executed_h3_lane(self) -> None:
        settings = FixedH3Settings(
            "declared.model",
            "declared.host",
            "sha256:" + ("1" * 64),
            7,
            "declared.schedule",
            "512x512",
            4.0,
            "bf16",
            "declared.device",
            ("sha256:" + ("2" * 64),),
        )
        for status, output in (
            (FixedH3Status.PASSED, "sha256:" + ("3" * 64)),
            (FixedH3Status.FAILED, None),
            (FixedH3Status.BLOCKED, None),
        ):
            with (
                self.subTest(status=status.value),
                self.assertRaisesRegex(
                    ContractValidationError,
                    "no fixed H3 runtime authority is activated",
                ),
            ):
                FixedH3Receipt(
                    "caller.declared",
                    status,
                    settings,
                    output_fingerprint=output,
                    redacted_message=None if status is FixedH3Status.PASSED else "declared state",
                )

        not_run = FixedH3Receipt(
            "caller.not_run",
            FixedH3Status.NOT_RUN,
            settings,
            redacted_message="fixed H3 execution was not run",
        )
        self.assertFalse(not_run.is_successful)

    def test_legacy_receipt_revalidates_before_portable_serialization(self) -> None:
        settings = FixedH3Settings(
            "declared.model",
            "declared.host",
            "sha256:" + ("1" * 64),
            7,
            "declared.schedule",
            "512x512",
            4.0,
            "bf16",
            "declared.device",
        )
        receipt = FixedH3Receipt(
            "caller.not_run",
            FixedH3Status.NOT_RUN,
            settings,
            redacted_message="fixed H3 execution was not run",
        )
        object.__setattr__(receipt, "status", FixedH3Status.PASSED)
        object.__setattr__(receipt, "output_fingerprint", "sha256:" + ("3" * 64))
        with self.assertRaisesRegex(
            ContractValidationError, "no fixed H3 runtime authority is activated"
        ):
            receipt.to_wire()

        deep_tampered = FixedH3Receipt(
            "caller.deep_tampered",
            FixedH3Status.NOT_RUN,
            settings,
            redacted_message="fixed H3 execution was not run",
        )
        object.__setattr__(settings, "model_revision", "https://private.invalid/model")
        with self.assertRaises(ContractValidationError):
            settings.to_wire()
        with self.assertRaises(ContractValidationError):
            deep_tampered.require_admitted()
        with self.assertRaises(ContractValidationError):
            deep_tampered.to_wire()

    def test_terminal_and_nested_projections_revalidate_after_mutation(self) -> None:
        record = fixed.build_fixed_h3_terminal_record()
        object.__setattr__(record, "authorization_granted", True)
        object.__setattr__(record, "generation_attempt_count", 1)
        object.__setattr__(record, "run_receipts", ("caller.run",))
        object.__setattr__(record, "promotion_authorized", True)
        for projection in (record.to_wire, record.to_wire_bytes, lambda: record.fingerprint):
            with self.subTest(projection=projection), self.assertRaises(fixed.FixedH3Error):
                projection()

        family = fixed.FixedH3FamilyReceipt(fixed.FixedH3PromptFamily.RAW_MANUAL)
        object.__setattr__(family, "prompt_count", 1)
        object.__setattr__(family, "prompt_fingerprints", ("sha256:" + ("4" * 64),))
        with self.assertRaises(fixed.FixedH3Error):
            family.to_wire()

        metric = fixed.FixedH3MetricReceipt(fixed.FixedH3Metric.INSTRUCTION)
        object.__setattr__(metric, "direction", "higher_is_better")
        object.__setattr__(metric, "noninferiority_margin", "0.01")
        object.__setattr__(metric, "observation_count", 1)
        with self.assertRaises(fixed.FixedH3Error):
            metric.to_wire()

        native = fixed.FixedH3NativePrerequisite()
        object.__setattr__(native, "executable_workflow_admitted", True)
        with self.assertRaises(fixed.FixedH3Error):
            native.to_wire()

        nested = fixed.build_fixed_h3_terminal_record()
        object.__setattr__(nested, "prompt_families", (family, *nested.prompt_families[1:]))
        object.__setattr__(nested, "metrics", (metric, *nested.metrics[1:]))
        object.__setattr__(nested, "native_prerequisites", native)
        with self.assertRaises(fixed.FixedH3Error):
            nested.to_wire()

    def test_terminal_nested_values_are_isolated_per_record(self) -> None:
        with self.subTest(kind="family"):
            first = fixed.build_fixed_h3_terminal_record()
            second = fixed.build_fixed_h3_terminal_record()
            family = first.prompt_families[0]
            object.__setattr__(family, "prompt_count", 1)
            try:
                self.assertEqual(second.to_wire(), self.wire)
                self.assertEqual(fixed.build_fixed_h3_terminal_record().to_wire(), self.wire)
            finally:
                object.__setattr__(family, "prompt_count", 0)
            self.assertIsNot(first.prompt_families[0], second.prompt_families[0])

        with self.subTest(kind="metric"):
            first = fixed.build_fixed_h3_terminal_record()
            second = fixed.build_fixed_h3_terminal_record()
            metric = first.metrics[0]
            object.__setattr__(metric, "observation_count", 1)
            try:
                self.assertEqual(second.to_wire(), self.wire)
                self.assertEqual(fixed.build_fixed_h3_terminal_record().to_wire(), self.wire)
            finally:
                object.__setattr__(metric, "observation_count", 0)
            self.assertIsNot(first.metrics[0], second.metrics[0])

        with self.subTest(kind="native"):
            first = fixed.build_fixed_h3_terminal_record()
            second = fixed.build_fixed_h3_terminal_record()
            native = first.native_prerequisites
            object.__setattr__(native, "executable_workflow_admitted", True)
            try:
                self.assertEqual(second.to_wire(), self.wire)
                self.assertEqual(fixed.build_fixed_h3_terminal_record().to_wire(), self.wire)
            finally:
                object.__setattr__(native, "executable_workflow_admitted", False)
            self.assertIsNot(first.native_prerequisites, second.native_prerequisites)


if __name__ == "__main__":
    unittest.main()
