"""ComfyUI-native exact OCR adapter seam for M11-03.

The adapter only consumes a host-owned CLIP supplied by the integration layer.  It does not scan
model directories, load checkpoints, invoke OCR packages, or select Ollama implicitly.
"""

from __future__ import annotations

from collections.abc import Callable

from comfyui_h3_context.core.contracts import MediaKind, TaskMode
from comfyui_h3_context.core.errors import LocalAdapterCapabilityError, OCRObservationError
from comfyui_h3_context.core.local_adapters import (
    LocalAdapter,
    LocalAdapterDescriptor,
    LocalAdapterExecutionRequest,
    LocalAdapterKind,
    LocalAdapterResult,
    LocalAdapterRuntime,
    LocalBudgetGuard,
    LocalCancellationProbe,
    LocalDeviceKind,
    LocalDeviceSpec,
    LocalResourceBudget,
    run_local_adapter,
)
from comfyui_h3_context.core.model_manifest import (
    ModelCapability,
    ModelCapabilityState,
    ModelGenerationRequest,
    ModelGenerationResult,
    ModelManifest,
)
from comfyui_h3_context.core.ocr_observation import (
    MAX_OCR_CANDIDATES,
    OCRObservationDocument,
    OCRObservationRequest,
    build_ocr_prompt,
    parse_ocr_generation_result,
)

from .comfyui_native import ComfyUINativeCLIPAdapter, HostClip


def _descriptor(manifest: ModelManifest) -> LocalAdapterDescriptor:
    limits = manifest.runtime.limits
    return LocalAdapterDescriptor(
        adapter_id="comfyui_native_ocr",
        kind=LocalAdapterKind.PERCEPTION,
        adapter_version="1.0.0",
        minimum_version="1.0.0",
        maximum_version="1.0.0",
        supported_task_modes=frozenset(TaskMode),
        supported_media=frozenset({MediaKind.IMAGE}),
        supported_devices=frozenset(LocalDeviceKind),
        optional_dependencies=("comfy_api",),
        output_schema="h3.ocr.observation.v1",
        limits=LocalResourceBudget(
            max_memory_bytes=limits.max_memory_bytes,
            max_wall_time_seconds=limits.max_wall_time_seconds,
            max_references=limits.max_references,
            max_output_bytes=limits.max_output_bytes,
            max_output_items=max(limits.max_output_items, MAX_OCR_CANDIDATES),
            max_concurrency=limits.max_concurrency,
        ),
        supports_determinism=True,
        supports_seed=True,
        supports_cancellation=manifest.cancellation is ModelCapabilityState.QUALIFIED,
    )


class NativeOCRObservationAdapter(LocalAdapter):
    """Parse exact OCR through one explicitly supplied native ComfyUI CLIP."""

    def __init__(self, clip: HostClip, manifest: ModelManifest) -> None:
        if not isinstance(manifest, ModelManifest):
            raise LocalAdapterCapabilityError("native OCR manifest is invalid")
        if ModelCapability.VISION not in manifest.capabilities:
            raise LocalAdapterCapabilityError("native OCR manifest requires vision capability")
        self._generation = ComfyUINativeCLIPAdapter(clip, manifest)
        self._descriptor = _descriptor(manifest)

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._descriptor

    def run(
        self, request: LocalAdapterExecutionRequest, guard: LocalBudgetGuard
    ) -> LocalAdapterResult:
        if not isinstance(request.input_value, OCRObservationRequest):
            raise LocalAdapterCapabilityError("native OCR adapter requires OCRObservationRequest")
        if not isinstance(guard, LocalBudgetGuard):
            raise LocalAdapterCapabilityError("native OCR adapter requires LocalBudgetGuard")
        ocr_request = request.input_value
        generation_request = ModelGenerationRequest(
            prompt=build_ocr_prompt(ocr_request),
            max_tokens=1024,
            output_schema=ocr_request.output_schema,
            do_sample=False,
            temperature=0.0,
            top_k=0,
            top_p=1.0,
            min_p=0.0,
            repetition_penalty=1.0,
            presence_penalty=0.0,
            seed=ocr_request.seed,
            thinking=False,
            use_default_template=True,
            media_fingerprints=ocr_request.media_fingerprints,
            image=ocr_request.image_payloads,
            image_payloads=ocr_request.image_payloads,
        )
        inner_request = LocalAdapterExecutionRequest(
            adapter_id=self._generation.descriptor.adapter_id,
            task_mode=ocr_request.image_request.task_mode,
            media_kinds=(MediaKind.IMAGE,),
            reference_count=len(ocr_request.image_request.selections),
            device=request.device,
            estimated_memory_bytes=request.estimated_memory_bytes,
            estimated_wall_time_seconds=request.estimated_wall_time_seconds,
            estimated_output_bytes=request.estimated_output_bytes,
            deterministic_required=request.deterministic_required,
            seed=ocr_request.seed,
            cancellation_required=request.cancellation_required,
            input_value=generation_request,
        )
        generated = self._generation.run(inner_request, guard)
        if not isinstance(generated.value, ModelGenerationResult):
            raise OCRObservationError("native CLIP adapter did not return ModelGenerationResult")
        document = parse_ocr_generation_result(generated.value, ocr_request, device=request.device)
        if not isinstance(document, OCRObservationDocument) or not document.complete:
            raise OCRObservationError("native OCR document is not complete")
        return LocalAdapterResult(
            adapter_id=self.descriptor.adapter_id,
            adapter_version=self.descriptor.adapter_version,
            device=request.device,
            value=document,
            output_bytes=generated.output_bytes,
            output_items=len(document.candidates),
        )


def execute_native_ocr_observation(
    adapter: NativeOCRObservationAdapter,
    request: OCRObservationRequest,
    *,
    runtime: LocalAdapterRuntime | None = None,
    device: LocalDeviceSpec | None = None,
    cancellation_probe: LocalCancellationProbe | None = None,
    clock: Callable[[], float] | None = None,
    memory_meter: Callable[[], int] | None = None,
) -> OCRObservationDocument:
    """Execute one explicitly selected native OCR profile through bounded local runtime."""

    if not isinstance(adapter, NativeOCRObservationAdapter):
        raise OCRObservationError("adapter must be NativeOCRObservationAdapter")
    if not isinstance(request, OCRObservationRequest):
        raise OCRObservationError("request must be OCRObservationRequest")
    device_value = LocalDeviceSpec(LocalDeviceKind.AUTO) if device is None else device
    if not isinstance(device_value, LocalDeviceSpec):
        raise OCRObservationError("device must be LocalDeviceSpec")
    execution_request = LocalAdapterExecutionRequest(
        adapter_id=adapter.descriptor.adapter_id,
        task_mode=request.image_request.task_mode,
        media_kinds=(MediaKind.IMAGE,),
        reference_count=len(request.image_request.selections),
        device=device_value,
        estimated_memory_bytes=sum(len(payload) for payload in request.image_payloads),
        estimated_output_bytes=adapter.descriptor.limits.max_output_bytes,
        deterministic_required=True,
        seed=request.seed,
        cancellation_required=cancellation_probe is not None,
        input_value=request,
    )
    if clock is not None:
        result = run_local_adapter(
            adapter,
            execution_request,
            runtime=runtime,
            cancellation_probe=cancellation_probe,
            clock=clock,
            memory_meter=memory_meter,
        )
    else:
        result = run_local_adapter(
            adapter,
            execution_request,
            runtime=runtime,
            cancellation_probe=cancellation_probe,
            memory_meter=memory_meter,
        )
    if not isinstance(result.value, OCRObservationDocument):
        raise OCRObservationError("native OCR result did not contain a document")
    return result.value


__all__ = ["NativeOCRObservationAdapter", "execute_native_ocr_observation"]
