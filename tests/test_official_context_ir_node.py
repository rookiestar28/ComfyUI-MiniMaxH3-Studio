"""M4-06 mocked ComfyUI-node and sanitized recording contract tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    AssetRole,
    MediaKind,
    OfficialAspectRatio,
    OfficialContextIRMedia,
    OfficialContextIRMediaRole,
    OfficialContextIRRecording,
    OfficialContextIRTransportResponse,
    ProviderIdentity,
    RawContextRequest,
    ReferenceAsset,
    ResolvedCredential,
    TaskMode,
    build_official_context_ir_recording,
    build_reference_registry,
)
from comfyui_h3_context.core.errors import OfficialContextIRError
from comfyui_h3_context.nodes import (
    NODE_CLASS_MAPPINGS,
    OFFICIAL_CONTEXT_IR_NODE_ID,
    H3OfficialContextIRNode,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "m4_06_official_context_ir_recording.json"
SCHEMA = ROOT / "governance" / "contracts" / "official_context_ir_recording_v1.schema.json"


class Resolver:
    def resolve(self, reference: str) -> ResolvedCredential:
        return ResolvedCredential(reference=reference, value="runtime-secret")


class Transport:
    def __init__(self, *, prompt: str = "official node prompt") -> None:
        self.prompt = prompt
        self.create_count = 0
        self.query_count = 0
        self.payload: dict[str, object] | None = None

    def create(
        self,
        payload: dict[str, object],
        credential: ResolvedCredential,
    ) -> OfficialContextIRTransportResponse:
        self.create_count += 1
        self.payload = payload
        assert credential.value == "runtime-secret"
        return OfficialContextIRTransportResponse(202, {"task_id": "node-task-1"})

    def query(
        self,
        task_id: str,
        credential: ResolvedCredential,
    ) -> OfficialContextIRTransportResponse:
        self.query_count += 1
        assert task_id == "node-task-1"
        assert credential.value == "runtime-secret"
        return OfficialContextIRTransportResponse(
            200,
            {
                "task": {
                    "status": "succeeded",
                    "task_type": "h3_context_ir",
                    "content": {"prompt": self.prompt},
                }
            },
        )


def raw_request(
    mode: TaskMode = TaskMode.T2VA,
    *,
    registry_assets: tuple[ReferenceAsset, ...] = (),
) -> RawContextRequest:
    registry = build_reference_registry(registry_assets)
    return RawContextRequest(
        mode=mode,
        user_intent="A person walks through a sunlit courtyard.",
        duration_seconds=5,
        assets=registry.to_asset_descriptors(),
        reference_registry=registry,
    )


def image_media(asset_id: str = "image_1") -> OfficialContextIRMedia:
    return OfficialContextIRMedia(
        asset_id=asset_id,
        kind=MediaKind.IMAGE,
        role=OfficialContextIRMediaRole.REFERENCE_IMAGE,
        url="https://media.example.test/assets/reference.png",
        media_type="image/png",
        size_bytes=1024,
        width=1024,
        height=576,
        connection_order=1,
    )


class OfficialContextIRNodeTests(unittest.TestCase):
    def test_registration_and_input_surface_are_optional_and_namespaced(self) -> None:
        self.assertTrue(OFFICIAL_CONTEXT_IR_NODE_ID.startswith("comfyui_h3_context."))
        self.assertIs(NODE_CLASS_MAPPINGS[OFFICIAL_CONTEXT_IR_NODE_ID], H3OfficialContextIRNode)
        self.assertEqual(
            H3OfficialContextIRNode.RETURN_NAMES,
            ("prompt", "receipt", "consent_notice"),
        )
        inputs = H3OfficialContextIRNode.INPUT_TYPES()
        self.assertIn("upload_consent", inputs["required"])
        self.assertIn("network_allowed", inputs["required"])
        self.assertIn("credential_reference", inputs["required"])
        self.assertIn("media", inputs["optional"])

    def test_prevalidation_never_calls_runtime_transport(self) -> None:
        transport = Transport()
        node = H3OfficialContextIRNode(transport=transport, resolver=Resolver())
        self.assertTrue(node.VALIDATE_INPUTS(None))
        request = raw_request()
        message = node.VALIDATE_INPUTS(
            request,
            ratio=OfficialAspectRatio.RATIO_16_9.value,
            upload_consent=False,
            network_allowed=False,
            credential_reference="env.official_minimax",
        )
        self.assertIsInstance(message, str)
        assert isinstance(message, str)
        self.assertIn("upload_consent", message)
        self.assertEqual(transport.create_count, 0)
        self.assertEqual(transport.query_count, 0)

    def test_t2va_execution_returns_prompt_receipt_and_notice_without_native_generation(
        self,
    ) -> None:
        transport = Transport()
        node = H3OfficialContextIRNode(transport=transport, resolver=Resolver())
        prompt, receipt, notice = node.execute(
            raw_request(),
            ratio=OfficialAspectRatio.RATIO_16_9.value,
            upload_consent=True,
            network_allowed=True,
            credential_reference="env.official_minimax",
        )
        self.assertEqual(prompt, "official node prompt")
        self.assertEqual(receipt.provider, ProviderIdentity.OFFICIAL_MINIMAX)
        self.assertEqual(receipt.task_id, "node-task-1")
        self.assertEqual(notice.provider.value, "official_minimax")
        self.assertTrue(notice.consent_granted)
        self.assertEqual(transport.create_count, 1)
        self.assertEqual(transport.query_count, 1)
        assert transport.payload is not None
        self.assertEqual(
            transport.payload["content"],
            [
                {
                    "type": "text",
                    "text": "A person walks through a sunlit courtyard.",
                }
            ],
        )
        public = json.dumps(
            {"prompt": prompt, "receipt": receipt.to_wire(), "notice": notice.to_public_dict()}
        )
        self.assertNotIn("runtime-secret", public)
        self.assertNotIn("media.example.test", public)
        self.assertNotIn("MiniMaxH3ImageToVideo", public)

    def test_reference_mapping_is_canonical_and_policy_failure_never_calls_transport(self) -> None:
        asset = ReferenceAsset("image_1", MediaKind.IMAGE, AssetRole.REFERENCE, 1)
        transport = Transport()
        node = H3OfficialContextIRNode(transport=transport, resolver=Resolver())
        prompt, _, _ = node.execute(
            raw_request(TaskMode.REF2VA, registry_assets=(asset,)),
            ratio=OfficialAspectRatio.ADAPTIVE.value,
            media=(image_media(),),
            upload_consent=True,
            network_allowed=True,
            credential_reference="env.official_minimax",
        )
        self.assertEqual(prompt, "official node prompt")
        assert transport.payload is not None
        content = cast(list[dict[str, object]], transport.payload["content"])
        reference_item = content[1]
        self.assertEqual(reference_item["role"], "reference_image")
        image_url = cast(dict[str, object], reference_item["image_url"])
        self.assertEqual(
            image_url["url"],
            "https://media.example.test/assets/reference.png",
        )

        blocked_transport = Transport()
        blocked_node = H3OfficialContextIRNode(transport=blocked_transport, resolver=Resolver())
        with self.assertRaises(OfficialContextIRError) as failure:
            blocked_node.execute(
                raw_request(),
                ratio=OfficialAspectRatio.RATIO_16_9.value,
                upload_consent=False,
                network_allowed=True,
                credential_reference="env.official_minimax",
            )
        self.assertEqual(failure.exception.category, "policy")
        self.assertEqual(blocked_transport.create_count, 0)

    def test_missing_runtime_transport_fails_closed_without_fake_success(self) -> None:
        node = H3OfficialContextIRNode(resolver=Resolver())
        with self.assertRaises(OfficialContextIRError) as failure:
            node.execute(
                raw_request(),
                ratio=OfficialAspectRatio.RATIO_16_9.value,
                upload_consent=True,
                network_allowed=True,
                credential_reference="env.official_minimax",
            )
        self.assertEqual(failure.exception.category, "transport_unconfigured")

    def test_recording_is_authorized_sanitized_and_fingerprinted(self) -> None:
        transport = Transport()
        node = H3OfficialContextIRNode(transport=transport, resolver=Resolver())
        prompt, receipt, _ = node.execute(
            raw_request(),
            ratio=OfficialAspectRatio.RATIO_16_9.value,
            upload_consent=True,
            network_allowed=True,
            credential_reference="env.official_minimax",
        )
        self.assertEqual(prompt, "official node prompt")
        result = node.last_result
        self.assertIsNotNone(result)
        assert result is not None
        recording = build_official_context_ir_recording(
            result, authorization_label="mocked_fixture"
        )
        public = recording.to_public_dict()
        self.assertEqual(public["authorization_label"], "mocked_fixture")
        self.assertNotIn("prompt", public)
        self.assertNotIn("media.example.test", json.dumps(public))
        self.assertEqual(public["task_id"], receipt.task_id)
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(fixture["schema_version"], public["schema_version"])
        self.assertTrue(fixture["sanitized"])
        self.assertNotIn("prompt", fixture)
        loaded = OfficialContextIRRecording.from_public_dict(fixture)
        self.assertEqual(loaded.to_public_dict(), fixture)
        self.assertEqual(loaded.to_public_dict(), public)

    def test_recording_schema_and_fixture_are_closed_and_secret_free(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertTrue(schema["additionalProperties"] is False)
        self.assertEqual(set(schema["required"]), set(fixture))
        self.assertEqual(schema["properties"]["provider"]["const"], "official_minimax")
        self.assertEqual(schema["properties"]["sanitized"]["const"], True)
        encoded = json.dumps(fixture, sort_keys=True)
        for marker in ("prompt", "credential", "https://", "/", "secret", "password", "token"):
            self.assertNotIn(marker, encoded.casefold())


if __name__ == "__main__":
    unittest.main()
