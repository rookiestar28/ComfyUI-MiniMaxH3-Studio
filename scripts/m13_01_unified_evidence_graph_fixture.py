"""Offline M13-01 unified multimodal evidence graph fixture."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from decimal import Decimal

from comfyui_h3_context.core import (
    EvidenceLevel,
    EvidenceOrigin,
    EvidenceRecord,
    EvidenceSource,
    EvidenceSourceKind,
    GraphAlternative,
    GraphConflict,
    GraphEntity,
    GraphEntityKind,
    GraphEvent,
    GraphEventKind,
    GraphNodeKind,
    GraphObservation,
    GraphObservationKind,
    GraphResolution,
    GraphSourceSpan,
    GraphSupport,
    GraphTrack,
    GraphUncertainty,
    MediaKind,
    Provenance,
    ProviderIdentity,
    SupportStatus,
    UncertaintyKind,
    UnifiedEvidenceGraph,
    ValidationSeverity,
    build_unified_evidence_graph,
)

FP_VISUAL = "sha256:" + "a" * 64
FP_AUDIO = "sha256:" + "b" * 64
FP_TEXT = "sha256:" + "c" * 64


def _record(evidence_id: str, asset_id: str, source_id: str) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id,
        "bounded fixture observation",
        EvidenceOrigin.OBSERVED,
        SupportStatus.SUPPORTED,
        Provenance(
            EvidenceSource(EvidenceSourceKind.MEDIA_ASSET, source_id, asset_id=asset_id),
            ProviderIdentity.LOCAL,
            EvidenceLevel.EXPERIMENTAL,
            provider_version="1.0.0",
            source_revision="fixture.rev",
        ),
        Decimal("0.90"),
    )


def _span(
    asset_id: str, source_id: str, fingerprint: str, start_ms: int, end_ms: int
) -> GraphSourceSpan:
    return GraphSourceSpan(asset_id, source_id, fingerprint, start_ms, end_ms)


def build_fixture_graph() -> UnifiedEvidenceGraph:
    evidence = (
        _record("ev.visual", "asset.visual", "source.visual"),
        _record("ev.audio", "asset.audio", "source.audio"),
        _record("ev.text", "asset.text", "source.text"),
    )
    uncertainties = (
        GraphUncertainty(
            "uncertain.identity",
            UncertaintyKind.AMBIGUOUS,
            "two explicit identity candidates remain",
        ),
        GraphUncertainty(
            "uncertain.audio",
            UncertaintyKind.MISSING_SOURCE,
            "audio grounding is unavailable for this fixture",
        ),
    )
    observations = (
        GraphObservation(
            "obs.visual.subject",
            "asset.visual",
            MediaKind.IMAGE,
            GraphObservationKind.SUBJECT,
            "person near a doorway",
            _span("asset.visual", "source.visual", FP_VISUAL, 0, 1000),
            evidence_ids=("ev.visual",),
            entity_ids=("entity.subject",),
            track_ids=("track.subject",),
            event_ids=("event.action",),
            confidence=Decimal("0.88"),
        ),
        GraphObservation(
            "obs.audio.speech",
            "asset.audio",
            MediaKind.AUDIO,
            GraphObservationKind.SPEECH,
            "observed speech segment",
            _span("asset.audio", "source.audio", FP_AUDIO, 0, 900),
            evidence_ids=("ev.audio",),
            entity_ids=("entity.speaker",),
            track_ids=("track.speaker",),
            event_ids=("event.speech",),
            confidence=Decimal("0.74"),
        ),
        GraphObservation(
            "obs.text.visible",
            "asset.text",
            MediaKind.IMAGE,
            GraphObservationKind.TEXT,
            "visible sign candidate",
            _span("asset.text", "source.text", FP_TEXT, 200, 800),
            evidence_ids=("ev.text",),
            event_ids=("event.text",),
            confidence=Decimal("0.81"),
        ),
        GraphObservation(
            "obs.audio.missing",
            "asset.audio",
            MediaKind.AUDIO,
            GraphObservationKind.AUDIO,
            "audio grounding unavailable",
            None,
            support=GraphSupport.MISSING,
            uncertainty_ids=("uncertain.audio",),
        ),
    )
    entities = (
        GraphEntity(
            "entity.subject",
            GraphEntityKind.SUBJECT,
            observation_ids=("obs.visual.subject",),
            track_ids=("track.subject",),
            evidence_ids=("ev.visual",),
            resolution=GraphResolution.AMBIGUOUS,
            candidate_ids=("subject.a", "subject.b"),
            label="subject hypothesis",
            confidence=Decimal("0.62"),
            uncertainty_ids=("uncertain.identity",),
        ),
        GraphEntity(
            "entity.speaker",
            GraphEntityKind.SPEAKER,
            observation_ids=("obs.audio.speech",),
            track_ids=("track.speaker",),
            evidence_ids=("ev.audio",),
            label="speaker hypothesis",
            confidence=Decimal("0.70"),
        ),
    )
    tracks = (
        GraphTrack(
            "track.subject",
            ("obs.visual.subject",),
            entity_id="entity.subject",
            spans=(_span("asset.visual", "source.visual", FP_VISUAL, 0, 1000),),
            confidence=Decimal("0.80"),
            uncertainty_ids=("uncertain.identity",),
        ),
        GraphTrack(
            "track.speaker",
            ("obs.audio.speech",),
            entity_id="entity.speaker",
            spans=(_span("asset.audio", "source.audio", FP_AUDIO, 0, 900),),
            confidence=Decimal("0.72"),
        ),
    )
    events = (
        GraphEvent(
            "event.action",
            GraphEventKind.ACTION,
            ("obs.visual.subject",),
            entity_ids=("entity.subject",),
            span=_span("asset.visual", "source.visual", FP_VISUAL, 0, 1000),
            evidence_ids=("ev.visual",),
            confidence=Decimal("0.84"),
        ),
        GraphEvent(
            "event.speech",
            GraphEventKind.SPEECH,
            ("obs.audio.speech",),
            entity_ids=("entity.speaker",),
            span=_span("asset.audio", "source.audio", FP_AUDIO, 0, 900),
            evidence_ids=("ev.audio",),
            confidence=Decimal("0.74"),
        ),
        GraphEvent(
            "event.text",
            GraphEventKind.TEXT,
            ("obs.text.visible",),
            span=_span("asset.text", "source.text", FP_TEXT, 200, 800),
            evidence_ids=("ev.text",),
            confidence=Decimal("0.81"),
        ),
    )
    return build_unified_evidence_graph(
        observations,
        entities,
        tracks,
        events,
        evidence=evidence,
        uncertainties=uncertainties,
        alternatives=(
            GraphAlternative(
                "alternative.subject",
                GraphNodeKind.ENTITY,
                "entity.subject",
                ("subject.a", "subject.b"),
                "identity remains an explicit alternative",
                uncertainty_ids=("uncertain.identity",),
            ),
        ),
        conflicts=(
            GraphConflict(
                "conflict.visual.audio",
                GraphNodeKind.OBSERVATION,
                ("obs.visual.subject", "obs.audio.speech"),
                "visible action and speech grounding are not asserted identical",
                ValidationSeverity.WARNING,
                evidence_ids=("ev.visual", "ev.audio"),
            ),
        ),
        diagnostics=("missing_audio_grounding",),
    )


def _assert_redacted(value: object) -> None:
    if isinstance(value, str):
        lowered = value.casefold()
        if any(
            marker in lowered
            for marker in (
                "http://",
                "https://",
                "file://",
                "/mnt/",
                "token=",
                "authorization",
                "password",
            )
        ):
            raise AssertionError("fixture contains sensitive locator material")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_redacted(key)
            _assert_redacted(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_redacted(item)


def run() -> dict[str, object]:
    graph = build_fixture_graph()
    summary = graph.to_public_dict()
    summary.update(
        {
            "observation_count": len(graph.observations),
            "entity_count": len(graph.entities),
            "track_count": len(graph.tracks),
            "event_count": len(graph.events),
            "evidence_count": len(graph.evidence),
            "alternative_count": len(graph.alternatives),
            "conflict_count": len(graph.conflicts),
            "network": "disabled",
            "provider_execution": "not_started",
            "media_access": "not_started",
        }
    )
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
            "M13-01 unified evidence graph fixture: "
            f"{result['status']} "
            f"({result['observation_count']} observations; no provider execution)"
        )


if __name__ == "__main__":
    main()
