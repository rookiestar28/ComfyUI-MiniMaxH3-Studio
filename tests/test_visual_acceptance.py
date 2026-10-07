"""M11-08 visual-perception acceptance gate tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from comfyui_h3_context.core import (
    AcceptanceStatus,
    VisualAcceptanceError,
    VisualAcceptanceRuntime,
    VisualBenchmarkPlan,
    VisualCapabilityMeasurement,
    VisualDisposition,
    VisualProfileEvidence,
    build_default_visual_benchmark_plan,
    evaluate_visual_acceptance,
)
from scripts.m11_08_visual_acceptance_fixture import run

ROOT = Path(__file__).resolve().parents[1]


def _qualified_plan(*, accuracy: int = 9_000) -> VisualBenchmarkPlan:
    plan = build_default_visual_benchmark_plan()
    candidate = plan.candidates[0]
    measurements = tuple(
        VisualCapabilityMeasurement(
            capability,
            accuracy_basis_points=accuracy,
            calibration_ece_basis_points=1_000,
            deterministic=True,
            platform_supported=True,
        )
        for capability in candidate.capabilities
    )
    qualified = replace(
        candidate,
        supports_cancellation_cleanup=True,
        disposition=VisualDisposition.QUALIFIED,
        measurements=measurements,
    )
    return replace(plan, candidates=(qualified, *plan.candidates[1:]))


class VisualAcceptanceTests(unittest.TestCase):
    def test_schema_fixture_is_redacted_and_all_negative_is_unsupported_research(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "visual_acceptance_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/visual_acceptance_v1.schema.json"
        )
        summary = run()
        self.assertEqual(summary["status"], AcceptanceStatus.UNSUPPORTED_RESEARCH.value)
        self.assertEqual(summary["executable_candidate_ids"], [])
        self.assertEqual(summary["native_live_execution"], "not_started")
        self.assertEqual(summary["ollama_live_execution"], "not_started")
        self.assertTrue(summary["optional_dependencies_absent"])
        wire = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("http://", wire)
        self.assertNotIn("https://", wire)
        self.assertNotIn("/mnt/", wire)

    def test_family_dispositions_are_separate_and_deterministic(self) -> None:
        first = evaluate_visual_acceptance(build_default_visual_benchmark_plan())
        second = evaluate_visual_acceptance(build_default_visual_benchmark_plan())
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(
            {summary.family.value for summary in first.family_summaries},
            {"comfyui_native", "ollama", "specialist"},
        )
        self.assertEqual(first.fallback_used, False)
        self.assertEqual(first.to_public_dict()["status"], "unsupported_research")

    def test_hidden_fallback_network_or_dependency_presence_fails_closed(self) -> None:
        plan = build_default_visual_benchmark_plan()
        for runtime in (
            VisualAcceptanceRuntime(fallback_used=True),
            VisualAcceptanceRuntime(network_contacted=True),
            VisualAcceptanceRuntime(optional_dependencies_absent=False),
        ):
            with self.assertRaises(VisualAcceptanceError):
                evaluate_visual_acceptance(plan, runtime=runtime)

    def test_unknown_profile_evidence_and_unsafe_limitations_fail_closed(self) -> None:
        plan = build_default_visual_benchmark_plan()
        with self.assertRaises(VisualAcceptanceError):
            evaluate_visual_acceptance(
                plan,
                runtime=VisualAcceptanceRuntime(
                    profile_evidence=(
                        VisualProfileEvidence(
                            "unknown_profile",
                            latency_ms=1,
                            peak_vram_mb=1,
                            peak_ram_mb=1,
                            deterministic=True,
                            platform_supported=True,
                            cancellation_cleanup_verified=True,
                        ),
                    )
                ),
            )
        with self.assertRaises(VisualAcceptanceError):
            VisualAcceptanceRuntime(limitations=("https://private.example",))

    def test_qualified_profile_requires_runtime_evidence_and_is_rejected_without_it(self) -> None:
        report = evaluate_visual_acceptance(_qualified_plan())
        self.assertEqual(report.status, AcceptanceStatus.REJECTED)
        self.assertEqual(report.executable_candidate_ids, ())
        receipt = report.candidate_receipts[0]
        self.assertIn("runtime_evidence_missing", receipt.failed_checks)

    def test_qualified_profile_with_passing_resource_evidence_is_partial_but_executable(
        self,
    ) -> None:
        plan = _qualified_plan()
        report = evaluate_visual_acceptance(
            plan,
            runtime=VisualAcceptanceRuntime(
                profile_evidence=(
                    VisualProfileEvidence(
                        plan.candidates[0].candidate_id,
                        latency_ms=100,
                        peak_vram_mb=100,
                        peak_ram_mb=100,
                        deterministic=True,
                        platform_supported=True,
                        cancellation_cleanup_verified=True,
                    ),
                )
            ),
        )
        self.assertEqual(report.status, AcceptanceStatus.QUALIFIED)
        self.assertEqual(report.executable_candidate_ids, (plan.candidates[0].candidate_id,))
        self.assertIn("identity", report.unsupported_capabilities)

    def test_threshold_failure_never_becomes_executable(self) -> None:
        report = evaluate_visual_acceptance(
            _qualified_plan(accuracy=6_000),
            runtime=VisualAcceptanceRuntime(
                profile_evidence=(
                    VisualProfileEvidence(
                        "native.comfyui.clip-textgenerate",
                        latency_ms=100,
                        peak_vram_mb=100,
                        peak_ram_mb=100,
                        deterministic=True,
                        platform_supported=True,
                        cancellation_cleanup_verified=True,
                    ),
                )
            ),
        )
        self.assertEqual(report.status, AcceptanceStatus.REJECTED)
        self.assertEqual(report.executable_candidate_ids, ())
        self.assertTrue(report.candidate_receipts[0].failed_checks)


if __name__ == "__main__":
    unittest.main()
