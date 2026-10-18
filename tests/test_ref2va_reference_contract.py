"""Shared Ref2VA capacity/label contracts and mixed-audio physical slot regression."""

from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from comfyui_h3_context import context_request_nodes
from comfyui_h3_context.core import ContextReport, NativeH3AdapterError, ReferenceRegistry
from comfyui_h3_context.core.contracts import AssetRole, MediaKind, TaskMode
from comfyui_h3_context.core.native_h3 import (
    NATIVE_H3_BASE_MAX_AUDIO,
    _h3_base_preflight,
    _reference_bindings,
)
from comfyui_h3_context.core.normalization import (
    MAX_REFERENCE_AUDIO,
    RawContextRequest,
    normalize_request,
)
from comfyui_h3_context.core.reference_set_authoring import (
    AddSource,
    AdmittedSourceInput,
    ReferenceSetAuthoringError,
    ReferenceSetState,
    TimedReferenceLimits,
    apply_command,
    build_h3_base_capacity,
    capacity_projection,
    create_reference_set,
)
from comfyui_h3_context.core.registry import (
    MAX_PAIRED_VIDEO_AUDIO,
    MAX_STANDALONE_AUDIO,
    MediaMetadata,
    ReferenceAsset,
    ReferenceRegistryError,
    build_reference_registry,
)
from comfyui_h3_context.nodes import H3ReferenceRegistryNode, ReferenceRegistryNodeError

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _assets(paired: int, standalone: int) -> tuple[ReferenceAsset, ...]:
    assets = [ReferenceAsset("image", MediaKind.IMAGE, AssetRole.REFERENCE, 1)]
    for index in range(paired):
        video_id = f"video-{index}"
        assets.append(
            ReferenceAsset(
                f"paired-{index}",
                MediaKind.AUDIO,
                AssetRole.AUDIO_SOURCE,
                len(assets) + 1,
                MediaMetadata(duration_seconds=Decimal("2")),
                paired_video_id=video_id,
            )
        )
        assets.append(
            ReferenceAsset(
                video_id,
                MediaKind.VIDEO,
                AssetRole.REFERENCE,
                len(assets) + 1,
                MediaMetadata(duration_seconds=Decimal("2")),
            )
        )
    for index in range(standalone):
        assets.append(
            ReferenceAsset(
                f"audio-{index}",
                MediaKind.AUDIO,
                AssetRole.AUDIO_SOURCE,
                len(assets) + 1,
                MediaMetadata(duration_seconds=Decimal("2")),
            )
        )
    return tuple(assets)


def _authoring(row: dict[str, Any]) -> ReferenceSetState:
    state = create_reference_set(
        build_h3_base_capacity(
            authority="test.capacity",
            fingerprint="sha256:" + "a" * 64,
            timed=TimedReferenceLimits(max_duration_milliseconds=15000, max_frames=360),
        )
    )
    counts = (
        (MediaKind.IMAGE, row["images"]),
        (MediaKind.VIDEO, row["videos"]),
        (MediaKind.AUDIO, row["audio_total"]),
    )
    for kind, count in counts:
        for index in range(count):
            source = AdmittedSourceInput(
                source_id=f"{kind.value}-{index}",
                kind=kind,
                fingerprint="sha256:" + "b" * 64,
                duration_milliseconds=None if kind is MediaKind.IMAGE else 2000,
            )
            state, _ = apply_command(state, AddSource(state.revision, source))
    return state


class ReferenceAudioAdmissionTests(unittest.TestCase):
    def test_audio_constants_share_the_native_total(self) -> None:
        from comfyui_h3_context.core import reference_set_authoring
        from comfyui_h3_context.core.reference_limits import H3_BASE_MAX_AUDIO_REFERENCES

        self.assertEqual(H3_BASE_MAX_AUDIO_REFERENCES, NATIVE_H3_BASE_MAX_AUDIO)
        authoring_max = vars(reference_set_authoring)["H3_BASE_MAX_AUDIO_REFERENCES"]
        self.assertEqual(authoring_max, NATIVE_H3_BASE_MAX_AUDIO)
        self.assertEqual(MAX_REFERENCE_AUDIO, NATIVE_H3_BASE_MAX_AUDIO)
        self.assertEqual(MAX_PAIRED_VIDEO_AUDIO, NATIVE_H3_BASE_MAX_AUDIO)
        self.assertEqual(MAX_STANDALONE_AUDIO, NATIVE_H3_BASE_MAX_AUDIO)

    def test_every_paired_standalone_mix_is_refused_at_first_admission(self) -> None:
        for paired in range(4):
            for standalone in range(5):
                with self.subTest(paired=paired, standalone=standalone):
                    assets = _assets(paired, standalone)
                    result = normalize_request(
                        RawContextRequest(
                            mode=TaskMode.REF2VA,
                            user_intent="a scene",
                            assets=tuple(asset.to_descriptor() for asset in assets),
                        )
                    )
                    if paired + standalone <= 3:
                        self.assertIsNotNone(result.request)
                        self.assertEqual(build_reference_registry(assets).assets, assets)
                    else:
                        self.assertIsNone(result.request)
                        self.assertIn("too_many_audio", {d.code for d in result.diagnostics})
                        with self.assertRaises(ReferenceRegistryError) as raised:
                            build_reference_registry(assets)
                        self.assertEqual(getattr(raised.exception, "code", None), "too_many_audio")

    def test_node_preserves_total_audio_refusal_before_runtime_capture(self) -> None:
        values = {"images": 0, "videos": 2, "audios": 2, "soundtrack": "included"}
        with mock.patch.object(
            context_request_nodes, "capture_process_authoring_sources"
        ) as capture:
            with self.assertRaises(ReferenceRegistryNodeError) as raised:
                _registry(values)
            diagnostic = H3ReferenceRegistryNode.VALIDATE_INPUTS(
                videos=[object(), object()],
                paired_audios=[object(), object()],
                audios=[object(), object()],
            )
            self.assertIn("too_many_audio", str(diagnostic))
            capture.assert_not_called()
        self.assertEqual([d.code for d in raised.exception.diagnostics], ["too_many_audio"])

    def test_authoring_matches_every_shared_capacity_fixture_row(self) -> None:
        fixture = json.loads((FIXTURES / "ref2va_reference_capacity_v1.json").read_bytes())
        for row in fixture["cases"]:
            with self.subTest(case=row["name"]):
                try:
                    _authoring(row)
                    admitted = True
                except ReferenceSetAuthoringError:
                    admitted = False
                self.assertEqual(admitted, row["capacity_allowed"])

    def test_audio_capacity_projection_does_not_advertise_a_fourth_file(self) -> None:
        state = _authoring({"images": 1, "videos": 0, "audio_total": 3})
        self.assertEqual(capacity_projection(state).standalone_audio_remaining, 0)
        self.assertEqual(capacity_projection(state).paired_audio_remaining, 0)

    def test_native_preflight_keeps_its_defense_in_depth_refusal(self) -> None:
        report = cast(
            ContextReport,
            SimpleNamespace(
                request=SimpleNamespace(
                    requested_duration_seconds=5.0,
                    effective_duration_seconds=5.0,
                    reference_registry=SimpleNamespace(assets=_assets(2, 2)),
                )
            ),
        )
        with self.assertRaises(NativeH3AdapterError) as raised:
            _h3_base_preflight(report)
        self.assertEqual(raised.exception.code, "h3_base_audio_reference_limit")


def _registry(row: dict[str, Any]) -> ReferenceRegistry:
    values = {
        **{f"images.image{i}": object() for i in range(row["images"])},
        **{f"videos.video{i}": object() for i in range(row["videos"])},
        **{f"audios.audio{i}": object() for i in range(row["audios"])},
    }
    if row["soundtrack"] == "included":
        values.update({f"paired_audios.paired_audio{i}": object() for i in range(row["videos"])})
    return H3ReferenceRegistryNode().build_registry(**values)[0]


class ReferenceSharedContractTests(unittest.TestCase):
    def test_label_fixture_matches_registry_asset_order(self) -> None:
        fixture = json.loads((FIXTURES / "ref2va_reference_labels_v1.json").read_bytes())
        for row in fixture["cases"]:
            with self.subTest(case=row["name"]):
                registry = _registry(row)
                self.assertEqual(
                    [label.label for label in registry.labels], row["labels"]["ordered"]
                )

    def test_capacity_fixture_preserves_native_audio_only_support(self) -> None:
        fixture = json.loads((FIXTURES / "ref2va_reference_capacity_v1.json").read_bytes())
        for row in fixture["cases"]:
            with self.subTest(case=row["name"]):
                passed = True
                try:
                    registry = _registry(row)
                    report = cast(
                        ContextReport,
                        SimpleNamespace(
                            request=SimpleNamespace(
                                requested_duration_seconds=5.0,
                                effective_duration_seconds=5.0,
                                reference_registry=registry,
                            )
                        ),
                    )
                    _h3_base_preflight(report)
                except (ReferenceRegistryNodeError, NativeH3AdapterError):
                    passed = False
                self.assertEqual(passed, row["capacity_allowed"])

    def test_standalone_native_slots_do_not_count_paired_soundtracks(self) -> None:
        registry = _registry({"images": 1, "videos": 1, "audios": 2, "soundtrack": "included"})
        standalone = [
            binding
            for binding in _reference_bindings(registry)
            if binding.native_input == "ref_audios"
        ]
        self.assertEqual([binding.label for binding in standalone], ["<Audio 2>", "<Audio 3>"])
        self.assertEqual(
            [binding.native_slot for binding in standalone], ["ref_audio_0", "ref_audio_1"]
        )
