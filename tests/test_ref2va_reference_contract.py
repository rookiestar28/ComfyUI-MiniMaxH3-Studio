"""Shared Ref2VA capacity/label contracts and mixed-audio physical slot regression."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from comfyui_h3_context.core import ContextReport, NativeH3AdapterError, ReferenceRegistry
from comfyui_h3_context.core.native_h3 import (
    _h3_base_preflight,
    _reference_bindings,
)
from comfyui_h3_context.nodes import H3ReferenceRegistryNode, ReferenceRegistryNodeError

FIXTURES = Path(__file__).resolve().parent / "fixtures"


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
