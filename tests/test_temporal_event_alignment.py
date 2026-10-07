"""M13-03 cross-modal temporal event and causality alignment tests."""

from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path

from comfyui_h3_context.core import (
    AssetRole,
    GraphEvent,
    GraphEventKind,
    GraphObservation,
    GraphObservationKind,
    GraphSourceSpan,
    MediaKind,
    ReferenceAsset,
    ReferenceRole,
    ReferenceRoleProposal,
    RoleResolution,
    TemporalAlignmentStatus,
    TemporalEvent,
    TemporalEventAlignmentError,
    TemporalEventKind,
    TemporalEventResolution,
    TemporalEventSupport,
    TemporalGroundingInterval,
    TemporalRelation,
    TemporalRelationKind,
    TemporalRelationResolution,
    TemporalTimeModel,
    UnifiedEvidenceGraph,
    build_reference_registry,
    build_reference_role_resolution,
    build_temporal_event_alignment,
    build_unified_evidence_graph,
)
from comfyui_h3_context.core.reference_role_resolution import ReferenceRoleResolutionGraph

ROOT = Path(__file__).resolve().parents[1]
FP_VIDEO = "sha256:" + "a" * 64
FP_AUDIO = "sha256:" + "b" * 64
FP_IMAGE = "sha256:" + "c" * 64


def _span(asset_id: str, source_id: str, fingerprint: str, start: int, end: int) -> GraphSourceSpan:
    return GraphSourceSpan(asset_id, source_id, fingerprint, start, end)


def _inputs() -> tuple[
    TemporalTimeModel,
    UnifiedEvidenceGraph,
    ReferenceRoleResolutionGraph,
    tuple[TemporalEvent, ...],
    tuple[TemporalRelation, ...],
]:
    registry = build_reference_registry(
        (
            ReferenceAsset("image.ref", MediaKind.IMAGE, AssetRole.REFERENCE, 1),
            ReferenceAsset("video.ref", MediaKind.VIDEO, AssetRole.REFERENCE, 2),
            ReferenceAsset("audio.ref", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE, 3),
        )
    )
    specs = (
        (
            "obs.ref",
            "image.ref",
            MediaKind.IMAGE,
            GraphObservationKind.SUBJECT,
            "source.image",
            FP_IMAGE,
            0,
            200,
            "event.reference",
        ),
        (
            "obs.shot.1",
            "video.ref",
            MediaKind.VIDEO,
            GraphObservationKind.SCENE,
            "source.video",
            FP_VIDEO,
            0,
            500,
            "event.shot.1",
        ),
        (
            "obs.action",
            "video.ref",
            MediaKind.VIDEO,
            GraphObservationKind.ACTION,
            "source.video",
            FP_VIDEO,
            100,
            300,
            "event.action",
        ),
        (
            "obs.state",
            "video.ref",
            MediaKind.VIDEO,
            GraphObservationKind.ACTION,
            "source.video",
            FP_VIDEO,
            300,
            500,
            "event.state",
        ),
        (
            "obs.shot.2",
            "video.ref",
            MediaKind.VIDEO,
            GraphObservationKind.SCENE,
            "source.video",
            FP_VIDEO,
            500,
            1000,
            "event.shot.2",
        ),
        (
            "obs.dialogue",
            "audio.ref",
            MediaKind.AUDIO,
            GraphObservationKind.SPEECH,
            "source.audio",
            FP_AUDIO,
            150,
            350,
            "event.dialogue",
        ),
        (
            "obs.text",
            "video.ref",
            MediaKind.VIDEO,
            GraphObservationKind.TEXT,
            "source.video",
            FP_VIDEO,
            450,
            600,
            "event.text",
        ),
        (
            "obs.audio",
            "audio.ref",
            MediaKind.AUDIO,
            GraphObservationKind.AUDIO,
            "source.audio",
            FP_AUDIO,
            450,
            650,
            "event.audio",
        ),
        (
            "obs.cut",
            "video.ref",
            MediaKind.VIDEO,
            GraphObservationKind.ACTION,
            "source.video",
            FP_VIDEO,
            500,
            501,
            "event.transition",
        ),
    )
    observations = tuple(
        GraphObservation(
            observation_id,
            asset_id,
            modality,
            kind,
            label,
            _span(asset_id, source_id, fingerprint, start, end),
            event_ids=(event_id,),
        )
        for (
            observation_id,
            asset_id,
            modality,
            kind,
            source_id,
            fingerprint,
            start,
            end,
            event_id,
        ) in specs
        for label in (event_id.replace("event.", "observed "),)
    )
    graph_events = tuple(
        GraphEvent(
            event_id,
            GraphEventKind.AUDIO
            if event_id in {"event.dialogue", "event.audio"}
            else GraphEventKind.TEXT
            if event_id == "event.text"
            else GraphEventKind.EDIT
            if event_id == "event.transition"
            else GraphEventKind.ACTION,
            observation_ids=(observation_id,),
            span=_span(asset_id, source_id, fingerprint, start, end),
        )
        for (
            observation_id,
            asset_id,
            _modality,
            _kind,
            source_id,
            fingerprint,
            start,
            end,
            event_id,
        ) in specs
    )
    graph = build_unified_evidence_graph(observations, events=graph_events)
    role_graph = build_reference_role_resolution(
        graph,
        registry,
        (
            ReferenceRoleProposal(
                "proposal.reference",
                ReferenceRole.PICTURE,
                ("image.ref",),
                "<Picture 1>",
                ("obs.ref",),
                ("image.ref",),
                (),
                RoleResolution.RESOLVED,
                Decimal("0.90"),
            ),
        ),
    )
    time_model = TemporalTimeModel(
        "model.identity",
        1,
        1000,
        1,
        1000,
        1000,
        1000,
        1,
        1,
        0,
    )

    def event(
        event_id: str,
        kind: TemporalEventKind,
        observation_id: str,
        asset_id: str,
        fingerprint: str,
        start: int,
        end: int,
        *,
        shot_ids: tuple[str, ...] = (),
        roles: tuple[str, ...] = (),
    ) -> TemporalEvent:
        return TemporalEvent(
            event_id,
            kind,
            event_id.replace("event.", "observed "),
            (observation_id,),
            (asset_id,),
            (fingerprint,),
            TemporalGroundingInterval(f"interval.source.{event_id}", "source", start, end),
            TemporalGroundingInterval(f"interval.target.{event_id}", "target", start, end),
            shot_ids=shot_ids,
            role_candidate_ids=roles,
            support=TemporalEventSupport.SUPPORTED,
            resolution=TemporalEventResolution.RESOLVED,
        )

    events = (
        event(
            "event.reference",
            TemporalEventKind.REFERENCE,
            "obs.ref",
            "image.ref",
            FP_IMAGE,
            0,
            200,
            roles=("image.ref",),
        ),
        event(
            "event.shot.1",
            TemporalEventKind.SHOT,
            "obs.shot.1",
            "video.ref",
            FP_VIDEO,
            0,
            500,
            shot_ids=("shot.1",),
        ),
        event(
            "event.action",
            TemporalEventKind.ACTION,
            "obs.action",
            "video.ref",
            FP_VIDEO,
            100,
            300,
            shot_ids=("shot.1",),
        ),
        event(
            "event.state",
            TemporalEventKind.STATE_CHANGE,
            "obs.state",
            "video.ref",
            FP_VIDEO,
            300,
            500,
            shot_ids=("shot.1",),
        ),
        event(
            "event.shot.2",
            TemporalEventKind.SHOT,
            "obs.shot.2",
            "video.ref",
            FP_VIDEO,
            500,
            1000,
            shot_ids=("shot.2",),
        ),
        event(
            "event.dialogue",
            TemporalEventKind.DIALOGUE,
            "obs.dialogue",
            "audio.ref",
            FP_AUDIO,
            150,
            350,
            shot_ids=("shot.1",),
        ),
        event(
            "event.text",
            TemporalEventKind.VISIBLE_TEXT,
            "obs.text",
            "video.ref",
            FP_VIDEO,
            450,
            600,
            shot_ids=("shot.1", "shot.2"),
        ),
        event(
            "event.audio",
            TemporalEventKind.AUDIO_EVENT,
            "obs.audio",
            "audio.ref",
            FP_AUDIO,
            450,
            650,
            shot_ids=("shot.1", "shot.2"),
        ),
        event(
            "event.transition",
            TemporalEventKind.TRANSITION,
            "obs.cut",
            "video.ref",
            FP_VIDEO,
            500,
            501,
            shot_ids=("shot.1", "shot.2"),
        ),
    )
    relations = (
        TemporalRelation(
            "relation.overlap",
            TemporalRelationKind.OVERLAP,
            ("event.action",),
            ("event.dialogue",),
            TemporalRelationResolution.EXPLICIT,
        ),
        TemporalRelation(
            "relation.gap",
            TemporalRelationKind.GAP,
            ("event.shot.1",),
            ("event.shot.2",),
            TemporalRelationResolution.EXPLICIT,
        ),
        TemporalRelation(
            "relation.cut",
            TemporalRelationKind.CUT,
            ("event.shot.1",),
            ("event.shot.2",),
            TemporalRelationResolution.EXPLICIT,
        ),
        TemporalRelation(
            "relation.cross_audio",
            TemporalRelationKind.CROSS_SHOT_AUDIO,
            ("event.audio",),
            ("event.shot.1", "event.shot.2"),
            TemporalRelationResolution.EXPLICIT,
        ),
        TemporalRelation(
            "relation.continuation",
            TemporalRelationKind.CONTINUATION,
            ("event.action",),
            ("event.state",),
            TemporalRelationResolution.EXPLICIT,
        ),
        TemporalRelation(
            "relation.copy",
            TemporalRelationKind.COPY_EVENT,
            ("event.reference",),
            ("event.audio",),
            TemporalRelationResolution.EXPLICIT,
        ),
        TemporalRelation(
            "relation.before",
            TemporalRelationKind.BEFORE,
            ("event.action",),
            ("event.shot.2",),
            TemporalRelationResolution.EXPLICIT,
        ),
        TemporalRelation(
            "relation.after",
            TemporalRelationKind.AFTER,
            ("event.shot.2",),
            ("event.action",),
            TemporalRelationResolution.EXPLICIT,
        ),
        TemporalRelation(
            "relation.causes",
            TemporalRelationKind.CAUSES,
            ("event.action",),
            ("event.state",),
            TemporalRelationResolution.INFERRED,
        ),
        TemporalRelation(
            "relation.caused_by",
            TemporalRelationKind.CAUSED_BY,
            ("event.state",),
            ("event.action",),
            TemporalRelationResolution.INFERRED,
        ),
        TemporalRelation(
            "relation.uncertain",
            TemporalRelationKind.UNCERTAIN_CAUSALITY,
            ("event.dialogue",),
            ("event.audio",),
            TemporalRelationResolution.UNCERTAIN,
            uncertainty_ids=("uncertain.causality",),
        ),
    )
    return time_model, graph, role_graph, events, relations


class TemporalEventAlignmentTests(unittest.TestCase):
    def test_schema_fixture_and_deterministic_fingerprint(self) -> None:
        schema = json.loads(
            (
                ROOT / "governance" / "contracts" / "temporal_event_alignment_v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/temporal_event_alignment_v1.schema.json"
        )
        time_model, graph, role_graph, events, relations = _inputs()
        first = build_temporal_event_alignment(graph, role_graph, time_model, events, relations)
        second = build_temporal_event_alignment(
            graph, role_graph, time_model, tuple(reversed(events)), tuple(reversed(relations))
        )
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.status, TemporalAlignmentStatus.AMBIGUOUS)
        self.assertTrue(first.metrics.interval_metric_pass)

    def test_one_source_target_model_and_referential_integrity(self) -> None:
        time_model, graph, role_graph, events, relations = _inputs()
        built = build_temporal_event_alignment(graph, role_graph, time_model, events, relations)
        self.assertEqual(built.time_model.model_id, "model.identity")
        self.assertEqual(built.events[0].source_interval.domain, "source")
        with self.assertRaises(TemporalEventAlignmentError):
            bad = list(events)
            bad[0] = TemporalEvent(
                bad[0].event_id,
                bad[0].kind,
                bad[0].label,
                bad[0].observation_ids,
                bad[0].source_asset_ids,
                bad[0].source_fingerprints,
                bad[0].source_interval,
                TemporalGroundingInterval("interval.target.bad", "target", 1, 201),
                role_candidate_ids=bad[0].role_candidate_ids,
            )
            build_temporal_event_alignment(graph, role_graph, time_model, tuple(bad), relations)

    def test_required_temporal_grounding_relations_are_explicit(self) -> None:
        time_model, graph, role_graph, events, relations = _inputs()
        built = build_temporal_event_alignment(graph, role_graph, time_model, events, relations)
        self.assertEqual({item.kind for item in built.events}, set(TemporalEventKind))
        self.assertEqual({item.kind for item in built.relations}, set(TemporalRelationKind))
        self.assertEqual(built.metrics.gap_count, 1)
        self.assertEqual(built.metrics.overlap_count, 1)
        self.assertEqual(built.metrics.cross_shot_audio_count, 1)
        self.assertEqual(built.metrics.uncertain_causality_count, 1)

    def test_uncertain_causality_is_not_a_fact(self) -> None:
        time_model, graph, role_graph, events, relations = _inputs()
        built = build_temporal_event_alignment(graph, role_graph, time_model, events, relations)
        uncertain = next(
            item
            for item in built.relations
            if item.kind is TemporalRelationKind.UNCERTAIN_CAUSALITY
        )
        self.assertEqual(uncertain.resolution, TemporalRelationResolution.UNCERTAIN)
        self.assertEqual(built.status, TemporalAlignmentStatus.AMBIGUOUS)

    def test_unsafe_values_and_duplicate_ids_fail_closed(self) -> None:
        time_model, graph, role_graph, events, relations = _inputs()
        with self.assertRaises(TemporalEventAlignmentError):
            TemporalGroundingInterval("interval.bad", "source", 20, 10)
        with self.assertRaises(TemporalEventAlignmentError):
            TemporalEvent(
                "event.bad",
                TemporalEventKind.ACTION,
                "https://secret",
                (),
                (),
                (),
                TemporalGroundingInterval("s", "source", 0, 1),
                TemporalGroundingInterval("t", "target", 0, 1),
            )
        with self.assertRaises(TemporalEventAlignmentError):
            build_temporal_event_alignment(
                graph, role_graph, time_model, events, relations + (relations[0],)
            )


if __name__ == "__main__":
    unittest.main()
