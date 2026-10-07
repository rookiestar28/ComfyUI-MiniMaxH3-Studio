"""Offline M12-05 audiovisual synchronization and source-grounding fixture."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import cast

from comfyui_h3_context.core import (
    AVSyncDocument,
    AVSyncMediaKind,
    AVSyncOffsetInterval,
    AVSyncReceipt,
    AVSyncRelation,
    AVSyncRelationKind,
    AVSyncRequest,
    AVSyncRoute,
    AVSyncSourceRole,
    AVSyncSourceSpan,
    AVSyncStatus,
    AVSyncSupport,
    AVSyncUncertainty,
    AVSyncUncertaintyKind,
    AVSyncVisibility,
    PresentationTimestamp,
    build_av_sync_abstention,
    build_default_av_sync_benchmark_plan,
    execute_av_sync,
)

VIDEO_FINGERPRINT = "sha256:" + "a" * 64
AUDIO_FINGERPRINT = "sha256:" + "b" * 64
PREPROCESSING_FINGERPRINT = "sha256:" + "c" * 64
MODEL_FINGERPRINT = "sha256:" + "d" * 64


def _video(
    start: int,
    end: int,
    role: AVSyncSourceRole,
    *,
    frame_ids: tuple[str, ...] = (),
    shot_id: str | None = None,
) -> AVSyncSourceSpan:
    return AVSyncSourceSpan(
        "av_sync.video",
        "source.av_sync.video",
        VIDEO_FINGERPRINT,
        AVSyncMediaKind.VIDEO,
        role,
        PresentationTimestamp(start, 1, 1000),
        PresentationTimestamp(end, 1, 1000),
        frame_ids,
        shot_id,
    )


def _audio(start: int, end: int, role: AVSyncSourceRole) -> AVSyncSourceSpan:
    return AVSyncSourceSpan(
        "av_sync.audio",
        "source.av_sync.audio",
        AUDIO_FINGERPRINT,
        AVSyncMediaKind.AUDIO,
        role,
        PresentationTimestamp(start, 1, 1000),
        PresentationTimestamp(end, 1, 1000),
    )


def _request() -> AVSyncRequest:
    return AVSyncRequest(
        "av_sync.video",
        "source.av_sync.video",
        VIDEO_FINGERPRINT,
        PresentationTimestamp(10_000, 1, 1000),
        "av_sync.audio",
        "source.av_sync.audio",
        AUDIO_FINGERPRINT,
        PresentationTimestamp(10_000, 1, 1000),
        PREPROCESSING_FINGERPRINT,
        route=AVSyncRoute.STATIC_INJECTED,
    )


def _uncertainty(kind: AVSyncUncertaintyKind, detail: str) -> AVSyncUncertainty:
    return AVSyncUncertainty(kind, detail)


def _relations() -> tuple[AVSyncRelation, ...]:
    overlap_group = "av.overlap.1"
    return (
        AVSyncRelation(
            "relation.speech.lip",
            AVSyncRelationKind.SPEECH_LIP,
            AVSyncSupport.ALIGNED,
            _video(500, 1500, AVSyncSourceRole.LIP, frame_ids=("frame.lip.1",), shot_id="shot.1"),
            _audio(500, 1500, AVSyncSourceRole.SPEECH),
            AVSyncOffsetInterval(-20, 20),
            Decimal("0.96"),
            Decimal("0.91"),
            AVSyncVisibility.ON_SCREEN,
            (_uncertainty(AVSyncUncertaintyKind.CALIBRATION, "injected alignment tolerance"),),
            label="speech_lip",
        ),
        AVSyncRelation(
            "relation.action.sound",
            AVSyncRelationKind.ACTION_SOUND,
            AVSyncSupport.ALIGNED,
            _video(2000, 2500, AVSyncSourceRole.VISIBLE_ACTION, shot_id="shot.1"),
            _audio(1950, 2500, AVSyncSourceRole.SOUND_EVENT),
            AVSyncOffsetInterval(10, 60),
            Decimal("0.86"),
            Decimal("0.78"),
            AVSyncVisibility.ON_SCREEN,
            (_uncertainty(AVSyncUncertaintyKind.BOUNDARY_UNCERTAIN, "effect onset is bounded"),),
            label="visible_action_sound",
        ),
        AVSyncRelation(
            "relation.music.edit",
            AVSyncRelationKind.MUSIC_EDIT,
            AVSyncSupport.UNCERTAIN,
            _video(3200, 5000, AVSyncSourceRole.EDIT, shot_id="shot.2"),
            _audio(3000, 5000, AVSyncSourceRole.MUSIC),
            AVSyncOffsetInterval(150, 260),
            Decimal("0.64"),
            Decimal("0.42"),
            AVSyncVisibility.UNKNOWN,
            (_uncertainty(AVSyncUncertaintyKind.CUT, "edit boundary is an alignment anchor"),),
            label="music_edit",
        ),
        AVSyncRelation(
            "relation.video.soundtrack",
            AVSyncRelationKind.VIDEO_SOUNDTRACK,
            AVSyncSupport.ALIGNED,
            _video(5000, 6500, AVSyncSourceRole.VIDEO, shot_id="shot.3"),
            _audio(5000, 6500, AVSyncSourceRole.SOUNDTRACK),
            AVSyncOffsetInterval(-50, 50),
            Decimal("0.89"),
            Decimal("0.83"),
            AVSyncVisibility.UNKNOWN,
        ),
        AVSyncRelation(
            "relation.source.offscreen",
            AVSyncRelationKind.SOURCE_VISIBILITY,
            AVSyncSupport.UNCERTAIN,
            None,
            _audio(6500, 7100, AVSyncSourceRole.SOUND_EVENT),
            None,
            Decimal("0.68"),
            Decimal("0.38"),
            AVSyncVisibility.OFF_SCREEN,
            (
                _uncertainty(AVSyncUncertaintyKind.OFF_SCREEN, "no visible source in the interval"),
                _uncertainty(
                    AVSyncUncertaintyKind.NO_VISIBLE_SOURCE, "source ownership remains audio-local"
                ),
            ),
            label="offscreen_sound",
        ),
        AVSyncRelation(
            "relation.dubbing.voice",
            AVSyncRelationKind.DUBBING,
            AVSyncSupport.DESYNCHRONIZED,
            _video(7500, 8200, AVSyncSourceRole.LIP, shot_id="shot.4"),
            _audio(7800, 8500, AVSyncSourceRole.DUBBED_VOICE),
            AVSyncOffsetInterval(-360, -280),
            Decimal("0.81"),
            Decimal("0.57"),
            AVSyncVisibility.ON_SCREEN,
            (
                _uncertainty(
                    AVSyncUncertaintyKind.DUBBING, "voice source is not the captured speech"
                ),
            ),
            label="dubbed_voice",
        ),
        AVSyncRelation(
            "relation.overlap.action",
            AVSyncRelationKind.ACTION_SOUND,
            AVSyncSupport.ALIGNED,
            _video(8500, 9000, AVSyncSourceRole.VISIBLE_ACTION, shot_id="shot.5"),
            _audio(8500, 9000, AVSyncSourceRole.SOUND_EVENT),
            AVSyncOffsetInterval(-10, 40),
            Decimal("0.77"),
            Decimal("0.73"),
            AVSyncVisibility.ON_SCREEN,
            (_uncertainty(AVSyncUncertaintyKind.OVERLAP, "music and effect overlap"),),
            overlap_group,
            "overlap_action",
        ),
        AVSyncRelation(
            "relation.overlap.music",
            AVSyncRelationKind.MUSIC_EDIT,
            AVSyncSupport.UNCERTAIN,
            _video(8550, 9050, AVSyncSourceRole.EDIT, shot_id="shot.5"),
            _audio(8550, 9050, AVSyncSourceRole.MUSIC),
            AVSyncOffsetInterval(-120, 80),
            Decimal("0.58"),
            Decimal("0.45"),
            AVSyncVisibility.UNKNOWN,
            (_uncertainty(AVSyncUncertaintyKind.OVERLAP, "multilabel sound/visual overlap"),),
            overlap_group,
            "overlap_music",
        ),
    )


def _document(
    request: AVSyncRequest,
    *,
    visual_override: AVSyncSourceSpan | None = None,
    invalid_unknown: bool = False,
    invalid_aligned: bool = False,
) -> AVSyncDocument:
    relations = _relations()
    if visual_override is not None:
        relations = (replace(relations[0], visual_span=visual_override), *relations[1:])
    if invalid_unknown:
        relations = (
            *relations,
            AVSyncRelation(
                "relation.invalid.unknown",
                AVSyncRelationKind.SOURCE_VISIBILITY,
                AVSyncSupport.UNKNOWN,
                None,
                _audio(9100, 9300, AVSyncSourceRole.SOUND_EVENT),
                AVSyncOffsetInterval(0, 0),
                None,
                None,
                AVSyncVisibility.UNKNOWN,
                (_uncertainty(AVSyncUncertaintyKind.UNKNOWN, "not observed"),),
            ),
        )
    if invalid_aligned:
        relations = (
            *relations,
            AVSyncRelation(
                "relation.invalid.aligned",
                AVSyncRelationKind.SPEECH_LIP,
                AVSyncSupport.ALIGNED,
                _video(9100, 9300, AVSyncSourceRole.LIP),
                _audio(9100, 9300, AVSyncSourceRole.SPEECH),
                None,
                Decimal("0.4"),
                Decimal("0.4"),
                AVSyncVisibility.ON_SCREEN,
            ),
        )
    receipt = AVSyncReceipt(
        AVSyncRoute.STATIC_INJECTED,
        "fixture_av_sync",
        "1.0.0",
        "injected-av-sync-fixture",
        MODEL_FINGERPRINT,
        VIDEO_FINGERPRINT,
        AUDIO_FINGERPRINT,
        PREPROCESSING_FINGERPRINT,
    )
    return AVSyncDocument(
        "av_sync.document.fixture",
        AVSyncStatus.COMPLETE,
        request,
        relations,
        receipt,
        diagnostics=(
            "source_pts_are_preserved",
            "offscreen_and_dubbing_are_not_alignment_success",
            "overlap_group_is_explicit",
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
            "raw_video",
            "provider_payload",
        )
        if any(marker in lowered for marker in forbidden):
            raise AssertionError("audiovisual fixture projection contains sensitive material")
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
    plan = build_default_av_sync_benchmark_plan()
    request = _request()
    document = execute_av_sync(lambda current: _document(current), request)
    empty = build_av_sync_abstention(request, AVSyncStatus.EMPTY, "no_alignment_claim")
    cancelled = build_av_sync_abstention(request, AVSyncStatus.CANCELLED, "cancelled")
    corrupt = build_av_sync_abstention(request, AVSyncStatus.CORRUPT, "corrupt_source")
    summary: dict[str, object] = {
        "status": cast(AVSyncStatus, document.status).value,
        "document_fingerprint": document.fingerprint,
        "benchmark": plan.to_public_summary(),
        "relation_count": len(document.relations),
        "aligned_count": sum(item.support is AVSyncSupport.ALIGNED for item in document.relations),
        "desynchronized_count": sum(
            item.support is AVSyncSupport.DESYNCHRONIZED for item in document.relations
        ),
        "offscreen_count": sum(
            item.visibility is AVSyncVisibility.OFF_SCREEN for item in document.relations
        ),
        "overlap_group_count": len(
            {item.overlap_group for item in document.relations if item.overlap_group}
        ),
        "terminal_statuses": [
            cast(AVSyncStatus, item.status).value for item in (empty, cancelled, corrupt)
        ],
        "ollama_route": "not_contacted",
        "native_route": "not_contacted",
        "media_decoder": "not_started",
    }
    _assert_redacted(summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the redacted summary as JSON")
    args = parser.parse_args()
    result = run()
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2) if args.json else result)


if __name__ == "__main__":
    main()
