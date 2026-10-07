"""Offline M12-03 speaker diarization and voice-reference fixture."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import cast

from comfyui_h3_context.core import (
    AudioSourceSpan,
    PresentationTimestamp,
    SpeakerAssociation,
    SpeakerAssociationEvidence,
    SpeakerDocument,
    SpeakerHypothesis,
    SpeakerReceipt,
    SpeakerReference,
    SpeakerRequest,
    SpeakerRetention,
    SpeakerRoute,
    SpeakerStatus,
    SpeakerTurn,
    SpeakerUncertainty,
    SpeakerUncertaintyKind,
    build_default_speaker_benchmark_plan,
    build_speaker_abstention,
    execute_speaker,
)

SOURCE_FINGERPRINT = "sha256:" + "a" * 64
PREPROCESSING_FINGERPRINT = "sha256:" + "b" * 64
MODEL_FINGERPRINT = "sha256:" + "c" * 64
REFERENCE_FINGERPRINT = "sha256:" + "d" * 64
EMBEDDING_FINGERPRINT = "sha256:" + "e" * 64
VIDEO_FINGERPRINT = "sha256:" + "f" * 64
EVIDENCE_FINGERPRINT = "sha256:" + "1" * 64


def _span(start: int, end: int) -> AudioSourceSpan:
    return AudioSourceSpan(
        "audio_pair_a",
        "source.audio_pair",
        SOURCE_FINGERPRINT,
        PresentationTimestamp(start, 1, 1000),
        PresentationTimestamp(end, 1, 1000),
    )


def _request() -> SpeakerRequest:
    return SpeakerRequest(
        "audio_pair_a",
        "source.audio_pair",
        SOURCE_FINGERPRINT,
        PREPROCESSING_FINGERPRINT,
        PresentationTimestamp(8000, 1, 1000),
        references=(SpeakerReference("voice_ref_a", "source.voice_ref_a", REFERENCE_FINGERPRINT),),
        embedding_requested=True,
        biometric_opt_in=True,
        retention=SpeakerRetention.EPHEMERAL,
        user_selected_speaker_ids=("speaker_a", "speaker_b"),
        route=SpeakerRoute.STATIC_INJECTED,
    )


def _turns() -> tuple[SpeakerTurn, ...]:
    return (
        SpeakerTurn(
            "turn.a.1",
            _span(0, 2000),
            "speaker_a",
            Decimal("0.92"),
            embedding_fingerprint=EMBEDDING_FINGERPRINT,
            embedding_retention=SpeakerRetention.EPHEMERAL,
        ),
        SpeakerTurn("turn.b.1", _span(2000, 3500), "speaker_b", Decimal("0.88")),
        SpeakerTurn(
            "turn.unknown",
            _span(3500, 4000),
            None,
            Decimal("0.18"),
            uncertainties=(
                SpeakerUncertainty(
                    SpeakerUncertaintyKind.UNKNOWN_SPEAKER,
                    "voice cannot be linked to a stable local label",
                ),
            ),
        ),
        SpeakerTurn(
            "turn.a.overlap",
            _span(5000, 6500),
            "speaker_a",
            Decimal("0.68"),
            uncertainties=(
                SpeakerUncertainty(SpeakerUncertaintyKind.OVERLAP, "concurrent speech"),
            ),
            overlap_group="overlap.1",
        ),
        SpeakerTurn(
            "turn.b.overlap",
            _span(5000, 6500),
            "speaker_b",
            Decimal("0.63"),
            uncertainties=(
                SpeakerUncertainty(SpeakerUncertaintyKind.OVERLAP, "concurrent speech"),
            ),
            overlap_group="overlap.1",
        ),
    )


def _hypotheses() -> tuple[SpeakerHypothesis, ...]:
    reference = SpeakerReference("voice_ref_a", "source.voice_ref_a", REFERENCE_FINGERPRINT)
    return (
        SpeakerHypothesis(
            "hypothesis.a.reference",
            "speaker_a",
            reference,
            Decimal("0.61"),
            3200,
            True,
            False,
            uncertainties=(
                SpeakerUncertainty(
                    SpeakerUncertaintyKind.AMBIGUOUS, "cross-asset voice match is unresolved"
                ),
                SpeakerUncertainty(
                    SpeakerUncertaintyKind.FALSE_LINK_RISK, "similarity is not identity proof"
                ),
            ),
        ),
        SpeakerHypothesis(
            "hypothesis.b.reference",
            "speaker_b",
            reference,
            Decimal("0.43"),
            4500,
            True,
            False,
            uncertainties=(
                SpeakerUncertainty(
                    SpeakerUncertaintyKind.AMBIGUOUS, "voice evidence is insufficient"
                ),
            ),
        ),
    )


def _associations() -> tuple[SpeakerAssociation, ...]:
    return (
        SpeakerAssociation(
            "association.a.av",
            "speaker_a",
            "video_a",
            SpeakerAssociationEvidence.AV_EVIDENCE,
            Decimal("0.79"),
            video_source_fingerprint=VIDEO_FINGERPRINT,
            evidence_fingerprint=EVIDENCE_FINGERPRINT,
            video_source_id="source.video_a",
        ),
        SpeakerAssociation(
            "association.b.user",
            "speaker_b",
            "video_a",
            SpeakerAssociationEvidence.USER_SELECTION,
            Decimal("1.0"),
            user_selected=True,
        ),
    )


def _document(request: SpeakerRequest, *, embedding_logged: bool = False) -> SpeakerDocument:
    receipt = SpeakerReceipt(
        SpeakerRoute.STATIC_INJECTED,
        "fixture_speaker",
        "1.0.0",
        "injected-speaker-fixture",
        MODEL_FINGERPRINT,
        SOURCE_FINGERPRINT,
        PREPROCESSING_FINGERPRINT,
        embedding_generated=True,
        embedding_logged=embedding_logged,
    )
    return SpeakerDocument(
        "speaker.document.fixture",
        SpeakerStatus.COMPLETE,
        request,
        _turns(),
        _hypotheses(),
        _associations(),
        receipt,
        diagnostics=("speaker_labels_are_source_local_hypotheses",),
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
            "raw_embedding",
            "voiceprint",
        )
        if any(marker in lowered for marker in forbidden):
            raise AssertionError("speaker fixture projection contains sensitive material")
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
    plan = build_default_speaker_benchmark_plan()
    request = _request()
    document = execute_speaker(lambda current: _document(current), request)
    empty = build_speaker_abstention(request, SpeakerStatus.EMPTY, "no_speech")
    cancelled = build_speaker_abstention(request, SpeakerStatus.CANCELLED, "cancelled")
    corrupt = build_speaker_abstention(request, SpeakerStatus.CORRUPT, "corrupt_source")
    summary: dict[str, object] = {
        "status": cast(SpeakerStatus, document.status).value,
        "document_fingerprint": document.fingerprint,
        "benchmark": plan.to_public_summary(),
        "turn_count": len(document.turns),
        "speaker_label_count": len(
            {item.speaker_label for item in document.turns if item.speaker_label}
        ),
        "overlap_turn_count": sum(item.overlap_group == "overlap.1" for item in document.turns),
        "hypothesis_count": len(document.hypotheses),
        "association_count": len(document.associations),
        "ambiguous_hypothesis_count": sum(item.ambiguous for item in document.hypotheses),
        "embedding_generated": document.receipt.embedding_generated if document.receipt else False,
        "embedding_logged": document.receipt.embedding_logged if document.receipt else False,
        "privacy_policy": document.to_wire()["privacy_policy"],
        "empty_status": cast(SpeakerStatus, empty.status).value,
        "cancelled": cast(SpeakerStatus, cancelled.status).value,
        "corrupt": cast(SpeakerStatus, corrupt.status).value,
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
            f"M12-03 speaker fixture: {result['status']} "
            f"({result['turn_count']} turns; no live execution)"
        )


if __name__ == "__main__":
    main()
