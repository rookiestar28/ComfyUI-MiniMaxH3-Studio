"""M3-03 H3 Reference Registry V1 node adapter tests."""

from __future__ import annotations

import ast
import json
import math
import unittest
from pathlib import Path
from unittest import mock

import comfyui_h3_context.context_request_nodes as context_request_nodes
from comfyui_h3_context.core import AssetRole, MediaKind
from comfyui_h3_context.nodes import (
    REFERENCE_NODE_ID,
    REFERENCE_SOCKET_TYPE,
    H3ReferenceRegistryNode,
    ReferenceRegistryNodeError,
)

ROOT = Path(__file__).resolve().parents[1]
NODE_MODULE = ROOT / "comfyui_h3_context" / "nodes.py"


class NonIterableMedia:
    """Opaque host-like value proving that the node does not inspect media content."""

    def __iter__(self) -> object:
        raise AssertionError("reference node must not iterate opaque media")


class ReferenceNodeTests(unittest.TestCase):
    def test_v1_metadata_matches_the_frozen_contract(self) -> None:
        metadata = H3ReferenceRegistryNode.INPUT_TYPES()
        self.assertEqual(H3ReferenceRegistryNode.NODE_ID, REFERENCE_NODE_ID)
        self.assertEqual(H3ReferenceRegistryNode.RETURN_TYPES, (REFERENCE_SOCKET_TYPE,))
        self.assertEqual(H3ReferenceRegistryNode.RETURN_NAMES, ("reference_registry",))
        self.assertEqual(H3ReferenceRegistryNode.FUNCTION, "build_registry")
        self.assertEqual(H3ReferenceRegistryNode.CATEGORY, "h3_context/contracts")
        self.assertTrue(H3ReferenceRegistryNode.INPUT_IS_LIST)
        self.assertTrue(math.isnan(H3ReferenceRegistryNode.IS_CHANGED()))
        self.assertEqual(tuple(metadata), ("required", "optional"))
        self.assertEqual(metadata["required"], {})
        self.assertEqual(
            tuple(metadata["optional"]),
            ("first_frame", "last_frame", "images", "videos", "paired_audios", "audios"),
        )
        self.assertEqual(metadata["optional"]["first_frame"][0], "IMAGE")
        self.assertEqual(metadata["optional"]["last_frame"][0], "IMAGE")
        self.assertEqual(metadata["optional"]["images"][0], "IMAGE")
        self.assertEqual(metadata["optional"]["videos"][0], "VIDEO")
        self.assertEqual(metadata["optional"]["paired_audios"][0], "AUDIO")
        self.assertEqual(metadata["optional"]["audios"][0], "AUDIO")
        self.assertEqual(metadata["optional"]["first_frame"][1]["max_items"], 1)
        self.assertEqual(metadata["optional"]["last_frame"][1]["max_items"], 1)
        self.assertEqual(metadata["optional"]["images"][1]["max_items"], 9)
        self.assertEqual(metadata["optional"]["videos"][1]["max_items"], 3)
        self.assertEqual(metadata["optional"]["paired_audios"][1]["max_items"], 3)
        self.assertEqual(metadata["optional"]["audios"][1]["max_items"], 3)

    def test_scalar_and_ordered_collections_map_to_canonical_order_and_labels(self) -> None:
        image_a = NonIterableMedia()
        image_b = NonIterableMedia()
        video_a = NonIterableMedia()
        audio_a = {"waveform": object(), "sample_rate": 32_000}
        registry = H3ReferenceRegistryNode().build_registry(
            images=[image_a, image_b],
            videos=video_a,
            audios=(audio_a,),
        )[0]

        self.assertEqual(
            [
                (asset.asset_id, asset.kind, asset.role, asset.connection_order)
                for asset in registry.assets
            ],
            [
                ("image_1", MediaKind.IMAGE, AssetRole.REFERENCE, 1),
                ("image_2", MediaKind.IMAGE, AssetRole.REFERENCE, 2),
                ("video_1", MediaKind.VIDEO, AssetRole.REFERENCE, 3),
                ("audio_1", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE, 4),
            ],
        )
        self.assertEqual(
            [(label.asset_id, label.label) for label in registry.labels],
            [
                ("image_1", "<Picture 1>"),
                ("image_2", "<Picture 2>"),
                ("video_1", "<Video 1>"),
                ("audio_1", "<Audio 1>"),
            ],
        )
        self.assertTrue(all(asset.paired_video_id is None for asset in registry.assets))
        wire = registry.to_wire()
        self.assertEqual(json.loads(json.dumps(wire)), wire)
        self.assertNotIn("waveform", json.dumps(wire))

    def test_rearranged_connections_follow_documented_group_and_list_order(self) -> None:
        first = H3ReferenceRegistryNode().build_registry(
            images=[object(), object()], videos=[object()], audios=[object(), object()]
        )[0]
        second = H3ReferenceRegistryNode().build_registry(
            images=[object(), object()], videos=[object()], audios=[object(), object()]
        )[0]
        self.assertEqual(
            [(asset.asset_id, asset.connection_order) for asset in first.assets],
            [(asset.asset_id, asset.connection_order) for asset in second.assets],
        )
        self.assertEqual(
            [asset.kind for asset in first.assets],
            [MediaKind.IMAGE, MediaKind.IMAGE, MediaKind.VIDEO, MediaKind.AUDIO, MediaKind.AUDIO],
        )
        self.assertEqual([label.ordinal for label in first.labels], [1, 2, 1, 1, 2])

    def test_omitted_inputs_are_empty_but_missing_or_nested_items_fail(self) -> None:
        self.assertEqual(H3ReferenceRegistryNode().build_registry()[0].assets, ())
        invalid_cases = (
            ("missing_reference_media", {"images": [object(), None]}),
            ("ambiguous_nested_reference_input", {"images": [[object()]]}),
            ("missing_reference_media", {"videos": (None,)}),
        )
        for code, values in invalid_cases:
            with self.subTest(code=code):
                with self.assertRaises(ReferenceRegistryNodeError) as context:
                    H3ReferenceRegistryNode().build_registry(**values)
                self.assertIn(code, str(context.exception))
                self.assertTrue(any(item.code == code for item in context.exception.diagnostics))

    def test_cardinality_limits_fail_before_emitting_a_registry(self) -> None:
        cases = (
            ("images", 10, "too_many_images"),
            ("videos", 4, "too_many_videos"),
            ("audios", 4, "too_many_audio"),
        )
        for field, count, code in cases:
            with self.subTest(field=field):
                with self.assertRaises(ReferenceRegistryNodeError) as context:
                    H3ReferenceRegistryNode().build_registry(**{field: [object()] * count})
                self.assertIn(code, str(context.exception))

    def test_validation_hook_returns_actionable_error_without_fallback(self) -> None:
        node = H3ReferenceRegistryNode
        self.assertIs(node.VALIDATE_INPUTS(images=[object()]), True)
        result = node.VALIDATE_INPUTS(images=[object(), None])
        self.assertIsInstance(result, str)
        if isinstance(result, str):
            self.assertIn("missing_reference_media", result)

    def test_validation_hook_defers_only_host_unresolved_list_placeholders(self) -> None:
        node = H3ReferenceRegistryNode
        self.assertIs(node.VALIDATE_INPUTS(images=None), True)
        self.assertIs(node.VALIDATE_INPUTS(images=(None,)), True)
        self.assertIsInstance(node.VALIDATE_INPUTS(images=[None]), str)
        self.assertIsInstance(node.VALIDATE_INPUTS(images=(None, object())), str)

    def test_registry_node_does_not_infer_audio_pairing_or_read_media_values(self) -> None:
        audio = {"waveform": NonIterableMedia(), "sample_rate": 32_000}
        registry = H3ReferenceRegistryNode().build_registry(
            videos=NonIterableMedia(), audios=audio
        )[0]
        self.assertEqual(registry.assets[0].asset_id, "video_1")
        self.assertEqual(registry.assets[1].asset_id, "audio_1")
        self.assertIsNone(registry.assets[1].paired_video_id)
        self.assertEqual(registry.assets[1].role, AssetRole.AUDIO_SOURCE)

    def test_explicit_frame_and_paired_audio_roles_are_reachable_and_ordered(self) -> None:
        registry = H3ReferenceRegistryNode().build_registry(
            first_frame=NonIterableMedia(),
            last_frame=NonIterableMedia(),
            images=[NonIterableMedia()],
            videos=[NonIterableMedia(), NonIterableMedia()],
            paired_audios=[NonIterableMedia()],
        )[0]
        self.assertEqual(
            [
                (asset.asset_id, asset.role, asset.connection_order, asset.paired_video_id)
                for asset in registry.assets
            ],
            [
                ("first_frame_1", AssetRole.FIRST_FRAME, 1, None),
                ("last_frame_1", AssetRole.LAST_FRAME, 2, None),
                ("image_1", AssetRole.REFERENCE, 3, None),
                ("audio_pair_1", AssetRole.AUDIO_SOURCE, 4, "video_1"),
                ("video_1", AssetRole.REFERENCE, 5, None),
                ("video_2", AssetRole.REFERENCE, 6, None),
            ],
        )

    def test_paired_audio_without_a_video_fails_closed(self) -> None:
        with self.assertRaises(ReferenceRegistryNodeError) as context:
            H3ReferenceRegistryNode().build_registry(paired_audios=[object()])
        self.assertIn("paired_audio_without_video", str(context.exception))

    def test_execution_captures_only_ordered_video_sources_after_registry_success(self) -> None:
        captured: list[tuple[object, tuple[tuple[str, MediaKind, object], ...]]] = []

        def capture(
            *,
            exact_registry: object,
            sources: tuple[tuple[str, MediaKind, object], ...],
        ) -> None:
            captured.append((exact_registry, sources))

        video_1 = object()
        video_2 = object()
        paired_audio = object()
        standalone_audio = object()
        with mock.patch.object(
            context_request_nodes,
            "capture_process_authoring_sources",
            side_effect=capture,
            create=True,
        ):
            registry = H3ReferenceRegistryNode().build_registry(
                videos=[video_1, video_2],
                paired_audios=[paired_audio],
                audios=[standalone_audio],
            )[0]
            self.assertEqual(
                captured,
                [
                    (
                        registry,
                        (
                            ("video_1", MediaKind.VIDEO, video_1),
                            ("video_2", MediaKind.VIDEO, video_2),
                        ),
                    )
                ],
            )

            captured.clear()
            self.assertIs(H3ReferenceRegistryNode.VALIDATE_INPUTS(videos=[video_1]), True)
            self.assertEqual(captured, [])
            with self.assertRaises(ReferenceRegistryNodeError):
                H3ReferenceRegistryNode().build_registry(paired_audios=[paired_audio])
            self.assertEqual(captured, [])

    def test_repeated_execution_recaptures_the_one_time_video_receipt(self) -> None:
        video = object()
        captured: list[tuple[object, tuple[tuple[str, MediaKind, object], ...]]] = []

        def capture(
            *,
            exact_registry: object,
            sources: tuple[tuple[str, MediaKind, object], ...],
        ) -> None:
            captured.append((exact_registry, sources))

        with mock.patch.object(
            context_request_nodes,
            "capture_process_authoring_sources",
            side_effect=capture,
            create=True,
        ):
            first = H3ReferenceRegistryNode().build_registry(videos=[video])[0]
            second = H3ReferenceRegistryNode().build_registry(videos=[video])[0]

        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertEqual(
            captured,
            [
                (first, (("video_1", MediaKind.VIDEO, video),)),
                (second, (("video_1", MediaKind.VIDEO, video),)),
            ],
        )

    def test_node_module_has_no_host_or_optional_runtime_imports(self) -> None:
        tree = ast.parse(NODE_MODULE.read_text(encoding="utf-8"), filename=str(NODE_MODULE))
        forbidden = {
            "aiohttp",
            "comfy",
            "comfy_api",
            "cv2",
            "diffusers",
            "httpx",
            "moviepy",
            "numpy",
            "requests",
            "torch",
            "transformers",
        }
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".")[0])
        self.assertTrue(forbidden.isdisjoint(imports), imports)


if __name__ == "__main__":
    unittest.main()
