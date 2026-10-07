"""M12-02 source-owned ASR, language, timing, and uncertainty contracts."""

from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    ASRAlternative,
    ASRBenchmarkPlan,
    ASRCandidateFamily,
    ASRCapability,
    ASRDisposition,
    ASRRoute,
    ASRSegment,
    ASRStatus,
    ASRTextAuthority,
    ASRTextStatus,
    ASRUncertainty,
    ASRUncertaintyKind,
    ASRWord,
    AudioSourceSpan,
    PresentationTimestamp,
    build_asr_abstention,
    build_default_asr_benchmark_plan,
    execute_asr,
)
from comfyui_h3_context.core.errors import ASRPerceptionError
from scripts.m12_02_asr_fixture import _document, _request, run

ROOT = Path(__file__).resolve().parents[1]


def span(start: int, end: int, *, source_id: str = "source.video_audio") -> AudioSourceSpan:
    fingerprint = "sha256:" + "a" * 64
    return AudioSourceSpan(
        "video_audio_a",
        source_id,
        fingerprint,
        PresentationTimestamp(start, 1, 1000),
        PresentationTimestamp(end, 1, 1000),
    )


class ASRPerceptionTests(unittest.TestCase):
    def test_schema_fixture_and_benchmark_are_frozen(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "asr_perception_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/asr_perception_v1.schema.json"
        )
        summary = run()
        benchmark = cast(dict[str, object], summary["benchmark"])
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(benchmark["case_count"], 6)
        self.assertEqual(benchmark["capability_count"], len(ASRCapability))
        self.assertEqual(summary["empty_status"], "empty")
        self.assertEqual(summary["cancelled"], "cancelled")
        self.assertEqual(summary["ollama_route"], "not_contacted")

    def test_segments_and_words_preserve_source_pts_and_fingerprints(self) -> None:
        document = _document(_request())
        self.assertIs(document.status, ASRStatus.COMPLETE)
        self.assertEqual(document.segments[0].span.start.ticks, 0)
        self.assertEqual(document.segments[0].words[0].span.end.ticks, 900)
        self.assertEqual(document.segments[0].span.source_fingerprint, "sha256:" + "a" * 64)
        self.assertEqual(
            cast(ASRTextAuthority, document.segments[0].authority).value,
            "observed_asr",
        )

    def test_language_confidence_alternatives_and_unclear_are_explicit(self) -> None:
        document = _document(_request())
        multilingual = next(item for item in document.segments if item.language == "zh-Hant")
        self.assertEqual(multilingual.language_confidence, Decimal("0.88"))
        self.assertTrue(multilingual.alternatives)
        unclear = next(item for item in document.segments if item.segment_id == "asr.music.abstain")
        self.assertIs(unclear.text_status, ASRTextStatus.UNCLEAR)
        self.assertTrue(
            any(item.kind is ASRUncertaintyKind.MUSIC_MASKING for item in unclear.uncertainties)
        )
        self.assertEqual(unclear.words, ())

    def test_overlap_requires_explicit_group_and_retains_both_sources(self) -> None:
        document = _document(_request())
        overlapping = [item for item in document.segments if item.overlap_group == "overlap.1"]
        self.assertEqual(len(overlapping), 2)
        self.assertEqual({item.span.start.ticks for item in overlapping}, {5000})
        with self.assertRaises(ASRPerceptionError):
            ASRSegment(
                "bad.overlap",
                span(5000, 5500),
                "invented",
                ASRTextStatus.TRANSCRIBED,
                "en-US",
                Decimal("0.9"),
                Decimal("0.9"),
                (),
                (),
                (),
            )

    def test_exact_dialogue_authority_cannot_be_changed_or_fabricated(self) -> None:
        document = _document(_request())
        self.assertTrue(
            all(
                cast(ASRTextAuthority, item.authority).value == "observed_asr"
                for item in document.segments
            )
        )
        with self.assertRaises(ASRPerceptionError):
            ASRSegment(
                "bad.authority",
                span(0, 500),
                "user exact line",
                ASRTextStatus.TRANSCRIBED,
                "en-US",
                Decimal("1.0"),
                Decimal("1.0"),
                (),
                (),
                (),
                authority="user_exact",
            )

    def test_source_and_time_base_mismatch_is_rejected(self) -> None:
        request = _request()
        document = _document(request)
        first = document.segments[0]
        with self.assertRaises(ASRPerceptionError):
            ASRSegment(
                "bad.clock",
                first.span,
                "bad clock",
                ASRTextStatus.TRANSCRIBED,
                "en-US",
                Decimal("0.5"),
                Decimal("0.5"),
                words=(
                    ASRWord(
                        "bad.word",
                        AudioSourceSpan(
                            "video_audio_a",
                            "source.video_audio",
                            "sha256:" + "a" * 64,
                            PresentationTimestamp(0, 1, 900),
                            PresentationTimestamp(100, 1, 900),
                        ),
                        "bad",
                        Decimal("0.5"),
                    ),
                ),
            )
        self.assertEqual(first.span.start.time_base_den, 1000)

    def test_terminal_empty_corrupt_and_cancelled_outputs_cannot_carry_hypotheses(self) -> None:
        request = _request()
        for status in (ASRStatus.EMPTY, ASRStatus.CORRUPT, ASRStatus.CANCELLED):
            result = build_asr_abstention(request, status, f"asr_{status.value}")
            self.assertFalse(result.complete)
            self.assertEqual(result.segments, ())
            self.assertIsNone(result.receipt)

    def test_execute_asr_checks_cancellation_and_no_fallback(self) -> None:
        request = _request()
        cancelled = type("Cancelled", (), {"is_cancelled": lambda self: True})()
        with self.assertRaises(ASRPerceptionError):
            execute_asr(lambda _: _document(request), request, cancellation_probe=cancelled)
        self.assertEqual(request.route, ASRRoute.STATIC_INJECTED)
        self.assertEqual(
            build_default_asr_benchmark_plan().routing.automatic_fallback,
            False,
        )

    def test_unsafe_text_and_bounds_fail_closed(self) -> None:
        with self.assertRaises(ASRPerceptionError):
            ASRAlternative("https://secret", Decimal("0.2"), 1)
        with self.assertRaises(ASRPerceptionError):
            ASRUncertainty(ASRUncertaintyKind.LOW_CONFIDENCE, "\x00unsafe")
        with self.assertRaises(ASRPerceptionError):
            ASRWord("bad.word", span(0, 500), "[unclear]", Decimal("0.5"))

    def test_plan_has_all_candidates_and_no_executable_profile(self) -> None:
        plan: ASRBenchmarkPlan = build_default_asr_benchmark_plan()
        self.assertEqual(
            {cast(ASRCandidateFamily, item.family).value for item in plan.candidates},
            {"comfyui_native", "ollama", "specialist"},
        )
        self.assertEqual(plan.executable_candidate_ids, ())
        self.assertEqual(
            {item.disposition for item in plan.candidates},
            {ASRDisposition.UNAVAILABLE, ASRDisposition.UNSUPPORTED},
        )


if __name__ == "__main__":
    unittest.main()
