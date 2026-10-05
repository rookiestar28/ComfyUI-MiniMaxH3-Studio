"""Explicit ComfyUI-native model adapter seam for M10-03.

The adapter consumes a host-owned ``CLIP`` object supplied by a node/host integration. It never
loads a checkpoint, scans model directories, imports ComfyUI, or claims capability from a filename.
The host object is intentionally runtime-only and is exercised through a small structural protocol
in tests.
"""

from __future__ import annotations

from typing import Protocol

from comfyui_h3_context.core.contracts import MediaKind, TaskMode
from comfyui_h3_context.core.errors import LocalAdapterCapabilityError, ModelOutputError
from comfyui_h3_context.core.local_adapters import (
    LocalAdapter,
    LocalAdapterDescriptor,
    LocalAdapterExecutionRequest,
    LocalAdapterKind,
    LocalAdapterResult,
    LocalBudgetGuard,
    LocalDeviceKind,
)
from comfyui_h3_context.core.model_manifest import (
    ModelBackendFamily,
    ModelCapability,
    ModelGenerationRequest,
    ModelManifest,
    build_model_result,
    validate_model_generation_request,
)


class HostClip(Protocol):
    """Minimal host-owned ComfyUI CLIP surface used by the pinned TextGenerate path."""

    def tokenize(
        self,
        prompt: str,
        *,
        image: object = None,
        skip_template: bool = False,
        min_length: int = 1,
        thinking: bool = False,
        video: object = None,
        audio: object = None,
    ) -> object: ...

    def generate(
        self,
        tokens: object,
        *,
        do_sample: bool,
        max_length: int,
        temperature: float,
        top_k: int,
        top_p: float,
        min_p: float,
        repetition_penalty: float,
        presence_penalty: float,
        seed: int | None,
    ) -> object: ...

    def decode(self, generated_ids: object) -> str: ...


def _descriptor(manifest: ModelManifest) -> LocalAdapterDescriptor:
    supported_media = frozenset(
        {
            media
            for capability, media in (
                (ModelCapability.VISION, MediaKind.IMAGE),
                (ModelCapability.VIDEO, MediaKind.VIDEO),
                (ModelCapability.AUDIO, MediaKind.AUDIO),
            )
            if capability in manifest.capabilities
        }
    )
    return LocalAdapterDescriptor(
        adapter_id=manifest.adapter_id,
        kind=LocalAdapterKind.REASONING,
        adapter_version=manifest.adapter_version,
        minimum_version=manifest.adapter_version,
        maximum_version=manifest.adapter_version,
        supported_task_modes=frozenset(TaskMode),
        supported_media=supported_media,
        supported_devices=frozenset(LocalDeviceKind),
        optional_dependencies=("comfy_api",),
        output_schema="h3.model.generation.result.v1",
        limits=manifest.runtime.limits,
        supports_determinism=True,
        supports_seed=True,
        supports_cancellation=manifest.cancellation.value == "qualified",
    )


class ComfyUINativeCLIPAdapter(LocalAdapter):
    """Thin adapter around one explicitly supplied, host-resident ComfyUI CLIP."""

    def __init__(self, clip: HostClip, manifest: ModelManifest) -> None:
        if not isinstance(manifest, ModelManifest):
            raise LocalAdapterCapabilityError("native model manifest is invalid")
        if manifest.backend_family is not ModelBackendFamily.COMFYUI_NATIVE:
            raise LocalAdapterCapabilityError("native adapter received a non-native manifest")
        if not manifest.approved or not manifest.supports_generation:
            raise LocalAdapterCapabilityError(
                "native model manifest is not approved for generation"
            )
        if clip is None:
            raise LocalAdapterCapabilityError("native adapter requires a host-owned CLIP")
        self._clip = clip
        self._manifest = manifest
        self._descriptor = _descriptor(manifest)

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._descriptor

    def run(
        self, request: LocalAdapterExecutionRequest, guard: LocalBudgetGuard
    ) -> LocalAdapterResult:
        if not isinstance(request.input_value, ModelGenerationRequest):
            raise LocalAdapterCapabilityError("native adapter requires ModelGenerationRequest")
        generation = request.input_value
        validate_model_generation_request(self._manifest, generation)
        guard.checkpoint()
        tokens = self._clip.tokenize(
            generation.prompt,
            image=generation.image,
            skip_template=not generation.use_default_template,
            min_length=1,
            thinking=generation.thinking,
            video=generation.video,
            audio=generation.audio,
        )
        guard.checkpoint()
        generated_ids = self._clip.generate(
            tokens,
            do_sample=generation.do_sample,
            max_length=generation.max_tokens,
            temperature=generation.temperature,
            top_k=generation.top_k,
            top_p=generation.top_p,
            min_p=generation.min_p,
            repetition_penalty=generation.repetition_penalty,
            presence_penalty=generation.presence_penalty,
            seed=generation.seed,
        )
        guard.checkpoint()
        text = self._clip.decode(generated_ids)
        if not isinstance(text, str) or not text:
            raise ModelOutputError("native CLIP decode returned no text")
        result = build_model_result(self._manifest, generation, text)
        guard.record_output(byte_count=len(text.encode("utf-8")), item_count=1)
        return LocalAdapterResult(
            adapter_id=self.descriptor.adapter_id,
            adapter_version=self.descriptor.adapter_version,
            device=request.device,
            value=result,
            output_bytes=len(text.encode("utf-8")),
            output_items=1,
        )


__all__ = ["ComfyUINativeCLIPAdapter", "HostClip"]
