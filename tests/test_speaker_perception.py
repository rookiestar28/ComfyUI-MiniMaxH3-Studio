"""M12-03 source-owned speaker and voice-reference contracts."""

from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    AudioSourceSpan,
    PresentationTimestamp,
    SpeakerAssociation,
    SpeakerAssociationEvidence,
    SpeakerBenchmarkPlan,
    SpeakerCandidateFamily,
    SpeakerCapability,
    SpeakerDisposition,
    SpeakerDocument,
    SpeakerHypothesis,
    SpeakerReference,
    SpeakerRequest,
    SpeakerRetention,
    SpeakerRoute,
    SpeakerStatus,
    SpeakerTurn,
    SpeakerUncertaintyKind,
    build_default_speaker_benchmark_plan,
    build_speaker_abstention,
    execute_speaker,
)
from comfyui_h3_context.core.errors import SpeakerPerceptionError
from scripts.m12_03_speaker_fixture import _document, _request, run

ROOT = Path(__file__).resolve().parents[1]


class SpeakerPerceptionTests(unittest.TestCase):
    def test_schema_fixture_and_benchmark_are_frozen(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "speaker_perception_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"],
            "comfyui-h3-context://contracts/speaker_perception_v1.schema.json",
        )
        summary = run()
        benchmark = cast(dict[str, object], summary["benchmark"])
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(benchmark["case_count"], 5)
        self.assertEqual(benchmark["capability_count"], len(SpeakerCapability))
        self.assertEqual(summary["embedding_logged"], False)
        self.assertEqual(summary["ollama_route"], "not_contacted")

    def test_turns_retain_source_pts_and_explicit_overlap(self) -> None:
        document = _document(_request())
        self.assertIs(document.status, SpeakerStatus.COMPLETE)
        self.assertEqual(document.turns[0].span.start.ticks, 0)
        overlapping = [item for item in document.turns if item.overlap_group == "overlap.1"]
        self.assertEqual(len(overlapping), 2)
        self.assertEqual({item.speaker_label for item in overlapping}, {"speaker_a", "speaker_b"})
        self.assertEqual(document.turns[0].span.source_fingerprint, "sha256:" + "a" * 64)

    def test_unknown_and_ambiguous_voice_hypotheses_remain_visible(self) -> None:
        document = _document(_request())
        unknown = next(item for item in document.turns if item.speaker_label is None)
        self.assertTrue(
            any(
                item.kind is SpeakerUncertaintyKind.UNKNOWN_SPEAKER
                for item in unknown.uncertainties
            )
        )
        hypothesis = document.hypotheses[0]
        self.assertTrue(hypothesis.ambiguous)
        self.assertFalse(hypothesis.user_confirmed)
        self.assertGreater(hypothesis.false_link_risk_bps, 0)

    def test_embedding_requires_opt_in_retention_and_never_logged(self) -> None:
        request = _request()
        self.assertTrue(request.embedding_requested)
        self.assertTrue(request.biometric_opt_in)
        self.assertIs(request.retention, SpeakerRetention.EPHEMERAL)
        self.assertTrue(
            any(item.embedding_fingerprint is not None for item in _document(request).turns)
        )
        with self.assertRaises(SpeakerPerceptionError):
            SpeakerRequest(
                request.asset_id,
                request.source_id,
                request.source_fingerprint,
                request.preprocessing_fingerprint,
                request.duration_end,
                request.references,
                embedding_requested=True,
                biometric_opt_in=False,
            )
        with self.assertRaises(SpeakerPerceptionError):
            _document(request, embedding_logged=True)

    def test_visible_association_requires_av_evidence_or_user_selection(self) -> None:
        document = _document(_request())
        self.assertEqual(
            {item.evidence_kind for item in document.associations},
            {SpeakerAssociationEvidence.AV_EVIDENCE, SpeakerAssociationEvidence.USER_SELECTION},
        )
        with self.assertRaises(SpeakerPerceptionError):
            SpeakerAssociation(
                "bad.association",
                "speaker_a",
                "video_a",
                SpeakerAssociationEvidence.AV_EVIDENCE,
                Decimal("0.7"),
                video_source_fingerprint=None,
                evidence_fingerprint=None,
            )

    def test_cross_asset_reference_ownership_and_time_base_are_checked(self) -> None:
        document = _document(_request())
        reference = document.hypotheses[0].reference
        self.assertEqual(reference.asset_id, "voice_ref_a")
        with self.assertRaises(SpeakerPerceptionError):
            SpeakerHypothesis(
                "bad.reference",
                "speaker_a",
                SpeakerReference(
                    "voice_ref_a",
                    "source.voice_ref_a",
                    "sha256:" + "z" * 64,
                ),
                Decimal("0.5"),
                3000,
                True,
                False,
            )
        bad_turn = SpeakerTurn(
            "bad.clock",
            AudioSourceSpan(
                "audio_pair_a",
                "source.audio_pair",
                "sha256:" + "a" * 64,
                PresentationTimestamp(0, 1, 900),
                PresentationTimestamp(100, 1, 900),
            ),
            "speaker_a",
            Decimal("0.5"),
        )
        with self.assertRaises(SpeakerPerceptionError):
            SpeakerDocument(
                "bad.clock.document",
                SpeakerStatus.COMPLETE,
                _request(),
                (bad_turn,),
                receipt=document.receipt,
            )

    def test_terminal_abstentions_and_cancellation_have_no_output(self) -> None:
        request = _request()
        for status in (SpeakerStatus.EMPTY, SpeakerStatus.CORRUPT, SpeakerStatus.CANCELLED):
            result = build_speaker_abstention(request, status, f"speaker_{status.value}")
            self.assertFalse(result.complete)
            self.assertEqual(result.turns, ())
            self.assertIsNone(result.receipt)
        cancelled = type("Cancelled", (), {"is_cancelled": lambda self: True})()
        with self.assertRaises(SpeakerPerceptionError):
            execute_speaker(
                lambda current: _document(current), request, cancellation_probe=cancelled
            )
        self.assertEqual(request.route, SpeakerRoute.STATIC_INJECTED)

    def test_no_fallback_and_candidate_families_remain_separate(self) -> None:
        plan: SpeakerBenchmarkPlan = build_default_speaker_benchmark_plan()
        self.assertEqual(
            {cast(SpeakerCandidateFamily, item.family) for item in plan.candidates},
            set(SpeakerCandidateFamily),
        )
        self.assertEqual(plan.executable_candidate_ids, ())
        self.assertEqual(
            {cast(SpeakerDisposition, item.disposition) for item in plan.candidates},
            {SpeakerDisposition.UNAVAILABLE, SpeakerDisposition.UNSUPPORTED},
        )
        self.assertFalse(plan.routing.automatic_fallback)


if __name__ == "__main__":
    unittest.main()
