"""Offline M12-04 audio-event and reference-semantics fixture."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import cast

from comfyui_h3_context.core import (
    AudioEvent,
    AudioEventDocument,
    AudioEventKind,
    AudioEventPresence,
    AudioEventReceipt,
    AudioEventReference,
    AudioEventReferenceSemantic,
    AudioEventRequest,
    AudioEventRoute,
    AudioEventStatus,
    AudioEventUncertainty,
    AudioEventUncertaintyKind,
    AudioSourceSpan,
    PresentationTimestamp,
    build_audio_event_abstention,
    build_default_audio_event_benchmark_plan,
    execute_audio_event,
)

SOURCE_FINGERPRINT = "sha256:" + "a" * 64
PREPROCESSING_FINGERPRINT = "sha256:" + "b" * 64
MODEL_FINGERPRINT = "sha256:" + "c" * 64
COPY_REFERENCE_FINGERPRINT = "sha256:" + "d" * 64
REPERFORM_REFERENCE_FINGERPRINT = "sha256:" + "e" * 64
TIMBRE_REFERENCE_FINGERPRINT = "sha256:" + "f" * 64
AMBIENT_REFERENCE_FINGERPRINT = "sha256:" + "1" * 64


def _span(start: int, end: int) -> AudioSourceSpan:
    return AudioSourceSpan(
        "audio_events_a",
        "source.audio_events",
        SOURCE_FINGERPRINT,
        PresentationTimestamp(start, 1, 1000),
        PresentationTimestamp(end, 1, 1000),
    )


def _request() -> AudioEventRequest:
    return AudioEventRequest(
        "audio_events_a",
        "source.audio_events",
        SOURCE_FINGERPRINT,
        PREPROCESSING_FINGERPRINT,
        PresentationTimestamp(8000, 1, 1000),
        references=(
            AudioEventReference(
                "event.copy.ref",
                "source.event.copy.ref",
                COPY_REFERENCE_FINGERPRINT,
                AudioEventReferenceSemantic.COPIED_AUDIO,
            ),
            AudioEventReference(
                "event.reperform.ref",
                "source.event.reperform.ref",
                REPERFORM_REFERENCE_FINGERPRINT,
                AudioEventReferenceSemantic.REPERFORMED_AUDIO,
            ),
            AudioEventReference(
                "event.timbre.ref",
                "source.event.timbre.ref",
                TIMBRE_REFERENCE_FINGERPRINT,
                AudioEventReferenceSemantic.TIMBRE_REFERENCE,
            ),
            AudioEventReference(
                "event.ambient.ref",
                "source.event.ambient.ref",
                AMBIENT_REFERENCE_FINGERPRINT,
                AudioEventReferenceSemantic.AMBIENT_REFERENCE,
            ),
        ),
        route=AudioEventRoute.STATIC_INJECTED,
    )


def _events() -> tuple[AudioEvent, ...]:
    return (
        AudioEvent(
            "event.speech.1",
            _span(0, 2000),
            AudioEventKind.SPEECH,
            AudioEventPresence.PRESENT,
            "speech",
            Decimal("0.94"),
            Decimal("0.90"),
            overlap_group="event.overlap.1",
            uncertainties=(
                AudioEventUncertainty(AudioEventUncertaintyKind.OVERLAP, "music overlaps speech"),
            ),
        ),
        AudioEvent(
            "event.music.1",
            _span(1000, 3000),
            AudioEventKind.MUSIC,
            AudioEventPresence.PRESENT,
            "music",
            Decimal("0.88"),
            Decimal("0.84"),
            overlap_group="event.overlap.1",
            reference_semantic=AudioEventReferenceSemantic.COPIED_AUDIO,
            reference_asset_id="event.copy.ref",
            uncertainties=(
                AudioEventUncertainty(AudioEventUncertaintyKind.OVERLAP, "speech overlaps music"),
            ),
        ),
        AudioEvent(
            "event.ambience.1",
            _span(3000, 4000),
            AudioEventKind.AMBIENCE,
            AudioEventPresence.PRESENT,
            "room_tone",
            Decimal("0.82"),
            Decimal("0.80"),
            reference_semantic=AudioEventReferenceSemantic.REPERFORMED_AUDIO,
            reference_asset_id="event.reperform.ref",
        ),
        AudioEvent(
            "event.effect.1",
            _span(4000, 4500),
            AudioEventKind.PHYSICAL_EFFECT,
            AudioEventPresence.PRESENT,
            "door_click",
            Decimal("0.87"),
            Decimal("0.86"),
            reference_semantic=AudioEventReferenceSemantic.TIMBRE_REFERENCE,
            reference_asset_id="event.timbre.ref",
        ),
        AudioEvent(
            "event.rhythm.1",
            _span(4500, 6000),
            AudioEventKind.RHYTHM,
            AudioEventPresence.PRESENT,
            "steady_pulse",
            Decimal("0.76"),
            Decimal("0.72"),
            overlap_group="event.rhythm.1",
            reference_semantic=AudioEventReferenceSemantic.AMBIENT_REFERENCE,
            reference_asset_id="event.ambient.ref",
        ),
        AudioEvent(
            "event.tempo.1",
            _span(4500, 6000),
            AudioEventKind.TEMPO,
            AudioEventPresence.PRESENT,
            "120_bpm",
            Decimal("0.74"),
            Decimal("0.70"),
            overlap_group="event.rhythm.1",
        ),
        AudioEvent(
            "event.unknown.1",
            _span(6000, 7000),
            AudioEventKind.UNKNOWN,
            AudioEventPresence.UNKNOWN,
            None,
            None,
            None,
            uncertainties=(
                AudioEventUncertainty(
                    AudioEventUncertaintyKind.UNKNOWN, "masked event cannot be resolved"
                ),
            ),
        ),
        AudioEvent(
            "event.ambience.absent",
            _span(7000, 8000),
            AudioEventKind.AMBIENCE,
            AudioEventPresence.ABSENT,
            None,
            None,
            None,
            uncertainties=(
                AudioEventUncertainty(
                    AudioEventUncertaintyKind.ABSENT, "no ambience claim in the interval"
                ),
            ),
        ),
    )


def _document(
    request: AudioEventRequest, *, events: tuple[AudioEvent, ...] | None = None
) -> AudioEventDocument:
    receipt = AudioEventReceipt(
        AudioEventRoute.STATIC_INJECTED,
        "fixture_audio_events",
        "1.0.0",
        "injected-audio-event-fixture",
        MODEL_FINGERPRINT,
        SOURCE_FINGERPRINT,
        PREPROCESSING_FINGERPRINT,
    )
    return AudioEventDocument(
        "audio_event.document.fixture",
        AudioEventStatus.COMPLETE,
        request,
        _events() if events is None else events,
        receipt,
        diagnostics=(
            "event_labels_are_source_local_descriptors",
            "reference_semantics_are_explicit_and_non_interchangeable",
        ),
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
            "raw_audio",
            "provider_payload",
        )
        if any(marker in lowered for marker in forbidden):
            raise AssertionError("audio-event fixture projection contains sensitive material")
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
    plan = build_default_audio_event_benchmark_plan()
    request = _request()
    document = execute_audio_event(lambda current: _document(current), request)
    empty = build_audio_event_abstention(request, AudioEventStatus.EMPTY, "no_event_claim")
    cancelled = build_audio_event_abstention(request, AudioEventStatus.CANCELLED, "cancelled")
    corrupt = build_audio_event_abstention(request, AudioEventStatus.CORRUPT, "corrupt_source")
    events = document.events
    summary: dict[str, object] = {
        "status": cast(AudioEventStatus, document.status).value,
        "document_fingerprint": document.fingerprint,
        "benchmark": plan.to_public_summary(),
        "event_count": len(events),
        "present_event_count": sum(item.presence is AudioEventPresence.PRESENT for item in events),
        "unknown_event_count": sum(item.presence is AudioEventPresence.UNKNOWN for item in events),
        "absent_event_count": sum(item.presence is AudioEventPresence.ABSENT for item in events),
        "overlap_group_count": len({item.overlap_group for item in events if item.overlap_group}),
        "reference_semantic_count": sum(item.reference_semantic is not None for item in events),
        "empty_status": cast(AudioEventStatus, empty.status).value,
        "cancelled": cast(AudioEventStatus, cancelled.status).value,
        "corrupt": cast(AudioEventStatus, corrupt.status).value,
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
    if args.json:
        print(json.dumps(run(), ensure_ascii=True, sort_keys=True, indent=2))
    else:
        print("audio-event fixture ready")


if __name__ == "__main__":
    main()
