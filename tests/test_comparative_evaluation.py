"""M7-08 comparative Base/Reference protocol and regression threshold tests."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from comfyui_h3_context.core import (
    COMPARATIVE_EVALUATION_SCHEMA,
    ComparativeEvaluationCase,
    ComparativeEvaluationCorpus,
    ComparativeEvidenceStatus,
    ComparativeProfile,
    ComparativeReportStatus,
    ComparativeReviewerRubric,
    ComparativeReviewerScore,
    ComparativeRoute,
    ComparativeRouteEvidence,
    ComparativeThresholdPolicy,
    EvaluationMeasurement,
    FixedH3Receipt,
    FixedH3Settings,
    FixedH3Status,
    evaluate_comparative_case,
    evaluate_comparative_corpus,
)
from comfyui_h3_context.core.errors import ContractValidationError

ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT_A = "sha256:" + "a" * 64
FINGERPRINT_B = "sha256:" + "b" * 64


def _recorded(
    route: ComparativeRoute,
    *,
    structural_valid: bool = True,
    hard_constraints_preserved: bool = True,
    latency: float = 0.2,
    reviewer_scores: tuple[ComparativeReviewerScore, ...] = (),
    fixed_h3: FixedH3Receipt | None = None,
) -> ComparativeRouteEvidence:
    return ComparativeRouteEvidence(
        route,
        ComparativeEvidenceStatus.RECORDED,
        structural_valid,
        hard_constraints_preserved,
        FINGERPRINT_A,
        EvaluationMeasurement(latency, 1024, 2048),
        reviewer_scores,
        fixed_h3=fixed_h3,
    )


def _not_requested(route: ComparativeRoute) -> ComparativeRouteEvidence:
    return ComparativeRouteEvidence(
        route,
        ComparativeEvidenceStatus.NOT_REQUESTED,
        limitation_codes=("lane_not_requested",),
    )


def _blocked(route: ComparativeRoute) -> ComparativeRouteEvidence:
    return ComparativeRouteEvidence(
        route,
        ComparativeEvidenceStatus.BLOCKED,
        diagnostic_codes=("lane_not_authorized",),
    )


def _case(
    *,
    manual: ComparativeRouteEvidence | None = None,
    local: ComparativeRouteEvidence | None = None,
    remote: ComparativeRouteEvidence | None = None,
    oracle: ComparativeRouteEvidence | None = None,
) -> ComparativeEvaluationCase:
    return ComparativeEvaluationCase(
        "case.base.1",
        ComparativeProfile.BASE,
        (FINGERPRINT_A,),
        (
            manual or _recorded(ComparativeRoute.MANUAL_DETERMINISTIC),
            local or _not_requested(ComparativeRoute.LOCAL_ASSISTED),
            remote or _blocked(ComparativeRoute.REMOTE_CUSTOM),
            oracle or _not_requested(ComparativeRoute.OFFICIAL_ORACLE),
        ),
    )


def _corpus(
    case: ComparativeEvaluationCase | None = None,
    *,
    thresholds: ComparativeThresholdPolicy | None = None,
    rubric: ComparativeReviewerRubric | None = None,
) -> ComparativeEvaluationCorpus:
    return ComparativeEvaluationCorpus(
        "m7.comparative.fixture",
        (case or _case(),),
        thresholds
        or ComparativeThresholdPolicy(
            "thresholds.v1",
            "1",
            max_latency_seconds=1.0,
            max_peak_memory_bytes=4096,
            max_output_bytes=4096,
        ),
        rubric or ComparativeReviewerRubric("reviewer.v1", "1", ("clarity", "fidelity")),
    )


class ComparativeEvaluationTests(unittest.TestCase):
    def test_fixture_and_schema_are_versioned_with_two_profiles_and_four_routes(self) -> None:
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "m7_comparative_evaluation_corpus.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(fixture["schema"], COMPARATIVE_EVALUATION_SCHEMA)
        self.assertEqual({case["profile"] for case in fixture["cases"]}, {"base", "reference"})
        self.assertTrue(all(len(case["routes"]) == 4 for case in fixture["cases"]))
        self.assertEqual(
            {route["route"] for route in fixture["cases"][0]["routes"]},
            {item.value for item in ComparativeRoute},
        )
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "comparative_evaluation_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"],
            "comfyui-h3-context://contracts/comparative_evaluation_v1.schema.json",
        )

    def test_optional_lanes_remain_visible_without_masking_manual_gate(self) -> None:
        report = evaluate_comparative_corpus(_corpus())
        self.assertEqual(report.status, ComparativeReportStatus.PASSED)
        self.assertTrue(report.is_gate_passed)
        result = report.results[0]
        self.assertEqual(result.structural_pass_ratio, "1")
        self.assertEqual(result.hard_constraint_pass_ratio, "1")
        self.assertEqual(
            result.route_statuses[ComparativeRoute.REMOTE_CUSTOM.value],
            ComparativeEvidenceStatus.BLOCKED,
        )
        self.assertIn("official_oracle_evidence_unavailable", result.limitations)
        self.assertEqual(
            report.route_summary[ComparativeRoute.MANUAL_DETERMINISTIC.value]["recorded"], 1
        )

    def test_repeated_evaluation_has_stable_case_and_corpus_fingerprints(self) -> None:
        corpus = _corpus()
        first = evaluate_comparative_corpus(corpus)
        second = evaluate_comparative_corpus(corpus)
        self.assertEqual(corpus.fingerprint, _corpus().fingerprint)
        self.assertEqual(first.to_wire(), second.to_wire())
        self.assertEqual(first.report_id, second.report_id)

    def test_structural_or_hard_constraint_failure_cannot_pass_thresholds(self) -> None:
        failed_manual = ComparativeRouteEvidence(
            ComparativeRoute.MANUAL_DETERMINISTIC,
            ComparativeEvidenceStatus.RECORDED,
            False,
            True,
            FINGERPRINT_A,
            EvaluationMeasurement(0.2, 1024, 2048),
        )
        result = evaluate_comparative_case(
            _case(manual=failed_manual),
            thresholds=_corpus().thresholds,
            reviewer_rubric=_corpus().reviewer_rubric,
        )
        self.assertEqual(result.status, ComparativeReportStatus.FAILED)
        self.assertIn("threshold.structural_pass_ratio", result.diagnostic_codes)

    def test_measurement_and_reviewer_thresholds_are_independent(self) -> None:
        scores = (
            ComparativeReviewerScore("clarity", 2.0),
            ComparativeReviewerScore("fidelity", 2.0),
        )
        thresholds = ComparativeThresholdPolicy(
            "thresholds.strict",
            "1",
            max_latency_seconds=0.1,
            min_reviewer_score=4.0,
        )
        case = _case(
            manual=_recorded(
                ComparativeRoute.MANUAL_DETERMINISTIC,
                latency=0.2,
                reviewer_scores=scores,
            )
        )
        result = evaluate_comparative_case(
            case,
            thresholds=thresholds,
            reviewer_rubric=ComparativeReviewerRubric("reviewer.v1", "1", ("clarity", "fidelity")),
        )
        self.assertEqual(result.status, ComparativeReportStatus.FAILED)
        self.assertIn("threshold.manual_deterministic.latency", result.diagnostic_codes)
        self.assertIn("threshold.reviewer_score", result.diagnostic_codes)

    def test_unknown_or_out_of_range_reviewer_scores_fail_closed(self) -> None:
        evidence = _recorded(
            ComparativeRoute.MANUAL_DETERMINISTIC,
            reviewer_scores=(
                ComparativeReviewerScore("unknown", 2.0),
                ComparativeReviewerScore("clarity", 7.0),
            ),
        )
        result = evaluate_comparative_case(
            _case(manual=evidence),
            thresholds=_corpus().thresholds,
            reviewer_rubric=_corpus().reviewer_rubric,
        )
        self.assertEqual(result.status, ComparativeReportStatus.FAILED)
        self.assertIn("reviewer.unknown_dimension", result.diagnostic_codes)
        self.assertIn("reviewer.score_out_of_range", result.diagnostic_codes)

    def test_fixed_h3_routes_remain_not_run_without_runtime_authority(self) -> None:
        def fixed(host_revision: str) -> FixedH3Receipt:
            return FixedH3Receipt(
                f"fixed.{host_revision}",
                FixedH3Status.NOT_RUN,
                FixedH3Settings(
                    "model.v1",
                    host_revision,
                    FINGERPRINT_A,
                    7,
                    "euler",
                    "512x512",
                    4.0,
                    "bf16",
                    "cuda:0",
                ),
                redacted_message="fixed H3 execution was not run",
            )

        with self.assertRaisesRegex(
            ContractValidationError, "no fixed H3 runtime authority is activated"
        ):
            FixedH3Receipt(
                "fixed.caller_pass",
                FixedH3Status.PASSED,
                FixedH3Settings(
                    "model.v1",
                    "host.v1",
                    FINGERPRINT_A,
                    7,
                    "euler",
                    "512x512",
                    4.0,
                    "bf16",
                    "cuda:0",
                ),
                output_fingerprint=FINGERPRINT_B,
            )

        case = _case(
            local=_recorded(
                ComparativeRoute.LOCAL_ASSISTED,
                fixed_h3=fixed("host.v2"),
            ),
            manual=_recorded(
                ComparativeRoute.MANUAL_DETERMINISTIC,
                fixed_h3=fixed("host.v1"),
            ),
        )
        result = evaluate_comparative_case(
            case,
            thresholds=_corpus().thresholds,
            reviewer_rubric=_corpus().reviewer_rubric,
        )
        self.assertEqual(result.status, ComparativeReportStatus.PASSED)
        self.assertIsNone(result.fixed_h3_settings_fingerprint)
        self.assertNotIn("fixed_h3.settings_drift", result.diagnostic_codes)
        self.assertIn("fixed_h3.local_assisted.not_run", result.limitations)
        self.assertIn("fixed_h3.manual_deterministic.not_run", result.limitations)

        tampered = fixed("host.tampered")
        object.__setattr__(tampered, "status", FixedH3Status.PASSED)
        object.__setattr__(tampered, "output_fingerprint", FINGERPRINT_B)
        with self.assertRaisesRegex(
            ContractValidationError, "no fixed H3 runtime authority is activated"
        ):
            _recorded(ComparativeRoute.LOCAL_ASSISTED, fixed_h3=tampered)

        post_route_receipt = fixed("host.post_route")
        post_route = _recorded(
            ComparativeRoute.LOCAL_ASSISTED,
            fixed_h3=post_route_receipt,
        )
        object.__setattr__(post_route_receipt, "status", FixedH3Status.PASSED)
        object.__setattr__(post_route_receipt, "output_fingerprint", FINGERPRINT_B)
        post_route_case = _case(local=post_route)
        with self.assertRaisesRegex(
            ContractValidationError, "no fixed H3 runtime authority is activated"
        ):
            evaluate_comparative_case(
                post_route_case,
                thresholds=_corpus().thresholds,
                reviewer_rubric=_corpus().reviewer_rubric,
            )

        class ForgedReceipt:
            def to_wire(self) -> dict[str, object]:
                return {"status": "passed", "output_fingerprint": FINGERPRINT_B}

        duck_route = _recorded(
            ComparativeRoute.LOCAL_ASSISTED,
            fixed_h3=fixed("host.duck"),
        )
        object.__setattr__(duck_route, "fixed_h3", ForgedReceipt())
        with self.assertRaisesRegex(ContractValidationError, "fixed_h3 must be FixedH3Receipt"):
            duck_route.to_wire()

    def test_recorded_route_prompt_drift_is_visible_as_a_limitation(self) -> None:
        different_local = ComparativeRouteEvidence(
            ComparativeRoute.LOCAL_ASSISTED,
            ComparativeEvidenceStatus.RECORDED,
            True,
            True,
            FINGERPRINT_B,
            EvaluationMeasurement(0.2, 1024, 2048),
        )
        result = evaluate_comparative_case(
            _case(local=different_local),
            thresholds=_corpus().thresholds,
            reviewer_rubric=_corpus().reviewer_rubric,
        )
        self.assertEqual(result.status, ComparativeReportStatus.PASSED)
        self.assertFalse(
            result.route_prompt_fingerprint_match[ComparativeRoute.LOCAL_ASSISTED.value]
        )
        self.assertIn(
            "route.local_assisted.prompt_fingerprint_differs",
            result.limitations,
        )

    def test_malformed_result_ratio_is_a_contract_error(self) -> None:
        with self.assertRaises(ContractValidationError):
            from comfyui_h3_context.core import ComparativeEvaluationResult

            ComparativeEvaluationResult(
                "result.bad",
                "case.bad",
                ComparativeProfile.BASE,
                ComparativeReportStatus.PASSED,
                {route.value: ComparativeEvidenceStatus.RECORDED for route in ComparativeRoute},
                {route.value: True for route in ComparativeRoute},
                "not-a-ratio",
                "1",
                None,
                (),
                (),
                None,
            )

    def test_route_and_secret_validation_fail_closed(self) -> None:
        with self.assertRaises(ContractValidationError):
            ComparativeEvaluationCase(
                "case.bad",
                ComparativeProfile.BASE,
                ("https://private.invalid/x",),
                _case().routes,
            )
        with self.assertRaises(ContractValidationError):
            ComparativeRouteEvidence(
                ComparativeRoute.REMOTE_CUSTOM,
                ComparativeEvidenceStatus.BLOCKED,
                diagnostic_codes=("blocked",),
                measurement=EvaluationMeasurement(0.1),
            )
        with self.assertRaises(ContractValidationError):
            ComparativeRouteEvidence(
                ComparativeRoute.OFFICIAL_ORACLE,
                ComparativeEvidenceStatus.RECORDED,
                True,
                True,
                FINGERPRINT_A,
                EvaluationMeasurement(0.1),
            )

    def test_core_module_has_no_optional_runtime_imports(self) -> None:
        source = (ROOT / "comfyui_h3_context" / "core" / "comparative_evaluation.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertTrue(
            {
                "aiohttp",
                "av",
                "comfy",
                "cv2",
                "httpx",
                "numpy",
                "requests",
                "torch",
                "transformers",
            }.isdisjoint(imported)
        )


if __name__ == "__main__":
    unittest.main()
