"""M13-01 unified multimodal evidence graph tests."""

from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path

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
    GraphStatus,
    GraphSupport,
    GraphTrack,
    GraphUncertainty,
    MediaKind,
    Provenance,
    ProviderIdentity,
    SupportStatus,
    UncertaintyKind,
    UnifiedEvidenceGraphError,
    ValidationSeverity,
    build_unified_evidence_graph,
)
from scripts.m13_01_unified_evidence_graph_fixture import run

ROOT = Path(__file__).resolve().parents[1]
FP_A = "sha256:" + "a" * 64
FP_B = "sha256:" + "b" * 64


def _record(evidence_id: str, *, asset_id: str = "asset.a") -> EvidenceRecord:
    source = EvidenceSource(
        EvidenceSourceKind.MEDIA_ASSET,
        "source.a",
        asset_id=asset_id,
        span="bounded_span",
    )
    return EvidenceRecord(
        evidence_id,
        "bounded observed claim",
        EvidenceOrigin.OBSERVED,
        support=SupportStatus.SUPPORTED,
        provenance=Provenance(
            source,
            ProviderIdentity.LOCAL,
            EvidenceLevel.EXPERIMENTAL,
            provider_version="1.0.0",
            source_revision="fixture.rev",
        ),
        confidence=Decimal("0.90"),
    )


def _span(
    asset_id: str = "asset.a", source_id: str = "source.a", fingerprint: str = FP_A
) -> GraphSourceSpan:
    return GraphSourceSpan(asset_id, source_id, fingerprint, 0, 1000)


def _observation(
    observation_id: str,
    *,
    asset_id: str = "asset.a",
    source_id: str = "source.a",
    fingerprint: str = FP_A,
    kind: GraphObservationKind = GraphObservationKind.SUBJECT,
    modality: MediaKind = MediaKind.IMAGE,
    evidence_ids: tuple[str, ...] = ("ev.a",),
    support: GraphSupport = GraphSupport.SUPPORTED,
    entity_ids: tuple[str, ...] = ("entity.person",),
    track_ids: tuple[str, ...] = ("track.person",),
    event_ids: tuple[str, ...] = ("event.action",),
) -> GraphObservation:
    return GraphObservation(
        observation_id,
        asset_id,
        modality,
        kind,
        "person acts",
        _span(asset_id, source_id, fingerprint),
        evidence_ids=evidence_ids,
        support=support,
        confidence=Decimal("0.90") if support is not GraphSupport.MISSING else None,
        uncertainty_ids=("uncertain.a",) if support is not GraphSupport.SUPPORTED else (),
        entity_ids=entity_ids,
        track_ids=track_ids,
        event_ids=event_ids,
    )


def _complete_inputs() -> tuple[
    tuple[EvidenceRecord, ...],
    tuple[GraphObservation, ...],
    tuple[GraphEntity, ...],
    tuple[GraphTrack, ...],
    tuple[GraphEvent, ...],
]:
    evidence = (_record("ev.a"),)
    observations = (_observation("obs.a"),)
    entities = (
        GraphEntity(
            "entity.person",
            GraphEntityKind.SUBJECT,
            observation_ids=("obs.a",),
            track_ids=("track.person",),
            evidence_ids=("ev.a",),
        ),
    )
    tracks = (
        GraphTrack(
            "track.person", observation_ids=("obs.a",), entity_id="entity.person", spans=(_span(),)
        ),
    )
    events = (
        GraphEvent(
            "event.action",
            GraphEventKind.ACTION,
            observation_ids=("obs.a",),
            entity_ids=("entity.person",),
            span=_span(),
            evidence_ids=("ev.a",),
        ),
    )
    return evidence, observations, entities, tracks, events


class UnifiedEvidenceGraphTests(unittest.TestCase):
    def test_schema_fixture_and_deterministic_ordering(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "unified_evidence_graph_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/unified_evidence_graph_v1.schema.json"
        )
        evidence, observations, entities, tracks, events = _complete_inputs()
        first = build_unified_evidence_graph(
            observations, entities, tracks, events, evidence=evidence
        )
        second = build_unified_evidence_graph(
            tuple(reversed(observations)), entities, tracks, events, evidence=evidence
        )
        self.assertEqual(first.status, GraphStatus.COMPLETE)
        self.assertEqual(first.fingerprint, second.fingerprint)
        wire_observations = first.to_wire()["observations"]
        assert isinstance(wire_observations, list)
        first_observation = wire_observations[0]
        assert isinstance(first_observation, dict)
        self.assertEqual(first_observation.get("observation_id"), "obs.a")
        summary = run()
        self.assertEqual(summary.get("status"), GraphStatus.CONFLICTING.value)
        self.assertEqual(summary.get("network"), "disabled")

    def test_source_evidence_and_referential_integrity_are_required(self) -> None:
        evidence, observations, entities, tracks, events = _complete_inputs()
        with self.assertRaises(UnifiedEvidenceGraphError):
            build_unified_evidence_graph(observations, entities, tracks, events, evidence=())
        with self.assertRaises(UnifiedEvidenceGraphError):
            build_unified_evidence_graph(
                observations,
                (GraphEntity("entity.bad", GraphEntityKind.SUBJECT, observation_ids=("unknown",)),),
                tracks,
                events,
                evidence=evidence,
            )
        with self.assertRaises(UnifiedEvidenceGraphError):
            build_unified_evidence_graph(
                (
                    GraphObservation(
                        "obs.bad",
                        "asset.a",
                        MediaKind.IMAGE,
                        GraphObservationKind.SUBJECT,
                        "person acts",
                        _span("asset.b", "source.b", FP_B),
                        evidence_ids=("ev.a",),
                    ),
                ),
                entities,
                tracks,
                events,
                evidence=evidence,
            )

    def test_missing_and_unsupported_evidence_remain_explicit(self) -> None:
        missing = GraphObservation(
            "obs.missing",
            "asset.a",
            MediaKind.AUDIO,
            GraphObservationKind.AUDIO,
            "audio unavailable",
            None,
            support=GraphSupport.MISSING,
            uncertainty_ids=("uncertain.missing",),
        )
        graph = build_unified_evidence_graph(
            (missing,),
            uncertainties=(
                GraphUncertainty(
                    "uncertain.missing",
                    UncertaintyKind.MISSING_SOURCE,
                    "audio source was not available",
                    ValidationSeverity.WARNING,
                ),
            ),
        )
        self.assertEqual(graph.status, GraphStatus.PARTIAL)
        self.assertEqual(graph.observations[0].support, GraphSupport.MISSING)
        self.assertIsNone(graph.observations[0].source_span)
        self.assertIn("uncertain.missing", graph.observations[0].uncertainty_ids)

    def test_alternatives_and_conflicts_are_retained_without_majority_selection(self) -> None:
        evidence, observations, entities, tracks, events = _complete_inputs()
        alternative = GraphAlternative(
            "alternative.person",
            GraphNodeKind.ENTITY,
            "entity.person",
            ("person.a", "person.b"),
            "two explicit identity hypotheses",
        )
        conflict = GraphConflict(
            "conflict.action",
            GraphNodeKind.OBSERVATION,
            ("obs.a", "obs.b"),
            "visual and audio action labels disagree",
        )
        extra = _observation(
            "obs.b",
            asset_id="asset.b",
            source_id="source.b",
            fingerprint=FP_B,
            evidence_ids=(),
            entity_ids=(),
            track_ids=(),
            event_ids=(),
        )
        graph = build_unified_evidence_graph(
            (observations[0], extra),
            entities,
            tracks,
            events,
            evidence=evidence,
            alternatives=(alternative,),
            conflicts=(conflict,),
        )
        self.assertEqual(graph.status, GraphStatus.CONFLICTING)
        self.assertEqual(graph.alternatives[0].candidate_ids, ("person.a", "person.b"))
        self.assertEqual(graph.conflicts[0].target_ids, ("obs.a", "obs.b"))
        self.assertEqual(graph.entities[0].resolution, GraphResolution.RESOLVED)

    def test_unsafe_values_duplicate_ids_and_confidence_fail_closed(self) -> None:
        evidence, observations, entities, tracks, events = _complete_inputs()
        with self.assertRaises(UnifiedEvidenceGraphError):
            GraphSourceSpan("asset.a", "source.a", "sha256:" + "z" * 64, 0, 1)
        with self.assertRaises(UnifiedEvidenceGraphError):
            GraphObservation(
                "obs.url",
                "asset.a",
                MediaKind.IMAGE,
                GraphObservationKind.TEXT,
                "https://secret",
                _span(),
            )
        with self.assertRaises(UnifiedEvidenceGraphError):
            build_unified_evidence_graph(
                observations + observations, entities, tracks, events, evidence=evidence
            )
        with self.assertRaises(UnifiedEvidenceGraphError):
            GraphEntity("entity.bad", GraphEntityKind.SUBJECT, confidence=Decimal("1.2"))


if __name__ == "__main__":
    unittest.main()
