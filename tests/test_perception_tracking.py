"""M11-05 source-owned detection, segmentation, tracking, and re-ID contracts."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    IdentityEmbedding,
    IdentityPolicy,
    NormalizedRegion,
    PerceptionRoute,
    ReIDResolution,
    TimePoint,
    TrackingStatus,
    TrackVisibility,
    build_tracking_abstention,
)
from comfyui_h3_context.core.errors import PerceptionTrackingError
from scripts.m11_05_detection_tracking_fixture import _document, _request, run

ROOT = Path(__file__).resolve().parents[1]


class PerceptionTrackingTests(unittest.TestCase):
    def test_schema_fixture_and_benchmark_are_frozen(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "perception_tracking_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"],
            "comfyui-h3-context://contracts/perception_tracking_v1.schema.json",
        )
        summary = run()
        benchmark = cast(dict[str, object], summary["benchmark"])
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(benchmark["case_count"], 9)
        self.assertEqual(benchmark["threshold_count"], 6)
        self.assertEqual(summary["cancelled"], "cancelled")
        self.assertEqual(summary["corrupt_outcome"], "corrupt")
        self.assertEqual(summary["ollama_route"], "not_contacted")

    def test_detection_and_region_retain_exact_source_pts(self) -> None:
        document = _document(_request())
        detection = document.detections[0]
        self.assertEqual(detection.source_pts, document.decode_document.frames[0].source_pts)
        self.assertEqual(detection.region.x, Decimal("0.10"))
        self.assertEqual(detection.label, "person in red coat")
        self.assertTrue(detection.uncertainties)

    def test_document_rejects_unknown_frame_or_mismatched_pts(self) -> None:
        document = _document(_request())
        with self.assertRaises(PerceptionTrackingError):
            replace(
                document,
                detections=(replace(document.detections[0], frame_id="frame_unknown"),),
            )
        wrong_pts = replace(
            document.detections[0].source_pts,
            pts=1,
            timestamp=TimePoint.from_text("0.001"),
        )
        with self.assertRaises(PerceptionTrackingError):
            replace(
                document,
                detections=(replace(document.detections[0], source_pts=wrong_pts),),
            )
        with self.assertRaises(PerceptionTrackingError):
            NormalizedRegion(Decimal("0.8"), Decimal("0.2"), Decimal("0.3"), Decimal("0.1"))

    def test_mask_is_source_owned_and_bound_to_detection(self) -> None:
        document = _document(_request())
        mask = document.masks[0]
        self.assertEqual(mask.detection_id, document.detections[0].detection_id)
        self.assertEqual(mask.source_pts, document.detections[0].source_pts)
        self.assertGreaterEqual(len(mask.polygon), 3)
        with self.assertRaises(PerceptionTrackingError):
            replace(document, masks=(replace(mask, detection_id="det_unknown"),))

    def test_tracks_preserve_occlusion_reentry_and_shot_cut(self) -> None:
        document = _document(_request())
        track = document.tracks[0]
        self.assertEqual(
            tuple(link.visibility for link in track.links),
            (TrackVisibility.VISIBLE, TrackVisibility.OCCLUDED, TrackVisibility.RE_ENTERED),
        )
        self.assertEqual(track.shot_ids, ("shot_1", "shot_2"))
        with self.assertRaises(PerceptionTrackingError):
            replace(
                document,
                tracks=(
                    replace(
                        track,
                        links=(track.links[1], track.links[0], *track.links[2:]),
                    ),
                ),
            )

    def test_ambiguity_and_multi_reference_candidates_are_not_collapsed(self) -> None:
        document = _document(_request())
        candidate = document.reid_candidates[0]
        self.assertEqual(candidate.resolution, ReIDResolution.AMBIGUOUS)
        self.assertEqual(candidate.reference_ids, ("ref_a", "ref_b"))
        self.assertIsNone(candidate.embedding_id)
        with self.assertRaises(PerceptionTrackingError):
            replace(
                document,
                reid_candidates=(replace(candidate, resolution=ReIDResolution.RESOLVED),),
            )

    def test_identity_embedding_requires_explicit_local_opt_in(self) -> None:
        request = _request(identity_policy=IdentityPolicy.DISABLED)
        embedding = IdentityEmbedding(
            "embedding_1",
            "video_a",
            "source_video_a",
            "track_1",
            "injected_reid",
            "1.0.0",
            "reid_model",
            "sha256:" + "a" * 64,
            128,
            "sha256:" + "b" * 64,
            IdentityPolicy.LOCAL_OPT_IN,
        )
        with self.assertRaises(PerceptionTrackingError):
            _document(request, embeddings=(embedding,))
        opt_in = _request(identity_policy=IdentityPolicy.LOCAL_OPT_IN)
        document = _document(opt_in, embeddings=(embedding,))
        self.assertEqual(document.embeddings[0].policy, IdentityPolicy.LOCAL_OPT_IN)
        self.assertNotIn("vector", json.dumps(document.to_public_dict()))

    def test_abstention_and_native_route_do_not_contact_ollama(self) -> None:
        request = _request()
        abstention = build_tracking_abstention(request, TrackingStatus.CORRUPT, "decoder_corrupt")
        self.assertEqual(abstention.status, TrackingStatus.CORRUPT)
        self.assertFalse(abstention.complete)
        self.assertEqual(_document(request).route, PerceptionRoute.COMFYUI_NATIVE)


if __name__ == "__main__":
    unittest.main()
