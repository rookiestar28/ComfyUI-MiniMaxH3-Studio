"""M3-06 native MiniMax H3 wiring adapter tests."""

from __future__ import annotations

import ast
import json
import unittest
import weakref
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from comfyui_h3_context.core import (
    NATIVE_H3_HOST_REVISION,
    NATIVE_H3_SOURCE_BLOB,
    AssetRole,
    ContextReport,
    MediaKind,
    MediaMetadata,
    NativeH3AdapterError,
    PromptRenderStatus,
    RawContextRequest,
    ReferenceAsset,
    ReferenceRegistry,
    TaskMode,
    build_native_h3_wiring,
    build_reference_registry,
)
from comfyui_h3_context.nodes import (
    NATIVE_H3_ADAPTER_NODE_ID,
    NATIVE_H3_WIRING_SOCKET_TYPE,
    H3ContextCompilerNode,
    H3ContextNativeH3AdapterNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
    H3ReferenceRegistryNode,
    PipelineNodeError,
)

ROOT = Path(__file__).resolve().parents[1]
CORE_MODULE = ROOT / "comfyui_h3_context" / "core" / "native_h3.py"
FIXTURE = ROOT / "tests" / "fixtures" / "m3_06_native_h3_reference_workflow.json"


def _request(mode: TaskMode) -> RawContextRequest:
    return H3ContextRequestNode().build_request(
        mode,
        "Preserve the exact declared intent.",
        duration_seconds=5.0,
    )[0]


def _single_keyframe_request(mode: TaskMode) -> RawContextRequest:
    role = AssetRole.FIRST_FRAME if mode is TaskMode.I2VA else AssetRole.LAST_FRAME
    registry = build_reference_registry((ReferenceAsset("anchor_1", MediaKind.IMAGE, role, 1),))
    return RawContextRequest(
        mode=mode,
        user_intent="Preserve the exact declared intent.",
        duration_seconds=5.0,
        reference_registry=registry,
    )


def _report(mode: TaskMode = TaskMode.T2VA, *, with_references: bool = False) -> ContextReport:
    registry = ReferenceRegistry.empty()
    if with_references:
        registry = H3ReferenceRegistryNode().build_registry(
            images=[object(), object()],
            videos=[object()],
            audios=[object()],
        )[0]
    plan = H3ContextPlanNode().build_plan(_request(mode), registry)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    assert document.status is PromptRenderStatus.RENDERED
    return H3ContextValidatorNode().validate(plan, document)[1]


def _single_keyframe_report(mode: TaskMode) -> ContextReport:
    plan = H3ContextPlanNode().build_plan(_single_keyframe_request(mode))[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    assert document.status is PromptRenderStatus.RENDERED
    return H3ContextValidatorNode().validate(plan, document)[1]


def _keyframe_report() -> ContextReport:
    registry = build_reference_registry(
        (
            ReferenceAsset("first_frame_1", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1),
            ReferenceAsset("last_frame_1", MediaKind.IMAGE, AssetRole.LAST_FRAME, 2),
        )
    )
    raw = RawContextRequest(
        mode=TaskMode.FL2VA,
        user_intent="Preserve the exact declared intent.",
        duration_seconds=5.0,
        reference_registry=registry,
    )
    plan = H3ContextPlanNode().build_plan(raw)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    assert document.status is PromptRenderStatus.RENDERED
    return H3ContextValidatorNode().validate(plan, document)[1]


def _paired_reference_report() -> ContextReport:
    registry = build_reference_registry(
        (
            ReferenceAsset("image_1", MediaKind.IMAGE, AssetRole.REFERENCE, 1),
            ReferenceAsset(
                "audio_1",
                MediaKind.AUDIO,
                AssetRole.AUDIO_SOURCE,
                2,
                paired_video_id="video_1",
            ),
            ReferenceAsset("video_1", MediaKind.VIDEO, AssetRole.REFERENCE, 3),
        )
    )
    raw = RawContextRequest(
        mode=TaskMode.REF2VA,
        user_intent="Preserve the exact declared intent.",
        duration_seconds=5.0,
        assets=registry.to_asset_descriptors(),
        reference_registry=registry,
    )
    plan = H3ContextPlanNode().build_plan(raw)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    assert document.status is PromptRenderStatus.RENDERED
    return H3ContextValidatorNode().validate(plan, document)[1]


def _audio_only_reference_report(*, with_duration: bool = True) -> ContextReport:
    registry = build_reference_registry(
        (
            ReferenceAsset(
                "audio_1",
                MediaKind.AUDIO,
                AssetRole.AUDIO_SOURCE,
                1,
                metadata=(MediaMetadata(duration_seconds=Decimal("5")) if with_duration else None),
            ),
        )
    )
    raw = RawContextRequest(
        mode=TaskMode.REF2VA,
        user_intent="Preserve the exact declared intent.",
        duration_seconds=5.0,
        assets=registry.to_asset_descriptors(),
        reference_registry=registry,
    )
    plan = H3ContextPlanNode().build_plan(raw)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    assert document.status is PromptRenderStatus.RENDERED
    return H3ContextValidatorNode().validate(plan, document)[1]


def _registry_report(
    registry: ReferenceRegistry, *, duration_seconds: float = 5.0
) -> ContextReport:
    raw = RawContextRequest(
        mode=TaskMode.REF2VA,
        user_intent="Preserve the exact declared intent.",
        duration_seconds=duration_seconds,
        assets=registry.to_asset_descriptors(),
        reference_registry=registry,
    )
    plan = H3ContextPlanNode().build_plan(raw)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    assert document.status is PromptRenderStatus.RENDERED
    return H3ContextValidatorNode().validate(plan, document)[1]


class NativeH3AdapterTests(unittest.TestCase):
    def test_metadata_preserves_prompt_and_declares_native_mapping(self) -> None:
        report = _report()
        prompt, wiring = H3ContextNativeH3AdapterNode().adapt(report)
        self.assertEqual(prompt, report.prompt_document.text)
        self.assertEqual(wiring.schema, "h3-native-h3-wiring/1")
        self.assertEqual(wiring.task_mode, TaskMode.T2VA)
        self.assertEqual(wiring.native_node_id, "MiniMaxH3ImageToVideo")
        self.assertEqual(wiring.native_input_names[2], "prompt")
        self.assertEqual(wiring.media_flow, "direct_to_native")
        self.assertEqual(wiring.media_processing, "native_only")
        self.assertEqual(wiring.upload_path, "none")
        self.assertEqual(wiring.prompt, prompt)
        self.assertIs(weakref.ref(wiring)(), wiring)

    def test_reference_labels_and_input_order_are_copied_exactly(self) -> None:
        report = _report(TaskMode.REF2VA, with_references=True)
        prompt, wiring = H3ContextNativeH3AdapterNode().adapt(report)
        self.assertEqual(prompt, report.prompt_document.text)
        # Attached inputs retain exact native bindings without inventing a prompt role.
        for label in ("<Picture 1>", "<Picture 2>", "<Video 1>", "<Audio 1>"):
            self.assertNotIn(label, prompt)
        self.assertIn(
            "fidelity.reference.unused",
            {item.code for item in report.validation.diagnostics},
        )
        self.assertEqual(wiring.native_node_id, "MiniMaxH3ReferenceToVideo")
        self.assertEqual(
            [(item.asset_id, item.label, item.native_input) for item in wiring.bindings],
            [
                ("image_1", "<Picture 1>", "ref_images"),
                ("image_2", "<Picture 2>", "ref_images"),
                ("video_1", "<Video 1>", "ref_videos"),
                ("audio_1", "<Audio 1>", "ref_audios"),
            ],
        )
        self.assertEqual(
            tuple(item.native_slot for item in wiring.bindings),
            ("ref_image_0", "ref_image_1", "ref_video_0", "ref_audio_0"),
        )

    def test_fl2va_uses_only_explicit_keyframe_roles(self) -> None:
        prompt, wiring = H3ContextNativeH3AdapterNode().adapt(_keyframe_report())
        self.assertEqual(wiring.task_mode, TaskMode.FL2VA)
        self.assertEqual(wiring.native_node_id, "MiniMaxH3ImageToVideo")
        self.assertEqual(
            [(item.asset_id, item.native_input, item.native_slot) for item in wiring.bindings],
            [
                ("first_frame_1", "first_frame", "first_frame"),
                ("last_frame_1", "last_frame", "last_frame"),
            ],
        )
        self.assertEqual(wiring.prompt, prompt)

    def test_ref2va_preserves_explicit_paired_soundtrack_slot(self) -> None:
        wiring = build_native_h3_wiring(_paired_reference_report())
        self.assertEqual(
            [(item.asset_id, item.native_input, item.native_slot) for item in wiring.bindings],
            [
                ("image_1", "ref_images", "ref_image_0"),
                ("audio_1", "ref_video_audios", "ref_video_audio_0"),
                ("video_1", "ref_videos", "ref_video_0"),
            ],
        )

    def test_fl2va_missing_keyframe_role_fails_closed(self) -> None:
        registry = build_reference_registry(
            (ReferenceAsset("first_frame_1", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1),)
        )
        valid_report = _keyframe_report()
        request = replace(
            valid_report.request,
            assets=registry.to_asset_descriptors(),
            reference_registry=registry,
        )
        plan = replace(
            valid_report.plan,
            request=request,
            intent_graph=replace(valid_report.plan.intent_graph, registry=registry),
        )
        report = replace(valid_report, request=request, plan=plan)
        with self.assertRaises(NativeH3AdapterError) as context:
            build_native_h3_wiring(report)
        self.assertIn("missing_keyframe", str(context.exception))

    def test_single_keyframe_modes_use_exact_native_image_inputs(self) -> None:
        expected = {
            TaskMode.I2VA: ("first_frame", "first_frame"),
            TaskMode.L2VA: ("last_frame", "last_frame"),
        }
        for mode, (native_input, native_path) in expected.items():
            with self.subTest(mode=mode):
                wiring = build_native_h3_wiring(_single_keyframe_report(mode))
                self.assertEqual(wiring.native_node_id, "MiniMaxH3ImageToVideo")
                self.assertEqual(len(wiring.bindings), 1)
                self.assertEqual(wiring.bindings[0].native_input, native_input)
                self.assertEqual(wiring.bindings[0].native_path, native_path)
                self.assertTrue(wiring.queue_ready)

    def test_reference_paths_are_fully_qualified_zero_based(self) -> None:
        wiring = build_native_h3_wiring(_paired_reference_report())
        self.assertEqual(
            [item.native_path for item in wiring.bindings],
            [
                "ref_images.ref_image_0",
                "ref_video_audios.ref_video_audio_0",
                "ref_videos.ref_video_0",
            ],
        )
        self.assertEqual([item.ordinal for item in wiring.bindings], [1, 1, 1])

    def test_h3_base_aggregate_reference_limit_fails_closed(self) -> None:
        assets = [
            ReferenceAsset(f"image_{index}", MediaKind.IMAGE, AssetRole.REFERENCE, index)
            for index in range(1, 10)
        ]
        assets.append(
            ReferenceAsset(
                "video_1",
                MediaKind.VIDEO,
                AssetRole.REFERENCE,
                10,
                metadata=MediaMetadata(duration_seconds=Decimal("2")),
            )
        )
        assets.extend(
            ReferenceAsset(
                f"audio_{index}",
                MediaKind.AUDIO,
                AssetRole.AUDIO_SOURCE,
                10 + index,
                metadata=MediaMetadata(duration_seconds=Decimal("2")),
            )
            for index in range(1, 4)
        )
        registry = build_reference_registry(tuple(assets))
        with self.assertRaisesRegex(NativeH3AdapterError, "h3_base_total_reference_limit"):
            build_native_h3_wiring(_registry_report(registry))

    def test_h3_base_output_duration_limits_fail_closed(self) -> None:
        registry = build_reference_registry(
            (ReferenceAsset("image_1", MediaKind.IMAGE, AssetRole.REFERENCE, 1),)
        )
        for duration in (3.9, 15.1):
            with self.subTest(duration=duration):
                with self.assertRaisesRegex(NativeH3AdapterError, "h3_base_output_duration_limit"):
                    build_native_h3_wiring(_registry_report(registry, duration_seconds=duration))

    def test_h3_base_known_duration_limits_fail_closed(self) -> None:
        too_long = build_reference_registry(
            (
                ReferenceAsset("image_1", MediaKind.IMAGE, AssetRole.REFERENCE, 1),
                ReferenceAsset(
                    "video_1",
                    MediaKind.VIDEO,
                    AssetRole.REFERENCE,
                    2,
                    metadata=MediaMetadata(duration_seconds=Decimal("16")),
                ),
            )
        )
        with self.assertRaisesRegex(NativeH3AdapterError, "h3_base_reference_duration_limit"):
            build_native_h3_wiring(_registry_report(too_long))

        total_too_long = build_reference_registry(
            (
                ReferenceAsset("image_1", MediaKind.IMAGE, AssetRole.REFERENCE, 1),
                ReferenceAsset(
                    "video_1",
                    MediaKind.VIDEO,
                    AssetRole.REFERENCE,
                    2,
                    metadata=MediaMetadata(duration_seconds=Decimal("8")),
                ),
                ReferenceAsset(
                    "video_2",
                    MediaKind.VIDEO,
                    AssetRole.REFERENCE,
                    3,
                    metadata=MediaMetadata(duration_seconds=Decimal("8")),
                ),
            )
        )
        with self.assertRaisesRegex(NativeH3AdapterError, "h3_base_video_duration_total"):
            build_native_h3_wiring(_registry_report(total_too_long))

    def test_missing_timed_metadata_is_visible_and_not_queue_ready(self) -> None:
        registry = build_reference_registry(
            (
                ReferenceAsset("image_1", MediaKind.IMAGE, AssetRole.REFERENCE, 1),
                ReferenceAsset("video_1", MediaKind.VIDEO, AssetRole.REFERENCE, 2),
            )
        )
        wiring = build_native_h3_wiring(_registry_report(registry))
        self.assertFalse(wiring.queue_ready)
        self.assertIn("h3_base_reference_duration_metadata_unverified", wiring.limitations)
        self.assertFalse(wiring.to_wire()["queue_ready"])

    def test_audio_only_reference_uses_exact_native_audio_binding(self) -> None:
        wiring = build_native_h3_wiring(_audio_only_reference_report())
        self.assertEqual(wiring.native_node_id, "MiniMaxH3ReferenceToVideo")
        self.assertIn("vae", wiring.native_input_names)
        self.assertIn("audio_vae", wiring.native_input_names)
        self.assertEqual(
            [item.to_wire() for item in wiring.bindings],
            [
                {
                    "asset_id": "audio_1",
                    "kind": "audio",
                    "label": "<Audio 1>",
                    "ordinal": 1,
                    "native_input": "ref_audios",
                    "native_slot": "ref_audio_0",
                    "native_path": "ref_audios.ref_audio_0",
                }
            ],
        )
        self.assertEqual(wiring.host_revision, NATIVE_H3_HOST_REVISION)
        self.assertEqual(wiring.native_source_blob, NATIVE_H3_SOURCE_BLOB)
        self.assertTrue(wiring.queue_ready)

    def test_audio_only_missing_duration_is_visible_and_not_queue_ready(self) -> None:
        wiring = build_native_h3_wiring(_audio_only_reference_report(with_duration=False))
        self.assertEqual(wiring.bindings[0].native_path, "ref_audios.ref_audio_0")
        self.assertFalse(wiring.queue_ready)
        self.assertEqual(wiring.limitations, ("h3_base_reference_duration_metadata_unverified",))

    def test_ref2va_without_reference_media_fails_closed(self) -> None:
        valid_report = _audio_only_reference_report()
        registry = ReferenceRegistry.empty()
        request = replace(
            valid_report.request,
            assets=registry.to_asset_descriptors(),
            reference_registry=registry,
        )
        plan = replace(
            valid_report.plan,
            request=request,
            intent_graph=replace(valid_report.plan.intent_graph, registry=registry),
        )
        report = replace(valid_report, request=request, plan=plan)
        with self.assertRaisesRegex(NativeH3AdapterError, "missing_reference_media"):
            build_native_h3_wiring(report)

    def test_failed_report_or_draft_prompt_never_emits_manifest(self) -> None:
        report = _report()
        with self.assertRaises(NativeH3AdapterError) as context:
            build_native_h3_wiring(
                report.__class__(
                    report_id=report.report_id,
                    schema_version=report.schema_version,
                    request=report.request,
                    plan=report.plan,
                    prompt_document=report.prompt_document.__class__(
                        document_id=report.prompt_document.document_id,
                        schema_version=report.prompt_document.schema_version,
                        profile=report.prompt_document.profile,
                        task_mode=report.prompt_document.task_mode,
                        plan_id=report.prompt_document.plan_id,
                        text=report.prompt_document.text,
                        sections=report.prompt_document.sections,
                        status=PromptRenderStatus.DRAFT,
                    ),
                    validation=report.validation,
                    evidence=report.evidence,
                    limitations=report.limitations,
                    diagnostics=report.diagnostics,
                    provider_receipt=report.provider_receipt,
                )
            )
        self.assertIn("prompt_not_rendered", str(context.exception))

    def test_wire_projection_is_json_safe_and_deterministic(self) -> None:
        first = build_native_h3_wiring(_report(TaskMode.REF2VA, with_references=True)).to_wire()
        second = build_native_h3_wiring(_report(TaskMode.REF2VA, with_references=True)).to_wire()
        self.assertEqual(first, second)
        self.assertEqual(json.loads(json.dumps(first, sort_keys=True)), first)
        self.assertEqual(first["host_revision"], NATIVE_H3_HOST_REVISION)
        self.assertEqual(first["native_source_blob"], NATIVE_H3_SOURCE_BLOB)

    def test_host_identity_is_provenance_when_native_structure_is_valid(self) -> None:
        wiring = build_native_h3_wiring(_report())
        changed = replace(
            wiring,
            host_version="0.33.0",
            host_revision="1" * 40,
            native_source_blob="2" * 40,
        )
        self.assertEqual(changed.host_version, "0.33.0")
        self.assertEqual(changed.native_node_id, wiring.native_node_id)
        with self.assertRaises(NativeH3AdapterError):
            replace(wiring, host_version="latest")
        with self.assertRaises(NativeH3AdapterError):
            replace(wiring, host_version=f"{'9' * 5_000}.0.0")

    def test_adapter_metadata_and_registration_are_stable(self) -> None:
        inputs = H3ContextNativeH3AdapterNode.INPUT_TYPES()
        self.assertEqual(NATIVE_H3_ADAPTER_NODE_ID, "comfyui_h3_context.H3Context.NativeH3Adapter")
        self.assertEqual(
            H3ContextNativeH3AdapterNode.RETURN_TYPES,
            ("H3_PROMPT_STRING", NATIVE_H3_WIRING_SOCKET_TYPE),
        )
        self.assertEqual(tuple(inputs["required"]), ("report",))
        self.assertIn(
            NATIVE_H3_ADAPTER_NODE_ID,
            __import__(
                "comfyui_h3_context.nodes", fromlist=["NODE_CLASS_MAPPINGS"]
            ).NODE_CLASS_MAPPINGS,
        )

    def test_native_core_has_no_optional_runtime_imports(self) -> None:
        tree = ast.parse(CORE_MODULE.read_text(encoding="utf-8"), filename=str(CORE_MODULE))
        forbidden = {
            "comfy",
            "comfy_api",
            "torch",
            "torchaudio",
            "cv2",
            "ffmpeg",
            "requests",
            "aiohttp",
        }
        imports = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
            for alias in node.names
        )
        self.assertTrue(forbidden.isdisjoint(imports))

    def test_fixture_pins_native_links_without_private_values(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(fixture["schema"], "h3-native-h3-fixture/1")
        self.assertEqual(
            "".join(fixture["host"]["revision_parts"]),
            NATIVE_H3_HOST_REVISION,
        )
        self.assertEqual(
            "".join(fixture["host"]["native_source_blob_parts"]),
            NATIVE_H3_SOURCE_BLOB,
        )
        self.assertEqual(fixture["native"]["class_type"], "MiniMaxH3ReferenceToVideo")
        self.assertEqual(fixture["native"]["task_mode"], "ref2va")
        self.assertEqual(
            [item["label"] for item in fixture["native"]["media_links"]],
            ["<Picture 1>", "<Video 1>", "<Audio 1>"],
        )
        self.assertEqual(
            [item["target_input"] for item in fixture["native"]["media_links"]],
            [
                "ref_images.ref_image_0",
                "ref_videos.ref_video_0",
                "ref_audios.ref_audio_0",
            ],
        )
        serialized = json.dumps(fixture, sort_keys=True).casefold()
        for marker in ("/home/", "c:\\", "authorization", "bearer ", "api_key", "https://"):
            self.assertNotIn(marker, serialized)

    def test_invalid_node_input_fails_closed(self) -> None:
        with self.assertRaises(PipelineNodeError) as context:
            H3ContextNativeH3AdapterNode().adapt(object())
        self.assertIn("invalid_report", str(context.exception))


if __name__ == "__main__":
    unittest.main()
