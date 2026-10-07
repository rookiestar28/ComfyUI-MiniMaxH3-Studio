"""M12-06 source-owned audio evidence fusion and calibration tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    AudioFusionAuthority,
    AudioFusionCategory,
    AudioFusionResolution,
    AudioFusionRoute,
    AudioFusionSourceRef,
    AudioFusionStatus,
    AudioFusionSupport,
    build_audio_fusion_abstention,
    calibrate_audio_confidence,
    fuse_audio_evidence,
)
from comfyui_h3_context.core.errors import AudioEvidenceFusionError
from scripts.m12_06_audio_fusion_fixture import _request, run

ROOT = Path(__file__).resolve().parents[1]


class AudioEvidenceFusionTests(unittest.TestCase):
    def test_schema_fixture_benchmark_and_receipt_are_frozen(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "audio_evidence_fusion_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"],
            "comfyui-h3-context://contracts/audio_evidence_fusion_v1.schema.json",
        )
        summary = run()
        self.assertEqual(summary["status"], "complete")
        benchmark = cast(dict[str, object], summary["benchmark"])
        self.assertEqual(benchmark["case_count"], 8)
        self.assertEqual(benchmark["held_out_case_count"], 4)
        self.assertEqual(summary["native_route"], "not_contacted")
        self.assertEqual(summary["ollama_route"], "not_contacted")
        self.assertEqual(summary["network"], "disabled")

    def test_consensus_retains_sources_and_authority(self) -> None:
        document = cast(dict[str, object], run()["document"])
        self.assertEqual(document["group_count"], 5)
        groups = cast(list[dict[str, object]], document["groups"])
        consensus = next(group for group in groups if group["group_id"] == "group_transcript")
        self.assertEqual(consensus["resolution"], AudioFusionResolution.CONSENSUS.value)
        self.assertEqual(
            consensus["candidate_ids"],
            ["candidate_dialogue_user", "candidate_dialogue_asr"],
        )

    def test_exact_user_dialogue_cannot_be_overwritten_by_asr(self) -> None:
        request = _request()
        candidates = list(request.candidates)
        user = next(
            item for item in candidates if item.authority is AudioFusionAuthority.USER_AUTHORED
        )
        bad_asr = replace(
            next(item for item in candidates if item.category is AudioFusionCategory.TRANSCRIPT),
            candidate_id="candidate_dialogue_asr_bad",
            claim="different observed words",
        )
        candidates[candidates.index(user) + 1] = bad_asr
        document = fuse_audio_evidence(replace(request, candidates=tuple(candidates)))
        decision = next(item for item in document.decisions if item.group_id == user.group_id)
        self.assertEqual(decision.resolution, AudioFusionResolution.ABSTAINED)
        self.assertEqual(decision.selected_candidate_ids, ())
        self.assertEqual(
            set(decision.alternative_candidate_ids),
            {"candidate_dialogue_user", "candidate_dialogue_asr_bad"},
        )

    def test_disagreement_remains_conflict_or_alternatives(self) -> None:
        document = cast(dict[str, object], run()["document"])
        groups = cast(list[dict[str, object]], document["groups"])
        conflict = next(group for group in groups if group["group_id"] == "group_event_av")
        self.assertEqual(conflict["resolution"], AudioFusionResolution.CONFLICT.value)
        self.assertEqual(conflict["alternative_count"], 2)

    def test_source_anchor_mismatch_fails_closed(self) -> None:
        request = _request()
        bad = replace(
            request.candidates[0],
            source=AudioFusionSourceRef("audio_b", "other_source", "sha256:" + "b" * 64),
        )
        with self.assertRaises(AudioEvidenceFusionError):
            fuse_audio_evidence(replace(request, candidates=(bad, *request.candidates[1:])))

    def test_calibration_is_monotonic_and_fit_split_excludes_held_out(self) -> None:
        profile = replace(
            _request().calibration_profile,
            profile_id="fixture_audio_profile",
            fit_case_ids=("audio_dev_1", "audio_dev_2"),
            held_out_case_ids=("audio_hold_1",),
        )
        self.assertEqual(calibrate_audio_confidence(profile, Decimal("0.20")), Decimal("0.20"))
        self.assertNotIn("audio_hold_1", profile.fit_case_ids)
        with self.assertRaises(AudioEvidenceFusionError):
            replace(profile, fit_case_ids=("audio_dev_1", "audio_hold_1"))

    def test_abstention_improves_held_out_high_impact_error(self) -> None:
        evaluation = cast(dict[str, object], run()["evaluation"])
        self.assertGreaterEqual(
            Decimal(cast(str, evaluation["high_impact_error_improvement"])), Decimal("0.20")
        )
        self.assertTrue(evaluation["abstention_improves_high_impact_error"])

    def test_terminal_abstention_never_becomes_complete(self) -> None:
        request = _request()
        for status in (
            AudioFusionStatus.CANCELLED,
            AudioFusionStatus.CORRUPT,
            AudioFusionStatus.UNSUPPORTED,
        ):
            document = build_audio_fusion_abstention(request, status, "fixture_terminal")
            self.assertEqual(document.status, status)
            self.assertFalse(document.complete)
            self.assertIsNone(document.receipt)

    def test_unsupported_candidate_has_no_claim(self) -> None:
        request = _request()
        unsupported = next(
            candidate
            for candidate in request.candidates
            if candidate.support is AudioFusionSupport.UNSUPPORTED
        )
        self.assertIsNone(unsupported.claim)

    def test_native_and_ollama_routes_are_not_silently_contacted(self) -> None:
        summary = run()
        self.assertEqual(summary["preferred_route"], AudioFusionRoute.COMFYUI_NATIVE.value)
        self.assertEqual(summary["first_fallback"], AudioFusionRoute.OLLAMA.value)
        self.assertFalse(summary["automatic_fallback"])


if __name__ == "__main__":
    unittest.main()
