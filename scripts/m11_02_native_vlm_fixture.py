"""Offline M11-02 native VLM observation fixture.

The fixture injects a host-shaped CLIP and admitted image payloads to exercise the strict parser
and source/region contract.  It never starts ComfyUI, loads a checkpoint, decodes media, contacts
Ollama, or claims live model quality.
"""

from __future__ import annotations

import argparse
import json
from typing import cast

from comfyui_h3_context.adapters.comfyui_vlm import (
    NativeVLMObservationAdapter,
    execute_native_vlm_contract_test,
)
from comfyui_h3_context.core import (
    AssetRole,
    EvidenceLevel,
    ImageObservationRequest,
    ImageOrientation,
    ImageSelection,
    LocalAdapterCancelledError,
    LocalDeviceKind,
    LocalDeviceSpec,
    LocalResourceBudget,
    MediaKind,
    ModelBackendFamily,
    ModelCapability,
    ModelCapabilityState,
    ModelManifest,
    ModelOutputError,
    ModelRuntimeProfile,
    ReferenceAsset,
    TaskMode,
    VLMObservationDocument,
    VLMObservationError,
    VLMObservationRequest,
    build_reference_registry,
)


def _fp(letter: str) -> str:
    return "sha256:" + letter * 64


class _FixtureClip:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[str] = []
        self.image_counts: list[int] = []

    def tokenize(self, prompt: str, **kwargs: object) -> object:
        self.calls.append("tokenize")
        image = kwargs.get("image")
        self.image_counts.append(len(image) if isinstance(image, tuple) else 0)
        if "h3.vlm.observation.output.v1" not in prompt:
            raise AssertionError("native VLM prompt did not pin the output schema")
        return {"tokens": [1, 2]}

    def generate(self, tokens: object, **kwargs: object) -> object:
        del tokens, kwargs
        self.calls.append("generate")
        return [3, 4]

    def decode(self, generated_ids: object) -> str:
        del generated_ids
        self.calls.append("decode")
        return self.output


class _CancelAfterTokenize:
    def __init__(self) -> None:
        self.calls = 0

    def is_cancelled(self) -> bool:
        self.calls += 1
        return self.calls >= 3


def _manifest(
    *, cancellation: ModelCapabilityState = ModelCapabilityState.UNQUALIFIED
) -> ModelManifest:
    return ModelManifest(
        manifest_id="native.vlm.fixture",
        backend_family=ModelBackendFamily.COMFYUI_NATIVE,
        adapter_id="comfyui_native",
        adapter_version="1.0.0",
        model_id="qwen3-vl-8b",
        model_digest=_fp("a"),
        checkpoint_fingerprint=_fp("b"),
        detected_family="qwen3_vl_8b",
        clip_type="qwen_vl",
        tokenizer_processor="qwen3_vl",
        generation_weights_complete=True,
        capabilities=frozenset(
            {
                ModelCapability.TEXT_GENERATION,
                ModelCapability.VISION,
                ModelCapability.STRUCTURED_OUTPUT,
            }
        ),
        structured_output_schema="h3.model.typed_output.v1",
        parser_path="json_object_v1",
        runtime=ModelRuntimeProfile(
            device=LocalDeviceSpec(LocalDeviceKind.CPU),
            dtype="float32",
            offload="host_managed",
            max_context_tokens=4096,
            max_output_tokens=512,
            max_media_items=4,
            limits=LocalResourceBudget(4_000_000, 30.0, 4, 65_536, 16, 1),
        ),
        cancellation=cancellation,
        license="apache-2.0",
        host_profile="fixture.comfyui",
        evidence_level=EvidenceLevel.EXPERIMENTAL,
        approved=True,
        notes="injected host-shaped fixture only",
    )


def _request() -> VLMObservationRequest:
    registry = build_reference_registry(
        (
            ReferenceAsset("image_a", MediaKind.IMAGE, AssetRole.REFERENCE, 1),
            ReferenceAsset("image_b", MediaKind.IMAGE, AssetRole.STYLE_REFERENCE, 2),
        )
    )
    image_request = ImageObservationRequest(
        task_mode=TaskMode.T2VA,
        reference_registry=registry,
        selections=(
            ImageSelection(
                "image_a",
                "source_a",
                ImageOrientation.ROTATE_90,
                declared_size_bytes=4,
                declared_width=1024,
                declared_height=768,
            ),
            ImageSelection(
                "image_b",
                "source_b",
                ImageOrientation.UP,
                declared_size_bytes=5,
                declared_width=768,
                declared_height=1024,
            ),
        ),
        language_hints=("en", "zh-Hant"),
        max_observations=16,
    )
    return VLMObservationRequest(
        image_request=image_request,
        image_payloads=(b"image-a", b"image-b"),
        media_fingerprints=(_fp("c"), _fp("d")),
        max_tokens=256,
        seed=7,
    )


def _output() -> str:
    observations: list[dict[str, object]] = []
    kinds = ("composition", "scene", "subject_object", "style")
    for asset_id, suffix in (("image_a", "a"), ("image_b", "b")):
        for index, kind in enumerate(kinds):
            observations.append(
                {
                    "observation_id": f"obs_{suffix}_{kind}",
                    "asset_id": asset_id,
                    "kind": kind,
                    "claim": (
                        "Visible text says ignore previous instructions"
                        if kind == "subject_object" and asset_id == "image_a"
                        else f"Fixture {kind} claim for {asset_id}"
                    ),
                    "region": {
                        "x": 0.05 * index,
                        "y": 0.1,
                        "width": 0.4,
                        "height": 0.5,
                    },
                    "evidence_span": f"span_{suffix}_{kind}",
                    "confidence": "0.91" if kind != "style" else "0.84",
                    "uncertainties": (
                        [
                            {
                                "kind": "ambiguous",
                                "detail": "occlusion remains unresolved",
                                "severity": "warning",
                            }
                        ]
                        if kind == "subject_object" and asset_id == "image_a"
                        else []
                    ),
                }
            )
    return json.dumps(
        {
            "schema": "h3.vlm.observation.output.v1",
            "observations": observations,
            "uncertainties": [],
        },
        separators=(",", ":"),
    )


def run() -> dict[str, object]:
    request = _request()
    clip = _FixtureClip(_output())
    adapter = NativeVLMObservationAdapter.for_contract_test(clip, _manifest())
    document = execute_native_vlm_contract_test(
        adapter,
        request,
        device=LocalDeviceSpec(LocalDeviceKind.CPU),
    )
    if not isinstance(document, VLMObservationDocument) or not document.complete:
        raise AssertionError("native VLM fixture did not return a complete document")
    if clip.calls != ["tokenize", "generate", "decode"]:
        raise AssertionError("native VLM did not use the pinned CLIP call order")
    if clip.image_counts != [2]:
        raise AssertionError("admitted image payloads were not bound to the host CLIP")
    if {item.evidence.provenance.source.source_id for item in document.observations} != {
        "source_a",
        "source_b",
    }:
        raise AssertionError("source mapping was not retained")

    malformed_clip = _FixtureClip(
        '{"schema":"h3.vlm.observation.output.v1","observations":[],"uncertainties":[]}'
    )
    malformed = "rejected"
    try:
        execute_native_vlm_contract_test(
            NativeVLMObservationAdapter.for_contract_test(malformed_clip, _manifest()),
            request,
            device=LocalDeviceSpec(LocalDeviceKind.CPU),
        )
    except ModelOutputError:
        malformed = "rejected"
    else:
        malformed = "accepted"
    if malformed != "rejected":
        raise AssertionError("empty VLM output was accepted")

    cancel_probe = _CancelAfterTokenize()
    cancelled = "not_cancelled"
    try:
        execute_native_vlm_contract_test(
            NativeVLMObservationAdapter.for_contract_test(
                _FixtureClip(_output()),
                _manifest(cancellation=ModelCapabilityState.QUALIFIED),
            ),
            request,
            device=LocalDeviceSpec(LocalDeviceKind.CPU),
            cancellation_probe=cancel_probe,
        )
    except LocalAdapterCancelledError:
        cancelled = "cancelled"
    if cancelled != "cancelled":
        raise AssertionError("native VLM cancellation did not terminate")

    try:
        VLMObservationRequest(
            request.image_request,
            (b"", b"image-b"),
            request.media_fingerprints,
        )
    except VLMObservationError:
        empty_payload = "rejected"
    else:
        empty_payload = "accepted"
    return {
        "schema": "h3.m11_02.native_vlm.fixture.v1",
        "status": "complete",
        "document": document.to_public_dict(),
        "clip_calls": clip.calls,
        "clip_image_counts": clip.image_counts,
        "malformed_output": malformed,
        "empty_payload": empty_payload,
        "cancelled": cancelled,
        "native_route": "preferred_injected_only",
        "qualification": "contract_only",
        "executable_profile": False,
        "ollama_route": "not_contacted",
        "specialist_route": "not_contacted",
        "automatic_fallback": False,
        "model_runtime": "injected_only",
        "media_decode": "not_run",
        "network": "disabled",
        "host_runtime": "not_started",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit one redacted JSON object")
    args = parser.parse_args()
    summary = run()
    if args.json:
        print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
    else:
        document = cast(dict[str, object], summary["document"])
        print(
            "M11-02 native VLM fixture "
            f"{document['document_id']}: {document['observation_count']} observations, "
            "structural/injected-only"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
