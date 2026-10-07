"""Offline M12-01 audio extraction, normalization, and segmentation fixture."""

from __future__ import annotations

import argparse
import json

from comfyui_h3_context.core import (
    AudioGap,
    AudioGapKind,
    AudioPreprocessDocument,
    AudioPreprocessReceipt,
    AudioPreprocessRequest,
    AudioPreprocessStatus,
    AudioRoute,
    AudioSegment,
    AudioSourceSpan,
    PresentationTimestamp,
    build_audio_preprocess_abstention,
    build_default_audio_benchmark_plan,
)


def _fp(letter: str) -> str:
    return "sha256:" + letter * 64


def _pts(ticks: int) -> PresentationTimestamp:
    return PresentationTimestamp(ticks, 1, 1000)


def _request() -> AudioPreprocessRequest:
    return AudioPreprocessRequest(
        asset_id="video_audio_a",
        source_id="source.video_audio",
        source_fingerprint=_fp("a"),
        duration_end=_pts(8000),
        sample_rate=48_000,
        channels=2,
        channel_layout="stereo",
        paired_video_asset_id="video_a",
    )


def _span(request: AudioPreprocessRequest, start: int, end: int) -> AudioSourceSpan:
    return AudioSourceSpan(
        request.asset_id,
        request.source_id,
        request.source_fingerprint,
        _pts(start),
        _pts(end),
    )


def _document(request: AudioPreprocessRequest) -> AudioPreprocessDocument:
    segments = (
        AudioSegment(
            "segment.speech",
            _span(request, 0, 2000),
            request.sample_rate,
            request.channels,
            request.channel_layout,
            _fp("b"),
            _fp("c"),
        ),
        AudioSegment(
            "segment.noise",
            _span(request, 2000, 3000),
            request.sample_rate,
            request.channels,
            request.channel_layout,
            _fp("d"),
            _fp("e"),
        ),
        AudioSegment(
            "segment.music",
            _span(request, 3500, 5000),
            request.sample_rate,
            request.channels,
            request.channel_layout,
            _fp("f"),
            _fp("a"),
        ),
        AudioSegment(
            "segment.overlap.dialogue",
            _span(request, 5000, 6500),
            request.sample_rate,
            request.channels,
            request.channel_layout,
            _fp("8"),
            _fp("9"),
            overlap_group="overlap.1",
        ),
        AudioSegment(
            "segment.overlap.sfx",
            _span(request, 5000, 6500),
            request.sample_rate,
            request.channels,
            request.channel_layout,
            _fp("1"),
            _fp("2"),
            overlap_group="overlap.1",
        ),
    )
    gaps = (AudioGap("gap.missing", _span(request, 3000, 3500), AudioGapKind.MISSING),)
    receipt = AudioPreprocessReceipt(
        AudioRoute.STATIC_INJECTED,
        "injected_audio_preprocess",
        "1.0.0",
        request.source_fingerprint,
        _fp("3"),
    )
    return AudioPreprocessDocument(
        "audio.document.fixture",
        AudioPreprocessStatus.COMPLETE,
        request,
        segments,
        gaps,
        receipt,
    )


def run() -> dict[str, object]:
    request = _request()
    document = _document(request)
    plan = build_default_audio_benchmark_plan()
    cancelled = build_audio_preprocess_abstention(
        request, AudioPreprocessStatus.CANCELLED, "fixture_cancelled"
    )
    corrupt = build_audio_preprocess_abstention(
        request, AudioPreprocessStatus.CORRUPT, "fixture_corrupt"
    )
    return {
        "schema": document.schema,
        "status": document.status.value,
        "document_fingerprint": document.fingerprint,
        "segment_count": len(document.segments),
        "gap_count": len(document.gaps),
        "overlap_segment_count": sum(
            segment.overlap_group is not None for segment in document.segments
        ),
        "paired_video_asset_id": request.paired_video_asset_id,
        "benchmark_plan_fingerprint": plan.fingerprint,
        "case_count": len(plan.fixtures),
        "capability_count": len(plan.thresholds),
        "candidate_dispositions": {
            candidate.candidate_id: candidate.disposition.value for candidate in plan.candidates
        },
        "native_route": "not_contacted",
        "ollama_route": "not_contacted",
        "network": "disabled",
        "decoder": "not_started",
        "host_runtime": "not_started",
        "cancelled": cancelled.status.value,
        "corrupt": corrupt.status.value,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the redacted fixture summary")
    args = parser.parse_args()
    result = run()
    if args.json:
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    else:
        print(f"M12-01 audio extraction fixture: {result['status']}")


if __name__ == "__main__":
    main()
