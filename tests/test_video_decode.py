"""M11-04 source-PTS decode, shot, keyframe, and frame-batch contracts."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

from comfyui_h3_context.adapters.video_decode import InjectedVideoDecodeAdapter
from comfyui_h3_context.core import (
    FrameBatchRoute,
    FrameReference,
    LocalDeviceKind,
    LocalDeviceSpec,
    ShotBoundary,
    ShotBoundaryKind,
    SourcePTS,
    TimePoint,
    VideoDecodeDocument,
    VideoDecodeStatus,
    VideoTimestampPolicy,
    execute_video_decode,
)
from comfyui_h3_context.core.errors import VideoDecodeError
from scripts.m11_04_video_decode_fixture import _document, _request, run

ROOT = Path(__file__).resolve().parents[1]


class VideoDecodeTests(unittest.TestCase):
    def test_schema_fixture_and_benchmark_are_frozen(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "video_decode_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/video_decode_v1.schema.json"
        )
        summary = run()
        benchmark = cast(dict[str, object], summary["benchmark"])
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(benchmark["case_count"], 9)
        self.assertEqual(benchmark["threshold_count"], 5)
        self.assertEqual(summary["cancelled"], "cancelled")
        self.assertEqual(summary["corrupt_outcome"], "corrupt")
        self.assertEqual(summary["ollama_route"], "not_contacted")
        self.assertEqual(summary["ffmpeg"], "not_started")

    def test_source_pts_is_exact_and_authoritative(self) -> None:
        source_pts = SourcePTS(1100, 1, 1000, TimePoint.from_text("1.100"))
        self.assertEqual(source_pts.timestamp.seconds, Decimal("1.100"))
        self.assertTrue(source_pts.source_authoritative)
        with self.assertRaises(VideoDecodeError):
            SourcePTS(1100, 1, 1000, TimePoint.from_text("1.101"))
        with self.assertRaises(VideoDecodeError):
            SourcePTS(1100, 1, 1000, TimePoint.from_text("1.100"), False)

    def test_native_batch_retains_one_to_one_source_pts_mapping(self) -> None:
        request = _request()
        document = execute_video_decode(
            InjectedVideoDecodeAdapter(lambda current, guard: _document(current)),
            request,
            device=LocalDeviceSpec(LocalDeviceKind.CPU),
        )
        batch = document.batches[0]
        self.assertEqual(batch.route, FrameBatchRoute.COMFYUI_NATIVE_VLM)
        self.assertEqual(
            tuple(item.source_pts for item in batch.frames),
            tuple(item.source_pts for item in document.frames),
        )
        self.assertEqual(document.frames[2].source_pts.pts, 1100)
        self.assertEqual(document.shots[1].boundary_ids, ("boundary_1",))

    def test_document_rejects_non_monotonic_frames_and_mismatched_batch_pts(self) -> None:
        document = _document(_request())
        with self.assertRaises(VideoDecodeError):
            VideoDecodeDocument(
                document.document_id,
                document.schema,
                document.status,
                document.selected_asset_ids,
                document.timestamp_policy,
                frames=(document.frames[1], document.frames[0], *document.frames[2:]),
                batches=document.batches,
                boundaries=document.boundaries,
                shots=document.shots,
                receipt=document.receipt,
            )

        wrong_reference = FrameReference(
            document.frames[0].frame_id,
            SourcePTS(1, 1, 1000, TimePoint.from_text("0.001")),
        )
        with self.assertRaises(VideoDecodeError):
            replace(
                document,
                batches=(replace(document.batches[0], frames=(wrong_reference,)),),
            )

    def test_boundary_confidence_and_cross_reference_are_bounded(self) -> None:
        document = _document(_request())
        with self.assertRaises(VideoDecodeError):
            ShotBoundary(
                "boundary.bad",
                "video_a",
                "source_video_a",
                document.boundaries[0].source_pts,
                "frame_0",
                "frame_1",
                ShotBoundaryKind.HARD_CUT,
                Decimal("1.01"),
            )
        with self.assertRaises(VideoDecodeError):
            replace(
                document,
                shots=(replace(document.shots[0], frame_ids=("frame_unknown",)),),
            )

    def test_document_rejects_mismatched_time_bases_and_duplicate_assets(self) -> None:
        document = _document(_request())
        alternate_pts = SourcePTS(1, 1, 2000, TimePoint.from_text("0.0005"))
        alternate_frame = replace(document.frames[0], source_pts=alternate_pts)
        alternate_reference = replace(document.batches[0].frames[0], source_pts=alternate_pts)
        alternate_batch = replace(
            document.batches[0], frames=(alternate_reference, *document.batches[0].frames[1:])
        )
        with self.assertRaises(VideoDecodeError):
            replace(
                document,
                frames=(alternate_frame, *document.frames[1:]),
                batches=(alternate_batch,),
            )
        with self.assertRaises(VideoDecodeError):
            replace(document, selected_asset_ids=("video_a", "video_a"))

    def test_shot_ownership_requires_source_and_keyframe_flags(self) -> None:
        document = _document(_request())
        with self.assertRaises(VideoDecodeError):
            replace(
                document,
                boundaries=(replace(document.boundaries[0], source_id="source.other"),),
            )
        with self.assertRaises(VideoDecodeError):
            replace(
                document,
                shots=(replace(document.shots[0], keyframe_ids=("frame_1",)),),
            )

    def test_empty_payload_and_degraded_outputs_are_explicit(self) -> None:
        request = _request()
        with self.assertRaises(VideoDecodeError):
            replace(request, video_payloads=(b"",))
        abstention = VideoDecodeDocument(
            "decode.partial",
            "h3.video.decode.v1",
            VideoDecodeStatus.PARTIAL,
            request.selected_asset_ids,
            VideoTimestampPolicy.SOURCE_PTS_ONLY,
            diagnostics=("decoder_timeout",),
        )
        self.assertFalse(abstention.complete)
        self.assertEqual(abstention.diagnostics, ("decoder_timeout",))


if __name__ == "__main__":
    unittest.main()
