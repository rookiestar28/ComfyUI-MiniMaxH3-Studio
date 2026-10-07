"""M12-01 source-clock audio extraction and benchmark contract tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from comfyui_h3_context.core import (
    AudioCapability,
    AudioDisposition,
    AudioPreprocessError,
    AudioPreprocessStatus,
    AudioRoute,
    AudioSourceSpan,
    PresentationTimestamp,
    build_audio_preprocess_abstention,
    build_default_audio_benchmark_plan,
    execute_audio_preprocess,
)
from scripts.m12_01_audio_extraction_fixture import _document, _request, run

ROOT = Path(__file__).resolve().parents[1]


def _pts(ticks: int) -> PresentationTimestamp:
    return PresentationTimestamp(ticks, 1, 1000)


class AudioPerceptionBenchmarkTests(unittest.TestCase):
    def test_schema_fixture_and_capability_coverage_are_frozen(self) -> None:
        schema = json.loads(
            (
                ROOT / "governance" / "contracts" / "audio_perception_benchmark_v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["$id"],
            "comfyui-h3-context://contracts/audio_perception_benchmark_v1.schema.json",
        )
        summary = run()
        self.assertEqual(summary["status"], AudioPreprocessStatus.COMPLETE.value)
        self.assertEqual(summary["case_count"], 5)
        self.assertEqual(summary["capability_count"], len(AudioCapability))
        self.assertEqual(summary["native_route"], "not_contacted")
        self.assertEqual(summary["ollama_route"], "not_contacted")
        self.assertEqual(summary["network"], "disabled")

    def test_source_pts_rate_channels_gaps_and_overlap_are_preserved(self) -> None:
        document = _document(_request())
        self.assertEqual(document.status, AudioPreprocessStatus.COMPLETE)
        self.assertIsNotNone(document.receipt)
        assert document.receipt is not None
        self.assertEqual(document.receipt.route, AudioRoute.STATIC_INJECTED)
        self.assertGreaterEqual(len(document.gaps), 1)
        self.assertTrue(any(segment.overlap_group for segment in document.segments))
        self.assertEqual(document.segments[0].start, _pts(0))
        self.assertEqual(document.segments[0].sample_rate, 48_000)
        self.assertEqual(document.segments[0].channels, 2)

    def test_video_audio_ownership_is_explicit(self) -> None:
        request = _request()
        self.assertEqual(request.paired_video_asset_id, "video_a")
        self.assertEqual(request.source_id, "source.video_audio")
        self.assertTrue(
            all(segment.asset_id == request.asset_id for segment in _document(request).segments)
        )

    def test_document_rejects_source_mismatch_and_non_monotone_output(self) -> None:
        request = _request()
        document = _document(request)
        bad_span = replace(document.segments[0].span, source_fingerprint="sha256:" + "b" * 64)
        bad_source = replace(document.segments[0], span=bad_span)
        with self.assertRaises(AudioPreprocessError):
            replace(document, segments=(bad_source, *document.segments[1:]))
        reversed_segments = tuple(reversed(document.segments))
        with self.assertRaises(AudioPreprocessError):
            replace(document, segments=reversed_segments)

    def test_gap_and_segment_endpoints_require_one_clock(self) -> None:
        interval = AudioSourceSpan(
            "audio.a",
            "source.a",
            "sha256:" + "a" * 64,
            _pts(100),
            _pts(200),
        )
        self.assertEqual(interval.end, _pts(200))
        with self.assertRaises(AudioPreprocessError):
            AudioSourceSpan(
                "audio.a",
                "source.a",
                "sha256:" + "a" * 64,
                _pts(100),
                PresentationTimestamp(200, 2, 1000),
            )

    def test_terminal_corrupt_and_cancelled_documents_cannot_be_complete(self) -> None:
        request = _request()
        for status in (AudioPreprocessStatus.CORRUPT, AudioPreprocessStatus.CANCELLED):
            document = build_audio_preprocess_abstention(request, status, "fixture_terminal")
            self.assertFalse(document.complete)
            self.assertIsNone(document.receipt)
            self.assertEqual(document.segments, ())

    def test_no_hidden_fallback_or_ollama_contact(self) -> None:
        plan = build_default_audio_benchmark_plan()
        self.assertFalse(plan.routing.automatic_fallback)
        self.assertEqual(plan.executable_candidate_ids, ())
        self.assertEqual(
            {candidate.disposition for candidate in plan.candidates},
            {AudioDisposition.UNAVAILABLE, AudioDisposition.UNSUPPORTED},
        )
        summary = run()
        self.assertEqual(summary["native_route"], "not_contacted")
        self.assertEqual(summary["ollama_route"], "not_contacted")

    def test_injected_adapter_cancellation_is_terminal(self) -> None:
        request = _request()

        class Cancelled:
            def is_cancelled(self) -> bool:
                return True

        with self.assertRaises(AudioPreprocessError):
            execute_audio_preprocess(
                lambda current: _document(current),
                request,
                cancellation_probe=Cancelled(),
            )

    def test_unsafe_metadata_and_rate_limit_fail_closed(self) -> None:
        with self.assertRaises(AudioPreprocessError):
            replace(_request(), source_id="https://private.example/audio")
        segment = _document(_request()).segments[0]
        with self.assertRaises(AudioPreprocessError):
            replace(segment, sample_rate=192_000)


if __name__ == "__main__":
    unittest.main()
