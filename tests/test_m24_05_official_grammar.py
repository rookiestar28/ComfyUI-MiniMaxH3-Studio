"""Independent M24-05 oracle for current Full retention grammar and legacy reading."""

from __future__ import annotations

import unittest
from dataclasses import replace
from typing import cast

from test_full_rendering import full_plan

from comfyui_h3_context.core import (
    AssetRole,
    AudioRetentionMarker,
    PromptRenderingError,
    RetentionScope,
    TaskMode,
    VisualRetentionMarker,
    render_full_reference_prompt,
)
from comfyui_h3_context.core.semantic_graph_comparator import (
    LEGACY_SEMANTIC_GRAPH_SCHEMA,
    SEMANTIC_GRAPH_SCHEMA,
    SemanticDiffOutcome,
    SemanticGraphError,
    SemanticSourceKind,
    compare_semantic_graphs,
    parse_legacy_semantic_prompt_v1,
    parse_semantic_prompt,
    validate_current_semantic_graph_wire,
    validate_semantic_graph_wire,
)


class OfficialGrammarTests(unittest.TestCase):
    def test_current_output_uses_literal_guide_markers_and_combined_tasks(self) -> None:
        plan = full_plan()
        document = render_full_reference_prompt(plan)
        sections = {section.heading: section.body for section in document.sections}

        self.assertEqual(
            sections["summary"].split("]", 1)[0] + "]",
            "[video editing + reference generation + audio reuse + audio reference]",
        )
        self.assertIn("The target video is an edited version of <Video 1>.", sections["summary"])
        self.assertIn(
            "<Subject 1>: fully_preserved - <Subject 1> retains its defined subject role",
            sections["retention_analysis"],
        )
        self.assertIn(
            "<Audio 1>: partially_copy - the selected audio layer from <Audio 1>",
            sections["retention_analysis"],
        )
        self.assertNotIn("retention_1", document.text)
        self.assertNotIn(" -> ", sections["retention_analysis"])

        definition_lines = sections["subject_definitions"].splitlines()
        self.assertIn("<Subject 2> is the bakery interior from <Video 1>.", definition_lines)
        self.assertIn("<Video 1> is the footage being edited.", definition_lines)
        self.assertNotIn("<Picture 2> is a supplied visual reference.", definition_lines)

    def test_presence_only_assets_do_not_invent_a_task_relationship(self) -> None:
        plan = full_plan()
        registry = replace(
            plan.request.reference_registry,
            assets=tuple(
                replace(asset, role=AssetRole.REFERENCE)
                for asset in plan.request.reference_registry.assets
            ),
        )
        subjects = tuple(
            replace(subject, source_asset_ids=()) for subject in plan.intent_graph.subjects
        )
        segments = tuple(
            replace(segment, audio_ids=(), event_ids=()) for segment in plan.intent_graph.segments
        )
        graph = replace(
            plan.intent_graph,
            registry=registry,
            subjects=subjects,
            audios=(),
            events=(),
            retention=(),
            segments=segments,
        )
        unbound = replace(
            plan,
            request=replace(
                plan.request,
                assets=registry.to_asset_descriptors(),
                reference_registry=registry,
            ),
            intent_graph=graph,
        )

        summary = render_full_reference_prompt(unbound).sections[1].body
        self.assertTrue(summary.startswith("The task relationship is not explicitly declared."))
        for official_task in (
            "keyframe completion",
            "reference generation",
            "video editing",
            "video continuation",
            "audio reuse",
            "audio reference",
        ):
            self.assertNotIn(f"[{official_task}]", summary)

    def test_current_graph_preserves_denotation_scope_and_detects_marker_mutation(self) -> None:
        plan = full_plan()
        source = render_full_reference_prompt(plan).text
        graph = parse_semantic_prompt(
            source,
            plan.request.profile,
            TaskMode.REF2VA,
            SemanticSourceKind.LOCAL,
            "local.m24.current",
        )

        self.assertEqual(graph.schema, SEMANTIC_GRAPH_SCHEMA)
        scoped = {
            (relation.value, relation.target_id): relation.retention_scope.value
            for relation in graph.relations
            if relation.retention_scope is not None
        }
        self.assertEqual(scoped[("fully_preserved", "entity.subject.1")], "subject")
        self.assertEqual(scoped[("partially_copy", "asset.audio.1")], "audio_layer")
        wire_relations = cast(list[dict[str, object]], graph.to_wire()["relations"])
        self.assertTrue(all("retention_scope" in relation for relation in wire_relations))
        self.assertEqual(validate_current_semantic_graph_wire(graph.to_wire()), graph)

        changed = parse_semantic_prompt(
            source.replace("fully_preserved", "partially_preserved", 1),
            plan.request.profile,
            TaskMode.REF2VA,
            SemanticSourceKind.LOCAL,
            "local.m24.marker-mutation",
        )
        comparison = compare_semantic_graphs(changed, graph)
        self.assertEqual(comparison.primary_outcome, SemanticDiffOutcome.HARD_MUTATION)

        wrong_domain = parse_semantic_prompt(
            source.replace("fully_preserved", "fully_copy", 1),
            plan.request.profile,
            TaskMode.REF2VA,
            SemanticSourceKind.LOCAL,
            "local.m24.wrong-marker-domain",
        )
        self.assertIn("invalid.retention_marker_domain", wrong_domain.unknowns)
        with self.assertRaisesRegex(SemanticGraphError, "not current authority"):
            validate_current_semantic_graph_wire(wrong_domain.to_wire())

    def test_video_retention_uses_official_markers_and_human_denotation(self) -> None:
        plan = full_plan()
        for marker in (
            "fully_preserved",
            "partially_preserved",
            "attribute_transfer",
            "weak_reference",
        ):
            with self.subTest(marker=marker):
                relation = replace(
                    plan.intent_graph.retention[0],
                    source_asset_ids=("video_source",),
                    target_id="video_source",
                    scope=RetentionScope.VIDEO_STRUCTURE,
                    marker=VisualRetentionMarker(marker),
                )
                candidate = replace(
                    plan, intent_graph=replace(plan.intent_graph, retention=(relation,))
                )
                text = render_full_reference_prompt(candidate).text
                self.assertIn(f"<Video 1>: {marker} -", text)
                self.assertIn("defined video structure role", text)
                self.assertNotIn("video_structure", text)
                graph = parse_semantic_prompt(
                    text,
                    plan.request.profile,
                    TaskMode.REF2VA,
                    SemanticSourceKind.LOCAL,
                    "local.m24.video-markers",
                )
                retained = [r for r in graph.relations if r.retention_scope is not None]
                self.assertEqual(len(retained), 1)
                assert retained[0].retention_scope is not None
                self.assertEqual(retained[0].retention_scope.value, "video_structure")
                self.assertEqual(retained[0].value, marker)

    def test_complete_final_track_and_layer_copy_are_distinct(self) -> None:
        plan = full_plan()
        audio_relation = replace(
            plan.intent_graph.retention[1],
            marker=AudioRetentionMarker.FULLY_COPY,
            scope=RetentionScope.COMPLETE_FINAL_AUDIO_TRACK,
        )
        segments = tuple(
            replace(
                segment, audio_ids=tuple(value for value in segment.audio_ids if value == "audio_1")
            )
            for segment in plan.intent_graph.segments
        )
        graph = replace(
            plan.intent_graph,
            audios=(plan.intent_graph.audios[0],),
            retention=(plan.intent_graph.retention[0], audio_relation),
            segments=segments,
        )
        final_track = render_full_reference_prompt(replace(plan, intent_graph=graph)).text
        self.assertIn(
            "<Audio 1>: fully_copy - <Audio 1> is reused 1:1 as the target video's complete "
            "final audio track.",
            final_track,
        )
        self.assertNotIn("partially_copy", final_track)

        with self.assertRaisesRegex(PromptRenderingError, "sole unchanged audio contribution"):
            render_full_reference_prompt(
                replace(
                    plan,
                    intent_graph=replace(
                        plan.intent_graph,
                        retention=(plan.intent_graph.retention[0], audio_relation),
                    ),
                )
            )

        with self.assertRaisesRegex(PromptRenderingError, "audio-layer scope"):
            render_full_reference_prompt(
                replace(
                    plan,
                    intent_graph=replace(
                        plan.intent_graph,
                        retention=(
                            plan.intent_graph.retention[0],
                            replace(
                                plan.intent_graph.retention[1],
                                marker=AudioRetentionMarker.FULLY_COPY,
                            ),
                        ),
                    ),
                )
            )
        with self.assertRaisesRegex(PromptRenderingError, "complete final-track scope"):
            render_full_reference_prompt(
                replace(
                    plan,
                    intent_graph=replace(
                        plan.intent_graph,
                        retention=(
                            plan.intent_graph.retention[0],
                            replace(
                                plan.intent_graph.retention[1],
                                scope=RetentionScope.COMPLETE_FINAL_AUDIO_TRACK,
                            ),
                        ),
                    ),
                )
            )

    def test_legacy_rows_remain_inspectable_but_cannot_authorize_current(self) -> None:
        plan = full_plan()
        legacy_graph = replace(
            plan.intent_graph,
            retention=tuple(
                replace(relation, scope=RetentionScope.UNSPECIFIED)
                for relation in plan.intent_graph.retention
            ),
        )
        source = render_full_reference_prompt(replace(plan, intent_graph=legacy_graph)).text
        current = parse_semantic_prompt(
            source,
            plan.request.profile,
            TaskMode.REF2VA,
            SemanticSourceKind.LOCAL,
            "local.m24.inspect-legacy",
        )
        self.assertIn("legacy.retention_scope_unspecified", current.unknowns)
        self.assertEqual(validate_semantic_graph_wire(current.to_wire()), current)
        with self.assertRaisesRegex(SemanticGraphError, "not current authority"):
            validate_current_semantic_graph_wire(current.to_wire())

        retained = parse_legacy_semantic_prompt_v1(
            source,
            plan.request.profile,
            TaskMode.REF2VA,
            SemanticSourceKind.LOCAL,
            "local.m24.retained-v1",
        )
        self.assertEqual(retained.schema, LEGACY_SEMANTIC_GRAPH_SCHEMA)
        retained_wire = retained.to_wire()
        retained_relations = cast(list[dict[str, object]], retained_wire["relations"])
        self.assertTrue(all("retention_scope" not in relation for relation in retained_relations))
        self.assertEqual(validate_semantic_graph_wire(retained_wire), retained)
        with self.assertRaisesRegex(SemanticGraphError, "legacy semantic graph"):
            validate_current_semantic_graph_wire(retained_wire)


if __name__ == "__main__":
    unittest.main()
