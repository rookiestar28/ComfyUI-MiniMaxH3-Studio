"""M12-07 audio-perception acceptance gate tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from comfyui_h3_context.core import (
    AudioAcceptanceError,
    AudioAcceptanceRuntime,
    AudioAcceptanceStatus,
    AudioBenchmarkPlan,
    AudioCapabilityMeasurement,
    AudioDisposition,
    AudioProfileEvidence,
    build_default_audio_benchmark_plan,
    evaluate_audio_acceptance,
)
from scripts.m12_07_audio_acceptance_fixture import run

ROOT = Path(__file__).resolve().parents[1]


def _qualified_plan(*, score_basis_points: int = 9_500) -> tuple[AudioBenchmarkPlan, int]:
    plan = build_default_audio_benchmark_plan()
    candidate = plan.candidates[0]
    qualified = replace(
        candidate,
        disposition=AudioDisposition.QUALIFIED,
        disposition_reason="injected qualified test profile",
    )
    return replace(plan, candidates=(qualified, *plan.candidates[1:])), score_basis_points


def _evidence(plan: AudioBenchmarkPlan, score_basis_points: int = 9_500) -> AudioProfileEvidence:
    candidate = plan.candidates[0]
    return AudioProfileEvidence(
        candidate_id=candidate.candidate_id,
        latency_ms=100,
        peak_vram_mb=128,
        peak_ram_mb=256,
        deterministic=True,
        platform_supported=True,
        cancellation_cleanup_verified=True,
        exact_dialogue_ownership_preserved=True,
        no_speech_and_degraded_bounded=True,
        measurements=tuple(
            AudioCapabilityMeasurement(capability, score_basis_points)
            for capability in candidate.capabilities
        ),
    )


class AudioAcceptanceTests(unittest.TestCase):
    def test_schema_fixture_is_redacted_and_all_negative_is_unsupported_research(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "audio_acceptance_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/audio_acceptance_v1.schema.json"
        )
        summary = run()
        self.assertEqual(summary["status"], AudioAcceptanceStatus.UNSUPPORTED_RESEARCH.value)
        self.assertEqual(summary["executable_candidate_ids"], [])
        self.assertEqual(summary["native_live_execution"], "not_started")
        self.assertEqual(summary["ollama_live_execution"], "not_started")
        self.assertTrue(summary["optional_dependencies_absent"])
        wire = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("http://", wire)
        self.assertNotIn("https://", wire)
        self.assertNotIn("/mnt/", wire)

    def test_family_dispositions_are_separate_and_deterministic(self) -> None:
        plan = build_default_audio_benchmark_plan()
        first = evaluate_audio_acceptance(plan)
        second = evaluate_audio_acceptance(plan)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(
            {summary.family.value for summary in first.family_summaries},
            {"comfyui_native", "ollama", "specialist"},
        )
        self.assertFalse(first.fallback_used)
        self.assertEqual(first.status, AudioAcceptanceStatus.UNSUPPORTED_RESEARCH)

    def test_hidden_fallback_network_media_or_optional_dependency_fails_closed(self) -> None:
        plan = build_default_audio_benchmark_plan()
        for runtime in (
            AudioAcceptanceRuntime(fallback_used=True),
            AudioAcceptanceRuntime(network_contacted=True),
            AudioAcceptanceRuntime(media_started=True),
            AudioAcceptanceRuntime(optional_dependencies_absent=False),
        ):
            with self.assertRaises(AudioAcceptanceError):
                evaluate_audio_acceptance(plan, runtime=runtime)

    def test_qualified_profile_requires_complete_runtime_and_capability_evidence(self) -> None:
        plan, _ = _qualified_plan()
        rejected = evaluate_audio_acceptance(plan)
        self.assertEqual(rejected.status, AudioAcceptanceStatus.REJECTED)
        self.assertEqual(rejected.executable_candidate_ids, ())
        self.assertIn("runtime_evidence_missing", rejected.candidate_receipts[0].failed_checks)

        qualified = evaluate_audio_acceptance(
            plan, runtime=AudioAcceptanceRuntime(profile_evidence=(_evidence(plan),))
        )
        self.assertEqual(qualified.status, AudioAcceptanceStatus.QUALIFIED)
        self.assertEqual(qualified.executable_candidate_ids, (plan.candidates[0].candidate_id,))
        self.assertEqual(qualified.unsupported_capabilities, ())

    def test_threshold_failure_and_exact_dialogue_or_degraded_failure_never_execute(self) -> None:
        plan, _ = _qualified_plan(score_basis_points=6_000)
        low = evaluate_audio_acceptance(
            plan, runtime=AudioAcceptanceRuntime(profile_evidence=(_evidence(plan, 6_000),))
        )
        self.assertEqual(low.status, AudioAcceptanceStatus.REJECTED)
        self.assertTrue(low.candidate_receipts[0].failed_checks)

        bad_text = replace(_evidence(plan), exact_dialogue_ownership_preserved=False)
        bad = evaluate_audio_acceptance(
            plan, runtime=AudioAcceptanceRuntime(profile_evidence=(bad_text,))
        )
        self.assertEqual(bad.status, AudioAcceptanceStatus.REJECTED)
        self.assertIn("exact_dialogue_ownership_failed", bad.candidate_receipts[0].failed_checks)

        bad_degraded = replace(_evidence(plan), no_speech_and_degraded_bounded=False)
        degraded = evaluate_audio_acceptance(
            plan, runtime=AudioAcceptanceRuntime(profile_evidence=(bad_degraded,))
        )
        self.assertEqual(degraded.status, AudioAcceptanceStatus.REJECTED)
        self.assertIn(
            "degraded_outcome_bound_missing", degraded.candidate_receipts[0].failed_checks
        )

    def test_unknown_evidence_and_unsafe_runtime_fields_fail_closed(self) -> None:
        plan = build_default_audio_benchmark_plan()
        with self.assertRaises(AudioAcceptanceError):
            evaluate_audio_acceptance(
                plan,
                runtime=AudioAcceptanceRuntime(
                    profile_evidence=(replace(_evidence(plan), candidate_id="unknown.audio"),)
                ),
            )
        with self.assertRaises(AudioAcceptanceError):
            AudioAcceptanceRuntime(limitations=("https://private.example",))


if __name__ == "__main__":
    unittest.main()
