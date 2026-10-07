"""M4-03 official MiniMax H3-Context-IR adapter contract tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from comfyui_h3_context.core import (
    AssetRole,
    MediaKind,
    NormalizedContextRequest,
    OfficialAspectRatio,
    OfficialContextIRAdapter,
    OfficialContextIRMedia,
    OfficialContextIRTransportResponse,
    ProviderExecutionPolicy,
    ProviderIdentity,
    ProviderPrivacyMode,
    RawContextRequest,
    ReferenceAsset,
    ResolvedCredential,
    TaskMode,
    build_official_context_ir_request,
    build_reference_registry,
    normalize_request,
)
from comfyui_h3_context.core.errors import (
    OfficialContextIRError,
    ReferenceRegistryError,
    SecurityPolicyError,
)


class Resolver:
    def resolve(self, reference: str) -> ResolvedCredential:
        return ResolvedCredential(reference=reference, value="runtime-only-secret")


class Transport:
    def __init__(
        self,
        create: OfficialContextIRTransportResponse,
        query: OfficialContextIRTransportResponse,
    ) -> None:
        self.create_response = create
        self.query_response = query
        self.create_payload: dict[str, object] | None = None
        self.create_credential: ResolvedCredential | None = None
        self.query_task_id: str | None = None
        self.query_credential: ResolvedCredential | None = None

    def create(
        self,
        payload: dict[str, object],
        credential: ResolvedCredential,
    ) -> OfficialContextIRTransportResponse:
        self.create_payload = payload
        self.create_credential = credential
        return self.create_response

    def query(
        self,
        task_id: str,
        credential: ResolvedCredential,
    ) -> OfficialContextIRTransportResponse:
        self.query_task_id = task_id
        self.query_credential = credential
        return self.query_response


def normalized_request(
    mode: TaskMode,
    *,
    assets: tuple[ReferenceAsset, ...] = (),
    duration: int | float = 5,
) -> NormalizedContextRequest:
    registry = build_reference_registry(assets)
    raw = RawContextRequest(
        mode=mode,
        user_intent="A woman walks through a sunlit courtyard.",
        duration_seconds=duration,
        assets=registry.to_asset_descriptors(),
        reference_registry=registry,
    )
    result = normalize_request(raw)
    assert result.request is not None, result.diagnostics
    return result.request


def remote_policy() -> ProviderExecutionPolicy:
    return ProviderExecutionPolicy(
        provider=ProviderIdentity.OFFICIAL_MINIMAX,
        privacy_mode=ProviderPrivacyMode.EXPLICIT_REMOTE,
        offline=False,
        network_allowed=True,
        upload_consent=True,
        credential_reference="env.official_minimax",
    )


def image(
    asset_id: str,
    *,
    role: str,
    order: int,
    url: str = "https://cdn.example.test/frame.png",
) -> OfficialContextIRMedia:
    return OfficialContextIRMedia(
        asset_id=asset_id,
        kind=MediaKind.IMAGE,
        role=role,
        url=url,
        media_type="image/png",
        size_bytes=1024,
        width=1024,
        height=576,
        connection_order=order,
    )


def audio(asset_id: str = "audio", *, order: int = 1) -> OfficialContextIRMedia:
    return OfficialContextIRMedia(
        asset_id=asset_id,
        kind=MediaKind.AUDIO,
        role="reference_audio",
        url="https://cdn.example.test/reference.wav",
        media_type="audio/wav",
        size_bytes=1024,
        duration_seconds=5,
        connection_order=order,
    )


class OfficialContextIRTests(unittest.TestCase):
    def test_audio_only_reference_payload_executes_only_with_consent(self) -> None:
        assets = (ReferenceAsset("audio", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE, 1),)
        media = audio()
        request = build_official_context_ir_request(
            normalized_request(TaskMode.REF2VA, assets=assets),
            ratio=OfficialAspectRatio.RATIO_16_9,
            media=(media,),
        )
        transport = Transport(
            OfficialContextIRTransportResponse(200, {"task_id": "audio-task"}),
            OfficialContextIRTransportResponse(
                200,
                {
                    "task": {
                        "id": "audio-task",
                        "status": "succeeded",
                        "task_type": "h3_context_ir",
                        "content": {"prompt": "A dancer moves."},
                    }
                },
            ),
        )
        with self.assertRaises(SecurityPolicyError):
            OfficialContextIRAdapter().execute(
                request,
                policy=replace(remote_policy(), upload_consent=False),
                resolver=Resolver(),
                transport=transport,
                receipt_id="audio-denied",
            )
        self.assertIsNone(transport.create_payload)
        result = OfficialContextIRAdapter().execute(
            request,
            policy=remote_policy(),
            resolver=Resolver(),
            transport=transport,
            receipt_id="audio-success",
        )
        assert transport.create_payload is not None
        self.assertEqual(
            transport.create_payload["content"],
            [
                {"type": "text", "text": "A woman walks through a sunlit courtyard."},
                {"type": "audio_url", "audio_url": {"url": media.url}, "role": "reference_audio"},
            ],
        )
        self.assertEqual(result.receipt.outcome.value, "succeeded")
        self.assertNotIn(media.url, json.dumps(result.to_public_dict()))
        self.assertNotIn("runtime-only-secret", json.dumps(result.to_public_dict()))

    def test_audio_reference_limits_and_registry_identity_remain_enforced(self) -> None:
        assets = tuple(
            ReferenceAsset(f"audio-{index}", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE, index)
            for index in range(1, 5)
        )
        media = tuple(audio(asset.asset_id, order=asset.connection_order) for asset in assets)
        for changes in (
            {"size_bytes": 15 * 1024 * 1024 + 1},
            {"duration_seconds": 1.99},
            {"duration_seconds": 15.01},
            {"duration_seconds": None},
            {"media_type": "audio/ogg"},
            {"role": "first_frame"},
        ):
            with self.subTest(changes=changes), self.assertRaises(OfficialContextIRError):
                replace(media[0], **changes)
        with self.assertRaises(ReferenceRegistryError):
            normalized_request(TaskMode.REF2VA, assets=assets)
        cases = (
            (assets[:2], tuple(replace(item, duration_seconds=8) for item in media[:2])),
            (assets[:2], tuple(reversed(media[:2]))),
            (assets[:1], (replace(media[0], asset_id="foreign"),)),
        )
        for references, payload_media in cases:
            with self.subTest(media=payload_media), self.assertRaises(OfficialContextIRError):
                build_official_context_ir_request(
                    normalized_request(TaskMode.REF2VA, assets=references),
                    ratio=OfficialAspectRatio.RATIO_16_9,
                    media=payload_media,
                )
        valid = build_official_context_ir_request(
            normalized_request(TaskMode.REF2VA, assets=assets[:3]),
            ratio=OfficialAspectRatio.RATIO_16_9,
            media=media[:3],
        )
        content = valid.to_payload()["content"]
        assert isinstance(content, list)
        self.assertEqual(
            [item["type"] for item in content],
            ["text", "audio_url", "audio_url", "audio_url"],
        )

    def test_t2va_payload_and_success_extract_content_prompt(self) -> None:
        request = build_official_context_ir_request(
            normalized_request(TaskMode.T2VA),
            ratio=OfficialAspectRatio.RATIO_16_9,
        )
        transport = Transport(
            OfficialContextIRTransportResponse(200, {"task_id": "12345"}),
            OfficialContextIRTransportResponse(
                200,
                {
                    "task": {
                        "id": "12345",
                        "status": "succeeded",
                        "task_type": "h3_context_ir",
                        "content": {"prompt": "integrated_multimodal_description: [Shot 1]"},
                    }
                },
            ),
        )

        result = OfficialContextIRAdapter().execute(
            request,
            policy=remote_policy(),
            resolver=Resolver(),
            transport=transport,
            receipt_id="receipt-1",
        )

        assert transport.create_payload is not None
        self.assertEqual(transport.create_payload["model"], "MiniMax-H3")
        self.assertEqual(transport.create_payload["duration"], 5)
        self.assertEqual(transport.create_payload["ratio"], "16:9")
        self.assertEqual(
            transport.create_payload["content"],
            [{"type": "text", "text": "A woman walks through a sunlit courtyard."}],
        )
        self.assertEqual(result.prompt, "integrated_multimodal_description: [Shot 1]")
        self.assertEqual(result.receipt.outcome.value, "succeeded")
        self.assertEqual(result.receipt.task_id, "12345")
        self.assertNotIn("runtime-only-secret", json.dumps(result.to_public_dict()))
        self.assertNotIn("cdn.example.test", json.dumps(result.to_public_dict()))

    def test_frame_and_reference_role_payloads_are_explicit_and_exclusive(self) -> None:
        first = ReferenceAsset(
            "first",
            MediaKind.IMAGE,
            AssetRole.FIRST_FRAME,
            1,
        )
        last = ReferenceAsset(
            "last",
            MediaKind.IMAGE,
            AssetRole.LAST_FRAME,
            2,
        )
        request = build_official_context_ir_request(
            normalized_request(TaskMode.FL2VA, assets=(first, last)),
            ratio=OfficialAspectRatio.ADAPTIVE,
            media=(
                image("first", role="first_frame", order=1),
                image("last", role="last_frame", order=2),
            ),
        )
        payload = request.to_payload()
        self.assertEqual(
            payload["content"],
            [
                {"type": "text", "text": "A woman walks through a sunlit courtyard."},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://cdn.example.test/frame.png"},
                    "role": "first_frame",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": "https://cdn.example.test/frame.png"},
                    "role": "last_frame",
                },
            ],
        )

        reference = ReferenceAsset(
            "ref",
            MediaKind.IMAGE,
            AssetRole.REFERENCE,
            1,
        )
        with self.assertRaises(OfficialContextIRError):
            build_official_context_ir_request(
                normalized_request(TaskMode.REF2VA, assets=(reference,)),
                ratio=OfficialAspectRatio.ADAPTIVE,
                media=(image("ref", role="first_frame", order=1),),
            )

    def test_documented_limits_fail_before_transport(self) -> None:
        oversized = image("first", role="first_frame", order=1)
        with self.assertRaises(OfficialContextIRError):
            oversized = replace(oversized, size_bytes=30 * 1024 * 1024 + 1)
            build_official_context_ir_request(
                normalized_request(
                    TaskMode.I2VA,
                    assets=(
                        ReferenceAsset(
                            "first",
                            MediaKind.IMAGE,
                            AssetRole.FIRST_FRAME,
                            1,
                        ),
                    ),
                ),
                ratio=OfficialAspectRatio.ADAPTIVE,
                media=(oversized,),
            )

        with self.assertRaises(OfficialContextIRError):
            build_official_context_ir_request(
                normalized_request(TaskMode.T2VA, duration=3),
                ratio=OfficialAspectRatio.RATIO_16_9,
            )

    def test_policy_and_credential_are_required_before_create(self) -> None:
        request = build_official_context_ir_request(
            normalized_request(TaskMode.T2VA),
            ratio=OfficialAspectRatio.RATIO_16_9,
        )
        transport = Transport(
            OfficialContextIRTransportResponse(200, {"task_id": "never"}),
            OfficialContextIRTransportResponse(200, {}),
        )
        with self.assertRaises(SecurityPolicyError):
            OfficialContextIRAdapter().execute(
                request,
                policy=ProviderExecutionPolicy(
                    ProviderIdentity.OFFICIAL_MINIMAX,
                    ProviderPrivacyMode.EXPLICIT_REMOTE,
                    offline=True,
                    network_allowed=False,
                    upload_consent=False,
                    credential_reference="env.official_minimax",
                ),
                resolver=Resolver(),
                transport=transport,
            )
        self.assertIsNone(transport.create_payload)

    def test_terminal_api_error_is_redacted_and_never_success(self) -> None:
        request = build_official_context_ir_request(
            normalized_request(TaskMode.T2VA),
            ratio=OfficialAspectRatio.RATIO_16_9,
        )
        transport = Transport(
            OfficialContextIRTransportResponse(
                401,
                {
                    "type": "error",
                    "error": {
                        "type": "authorized_error",
                        "message": "Bearer secret-must-not-leak",
                        "http_code": "401",
                    },
                },
            ),
            OfficialContextIRTransportResponse(200, {}),
        )
        with self.assertRaises(OfficialContextIRError) as failure:
            OfficialContextIRAdapter().execute(
                request,
                policy=remote_policy(),
                resolver=Resolver(),
                transport=transport,
            )
        self.assertEqual(failure.exception.category, "authentication")
        self.assertNotIn("secret-must-not-leak", str(failure.exception))
        self.assertNotIn("runtime-only-secret", str(failure.exception))
        receipt = failure.exception.receipt
        assert receipt is not None
        self.assertEqual(receipt.outcome.value, "authentication")

    def test_wrong_task_type_or_missing_prompt_fails_closed(self) -> None:
        request = build_official_context_ir_request(
            normalized_request(TaskMode.T2VA),
            ratio=OfficialAspectRatio.RATIO_16_9,
        )
        for task in (
            {"id": "1", "status": "succeeded", "task_type": "generation", "content": {}},
            {
                "id": "1",
                "status": "succeeded",
                "task_type": "h3_context_ir",
                "content": {"prompt": ""},
            },
        ):
            transport = Transport(
                OfficialContextIRTransportResponse(200, {"task_id": "1"}),
                OfficialContextIRTransportResponse(200, {"task": task}),
            )
            with self.subTest(task=task), self.assertRaises(OfficialContextIRError):
                OfficialContextIRAdapter().execute(
                    request,
                    policy=remote_policy(),
                    resolver=Resolver(),
                    transport=transport,
                )

    def test_media_url_rejects_signed_or_non_https_reference(self) -> None:
        with self.assertRaises(OfficialContextIRError):
            OfficialContextIRMedia(
                asset_id="first",
                kind=MediaKind.IMAGE,
                role="first_frame",
                url="http://cdn.example.test/frame.png",
                media_type="image/png",
                size_bytes=1024,
                width=1024,
                height=576,
                connection_order=1,
            )
        with self.assertRaises(OfficialContextIRError):
            OfficialContextIRMedia(
                asset_id="first",
                kind=MediaKind.IMAGE,
                role="first_frame",
                url="https://cdn.example.test/frame.png?sig=secret",
                media_type="image/png",
                size_bytes=1024,
                width=1024,
                height=576,
                connection_order=1,
            )


if __name__ == "__main__":
    unittest.main()
