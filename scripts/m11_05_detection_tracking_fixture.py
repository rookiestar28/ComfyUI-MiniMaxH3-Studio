"""Offline M11-05 detection/segmentation/tracking/re-ID fixture.

The fixture injects source-owned metadata only.  It never opens media, loads a detector or
embedding model, launches a process, contacts ComfyUI/Ollama, or claims live perception quality.
"""

from __future__ import annotations

import argparse
import importlib
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any, cast

from comfyui_h3_context.adapters.perception_tracking import InjectedPerceptionTrackingAdapter
from comfyui_h3_context.core import (
    Detection,
    IdentityEmbedding,
    IdentityPolicy,
    LocalAdapterCancelledError,
    LocalDeviceKind,
    LocalDeviceSpec,
    MaskRepresentation,
    NormalizedPoint,
    NormalizedRegion,
    PerceptionRoute,
    ReIDCandidate,
    ReIDResolution,
    SegmentationMask,
    TrackingDocument,
    TrackingReceipt,
    TrackingRequest,
    TrackingStatus,
    TrackingTrack,
    TrackLink,
    TrackVisibility,
    VideoDecodeDocument,
    VideoDecodeRequest,
    build_default_tracking_benchmark_plan,
    build_tracking_abstention,
    execute_perception_tracking,
)
from comfyui_h3_context.core.evidence import Uncertainty, UncertaintyKind


def _load_decode_fixture() -> tuple[
    Callable[[VideoDecodeRequest], VideoDecodeDocument], Callable[[], VideoDecodeRequest]
]:
    try:
        from scripts.m11_04_video_decode_fixture import _document, _request

        return _document, _request
    except ModuleNotFoundError:
        module: Any = importlib.import_module("m11_04_video_decode_fixture")
        return (
            cast(Callable[[VideoDecodeRequest], VideoDecodeDocument], module._document),
            cast(Callable[[], VideoDecodeRequest], module._request),
        )


decode_document, decode_request = _load_decode_fixture()


def _fp(letter: str) -> str:
    return "sha256:" + letter * 64


class _CancelAfterCheckpoint:
    def __init__(self) -> None:
        self.calls = 0

    def is_cancelled(self) -> bool:
        self.calls += 1
        return self.calls >= 2


def _request(*, identity_policy: IdentityPolicy = IdentityPolicy.DISABLED) -> TrackingRequest:
    media_request = decode_request()
    return TrackingRequest(
        decode_document=decode_document(media_request),
        route=PerceptionRoute.COMFYUI_NATIVE,
        adapter_id="injected_tracking",
        identity_policy=identity_policy,
        reference_ids=("ref_a", "ref_b"),
    )


def _document(
    request: TrackingRequest,
    *,
    embeddings: tuple[IdentityEmbedding, ...] = (),
) -> TrackingDocument:
    decode = request.decode_document
    frame_0, frame_1, frame_2, frame_3 = decode.frames[:4]
    uncertain = Uncertainty(UncertaintyKind.AMBIGUOUS, "partial occlusion retained")
    detections = (
        Detection(
            "det_0",
            frame_0.asset_id,
            frame_0.source_id,
            frame_0.frame_id,
            "shot_1",
            frame_0.source_pts,
            "person in red coat",
            NormalizedRegion(Decimal("0.10"), Decimal("0.20"), Decimal("0.20"), Decimal("0.50")),
            Decimal("0.96"),
            "mask_0",
            (uncertain,),
        ),
        Detection(
            "det_1",
            frame_1.asset_id,
            frame_1.source_id,
            frame_1.frame_id,
            "shot_1",
            frame_1.source_pts,
            "person in red coat",
            NormalizedRegion(Decimal("0.11"), Decimal("0.20"), Decimal("0.19"), Decimal("0.49")),
            Decimal("0.71"),
            "mask_1",
            (uncertain,),
        ),
        Detection(
            "det_2",
            frame_2.asset_id,
            frame_2.source_id,
            frame_2.frame_id,
            "shot_2",
            frame_2.source_pts,
            "person in red coat",
            NormalizedRegion(Decimal("0.13"), Decimal("0.19"), Decimal("0.21"), Decimal("0.51")),
            Decimal("0.89"),
            "mask_2",
        ),
        Detection(
            "det_3",
            frame_3.asset_id,
            frame_3.source_id,
            frame_3.frame_id,
            "shot_2",
            frame_3.source_pts,
            "look-alike person",
            NormalizedRegion(Decimal("0.52"), Decimal("0.20"), Decimal("0.20"), Decimal("0.50")),
            Decimal("0.83"),
            None,
            (uncertain,),
        ),
    )
    polygon = (
        NormalizedPoint(Decimal("0.10"), Decimal("0.20")),
        NormalizedPoint(Decimal("0.30"), Decimal("0.20")),
        NormalizedPoint(Decimal("0.30"), Decimal("0.70")),
        NormalizedPoint(Decimal("0.10"), Decimal("0.70")),
    )
    masks = (
        SegmentationMask(
            "mask_0",
            frame_0.asset_id,
            frame_0.source_id,
            frame_0.frame_id,
            "shot_1",
            "det_0",
            frame_0.source_pts,
            frame_0.width,
            frame_0.height,
            MaskRepresentation.POLYGON,
            polygon,
        ),
        SegmentationMask(
            "mask_1",
            frame_1.asset_id,
            frame_1.source_id,
            frame_1.frame_id,
            "shot_1",
            "det_1",
            frame_1.source_pts,
            frame_1.width,
            frame_1.height,
            MaskRepresentation.POLYGON,
            polygon,
            uncertainties=(uncertain,),
        ),
        SegmentationMask(
            "mask_2",
            frame_2.asset_id,
            frame_2.source_id,
            frame_2.frame_id,
            "shot_2",
            "det_2",
            frame_2.source_pts,
            frame_2.width,
            frame_2.height,
            MaskRepresentation.FINGERPRINT,
            mask_fingerprint=_fp("a"),
        ),
    )
    links = (
        TrackLink("det_0", frame_0.frame_id, "shot_1", frame_0.source_pts),
        TrackLink(
            "det_1", frame_1.frame_id, "shot_1", frame_1.source_pts, TrackVisibility.OCCLUDED
        ),
        TrackLink(
            "det_2", frame_2.frame_id, "shot_2", frame_2.source_pts, TrackVisibility.RE_ENTERED
        ),
    )
    tracks = (
        TrackingTrack(
            "track_1",
            frame_0.asset_id,
            frame_0.source_id,
            links,
            ("shot_1", "shot_2"),
            "person in red coat",
            Decimal("0.88"),
            ("ref_a", "ref_b"),
            (uncertain,),
        ),
        TrackingTrack(
            "track_2",
            frame_3.asset_id,
            frame_3.source_id,
            (TrackLink("det_3", frame_3.frame_id, "shot_2", frame_3.source_pts),),
            ("shot_2",),
            "look-alike person",
            Decimal("0.62"),
            (),
            (uncertain,),
        ),
    )
    candidates = (
        ReIDCandidate(
            "candidate_1",
            "track_1",
            ("ref_a", "ref_b"),
            Decimal("0.62"),
            ReIDResolution.AMBIGUOUS,
            uncertainties=(uncertain,),
        ),
    )
    decode_receipt = decode.receipt
    if decode_receipt is None:
        raise RuntimeError("fixture decode receipt is required")
    receipt = TrackingReceipt(
        "injected_tracking",
        "1.0.0",
        request.route,
        "injected_tracking_model",
        _fp("b"),
        _fp("d"),
        (decode_receipt.source_fingerprint,),
        request.identity_policy,
    )
    return TrackingDocument(
        "tracking_doc_fixture",
        "h3.visual.perception_tracking.v1",
        TrackingStatus.COMPLETE,
        decode,
        request.route,
        request.identity_policy,
        detections,
        masks,
        tracks,
        embeddings,
        candidates,
        receipt,
    )


def run() -> dict[str, object]:
    request = _request()
    document = execute_perception_tracking(
        InjectedPerceptionTrackingAdapter(lambda current, guard: _document(current)),
        request,
        device=LocalDeviceSpec(LocalDeviceKind.CPU),
    )
    cancelled = "not_started"
    try:
        execute_perception_tracking(
            InjectedPerceptionTrackingAdapter(lambda current, guard: _document(current)),
            request,
            device=LocalDeviceSpec(LocalDeviceKind.CPU),
            cancellation_probe=_CancelAfterCheckpoint(),
        )
    except LocalAdapterCancelledError:
        cancelled = "cancelled"
    corrupt = build_tracking_abstention(request, TrackingStatus.CORRUPT, "perception_corrupt")
    benchmark = build_default_tracking_benchmark_plan()
    return {
        "schema": document.schema,
        "status": document.status.value,
        "document": document.to_public_dict(),
        "benchmark": {
            "case_count": len(benchmark.cases),
            "threshold_count": len(benchmark.thresholds),
            "fingerprint": benchmark.fingerprint,
        },
        "cancelled": cancelled,
        "corrupt_outcome": corrupt.status.value,
        "native_route": document.route_value.value,
        "ollama_route": "not_contacted",
        "network": "disabled",
        "detector": "not_started",
        "segmenter": "not_started",
        "embedding_runtime": "not_started",
        "host_runtime": "not_started",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the redacted fixture summary")
    args = parser.parse_args()
    result = run()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print("M11-05 injected detection/tracking fixture: PASS")


if __name__ == "__main__":
    main()
