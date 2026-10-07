"""M12-04 source-owned audio-event contracts."""

from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    AudioEvent,
    AudioEventBenchmarkPlan,
    AudioEventCandidateFamily,
    AudioEventCapability,
    AudioEventDisposition,
    AudioEventKind,
    AudioEventPerceptionError,
    AudioEventPresence,
    AudioEventReferenceSemantic,
    AudioEventStatus,
    AudioEventUncertainty,
    AudioEventUncertaintyKind,
    AudioSourceSpan,
    PresentationTimestamp,
    build_audio_event_abstention,
    build_default_audio_event_benchmark_plan,
    execute_audio_event,
)
from scripts.m12_04_audio_event_fixture import _document, _request, run

ROOT = Path(__file__).resolve().parents[1]


class AudioEventPerceptionTests(unittest.TestCase):
    def test_schema_fixture_and_benchmark_are_frozen(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "audio_event_perception_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"],
            "comfyui-h3-context://contracts/audio_event_perception_v1.schema.json",
        )
        summary = run()
        benchmark = cast(dict[str, object], summary["benchmark"])
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(benchmark["case_count"], 5)
        self.assertEqual(benchmark["capability_count"], len(AudioEventCapability))
        self.assertEqual(summary["unknown_event_count"], 1)
        self.assertEqual(summary["ollama_route"], "not_contacted")

    def test_events_preserve_pts_multilabel_and_boundary_confidence(self) -> None:
        document = _document(_request())
        self.assertIs(document.status, AudioEventStatus.COMPLETE)
        self.assertEqual(document.events[0].span.start.ticks, 0)
        self.assertEqual(document.events[0].span.source_fingerprint, "sha256:" + "a" * 64)
        self.assertGreaterEqual(
            cast(Decimal, document.events[0].boundary_confidence), Decimal("0.8")
        )
        kinds = {cast(AudioEventKind, item.kind) for item in document.events}
        self.assertTrue({AudioEventKind.SPEECH, AudioEventKind.MUSIC}.issubset(kinds))
        overlap = [item for item in document.events if item.overlap_group == "event.overlap.1"]
        self.assertGreaterEqual(len(overlap), 2)

    def test_reference_semantics_are_distinct_and_source_owned(self) -> None:
        document = _document(_request())
        semantics = {
            cast(AudioEventReferenceSemantic, item.reference_semantic)
            for item in document.events
            if item.reference_semantic is not None
        }
        self.assertEqual(
            semantics,
            {
                AudioEventReferenceSemantic.COPIED_AUDIO,
                AudioEventReferenceSemantic.REPERFORMED_AUDIO,
                AudioEventReferenceSemantic.TIMBRE_REFERENCE,
                AudioEventReferenceSemantic.AMBIENT_REFERENCE,
            },
        )
        with self.assertRaises(AudioEventPerceptionError):
            execute_audio_event(
                lambda request: _document(
                    request,
                    events=(
                        AudioEvent(
                            "bad.reference",
                            document.events[0].span,
                            AudioEventKind.MUSIC,
                            AudioEventPresence.PRESENT,
                            "music",
                            Decimal("0.8"),
                            Decimal("0.8"),
                            reference_semantic=AudioEventReferenceSemantic.COPIED_AUDIO,
                            reference_asset_id="unknown.reference",
                        ),
                    ),
                ),
                _request(),
            )

    def test_unknown_event_cannot_fabricate_a_label(self) -> None:
        document = _document(_request())
        unknown = next(
            item for item in document.events if item.presence is AudioEventPresence.UNKNOWN
        )
        self.assertIsNone(unknown.label)
        self.assertTrue(
            any(item.kind is AudioEventUncertaintyKind.UNKNOWN for item in unknown.uncertainties)
        )
        with self.assertRaises(AudioEventPerceptionError):
            AudioEvent(
                "bad.unknown",
                unknown.span,
                AudioEventKind.UNKNOWN,
                AudioEventPresence.UNKNOWN,
                "invented event",
                Decimal("0.2"),
                Decimal("0.2"),
                uncertainties=(
                    AudioEventUncertainty(AudioEventUncertaintyKind.UNKNOWN, "uncertain"),
                ),
            )

    def test_absent_and_terminal_states_carry_no_plausible_output(self) -> None:
        request = _request()
        for status in (
            AudioEventStatus.EMPTY,
            AudioEventStatus.CORRUPT,
            AudioEventStatus.CANCELLED,
        ):
            result = build_audio_event_abstention(request, status, f"event_{status.value}")
            self.assertFalse(result.complete)
            self.assertEqual(result.events, ())
            self.assertIsNone(result.receipt)
        document = _document(request)
        absent = next(
            item for item in document.events if item.presence is AudioEventPresence.ABSENT
        )
        self.assertIsNone(absent.label)

    def test_request_ownership_and_time_base_are_checked(self) -> None:
        request = _request()
        bad_event = AudioEvent(
            "bad.clock",
            AudioSourceSpan(
                "audio_events_a",
                "source.audio_events",
                "sha256:" + "a" * 64,
                PresentationTimestamp(0, 1, 900),
                PresentationTimestamp(100, 1, 900),
            ),
            AudioEventKind.SPEECH,
            AudioEventPresence.PRESENT,
            "speech",
            Decimal("0.5"),
            Decimal("0.5"),
        )
        with self.assertRaises(AudioEventPerceptionError):
            execute_audio_event(lambda _: _document(request, events=(bad_event,)), request)

    def test_cancellation_and_candidate_families_remain_explicit(self) -> None:
        request = _request()
        cancelled = type("Cancelled", (), {"is_cancelled": lambda self: True})()
        with self.assertRaises(AudioEventPerceptionError):
            execute_audio_event(lambda _: _document(request), request, cancellation_probe=cancelled)
        plan: AudioEventBenchmarkPlan = build_default_audio_event_benchmark_plan()
        self.assertEqual(
            {cast(AudioEventCandidateFamily, item.family) for item in plan.candidates},
            set(AudioEventCandidateFamily),
        )
        self.assertEqual(plan.executable_candidate_ids, ())
        self.assertFalse(plan.routing.automatic_fallback)
        self.assertEqual(
            {cast(AudioEventDisposition, item.disposition) for item in plan.candidates},
            {AudioEventDisposition.UNAVAILABLE, AudioEventDisposition.UNSUPPORTED},
        )

    def test_unsafe_labels_and_invalid_boundary_confidence_fail_closed(self) -> None:
        document = _document(_request())
        with self.assertRaises(AudioEventPerceptionError):
            AudioEvent(
                "bad.label",
                document.events[0].span,
                AudioEventKind.SPEECH,
                AudioEventPresence.PRESENT,
                "https://secret",
                Decimal("0.5"),
                Decimal("0.5"),
            )
        with self.assertRaises(AudioEventPerceptionError):
            AudioEvent(
                "bad.boundary",
                document.events[0].span,
                AudioEventKind.SPEECH,
                AudioEventPresence.PRESENT,
                "speech",
                Decimal("0.5"),
                Decimal("1.1"),
            )


if __name__ == "__main__":
    unittest.main()
