"""M11-01 frozen visual benchmark and adapter-selection contract tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from comfyui_h3_context.core import (
    VISUAL_BENCHMARK_SCHEMA,
    VisualBenchmarkError,
    VisualCandidateFamily,
    VisualDisposition,
    VisualMetricKind,
    build_default_visual_benchmark_plan,
)
from scripts.m11_01_visual_benchmark_fixture import run

ROOT = Path(__file__).resolve().parents[1]


class VisualBenchmarkTests(unittest.TestCase):
    def test_schema_and_fingerprint_are_deterministic(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "visual_benchmark_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/visual_benchmark_v1.schema.json"
        )
        self.assertEqual(schema["properties"]["schema"]["const"], VISUAL_BENCHMARK_SCHEMA)
        first = build_default_visual_benchmark_plan()
        second = build_default_visual_benchmark_plan()
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(json.loads(json.dumps(first.to_wire())), first.to_wire())

    def test_fixtures_cover_all_capabilities_and_metrics(self) -> None:
        plan = build_default_visual_benchmark_plan()
        covered = {capability for fixture in plan.fixtures for capability in fixture.capabilities}
        self.assertEqual(
            covered, set(plan.capability_policy.mandatory) | set(plan.capability_policy.optional)
        )
        for capability in covered:
            metrics = {metric.metric for metric in plan.metrics if metric.capability is capability}
            self.assertEqual(metrics, {VisualMetricKind.ACCURACY, VisualMetricKind.CALIBRATION})
        self.assertEqual(
            {metric.metric for metric in plan.metrics if metric.capability is None},
            {
                VisualMetricKind.LATENCY,
                VisualMetricKind.VRAM,
                VisualMetricKind.RAM,
                VisualMetricKind.DETERMINISM,
                VisualMetricKind.PLATFORM_SUPPORT,
            },
        )

    def test_dispositions_and_routing_never_turn_static_fixture_into_execution_evidence(
        self,
    ) -> None:
        plan = build_default_visual_benchmark_plan()
        self.assertEqual(
            {candidate.family for candidate in plan.candidates},
            set(VisualCandidateFamily),
        )
        self.assertEqual(
            {candidate.disposition for candidate in plan.candidates},
            {
                VisualDisposition.REJECTED,
                VisualDisposition.UNAVAILABLE,
                VisualDisposition.UNSUPPORTED,
            },
        )
        self.assertEqual(plan.executable_candidate_ids, ())
        self.assertEqual(
            plan.routing.preference_order,
            (
                VisualCandidateFamily.COMFYUI_NATIVE,
                VisualCandidateFamily.OLLAMA,
                VisualCandidateFamily.SPECIALIST,
            ),
        )
        self.assertFalse(plan.routing.automatic_fallback)
        self.assertFalse(plan.routing.fidelity_winner_preselected)

    def test_offline_fixture_summary_is_redacted_and_explicit(self) -> None:
        summary = run()
        self.assertEqual(summary["schema"], VISUAL_BENCHMARK_SCHEMA)
        self.assertEqual(summary["claim_ceiling"], "structural_only")
        self.assertEqual(summary["native_live_qualification"], "not_run")
        self.assertEqual(summary["ollama_live_qualification"], "not_run")
        self.assertEqual(summary["media_decode"], "not_run")
        self.assertEqual(summary["network_contact"], "not_run")
        wire = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("http://", wire)
        self.assertNotIn("https://", wire)
        self.assertNotIn("/mnt/", wire)
        self.assertNotIn("prompt text", wire)

    def test_unsafe_locator_and_unfrozen_threshold_fail_closed(self) -> None:
        plan = build_default_visual_benchmark_plan()
        with self.assertRaises(VisualBenchmarkError):
            replace(plan.fixtures[0], source_fingerprint="https://private.example/media")
        with self.assertRaises(VisualBenchmarkError):
            replace(plan.metrics[0], minimum=None, maximum=None)

    def test_qualified_profile_requires_complete_measurements_and_cleanup(self) -> None:
        plan = build_default_visual_benchmark_plan()
        candidate = plan.candidates[0]
        with self.assertRaises(VisualBenchmarkError):
            replace(candidate, disposition=VisualDisposition.QUALIFIED)
        with self.assertRaises(VisualBenchmarkError):
            replace(
                candidate,
                disposition=VisualDisposition.QUALIFIED,
                supports_cancellation_cleanup=True,
                measurements=(),
            )


if __name__ == "__main__":
    unittest.main()
