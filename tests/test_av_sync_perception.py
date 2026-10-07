"""M12-05 source-owned audiovisual synchronization contracts."""

from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    AVSyncBenchmarkPlan,
    AVSyncCandidateFamily,
    AVSyncCapability,
    AVSyncDisposition,
    AVSyncMediaKind,
    AVSyncOffsetInterval,
    AVSyncPerceptionError,
    AVSyncRelation,
    AVSyncRelationKind,
    AVSyncSourceRole,
    AVSyncSourceSpan,
    AVSyncStatus,
    AVSyncSupport,
    AVSyncUncertaintyKind,
    AVSyncVisibility,
    build_av_sync_abstention,
    build_default_av_sync_benchmark_plan,
    execute_av_sync,
)
from scripts.m12_05_av_sync_fixture import _document, _request, run

ROOT = Path(__file__).resolve().parents[1]


class AVSyncPerceptionTests(unittest.TestCase):
    def test_schema_fixture_and_benchmark_are_frozen(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "av_sync_perception_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"],
            "comfyui-h3-context://contracts/av_sync_perception_v1.schema.json",
        )
        summary = run()
        benchmark = cast(dict[str, object], summary["benchmark"])
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(benchmark["case_count"], 6)
        self.assertEqual(benchmark["capability_count"], len(AVSyncCapability))
        self.assertEqual(summary["ollama_route"], "not_contacted")

    def test_relations_preserve_pts_offset_intervals_and_grounding(self) -> None:
        document = _document(_request())
        self.assertIs(document.status, AVSyncStatus.COMPLETE)
        relation = document.relations[0]
        visual_span = cast(AVSyncSourceSpan, relation.visual_span)
        audio_span = cast(AVSyncSourceSpan, relation.audio_span)
        offset = cast(AVSyncOffsetInterval, relation.offset)
        self.assertEqual(visual_span.start.ticks, 500)
        self.assertEqual(audio_span.start.ticks, 500)
        self.assertEqual(offset.minimum_millis, -20)
        self.assertEqual(offset.maximum_millis, 20)
        self.assertIs(relation.kind, AVSyncRelationKind.SPEECH_LIP)
        self.assertIs(relation.support, AVSyncSupport.ALIGNED)

    def test_desync_cut_offscreen_dubbing_and_overlap_are_inspectable(self) -> None:
        document = _document(_request())
        kinds = {cast(AVSyncRelationKind, item.kind) for item in document.relations}
        self.assertEqual(kinds, set(AVSyncRelationKind))
        desync = [
            item for item in document.relations if item.support is AVSyncSupport.DESYNCHRONIZED
        ]
        self.assertTrue(desync)
        self.assertTrue(
            any(
                uncertainty.kind is AVSyncUncertaintyKind.DUBBING
                for item in desync
                for uncertainty in item.uncertainties
            )
        )
        self.assertTrue(
            any(
                cast(AVSyncVisibility, item.visibility) is AVSyncVisibility.OFF_SCREEN
                for item in document.relations
            )
        )
        self.assertTrue(any(item.overlap_group == "av.overlap.1" for item in document.relations))
        self.assertTrue(
            any(
                uncertainty.kind is AVSyncUncertaintyKind.CUT
                for item in document.relations
                for uncertainty in item.uncertainties
            )
        )

    def test_source_ownership_and_time_base_are_checked(self) -> None:
        request = _request()
        bad_relation = _document(request).relations[0]
        original_visual = cast(AVSyncSourceSpan, bad_relation.visual_span)
        foreign_visual = AVSyncSourceSpan(
            "foreign.video",
            "source.foreign.video",
            "sha256:" + "9" * 64,
            cast(AVSyncMediaKind, original_visual.media_kind),
            cast(AVSyncSourceRole, original_visual.role),
            original_visual.start,
            original_visual.end,
            original_visual.frame_ids,
            original_visual.shot_id,
        )
        with self.assertRaises(AVSyncPerceptionError):
            execute_av_sync(
                lambda current: _document(current, visual_override=foreign_visual),
                request,
            )

    def test_unknown_cannot_fabricate_aligned_offset(self) -> None:
        request = _request()
        with self.assertRaises(AVSyncPerceptionError):
            _document(request, invalid_unknown=True)
        with self.assertRaises(AVSyncPerceptionError):
            _document(request, invalid_aligned=True)

    def test_terminal_states_and_cancellation_emit_no_plausible_output(self) -> None:
        request = _request()
        for status in (
            AVSyncStatus.EMPTY,
            AVSyncStatus.CORRUPT,
            AVSyncStatus.CANCELLED,
        ):
            result = build_av_sync_abstention(request, status, f"sync_{status.value}")
            self.assertFalse(result.complete)
            self.assertEqual(result.relations, ())
            self.assertIsNone(result.receipt)
        cancelled = type("Cancelled", (), {"is_cancelled": lambda self: True})()
        with self.assertRaises(AVSyncPerceptionError):
            execute_av_sync(
                lambda current: _document(current), request, cancellation_probe=cancelled
            )

    def test_candidate_families_are_explicit_and_never_fallback(self) -> None:
        plan: AVSyncBenchmarkPlan = build_default_av_sync_benchmark_plan()
        self.assertEqual(
            {cast(AVSyncCandidateFamily, item.family) for item in plan.candidates},
            set(AVSyncCandidateFamily),
        )
        self.assertEqual(plan.executable_candidate_ids, ())
        self.assertFalse(plan.routing.automatic_fallback)
        self.assertEqual(
            {cast(AVSyncDisposition, item.disposition) for item in plan.candidates},
            {AVSyncDisposition.UNAVAILABLE, AVSyncDisposition.UNSUPPORTED},
        )

    def test_unsafe_relation_metadata_and_invalid_interval_fail_closed(self) -> None:
        document = _document(_request())
        relation = document.relations[0]
        with self.assertRaises(AVSyncPerceptionError):
            AVSyncRelation(
                "bad.relation",
                relation.kind,
                relation.support,
                relation.visual_span,
                relation.audio_span,
                relation.offset,
                Decimal("0.5"),
                Decimal("0.5"),
                relation.visibility,
                relation.uncertainties,
                relation.overlap_group,
                "https://unsafe",
            )


if __name__ == "__main__":
    unittest.main()
