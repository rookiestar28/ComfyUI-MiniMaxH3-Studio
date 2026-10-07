"""M1-04 asset/reference registry contract tests."""

from __future__ import annotations

import ast
import json
import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

from comfyui_h3_context.core import (
    CURRENT_SCHEMA_VERSION,
    AssetDescriptor,
    AssetRole,
    EvidenceLevel,
    H3ContextContract,
    MediaKind,
    ModelVariant,
    ProfileIdentity,
    PromptProfile,
    ProviderIdentity,
    RawContextRequest,
    TaskMode,
    normalize_request,
)
from comfyui_h3_context.core.registry import (
    BackendLabel,
    BackendLabelKind,
    BackendTarget,
    MediaMetadata,
    ReferenceAsset,
    ReferenceRegistry,
    ReferenceRegistryError,
    build_reference_registry,
)

ROOT = Path(__file__).resolve().parents[1]


def image(asset_id: str, order: int) -> ReferenceAsset:
    return ReferenceAsset(
        asset_id,
        MediaKind.IMAGE,
        AssetRole.REFERENCE,
        order,
        MediaMetadata(width=1920, height=1080),
    )


def video(asset_id: str, order: int) -> ReferenceAsset:
    return ReferenceAsset(
        asset_id,
        MediaKind.VIDEO,
        AssetRole.REFERENCE,
        order,
        MediaMetadata(duration_seconds=Decimal("2.50"), frame_count=61),
    )


def paired_audio(asset_id: str, order: int, video_id: str) -> ReferenceAsset:
    return ReferenceAsset(
        asset_id,
        MediaKind.AUDIO,
        AssetRole.AUDIO_SOURCE,
        order,
        MediaMetadata(duration_seconds=Decimal("2.50"), sample_rate=32000, channels=2),
        paired_video_id=video_id,
    )


def standalone_audio(asset_id: str, order: int) -> ReferenceAsset:
    return ReferenceAsset(
        asset_id,
        MediaKind.AUDIO,
        AssetRole.AUDIO_SOURCE,
        order,
        MediaMetadata(sample_rate=44100, channels=2),
    )


def sample_assets() -> tuple[ReferenceAsset, ...]:
    return (
        image("picture_a", 1),
        image("picture_b", 2),
        paired_audio("audio_pair", 3, "video_a"),
        video("video_a", 4),
        video("video_b", 5),
        standalone_audio("audio_standalone", 6),
    )


class RegistryTests(unittest.TestCase):
    def test_native_reference_order_and_independent_label_ordinals(self) -> None:
        registry = build_reference_registry(sample_assets())

        self.assertEqual(
            [item.asset_id for item in registry.assets],
            ["picture_a", "picture_b", "audio_pair", "video_a", "video_b", "audio_standalone"],
        )
        self.assertEqual(
            [(item.asset_id, item.label) for item in registry.labels],
            [
                ("picture_a", "<Picture 1>"),
                ("picture_b", "<Picture 2>"),
                ("audio_pair", "<Audio 1>"),
                ("video_a", "<Video 1>"),
                ("video_b", "<Video 2>"),
                ("audio_standalone", "<Audio 2>"),
            ],
        )
        self.assertEqual(
            registry.label_for("video_a"),
            BackendLabel(
                BackendTarget.COMFYUI_H3, BackendLabelKind.VIDEO, 1, "video_a", "<Video 1>"
            ),
        )
        self.assertEqual(registry.to_asset_descriptors()[0].asset_id, "picture_a")

    def test_metadata_and_canonical_values_are_immutable_and_wire_safe(self) -> None:
        registry = build_reference_registry(sample_assets())
        metadata = registry.assets[0].metadata
        assert metadata is not None
        self.assertEqual(metadata.to_wire()["width"], 1920)
        with self.assertRaises(FrozenInstanceError):
            registry.assets[0].asset_id = "changed"  # type: ignore[misc]
        encoded = json.dumps(registry.to_wire(), ensure_ascii=False, sort_keys=True)
        self.assertIn("<Picture 1>", encoded)
        self.assertNotIn("path", encoded.lower())

    def test_empty_registry_is_valid_and_unknown_lookup_fails(self) -> None:
        registry = ReferenceRegistry.empty()
        self.assertEqual(registry.assets, ())
        self.assertEqual(registry.labels, ())
        with self.assertRaises(ReferenceRegistryError):
            registry.label_for("missing")

    def test_duplicate_missing_ambiguous_and_reordered_inputs_fail_closed(self) -> None:
        assets = sample_assets()
        with self.assertRaises(ReferenceRegistryError):
            build_reference_registry((assets[0], assets[0], *assets[2:]))
        with self.assertRaises(ReferenceRegistryError):
            build_reference_registry(
                (assets[0], assets[1], assets[2], video("video_a", 4), video("video_b", 4))
            )
        with self.assertRaises(ReferenceRegistryError):
            build_reference_registry(
                (assets[0], assets[1], video("video_a", 3), assets[2], assets[4], assets[5])
            )
        with self.assertRaises(ReferenceRegistryError):
            build_reference_registry(
                (assets[0], assets[1], assets[3], assets[2], assets[4], assets[5])
            )

    def test_group_order_and_explicit_pairing_are_enforced(self) -> None:
        assets = sample_assets()
        with self.assertRaises(ReferenceRegistryError):
            build_reference_registry(
                (assets[0], assets[1], assets[3], assets[4], assets[2], assets[5])
            )
        with self.assertRaises(ReferenceRegistryError):
            build_reference_registry(
                (
                    assets[0],
                    assets[1],
                    paired_audio("orphan", 3, "missing_video"),
                    assets[3],
                    assets[4],
                    assets[5],
                )
            )
        with self.assertRaises(ReferenceRegistryError):
            build_reference_registry(
                (
                    assets[0],
                    assets[1],
                    paired_audio("pair_a", 3, "video_a"),
                    paired_audio("pair_b", 4, "video_a"),
                    assets[3],
                    assets[4],
                    assets[5],
                )
            )

    def test_cardinality_and_metadata_limits_fail_closed(self) -> None:
        nine_images = tuple(image(f"image_{index}", index) for index in range(1, 10))
        self.assertEqual(len(build_reference_registry(nine_images).labels), 9)
        with self.assertRaises(ReferenceRegistryError):
            build_reference_registry(nine_images + (image("image_10", 10),))

        with self.assertRaises(ReferenceRegistryError):
            ReferenceAsset(
                "bad_image",
                MediaKind.IMAGE,
                AssetRole.REFERENCE,
                1,
                MediaMetadata(width=0),
            )
        with self.assertRaises(ReferenceRegistryError):
            MediaMetadata(duration_seconds=Decimal("NaN"))

        videos = tuple(video(f"video_{index}", index) for index in range(1, 4))
        self.assertEqual(len(build_reference_registry(videos).labels), 3)
        with self.assertRaises(ReferenceRegistryError):
            build_reference_registry(videos + (video("video_4", 4),))

        paired = tuple(
            item
            for index in range(1, 4)
            for item in (
                paired_audio(f"pair_{index}", 2 * index - 1, f"video_{index}"),
                video(f"video_{index}", 2 * index),
            )
        )
        self.assertEqual(len(build_reference_registry(paired).labels), 6)
        with self.assertRaises(ReferenceRegistryError):
            build_reference_registry(
                paired + (paired_audio("pair_4", 7, "video_4"), video("video_4", 8))
            )

        standalone = tuple(standalone_audio(f"audio_{index}", index) for index in range(1, 4))
        self.assertEqual(len(build_reference_registry(standalone).labels), 3)
        with self.assertRaises(ReferenceRegistryError):
            build_reference_registry(standalone + (standalone_audio("audio_4", 4),))

    def test_contract_and_request_carry_registry_without_optional_dependencies(self) -> None:
        registry = build_reference_registry((image("picture_a", 1),))
        descriptors = registry.to_asset_descriptors()
        contract = H3ContextContract(
            CURRENT_SCHEMA_VERSION,
            ProfileIdentity(PromptProfile.FULL_REFERENCE, CURRENT_SCHEMA_VERSION),
            TaskMode.REF2VA,
            ModelVariant.BASE_REF2VA,
            EvidenceLevel.FRAMEWORK_REFERENCE,
            ProviderIdentity.MANUAL,
            assets=descriptors,
            reference_registry=registry,
        )
        self.assertEqual(contract.to_wire()["reference_registry"], registry.to_wire())

        result = normalize_request(
            RawContextRequest(
                TaskMode.REF2VA,
                "Use the image",
                reference_registry=registry,
            )
        )
        self.assertTrue(result.is_valid)
        assert result.request is not None
        self.assertEqual(result.request.reference_registry, registry)
        self.assertEqual(result.request.assets, descriptors)

        mismatch = normalize_request(
            RawContextRequest(
                TaskMode.REF2VA,
                "Use the image",
                assets=(AssetDescriptor("other", MediaKind.IMAGE, AssetRole.REFERENCE),),
                reference_registry=registry,
            )
        )
        self.assertFalse(mismatch.is_valid)
        self.assertIn("asset_registry_mismatch", {item.code for item in mismatch.diagnostics})

        with self.assertRaises(ValueError):
            H3ContextContract(
                CURRENT_SCHEMA_VERSION,
                ProfileIdentity(PromptProfile.FULL_REFERENCE, CURRENT_SCHEMA_VERSION),
                TaskMode.REF2VA,
                ModelVariant.BASE_REF2VA,
                EvidenceLevel.FRAMEWORK_REFERENCE,
                ProviderIdentity.MANUAL,
                assets=(AssetDescriptor("other", MediaKind.IMAGE, AssetRole.REFERENCE),),
                reference_registry=registry,
            )

    def test_registry_module_has_no_optional_runtime_imports(self) -> None:
        module = ast.parse(
            (ROOT / "comfyui_h3_context" / "core" / "registry.py").read_text(encoding="utf-8")
        )
        optional_roots = {"comfy", "torch", "torchaudio", "requests", "httpx", "PIL", "numpy"}
        imported: set[str] = set()
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue(optional_roots.isdisjoint(imported))

    def test_schema_declares_registry_and_backend_label_artifacts(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "h3_context_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("reference_registry", schema["required"])
        self.assertEqual(
            schema["properties"]["reference_registry"]["$ref"],
            "#/$defs/ReferenceRegistry",
        )
        self.assertIn("BackendLabel", schema["$defs"])


if __name__ == "__main__":
    unittest.main()
