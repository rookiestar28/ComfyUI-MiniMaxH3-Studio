"""M5-06 offline Base-mode evaluation and fixed-H3 comparison tests."""

from __future__ import annotations

import ast
import json
import unittest
from dataclasses import replace
from pathlib import Path

from test_rendering import make_plan

from comfyui_h3_context.core import (
    BASE_EVALUATION_SCHEMA,
    BaseEvaluationCase,
    BaseEvaluationCorpus,
    EvaluationMeasurement,
    EvaluationRoute,
    EvaluationStatus,
    FixedH3Receipt,
    FixedH3Settings,
    FixedH3Status,
    OfficialContextIRRecording,
    OracleComparison,
    OracleComparisonMethod,
    OracleComparisonStatus,
    ProviderIdentity,
    TaskMode,
    evaluate_base_case,
    evaluate_base_corpus,
)
from comfyui_h3_context.core.context_reporting import ContextPlan, ProviderOutcome
from comfyui_h3_context.core.errors import ContractValidationError

ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT_A = "sha256:" + "a" * 64
FINGERPRINT_B = "sha256:" + "b" * 64


def measurement() -> EvaluationMeasurement:
    return EvaluationMeasurement(0.125, 128_000, 2048)


def settings() -> FixedH3Settings:
    return FixedH3Settings(
        "MiniMax-H3-base-rev-1",
        "comfyui-host-rev-1",
        FINGERPRINT_A,
        17,
        "euler",
        "1024x576",
        5.166666667,
        "bf16",
        "cuda:0",
        (FINGERPRINT_B,),
    )


def not_run_receipt() -> FixedH3Receipt:
    return FixedH3Receipt(
        "fixed_1",
        FixedH3Status.NOT_RUN,
        settings(),
        redacted_message="model execution was not authorized",
    )


def recording(output_fingerprint: str = FINGERPRINT_B) -> OfficialContextIRRecording:
    return OfficialContextIRRecording(
        "oracle_1",
        "h3.context.ir.recording.v1",
        provider=ProviderIdentity.OFFICIAL_MINIMAX,
        provider_version="MiniMax-H3",
        endpoint_revision="api-v2-h3",
        authorization_label="fixture_authorized",
        outcome=ProviderOutcome.SUCCEEDED,
        task_id="task_1",
        request_fingerprint=FINGERPRINT_A,
        output_fingerprint=output_fingerprint,
    )


def case(
    route: EvaluationRoute,
    *,
    baseline_plan: ContextPlan | None = None,
    oracle: OracleComparison | None = None,
    fixed_h3: FixedH3Receipt | None = None,
) -> BaseEvaluationCase:
    return BaseEvaluationCase(
        f"case.{route.value}",
        route,
        make_plan(TaskMode.T2VA),
        baseline_plan=baseline_plan,
        measurement=measurement(),
        source_fingerprints=(FINGERPRINT_A,),
        oracle=oracle,
        fixed_h3=fixed_h3,
    )


class BaseEvaluationTests(unittest.TestCase):
    def test_versioned_fixture_and_schema_are_explicit(self) -> None:
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "m5_base_evaluation_corpus.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(fixture["schema"], BASE_EVALUATION_SCHEMA)
        self.assertEqual(len(fixture["cases"]), 3)
        self.assertEqual(
            {item["route"] for item in fixture["cases"]},
            {"deterministic", "assisted", "official_provider"},
        )
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "base_evaluation_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"],
            "comfyui-h3-context://contracts/base_evaluation_v1.schema.json",
        )

    def test_deterministic_structural_and_constraint_metrics_are_reproducible(self) -> None:
        first = evaluate_base_case(case(EvaluationRoute.DETERMINISTIC))
        second = evaluate_base_case(case(EvaluationRoute.DETERMINISTIC))
        self.assertEqual(first.status, EvaluationStatus.PASSED)
        self.assertTrue(first.structural_valid)
        self.assertTrue(first.hard_constraints_preserved)
        self.assertEqual(first.evidence_coverage, "1")
        self.assertEqual(first.prompt_fingerprint, second.prompt_fingerprint)
        self.assertEqual(first.result_id, second.result_id)
        self.assertEqual(first.to_wire(), second.to_wire())

    def test_assisted_and_official_routes_remain_separate_in_corpus_report(self) -> None:
        oracle = OracleComparison(
            "oracle_compare",
            OracleComparisonMethod.FINGERPRINT,
            official_recording=recording(),
            limitation="fingerprint comparison does not prove implementation equivalence",
        )
        corpus = BaseEvaluationCorpus(
            "corpus_m5_smoke",
            (
                case(EvaluationRoute.DETERMINISTIC),
                case(EvaluationRoute.ASSISTED),
                case(EvaluationRoute.OFFICIAL_PROVIDER, oracle=oracle),
            ),
        )
        report = evaluate_base_corpus(corpus)
        self.assertEqual(len(report.results), 3)
        self.assertEqual(
            set(report.route_summary),
            {"deterministic", "assisted", "official_provider"},
        )
        self.assertEqual(report.route_summary["deterministic"]["case_count"], 1)
        self.assertEqual(report.route_summary["assisted"]["case_count"], 1)
        self.assertEqual(report.route_summary["official_provider"]["case_count"], 1)
        self.assertEqual(report.results[2].oracle.status, OracleComparisonStatus.DIFFERENT)
        self.assertIn(
            "oracle.output_fingerprint_differs", report.results[2].oracle.difference_codes
        )

    def test_baseline_mutation_is_a_failed_hard_constraint_metric(self) -> None:
        plan = make_plan(TaskMode.T2VA)
        baseline = replace(
            plan, request=replace(plan.request, user_intent="changed baseline intent")
        )
        result = evaluate_base_case(
            BaseEvaluationCase(
                "case.mutated",
                EvaluationRoute.ASSISTED,
                plan,
                baseline_plan=baseline,
                measurement=measurement(),
            )
        )
        self.assertEqual(result.status, EvaluationStatus.FAILED)
        self.assertFalse(result.hard_constraints_preserved)
        self.assertIn("evaluation.hard_constraint_mutation", result.diagnostic_codes)

    def test_fixed_h3_settings_are_pinned_and_not_run_is_not_success(self) -> None:
        result = evaluate_base_case(case(EvaluationRoute.DETERMINISTIC, fixed_h3=not_run_receipt()))
        self.assertEqual(result.status, EvaluationStatus.PASSED)
        self.assertEqual(result.fixed_h3.status, FixedH3Status.NOT_RUN)
        self.assertFalse(result.fixed_h3.is_successful)
        with self.assertRaisesRegex(
            ContractValidationError, "no fixed H3 runtime authority is activated"
        ):
            FixedH3Receipt(
                "fixed_pass",
                FixedH3Status.PASSED,
                settings(),
                output_fingerprint=FINGERPRINT_B,
            )
        with self.assertRaisesRegex(
            ContractValidationError, "no fixed H3 runtime authority is activated"
        ):
            FixedH3Receipt("bad", FixedH3Status.PASSED, settings())
        tampered = not_run_receipt()
        object.__setattr__(tampered, "status", FixedH3Status.PASSED)
        object.__setattr__(tampered, "output_fingerprint", FINGERPRINT_B)
        with self.assertRaisesRegex(
            ContractValidationError, "no fixed H3 runtime authority is activated"
        ):
            case(EvaluationRoute.DETERMINISTIC, fixed_h3=tampered)

        result_tampered = evaluate_base_case(
            case(EvaluationRoute.DETERMINISTIC, fixed_h3=not_run_receipt())
        )
        object.__setattr__(result_tampered.fixed_h3, "status", FixedH3Status.PASSED)
        object.__setattr__(result_tampered.fixed_h3, "output_fingerprint", FINGERPRINT_B)
        with self.assertRaisesRegex(
            ContractValidationError, "no fixed H3 runtime authority is activated"
        ):
            result_tampered.to_wire()

        class ForgedReceipt:
            def to_wire(self) -> dict[str, object]:
                return {"status": "passed", "output_fingerprint": FINGERPRINT_B}

        duck_tampered = evaluate_base_case(
            case(EvaluationRoute.DETERMINISTIC, fixed_h3=not_run_receipt())
        )
        object.__setattr__(duck_tampered, "fixed_h3", ForgedReceipt())
        with self.assertRaisesRegex(ContractValidationError, "fixed_h3 must be FixedH3Receipt"):
            duck_tampered.to_wire()

    def test_measurement_and_secret_shaped_metadata_fail_closed(self) -> None:
        with self.assertRaises(ContractValidationError):
            EvaluationMeasurement(-1.0, 1, 1)
        with self.assertRaises(ContractValidationError):
            FixedH3Settings(
                "model",
                "host",
                "https://private.invalid/output",
                1,
                "schedule",
                "1024x576",
                5,
                "bf16",
                "cuda",
                (),
            )
        with self.assertRaises(ContractValidationError):
            OracleComparison(
                "oracle_secret",
                OracleComparisonMethod.FINGERPRINT,
                limitation="token=must-not-appear",
            )

    def test_oracle_comparison_is_sanitized_and_structurally_limited(self) -> None:
        oracle = OracleComparison(
            "oracle_compare",
            OracleComparisonMethod.FINGERPRINT,
            official_recording=recording(),
            limitation="fingerprints are not a quality or equivalence proof",
        )
        result = evaluate_base_case(case(EvaluationRoute.OFFICIAL_PROVIDER, oracle=oracle))
        self.assertEqual(result.oracle.status, OracleComparisonStatus.DIFFERENT)
        self.assertIsNotNone(result.oracle.local_fingerprint)
        self.assertNotIn("cdn.example", json.dumps(result.to_wire()))
        self.assertNotIn("prompt text", json.dumps(result.to_wire()))
        self.assertEqual(result.oracle.method, OracleComparisonMethod.FINGERPRINT)

    def test_module_has_no_optional_runtime_imports(self) -> None:
        source = (ROOT / "comfyui_h3_context" / "core" / "base_evaluation.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertFalse(
            imports
            & {
                "aiohttp",
                "comfy",
                "cv2",
                "httpx",
                "moviepy",
                "numpy",
                "requests",
                "torch",
                "transformers",
            }
        )


if __name__ == "__main__":
    unittest.main()
