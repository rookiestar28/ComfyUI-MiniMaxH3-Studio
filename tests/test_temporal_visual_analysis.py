"""M11-06 source-PTS action, state, motion, camera, edit, and style contracts."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    ObservationResolution,
    TemporalClaimKind,
    TemporalInterval,
    TemporalRoute,
    TemporalStatus,
    TemporalSupport,
    TimePoint,
    build_temporal_visual_abstention,
)
from comfyui_h3_context.core.errors import TemporalVisualAnalysisError
from comfyui_h3_context.core.video_decode import SourcePTS
from scripts.m11_06_action_state_motion_fixture import _document, _request, run

ROOT = Path(__file__).resolve().parents[1]


class TemporalVisualAnalysisTests(unittest.TestCase):
    def test_schema_fixture_and_benchmark_are_frozen(self) -> None:
        schema = json.loads(
            (
                ROOT / "governance" / "contracts" / "temporal_visual_analysis_v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["$id"],
            "comfyui-h3-context://contracts/temporal_visual_analysis_v1.schema.json",
        )
        summary = run()
        benchmark = cast(dict[str, object], summary["benchmark"])
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(benchmark["case_count"], 9)
        self.assertEqual(benchmark["threshold_count"], 6)
        self.assertEqual(summary["cancelled"], "cancelled")
        self.assertEqual(summary["corrupt_outcome"], "corrupt")
        self.assertEqual(summary["ollama_route"], "not_contacted")

    def test_all_temporal_capabilities_retain_source_pts(self) -> None:
        document = _document(_request())
        kinds = {claim.kind for claim in document.claims}
        self.assertEqual(
            kinds,
            {
                TemporalClaimKind.SUBJECT_ACTION,
                TemporalClaimKind.OBJECT_STATE_CHANGE,
                TemporalClaimKind.OPTICAL_MOTION,
                TemporalClaimKind.CAMERA_MOTION,
                TemporalClaimKind.COMPOSITION,
                TemporalClaimKind.EDIT_TRANSITION,
                TemporalClaimKind.STYLE,
            },
        )
        interval = document.claims[0].interval
        self.assertEqual(interval.start.pts, 0)
        self.assertEqual(interval.end.pts, 1100)
        self.assertTrue(interval.start.source_authoritative)

    def test_interval_rejects_mismatched_time_base_or_fps_synthesis(self) -> None:
        document = _document(_request())
        claim = document.claims[0]
        wrong_end = SourcePTS(1100, 1, 2000, TimePoint.from_text("0.550"))
        with self.assertRaises(TemporalVisualAnalysisError):
            TemporalInterval(
                claim.interval.asset_id,
                claim.interval.source_id,
                claim.interval.start,
                wrong_end,
                claim.interval.frame_ids,
                claim.interval.shot_ids,
            )
        wrong_pts = SourcePTS(1, 1, 1000, TimePoint.from_text("0.001"))
        with self.assertRaises(TemporalVisualAnalysisError):
            replace(
                document,
                claims=(replace(claim, interval=replace(claim.interval, start=wrong_pts)),),
            )

    def test_disagreement_preserves_alternatives(self) -> None:
        document = _document(_request())
        disagreement = next(
            item
            for item in document.observations
            if item.resolution is ObservationResolution.DISAGREEMENT
        )
        self.assertEqual(len(disagreement.claim_ids), 2)
        labels = {
            claim.label for claim in document.claims if claim.claim_id in disagreement.claim_ids
        }
        self.assertEqual(labels, {"raises arm", "waves hand"})

    def test_unsupported_claim_cannot_fabricate_a_label(self) -> None:
        document = _document(_request())
        unsupported = next(
            claim for claim in document.claims if claim.support is TemporalSupport.UNSUPPORTED
        )
        self.assertIsNone(unsupported.label)
        with self.assertRaises(TemporalVisualAnalysisError):
            replace(
                document,
                claims=(
                    replace(unsupported, label="invented event"),
                    *document.claims[1:],
                ),
            )

    def test_claim_track_ownership_is_source_owned(self) -> None:
        document = _document(_request())
        claim = document.claims[0]
        with self.assertRaises(TemporalVisualAnalysisError):
            replace(
                document,
                claims=(replace(claim, track_ids=("track_unknown",)), *document.claims[1:]),
            )

    def test_observation_references_only_matching_claims(self) -> None:
        document = _document(_request())
        observation = document.observations[0]
        with self.assertRaises(TemporalVisualAnalysisError):
            replace(
                document,
                observations=(
                    replace(observation, claim_ids=("claim_unknown",)),
                    *document.observations[1:],
                ),
            )

    def test_abstention_and_native_route_do_not_contact_ollama(self) -> None:
        request = _request()
        abstention = build_temporal_visual_abstention(
            request, TemporalStatus.CORRUPT, "temporal_corrupt"
        )
        self.assertEqual(abstention.status, TemporalStatus.CORRUPT)
        self.assertFalse(abstention.complete)
        self.assertEqual(
            cast(TemporalRoute, _document(request).route).value,
            TemporalRoute.COMFYUI_NATIVE.value,
        )


if __name__ == "__main__":
    unittest.main()
