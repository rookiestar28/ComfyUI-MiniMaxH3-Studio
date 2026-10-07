"""Offline M12-02 ASR, language, word-timing, and uncertainty fixture."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import cast

from comfyui_h3_context.core import (
    ASRAlternative,
    ASRDocument,
    ASRReceipt,
    ASRRequest,
    ASRRoute,
    ASRSegment,
    ASRStatus,
    ASRTextStatus,
    ASRUncertainty,
    ASRUncertaintyKind,
    ASRWord,
    AudioSourceSpan,
    PresentationTimestamp,
    build_asr_abstention,
    build_default_asr_benchmark_plan,
    execute_asr,
)

SOURCE_FINGERPRINT = "sha256:" + "a" * 64
PREPROCESSING_FINGERPRINT = "sha256:" + "b" * 64
MODEL_FINGERPRINT = "sha256:" + "c" * 64


def _span(start: int, end: int) -> AudioSourceSpan:
    return AudioSourceSpan(
        "video_audio_a",
        "source.video_audio",
        SOURCE_FINGERPRINT,
        PresentationTimestamp(start, 1, 1000),
        PresentationTimestamp(end, 1, 1000),
    )


def _word(word_id: str, start: int, end: int, text: str, confidence: str) -> ASRWord:
    return ASRWord(
        word_id,
        _span(start, end),
        text,
        Decimal(confidence),
    )


def _request() -> ASRRequest:
    return ASRRequest(
        "video_audio_a",
        "source.video_audio",
        SOURCE_FINGERPRINT,
        PREPROCESSING_FINGERPRINT,
        PresentationTimestamp(8000, 1, 1000),
        language_hints=("en-US", "zh-Hant"),
        route=ASRRoute.STATIC_INJECTED,
    )


def _segment_values() -> tuple[ASRSegment, ...]:
    return (
        ASRSegment(
            "asr.en.dialogue",
            _span(0, 2000),
            "Hello team",
            ASRTextStatus.TRANSCRIBED,
            "en-US",
            Decimal("0.96"),
            Decimal("0.94"),
            words=(
                _word("word.hello", 0, 900, "Hello", "0.98"),
                _word("word.team", 1000, 1900, "team", "0.91"),
            ),
            alternatives=(ASRAlternative("Hello teem", Decimal("0.18"), 1),),
        ),
        ASRSegment(
            "asr.zh.dialogue",
            _span(2000, 3000),
            "你好",
            ASRTextStatus.TRANSCRIBED,
            "zh-Hant",
            Decimal("0.88"),
            Decimal("0.84"),
            words=(
                ASRWord(
                    "word.nihao",
                    _span(2050, 2900),
                    "你好",
                    Decimal("0.84"),
                    alternatives=(ASRAlternative("您好", Decimal("0.22"), 1),),
                ),
            ),
            alternatives=(ASRAlternative("您好", Decimal("0.22"), 1),),
        ),
        ASRSegment(
            "asr.noisy.abstain",
            _span(3000, 3500),
            "[unclear]",
            ASRTextStatus.UNCLEAR,
            "en-US",
            Decimal("0.31"),
            Decimal("0.20"),
            alternatives=(ASRAlternative("turn left", Decimal("0.17"), 1),),
            uncertainties=(
                ASRUncertainty(ASRUncertaintyKind.NOISE, "background noise masks the speech"),
                ASRUncertainty(
                    ASRUncertaintyKind.LOW_CONFIDENCE, "confidence is below the decode threshold"
                ),
            ),
        ),
        ASRSegment(
            "asr.music.abstain",
            _span(3500, 5000),
            "[unclear]",
            ASRTextStatus.UNCLEAR,
            None,
            None,
            None,
            alternatives=(ASRAlternative("background dialogue", Decimal("0.12"), 1),),
            uncertainties=(
                ASRUncertainty(ASRUncertaintyKind.MUSIC_MASKING, "music masks the voice"),
            ),
        ),
        ASRSegment(
            "asr.overlap.a",
            _span(5000, 6000),
            "Stay close",
            ASRTextStatus.TRANSCRIBED,
            "en-US",
            Decimal("0.74"),
            Decimal("0.71"),
            words=(
                _word("word.stay", 5000, 5450, "Stay", "0.78"),
                _word("word.close", 5500, 5950, "close", "0.69"),
            ),
            uncertainties=(ASRUncertainty(ASRUncertaintyKind.OVERLAP, "another speaker overlaps"),),
            overlap_group="overlap.1",
        ),
        ASRSegment(
            "asr.overlap.b",
            _span(5000, 6500),
            "[unclear]",
            ASRTextStatus.UNCLEAR,
            "en-US",
            Decimal("0.22"),
            Decimal("0.18"),
            alternatives=(ASRAlternative("wait there", Decimal("0.16"), 1),),
            uncertainties=(
                ASRUncertainty(ASRUncertaintyKind.OVERLAP, "concurrent speaker is unresolved"),
                ASRUncertainty(
                    ASRUncertaintyKind.LOW_CONFIDENCE, "overlap prevents reliable decoding"
                ),
            ),
            overlap_group="overlap.1",
        ),
    )


def _document(request: ASRRequest) -> ASRDocument:
    receipt = ASRReceipt(
        ASRRoute.STATIC_INJECTED,
        "fixture_asr",
        "1.0.0",
        "injected-asr-fixture",
        MODEL_FINGERPRINT,
        SOURCE_FINGERPRINT,
        PREPROCESSING_FINGERPRINT,
    )
    return ASRDocument(
        "asr.document.fixture",
        ASRStatus.COMPLETE,
        request,
        _segment_values(),
        receipt,
        diagnostics=("observed_asr_not_exact_dialogue",),
    )


def _assert_redacted(value: object) -> None:
    if isinstance(value, str):
        lowered = value.casefold()
        forbidden = (
            "http://",
            "https://",
            "file://",
            "token=",
            "authorization",
            "password",
        )
        if any(marker in lowered for marker in forbidden):
            raise AssertionError("ASR fixture projection contains sensitive locator material")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_redacted(key)
            _assert_redacted(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_redacted(item)


def run() -> dict[str, object]:
    plan = build_default_asr_benchmark_plan()
    request = _request()
    document = execute_asr(lambda current: _document(current), request)
    empty = build_asr_abstention(request, ASRStatus.EMPTY, "no_speech")
    cancelled = build_asr_abstention(request, ASRStatus.CANCELLED, "cancelled")
    corrupt = build_asr_abstention(request, ASRStatus.CORRUPT, "corrupt_source")
    summary: dict[str, object] = {
        "status": cast(ASRStatus, document.status).value,
        "document_fingerprint": document.fingerprint,
        "benchmark": plan.to_public_summary(),
        "segment_count": len(document.segments),
        "word_count": sum(len(item.words) for item in document.segments),
        "unclear_segment_count": sum(item.text == "[unclear]" for item in document.segments),
        "overlap_segment_count": sum(
            item.overlap_group == "overlap.1" for item in document.segments
        ),
        "language_count": len(
            {item.language for item in document.segments if item.language is not None}
        ),
        "authority_policy": document.to_wire()["authority_policy"],
        "empty_status": cast(ASRStatus, empty.status).value,
        "cancelled": cast(ASRStatus, cancelled.status).value,
        "corrupt": cast(ASRStatus, corrupt.status).value,
        "native_route": "not_contacted",
        "ollama_route": "not_contacted",
        "specialist_route": "not_contacted",
        "network": "disabled",
        "decoder": "not_started",
        "host": "not_started",
        "optional_dependencies_absent": True,
    }
    _assert_redacted(summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the redacted fixture summary")
    args = parser.parse_args()
    result = run()
    if args.json:
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    else:
        print(
            f"M12-02 ASR fixture: {result['status']} "
            f"({result['segment_count']} segments; no live execution)"
        )


if __name__ == "__main__":
    main()
