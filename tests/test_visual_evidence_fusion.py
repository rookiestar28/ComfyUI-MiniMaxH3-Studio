"""M11-07 provenance-preserving visual fusion and confidence calibration tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    CalibrationBin,
    CalibrationProfile,
    FusionResolution,
    FusionRoute,
    FusionSourceRef,
    FusionStatus,
    FusionSupport,
    build_visual_fusion_abstention,
    calibrate_confidence,
    fuse_visual_evidence,
)
from comfyui_h3_context.core.errors import VisualEvidenceFusionError
from scripts.m11_07_visual_fusion_fixture import _request, run

ROOT = Path(__file__).resolve().parents[1]


class VisualEvidenceFusionTests(unittest.TestCase):
    def test_schema_fixture_benchmark_and_receipt_are_frozen(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "visual_evidence_fusion_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"],
            "comfyui-h3-context://contracts/visual_evidence_fusion_v1.schema.json",
        )
        summary = run()
        self.assertEqual(summary["status"], "complete")
        benchmark = cast(dict[str, object], summary["benchmark"])
        self.assertEqual(benchmark["case_count"], 10)
        self.assertEqual(benchmark["held_out_case_count"], 4)
        self.assertEqual(summary["ollama_route"], "not_contacted")
        self.assertEqual(summary["network"], "disabled")

    def test_consensus_preserves_all_candidate_provenance(self) -> None:
        document = cast(dict[str, object], run()["document"])
        self.assertEqual(document["group_count"], 5)
        groups = cast(list[dict[str, object]], document["groups"])
        consensus = next(group for group in groups if group["group_id"] == "group_action")
        self.assertEqual(consensus["resolution"], FusionResolution.CONSENSUS.value)
        self.assertEqual(
            consensus["candidate_ids"],
            ["candidate_action_native", "candidate_action_native_secondary"],
        )
        self.assertEqual(consensus["route_count"], 1)

    def test_conflict_remains_an_explicit_alternative(self) -> None:
        document = cast(dict[str, object], run()["document"])
        groups = cast(list[dict[str, object]], document["groups"])
        conflict = next(group for group in groups if group["group_id"] == "group_camera")
        self.assertEqual(conflict["resolution"], FusionResolution.CONFLICT.value)
        self.assertEqual(conflict["candidate_ids"], ["candidate_camera_a", "candidate_camera_b"])
        self.assertEqual(conflict["alternative_count"], 2)

    def test_source_interval_mismatch_fails_closed(self) -> None:
        request = _request()
        bad = replace(
            request.candidates[0],
            source=FusionSourceRef("video_b", "other_source", "sha256:" + "b" * 64),
        )
        with self.assertRaises(VisualEvidenceFusionError):
            fuse_visual_evidence(replace(request, candidates=(bad, *request.candidates[1:])))

    def test_calibration_is_monotonic_and_fit_split_excludes_held_out(self) -> None:
        profile = CalibrationProfile(
            profile_id="fixture_profile",
            version="1.0.0",
            fit_case_ids=("dev_1", "dev_2"),
            bins=(
                CalibrationBin(Decimal("0"), Decimal("0.5"), Decimal("0.2")),
                CalibrationBin(Decimal("0.5"), Decimal("1"), Decimal("0.8")),
            ),
        )
        self.assertEqual(calibrate_confidence(profile, Decimal("0.25")), Decimal("0.2"))
        self.assertEqual(calibrate_confidence(profile, Decimal("0.75")), Decimal("0.8"))
        self.assertNotIn("held_1", profile.fit_case_ids)
        with self.assertRaises(VisualEvidenceFusionError):
            replace(
                profile,
                fit_case_ids=("dev_1", "held_1"),
                held_out_case_ids=("held_1",),
            )

    def test_abstention_improves_injected_high_impact_error_rate(self) -> None:
        evaluation = cast(dict[str, object], run()["evaluation"])
        self.assertGreaterEqual(
            Decimal(cast(str, evaluation["high_impact_error_improvement"])), Decimal("0.20")
        )
        self.assertTrue(evaluation["abstention_improves_high_impact_error"])

    def test_terminal_abstention_never_becomes_complete(self) -> None:
        request = _request()
        for status in (FusionStatus.CANCELLED, FusionStatus.CORRUPT, FusionStatus.UNSUPPORTED):
            document = build_visual_fusion_abstention(request, status, "fixture_terminal")
            self.assertEqual(document.status, status)
            self.assertFalse(document.complete)
            self.assertIsNone(document.receipt)

    def test_native_route_is_explicit_and_ollama_not_contacted(self) -> None:
        summary = run()
        self.assertEqual(summary["native_route"], FusionRoute.COMFYUI_NATIVE.value)
        self.assertEqual(summary["ollama_route"], "not_contacted")
        self.assertEqual(summary["host_runtime"], "not_started")

    def test_unsupported_candidate_has_no_label(self) -> None:
        request = _request()
        unsupported = next(
            candidate
            for candidate in request.candidates
            if candidate.support is FusionSupport.UNSUPPORTED
        )
        self.assertIsNone(unsupported.label)


if __name__ == "__main__":
    unittest.main()
