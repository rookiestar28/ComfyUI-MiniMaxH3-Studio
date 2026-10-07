"""M6-05 deterministic multimodal Full-Reference timeline planner tests."""

from __future__ import annotations

import ast
import json
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    CURRENT_SCHEMA_VERSION,
    AssetRole,
    AudioAnalysisBatch,
    AudioAnalysisStatus,
    AudioObservation,
    AudioObservationKind,
    AudioRetentionMarker,
    CrossReferenceGraph,
    CrossReferenceProposal,
    CrossReferenceRequest,
    DirectiveAction,
    DirectiveAuthority,
    DirectiveRequest,
    DirectiveSetStatus,
    DirectiveTargetKind,
    DurationSource,
    EvidenceLevel,
    EvidenceOrigin,
    EvidenceRecord,
    EvidenceSet,
    EvidenceSource,
    EvidenceSourceKind,
    FullReferenceTimelineRequest,
    MediaKind,
    ModelVariant,
    MultimodalTimelineConfig,
    MultimodalTimelineStatus,
    NormalizedContextRequest,
    ObservationReference,
    ProfileIdentity,
    PromptProfile,
    ProviderIdentity,
    ReferenceAsset,
    ReferenceDirective,
    ReferenceEntityKind,
    ReferenceRegistry,
    ResolvedDirectiveSet,
    RetentionAspect,
    RetentionScope,
    SegmentDevelopment,
    SupportStatus,
    TaskMode,
    TimelineSourceSpan,
    TimelineTransition,
    TimePoint,
    VideoAnalysisBatch,
    VideoAnalysisStatus,
    VideoKeyframe,
    VideoObservation,
    VideoObservationKind,
    VideoShot,
    VisualRetentionMarker,
    build_cross_reference_graph,
    build_reference_registry,
    plan_full_reference_timeline,
    resolve_reference_directives,
)
from comfyui_h3_context.core.constraints import HardConstraintSet
from comfyui_h3_context.core.errors import FullReferenceTimelineError
from comfyui_h3_context.core.evidence import Provenance

ROOT = Path(__file__).resolve().parents[1]


def registry(
    *,
    image_role: AssetRole = AssetRole.SUBJECT_REFERENCE,
    video_role: AssetRole = AssetRole.EDITING_SOURCE,
) -> ReferenceRegistry:
    return build_reference_registry(
        (
            ReferenceAsset("image_1", MediaKind.IMAGE, image_role, 1),
            ReferenceAsset("video_1", MediaKind.VIDEO, video_role, 2),
            ReferenceAsset("audio_1", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE, 3),
        )
    )


def record(
    identifier: str,
    claim: str,
    asset_id: str,
    source_id: str,
    start: str,
    end: str,
) -> EvidenceRecord:
    source = EvidenceSource(
        EvidenceSourceKind.MEDIA_ASSET,
        source_id,
        asset_id=asset_id,
        start=TimePoint.from_text(start),
        end=TimePoint.from_text(end),
    )
    return EvidenceRecord(
        identifier,
        claim,
        EvidenceOrigin.OBSERVED,
        SupportStatus.SUPPORTED,
        Provenance(
            source,
            provider=ProviderIdentity.LOCAL,
            evidence_level=EvidenceLevel.EXPERIMENTAL,
            provider_version="fixture-1",
            source_revision="fixture-rev-1",
        ),
        confidence=Decimal("0.8"),
    )


def normalized(reference_registry: ReferenceRegistry | None = None) -> NormalizedContextRequest:
    reference_registry = registry() if reference_registry is None else reference_registry
    assets = reference_registry.to_asset_descriptors()
    return NormalizedContextRequest(
        schema_version=CURRENT_SCHEMA_VERSION,
        profile=ProfileIdentity(PromptProfile.FULL_REFERENCE, CURRENT_SCHEMA_VERSION),
        task_mode=TaskMode.REF2VA,
        model_variant=ModelVariant.BASE_REF2VA,
        user_intent="A person crosses a station while the source ambience continues.",
        requested_duration_seconds=4.0,
        effective_frame_count=107,
        effective_duration_seconds=4.0,
        duration_source=DurationSource.SECONDS,
        assets=assets,
        hard_constraints=HardConstraintSet(),
        reference_registry=reference_registry,
    )


def video_batch() -> VideoAnalysisBatch:
    shot = VideoShot(
        "shot_1",
        "video_1",
        "video_source_1",
        TimePoint.from_text("0"),
        TimePoint.from_text("2"),
        keyframe_ids=("keyframe_1",),
        observation_ids=("video_obs_1",),
    )
    keyframe = VideoKeyframe("keyframe_1", "video_1", "video_source_1", TimePoint.from_text("0"))
    observation = VideoObservation(
        "video_obs_1",
        "video_1",
        VideoObservationKind.SCENE,
        record(
            "video_evidence_1", "a quiet station platform", "video_1", "video_source_1", "0", "2"
        ),
    )
    return VideoAnalysisBatch(
        "video_batch_1",
        "h3.video.analysis.v1",
        VideoAnalysisStatus.COMPLETE,
        ("video_1",),
        sampled_frame_count=8,
        keyframes=(keyframe,),
        observations=(observation,),
        shots=(shot,),
    )


def audio_batch() -> AudioAnalysisBatch:
    first = AudioObservation(
        "audio_obs_1",
        "audio_1",
        AudioObservationKind.AMBIENCE,
        record(
            "audio_evidence_1", "distant station ambience", "audio_1", "audio_source_1", "0", "1.5"
        ),
    )
    second = AudioObservation(
        "audio_obs_2",
        "audio_1",
        AudioObservationKind.SFX,
        record("audio_evidence_2", "a soft footstep", "audio_1", "audio_source_1", "1", "2.5"),
    )
    return AudioAnalysisBatch(
        "audio_batch_1",
        "h3.audio.analysis.v1",
        AudioAnalysisStatus.COMPLETE,
        ("audio_1",),
        analyzed_sample_count=48_000,
        observations=(first, second),
    )


def graph(
    *,
    include_audio: bool = True,
    reference_registry: ReferenceRegistry | None = None,
) -> CrossReferenceGraph:
    reference_registry = registry() if reference_registry is None else reference_registry
    observations = (
        ObservationReference("video_obs_1", "video_1", "video_source_1", MediaKind.VIDEO),
    ) + (
        (ObservationReference("audio_obs_1", "audio_1", "audio_source_1", MediaKind.AUDIO),)
        if include_audio
        else ()
    )
    request = CrossReferenceRequest(TaskMode.REF2VA, reference_registry, observations)
    subject_evidence = record(
        "subject_evidence_1", "the walking person", "video_1", "video_source_1", "0", "2"
    )
    proposal = CrossReferenceProposal(
        "proposal_subject_1",
        ReferenceEntityKind.SUBJECT,
        ("person_1",),
        "walking person",
        ("video_obs_1",),
        ("video_1",),
        (subject_evidence,),
    )
    return build_cross_reference_graph(request, (proposal,))


def directives() -> ResolvedDirectiveSet:
    directive = ReferenceDirective(
        "retain_subject_1",
        DirectiveAction.RETAIN,
        DirectiveTargetKind.SUBJECT,
        "person_1",
        source_asset_ids=("video_1",),
        retention_aspects=(RetentionAspect.IDENTITY,),
        authority=DirectiveAuthority.USER_PREFERENCE,
    )
    return resolve_reference_directives(
        DirectiveRequest(TaskMode.REF2VA, registry(), (directive,), HardConstraintSet())
    )


def reference_tail_batch(tail_frame_index: int = 145) -> VideoAnalysisBatch:
    base = video_batch()
    tail_time = TimePoint.from_text(format(Decimal(tail_frame_index - 1) / 24, "f"))
    discarded = tuple(
        VideoObservation(
            f"blue_{kind.value}",
            "video_1",
            kind,
            record(
                f"blue_evidence_{kind.value}",
                f"discarded blue {kind.value}",
                "video_1",
                "video_source_1",
                tail_time.raw,
                tail_time.raw,
            ),
        )
        for kind in (
            VideoObservationKind.SCENE,
            VideoObservationKind.ACTION,
            VideoObservationKind.CAMERA,
        )
    )
    return replace(
        base,
        keyframes=(
            replace(base.keyframes[0], frame_index=1),
            VideoKeyframe(
                "discarded_frame", "video_1", "video_source_1", tail_time, tail_frame_index
            ),
        ),
        observations=(*base.observations, *discarded),
        shots=(
            replace(
                base.shots[0],
                end=TimePoint.from_text("12"),
                keyframe_ids=("keyframe_1", "discarded_frame"),
                observation_ids=("video_obs_1", *(item.observation_id for item in discarded)),
            ),
        ),
        admitted_frame_count=288,
        frame_rate=Decimal(24),
    )


def reference_tail_graph(batch: VideoAnalysisBatch) -> CrossReferenceGraph:
    return build_cross_reference_graph(
        CrossReferenceRequest(
            TaskMode.REF2VA,
            registry(),
            tuple(
                ObservationReference(
                    item.observation_id, item.asset_id, item.source_id, MediaKind.VIDEO
                )
                for item in batch.observations
            ),
        ),
        (
            CrossReferenceProposal(
                "red_person",
                ReferenceEntityKind.SUBJECT,
                ("person_1",),
                "walking person",
                ("video_obs_1",),
                ("video_1",),
                (batch.observations[0].evidence,),
            ),
            CrossReferenceProposal(
                "blue_person",
                ReferenceEntityKind.SUBJECT,
                ("person_blue",),
                "discarded blue person",
                ("blue_scene",),
                ("video_1",),
                (batch.observations[1].evidence,),
            ),
        ),
    )


class FullReferenceTimelineTests(unittest.TestCase):
    def test_clipped_and_unclipped_shots_drop_tail_from_all_conditioned_outputs(self) -> None:
        for output_count, tail_frame_index in ((124, 145), (362, 281)):
            with self.subTest(output_count=output_count):
                batch = reference_tail_batch(tail_frame_index)
                source = replace(
                    normalized(),
                    effective_frame_count=output_count,
                    effective_duration_seconds=float(Decimal(output_count) / 24),
                    evidence=EvidenceSet(tuple(item.evidence for item in batch.observations)),
                )
                result = plan_full_reference_timeline(
                    FullReferenceTimelineRequest(
                        source, batch, None, reference_tail_graph(batch), directives()
                    )
                )
                self.assertTrue(result.is_valid)
                assert result.plan is not None and result.timeline is not None
                self.assertEqual(
                    result.timeline.segments[0].video_observation_ids, ("video_obs_1",)
                )
                self.assertEqual(
                    tuple(item.subject_id for item in result.plan.intent_graph.subjects),
                    ("person_1",),
                )
                self.assertFalse(
                    result.plan.intent_graph.actions or result.plan.intent_graph.cameras
                )
                self.assertNotIn("blue", json.dumps(result.plan.to_wire()).lower())
                diagnostic = next(
                    item
                    for item in result.diagnostics
                    if item.code == "perception_outside_conditioning_window"
                )
                self.assertEqual(diagnostic.severity.value, "warning")
                self.assertIn("3 visual observations", diagnostic.message)
                self.assertTrue(
                    any(item.code == diagnostic.code for item in result.plan.limitations)
                )

    def test_zero_survivors_follow_the_existing_gap_policy(self) -> None:
        batch = reference_tail_batch()
        batch = replace(
            batch,
            keyframes=(batch.keyframes[1],),
            observations=batch.observations[1:],
            shots=(
                replace(
                    batch.shots[0],
                    keyframe_ids=("discarded_frame",),
                    observation_ids=("blue_scene", "blue_action", "blue_camera"),
                ),
            ),
        )
        cross = build_cross_reference_graph(
            CrossReferenceRequest(
                TaskMode.REF2VA,
                registry(),
                tuple(
                    ObservationReference(
                        item.observation_id, item.asset_id, item.source_id, MediaKind.VIDEO
                    )
                    for item in batch.observations
                ),
            ),
            (),
        )
        empty_directives = resolve_reference_directives(
            DirectiveRequest(TaskMode.REF2VA, registry(), (), HardConstraintSet())
        )
        for fill in (True, False):
            with self.subTest(fill=fill):
                result = plan_full_reference_timeline(
                    FullReferenceTimelineRequest(
                        normalized(),
                        batch,
                        None,
                        cross,
                        empty_directives,
                        MultimodalTimelineConfig(fill_missing_spans=fill),
                    )
                )
                self.assertEqual(result.is_valid, fill)
                self.assertTrue(
                    any(item.code == "missing_timeline_span" for item in result.diagnostics)
                )
                if fill:
                    assert result.timeline is not None and result.plan is not None
                    self.assertTrue(all(item.missing_source for item in result.timeline.segments))
                    self.assertFalse(result.timeline.source_spans or result.plan.evidence.records)

    def test_unknown_or_ambiguous_frame_joins_do_not_authorize_observations(self) -> None:
        batch = reference_tail_batch()
        for frames in (
            (replace(batch.keyframes[0], frame_index=None), *batch.keyframes[1:]),
            (*batch.keyframes, replace(batch.keyframes[0], keyframe_id="duplicate_frame")),
        ):
            with self.subTest(frame_count=len(frames)):
                changed = replace(batch, keyframes=frames)
                empty_directives = resolve_reference_directives(
                    DirectiveRequest(TaskMode.REF2VA, registry(), (), HardConstraintSet())
                )
                result = plan_full_reference_timeline(
                    FullReferenceTimelineRequest(
                        normalized(), changed, None, reference_tail_graph(changed), empty_directives
                    )
                )
                self.assertTrue(result.is_valid)
                assert result.timeline is not None and result.plan is not None
                self.assertTrue(all(item.missing_source for item in result.timeline.segments))
                self.assertFalse(result.plan.evidence.records)

    def test_source_span_and_segment_wire_are_versioned_and_explicit(self) -> None:
        span = TimelineSourceSpan(
            "span_1",
            MediaKind.VIDEO,
            "video_1",
            "video_source_1",
            TimePoint.from_text("0"),
            TimePoint.from_text("2"),
            TimePoint.from_text("0"),
            TimePoint.from_text("2"),
            shot_id="shot_1",
        )
        self.assertEqual(cast(dict[str, object], span.to_wire()["source_start"])["raw"], "0")
        self.assertEqual(cast(dict[str, object], span.to_wire()["target_end"])["raw"], "2")
        self.assertEqual(TimelineTransition.OPENING.value, "opening")

    def test_video_audio_graph_and_directives_merge_into_canonical_plan(self) -> None:
        result = plan_full_reference_timeline(
            FullReferenceTimelineRequest(
                normalized(), video_batch(), audio_batch(), graph(), directives()
            )
        )
        self.assertTrue(result.is_valid)
        self.assertIsNotNone(result.timeline)
        self.assertIsNotNone(result.plan)
        timeline = result.timeline
        assert timeline is not None
        plan = result.plan
        assert plan is not None
        self.assertEqual(timeline.status, MultimodalTimelineStatus.PARTIAL)
        self.assertEqual(len(timeline.segments), 2)
        self.assertEqual(timeline.segments[0].transition, TimelineTransition.OPENING)
        self.assertEqual(timeline.segments[0].start.seconds, Decimal("0"))
        self.assertEqual(timeline.segments[-1].end.seconds, Decimal("4"))
        self.assertTrue(any(span.modality is MediaKind.AUDIO for span in timeline.source_spans))
        self.assertEqual(plan.intent_graph.registry, registry())

    def test_subject_scope_and_legacy_scope_survive_without_development_upgrade(self) -> None:
        for scope in (RetentionScope.SUBJECT, RetentionScope.UNSPECIFIED):
            with self.subTest(scope=scope.value):
                directive = ReferenceDirective(
                    f"retain_subject_{scope.value}",
                    DirectiveAction.RETAIN,
                    DirectiveTargetKind.SUBJECT,
                    "person_1",
                    source_asset_ids=("video_1",),
                    retention_aspects=(RetentionAspect.IDENTITY,),
                    authority=DirectiveAuthority.USER_PREFERENCE,
                    retention_scope=scope,
                )
                resolved = resolve_reference_directives(
                    DirectiveRequest(TaskMode.REF2VA, registry(), (directive,), HardConstraintSet())
                )
                self.assertEqual(resolved.status, DirectiveSetStatus.COMPLETE)
                self.assertEqual(resolved.accepted[0].retention_scope, scope)

                result = plan_full_reference_timeline(
                    FullReferenceTimelineRequest(
                        normalized(), video_batch(), None, graph(include_audio=False), resolved
                    )
                )
                self.assertTrue(result.is_valid)
                assert result.plan is not None
                relation = result.plan.intent_graph.retention[0]
                self.assertEqual(relation.scope, scope)
                self.assertEqual(relation.target_id, "person_1")
                self.assertEqual(relation.marker, VisualRetentionMarker.FULLY_PRESERVED)
                self.assertTrue(
                    all(
                        segment.development is SegmentDevelopment.UNSPECIFIED
                        for segment in result.plan.intent_graph.segments
                    )
                )

    def test_audio_layer_is_partial_while_legacy_scope_stays_unspecified(self) -> None:
        for scope, expected_marker in (
            (RetentionScope.AUDIO_LAYER, AudioRetentionMarker.PARTIALLY_COPY),
            (RetentionScope.UNSPECIFIED, AudioRetentionMarker.FULLY_COPY),
        ):
            with self.subTest(scope=scope.value):
                directive = ReferenceDirective(
                    f"retain_audio_{scope.value}",
                    DirectiveAction.RETAIN,
                    DirectiveTargetKind.AUDIO,
                    "audio.audio_obs_1",
                    source_asset_ids=("audio_1",),
                    retention_aspects=(RetentionAspect.AUDIO,),
                    authority=DirectiveAuthority.USER_PREFERENCE,
                    retention_scope=scope,
                )
                resolved = resolve_reference_directives(
                    DirectiveRequest(TaskMode.REF2VA, registry(), (directive,), HardConstraintSet())
                )
                self.assertEqual(resolved.status, DirectiveSetStatus.COMPLETE)
                self.assertEqual(resolved.accepted[0].retention_scope, scope)

                result = plan_full_reference_timeline(
                    FullReferenceTimelineRequest(
                        normalized(), video_batch(), audio_batch(), graph(), resolved
                    )
                )
                self.assertTrue(result.is_valid)
                assert result.plan is not None
                relation = result.plan.intent_graph.retention[0]
                self.assertEqual(relation.scope, scope)
                self.assertEqual(relation.marker, expected_marker)
                self.assertTrue(
                    all(
                        segment.development is SegmentDevelopment.UNSPECIFIED
                        for segment in result.plan.intent_graph.segments
                    )
                )

    def test_asset_scoped_picture_and_video_structure_survive_with_exact_targets(self) -> None:
        cases = (
            (
                RetentionScope.PICTURE,
                "image_1",
                registry(image_role=AssetRole.FIRST_FRAME),
            ),
            (RetentionScope.VIDEO_STRUCTURE, "video_1", registry()),
        )
        for scope, target_id, reference_registry in cases:
            with self.subTest(scope=scope.value):
                directive = ReferenceDirective(
                    f"retain_{scope.value}",
                    DirectiveAction.RETAIN,
                    DirectiveTargetKind.ASSET,
                    target_id,
                    source_asset_ids=(target_id,),
                    retention_aspects=(RetentionAspect.IDENTITY,),
                    authority=DirectiveAuthority.USER_PREFERENCE,
                    retention_scope=scope,
                )
                resolved = resolve_reference_directives(
                    DirectiveRequest(
                        TaskMode.REF2VA, reference_registry, (directive,), HardConstraintSet()
                    )
                )
                self.assertEqual(resolved.status, DirectiveSetStatus.COMPLETE)
                self.assertEqual(resolved.accepted[0].retention_scope, scope)
                result = plan_full_reference_timeline(
                    FullReferenceTimelineRequest(
                        normalized(reference_registry),
                        video_batch(),
                        None,
                        graph(include_audio=False, reference_registry=reference_registry),
                        resolved,
                    )
                )

                self.assertTrue(result.is_valid)
                assert result.plan is not None
                relation = result.plan.intent_graph.retention[0]
                self.assertEqual(relation.scope, scope)
                self.assertEqual(relation.target_id, target_id)
                self.assertEqual(relation.source_asset_ids, (target_id,))
                self.assertTrue(
                    all(
                        segment.development is SegmentDevelopment.UNSPECIFIED
                        for segment in result.plan.intent_graph.segments
                    )
                )

    def test_asset_scoped_picture_and_video_structure_reject_unqualified_targets(self) -> None:
        cases = (
            (RetentionScope.PICTURE, "image_1", registry()),
            (
                RetentionScope.VIDEO_STRUCTURE,
                "video_1",
                registry(video_role=AssetRole.REFERENCE),
            ),
        )
        for scope, target_id, reference_registry in cases:
            with self.subTest(scope=scope.value):
                directive = ReferenceDirective(
                    f"retain_bad_{scope.value}",
                    DirectiveAction.RETAIN,
                    DirectiveTargetKind.ASSET,
                    target_id,
                    source_asset_ids=(target_id,),
                    retention_aspects=(RetentionAspect.IDENTITY,),
                    authority=DirectiveAuthority.USER_PREFERENCE,
                    retention_scope=scope,
                )
                resolved = resolve_reference_directives(
                    DirectiveRequest(
                        TaskMode.REF2VA, reference_registry, (directive,), HardConstraintSet()
                    )
                )
                result = plan_full_reference_timeline(
                    FullReferenceTimelineRequest(
                        normalized(reference_registry),
                        video_batch(),
                        None,
                        graph(include_audio=False, reference_registry=reference_registry),
                        resolved,
                    )
                )

                self.assertFalse(result.is_valid)
                self.assertTrue(
                    any(
                        item.code == "retention_target_scope_mismatch"
                        for item in result.diagnostics
                    )
                )

    def test_overlapping_audio_is_retained_without_visual_overlap_error(self) -> None:
        result = plan_full_reference_timeline(
            FullReferenceTimelineRequest(
                normalized(), video_batch(), audio_batch(), graph(), directives()
            )
        )
        self.assertTrue(result.is_valid)
        timeline = result.timeline
        assert timeline is not None
        self.assertEqual(timeline.status, MultimodalTimelineStatus.PARTIAL)
        audio_spans = [span for span in timeline.source_spans if span.modality is MediaKind.AUDIO]
        self.assertGreaterEqual(len(audio_spans), 2)
        self.assertTrue(any(span.overlap_group_id is not None for span in audio_spans))

    def test_copy_event_directive_attaches_to_target_segment_and_graph(self) -> None:
        copy = ReferenceDirective(
            "copy_event_1",
            DirectiveAction.COPY_EVENT,
            DirectiveTargetKind.EVENT,
            "event_source_1",
            source_asset_ids=("video_1",),
            source_event_id="event_source_1",
            target_segment_id="segment_shot_1",
            authority=DirectiveAuthority.USER_PREFERENCE,
        )
        resolved = resolve_reference_directives(
            DirectiveRequest(TaskMode.REF2VA, registry(), (copy,), HardConstraintSet())
        )
        result = plan_full_reference_timeline(
            FullReferenceTimelineRequest(
                normalized(), video_batch(), None, graph(include_audio=False), resolved
            )
        )
        self.assertTrue(result.is_valid)
        timeline = result.timeline
        plan = result.plan
        assert timeline is not None
        assert plan is not None
        self.assertIn("copy_event_1", timeline.segments[0].directive_ids)
        self.assertIn("copy_event_1", plan.intent_graph.segments[0].event_ids)
        self.assertEqual(plan.intent_graph.events[0].target_segment_id, "segment_shot_1")

    def test_missing_video_tail_becomes_explicit_gap_fill(self) -> None:
        config = MultimodalTimelineConfig(fill_missing_spans=True)
        result = plan_full_reference_timeline(
            FullReferenceTimelineRequest(
                normalized(), video_batch(), None, graph(include_audio=False), directives(), config
            )
        )
        self.assertTrue(result.is_valid)
        timeline = result.timeline
        assert timeline is not None
        self.assertEqual(timeline.status, MultimodalTimelineStatus.PARTIAL)
        self.assertTrue(any(segment.missing_source for segment in timeline.segments))
        self.assertTrue(any(item.code == "missing_timeline_span" for item in timeline.diagnostics))

    def test_visual_overlap_and_conflicting_directives_fail_closed(self) -> None:
        first = video_batch()
        overlapping = VideoShot(
            "shot_2",
            "video_1",
            "video_source_1",
            TimePoint.from_text("1"),
            TimePoint.from_text("3"),
        )
        bad_video = VideoAnalysisBatch(
            first.batch_id,
            first.schema,
            first.status,
            first.selected_asset_ids,
            sampled_frame_count=first.sampled_frame_count,
            keyframes=first.keyframes,
            observations=first.observations,
            shots=(first.shots[0], overlapping),
        )
        result = plan_full_reference_timeline(
            FullReferenceTimelineRequest(
                normalized(), bad_video, None, graph(include_audio=False), directives()
            )
        )
        self.assertFalse(result.is_valid)
        self.assertTrue(any(item.code == "visual_overlap" for item in result.diagnostics))

        conflict = resolve_reference_directives(
            DirectiveRequest(
                TaskMode.REF2VA,
                registry(),
                (
                    ReferenceDirective(
                        "exclude_a",
                        DirectiveAction.EXCLUDE,
                        DirectiveTargetKind.SUBJECT,
                        "person_1",
                        source_asset_ids=("video_1",),
                        reason="exclude",
                        authority=DirectiveAuthority.USER_PREFERENCE,
                    ),
                    ReferenceDirective(
                        "exclude_b",
                        DirectiveAction.EXCLUDE,
                        DirectiveTargetKind.SUBJECT,
                        "person_1",
                        source_asset_ids=("video_1",),
                        reason="keep",
                        authority=DirectiveAuthority.USER_PREFERENCE,
                    ),
                ),
                HardConstraintSet(),
            )
        )
        self.assertEqual(conflict.status, DirectiveSetStatus.CONFLICTING)
        blocked = plan_full_reference_timeline(
            FullReferenceTimelineRequest(
                normalized(), video_batch(), None, graph(include_audio=False), conflict
            )
        )
        self.assertFalse(blocked.is_valid)
        self.assertTrue(any(item.code == "directive_conflict" for item in blocked.diagnostics))

    def test_registry_graph_and_directive_mismatches_are_rejected(self) -> None:
        with self.assertRaises(FullReferenceTimelineError):
            FullReferenceTimelineRequest(
                normalized(),
                video_batch(),
                audio_batch(),
                graph(),
                resolve_reference_directives(
                    DirectiveRequest(TaskMode.REF2VA, ReferenceRegistry.empty(), ())
                ),
            )

    def test_empty_or_corrupt_audio_remains_partial_without_invented_audio(self) -> None:
        empty = AudioAnalysisBatch(
            "audio_empty",
            "h3.audio.analysis.v1",
            AudioAnalysisStatus.EMPTY,
            ("audio_1",),
            diagnostics=(),
        )
        result = plan_full_reference_timeline(
            FullReferenceTimelineRequest(
                normalized(), video_batch(), empty, graph(include_audio=False), directives()
            )
        )
        self.assertTrue(result.is_valid)
        timeline = result.timeline
        assert timeline is not None
        self.assertEqual(timeline.status, MultimodalTimelineStatus.PARTIAL)
        self.assertFalse(any(span.modality is MediaKind.AUDIO for span in timeline.source_spans))

    def test_repeatability_and_optional_runtime_boundary(self) -> None:
        current = FullReferenceTimelineRequest(
            normalized(), video_batch(), audio_batch(), graph(), directives()
        )
        first = plan_full_reference_timeline(current)
        second = plan_full_reference_timeline(current)
        self.assertEqual(first.to_wire(), second.to_wire())
        assert first.timeline is not None
        self.assertTrue(first.timeline.fingerprint.startswith("sha256:"))
        tree = ast.parse(
            (ROOT / "comfyui_h3_context" / "core" / "full_reference_timeline.py").read_text()
        )
        modules = [node.module or "" for node in tree.body if isinstance(node, ast.ImportFrom)]
        self.assertFalse(any(name in {"torch", "transformers", "av", "cv2"} for name in modules))

    def test_schema_and_metadata_fixture_are_present(self) -> None:
        schema = json.loads(
            (
                ROOT / "governance" / "contracts" / "full_reference_timeline_v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/full_reference_timeline_v1.schema.json"
        )
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "m6_full_reference_timeline.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(fixture["schema"], "h3.full_reference.timeline.v1")


if __name__ == "__main__":
    unittest.main()
