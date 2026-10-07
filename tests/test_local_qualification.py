"""M14-04 bounded local perception/fusion/planner qualification tests."""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate

import comfyui_h3_context.core as core_api
import comfyui_h3_context.core.local_qualification as qualification_module
from comfyui_h3_context.core.local_qualification import (
    AblationFactor,
    AblationStatus,
    CandidateAvailability,
    CandidateCaseEvidence,
    CandidateDisposition,
    CandidateTopology,
    ExecutionLevel,
    LocalQualificationError,
    ProductScopeDisposition,
    QualificationBackend,
    QualificationCandidateEvidence,
    QualificationPlan,
    _ablation_receipt,
    _dominates,
    _evaluate_local_qualification_plan,
    _receipt,
    build_default_local_qualification_plan,
    evaluate_local_qualification,
)
from scripts.m14_04_local_qualification_fixture import run

ROOT = Path(__file__).resolve().parents[1]
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64


def _eligible_plan(candidate_ids: tuple[str, ...] = ("native.modular",)) -> QualificationPlan:
    plan = build_default_local_qualification_plan()
    selected = set(candidate_ids)
    candidates = tuple(
        replace(
            candidate,
            availability=(
                CandidateAvailability.ELIGIBLE
                if candidate.candidate_id in selected
                else candidate.availability
            ),
            unavailable_reason=(
                "" if candidate.candidate_id in selected else candidate.unavailable_reason
            ),
        )
        for candidate in plan.candidates
    )
    return replace(plan, candidates=candidates)


def _evidence(
    plan: QualificationPlan,
    candidate_id: str = "native.modular",
    *,
    execution_level: ExecutionLevel = ExecutionLevel.UNMOCKED_RAW_MEDIA,
    source_grounding_bp: int = 9_500,
    unsupported_fact_bp: int = 100,
    hard_constraints_preserved: bool = True,
    latency_ms: int = 100,
    peak_vram_mb: int = 256,
    peak_ram_mb: int = 512,
    deterministic: bool = True,
    validation_passed: bool = True,
    fallback_used: bool = False,
    cleanup_verified: bool = True,
    private_content_retained: bool = False,
) -> QualificationCandidateEvidence:
    candidate = next(item for item in plan.candidates if item.candidate_id == candidate_id)
    cases = tuple(
        CandidateCaseEvidence(
            case_id=case.case_id,
            media_fingerprint=case.media_fingerprint,
            admission_receipt_fingerprint=case.admission_receipt_fingerprint,
            seed_inventory=plan.seeds,
            source_grounding_bp=source_grounding_bp,
            unsupported_fact_bp=unsupported_fact_bp,
            hard_constraints_preserved=hard_constraints_preserved,
            latency_ms=latency_ms,
            peak_vram_mb=peak_vram_mb,
            peak_ram_mb=peak_ram_mb,
            deterministic=deterministic,
            validation_passed=validation_passed,
            prompt_fingerprint=SHA_B,
            report_fingerprint=SHA_C,
        )
        for case in plan.cases
    )
    return QualificationCandidateEvidence(
        candidate_id=candidate.candidate_id,
        plan_fingerprint=plan.fingerprint,
        backend=candidate.backend,
        topology=candidate.topology,
        profile_id=candidate.profile_id,
        execution_level=execution_level,
        settings_fingerprint=plan.settings_fingerprint,
        cases=cases,
        fallback_used=fallback_used,
        cleanup_verified=cleanup_verified,
        private_content_retained=private_content_retained,
    )


class LocalQualificationTests(unittest.TestCase):
    def test_default_plan_is_preregistered_bounded_and_covers_topologies_and_backends(self) -> None:
        plan = build_default_local_qualification_plan()
        self.assertEqual(
            {candidate.topology for candidate in plan.candidates}, set(CandidateTopology)
        )
        self.assertEqual(
            {candidate.backend for candidate in plan.candidates}, set(QualificationBackend)
        )
        self.assertGreaterEqual(len(plan.cases), 3)
        self.assertEqual(len(plan.predecessor_evidence_fingerprints), 3)
        self.assertTrue(
            all(value.startswith("sha256:") for value in plan.predecessor_evidence_fingerprints)
        )
        self.assertEqual(plan.fingerprint, build_default_local_qualification_plan().fingerprint)
        self.assertTrue(all(candidate.unavailable_reason for candidate in plan.candidates))
        self.assertEqual({ablation.factor for ablation in plan.ablations}, set(AblationFactor))

    def test_default_result_is_manual_only_and_content_free(self) -> None:
        report = evaluate_local_qualification(build_default_local_qualification_plan())
        self.assertEqual(report.product_scope, ProductScopeDisposition.MANUAL_ONLY_SCOPED)
        self.assertEqual(report.qualified_candidate_ids, ())
        self.assertEqual(report.pareto_candidate_ids, ())
        self.assertTrue(
            all(
                receipt.disposition is CandidateDisposition.UNAVAILABLE
                for receipt in report.candidate_receipts
            )
        )
        self.assertTrue(
            all(receipt.status is AblationStatus.MISSING for receipt in report.ablation_receipts)
        )
        wire = json.dumps(report.to_wire(), ensure_ascii=False, sort_keys=True)
        for forbidden in ("http://", "https://", "B:\\", "/mnt/", "token=", "secret"):
            self.assertNotIn(forbidden, wire.casefold())
        self.assertLess(len(report.to_wire_bytes()), 65_536)

        fixture = run()
        self.assertEqual(fixture["product_scope"], "MANUAL_ONLY_SCOPED")
        self.assertFalse(fixture["network_contacted"])
        self.assertFalse(fixture["host_started"])
        self.assertFalse(fixture["media_started"])
        self.assertFalse(fixture["reference_code_executed"])
        self.assertFalse(fixture["provider_selected"])

    def test_schema_is_closed_and_versioned(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "local_qualification_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"],
            "comfyui-h3-context://contracts/local_qualification_v1.schema.json",
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["properties"]["product_scope"]["enum"]),
            {item.value for item in ProductScopeDisposition},
        )
        validate(
            evaluate_local_qualification(build_default_local_qualification_plan()).to_wire(), schema
        )
        validated = qualification_module.validate_local_qualification_wire(
            evaluate_local_qualification(build_default_local_qualification_plan()).to_wire()
        )
        self.assertEqual(
            validated.fingerprint,
            evaluate_local_qualification(build_default_local_qualification_plan()).fingerprint,
        )

    def test_public_api_cannot_mint_unmocked_evidence_without_runtime_owner(self) -> None:
        self.assertFalse(hasattr(core_api, "issue_qualification_candidate_evidence"))
        self.assertFalse(hasattr(qualification_module, "issue_qualification_candidate_evidence"))
        self.assertNotIn(
            "issue_qualification_candidate_evidence",
            qualification_module.__all__,
        )
        accepted_plan = build_default_local_qualification_plan()
        caller_declared = replace(_evidence(accepted_plan))
        with self.assertRaisesRegex(LocalQualificationError, "no runtime qualification evidence"):
            evaluate_local_qualification(accepted_plan, evidence=(caller_declared,))

    def test_schema_and_production_validator_reject_contradictory_wire(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "local_qualification_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        wire: Any = evaluate_local_qualification(build_default_local_qualification_plan()).to_wire()
        wire["candidate_receipts"][0]["disposition"] = "qualified"
        wire["candidate_receipts"][0]["executable"] = True
        with self.assertRaises(ValidationError):
            validate(wire, schema)
        with self.assertRaises(LocalQualificationError):
            qualification_module.validate_local_qualification_wire(wire)

    def test_schema_and_production_validator_reject_sensitive_marker(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "local_qualification_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        wire: Any = evaluate_local_qualification(build_default_local_qualification_plan()).to_wire()
        wire["limitations"] = ["secret"]
        with self.assertRaises(ValidationError):
            validate(wire, schema)
        with self.assertRaises(LocalQualificationError):
            qualification_module.validate_local_qualification_wire(wire)

    def test_production_validator_rejects_inventory_and_identity_drift(self) -> None:
        wire: Any = evaluate_local_qualification(build_default_local_qualification_plan()).to_wire()
        variants: list[Any] = []

        missing_candidate = deepcopy(wire)
        missing_candidate["candidate_receipts"].pop()
        variants.append(missing_candidate)

        backend_drift = deepcopy(wire)
        backend_drift["backend_summaries"][0]["candidate_ids"].pop()
        variants.append(backend_drift)

        ablation_drift = deepcopy(wire)
        ablation_drift["ablation_receipts"][0]["baseline_candidate_id"] = "other.candidate"
        variants.append(ablation_drift)

        limitation_drift = deepcopy(wire)
        limitation_drift["limitations"].pop()
        variants.append(limitation_drift)

        sensitive_nested = deepcopy(wire)
        sensitive_nested["limitations"] = ["provider.secret"]
        variants.append(sensitive_nested)

        forged_unavailable = deepcopy(wire)
        original_reason = forged_unavailable["candidate_receipts"][0]["limitations"][0]
        forged_unavailable["candidate_receipts"][0]["limitations"] = ["forged.unavailable.reason"]
        forged_unavailable["limitations"] = [
            "forged.unavailable.reason" if value == original_reason else value
            for value in forged_unavailable["limitations"]
        ]
        variants.append(forged_unavailable)

        impossible_ablation = deepcopy(wire)
        impossible_ablation["ablation_receipts"][0].update(
            {
                "status": "complete",
                "source_grounding_delta_bp": 0,
                "unsupported_fact_delta_bp": 0,
                "latency_delta_ms": 0,
                "peak_vram_delta_mb": 0,
                "peak_ram_delta_mb": 0,
                "failure_delta": 0,
                "limitation": "",
            }
        )
        variants.append(impossible_ablation)

        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(LocalQualificationError):
                qualification_module.validate_local_qualification_wire(variant)

    def test_plan_rejects_incomplete_matrix_and_unexplained_unavailability(self) -> None:
        plan = build_default_local_qualification_plan()
        with self.assertRaises(LocalQualificationError):
            replace(
                plan,
                candidates=tuple(
                    candidate
                    for candidate in plan.candidates
                    if candidate.backend is QualificationBackend.COMFYUI_NATIVE
                ),
            )
        with self.assertRaises(LocalQualificationError):
            replace(plan, candidates=(replace(plan.candidates[0], unavailable_reason=""),))
        invalid_variant = replace(
            plan.candidates[1],
            fusion_rule_id="fusion.unexpected.second.change.v1",
        )
        with self.assertRaises(LocalQualificationError):
            replace(plan, candidates=(plan.candidates[0], invalid_variant, *plan.candidates[2:]))

    def test_eligible_candidate_without_runtime_evidence_is_rejected_not_qualified(self) -> None:
        with self.assertRaisesRegex(LocalQualificationError, "accepted release plan"):
            evaluate_local_qualification(_eligible_plan())

    def test_only_unmocked_raw_media_evidence_can_qualify(self) -> None:
        for level in (ExecutionLevel.SYNTHETIC, ExecutionLevel.BOUNDARY_MOCKED):
            plan = _eligible_plan()
            report = _evaluate_local_qualification_plan(
                plan, evidence=(_evidence(plan, execution_level=level),)
            )
            receipt = next(
                item for item in report.candidate_receipts if item.candidate_id == "native.modular"
            )
            self.assertEqual(receipt.disposition, CandidateDisposition.REJECTED)
            self.assertIn("unmocked_raw_media_required", receipt.failed_checks)
            self.assertEqual(report.product_scope, ProductScopeDisposition.MANUAL_ONLY_SCOPED)

    def test_declared_real_evidence_cannot_activate_an_assisted_profile(self) -> None:
        plan = _eligible_plan()
        with self.assertRaisesRegex(LocalQualificationError, "accepted release plan"):
            evaluate_local_qualification(plan, evidence=(_evidence(plan),))

    def test_fallback_incomplete_cleanup_or_private_retention_is_non_compensatory(self) -> None:
        plan = _eligible_plan()
        for evidence, code in (
            (_evidence(plan, fallback_used=True), "fallback_forbidden"),
            (_evidence(plan, cleanup_verified=False), "cleanup_not_verified"),
            (_evidence(plan, private_content_retained=True), "private_content_retained"),
        ):
            report = _evaluate_local_qualification_plan(plan, evidence=(evidence,))
            receipt = next(
                item for item in report.candidate_receipts if item.candidate_id == "native.modular"
            )
            self.assertIn(code, receipt.failed_checks)
            self.assertEqual(receipt.disposition, CandidateDisposition.REJECTED)

    def test_case_plan_settings_and_candidate_identity_must_join_exactly(self) -> None:
        plan = _eligible_plan()
        evidence = _evidence(plan)
        bad_case = replace(
            evidence,
            cases=(replace(evidence.cases[0], media_fingerprint=SHA_A), *evidence.cases[1:]),
        )
        bad_admission = replace(
            evidence,
            cases=(
                replace(evidence.cases[0], admission_receipt_fingerprint=SHA_A),
                *evidence.cases[1:],
            ),
        )
        bad_values = (
            replace(evidence, plan_fingerprint=SHA_A),
            replace(evidence, settings_fingerprint=SHA_A),
            replace(evidence, profile_id="profile.other.v1"),
            bad_case,
            bad_admission,
        )
        for bad in bad_values:
            with self.subTest(bad=bad), self.assertRaises(LocalQualificationError):
                _evaluate_local_qualification_plan(plan, evidence=(bad,))

    def test_missing_case_and_duplicate_or_unknown_evidence_fail_closed(self) -> None:
        plan = _eligible_plan()
        evidence = _evidence(plan)
        missing = QualificationCandidateEvidence(
            candidate_id=evidence.candidate_id,
            plan_fingerprint=evidence.plan_fingerprint,
            backend=evidence.backend,
            topology=evidence.topology,
            profile_id=evidence.profile_id,
            execution_level=evidence.execution_level,
            settings_fingerprint=evidence.settings_fingerprint,
            cases=evidence.cases[:-1],
            fallback_used=evidence.fallback_used,
            cleanup_verified=evidence.cleanup_verified,
            private_content_retained=evidence.private_content_retained,
            limitations=evidence.limitations,
        )
        report = _evaluate_local_qualification_plan(plan, evidence=(missing,))
        receipt = next(
            item for item in report.candidate_receipts if item.candidate_id == "native.modular"
        )
        self.assertIn("case_inventory_mismatch", receipt.failed_checks)
        with self.assertRaises(LocalQualificationError):
            _evaluate_local_qualification_plan(plan, evidence=(evidence, evidence))
        with self.assertRaises(LocalQualificationError):
            _evaluate_local_qualification_plan(
                plan, evidence=(replace(evidence, candidate_id="unknown.candidate"),)
            )

    def test_copied_or_directly_constructed_evidence_is_not_runtime_authority(self) -> None:
        plan = build_default_local_qualification_plan()
        declared = _evidence(plan)
        for value in (declared, replace(declared)):
            with self.assertRaisesRegex(
                LocalQualificationError, "no runtime qualification evidence"
            ):
                evaluate_local_qualification(plan, evidence=(value,))

    def test_all_metric_and_resource_failures_block_qualification(self) -> None:
        plan = _eligible_plan()
        variants = (
            (_evidence(plan, source_grounding_bp=1), "source_grounding_below_minimum"),
            (_evidence(plan, unsupported_fact_bp=9_999), "unsupported_fact_above_maximum"),
            (_evidence(plan, hard_constraints_preserved=False), "hard_constraint_mutation"),
            (
                _evidence(plan, latency_ms=plan.budget.max_wall_time_ms + 1),
                "latency_limit_exceeded",
            ),
            (_evidence(plan, peak_vram_mb=plan.budget.max_peak_vram_mb + 1), "vram_limit_exceeded"),
            (_evidence(plan, peak_ram_mb=plan.budget.max_peak_ram_mb + 1), "ram_limit_exceeded"),
            (_evidence(plan, deterministic=False), "determinism_failed"),
            (_evidence(plan, validation_passed=False), "validation_not_passed"),
            (
                QualificationCandidateEvidence(
                    candidate_id="native.modular",
                    plan_fingerprint=plan.fingerprint,
                    backend=QualificationBackend.COMFYUI_NATIVE,
                    topology=CandidateTopology.MODULAR_ONLY,
                    profile_id="profile.modular.v1",
                    execution_level=ExecutionLevel.UNMOCKED_RAW_MEDIA,
                    settings_fingerprint=plan.settings_fingerprint,
                    cases=(
                        replace(_evidence(plan).cases[0], seed_inventory=(999,)),
                        *_evidence(plan).cases[1:],
                    ),
                    fallback_used=False,
                    cleanup_verified=True,
                    private_content_retained=False,
                ),
                "seed_inventory_mismatch",
            ),
        )
        for evidence, code in variants:
            with self.subTest(code=code):
                report = _evaluate_local_qualification_plan(plan, evidence=(evidence,))
                receipt = next(
                    item
                    for item in report.candidate_receipts
                    if item.candidate_id == "native.modular"
                )
                self.assertIn(code, receipt.failed_checks)
                self.assertEqual(receipt.disposition, CandidateDisposition.REJECTED)

    def test_pareto_membership_uses_all_resource_and_quality_dimensions(self) -> None:
        plan = _eligible_plan(("native.modular", "ollama.modular"))
        native = _evidence(plan, "native.modular", latency_ms=100, peak_ram_mb=200)
        dominated = _evidence(
            plan,
            "ollama.modular",
            source_grounding_bp=9_000,
            unsupported_fact_bp=200,
            latency_ms=200,
            peak_vram_mb=300,
            peak_ram_mb=300,
        )
        candidates = {candidate.candidate_id: candidate for candidate in plan.candidates}
        native_receipt = _receipt(plan, candidates["native.modular"], native)
        dominated_receipt = _receipt(plan, candidates["ollama.modular"], dominated)
        self.assertTrue(_dominates(native_receipt, dominated_receipt))
        self.assertFalse(_dominates(dominated_receipt, native_receipt))

    def test_one_factor_ablation_completes_only_when_both_exact_candidates_qualify(self) -> None:
        plan = _eligible_plan(("native.modular", "native.modular.adapter_alt"))
        candidates = {candidate.candidate_id: candidate for candidate in plan.candidates}
        receipts = {
            candidate_id: _receipt(plan, candidates[candidate_id], evidence)
            for candidate_id, evidence in (
                ("native.modular", _evidence(plan, "native.modular")),
                (
                    "native.modular.adapter_alt",
                    _evidence(
                        plan,
                        "native.modular.adapter_alt",
                        source_grounding_bp=9_000,
                        latency_ms=150,
                    ),
                ),
            )
        }
        receipts.update(
            {
                candidate.candidate_id: _receipt(plan, candidate, None)
                for candidate in plan.candidates
                if candidate.candidate_id not in receipts
            }
        )
        adapter_ablation = next(
            ablation for ablation in plan.ablations if ablation.factor is AblationFactor.ADAPTER
        )
        adapter = _ablation_receipt(adapter_ablation, receipts)
        self.assertEqual(adapter.status, AblationStatus.COMPLETE)
        self.assertEqual(adapter.source_grounding_delta_bp, -500)
        self.assertEqual(adapter.latency_delta_ms, 50)
        for ablation in plan.ablations:
            if ablation.factor is not AblationFactor.ADAPTER:
                self.assertEqual(
                    _ablation_receipt(ablation, receipts).status,
                    AblationStatus.MISSING,
                )

    def test_evidence_rejects_unsafe_or_noncanonical_portable_values(self) -> None:
        plan = _eligible_plan()
        with self.assertRaises(LocalQualificationError):
            replace(_evidence(plan), profile_id="https://private.example/model")
        with self.assertRaises(LocalQualificationError):
            replace(_evidence(plan), limitations=("token=secret",))
        with self.assertRaises(LocalQualificationError):
            replace(_evidence(plan), plan_fingerprint="abc")


if __name__ == "__main__":
    unittest.main()
