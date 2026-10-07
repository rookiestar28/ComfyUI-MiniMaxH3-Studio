"""Offline M11-07 visual evidence fusion and calibration fixture.

The fixture merges injected source-owned candidates only.  It never opens media, loads a model,
launches a process, contacts ComfyUI/Ollama, or claims live calibration quality.
"""

from __future__ import annotations

import argparse
import importlib
import json
from decimal import Decimal

from comfyui_h3_context.adapters.visual_evidence_fusion import (
    InjectedVisualEvidenceFusionAdapter,
)
from comfyui_h3_context.core import (
    FusionCandidate,
    FusionCandidateReceipt,
    FusionCategory,
    FusionEvaluation,
    FusionEvaluationSample,
    FusionRequest,
    FusionRoute,
    FusionSourceRef,
    FusionStatus,
    FusionSupport,
    LocalAdapterCancelledError,
    LocalDeviceKind,
    LocalDeviceSpec,
    Uncertainty,
    UncertaintyKind,
    ValidationSeverity,
    build_default_visual_fusion_benchmark_plan,
    build_default_visual_fusion_calibration_profile,
    build_visual_fusion_abstention,
    evaluate_fusion_benchmark,
    execute_visual_evidence_fusion,
    fuse_visual_evidence,
)

try:
    from scripts.m11_06_action_state_motion_fixture import _document as temporal_document
    from scripts.m11_06_action_state_motion_fixture import _request as temporal_request
except ModuleNotFoundError:
    temporal_fixture = importlib.import_module("m11_06_action_state_motion_fixture")
    temporal_document = temporal_fixture._document
    temporal_request = temporal_fixture._request


def _fp(letter: str) -> str:
    return "sha256:" + letter * 64


class _CancelAfterCheckpoint:
    def __init__(self) -> None:
        self.calls = 0

    def is_cancelled(self) -> bool:
        self.calls += 1
        return self.calls >= 2


def _request() -> FusionRequest:
    temporal = temporal_document(temporal_request())
    interval = temporal.claims[0].interval
    source = FusionSourceRef("video_a", "source_video_a", _fp("a"), interval)

    def receipt(letter: str, adapter_id: str = "native_fusion_primary") -> FusionCandidateReceipt:
        safe = "abcdef"[(ord(letter) - ord("b")) % 6]
        output = "abcdef"[(ord(letter) - ord("a")) % 6]
        return FusionCandidateReceipt(
            FusionRoute.COMFYUI_NATIVE,
            adapter_id,
            "1.0.0",
            "injected_visual_model",
            _fp(safe),
            _fp(output),
        )

    conflict = Uncertainty(
        UncertaintyKind.CONFLICTING,
        "camera estimates disagree",
        ValidationSeverity.WARNING,
    )
    low_confidence = Uncertainty(
        UncertaintyKind.LOW_CONFIDENCE,
        "style signal is weak",
        ValidationSeverity.WARNING,
    )
    unsupported = Uncertainty(
        UncertaintyKind.UNSUPPORTED,
        "selected adapter does not support this category",
        ValidationSeverity.WARNING,
    )
    candidates = (
        FusionCandidate(
            "candidate_action_native",
            "group_action",
            FusionCategory.ACTION,
            "raises arm",
            FusionSupport.SUPPORTED,
            Decimal("0.85"),
            source,
            receipt("b"),
        ),
        FusionCandidate(
            "candidate_action_native_secondary",
            "group_action",
            FusionCategory.ACTION,
            "raises arm",
            FusionSupport.SUPPORTED,
            Decimal("0.80"),
            source,
            receipt("c", "native_fusion_secondary"),
        ),
        FusionCandidate(
            "candidate_state_native",
            "group_state",
            FusionCategory.STATE,
            "door opens",
            FusionSupport.SUPPORTED,
            Decimal("0.88"),
            source,
            receipt("d"),
        ),
        FusionCandidate(
            "candidate_camera_a",
            "group_camera",
            FusionCategory.CAMERA_MOTION,
            "camera pans right",
            FusionSupport.SUPPORTED,
            Decimal("0.62"),
            source,
            receipt("e"),
            (conflict,),
        ),
        FusionCandidate(
            "candidate_camera_b",
            "group_camera",
            FusionCategory.CAMERA_MOTION,
            "camera is static",
            FusionSupport.SUPPORTED,
            Decimal("0.58"),
            source,
            receipt("f", "native_fusion_secondary"),
            (conflict,),
        ),
        FusionCandidate(
            "candidate_style",
            "group_style",
            FusionCategory.STYLE,
            "warm high contrast",
            FusionSupport.UNCERTAIN,
            Decimal("0.20"),
            source,
            receipt("g"),
            (low_confidence,),
        ),
        FusionCandidate(
            "candidate_unsupported",
            "group_unsupported",
            FusionCategory.IDENTITY,
            None,
            FusionSupport.UNSUPPORTED,
            None,
            source,
            receipt("h"),
            (unsupported,),
        ),
    )
    plan = build_default_visual_fusion_benchmark_plan()
    profile = build_default_visual_fusion_calibration_profile(plan)
    return FusionRequest(candidates, profile, plan, "fusion_doc_fixture")


def _evaluation(request: FusionRequest) -> FusionEvaluation:
    plan = request.benchmark
    if plan is None:
        raise RuntimeError("fixture benchmark is required")
    samples = (
        # The two high-impact errors are abstained, so the ablation comparison is explicit.
        # The calibrated scores are derived from the profile, not fitted on these cases.
        FusionEvaluationSample(
            "fusion.low_confidence",
            request.calibration_profile.calibrate(Decimal("0.20")),
            False,
            True,
            True,
        ),
        FusionEvaluationSample(
            "fusion.high_impact_abstention",
            request.calibration_profile.calibrate(Decimal("0.10")),
            False,
            True,
            True,
        ),
        FusionEvaluationSample(
            "fusion.held_out_calibration",
            request.calibration_profile.calibrate(Decimal("0.80")),
            True,
            False,
            False,
        ),
        FusionEvaluationSample(
            "fusion.terminal",
            request.calibration_profile.calibrate(Decimal("0.00")),
            False,
            True,
            False,
        ),
    )
    return evaluate_fusion_benchmark(plan, request.calibration_profile, samples)


def run() -> dict[str, object]:
    request = _request()
    document = execute_visual_evidence_fusion(
        InjectedVisualEvidenceFusionAdapter(lambda current, guard: fuse_visual_evidence(current)),
        request,
        device=LocalDeviceSpec(LocalDeviceKind.CPU),
    )
    evaluation = _evaluation(request)
    document = document.__class__(
        document.document_id,
        document.status,
        document.candidates,
        document.groups,
        document.decisions,
        document.calibration_profile,
        document.receipt,
        document.benchmark,
        evaluation,
        document.diagnostics,
    )
    cancelled = "not_started"
    try:
        execute_visual_evidence_fusion(
            InjectedVisualEvidenceFusionAdapter(
                lambda current, guard: fuse_visual_evidence(current)
            ),
            request,
            device=LocalDeviceSpec(LocalDeviceKind.CPU),
            cancellation_probe=_CancelAfterCheckpoint(),
        )
    except LocalAdapterCancelledError:
        cancelled = "cancelled"
    corrupt = build_visual_fusion_abstention(request, FusionStatus.CORRUPT, "fusion_corrupt")
    plan = request.benchmark
    if plan is None:
        raise RuntimeError("fixture benchmark is required")
    return {
        "schema": document.schema,
        "status": document.status.value,
        "document": document.to_public_dict(),
        "benchmark": {
            "case_count": len(plan.cases),
            "held_out_case_count": len(plan.held_out_case_ids),
            "threshold_count": len(plan.thresholds),
            "fingerprint": plan.fingerprint,
        },
        "evaluation": evaluation.to_wire(),
        "cancelled": cancelled,
        "corrupt_outcome": corrupt.status.value,
        "native_route": document.receipt.route.value
        if document.receipt is not None
        else "not_started",
        "ollama_route": "not_contacted",
        "network": "disabled",
        "fusion_runtime": "not_started",
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
        print("M11-07 injected visual fusion fixture: PASS")


if __name__ == "__main__":
    main()
