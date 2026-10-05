"""ComfyUI-native VLM observation adapter for M11-02.

This module is an optional integration seam.  It receives a host-owned CLIP and reuses
``ComfyUINativeCLIPAdapter`` for the exact built-in Generate Text call order; it never discovers or
loads a checkpoint and never selects Ollama implicitly.
"""

from __future__ import annotations

from collections.abc import Callable

from comfyui_h3_context.core.contracts import MediaKind, TaskMode
from comfyui_h3_context.core.errors import (
    LocalAdapterCapabilityError,
    VisualQualificationError,
    VLMObservationError,
)
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
from comfyui_h3_context.core.visual_qualification import (
    VisualQualificationBinding,
    validate_visual_qualification_admission,
)
from comfyui_h3_context.core.vlm_observation import (
    MAX_VLM_OBSERVATIONS,
    VLMObservationDocument,
    VLMObservationRequest,
    build_vlm_prompt,
    parse_vlm_generation_result,
)

from .comfyui_native import ComfyUINativeCLIPAdapter, HostClip


def _descriptor(manifest: ModelManifest) -> LocalAdapterDescriptor:
    limits = manifest.runtime.limits
    return LocalAdapterDescriptor(
        adapter_id="comfyui_native_vlm",
        kind=LocalAdapterKind.PERCEPTION,
        adapter_version="1.0.0",
        minimum_version="1.0.0",
        maximum_version="1.0.0",
        supported_task_modes=frozenset(TaskMode),
        supported_media=frozenset({MediaKind.IMAGE}),
        supported_devices=frozenset(LocalDeviceKind),
        optional_dependencies=("comfy_api",),
        output_schema="h3.vlm.observation.v1",
        limits=LocalResourceBudget(
            max_memory_bytes=limits.max_memory_bytes,
            max_wall_time_seconds=limits.max_wall_time_seconds,
            max_references=limits.max_references,
            max_output_bytes=limits.max_output_bytes,
            max_output_items=max(limits.max_output_items, MAX_VLM_OBSERVATIONS),
            max_concurrency=limits.max_concurrency,
        ),
        supports_determinism=True,
        supports_seed=True,
        supports_cancellation=manifest.cancellation is ModelCapabilityState.QUALIFIED,
    )


class NativeVLMObservationAdapter(LocalAdapter):
    """Parse strict VLM observations through one host-owned ComfyUI CLIP."""

    _qualification: VisualQualificationBinding | None
    _contract_only: bool

    def __init__(
        self,
        clip: HostClip,
        manifest: ModelManifest,
        *,
        qualification: VisualQualificationBinding | None = None,
    ) -> None:
        # SECURITY: generic model approval cannot replace M11 visual qualification.
        if type(qualification) is not VisualQualificationBinding:
            raise LocalAdapterCapabilityError("native VLM requires an exact concrete binding")
        if not isinstance(manifest, ModelManifest):
            raise LocalAdapterCapabilityError("native VLM manifest is invalid")
        try:
            validate_visual_qualification_admission(qualification, manifest)
        except VisualQualificationError as exc:
            if "runtime manifest does not match" in str(exc):
                raise LocalAdapterCapabilityError(
                    "native VLM qualification manifest mismatch"
                ) from exc
            if "malformed evidence" in str(exc):
                raise LocalAdapterCapabilityError("native VLM malformed qualification") from exc
            raise
        self._initialize(clip, manifest)
        self._qualification = qualification
        self._contract_only = False

    @classmethod
    def for_contract_test(
        cls, clip: HostClip, manifest: ModelManifest
    ) -> NativeVLMObservationAdapter:
        """Build an injected parser/call-order fixture with no runtime-qualification claim."""

        instance = cls.__new__(cls)
        instance._initialize(clip, manifest)
        instance._qualification = None
        instance._contract_only = True
        return instance

    def _initialize(self, clip: HostClip, manifest: ModelManifest) -> None:
        if not isinstance(manifest, ModelManifest):
            raise LocalAdapterCapabilityError("native VLM manifest is invalid")
        if ModelCapability.VISION not in manifest.capabilities:
            raise LocalAdapterCapabilityError("native VLM manifest requires vision capability")
        self._generation = ComfyUINativeCLIPAdapter(clip, manifest)
        self._descriptor = _descriptor(manifest)
        self._manifest = manifest

    @property
    def contract_only(self) -> bool:
        return self._contract_only

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._descriptor

    @property
    def qualified_device(self) -> LocalDeviceSpec:
        return self._manifest.runtime.device

    def run(
        self, request: LocalAdapterExecutionRequest, guard: LocalBudgetGuard
    ) -> LocalAdapterResult:
        if not isinstance(request.input_value, VLMObservationRequest):
            raise LocalAdapterCapabilityError("native VLM adapter requires VLMObservationRequest")
        if not isinstance(guard, LocalBudgetGuard):
            raise LocalAdapterCapabilityError("native VLM adapter requires LocalBudgetGuard")
        observation_request = request.input_value
        generation_request = ModelGenerationRequest(
            prompt=build_vlm_prompt(observation_request),
            max_tokens=observation_request.max_tokens,
            output_schema=observation_request.output_schema,
            do_sample=False,
            temperature=0.0,
            top_k=0,
            top_p=1.0,
            min_p=0.0,
            repetition_penalty=1.0,
            presence_penalty=0.0,
            seed=observation_request.seed,
            thinking=False,
            use_default_template=True,
            media_fingerprints=observation_request.media_fingerprints,
            image=observation_request.image_payloads,
            image_payloads=observation_request.image_payloads,
        )
        inner_request = LocalAdapterExecutionRequest(
            adapter_id=self._generation.descriptor.adapter_id,
            task_mode=observation_request.image_request.task_mode,
            media_kinds=(MediaKind.IMAGE,),
            reference_count=len(observation_request.image_request.selections),
            device=request.device,
            estimated_memory_bytes=request.estimated_memory_bytes,
            estimated_wall_time_seconds=request.estimated_wall_time_seconds,
            estimated_output_bytes=request.estimated_output_bytes,
            deterministic_required=request.deterministic_required,
            seed=observation_request.seed,
            cancellation_required=request.cancellation_required,
            input_value=generation_request,
        )
        generated = self._generation.run(inner_request, guard)
        if not isinstance(generated.value, ModelGenerationResult):
            raise VLMObservationError("native CLIP adapter did not return ModelGenerationResult")
        document = parse_vlm_generation_result(
            generated.value,
            observation_request,
            device=request.device,
        )
        if not isinstance(document, VLMObservationDocument) or not document.complete:
            raise VLMObservationError("native VLM document is not complete")
        return LocalAdapterResult(
            adapter_id=self.descriptor.adapter_id,
            adapter_version=self.descriptor.adapter_version,
            device=request.device,
            value=document,
            output_bytes=generated.output_bytes,
            output_items=1,
        )


def execute_native_vlm_observation(
    adapter: NativeVLMObservationAdapter,
    request: VLMObservationRequest,
    *,
    runtime: LocalAdapterRuntime | None = None,
    device: LocalDeviceSpec | None = None,
    cancellation_probe: LocalCancellationProbe | None = None,
    clock: Callable[[], float] | None = None,
    memory_meter: Callable[[], int] | None = None,
) -> VLMObservationDocument:
    """Execute one explicitly selected native VLM profile through bounded local runtime."""

    if not isinstance(adapter, NativeVLMObservationAdapter):
        raise VLMObservationError("adapter must be NativeVLMObservationAdapter")
    if adapter.contract_only or adapter._qualification is None:
        raise VLMObservationError("contract-only native VLM adapter cannot execute runtime lane")
    device_value = adapter.qualified_device if device is None else device
    if not isinstance(device_value, LocalDeviceSpec):
        raise VLMObservationError("device must be LocalDeviceSpec")
    kind = device_value.kind
    kind_value = kind.value if hasattr(kind, "value") else str(kind)
    validate_visual_qualification_admission(
        adapter._qualification, adapter._manifest, kind_value, device_value.index
    )
    return _execute_native_vlm(
        adapter,
        request,
        runtime=runtime,
        device=device_value,
        cancellation_probe=cancellation_probe,
        clock=clock,
        memory_meter=memory_meter,
    )


def execute_native_vlm_contract_test(
    adapter: NativeVLMObservationAdapter,
    request: VLMObservationRequest,
    *,
    runtime: LocalAdapterRuntime | None = None,
    device: LocalDeviceSpec | None = None,
    cancellation_probe: LocalCancellationProbe | None = None,
    clock: Callable[[], float] | None = None,
    memory_meter: Callable[[], int] | None = None,
) -> VLMObservationDocument:
    """Exercise injected parsing and call order without claiming qualified runtime execution."""

    if not isinstance(adapter, NativeVLMObservationAdapter) or not adapter.contract_only:
        raise VLMObservationError("contract test requires a contract-only native VLM adapter")
    return _execute_native_vlm(
        adapter,
        request,
        runtime=runtime,
        device=device,
        cancellation_probe=cancellation_probe,
        clock=clock,
        memory_meter=memory_meter,
    )


def _execute_native_vlm(
    adapter: NativeVLMObservationAdapter,
    request: VLMObservationRequest,
    *,
    runtime: LocalAdapterRuntime | None,
    device: LocalDeviceSpec | None,
    cancellation_probe: LocalCancellationProbe | None,
    clock: Callable[[], float] | None,
    memory_meter: Callable[[], int] | None,
) -> VLMObservationDocument:
    if not isinstance(request, VLMObservationRequest):
        raise VLMObservationError("request must be VLMObservationRequest")
    device_value = LocalDeviceSpec(LocalDeviceKind.AUTO) if device is None else device
    if not isinstance(device_value, LocalDeviceSpec):
        raise VLMObservationError("device must be LocalDeviceSpec")
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
    if not isinstance(result.value, VLMObservationDocument):
        raise VLMObservationError("native VLM result did not contain a document")
    return result.value


__all__ = [
    "NativeVLMObservationAdapter",
    "execute_native_vlm_contract_test",
    "execute_native_vlm_observation",
]
