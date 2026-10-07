"""Portable editable data is strict and never a persisted execution/source grant."""

from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

MODULE = "comfyui_h3_context.core.project_document"


def document_wire(frames: int = 24) -> dict[str, Any]:
    asset = {
        "asset_id": "video.saved",
        "kind": "video",
        "source_time_base": {"num": 1, "den": 24},
        "source_frame_count": frames,
        "source_sample_count": frames * 2000,
        "embedded_audio": "present_bound",
        "timestamp_policy": "nonnegative_monotonic_v1",
        "landmarks": [
            {"frame_index": n, "pts": n, "dts": n, "duration_ticks": 1} for n in range(frames)
        ],
    }
    clip = {
        "clip_id": "clip.saved",
        "asset_id": "video.saved",
        "track_id": "track.main",
        "start_frame": 0,
        "duration_frames": 24,
        "source_start_frame": 0,
        "enabled": True,
        "transform": {
            "anchor_x_bp": 5000,
            "anchor_y_bp": 5000,
            "position_x_bp": 25,
            "position_y_bp": -25,
            "scale_x_bp": 11000,
            "scale_y_bp": 12000,
            "rotation_mdeg": 2500,
        },
        "crop": {"left_bp": 100, "top_bp": 200, "right_bp": 300, "bottom_bp": 400},
        "opacity_bp": 7500,
        "blend": "normal",
        "text": None,
        "transition": {"kind": "none", "duration_frames": 0},
        "effect": {
            "kind": "color_adjust_v1",
            "brightness_permille": 15,
            "contrast_permille": 1050,
            "saturation_permille": 900,
        },
        "audio": {"gain_mb": -100, "muted": True, "fade_in_frames": 2, "fade_out_frames": 3},
    }
    return {
        "format": "h3proj",
        "schema_version": 1,
        "title": "Synthetic editable project",
        "production": {
            "segments": [
                {
                    "segment_id": "segment.saved",
                    "task_mode": "t2va",
                    "duration_milliseconds": 5000,
                    "relation": "independent",
                    "predecessor_segment_id": None,
                    "source_asset_id": None,
                    "reference_asset_ids": [],
                }
            ],
            "selection": ["segment.saved"],
        },
        "planning": {
            "intent": "Synthetic intent",
            "script": "Synthetic script",
            "target_seconds": 10,
            "policy": "fixed_5",
            "shots": [
                {
                    "shot_id": "shot-1",
                    "ordinal": 1,
                    "start_milliseconds": 0,
                    "end_milliseconds": 10000,
                    "text": "Synthetic shot",
                    "hard_boundary": True,
                }
            ],
        },
        "editor": {
            "edit_capacity_frames": 3600,
            "assets": [asset],
            "tracks": [
                {
                    "track_id": "track.main",
                    "kind": "primary_video",
                    "order": 0,
                    "enabled": True,
                    "locked": False,
                }
            ],
            "clips": [clip],
            "audio_extension": {
                "schema": "h3.authoring.independent_audio_extension.v1",
                "track_profile": "none_v1",
                "command_namespace": "h3.authoring.audio.command.v1",
                "command_members": [],
                "preview_edit_capability": "unsupported",
                "final_render_edit_capability": "unsupported",
                "embedded_renderer_variant": "EmbeddedAudioSpanV1",
                "independent_audio_renderer_variant": "none_v1",
                "reason": "audio_editing_deferred",
            },
            "reference": {
                "sources": [
                    {"source_id": "video.saved", "kind": "video", "duration_milliseconds": 1000}
                ],
                "soundtracks": [],
            },
        },
        "media": [
            {
                "asset_id": "video.saved",
                "content_fingerprint": "sha256:" + "a" * 64,
                "byte_length": 1024,
                "retained_id": None,
            }
        ],
    }


class ProjectDocumentTests(unittest.TestCase):
    def module(self) -> ModuleType:
        self.assertIsNotNone(
            importlib.util.find_spec(MODULE), "missing strict editable project contract"
        )
        return importlib.import_module(MODULE)

    def decode(self, value: object) -> Any:
        return self.module().decode_project_document(json.dumps(value).encode())

    def test_all_editable_fields_round_trip_without_runtime_authority(self) -> None:
        expected = document_wire()
        module = self.module()
        document = self.decode(expected)
        self.assertEqual(document.to_wire(), expected)
        self.assertEqual(
            module.decode_project_document(module.encode_project_document(document)).to_wire(),
            expected,
        )
        state = document.fresh_authoring("project.new", "authoring-new")
        self.assertEqual(state.project_id, "project.new")
        self.assertEqual(state.workspace_handle, "authoring-new")
        self.assertEqual(state.workspace_revision, 1)
        self.assertEqual([c.to_wire() for c in state.clips], expected["editor"]["clips"])
        self.assertFalse(
            any(k in expected["editor"] for k in ("workspace_handle", "receipt", "cursor"))
        )

    def test_full_512_timing_landmarks_are_not_truncated_by_generic_canonicalizer(self) -> None:
        expected = document_wire(512)
        document = self.decode(expected)
        self.assertEqual(document.to_wire(), expected)
        self.assertEqual(
            len(document.fresh_authoring("project.new", "authoring-new").assets[0].landmarks), 512
        )

    def test_full_timing_multiple_assets_and_current_capacity(self) -> None:
        expected = document_wire(512)
        for index in range(1, 4):
            expected["editor"]["assets"].append(
                dict(expected["editor"]["assets"][0], asset_id=f"video.{index}")
            )
            expected["media"].append(dict(expected["media"][0], asset_id=f"video.{index}"))
        encoded = json.dumps(expected, separators=(",", ":")).encode()
        self.assertLess(len(encoded), 2 * 1024 * 1024)
        document = self.module().decode_project_document(encoded)
        self.assertEqual(document.to_wire(), expected)
        self.assertEqual(
            document.fresh_authoring("project.fresh", "authoring-fresh").edit_capacity_frames, 3600
        )
        expected["editor"]["edit_capacity_frames"] = 1024
        with self.assertRaises(ValueError):
            self.decode(expected)

    def test_empty_document_remains_editable(self) -> None:
        expected = document_wire()
        expected["production"] = {"segments": [], "selection": []}
        expected["editor"] = None
        expected["media"] = []
        document = self.decode(expected)
        self.assertEqual(document.to_wire(), expected)
        state = document.fresh_authoring("project.new", "authoring-new")
        self.assertEqual(state.clips, ())
        self.assertEqual(state.tracks[0].kind, "primary_video")

    def test_maximum_tracks_clips_segments_selection_and_planning(self) -> None:
        from comfyui_h3_context.core.segment_workspace import MAX_WORKSPACE_SEGMENTS

        expected = document_wire(512)
        editor = expected["editor"]
        editor["tracks"] += [
            {
                "track_id": f"track.overlay.{n}",
                "kind": "video_overlay",
                "order": n,
                "enabled": n % 2 == 0,
                "locked": n % 2 == 1,
            }
            for n in range(1, 8)
        ]
        editor["clips"] = [
            dict(editor["clips"][0], clip_id=f"clip.{n:03}", start_frame=n * 24) for n in range(128)
        ]
        segment = expected["production"]["segments"][0]
        expected["production"]["segments"] = [
            dict(segment, segment_id=f"segment.{n:03}") for n in range(MAX_WORKSPACE_SEGMENTS)
        ]
        expected["production"]["selection"] = [
            row["segment_id"] for row in expected["production"]["segments"]
        ]
        expected["planning"]["shots"] = [
            {
                "shot_id": f"shot.{n}",
                "ordinal": n + 1,
                "start_milliseconds": n * 1000,
                "end_milliseconds": (n + 1) * 1000,
                "text": f"Synthetic shot {n}",
                "hard_boundary": n % 2 == 0,
            }
            for n in range(32)
        ]
        self.assertEqual(self.decode(expected).to_wire(), expected)
        for key, row in (
            ("tracks", dict(editor["tracks"][-1], track_id="track.overflow", order=8)),
            ("clips", dict(editor["clips"][-1], clip_id="clip.overflow")),
        ):
            bad = copy.deepcopy(expected)
            bad["editor"][key].append(row)
            with self.subTest(field=key), self.assertRaises(ValueError):
                self.decode(bad)

    def test_text_font_overlay_transition_and_soundtrack_intent_round_trip(self) -> None:
        expected = document_wire()
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/m25_10_composition_contract_v1.json").read_text()
        )["snapshot"]
        editor = expected["editor"]
        for key in ("assets", "tracks", "clips", "audio_extension"):
            editor[key] = fixture[key]
        editor["reference"] = {
            "sources": [
                {"source_id": "vid-primary", "kind": "video", "duration_milliseconds": 3000},
                {"source_id": "audio.intent", "kind": "audio", "duration_milliseconds": 3000},
            ],
            "soundtracks": [
                {
                    "video_id": "vid-primary",
                    "intent": "included",
                    "soundtrack_source_id": "audio.intent",
                }
            ],
        }
        expected["media"] = [
            {"asset_id": key, "content_fingerprint": None, "byte_length": None, "retained_id": None}
            for key in ("vid-primary", "vid-overlay", "img-overlay", "audio.intent")
        ]
        self.assertEqual(self.decode(expected).to_wire(), expected)

    def test_untrusted_versions_unknown_keys_locator_grants_and_wrong_types_refused(self) -> None:
        module = self.module()
        cases = []
        for field, value in (
            ("schema_version", 2),
            ("schema_version", True),
            ("format", "workflow"),
        ):
            bad = document_wire()
            bad[field] = value
            cases.append(bad)
        for field in (
            "path",
            "url",
            "receipt",
            "credentials",
            "owner_id",
            "provider_payload",
            "workspace_handle",
        ):
            bad = document_wire()
            bad["media"][0][field] = "unsafe"
            cases.append(bad)
        bad = document_wire()
        bad["editor"]["clips"][0]["audio"]["gain_mb"] = True
        cases.append(bad)
        bad = document_wire()
        bad["production"]["segments"][0]["reference_asset_ids"] = ["https://example.invalid/video"]
        cases.append(bad)
        for candidate in cases:
            with self.subTest(value=candidate), self.assertRaises(module.ProjectDocumentError):
                self.decode(candidate)

    def test_duplicate_depth_byte_and_finite_json_limits(self) -> None:
        module = self.module()
        payloads = [
            b'{"format":"h3proj","format":"h3proj"}',
            b'{"x":NaN}',
            b"[" * 33 + b"0" + b"]" * 33,
            b" " * (2 * 1024 * 1024 + 1),
        ]
        for data in payloads:
            with self.subTest(size=len(data)), self.assertRaises(module.ProjectDocumentError):
                module.decode_project_document(data)

    def test_invalid_relations_ranges_media_and_partial_integrity_refused(self) -> None:
        module = self.module()
        cases = []
        bad = document_wire()
        bad["production"]["segments"][0]["predecessor_segment_id"] = "segment.missing"
        cases.append(bad)
        bad = document_wire()
        bad["editor"]["clips"][0]["duration_frames"] = 25
        cases.append(bad)
        bad = document_wire()
        bad["media"][0]["byte_length"] = None
        cases.append(bad)
        bad = document_wire()
        bad["media"].append(copy.deepcopy(bad["media"][0]))
        cases.append(bad)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(module.ProjectDocumentError):
                self.decode(value)


if __name__ == "__main__":
    unittest.main()
