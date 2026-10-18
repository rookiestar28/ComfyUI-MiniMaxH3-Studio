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


class DottedReferenceInputTests(unittest.TestCase):
    def validation_error(self, **values: object) -> str:
        result = H3ReferenceRegistryNode.VALIDATE_INPUTS(**values)
        if not isinstance(result, str):
            self.fail("invalid reference inputs must produce a typed validation error")
        return result

    def test_dotted_inputs_follow_ordinal_and_explicit_container_order(self) -> None:
        images = [NonIterableMedia() for _ in range(3)]
        videos = [NonIterableMedia() for _ in range(3)]
        audio = {"waveform": NonIterableMedia(), "sample_rate": 32_000}
        captured = []
        children = {
            "images.image8": (images[1], images[2]),
            "videos.video2": videos[2],
            "audios.audio2": audio,
            "images.image1": [images[0]],
            "paired_audios.paired_audio0": [object(), object()],
            "videos.video0": videos[:2],
        }
        with mock.patch.object(
            context_request_nodes,
            "capture_process_authoring_sources",
            side_effect=lambda **values: captured.append(values),
        ):
            registry = H3ReferenceRegistryNode().build_registry(**children)[0]
            self.assertIs(H3ReferenceRegistryNode.VALIDATE_INPUTS(**children), True)
        self.assertEqual(
            [(asset.asset_id, asset.paired_video_id) for asset in registry.assets],
            [
                ("image_1", None),
                ("image_2", None),
                ("image_3", None),
                ("audio_pair_1", "video_1"),
                ("video_1", None),
                ("audio_pair_2", "video_2"),
                ("video_2", None),
                ("video_3", None),
                ("audio_1", None),
            ],
        )
        self.assertEqual([asset.connection_order for asset in registry.assets], list(range(1, 10)))
        self.assertEqual(len(captured), 1)
        self.assertIs(captured[0]["exact_registry"], registry)
        self.assertEqual(
            captured[0]["sources"],
            tuple((f"video_{i}", MediaKind.VIDEO, value) for i, value in enumerate(videos, 1))
            + tuple((f"image_{i}", MediaKind.IMAGE, value) for i, value in enumerate(images, 1)),
        )
        self.assertNotIn("waveform", json.dumps(registry.to_wire()))

    def test_dense_children_accept_all_existing_per_kind_maxima(self) -> None:
        for prefix in ("paired_audios.paired_audio", "audios.audio"):
            with self.subTest(audio_socket=prefix):
                children = {
                    **{f"images.image{i}": [NonIterableMedia()] for i in range(9)},
                    **{f"videos.video{i}": (NonIterableMedia(),) for i in range(3)},
                    **{f"{prefix}{i}": NonIterableMedia() for i in range(3)},
                }
                registry = H3ReferenceRegistryNode().build_registry(**children)[0]
                self.assertEqual(len(registry.assets), 15)
                self.assertEqual(
                    [label.label for label in registry.labels][:9],
                    [f"<Picture {i}>" for i in range(1, 10)],
                )
                self.assertIs(H3ReferenceRegistryNode.VALIDATE_INPUTS(**children), True)

    def test_only_finite_canonical_child_names_are_accepted_without_value_disclosure(self) -> None:
        names = (
            "images.image9",
            "images.image00",
            "images.image-1",
            "images.image+1",
            "images.image1.extra",
            "images.image1\n",
            "images.image١",
            "images.image",
            "videos.video3",
            "paired_audios.paired_audio3",
            "audios.audio3",
            "first_frame.image0",
            "images.other0",
            "unknown",
            "images.image" + "9" * 1000,
        )
        opaque_value = "VALUE_MUST_NOT_APPEAR_IN_DIAGNOSTICS"
        for name in names:
            with self.subTest(name=name):
                with self.assertRaises(ReferenceRegistryNodeError) as raised:
                    H3ReferenceRegistryNode().build_registry(**{name: opaque_value})
                self.assertEqual(raised.exception.diagnostics[0].code, "unknown_reference_input")
                self.assertIn(repr(name), str(raised.exception))
                self.assertNotIn(opaque_value, str(raised.exception))
                result = self.validation_error(first_frame=(None,), **{name: (None,)})
                self.assertIsInstance(result, str)
                self.assertIn("unknown_reference_input", result)

    def test_same_group_mixed_spelling_is_refused_before_placeholder_deferral(self) -> None:
        for group, child in (
            ("images", "images.image0"),
            ("videos", "videos.video0"),
            ("paired_audios", "paired_audios.paired_audio0"),
            ("audios", "audios.audio0"),
        ):
            grouped_values: tuple[object, ...] = (NonIterableMedia(), [], (), (None,))
            for grouped_value in grouped_values:
                with self.subTest(group=group, value_type=type(grouped_value).__name__):
                    values = {group: grouped_value, child: (None,)}
                    result = self.validation_error(**values)
                    self.assertIsInstance(result, str)
                    self.assertIn("ambiguous_reference_spelling", result)
                    with self.assertRaises(ReferenceRegistryNodeError) as raised:
                        H3ReferenceRegistryNode().build_registry(**values)
                    self.assertEqual(
                        raised.exception.diagnostics[0].code, "ambiguous_reference_spelling"
                    )

    def test_grouped_none_is_omitted_and_other_groups_keep_legacy_spelling(self) -> None:
        registry = H3ReferenceRegistryNode().build_registry(
            images=None,
            videos=[NonIterableMedia()],
            paired_audios=(NonIterableMedia(),),
            **{"images.image8": NonIterableMedia(), "audios.audio2": NonIterableMedia()},
        )[0]
        self.assertEqual(
            [asset.asset_id for asset in registry.assets],
            ["image_1", "audio_pair_1", "video_1", "audio_1"],
        )
        self.assertEqual(
            H3ReferenceRegistryNode().build_registry(images=[], audios=())[0].assets, ()
        )

    def test_child_cardinality_is_bounded_after_concatenation(self) -> None:
        cases = (
            ("images.image", 9, "too_many_images"),
            ("videos.video", 3, "too_many_videos"),
            ("audios.audio", 3, "too_many_audio"),
            ("paired_audios.paired_audio", 3, "too_many_paired_audio"),
        )
        for prefix, maximum, code in cases:
            with self.subTest(prefix=prefix):
                values = {
                    prefix + "0": [NonIterableMedia()] * maximum,
                    prefix + "1": NonIterableMedia(),
                }
                if prefix.startswith("paired_audios"):
                    values.update(
                        {
                            "videos.video0": [NonIterableMedia()] * 2,
                            "videos.video1": NonIterableMedia(),
                        }
                    )
                with self.assertRaises(ReferenceRegistryNodeError) as raised:
                    H3ReferenceRegistryNode().build_registry(**values)
                self.assertEqual(raised.exception.diagnostics[0].code, code)

    def test_present_children_cannot_be_missing_empty_or_nested_media(self) -> None:
        cases: tuple[tuple[object, str], ...] = (
            (None, "missing_reference_media"),
            ([], "missing_reference_media"),
            ((), "missing_reference_media"),
            ([None], "missing_reference_media"),
            ((None,), "missing_reference_media"),
            ([[NonIterableMedia()]], "ambiguous_nested_reference_input"),
        )
        for value, code in cases:
            with self.subTest(code=code, value_type=type(value).__name__):
                with self.assertRaises(ReferenceRegistryNodeError) as raised:
                    H3ReferenceRegistryNode().build_registry(**{"images.image0": value})
                self.assertEqual(raised.exception.diagnostics[0].code, code)

    def test_pairing_child_ordinals_must_be_a_prefix_even_when_unresolved(self) -> None:
        for paired in (0, 2):
            children = {
                "videos.video1": (None,),
                "videos.video2": (None,),
                f"paired_audios.paired_audio{paired}": (None,),
            }
            with self.subTest(paired=paired):
                result = self.validation_error(first_frame=(None,), **children)
                self.assertIsInstance(result, str)
                self.assertIn("paired_audio_ordinal_mismatch", result)
        self.assertIn(
            "paired_audio_ordinal_mismatch",
            self.validation_error(**{"paired_audios.paired_audio0": (None,)}),
        )
        self.assertIs(
            H3ReferenceRegistryNode.VALIDATE_INPUTS(
                **{
                    "videos.video1": (None,),
                    "videos.video2": (None,),
                    "paired_audios.paired_audio1": (None,),
                }
            ),
            True,
        )

    def test_expanded_pairing_never_crosses_child_boundaries(self) -> None:
        video_children = {
            "videos.video0": [NonIterableMedia(), NonIterableMedia()],
            "videos.video2": NonIterableMedia(),
        }
        cases = (
            {
                **video_children,
                "paired_audios.paired_audio0": NonIterableMedia(),
                "paired_audios.paired_audio2": NonIterableMedia(),
            },
            {
                "videos.video0": NonIterableMedia(),
                "paired_audios.paired_audio0": [NonIterableMedia(), NonIterableMedia()],
            },
        )
        for children in cases:
            with self.subTest(keys=tuple(children)):
                with self.assertRaises(ReferenceRegistryNodeError) as raised:
                    H3ReferenceRegistryNode().build_registry(**children)
                self.assertEqual(
                    raised.exception.diagnostics[0].code, "paired_audio_ordinal_mismatch"
                )
                self.assertIn(
                    "paired_audio_ordinal_mismatch",
                    self.validation_error(**children),
                )
        for children, pairs in (
            ({**video_children, "paired_audios.paired_audio0": NonIterableMedia()}, 1),
            (
                {
                    **video_children,
                    "paired_audios.paired_audio0": [NonIterableMedia(), NonIterableMedia()],
                    "paired_audios.paired_audio2": NonIterableMedia(),
                },
                3,
            ),
            ({**video_children, "paired_audios": [NonIterableMedia(), NonIterableMedia()]}, 2),
        ):
            with self.subTest(pairs=pairs):
                registry = H3ReferenceRegistryNode().build_registry(**children)[0]
                self.assertEqual(
                    [asset.paired_video_id for asset in registry.assets if asset.paired_video_id],
                    [f"video_{i}" for i in range(1, pairs + 1)],
                )

    def test_paired_children_with_grouped_videos_use_resolved_position(self) -> None:
        registry = H3ReferenceRegistryNode().build_registry(
            videos=[NonIterableMedia(), NonIterableMedia()],
            **{
                "paired_audios.paired_audio0": NonIterableMedia(),
                "paired_audios.paired_audio1": NonIterableMedia(),
            },
        )[0]
        self.assertEqual(
            [asset.paired_video_id for asset in registry.assets if asset.paired_video_id],
            ["video_1", "video_2"],
        )
        result = self.validation_error(
            videos=[NonIterableMedia()], **{"paired_audios.paired_audio1": (None,)}
        )
        self.assertIn("paired_audio_ordinal_mismatch", result)
        self.assertIs(
            H3ReferenceRegistryNode.VALIDATE_INPUTS(
                videos=(None,), **{"paired_audios.paired_audio2": (None,)}
            ),
            True,
        )

    def test_dotted_validation_defers_only_the_exact_host_sentinel_without_capture(self) -> None:
        with mock.patch.object(
            context_request_nodes, "capture_process_authoring_sources"
        ) as capture:
            self.assertIs(
                H3ReferenceRegistryNode.VALIDATE_INPUTS(**{"images.image0": (None,)}), True
            )
            for value in ([None], (None, NonIterableMedia())):
                self.assertIn(
                    "missing_reference_media",
                    self.validation_error(**{"images.image0": value}),
                )
            self.assertIs(
                H3ReferenceRegistryNode.VALIDATE_INPUTS(
                    last_frame=(None,), **{"images.image0": NonIterableMedia()}
                ),
                True,
            )
            capture.assert_not_called()


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
