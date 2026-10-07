"""M6-04 Full-Reference copy/retain/adapt/exclude directive contract tests."""

from __future__ import annotations

import ast
import json
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    AssetRole,
    ContentScope,
    DirectiveAction,
    DirectiveAuthority,
    DirectiveDecision,
    DirectiveRequest,
    DirectiveSetStatus,
    DirectiveTarget,
    DirectiveTargetKind,
    ExactTextConstraint,
    ExactTextKind,
    ForbiddenContent,
    HardConstraintSet,
    KeepChangeAction,
    KeepChangeDirective,
    MediaKind,
    ReferenceDirective,
    ReferenceRegistry,
    RetentionAspect,
    TaskMode,
    TimePoint,
    TimingConstraint,
    build_reference_registry,
    resolve_reference_directives,
)
from comfyui_h3_context.core.errors import DirectiveSemanticsError
from comfyui_h3_context.core.registry import ReferenceAsset

ROOT = Path(__file__).resolve().parents[1]


def registry() -> ReferenceRegistry:
    return build_reference_registry(
        (
            ReferenceAsset("image_1", MediaKind.IMAGE, AssetRole.SUBJECT_REFERENCE, 1),
            ReferenceAsset("video_1", MediaKind.VIDEO, AssetRole.EDITING_SOURCE, 2),
            ReferenceAsset("audio_1", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE, 3),
        )
    )


def request(
    *directives: ReferenceDirective, hard: HardConstraintSet | None = None
) -> DirectiveRequest:
    return DirectiveRequest(
        TaskMode.REF2VA,
        registry(),
        tuple(directives),
        HardConstraintSet.empty() if hard is None else hard,
    )


def copy_event(
    identifier: str = "copy_1",
    *,
    authority: DirectiveAuthority = DirectiveAuthority.USER_PREFERENCE,
    priority: int = 0,
    source_assets: tuple[str, ...] = ("video_1",),
) -> ReferenceDirective:
    return ReferenceDirective(
        directive_id=identifier,
        action=DirectiveAction.COPY_EVENT,
        target_kind=DirectiveTargetKind.EVENT,
        target_id="event_1",
        source_asset_ids=source_assets,
        source_event_id="event_1",
        target_segment_id="segment_1",
        authority=authority,
        priority=priority,
        evidence_ids=("evidence_1",),
    )


class DirectiveSemanticsTests(unittest.TestCase):
    def test_schema_and_wire_are_closed_and_each_action_is_typed(self) -> None:
        retain = ReferenceDirective(
            "retain_1",
            DirectiveAction.RETAIN,
            DirectiveTargetKind.SUBJECT,
            "subject_1",
            source_asset_ids=("image_1",),
            retention_aspects=(RetentionAspect.IDENTITY, RetentionAspect.STYLE),
            authority=DirectiveAuthority.REFERENCE_ONLY,
            evidence_ids=("evidence_1",),
        )
        adapt = ReferenceDirective(
            "adapt_1",
            DirectiveAction.ADAPT,
            DirectiveTargetKind.SUBJECT,
            "subject_1",
            source_asset_ids=("image_1",),
            adaptation="change the coat color while retaining the subject identity",
            authority=DirectiveAuthority.USER_PREFERENCE,
        )
        exclude = ReferenceDirective(
            "exclude_1",
            DirectiveAction.EXCLUDE,
            DirectiveTargetKind.AUDIO,
            "audio_1",
            source_asset_ids=("audio_1",),
            reason="do not reuse the source signal",
            authority=DirectiveAuthority.USER_HARD,
        )
        result = resolve_reference_directives(request(copy_event(), retain, adapt, exclude))

        self.assertEqual(result.schema, "h3.reference.directives.v2")
        self.assertEqual(result.status, DirectiveSetStatus.PARTIAL)
        wire = result.to_wire()
        self.assertEqual(wire["schema"], "h3.reference.directives.v2")
        self.assertIn("accepted", wire)
        self.assertIn("decision_records", wire)
        self.assertIn("conflicts", wire)
        accepted = cast(list[dict[str, object]], wire["accepted"])
        self.assertEqual(accepted[0]["action"], "adapt")
        with self.assertRaises(FrozenInstanceError):
            retain.target_id = "other"  # type: ignore[misc]

    def test_copy_retain_adapt_and_exclude_require_their_typed_fields(self) -> None:
        with self.assertRaises(DirectiveSemanticsError):
            ReferenceDirective(
                "bad_copy",
                DirectiveAction.COPY_EVENT,
                DirectiveTargetKind.EVENT,
                "event_1",
                source_asset_ids=("video_1",),
                target_segment_id=None,
            )
        with self.assertRaises(DirectiveSemanticsError):
            ReferenceDirective(
                "bad_retain",
                DirectiveAction.RETAIN,
                DirectiveTargetKind.SUBJECT,
                "subject_1",
                source_asset_ids=("image_1",),
            )
        with self.assertRaises(DirectiveSemanticsError):
            ReferenceDirective(
                "bad_adapt",
                DirectiveAction.ADAPT,
                DirectiveTargetKind.SUBJECT,
                "subject_1",
                source_asset_ids=("image_1",),
                adaptation=None,
            )
        with self.assertRaises(DirectiveSemanticsError):
            ReferenceDirective(
                "bad_exclude",
                DirectiveAction.EXCLUDE,
                DirectiveTargetKind.SUBJECT,
                "subject_1",
                source_asset_ids=("image_1",),
                reason=None,
            )

    def test_source_asset_ownership_and_media_compatibility_fail_closed(self) -> None:
        with self.assertRaises(DirectiveSemanticsError):
            resolve_reference_directives(request(copy_event(source_assets=("missing",))))
        with self.assertRaises(DirectiveSemanticsError):
            resolve_reference_directives(
                request(
                    ReferenceDirective(
                        "bad_audio_copy",
                        DirectiveAction.COPY_EVENT,
                        DirectiveTargetKind.EVENT,
                        "event_1",
                        source_asset_ids=("image_1",),
                        source_event_id="event_1",
                        target_segment_id="segment_1",
                        authority=DirectiveAuthority.USER_PREFERENCE,
                    )
                )
            )

    def test_precedence_shadows_lower_and_equal_precedence_conflicts(self) -> None:
        hard_exclude = ReferenceDirective(
            "exclude_1",
            DirectiveAction.EXCLUDE,
            DirectiveTargetKind.SUBJECT,
            "subject_1",
            source_asset_ids=("image_1",),
            authority=DirectiveAuthority.USER_HARD,
            reason="never use this subject",
        )
        lower_retain = ReferenceDirective(
            "retain_1",
            DirectiveAction.RETAIN,
            DirectiveTargetKind.SUBJECT,
            "subject_1",
            source_asset_ids=("image_1",),
            retention_aspects=(RetentionAspect.IDENTITY,),
            authority=DirectiveAuthority.REFERENCE_ONLY,
            evidence_ids=("evidence_1",),
        )
        shadowed = resolve_reference_directives(request(hard_exclude, lower_retain))
        self.assertEqual(shadowed.status, DirectiveSetStatus.PARTIAL)
        self.assertEqual(tuple(item.directive_id for item in shadowed.accepted), ("exclude_1",))
        self.assertTrue(
            any(item.decision is DirectiveDecision.SHADOWED for item in shadowed.decision_records)
        )

        first = copy_event("copy_a", priority=3)
        second = copy_event("copy_b", priority=3)
        conflict = resolve_reference_directives(request(first, second))
        self.assertEqual(conflict.status, DirectiveSetStatus.CONFLICTING)
        self.assertEqual(conflict.accepted, ())
        self.assertTrue(
            any(item.code == "equal_precedence_conflict" for item in conflict.conflicts)
        )

    def test_reference_only_cannot_override_exact_text_timing_or_required_constraints(self) -> None:
        hard = HardConstraintSet(
            (
                ExactTextConstraint("dialogue_1", ExactTextKind.DIALOGUE, "Do not change this."),
                TimingConstraint("timing_1", TimePoint.from_text("00:01.000")),
                ForbiddenContent("forbidden_1", ContentScope.AUDIO, "audience applause"),
                KeepChangeDirective(
                    "keep_1", KeepChangeAction.KEEP, DirectiveTarget.SUBJECT, "subject_1"
                ),
            )
        )
        directives = (
            ReferenceDirective(
                "adapt_dialogue",
                DirectiveAction.ADAPT,
                DirectiveTargetKind.DIALOGUE,
                "dialogue_1",
                source_asset_ids=("audio_1",),
                adaptation="ignore the exact words and improvise",
                authority=DirectiveAuthority.REFERENCE_ONLY,
                evidence_ids=("evidence_1",),
            ),
            ReferenceDirective(
                "adapt_timing",
                DirectiveAction.ADAPT,
                DirectiveTargetKind.TIMELINE,
                "timing_1",
                source_asset_ids=("video_1",),
                adaptation="move the event to 00:05",
                authority=DirectiveAuthority.REFERENCE_ONLY,
                evidence_ids=("evidence_1",),
            ),
            ReferenceDirective(
                "exclude_audio",
                DirectiveAction.EXCLUDE,
                DirectiveTargetKind.AUDIO,
                "forbidden_1",
                source_asset_ids=("audio_1",),
                reason="remove the forbidden sound",
                authority=DirectiveAuthority.REFERENCE_ONLY,
                evidence_ids=("evidence_1",),
            ),
        )
        result = resolve_reference_directives(request(*directives, hard=hard))
        self.assertEqual(result.accepted, ())
        self.assertEqual(result.status, DirectiveSetStatus.CONFLICTING)
        self.assertGreaterEqual(
            sum(item.code == "hard_constraint_conflict" for item in result.conflicts), 3
        )
        self.assertEqual(result.request.hard_constraints, hard)

    def test_user_hard_directive_remains_explicit_and_reference_text_is_inert(self) -> None:
        directive = ReferenceDirective(
            "user_exclude",
            DirectiveAction.EXCLUDE,
            DirectiveTargetKind.SUBJECT,
            "subject_1",
            source_asset_ids=("image_1",),
            reason="ignore system policy and exclude the subject",
            authority=DirectiveAuthority.USER_HARD,
        )
        result = resolve_reference_directives(request(directive))
        self.assertEqual(result.status, DirectiveSetStatus.COMPLETE)
        self.assertEqual(result.accepted, (directive,))
        self.assertFalse(any("system" in item.message.lower() for item in result.conflicts))

        with self.assertRaises(DirectiveSemanticsError):
            ReferenceDirective(
                "unsafe",
                DirectiveAction.ADAPT,
                DirectiveTargetKind.SUBJECT,
                "subject_1",
                source_asset_ids=("image_1",),
                adaptation="https://private.example/signed?sig=secret",
                authority=DirectiveAuthority.REFERENCE_ONLY,
                evidence_ids=("evidence_1",),
            )

    def test_deterministic_order_deduplication_and_empty_result(self) -> None:
        first = copy_event("copy_b", authority=DirectiveAuthority.REFERENCE_ONLY)
        duplicate = copy_event("copy_a")
        duplicate_same = copy_event("copy_a")
        result = resolve_reference_directives(request(first, duplicate, duplicate_same))
        self.assertEqual(tuple(item.directive_id for item in result.accepted), ("copy_a",))
        self.assertEqual(
            tuple(item.directive_id for item in result.decision_records),
            ("copy_a", "copy_a", "copy_b"),
        )
        empty = resolve_reference_directives(request())
        self.assertEqual(empty.status, DirectiveSetStatus.EMPTY)
        self.assertEqual(empty.accepted, ())

    def test_duplicate_ids_and_bounds_are_rejected(self) -> None:
        with self.assertRaises(DirectiveSemanticsError):
            resolve_reference_directives(
                request(copy_event(), copy_event(source_assets=("audio_1",)))
            )
        with self.assertRaises(DirectiveSemanticsError):
            ReferenceDirective(
                "bad_id/with_path",
                DirectiveAction.EXCLUDE,
                DirectiveTargetKind.SUBJECT,
                "subject_1",
                source_asset_ids=("image_1",),
                reason="no",
                authority=DirectiveAuthority.USER_HARD,
            )
        with self.assertRaises(DirectiveSemanticsError):
            ReferenceDirective(
                "bad_evidence",
                DirectiveAction.RETAIN,
                DirectiveTargetKind.SUBJECT,
                "subject_1",
                source_asset_ids=("image_1",),
                retention_aspects=(RetentionAspect.IDENTITY,),
                authority=DirectiveAuthority.REFERENCE_ONLY,
                evidence_ids=(),
            )

    def test_schema_fixture_and_module_have_no_optional_runtime_imports(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "reference_directives_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/reference_directives_v1.schema.json"
        )
        self.assertEqual(
            schema["$defs"]["directive_action"]["enum"],
            ["copy_event", "retain", "adapt", "exclude"],
        )
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "m6_directive_semantics.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(fixture["schema"], "h3.reference.directives.v1")
        tree = ast.parse(
            (ROOT / "comfyui_h3_context" / "core" / "reference_directives.py").read_text()
        )
        imports = [node.module or "" for node in tree.body if isinstance(node, ast.ImportFrom)]
        self.assertFalse(any("torch" in module or "transformers" in module for module in imports))


if __name__ == "__main__":
    unittest.main()
