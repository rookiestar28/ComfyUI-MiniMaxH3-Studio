"""Offline M12-06 audio evidence fusion and calibration fixture.

Only redacted injected candidates are fused.  The fixture does not open media, launch a process,
contact ComfyUI/Ollama, load a model, or claim executable audio quality.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal

from comfyui_h3_context.core import (
    AUDIO_EVIDENCE_FUSION_SCHEMA,
    AudioFusionAuthority,
    AudioFusionCandidate,
    AudioFusionCandidateReceipt,
    AudioFusionCategory,
    AudioFusionDisposition,
    AudioFusionDocument,
    AudioFusionEvaluation,
    AudioFusionRequest,
    AudioFusionRoute,
    AudioFusionSourceRef,
    AudioFusionStatus,
    AudioFusionSupport,
    LocalAdapterDescriptor,
    LocalAdapterKind,
    LocalBudgetGuard,
    LocalDeviceKind,
    LocalDeviceSpec,
    LocalResourceBudget,
    MediaKind,
    TaskMode,
    Uncertainty,
    UncertaintyKind,
    ValidationSeverity,
    build_audio_fusion_abstention,
    build_default_audio_fusion_benchmark_plan,
    build_default_audio_fusion_calibration_profile,
    evaluate_audio_fusion_benchmark,
    execute_audio_evidence_fusion,
    fuse_audio_evidence,
)


def _fp(letter: str) -> str:
    safe = "abcdef"[(ord(letter.casefold()[0]) - ord("a")) % 6]
    return "sha256:" + safe * 64


class _InjectedAudioFusionAdapter:
    descriptor = LocalAdapterDescriptor(
        adapter_id="fixture_audio_fusion",
        kind=LocalAdapterKind.PERCEPTION,
        adapter_version="1.0.0",
        minimum_version="1.0.0",
        maximum_version="1.0.0",
        supported_task_modes=frozenset({TaskMode.T2VA}),
        supported_media=frozenset({MediaKind.AUDIO}),
        supported_devices=frozenset({LocalDeviceKind.AUTO, LocalDeviceKind.CPU}),
        optional_dependencies=(),
        output_schema=AUDIO_EVIDENCE_FUSION_SCHEMA,
        limits=LocalResourceBudget(
            max_memory_bytes=16_384,
            max_wall_time_seconds=5.0,
            max_references=16,
            max_output_bytes=65_536,
            max_output_items=320,
            max_concurrency=1,
        ),
        supports_determinism=True,
        supports_seed=True,
        supports_cancellation=True,
    )

    def analyze(self, request: AudioFusionRequest, guard: LocalBudgetGuard) -> AudioFusionDocument:
        guard.checkpoint()
        return fuse_audio_evidence(request)


def _candidate_receipt(letter: str) -> AudioFusionCandidateReceipt:
    return AudioFusionCandidateReceipt(
        AudioFusionRoute.STATIC_INJECTED,
        AudioFusionDisposition.QUALIFIED,
        "fixture_audio_adapter",
        "1.0.0",
        "fixture_audio_observation",
        _fp(letter),
        _fp(letter),
    )


def _source(letter: str, source_id: str, anchor: str) -> AudioFusionSourceRef:
    return AudioFusionSourceRef(
        "audio_asset_a",
        source_id,
        _fp(letter),
        "audio_span",
        anchor,
    )


def _request() -> AudioFusionRequest:
    low = Uncertainty(
        UncertaintyKind.LOW_CONFIDENCE, "speaker evidence is weak", ValidationSeverity.WARNING
    )
    conflict = Uncertainty(
        UncertaintyKind.CONFLICTING, "independent evidence disagrees", ValidationSeverity.WARNING
    )
    unsupported = Uncertainty(
        UncertaintyKind.UNSUPPORTED,
        "selected route cannot provide this claim",
        ValidationSeverity.WARNING,
    )
    candidates = (
        AudioFusionCandidate(
            "candidate_dialogue_user",
            "group_transcript",
            AudioFusionCategory.TRANSCRIPT,
            AudioFusionAuthority.USER_AUTHORED,
            "hello there",
            AudioFusionSupport.SUPPORTED,
            Decimal("0.98"),
            _source("a", "source_dialogue", "span_dialogue"),
            _candidate_receipt("a"),
            exact=True,
            high_impact=True,
        ),
        AudioFusionCandidate(
            "candidate_dialogue_asr",
            "group_transcript",
            AudioFusionCategory.TRANSCRIPT,
            AudioFusionAuthority.OBSERVED_ASR,
            "hello there",
            AudioFusionSupport.SUPPORTED,
            Decimal("0.82"),
            _source("a", "source_dialogue", "span_dialogue"),
            _candidate_receipt("b"),
            high_impact=True,
        ),
        AudioFusionCandidate(
            "candidate_speaker_a",
            "group_speaker",
            AudioFusionCategory.SPEAKER_IDENTITY,
            AudioFusionAuthority.SPEAKER_HYPOTHESIS,
            "speaker_a",
            AudioFusionSupport.UNCERTAIN,
            Decimal("0.46"),
            _source("b", "source_speaker", "turn_01"),
            _candidate_receipt("c"),
            (low,),
            high_impact=True,
        ),
        AudioFusionCandidate(
            "candidate_speaker_b",
            "group_speaker",
            AudioFusionCategory.SPEAKER_IDENTITY,
            AudioFusionAuthority.SPEAKER_HYPOTHESIS,
            "speaker_b",
            AudioFusionSupport.UNCERTAIN,
            Decimal("0.44"),
            _source("b", "source_speaker", "turn_01"),
            _candidate_receipt("d"),
            (low, conflict),
            high_impact=True,
        ),
        AudioFusionCandidate(
            "candidate_event_audio",
            "group_event_av",
            AudioFusionCategory.AUDIO_EVENT,
            AudioFusionAuthority.AUDIO_EVENT,
            "door slam",
            AudioFusionSupport.SUPPORTED,
            Decimal("0.78"),
            _source("c", "source_event", "event_01"),
            _candidate_receipt("e"),
            (conflict,),
        ),
        AudioFusionCandidate(
            "candidate_event_av",
            "group_event_av",
            AudioFusionCategory.AUDIO_EVENT,
            AudioFusionAuthority.AV_GROUNDING,
            "impact offscreen",
            AudioFusionSupport.SUPPORTED,
            Decimal("0.61"),
            _source("c", "source_event", "event_01"),
            _candidate_receipt("f"),
            (conflict,),
        ),
        AudioFusionCandidate(
            "candidate_reference",
            "group_reference",
            AudioFusionCategory.REFERENCE_SEMANTIC,
            AudioFusionAuthority.REFERENCE_SEMANTIC,
            "retain ambient source semantics",
            AudioFusionSupport.SUPPORTED,
            Decimal("0.87"),
            _source("d", "source_reference", "reference_01"),
            _candidate_receipt("g"),
        ),
        AudioFusionCandidate(
            "candidate_unsupported",
            "group_unsupported",
            AudioFusionCategory.AV_ALIGNMENT,
            AudioFusionAuthority.AV_GROUNDING,
            None,
            AudioFusionSupport.UNSUPPORTED,
            None,
            _source("e", "source_unknown", "unknown_01"),
            _candidate_receipt("h"),
            (unsupported,),
            high_impact=True,
        ),
    )
    plan = build_default_audio_fusion_benchmark_plan()
    profile = build_default_audio_fusion_calibration_profile(plan)
    return AudioFusionRequest(candidates, profile, plan, "audio_fusion_doc_fixture")


def _evaluation(request: AudioFusionRequest) -> AudioFusionEvaluation:
    plan = request.benchmark
    if plan is None:
        raise RuntimeError("fixture benchmark is required")
    samples = (
        # All three high-impact held-out errors are rejected by explicit abstention.
        # These are static values for an ablation, not a model-quality claim.
        (
            "audio.speaker_ambiguity",
            request.calibration_profile.calibrate(Decimal("0.20")),
            False,
            True,
            True,
        ),
        (
            "audio.unsupported_unknown",
            request.calibration_profile.calibrate(Decimal("0.10")),
            False,
            True,
            True,
        ),
        (
            "audio.high_impact_abstention",
            request.calibration_profile.calibrate(Decimal("0.15")),
            False,
            True,
            True,
        ),
        (
            "audio.held_out_calibration",
            request.calibration_profile.calibrate(Decimal("0.80")),
            True,
            False,
            False,
        ),
    )
    from comfyui_h3_context.core import AudioFusionEvaluationSample

    return evaluate_audio_fusion_benchmark(
        plan,
        request.calibration_profile,
        tuple(AudioFusionEvaluationSample(*item) for item in samples),
    )


def run() -> dict[str, object]:
    request = _request()
    document = execute_audio_evidence_fusion(
        _InjectedAudioFusionAdapter(),
        request,
        device=LocalDeviceSpec(LocalDeviceKind.CPU),
    )
    evaluation = _evaluation(request)
    terminal = build_audio_fusion_abstention(
        request, AudioFusionStatus.CANCELLED, "fixture_cancelled"
    )
    plan = request.benchmark
    if plan is None or document.receipt is None:
        raise RuntimeError("fixture outputs are incomplete")
    return {
        "schema": AUDIO_EVIDENCE_FUSION_SCHEMA,
        "status": document.status.value,
        "benchmark": {
            "case_count": len(plan.cases),
            "held_out_case_count": len(plan.held_out_case_ids),
            "fingerprint": plan.fingerprint,
        },
        "document": document.to_public_dict(),
        "evaluation": evaluation.to_wire(),
        "receipt": document.receipt.to_public_dict(),
        "resource": document.receipt.resource.to_wire(),
        "terminal_status": terminal.status.value,
        "preferred_route": plan.routing_policy.preferred_route.value,
        "first_fallback": plan.routing_policy.first_fallback.value,
        "automatic_fallback": plan.routing_policy.automatic_fallback,
        "native_route": "not_contacted",
        "ollama_route": "not_contacted",
        "network": "disabled",
        "host_runtime": "not_started",
        "decoder": "not_started",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="emit the redacted fixture summary as JSON"
    )
    args = parser.parse_args()
    result = run()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("M12-06 audio evidence fusion fixture: PASS")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
