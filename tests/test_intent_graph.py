"""M1-06 intent graph and audiovisual timeline contract tests."""

from __future__ import annotations

import ast
import json
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import TypedDict, cast

from comfyui_h3_context.core import (
    AssetRole,
    ContractValidationError,
    MediaKind,
)
from comfyui_h3_context.core.constraints import TimePoint
from comfyui_h3_context.core.intent_graph import (
    AudioIntent,
    AudioLayer,
    AudioRetentionMarker,
    CameraIntent,
    EventCopy,
    EventCopyMode,
    IntentAction,
    IntentScene,
    IntentSubject,
    RetentionDomain,
    RetentionRelation,
    StyleIntent,
    TimelineSegment,
    VisualRetentionMarker,
    build_intent_graph,
)
from comfyui_h3_context.core.registry import (
    MediaMetadata,
    ReferenceAsset,
    ReferenceRegistry,
    build_reference_registry,
)

ROOT = Path(__file__).resolve().parents[1]


class GraphInputs(TypedDict):
    effective_duration: TimePoint
    registry: ReferenceRegistry
    subjects: tuple[IntentSubject, ...]
    scenes: tuple[IntentScene, ...]
    actions: tuple[IntentAction, ...]
    cameras: tuple[CameraIntent, ...]
    styles: tuple[StyleIntent, ...]
    audios: tuple[AudioIntent, ...]
    events: tuple[EventCopy, ...]
    retention: tuple[RetentionRelation, ...]


def registry() -> ReferenceRegistry:
    return build_reference_registry(
        (
            ReferenceAsset(
                "asset_1",
                MediaKind.IMAGE,
                AssetRole.REFERENCE,
                1,
                MediaMetadata(width=640, height=480),
            ),
            ReferenceAsset(
                "asset_2",
                MediaKind.AUDIO,
                AssetRole.AUDIO_SOURCE,
                2,
                MediaMetadata(sample_rate=32000, channels=2),
            ),
        )
    )


def subject() -> IntentSubject:
    return IntentSubject("subject_1", "the woman", ("asset_1",), "the woman in a red coat")


def scene() -> IntentScene:
    return IntentScene("scene_1", "a quiet station", ("subject_1",), "style_1")


def action() -> IntentAction:
    return IntentAction("action_1", "walks toward the platform", ("subject_1",), "scene_1")


def camera() -> CameraIntent:
    return CameraIntent("camera_1", "slow forward tracking", "scene_1", ("subject_1",))


def style() -> StyleIntent:
    return StyleIntent("style_1", "cinematic live action")


def audio() -> AudioIntent:
    return AudioIntent(
        "audio_1",
        AudioLayer.DIEGETIC,
        "footsteps and station ambience",
        ("asset_2",),
        ("subject_1",),
    )


def event() -> EventCopy:
    return EventCopy(
        "event_1", RetentionDomain.AUDIO, "asset_2", "segment_1", EventCopyMode.REFERENCE
    )


def retention() -> RetentionRelation:
    return RetentionRelation(
        "retention_1",
        RetentionDomain.VISUAL,
        ("asset_1",),
        "subject_1",
        VisualRetentionMarker.FULLY_PRESERVED,
    )


def segment(start: str = "0", end: str = "5") -> TimelineSegment:
    return TimelineSegment(
        "segment_1",
        TimePoint.from_text(start),
        TimePoint.from_text(end),
        "scene_1",
        ("subject_1",),
        ("action_1",),
        "camera_1",
        "style_1",
        ("audio_1",),
        ("event_1",),
    )


class IntentGraphTests(unittest.TestCase):
    def test_valid_graph_preserves_typed_ownership_and_wire_order(self) -> None:
        result = build_intent_graph(
            effective_duration=TimePoint.from_text("5"),
            registry=registry(),
            subjects=(subject(),),
            scenes=(scene(),),
            actions=(action(),),
            cameras=(camera(),),
            styles=(style(),),
            audios=(audio(),),
            events=(event(),),
            retention=(retention(),),
            segments=(segment(),),
        )

        self.assertTrue(result.is_valid)
        assert result.graph is not None
        self.assertEqual(result.graph.segments[0].start.raw, "0")
        wire = result.graph.to_wire()
        segments = cast(list[dict[str, object]], wire["segments"])
        end = cast(dict[str, object], segments[0]["end"])
        self.assertEqual(end["raw"], "5")
        self.assertEqual(result.graph.subjects[0].source_asset_ids, ("asset_1",))

    def test_values_are_immutable_and_closed_audio_retention_domain(self) -> None:
        value = subject()
        with self.assertRaises(FrozenInstanceError):
            value.description = "changed"  # type: ignore[misc]
        self.assertEqual(AudioRetentionMarker.FULLY_COPY.value, "fully_copy")
        with self.assertRaises(ContractValidationError):
            RetentionRelation(
                "bad_audio_marker",
                RetentionDomain.VISUAL,
                ("asset_1",),
                "subject_1",
                AudioRetentionMarker.FULLY_COPY,
            )

    def test_unknown_nodes_assets_and_orphan_events_are_diagnostics(self) -> None:
        bad_subject = IntentSubject("subject_1", "the woman", ("missing_asset",), "unknown source")
        bad_scene = IntentScene("scene_1", "station", ("missing_subject",), "style_1")
        result = build_intent_graph(
            effective_duration=TimePoint.from_text("5"),
            registry=registry(),
            subjects=(bad_subject,),
            scenes=(bad_scene,),
            actions=(action(),),
            cameras=(camera(),),
            styles=(style(),),
            audios=(audio(),),
            events=(event(),),
            retention=(retention(),),
            segments=(segment(),),
        )
        self.assertFalse(result.is_valid)
        self.assertIsNone(result.graph)
        codes = [diagnostic.code for diagnostic in result.diagnostics]
        self.assertEqual(codes[:2], ["unknown_asset", "unknown_subject"])

    def test_timeline_overlap_gap_and_bounds_fail_deterministically(self) -> None:
        first = segment("0", "3")
        second = TimelineSegment(
            "segment_2",
            TimePoint.from_text("2"),
            TimePoint.from_text("5"),
            first.scene_id,
            first.subject_ids,
            first.action_ids,
            first.camera_id,
            first.style_id,
            first.audio_ids,
            first.event_ids,
        )
        common: GraphInputs = {
            "effective_duration": TimePoint.from_text("5"),
            "registry": registry(),
            "subjects": (subject(),),
            "scenes": (scene(),),
            "actions": (action(),),
            "cameras": (camera(),),
            "styles": (style(),),
            "audios": (audio(),),
            "events": (event(),),
            "retention": (retention(),),
        }
        overlap = build_intent_graph(segments=(first, second), **common)
        self.assertEqual(overlap.diagnostics[0].code, "timeline_overlap")

        gap_second = TimelineSegment(
            "segment_2",
            TimePoint.from_text("4"),
            TimePoint.from_text("5"),
            second.scene_id,
            second.subject_ids,
            second.action_ids,
            second.camera_id,
            second.style_id,
            second.audio_ids,
            second.event_ids,
        )
        gap = build_intent_graph(segments=(first, gap_second), **common)
        self.assertEqual(gap.diagnostics[0].code, "timeline_gap")
        allowed = build_intent_graph(segments=(first, gap_second), allow_gaps=True, **common)
        self.assertTrue(allowed.is_valid)

        out_of_bounds = build_intent_graph(segments=(segment("0", "6"),), **common)
        self.assertEqual(out_of_bounds.diagnostics[0].code, "timeline_out_of_bounds")

    def test_segment_reference_order_and_duration_edges_are_checked(self) -> None:
        bad_start = build_intent_graph(
            effective_duration=TimePoint.from_text("5"),
            registry=registry(),
            subjects=(subject(),),
            scenes=(scene(),),
            actions=(action(),),
            cameras=(camera(),),
            styles=(style(),),
            audios=(audio(),),
            events=(event(),),
            retention=(retention(),),
            segments=(segment("1", "5"),),
        )
        self.assertEqual(bad_start.diagnostics[0].code, "timeline_must_start_at_zero")
        bad_end = build_intent_graph(
            effective_duration=TimePoint.from_text("5"),
            registry=registry(),
            subjects=(subject(),),
            scenes=(scene(),),
            actions=(action(),),
            cameras=(camera(),),
            styles=(style(),),
            audios=(audio(),),
            events=(event(),),
            retention=(retention(),),
            segments=(segment("0", "4"),),
        )
        self.assertEqual(bad_end.diagnostics[0].code, "timeline_must_end_at_duration")

    def test_duplicate_ids_and_impossible_retention_fail_closed(self) -> None:
        duplicate = build_intent_graph(
            effective_duration=TimePoint.from_text("5"),
            registry=registry(),
            subjects=(subject(), subject()),
            scenes=(scene(),),
            actions=(action(),),
            cameras=(camera(),),
            styles=(style(),),
            audios=(audio(),),
            events=(event(),),
            retention=(retention(),),
            segments=(segment(),),
        )
        self.assertEqual(duplicate.diagnostics[0].code, "duplicate_subject_id")

        impossible = build_intent_graph(
            effective_duration=TimePoint.from_text("5"),
            registry=registry(),
            subjects=(subject(),),
            scenes=(scene(),),
            actions=(action(),),
            cameras=(camera(),),
            styles=(style(),),
            audios=(audio(),),
            events=(event(),),
            retention=(
                RetentionRelation(
                    "retention_1",
                    RetentionDomain.VISUAL,
                    ("asset_2",),
                    "subject_1",
                    VisualRetentionMarker.FULLY_PRESERVED,
                ),
            ),
            segments=(segment(),),
        )
        self.assertEqual(impossible.diagnostics[0].code, "retention_domain_mismatch")

    def test_json_wire_and_clean_import_boundary(self) -> None:
        result = build_intent_graph(
            effective_duration=TimePoint.from_text("5"),
            registry=registry(),
            subjects=(subject(),),
            scenes=(scene(),),
            actions=(action(),),
            cameras=(camera(),),
            styles=(style(),),
            audios=(audio(),),
            events=(event(),),
            retention=(retention(),),
            segments=(segment(),),
        )
        assert result.graph is not None
        self.assertIn("subject_1", json.dumps(result.graph.to_wire(), sort_keys=True))
        module = ast.parse(
            (ROOT / "comfyui_h3_context" / "core" / "intent_graph.py").read_text(encoding="utf-8")
        )
        optional_roots = {"comfy", "torch", "torchaudio", "requests", "httpx", "PIL", "numpy"}
        imported: set[str] = set()
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue(optional_roots.isdisjoint(imported))


if __name__ == "__main__":
    unittest.main()
