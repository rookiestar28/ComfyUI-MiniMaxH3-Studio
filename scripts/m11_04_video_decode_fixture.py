"""Offline M11-04 source-PTS video decode fixture.

The fixture injects decoded frame metadata and uses the explicit decoder seam.  It never launches
ffprobe/ffmpeg, opens media, contacts a model/Ollama server, or claims real VFR/shot quality.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from typing import cast

from comfyui_h3_context.adapters.video_decode import InjectedVideoDecodeAdapter
from comfyui_h3_context.core import (
    AssetRole,
    DecodedFrame,
    DecodedShot,
    FrameBatchRoute,
    FrameReference,
    LocalAdapterCancelledError,
    LocalDeviceKind,
    LocalDeviceSpec,
    MediaKind,
    ReferenceAsset,
    ShotBoundary,
    ShotBoundaryKind,
    SourcePTS,
    TaskMode,
    TimePoint,
    VideoAnalysisRequest,
    VideoDecodeDocument,
    VideoDecodeReceipt,
    VideoDecodeRequest,
    VideoDecodeStatus,
    VideoFrameBatch,
    VideoOrientation,
    VideoSamplingConfig,
    VideoSamplingStrategy,
    VideoSelection,
    VideoTimestampPolicy,
    build_default_video_decode_benchmark_plan,
    build_reference_registry,
    build_video_decode_abstention,
    execute_video_decode,
)


def _fp(letter: str) -> str:
    return "sha256:" + letter * 64


def _frame_fp(index: int) -> str:
    return _fp(("d", "e", "f", "0", "1")[index])


class _CancelAfterCheckpoint:
    def __init__(self) -> None:
        self.calls = 0

    def is_cancelled(self) -> bool:
        self.calls += 1
        return self.calls >= 2


def _request() -> VideoDecodeRequest:
    registry = build_reference_registry(
        (ReferenceAsset("video_a", MediaKind.VIDEO, AssetRole.REFERENCE, 1),)
    )
    video_request = VideoAnalysisRequest(
        task_mode=TaskMode.REF2VA,
        reference_registry=registry,
        selections=(
            VideoSelection(
                "video_a",
                "source_video_a",
                VideoOrientation.ROTATE_90,
                declared_size_bytes=7,
                declared_duration_seconds=Decimal("2.6"),
                declared_width=640,
                declared_height=360,
                declared_frame_count=5,
                declared_frame_rate=Decimal("2"),
                variable_frame_rate=True,
                has_audio_track=False,
            ),
        ),
        sampling=VideoSamplingConfig(
            strategy=VideoSamplingStrategy.HYBRID,
            max_samples=8,
            max_keyframes=3,
            max_shots=3,
            minimum_shot_duration=Decimal("0.1"),
            scene_change_threshold=Decimal("0.5"),
        ),
    )
    return VideoDecodeRequest(
        video_request=video_request,
        video_payloads=(b"video-a",),
        source_fingerprints=(_fp("c"),),
        max_frames=8,
        max_keyframes=3,
        max_shots=3,
    )


def _pts(value: int) -> SourcePTS:
    raw = f"{Decimal(value) / Decimal(1000):.3f}"
    return SourcePTS(value, 1, 1000, TimePoint.from_text(raw))


def _document(request: VideoDecodeRequest) -> VideoDecodeDocument:
    source = "source_video_a"
    frame_values = (0, 500, 1100, 2000, 2600)
    frames = tuple(
        DecodedFrame(
            frame_id=f"frame_{index}",
            asset_id="video_a",
            source_id=source,
            source_pts=_pts(value),
            width=640,
            height=360,
            content_fingerprint=_frame_fp(index),
            keyframe=index in {0, 2, 4},
            frame_index=index,
            orientation=VideoOrientation.ROTATE_90,
        )
        for index, value in enumerate(frame_values)
    )
    boundary = ShotBoundary(
        "boundary_1",
        "video_a",
        source,
        _pts(1100),
        "frame_1",
        "frame_2",
        ShotBoundaryKind.HARD_CUT,
        Decimal("0.97"),
    )
    batch = VideoFrameBatch(
        "batch_native_1",
        "video_a",
        source,
        FrameBatchRoute.COMFYUI_NATIVE_VLM,
        "comfyui_native_vlm",
        tuple(FrameReference(frame.frame_id, frame.source_pts) for frame in frames),
    )
    shots = (
        DecodedShot(
            "shot_1",
            "video_a",
            source,
            _pts(0),
            _pts(1100),
            ("frame_0", "frame_1"),
            ("frame_0",),
        ),
        DecodedShot(
            "shot_2",
            "video_a",
            source,
            _pts(1100),
            _pts(2601),
            ("frame_2", "frame_3", "frame_4"),
            ("frame_2", "frame_4"),
            ("boundary_1",),
        ),
    )
    receipt = VideoDecodeReceipt(
        "injected_media_decode",
        "1.0.0",
        "vfr_source_pts",
        request.source_fingerprints[0],
        _fp("e"),
        VideoTimestampPolicy.SOURCE_PTS_ONLY,
    )
    return VideoDecodeDocument(
        "decode_doc_fixture",
        "h3.video.decode.v1",
        VideoDecodeStatus.COMPLETE,
        request.selected_asset_ids,
        VideoTimestampPolicy.SOURCE_PTS_ONLY,
        frames=frames,
        batches=(batch,),
        boundaries=(boundary,),
        shots=shots,
        receipt=receipt,
    )


def run() -> dict[str, object]:
    request = _request()
    document = execute_video_decode(
        InjectedVideoDecodeAdapter(lambda current, guard: _document(current)),
        request,
        device=LocalDeviceSpec(LocalDeviceKind.CPU),
    )
    if not document.complete:
        raise AssertionError("decode fixture did not return a complete document")
    if document.timestamp_policy is not VideoTimestampPolicy.SOURCE_PTS_ONLY:
        raise AssertionError("decode fixture changed timestamp policy")
    if document.frames[2].source_pts.pts != 1100:
        raise AssertionError("VFR source PTS was not retained")
    if document.batches[0].route is not FrameBatchRoute.COMFYUI_NATIVE_VLM:
        raise AssertionError("frame route was not explicit")
    if document.shots[1].boundary_ids != ("boundary_1",):
        raise AssertionError("shot/boundary ownership was not retained")

    try:
        execute_video_decode(
            InjectedVideoDecodeAdapter(lambda current, guard: _document(current)),
            request,
            device=LocalDeviceSpec(LocalDeviceKind.CPU),
            cancellation_probe=_CancelAfterCheckpoint(),
        )
    except LocalAdapterCancelledError:
        cancelled = "cancelled"
    else:
        cancelled = "not_cancelled"
    plan = build_default_video_decode_benchmark_plan()
    metric_values = {
        "boundary_recall": "1.0",
        "boundary_time_tolerance": "0.0",
        "keyframe_recall": "1.0",
        "source_pts_mapping_accuracy": "1.0",
        "timestamp_drift": "0",
    }
    abstained = build_video_decode_abstention(request, VideoDecodeStatus.CORRUPT, "corrupt_input")
    return {
        "schema": "h3.m11_04.video_decode.fixture.v1",
        "status": "complete",
        "document": document.to_public_dict(),
        "benchmark": {
            "fingerprint": plan.fingerprint,
            "case_count": len(plan.cases),
            "threshold_count": len(plan.thresholds),
            "metric_values": metric_values,
            "abstained_cases": [case.case_id for case in plan.cases if case.should_abstain],
        },
        "cancelled": cancelled,
        "corrupt_outcome": abstained.status.value,
        "native_vlm_route": "explicit_batch_mapping_only",
        "ollama_route": "not_contacted",
        "decoder_runtime": "injected_only",
        "ffmpeg": "not_started",
        "network": "disabled",
        "host_runtime": "not_started",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit one redacted JSON object")
    args = parser.parse_args()
    summary = run()
    if args.json:
        print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
    else:
        document = cast(dict[str, object], summary["document"])
        print(
            "M11-04 video decode fixture "
            f"{document['document_id']}: {document['frame_count']} frames, "
            f"{document['shot_count']} shots, injected/static only"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
