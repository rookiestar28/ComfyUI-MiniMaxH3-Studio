"""Offline M11-06 action/state/motion/camera/edit/style fixture.

The fixture injects timestamped claims only.  It never opens media, loads a temporal model,
launches a process, contacts ComfyUI/Ollama, or claims live temporal quality.
"""

from __future__ import annotations

import argparse
import importlib
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any, cast

from comfyui_h3_context.adapters.temporal_visual_analysis import InjectedTemporalVisualAdapter
from comfyui_h3_context.core import (
    LocalAdapterCancelledError,
    LocalDeviceKind,
    LocalDeviceSpec,
    ObservationResolution,
    SourcePTS,
    TemporalClaim,
    TemporalClaimKind,
    TemporalInterval,
    TemporalObservation,
    TemporalRoute,
    TemporalStatus,
    TemporalSupport,
    TemporalVisualDocument,
    TemporalVisualReceipt,
    TemporalVisualRequest,
    TimePoint,
    TrackingDocument,
    TrackingRequest,
    build_default_temporal_benchmark_plan,
    build_temporal_visual_abstention,
    execute_temporal_visual_analysis,
)


def _fp(letter: str) -> str:
    return "sha256:" + letter * 64


def _load_tracking_fixture() -> tuple[
    Callable[[], TrackingRequest], Callable[[TrackingRequest], TrackingDocument]
]:
    try:
        from scripts.m11_05_detection_tracking_fixture import _document, _request

        return _request, _document
    except ModuleNotFoundError:
        module: Any = importlib.import_module("m11_05_detection_tracking_fixture")
        return (
            cast(Callable[[], TrackingRequest], module._request),
            cast(Callable[[TrackingRequest], TrackingDocument], module._document),
        )


_tracking_request, _tracking_document = _load_tracking_fixture()


class _CancelAfterCheckpoint:
    def __init__(self) -> None:
        self.calls = 0

    def is_cancelled(self) -> bool:
        self.calls += 1
        return self.calls >= 2


def _request() -> TemporalVisualRequest:
    tracking_request = _tracking_request()
    return TemporalVisualRequest(
        tracking_document=_tracking_document(tracking_request),
        route=TemporalRoute.COMFYUI_NATIVE,
        adapter_id="injected_temporal_analysis",
    )


def _interval(
    tracking: TrackingDocument,
    frame_ids: tuple[str, ...],
    shot_ids: tuple[str, ...],
    start_pts: int,
    end_pts: int,
) -> TemporalInterval:
    frame_map = {frame.frame_id: frame for frame in tracking.decode_document.frames}
    source = frame_map[frame_ids[0]].source_pts
    start = SourcePTS(
        start_pts,
        source.time_base_num,
        source.time_base_den,
        TimePoint.from_text(f"{Decimal(start_pts) / Decimal(source.time_base_den):.3f}"),
    )
    end = SourcePTS(
        end_pts,
        source.time_base_num,
        source.time_base_den,
        TimePoint.from_text(f"{Decimal(end_pts) / Decimal(source.time_base_den):.3f}"),
    )
    return TemporalInterval("video_a", "source_video_a", start, end, frame_ids, shot_ids)


def _document(request: TemporalVisualRequest) -> TemporalVisualDocument:
    tracking = request.tracking_document
    shot_one = _interval(tracking, ("frame_0", "frame_1"), ("shot_1",), 0, 1100)
    shot_two = _interval(tracking, ("frame_2", "frame_3", "frame_4"), ("shot_2",), 1100, 2601)

    def claim(
        claim_id: str,
        kind: TemporalClaimKind,
        label: str | None,
        interval: TemporalInterval,
        *,
        support: TemporalSupport = TemporalSupport.SUPPORTED,
        track_ids: tuple[str, ...] = (),
        adapter_id: str = "native_temporal_primary",
    ) -> TemporalClaim:
        return TemporalClaim(
            claim_id,
            interval.asset_id,
            interval.source_id,
            kind,
            label,
            interval,
            request.route,
            adapter_id,
            "injected_temporal_model",
            _fp("b"),
            support,
            Decimal("0.88") if support is not TemporalSupport.UNSUPPORTED else None,
            track_ids,
        )

    claims = (
        claim(
            "claim_action",
            TemporalClaimKind.SUBJECT_ACTION,
            "raises arm",
            shot_one,
            track_ids=("track_1",),
        ),
        claim("claim_state", TemporalClaimKind.OBJECT_STATE_CHANGE, "door opens", shot_one),
        claim("claim_optical", TemporalClaimKind.OPTICAL_MOTION, "object moves right", shot_one),
        claim("claim_camera", TemporalClaimKind.CAMERA_MOTION, "camera pans right", shot_two),
        claim(
            "claim_composition",
            TemporalClaimKind.COMPOSITION,
            "subject shifts to right third",
            shot_two,
        ),
        claim("claim_edit", TemporalClaimKind.EDIT_TRANSITION, "hard cut", shot_two),
        claim("claim_style", TemporalClaimKind.STYLE, "warm high contrast", shot_one),
        claim(
            "claim_action_alt",
            TemporalClaimKind.SUBJECT_ACTION,
            "waves hand",
            shot_one,
            adapter_id="native_temporal_secondary",
            track_ids=("track_1",),
        ),
        claim(
            "claim_unsupported",
            TemporalClaimKind.STYLE,
            None,
            shot_two,
            support=TemporalSupport.UNSUPPORTED,
            adapter_id="native_temporal_primary",
        ),
    )
    observations = (
        *tuple(
            TemporalObservation(
                f"observation_{index}",
                claim_item.asset_id,
                claim_item.source_id,
                claim_item.interval,
                (claim_item.claim_id,),
                ObservationResolution.CONSENSUS,
            )
            for index, claim_item in enumerate(claims[:7], start=1)
        ),
        TemporalObservation(
            "observation_disagreement",
            "video_a",
            "source_video_a",
            shot_one,
            ("claim_action", "claim_action_alt"),
            ObservationResolution.DISAGREEMENT,
        ),
        TemporalObservation(
            "observation_unsupported",
            "video_a",
            "source_video_a",
            shot_two,
            ("claim_unsupported",),
            ObservationResolution.UNSUPPORTED,
        ),
    )
    decode_receipt = tracking.decode_document.receipt
    if decode_receipt is None:
        raise RuntimeError("fixture decode receipt is required")
    receipt = TemporalVisualReceipt(
        "injected_temporal_analysis",
        "1.0.0",
        request.route,
        "injected_temporal_model",
        _fp("b"),
        _fp("d"),
        (decode_receipt.source_fingerprint,),
    )
    return TemporalVisualDocument(
        "temporal_doc_fixture",
        "h3.visual.temporal_analysis.v1",
        TemporalStatus.COMPLETE,
        tracking,
        request.route,
        claims,
        observations,
        receipt,
    )


def run() -> dict[str, object]:
    request = _request()
    document = execute_temporal_visual_analysis(
        InjectedTemporalVisualAdapter(lambda current, guard: _document(current)),
        request,
        device=LocalDeviceSpec(LocalDeviceKind.CPU),
    )
    cancelled = "not_started"
    try:
        execute_temporal_visual_analysis(
            InjectedTemporalVisualAdapter(lambda current, guard: _document(current)),
            request,
            device=LocalDeviceSpec(LocalDeviceKind.CPU),
            cancellation_probe=_CancelAfterCheckpoint(),
        )
    except LocalAdapterCancelledError:
        cancelled = "cancelled"
    corrupt = build_temporal_visual_abstention(request, TemporalStatus.CORRUPT, "temporal_corrupt")
    benchmark = build_default_temporal_benchmark_plan()
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
        "action_runtime": "not_started",
        "motion_runtime": "not_started",
        "camera_runtime": "not_started",
        "style_runtime": "not_started",
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
        print("M11-06 injected temporal visual fixture: PASS")


if __name__ == "__main__":
    main()
