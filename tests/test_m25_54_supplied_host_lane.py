from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from copy import deepcopy
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from scripts import supplied_host_lane as lane


def _receipt(test_id: str, *, status: str = "clean") -> dict[str, object]:
    payload = {
        "schema": "h3.context.host_log_scan_receipt.v1",
        "test_id": test_id,
        "retry": 0,
        "status": status,
        "complete": status == "clean",
        "incomplete_reason": None if status == "clean" else "short_read",
        "owned_count": 0,
        "foreign_count": 1,
        "owned_examples": [],
        "foreign_examples": [{"message": "Error handling request"}],
        "retained_examples_capped": False,
        "bytes_clipped": False,
    }
    return {
        "name": "h3_host_log_scan",
        "contentType": "application/json",
        "body": base64.b64encode(json.dumps(payload).encode()).decode(),
    }


def _json_attachment(name: str, payload: object) -> dict[str, object]:
    return {
        "name": name,
        "contentType": "application/json",
        "body": base64.b64encode(json.dumps(payload).encode()).decode(),
    }


def _report(rows: list[tuple[str, str, str, str | None]]) -> dict[str, Any]:
    specs = []
    for index, (file, title, status, scan_status) in enumerate(rows):
        test_id = f"test-{index}"
        attachments = [] if scan_status is None else [_receipt(test_id, status=scan_status)]
        specs.append(
            {
                "title": title,
                "id": test_id,
                "file": file,
                "tests": [
                    {
                        "expectedStatus": "passed",
                        "results": [
                            {
                                "status": status,
                                "retry": 0,
                                "errors": [],
                                "attachments": attachments,
                            }
                        ],
                    }
                ],
            }
        )
    return {"suites": [{"title": "rows", "specs": specs}], "errors": []}


class SuppliedHostLaneUnitTests(unittest.TestCase):
    def test_loopback_url_is_explicit_and_foreign_hosts_are_rejected(self) -> None:
        self.assertEqual(lane.validate_host_url("http://127.0.0.1:8188"), ("127.0.0.1", 8188))
        for value in (
            "https://127.0.0.1:8188",
            "http://example.invalid:8188",
            "http://0.0.0.0:8188",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "loopback"):
                lane.validate_host_url(value)

    def test_row_selection_and_controls_are_explicit_and_bounded(self) -> None:
        row = lane.parse_row(
            "tests/e2e/journeys/host/sidebarResizeHost.spec.ts::"
            "M25-43 real sidebar follows its gutter and tiles its planning actions"
        )
        self.assertEqual(row.spec, "tests/e2e/journeys/host/sidebarResizeHost.spec.ts")
        self.assertEqual(
            lane.parse_row_environment(["H3_CONTEXT_M25_43_HOST=1"]),
            {"H3_CONTEXT_M25_43_HOST": "1"},
        )
        self.assertEqual(
            lane.parse_row_environment(["H3_CONTEXT_M25_46_HOST=1"]),
            {"H3_CONTEXT_M25_46_HOST": "1"},
        )
        self.assertEqual(
            lane.parse_row_environment(["H3_CONTEXT_M25_47_HOST=1"]),
            {"H3_CONTEXT_M25_47_HOST": "1"},
        )
        self.assertEqual(
            lane.parse_row_environment(
                [
                    "H3_CONTEXT_M25_34_MEDIA_JOURNEY=ready",
                    "H3_CONTEXT_M25_34_EXPECTED_SOURCE=managed",
                    "H3_CONTEXT_M25_34_OBSERVER_FFMPEG=C:/qualified/ffmpeg.exe",
                    "H3_CONTEXT_M25_34_OBSERVER_FFPROBE=C:/qualified/ffprobe.exe",
                    "H3_CONTEXT_M25_34_RENDER=1",
                    "H3_CONTEXT_M25_34_SOURCE_MEDIA=A:/output/fixture.mp4",
                    "H3_CONTEXT_M25_56_UX14=1",
                ]
            ),
            {
                "H3_CONTEXT_M25_34_MEDIA_JOURNEY": "ready",
                "H3_CONTEXT_M25_34_EXPECTED_SOURCE": "managed",
                "H3_CONTEXT_M25_34_OBSERVER_FFMPEG": "C:/qualified/ffmpeg.exe",
                "H3_CONTEXT_M25_34_OBSERVER_FFPROBE": "C:/qualified/ffprobe.exe",
                "H3_CONTEXT_M25_34_RENDER": "1",
                "H3_CONTEXT_M25_34_SOURCE_MEDIA": "A:/output/fixture.mp4",
                "H3_CONTEXT_M25_56_UX14": "1",
            },
        )
        self.assertEqual(
            lane.parse_row_environment(
                [
                    "H3_CONTEXT_M25_48_HOST=1",
                    'H3_CONTEXT_M25_48_ARTIFACT_LOCATOR={"filename":"fixture.mp4",'
                    '"subfolder":"h3-context-m25-48-fixtures","type":"output"}',
                ]
            ),
            {
                "H3_CONTEXT_M25_48_HOST": "1",
                "H3_CONTEXT_M25_48_ARTIFACT_LOCATOR": (
                    '{"filename":"fixture.mp4","subfolder":'
                    '"h3-context-m25-48-fixtures","type":"output"}'
                ),
            },
        )
        self.assertEqual(
            lane.parse_row_environment(
                [
                    "H3_CONTEXT_M25_49_HOST=1",
                    "H3_CONTEXT_NLE_WORKSPACE_HOST=1",
                ]
            ),
            {
                "H3_CONTEXT_M25_49_HOST": "1",
                "H3_CONTEXT_NLE_WORKSPACE_HOST": "1",
            },
        )
        self.assertEqual(
            lane.parse_row_environment(
                [
                    "H3_CONTEXT_M25_52_HOST=1",
                    "H3_CONTEXT_NLE_WORKSPACE_FIXTURE=.planning/evidence/m25-52/host-fixture.json",
                    "H3_CONTEXT_M25_52_RESOURCE_RECEIPT=.planning/evidence/m25-52/resources.json",
                ]
            ),
            {
                "H3_CONTEXT_M25_52_HOST": "1",
                "H3_CONTEXT_NLE_WORKSPACE_FIXTURE": (".planning/evidence/m25-52/host-fixture.json"),
                "H3_CONTEXT_M25_52_RESOURCE_RECEIPT": (".planning/evidence/m25-52/resources.json"),
            },
        )
        self.assertEqual(
            lane.parse_row_environment(
                [
                    "H3_CONTEXT_M25_53_HOST=1",
                    "H3_CONTEXT_M25_53_EXPECTATION=.planning/evidence/m25-53/expectation.json",
                    "H3_CONTEXT_M25_53_OBSERVER_FFMPEG=C:/qualified/ffmpeg.exe",
                    "H3_CONTEXT_M25_53_OBSERVER_FFPROBE=C:/qualified/ffprobe.exe",
                ]
            ),
            {
                "H3_CONTEXT_M25_53_HOST": "1",
                "H3_CONTEXT_M25_53_EXPECTATION": (".planning/evidence/m25-53/expectation.json"),
                "H3_CONTEXT_M25_53_OBSERVER_FFMPEG": "C:/qualified/ffmpeg.exe",
                "H3_CONTEXT_M25_53_OBSERVER_FFPROBE": "C:/qualified/ffprobe.exe",
            },
        )
        self.assertEqual(
            lane.parse_row_environment(["H3_CONTEXT_M25_55_HOST=1"]),
            {"H3_CONTEXT_M25_55_HOST": "1"},
        )
        self.assertEqual(
            lane.parse_row_environment(["H3_CONTEXT_M25_57_HOST=1"]),
            {"H3_CONTEXT_M25_57_HOST": "1"},
        )
        self.assertEqual(
            lane.parse_row_environment(
                [
                    "H3_CONTEXT_EMBEDDED_AUDIO_HOST_FIXTURE=.planning/evidence/260929-M25-58/host-fixture.json",
                    "H3_CONTEXT_EMBEDDED_AUDIO_OBSERVER=scripts/process_audio_observer.py",
                ]
            ),
            {
                "H3_CONTEXT_EMBEDDED_AUDIO_HOST_FIXTURE": (
                    ".planning/evidence/260929-M25-58/host-fixture.json"
                ),
                "H3_CONTEXT_EMBEDDED_AUDIO_OBSERVER": "scripts/process_audio_observer.py",
            },
        )
        self.assertEqual(
            lane.parse_row_environment(["H3_CONTEXT_M25_59_HOST=1"]),
            {"H3_CONTEXT_M25_59_HOST": "1"},
        )
        self.assertEqual(
            lane.parse_row_environment(["H3_CONTEXT_HOST_HEADED=1"]),
            {"H3_CONTEXT_HOST_HEADED": "1"},
        )
        # The deployment journey learns its venue from this control; the journey itself refuses
        # any value outside its closed venue list.
        self.assertEqual(
            lane.parse_row_environment(["H3_CONTEXT_DEPLOYMENT_URL=http://localhost:8188"]),
            {"H3_CONTEXT_DEPLOYMENT_URL": "http://localhost:8188"},
        )
        # The registration lifecycle row runs a weight-backed start only when the operator names
        # this control explicitly, and only with the value "1".
        self.assertEqual(
            lane.parse_row_environment(["H3_CONTEXT_WEIGHT_SAMPLE=1"]),
            {"H3_CONTEXT_WEIGHT_SAMPLE": "1"},
        )
        with self.assertRaises(ValueError):
            lane.parse_row_environment(["H3_CONTEXT_WEIGHT_SAMPLE=yes"])
        self.assertEqual(
            lane.parse_row_environment(["H3_CONTEXT_M25_53_SCHEDULER_TRACE=1"]),
            {"H3_CONTEXT_M25_53_SCHEDULER_TRACE": "1"},
        )
        for name in (
            "H3_CONTEXT_M23_19_REAL_I2VA=1",
            "H3_CONTEXT_M23_39_RUNTIME_FAILURE=fault",
            "H3_CONTEXT_M23_32_TERMINAL_WAITING=real",
        ):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "not an admitted row control"),
            ):
                lane.parse_row_environment([name])
        with self.assertRaisesRegex(ValueError, "invalid value for admitted row control"):
            lane.parse_row_environment(["H3_CONTEXT_M25_53_SCHEDULER_TRACE=0"])
        with self.assertRaisesRegex(ValueError, "invalid value for admitted row control"):
            lane.parse_row_environment(["H3_CONTEXT_HOST_HEADED=0"])
        for name in (
            "H3_CONTEXT_M25_34_MEDIA_JOURNEY=other",
            "H3_CONTEXT_M25_34_RENDER=0",
            "H3_CONTEXT_M25_34_EXPECTED_SOURCE=",
            "H3_CONTEXT_M25_56_UX14=0",
        ):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "invalid value for admitted row control"),
            ):
                lane.parse_row_environment([name])

    def test_structured_collection_requires_exact_file_and_full_title(self) -> None:
        rows = [lane.RowSelection("tests/e2e/journeys/host/a.spec.ts", "row A")]
        collected = _report([("journeys/host/a.spec.ts", "row A", "skipped", None)])
        self.assertEqual(lane.admit_collection(rows, collected), {"test-0": rows[0]})
        for bad in (
            _report([]),
            _report([("journeys/host/a.spec.ts", "row B", "skipped", None)]),
            _report(
                [
                    ("journeys/host/a.spec.ts", "row A", "skipped", None),
                    ("journeys/host/b.spec.ts", "extra", "skipped", None),
                ]
            ),
        ):
            with self.subTest(bad=bad), self.assertRaisesRegex(lane.AdmissionError, "selection"):
                lane.admit_collection(rows, bad)

    def test_result_join_rejects_missing_skipped_failed_and_incomplete_rows(self) -> None:
        row = lane.RowSelection("tests/e2e/journeys/host/a.spec.ts", "row A")
        selected = {"test-0": row}
        passed = cast(
            dict[str, Any],
            lane.admit_results(
                selected,
                _report([("journeys/host/a.spec.ts", "row A", "passed", "clean")]),
            ),
        )
        self.assertEqual(
            passed["counts"],
            {"selected": 1, "executed": 1, "passed": 1, "failed": 0, "skipped": 0, "incomplete": 0},
        )
        self.assertEqual(
            passed["rows"][0]["log_scan_receipt"]["foreign_examples"],
            [{"message": "Error handling request"}],
        )
        for status, scan in (
            ("skipped", None),
            ("failed", "clean"),
            ("passed", None),
            ("passed", "incomplete"),
        ):
            with self.subTest(status=status, scan=scan):
                result = lane.admit_results(
                    selected,
                    _report([("journeys/host/a.spec.ts", "row A", status, scan)]),
                )
                self.assertEqual(result["verdict"], "FAIL")

    def test_m25_52_evidence_is_candidate_bound_and_retains_every_frame(self) -> None:
        row = lane.RowSelection(lane.M25_52_SPEC, lane.M25_52_TITLE)
        selected = {"test-0": row}
        report = _report([(lane.M25_52_SPEC, lane.M25_52_TITLE, "passed", "clean")])
        frames = list(range(240))
        payload = {
            "schema": lane.M25_52_EVIDENCE_SCHEMA,
            "candidate": {
                "bundleSha256": "a" * 64,
                "installedInventorySha256": "c" * 64,
                "backendInventorySha256": "b" * 64,
            },
            "playback": {
                "frameSamples": frames,
                "frameSampleCount": len(frames),
            },
        }
        result_entry = report["suites"][0]["specs"][0]["tests"][0]["results"][0]
        result_entry["attachments"].append(
            _json_attachment(lane.M25_52_EVIDENCE_ATTACHMENT, payload)
        )
        admitted = cast(
            dict[str, Any],
            lane.admit_results(
                selected,
                report,
                expected_bundle_sha256="a" * 64,
                expected_inventory_sha256="c" * 64,
            ),
        )
        self.assertEqual(admitted["verdict"], "PASS")
        self.assertEqual(admitted["rows"][0]["evidence"]["playback"]["frameSamples"], frames)
        self.assertEqual(
            cast(dict[str, Any], lane.sanitize_tree(admitted))["rows"][0]["evidence"]["playback"][
                "frameSamples"
            ],
            frames,
        )
        for mutation in ("missing", "bundle-mismatch", "inventory-mismatch", "duplicate"):
            bad = _report([(lane.M25_52_SPEC, lane.M25_52_TITLE, "passed", "clean")])
            attachments = bad["suites"][0]["specs"][0]["tests"][0]["results"][0]["attachments"]
            if mutation != "missing":
                attachments.append(_json_attachment(lane.M25_52_EVIDENCE_ATTACHMENT, payload))
            if mutation == "duplicate":
                attachments.append(_json_attachment(lane.M25_52_EVIDENCE_ATTACHMENT, payload))
            admitted_bad = lane.admit_results(
                selected,
                bad,
                expected_bundle_sha256=("c" if mutation == "bundle-mismatch" else "a") * 64,
                expected_inventory_sha256=("d" if mutation == "inventory-mismatch" else "c") * 64,
            )
            self.assertEqual(admitted_bad["verdict"], "FAIL")
            self.assertEqual(cast(dict[str, int], admitted_bad["counts"])["incomplete"], 1)

        dropped = _report([(lane.M25_52_SPEC, lane.M25_52_TITLE, "passed", "clean")])
        dropped_payload = json.loads(json.dumps(payload))
        dropped_payload["playback"]["frameSamples"] = [0, 1, 3]
        dropped_payload["playback"]["frameSampleCount"] = 3
        dropped["suites"][0]["specs"][0]["tests"][0]["results"][0]["attachments"].append(
            _json_attachment(lane.M25_52_EVIDENCE_ATTACHMENT, dropped_payload)
        )
        admitted_dropped = lane.admit_results(
            selected,
            dropped,
            expected_bundle_sha256="a" * 64,
            expected_inventory_sha256="c" * 64,
        )
        self.assertEqual(admitted_dropped["verdict"], "FAIL")
        self.assertEqual(cast(dict[str, int], admitted_dropped["counts"])["incomplete"], 1)

    def test_m25_52_composited_timeline_png_is_retained_before_report_sanitizing(self) -> None:
        row = lane.RowSelection(lane.M25_52_SPEC, lane.M25_52_TITLE)
        selected = {"test-0": row}
        report = _report([(lane.M25_52_SPEC, lane.M25_52_TITLE, "passed", "clean")])
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/kZcAAAAASUVORK5CYII="
        )
        payload = {
            "schema": lane.M25_52_EVIDENCE_SCHEMA,
            "candidate": {
                "bundleSha256": "a" * 64,
                "installedInventorySha256": "c" * 64,
                "backendInventorySha256": "b" * 64,
            },
            "playback": {"frameSamples": [12, 13], "frameSampleCount": 2},
            "decorations": {
                "compositedTimelineCapture": {
                    "source": 'Playwright composed crop of [data-h3-nle-area="timeline"]',
                    "contentType": "image/png",
                    "sha256": hashlib.sha256(png).hexdigest(),
                    "byteLength": len(png),
                    "cropWidthPx": 1,
                    "cropHeightPx": 1,
                    "viewport": {"width": 1402, "height": 868},
                    "pngBase64": base64.b64encode(png).decode("ascii"),
                }
            },
        }
        report["suites"][0]["specs"][0]["tests"][0]["results"][0]["attachments"].append(
            _json_attachment(lane.M25_52_EVIDENCE_ATTACHMENT, payload)
        )

        with tempfile.TemporaryDirectory() as temporary:
            evidence_root = Path(temporary)
            admitted = cast(
                dict[str, Any],
                lane.admit_results(
                    selected,
                    report,
                    expected_bundle_sha256="a" * 64,
                    expected_inventory_sha256="c" * 64,
                    capture_evidence_dir=evidence_root,
                    capture_attempt="waveform-evidence-01",
                ),
            )
            self.assertEqual(admitted["verdict"], "PASS")
            retained = admitted["rows"][0]["evidence"]["decorations"]["compositedTimelineCapture"]
            self.assertNotIn("pngBase64", retained)
            self.assertEqual(retained["evidencePath"], "captures/waveform-evidence-01-timeline.png")
            self.assertEqual(
                (evidence_root / retained["evidencePath"]).read_bytes(),
                png,
            )
            sanitized = cast(dict[str, Any], lane.sanitize_tree(admitted))
            emitted = json.dumps(sanitized)
            self.assertNotIn("pngBase64", emitted)
            self.assertIn("captures/waveform-evidence-01-timeline.png", emitted)
            report_path = evidence_root / "attempt.json"
            lane._write_report(report_path, admitted)
            persisted = json.loads(report_path.read_text(encoding="utf-8"))
            persisted_capture = persisted["rows"][0]["evidence"]["decorations"][
                "compositedTimelineCapture"
            ]
            self.assertNotIn("pngBase64", persisted_capture)
            self.assertEqual(
                (evidence_root / persisted_capture["evidencePath"]).read_bytes(),
                png,
            )
            duplicate = cast(
                dict[str, Any],
                lane.admit_results(
                    selected,
                    report,
                    expected_bundle_sha256="a" * 64,
                    expected_inventory_sha256="c" * 64,
                    capture_evidence_dir=evidence_root,
                    capture_attempt="waveform-evidence-01",
                ),
            )
            self.assertEqual(duplicate["verdict"], "FAIL")
            self.assertEqual(
                (evidence_root / persisted_capture["evidencePath"]).read_bytes(),
                png,
            )

    def test_m25_52_timeline_capture_rejects_invalid_integrity_and_attempt(self) -> None:
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/kZcAAAAASUVORK5CYII="
        )
        valid_capture = {
            "source": 'Playwright composed crop of [data-h3-nle-area="timeline"]',
            "contentType": "image/png",
            "sha256": hashlib.sha256(png).hexdigest(),
            "byteLength": len(png),
            "cropWidthPx": 1,
            "cropHeightPx": 1,
            "viewport": {"width": 1402, "height": 868},
            "pngBase64": base64.b64encode(png).decode("ascii"),
        }
        row = lane.RowSelection(lane.M25_52_SPEC, lane.M25_52_TITLE)
        selected = {"test-0": row}
        for field, value in (
            ("sha256", "0" * 64),
            ("cropWidthPx", 2),
            ("viewport", {"width": 1280, "height": 720}),
            ("pngBase64", "not-base64"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                capture = deepcopy(valid_capture)
                capture[field] = value
                payload = {
                    "schema": lane.M25_52_EVIDENCE_SCHEMA,
                    "candidate": {
                        "bundleSha256": "a" * 64,
                        "installedInventorySha256": "c" * 64,
                        "backendInventorySha256": "b" * 64,
                    },
                    "playback": {"frameSamples": [12, 13], "frameSampleCount": 2},
                    "decorations": {"compositedTimelineCapture": capture},
                }
                report = _report([(lane.M25_52_SPEC, lane.M25_52_TITLE, "passed", "clean")])
                report["suites"][0]["specs"][0]["tests"][0]["results"][0]["attachments"].append(
                    _json_attachment(lane.M25_52_EVIDENCE_ATTACHMENT, payload)
                )
                admitted = lane.admit_results(
                    selected,
                    report,
                    expected_bundle_sha256="a" * 64,
                    expected_inventory_sha256="c" * 64,
                    capture_evidence_dir=Path(temporary),
                    capture_attempt="waveform-integrity-01",
                )
                self.assertEqual(admitted["verdict"], "FAIL")
                self.assertEqual(
                    cast(dict[str, int], admitted["counts"])["incomplete"],
                    1,
                )
                self.assertFalse((Path(temporary) / "captures").exists())

        payload = {
            "schema": lane.M25_52_EVIDENCE_SCHEMA,
            "candidate": {
                "bundleSha256": "a" * 64,
                "installedInventorySha256": "c" * 64,
                "backendInventorySha256": "b" * 64,
            },
            "playback": {"frameSamples": [12, 13], "frameSampleCount": 2},
            "decorations": {"compositedTimelineCapture": valid_capture},
        }
        report = _report([(lane.M25_52_SPEC, lane.M25_52_TITLE, "passed", "clean")])
        report["suites"][0]["specs"][0]["tests"][0]["results"][0]["attachments"].append(
            _json_attachment(lane.M25_52_EVIDENCE_ATTACHMENT, payload)
        )
        with tempfile.TemporaryDirectory() as temporary:
            admitted = lane.admit_results(
                selected,
                report,
                expected_bundle_sha256="a" * 64,
                expected_inventory_sha256="c" * 64,
                capture_evidence_dir=Path(temporary),
                capture_attempt="../outside",
            )
            self.assertEqual(admitted["verdict"], "FAIL")
            self.assertFalse((Path(temporary).parent / "outside-timeline.png").exists())

    def test_m25_53_evidence_is_candidate_bound_and_requires_independent_observations(
        self,
    ) -> None:
        row = lane.RowSelection(lane.M25_53_SPEC, lane.M25_53_TITLE)
        selected = {"test-0": row}
        payload = {
            "schema": lane.M25_53_EVIDENCE_SCHEMA,
            "candidate": {
                "bundleSha256": "a" * 64,
                "installedInventorySha256": "c" * 64,
                "backendInventorySha256": "b" * 64,
            },
            "sources": {"assetIds": ["video_1", "video_2", "video_3"]},
            "export": {
                "downloadedSha256": "d" * 64,
                "downloadedBytes": 4096,
                "frameCount": 396,
                "decodedAudioSamples": 792000,
                "expectedAudioSamples": 792000,
                "expectedAudioExtentSamples": 792000,
                "observations": {
                    "independent": True,
                    "clipOrderMatched": True,
                    "sourceRangesMatched": True,
                    "transitionMatched": True,
                    "audioMatched": True,
                    "visualEditsMatched": True,
                },
                "observer": {
                    "landmarkSamples": [
                        {
                            "assetId": f"video_{index + 1}",
                            "sourceFrame": offset,
                            "outputFrame": index * 96 + offset,
                            "exactFrameIndexError": 2.0,
                            "adjacentFrameIndexError": 18.0,
                            "sourceMarker": {"red": 255, "green": 255, "blue": 255},
                            "outputMarker": {"red": 250, "green": 250, "blue": 250},
                            "sourceOther": {"red": 250, "green": 0, "blue": 0},
                            "outputOther": {"red": 245, "green": 0, "blue": 0},
                        }
                        for index in range(3)
                        for offset in (6, 32)
                    ],
                    "openingSilence": [
                        {"assetId": f"video_{index + 1}", "sourceRms": 0.0, "outputRms": 0.001}
                        for index in range(3)
                    ],
                    "audioOnsets": [
                        {
                            "assetId": f"video_{index + 1}",
                            "expectedSourceOnset": 12_000,
                            "sourceOnset": 12_000,
                            "expectedOutputOnset": index * 192_000 + 12_000,
                            "outputOnset": index * 192_000 + 12_000,
                        }
                        for index in range(3)
                    ],
                    "tailToneRms": 0.05,
                    "contentTail": {
                        "finalVideoFrame": 395,
                        "finalDecodedAudioWindowRms": 0.05,
                    },
                    "transitionAlpha": 0.5,
                    "titleBefore": 0,
                    "titleDuring": 200,
                },
            },
        }

        def report_with(value: dict[str, Any], *, duplicate: bool = False) -> dict[str, Any]:
            report = _report([(lane.M25_53_SPEC, lane.M25_53_TITLE, "passed", "clean")])
            attachments = report["suites"][0]["specs"][0]["tests"][0]["results"][0]["attachments"]
            attachments.append(_json_attachment(lane.M25_53_EVIDENCE_ATTACHMENT, value))
            if duplicate:
                attachments.append(_json_attachment(lane.M25_53_EVIDENCE_ATTACHMENT, value))
            return report

        admitted = cast(
            dict[str, Any],
            lane.admit_results(
                selected,
                report_with(payload),
                expected_bundle_sha256="a" * 64,
                expected_inventory_sha256="c" * 64,
            ),
        )
        self.assertEqual(admitted["verdict"], "PASS")
        self.assertEqual(admitted["rows"][0]["evidence"], payload)

        for malformed in ({"assetId": "video_1"}, ["video_1"]):
            bad_asset = cast(dict[str, Any], deepcopy(payload))
            bad_asset["sources"]["assetIds"][0] = malformed
            rejected = cast(
                dict[str, Any],
                lane.admit_results(
                    selected,
                    report_with(bad_asset),
                    expected_bundle_sha256="a" * 64,
                    expected_inventory_sha256="c" * 64,
                ),
            )
            self.assertEqual(rejected["verdict"], "FAIL")
            self.assertEqual(cast(dict[str, int], rejected["counts"])["incomplete"], 1)

        invalid_payloads: list[tuple[str, dict[str, Any]]] = []
        for observation in (
            "independent",
            "clipOrderMatched",
            "sourceRangesMatched",
            "transitionMatched",
            "audioMatched",
            "visualEditsMatched",
        ):
            value = json.loads(json.dumps(payload))
            value["export"]["observations"][observation] = False
            invalid_payloads.append((observation, value))
        duplicate_source = json.loads(json.dumps(payload))
        duplicate_source["sources"]["assetIds"] = ["video_1", "video_1", "video_3"]
        invalid_payloads.append(("duplicate-source", duplicate_source))
        empty_download = json.loads(json.dumps(payload))
        empty_download["export"]["downloadedBytes"] = 0
        invalid_payloads.append(("empty-download", empty_download))
        missing_landmarks = cast(dict[str, Any], deepcopy(payload))
        missing_landmarks["export"]["observer"]["landmarkSamples"] = []
        invalid_payloads.append(("missing-landmarks", missing_landmarks))
        shifted_frame = cast(dict[str, Any], deepcopy(payload))
        shifted_frame["export"]["observer"]["landmarkSamples"][0]["adjacentFrameIndexError"] = 2.0
        invalid_payloads.append(("shifted-frame", shifted_frame))
        missing_onset = cast(dict[str, Any], deepcopy(payload))
        missing_onset["export"]["observer"]["audioOnsets"] = []
        invalid_payloads.append(("missing-onset", missing_onset))

        delayed_onset = cast(dict[str, Any], deepcopy(payload))
        delayed_onset["export"]["observer"]["audioOnsets"][1]["outputOnset"] += 4_096
        invalid_payloads.append(("delayed-onset", delayed_onset))

        mismatched_content_tail = cast(dict[str, Any], deepcopy(payload))
        mismatched_content_tail["export"]["observer"]["contentTail"]["finalVideoFrame"] = 394
        invalid_payloads.append(("mismatched-content-tail", mismatched_content_tail))

        # The current pinned decoder honors the container's discard padding, so playable PCM is
        # held to the exact timeline extent. A decode two samples off or either a shortened or
        # codec-rounded expectation is refused.
        short_decode = cast(dict[str, Any], deepcopy(payload))
        short_decode["export"]["decodedAudioSamples"] = 791998
        invalid_payloads.append(("short-decode", short_decode))
        extent_below_timeline = cast(dict[str, Any], deepcopy(payload))
        extent_below_timeline["export"]["expectedAudioExtentSamples"] = 791999
        extent_below_timeline["export"]["decodedAudioSamples"] = 791999
        invalid_payloads.append(("extent-below-timeline", extent_below_timeline))
        codec_rounded_extent = cast(dict[str, Any], deepcopy(payload))
        codec_rounded_extent["export"]["expectedAudioExtentSamples"] = 792576
        codec_rounded_extent["export"]["decodedAudioSamples"] = 792576
        invalid_payloads.append(("codec-rounded-extent", codec_rounded_extent))
        silent_content_tail = cast(dict[str, Any], deepcopy(payload))
        silent_content_tail["export"]["observer"]["contentTail"]["finalDecodedAudioWindowRms"] = 0.0
        invalid_payloads.append(("silent-content-tail", silent_content_tail))
        wrong_audio_extent = cast(dict[str, Any], deepcopy(payload))
        wrong_audio_extent["export"]["decodedAudioSamples"] = 791000
        invalid_payloads.append(("wrong-audio-extent", wrong_audio_extent))
        absent_title = cast(dict[str, Any], deepcopy(payload))
        absent_title["export"]["observer"]["titleDuring"] = 0
        invalid_payloads.append(("absent-title", absent_title))

        for mutation, value in invalid_payloads:
            with self.subTest(mutation=mutation):
                rejected = lane.admit_results(
                    selected,
                    report_with(value),
                    expected_bundle_sha256="a" * 64,
                    expected_inventory_sha256="c" * 64,
                )
                self.assertEqual(rejected["verdict"], "FAIL")
                self.assertEqual(cast(dict[str, int], rejected["counts"])["incomplete"], 1)

        for mutation, report, bundle, inventory in (
            ("duplicate-attachment", report_with(payload, duplicate=True), "a" * 64, "c" * 64),
            ("bundle-mismatch", report_with(payload), "e" * 64, "c" * 64),
            ("inventory-mismatch", report_with(payload), "a" * 64, "e" * 64),
        ):
            with self.subTest(mutation=mutation):
                rejected = lane.admit_results(
                    selected,
                    report,
                    expected_bundle_sha256=bundle,
                    expected_inventory_sha256=inventory,
                )
                self.assertEqual(rejected["verdict"], "FAIL")
                self.assertEqual(cast(dict[str, int], rejected["counts"])["incomplete"], 1)

    def test_m25_56_ux14_evidence_is_candidate_bound_and_requires_full_source_lifecycle(
        self,
    ) -> None:
        row = lane.RowSelection(lane.M25_56_SPEC, lane.M25_56_TITLE)
        selected = {"test-0": row}
        payload = {
            "schema": lane.M25_56_EVIDENCE_SCHEMA,
            "candidate": {
                "bundleSha256": "a" * 64,
                "installedInventorySha256": "c" * 64,
                "backendInventorySha256": "d" * 64,
            },
            "disposition": "not_reproduced",
            "originalState": {
                "monitor": "source_unavailable",
                "audio": "suspended",
                "recoverOffered": True,
            },
            "currentState": {
                "monitorStatus": "Monitor paused.",
                "recoverOffered": False,
                "beforePlay": 12,
                "advancedTo": 18,
                "pausedAt": 18,
                "audioState": "suspended",
                "decodedPixelSamplesAfterPause": lane.M25_56_MINIMUM_DECODED_PIXEL_SAMPLES,
            },
            "input": {
                "kind": "production_output",
                "selectedOutputs": 1,
                "importedAssetIdPresent": True,
            },
            "sourceLease": [
                {
                    "operation": "create",
                    "phase": "before_close",
                    "leaseAlias": "monitor-playback-1",
                    "scope": "clip",
                    "clipIdPresent": True,
                    "derivativeKind": "video_proxy",
                    "ownerMatchesClip": True,
                    "status": 200,
                },
                {
                    "operation": "open",
                    "phase": "before_close",
                    "leaseAlias": "monitor-playback-1",
                    "scope": None,
                    "clipIdPresent": False,
                    "derivativeKind": None,
                    "ownerMatchesClip": True,
                    "status": 200,
                },
                {
                    "operation": "release",
                    "phase": "after_close",
                    "leaseAlias": "monitor-playback-1",
                    "scope": None,
                    "clipIdPresent": False,
                    "derivativeKind": None,
                    "ownerMatchesClip": True,
                    "status": 200,
                },
            ],
            "closed": True,
        }

        def report_with(value: dict[str, Any]) -> dict[str, Any]:
            report = _report([(lane.M25_56_SPEC, lane.M25_56_TITLE, "passed", "clean")])
            attachments = report["suites"][0]["specs"][0]["tests"][0]["results"][0]["attachments"]
            attachments.append(_json_attachment(lane.M25_56_EVIDENCE_ATTACHMENT, value))
            return report

        admitted = cast(
            dict[str, Any],
            lane.admit_results(
                selected,
                report_with(payload),
                expected_bundle_sha256="a" * 64,
                expected_inventory_sha256="c" * 64,
            ),
        )
        self.assertEqual(admitted["verdict"], "PASS")
        self.assertEqual(admitted["rows"][0]["evidence"], payload)

        for mutation in (
            "candidate",
            "installed-inventory",
            "missing-backend-inventory",
            "release",
            "wrong-order",
            "wrong-lease",
            "release-before-close",
            "missing-audio-state",
            "null-audio-state",
            "list-audio-state",
            "object-audio-state",
            "invalid-audio-state",
            "numeric-boolean",
            "float-status",
            "empty-monitor-state",
            "negative-frame",
            "insufficient-decoded-pixels",
        ):
            bad = cast(dict[str, Any], deepcopy(payload))
            if mutation == "candidate":
                bad["candidate"]["bundleSha256"] = "b" * 64
            elif mutation == "installed-inventory":
                bad["candidate"]["installedInventorySha256"] = "e" * 64
            elif mutation == "missing-backend-inventory":
                del bad["candidate"]["backendInventorySha256"]
            elif mutation == "release":
                bad["sourceLease"] = bad["sourceLease"][:-1]
            elif mutation == "wrong-order":
                bad["sourceLease"][0], bad["sourceLease"][1] = (
                    bad["sourceLease"][1],
                    bad["sourceLease"][0],
                )
            elif mutation == "wrong-lease":
                bad["sourceLease"][2]["leaseAlias"] = "monitor-playback-2"
            elif mutation == "release-before-close":
                bad["sourceLease"][2]["phase"] = "before_close"
            elif mutation == "missing-audio-state":
                del bad["currentState"]["audioState"]
            elif mutation == "null-audio-state":
                bad["currentState"]["audioState"] = None
            elif mutation == "list-audio-state":
                bad["currentState"]["audioState"] = []
            elif mutation == "object-audio-state":
                bad["currentState"]["audioState"] = {}
            elif mutation == "invalid-audio-state":
                bad["currentState"]["audioState"] = "unknown"
            elif mutation == "numeric-boolean":
                bad["sourceLease"][0]["clipIdPresent"] = 1
            elif mutation == "float-status":
                bad["sourceLease"][2]["status"] = 200.0
            elif mutation == "empty-monitor-state":
                bad["currentState"]["monitorStatus"] = ""
            elif mutation == "insufficient-decoded-pixels":
                bad["currentState"]["decodedPixelSamplesAfterPause"] = (
                    lane.M25_56_MINIMUM_DECODED_PIXEL_SAMPLES - 1
                )
            else:
                bad["currentState"]["beforePlay"] = -1
            rejected = cast(
                dict[str, Any],
                lane.admit_results(
                    selected,
                    report_with(bad),
                    expected_bundle_sha256="a" * 64,
                    expected_inventory_sha256="c" * 64,
                ),
            )
            self.assertEqual(rejected["verdict"], "FAIL")
            self.assertIn(
                "missing_or_invalid_row_evidence",
                rejected["rows"][0]["reasons"],
            )

    def test_m25_52_failed_row_retains_bounded_lifecycle_facts(self) -> None:
        row = lane.RowSelection(lane.M25_52_SPEC, lane.M25_52_TITLE)
        selected = {"test-0": row}
        report = _report([(lane.M25_52_SPEC, lane.M25_52_TITLE, "failed", "clean")])
        payload = {
            "schema": "h3.context.m25_52.full_route_failure_facts.v1",
            "stage": "cross_dissolve_with_request_pending",
            "overlappingHandlerRequest": {
                "url": "http://127.0.0.1:8188/api/h3-context/v1/authoring/media-source-leases",
                "operation": "create",
                "ownerId": "nle-decoration-1-0",
                "kind": "thumbnail",
                "endedAt": 25.0,
                "outcome": "response:200",
            },
            "overlapTransitionFrame": 12,
            "overlapPendingAtTransition": False,
        }
        result_entry = report["suites"][0]["specs"][0]["tests"][0]["results"][0]
        result_entry["attachments"].append(
            _json_attachment("m25-52-full-route-failure-facts", payload)
        )

        admitted = cast(dict[str, Any], lane.admit_results(selected, report))

        self.assertEqual(admitted["verdict"], "FAIL")
        self.assertEqual(admitted["rows"][0]["failure_facts"], payload)

    def test_failed_row_retains_bounded_sanitized_error_summaries(self) -> None:
        row = lane.RowSelection(lane.M25_52_SPEC, lane.M25_52_TITLE)
        selected = {"test-0": row}
        report = _report([(lane.M25_52_SPEC, lane.M25_52_TITLE, "failed", "clean")])
        result_entry = report["suites"][0]["specs"][0]["tests"][0]["results"][0]
        result_entry["errors"] = [
            {
                "message": "expect(received).toBeLessThan(expected)",
                "location": {
                    "file": "B:\\private\\repo\\m25_52EmbeddedAudioWaveform.spec.ts",
                    "line": 1139,
                    "column": 21,
                },
            }
        ]

        admitted = cast(dict[str, Any], lane.admit_results(selected, report))

        summaries = admitted["rows"][0]["error_summaries"]
        self.assertEqual(len(summaries), 1)
        self.assertIn("toBeLessThan", summaries[0]["message"])
        self.assertEqual(summaries[0]["location"]["file"], "<path>")
        self.assertEqual(summaries[0]["location"]["line"], 1139)

    def test_result_join_rechecks_identity_and_global_report_errors(self) -> None:
        row = lane.RowSelection("tests/e2e/journeys/host/a.spec.ts", "row A")
        selected = {"test-0": row}
        mismatched = _report([("journeys/host/a.spec.ts", "different title", "passed", "clean")])
        self.assertEqual(lane.admit_results(selected, mismatched)["verdict"], "FAIL")
        global_error = _report([("journeys/host/a.spec.ts", "row A", "passed", "clean")])
        global_error["errors"] = [{"message": "synthetic reporter failure"}]
        result = lane.admit_results(selected, global_error)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["global_error_count"], 1)

    def test_process_and_queue_snapshots_fail_closed(self) -> None:
        expected = lane.ExpectedProcess(
            pid=12,
            created=100.5,
            executable="C:/host/python.exe",
            cwd="C:/host",
            entrypoint="C:/host/main.py",
            port=8188,
        )
        observed = lane.ProcessSnapshot(**expected.__dict__, listening=True)
        lane.validate_process_snapshot(expected, observed)
        with self.assertRaisesRegex(lane.AdmissionError, "creation"):
            lane.validate_process_snapshot(
                expected, lane.ProcessSnapshot(**{**observed.__dict__, "created": 99.0})
            )
        lane.validate_idle_queue({"queue_running": [], "queue_pending": []})
        with self.assertRaisesRegex(lane.AdmissionError, "occupied"):
            lane.validate_idle_queue({"queue_running": [[1]], "queue_pending": []})

    def test_expected_process_uses_the_explicit_host_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python = root / "python.exe"
            entrypoint = root / "qualification" / "host_entry.py"
            entrypoint.parent.mkdir()
            python.touch()
            entrypoint.touch()
            arguments = Namespace(
                host_url="http://127.0.0.1:8188",
                host_root=str(root),
                host_python=str(python),
                host_entrypoint=str(entrypoint),
                pid=12,
                created=100.5,
            )

            expected = lane._expected_process(arguments)

            self.assertEqual(expected.entrypoint, str(entrypoint.resolve()))

    def test_candidate_inventory_is_bounded_and_sync_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            backup = root / "backup"
            source.mkdir()
            target.mkdir()
            (source / "a.py").write_text("new-a", encoding="utf-8")
            (source / "b.json").write_text("new-b", encoding="utf-8")
            (target / "a.py").write_text("old-a", encoding="utf-8")
            (target / "b.json").write_text("old-b", encoding="utf-8")
            changes = lane.candidate_changes(source, target)
            self.assertEqual([item.relative_path for item in changes.changed], ["a.py", "b.json"])

            writes = 0

            def fail_second(source_path: Path, temporary_path: Path) -> None:
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("synthetic write refusal")
                temporary_path.write_bytes(source_path.read_bytes())

            with self.assertRaisesRegex(OSError, "synthetic write refusal"):
                lane.synchronize_candidate(changes, backup, write_temporary=fail_second)
            self.assertEqual((target / "a.py").read_text(encoding="utf-8"), "old-a")
            self.assertEqual((target / "b.json").read_text(encoding="utf-8"), "old-b")

            successful_backup = root / "successful-backup"
            written = lane.synchronize_candidate(changes, successful_backup)
            self.assertEqual((target / "a.py").read_text(encoding="utf-8"), "new-a")
            lane.restore_candidate(changes, successful_backup, written)
            self.assertEqual((target / "a.py").read_text(encoding="utf-8"), "old-a")
            self.assertEqual((target / "b.json").read_text(encoding="utf-8"), "old-b")

    def test_timeout_terminates_the_owned_subprocess_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_pid = root / "child.pid"
            parent = root / "parent.py"
            parent.write_text(
                "\n".join(
                    [
                        "import pathlib, subprocess, sys, time",
                        "child = subprocess.Popen([sys.executable, '-c', "
                        "'import time; time.sleep(60)'])",
                        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')",
                        "time.sleep(60)",
                    ]
                ),
                encoding="utf-8",
            )
            result = lane.run_bounded(
                [sys.executable, str(parent), str(child_pid)],
                cwd=root,
                environment=os.environ,
                timeout=0.5,
            )
            self.assertTrue(result.timed_out)
            pid = int(child_pid.read_text(encoding="utf-8"))
            time_limit = 30
            while time_limit and lane.process_exists(pid):
                time_limit -= 1
                time.sleep(0.1)
            self.assertFalse(lane.process_exists(pid))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_candidate_inventory_rejects_an_escaping_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            outside = root / "outside.py"
            outside.write_text("outside", encoding="utf-8")
            try:
                (target / "x.py").symlink_to(outside)
            except OSError:
                self.skipTest("symlink creation unavailable")
            (source / "x.py").write_text("candidate", encoding="utf-8")
            with self.assertRaisesRegex(lane.AdmissionError, "unsafe"):
                lane.candidate_changes(source, target)

    def test_candidate_inventory_rejects_traversal_and_capacity_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            with self.assertRaisesRegex(lane.AdmissionError, "unsafe"):
                lane._assert_safe_destination(target, root / "outside.py")
            for index in range(lane.MAX_CANDIDATE_FILES + 1):
                (source / f"f{index:03}.py").write_text("x", encoding="utf-8")
            (target / "placeholder.py").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(lane.AdmissionError, "512"):
                lane.candidate_changes(source, target)

    def test_run_environment_drops_inherited_execution_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle.js"
            log = root / "host.log"
            bundle.write_text("bundle", encoding="utf-8")
            log.write_text("log\n", encoding="utf-8")
            arguments = Namespace(
                row_env=["H3_CONTEXT_M25_45_HOST=1"],
                host_root=str(root),
                host_url="http://127.0.0.1:8188",
                bundle_sha256="a" * 64,
            )
            with patch.dict(
                os.environ,
                {
                    "H3_CONTEXT_M23_19_REAL_I2VA": "1",
                    "H3_CONTEXT_M23_39_RUNTIME_FAILURE": "fault",
                    "H3_CONTEXT_FUTURE_UNADMITTED_CONTROL": "1",
                },
            ):
                environment = lane._run_environment(
                    arguments,
                    {
                        "candidate_root": root,
                        "bundle": bundle,
                        "log": log,
                        "drift": lane.CandidateChanges(
                            root,
                            root,
                            1,
                            (),
                            (),
                            "d" * 64,
                        ),
                    },
                )
            self.assertNotIn("H3_CONTEXT_M23_19_REAL_I2VA", environment)
            self.assertNotIn("H3_CONTEXT_M23_39_RUNTIME_FAILURE", environment)
            self.assertNotIn("H3_CONTEXT_FUTURE_UNADMITTED_CONTROL", environment)
            self.assertEqual(environment["H3_CONTEXT_M25_45_HOST"], "1")
            self.assertEqual(
                environment["H3_CONTEXT_CANDIDATE_INVENTORY_SHA256"],
                "d" * 64,
            )

    def test_missing_or_malformed_playwright_report_is_rejected(self) -> None:
        for stdout in ("", "not-json", "[]"):
            with self.subTest(stdout=stdout), self.assertRaisesRegex(lane.AdmissionError, "report"):
                lane._parse_json_output(
                    lane.CommandResult(1, stdout, "private diagnostic", False),
                    "execution",
                )

    def test_sanitizer_removes_private_paths_urls_and_tokens(self) -> None:
        private = {
            "message": "C:\\Users\\Private\\file.png token=super-secret",
            "url": "https://example.invalid/file?signature=signed-secret",
        }
        emitted = json.dumps(lane.sanitize_tree(private))
        for canary in ("Private", "super-secret", "example.invalid", "signed-secret"):
            self.assertNotIn(canary, emitted)

    def test_cli_retains_a_sanitized_failed_attempt_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "negative.json"
            entrypoint = root / "main.py"
            entrypoint.touch()
            arguments = [
                "run",
                "--host-url",
                "http://example.invalid:8188",
                "--host-root",
                str(root),
                "--host-python",
                sys.executable,
                "--host-entrypoint",
                str(entrypoint),
                "--pid",
                str(os.getpid()),
                "--created",
                "1",
                "--candidate-root",
                str(root),
                "--candidate-subject",
                "a" * 40,
                "--bundle",
                str(root / "missing.js"),
                "--bundle-sha256",
                "b" * 64,
                "--host-log",
                str(root / "missing.log"),
                "--pack-name",
                "Synthetic-Pack",
                "--evidence-dir",
                str(root),
                "--attempt",
                "negative",
                "--row",
                "tests/e2e/journeys/host/a.spec.ts::row A",
            ]
            self.assertEqual(lane.main(arguments), 4)
            first = report.read_text(encoding="utf-8")
            self.assertIn('"verdict": "FAIL"', first)
            self.assertNotIn("example.invalid", first)
            self.assertEqual(lane.main(arguments), 4)
            self.assertEqual(report.read_text(encoding="utf-8"), first)


if __name__ == "__main__":
    unittest.main()
