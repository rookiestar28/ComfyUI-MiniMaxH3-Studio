"""Offline M10-08 runtime-foundation integration fixture.

The fixture deliberately injects all runtime values.  It exercises the public seams in the
same order as a selected local execution, but never loads a checkpoint, opens a decoder, starts
ComfyUI, or contacts Ollama.  Its JSON output is a redacted acceptance summary rather than a
prompt/media dump.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import cast

from comfyui_h3_context.adapters.comfyui_native import ComfyUINativeCLIPAdapter
from comfyui_h3_context.core import (
    ArtifactCachePolicy,
    BackendRuntimeQualification,
    CancellationScope,
    CoordinatorCacheIdentity,
    CoordinatorCapability,
    CoordinatorCapacity,
    CoordinatorExecutionRequest,
    CoordinatorProfile,
    CoordinatorResourceBudget,
    EvidenceLevel,
    EvidenceOrigin,
    EvidenceRecord,
    EvidenceSource,
    EvidenceSourceKind,
    ExecutionCoordinator,
    GraphAnchor,
    GraphAnchorRole,
    GraphBindingError,
    ImageObservation,
    ImageObservationBatch,
    ImageObservationBatchStatus,
    ImageObservationKind,
    ImageObservationRequest,
    ImageOrientation,
    ImageRegion,
    ImageSelection,
    LocalAdapterDescriptor,
    LocalAdapterExecutionRequest,
    LocalAdapterKind,
    LocalAdapterResult,
    LocalBudgetGuard,
    LocalDeviceKind,
    LocalDeviceSpec,
    LocalResourceBudget,
    MediaAdmissionDecision,
    MediaAdmissionRequest,
    MediaAllowlist,
    MediaKind,
    MediaLimits,
    ModelBackendFamily,
    ModelCapability,
    ModelCapabilityState,
    ModelGenerationRequest,
    ModelGenerationResult,
    ModelManifest,
    ModelRuntimeProfile,
    PreprocessingSpec,
    ProviderIdentity,
    RawContextRequest,
    ReferenceRegistry,
    ResourceOwnershipPolicy,
    SourceIdentity,
    SupportStatus,
    TaskMode,
    admit_media_source,
    build_public_manifest,
    execute_image_observation,
    graph_to_manifest,
    manifest_to_graph,
    plan_base_timeline,
)
from comfyui_h3_context.core.evidence import Provenance
from comfyui_h3_context.core.security import (
    MediaSource,
    MediaSourceKind,
    MediaTransferPolicy,
    MediaTransferTimeouts,
)
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextNativeH3AdapterNode,
    H3ContextPlanNode,
    H3ContextPreviewNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
    H3ReferenceRegistryNode,
    NativeH3AdapterNodeError,
)


def _fp(letter: str) -> str:
    return "sha256:" + letter * 64


@dataclass(frozen=True, slots=True)
class ObservationPayload:
    """Coordinator-safe projection of a runtime observation batch."""

    batch: ImageObservationBatch

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.batch.schema,
            "status": self.batch.status.value,
            "batch_id": self.batch.batch_id,
            "selected_count": len(self.batch.selected_asset_ids),
            "observation_count": len(self.batch.observations),
            "visible_text_count": len(self.batch.visible_text),
        }


class _FixtureClip:
    """Minimal host-owned CLIP protocol used only by the native adapter fixture."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def tokenize(self, prompt: str, **kwargs: object) -> object:
        del prompt, kwargs
        self.calls.append("tokenize")
        return {"tokens": [1, 2]}

    def generate(self, tokens: object, **kwargs: object) -> object:
        del tokens, kwargs
        self.calls.append("generate")
        return [3, 4]

    def decode(self, generated_ids: object) -> str:
        del generated_ids
        self.calls.append("decode")
        return '{"objects":[{"label":"blue_door"}]}'


def _native_manifest() -> ModelManifest:
    return ModelManifest(
        manifest_id="native.fixture_qwen3_vl",
        backend_family=ModelBackendFamily.COMFYUI_NATIVE,
        adapter_id="comfyui_native",
        adapter_version="1.0.0",
        model_id="qwen3-vl-8b",
        model_digest=_fp("c"),
        checkpoint_fingerprint=_fp("d"),
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
            dtype="float16",
            offload="host_managed",
            max_context_tokens=4096,
            max_output_tokens=128,
            max_media_items=4,
            limits=LocalResourceBudget(2_000_000, 10.0, 4, 32_000, 4, 1),
        ),
        cancellation=ModelCapabilityState.UNQUALIFIED,
        license="apache-2.0",
        host_profile="fixture.comfyui",
        evidence_level=EvidenceLevel.EXPERIMENTAL,
        approved=True,
        notes="fixture-qualified-only",
    )


def _descriptor(adapter_id: str, *, cancellation: bool) -> LocalAdapterDescriptor:
    return LocalAdapterDescriptor(
        adapter_id=adapter_id,
        kind=LocalAdapterKind.PERCEPTION,
        adapter_version="1.0.0",
        minimum_version="1.0.0",
        maximum_version="1.0.0",
        supported_task_modes=frozenset(TaskMode),
        supported_media=frozenset({MediaKind.IMAGE}),
        supported_devices=frozenset({LocalDeviceKind.AUTO, LocalDeviceKind.CPU}),
        optional_dependencies=("fixture.runtime",),
        output_schema="h3.image.observation.v1",
        limits=LocalResourceBudget(2_000_000, 10.0, 4, 32_000, 32, 1),
        supports_determinism=True,
        supports_seed=True,
        supports_cancellation=cancellation,
    )


def _observation_batch(
    request: ImageObservationRequest, *, provider_version: str, source_revision: str
) -> ImageObservationBatch:
    observations: list[ImageObservation] = []
    claims = ("A blue door is visible.", "A warm light is visible.")
    for index, selection in enumerate(request.selections):
        source = EvidenceSource(
            EvidenceSourceKind.MEDIA_ASSET,
            selection.source_id,
            asset_id=selection.asset_id,
            span=f"frame.{index + 1}",
        )
        evidence = EvidenceRecord(
            f"observation.{selection.asset_id}",
            claims[index % len(claims)],
            EvidenceOrigin.OBSERVED,
            SupportStatus.SUPPORTED,
            Provenance(
                source,
                ProviderIdentity.LOCAL,
                EvidenceLevel.EXPERIMENTAL,
                provider_version=provider_version,
                source_revision=source_revision,
            ),
            confidence=Decimal("0.75"),
        )
        observations.append(
            ImageObservation(
                evidence.evidence_id,
                selection.asset_id,
                ImageObservationKind.SCENE,
                evidence,
                ImageRegion(0.1 + index * 0.1, 0.2, 0.5, 0.5),
                ImageOrientation.UP,
            )
        )
    return ImageObservationBatch(
        batch_id=f"batch.{provider_version.replace('.', '_')}",
        schema="h3.image.observation.v1",
        status=ImageObservationBatchStatus.COMPLETE,
        selected_asset_ids=request.selected_asset_ids,
        observations=tuple(observations),
    )


class _FixtureObserver:
    """Model-free observer and coordinator adapter used for the baseline lane."""

    def __init__(self, *, fail: bool = False) -> None:
        self._descriptor = _descriptor("fixture.observer", cancellation=True)
        self._fail = fail

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._descriptor

    def observe(
        self, request: ImageObservationRequest, guard: LocalBudgetGuard
    ) -> ImageObservationBatch:
        if self._fail:
            raise RuntimeError("fixture observer failure")
        guard.checkpoint()
        return _observation_batch(
            request, provider_version="fixture.observer.1", source_revision="fixture.weights.1"
        )

    def run(
        self, request: LocalAdapterExecutionRequest, guard: LocalBudgetGuard
    ) -> LocalAdapterResult:
        value = request.input_value
        if not isinstance(value, ImageObservationRequest):
            raise TypeError("fixture observer requires an ImageObservationRequest")
        batch = self.observe(value, guard)
        return LocalAdapterResult(
            self.descriptor.adapter_id,
            self.descriptor.adapter_version,
            request.device,
            ObservationPayload(batch),
            output_bytes=128,
            output_items=len(batch.observations),
        )


class _NativeObservationAdapter:
    """Observation-shaped wrapper around the preferred host-owned native CLIP adapter."""

    def __init__(self) -> None:
        self.clip = _FixtureClip()
        self._native = ComfyUINativeCLIPAdapter(self.clip, _native_manifest())
        self._descriptor = _descriptor("comfyui_native.fixture_vlm", cancellation=False)

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._descriptor

    def observe(
        self, request: ImageObservationRequest, guard: LocalBudgetGuard
    ) -> ImageObservationBatch:
        generation = ModelGenerationRequest(
            prompt="Describe the selected image as one JSON object.",
            max_tokens=64,
            do_sample=False,
            seed=7,
            media_fingerprints=(_fp("a"), _fp("b")),
            image=object(),
        )
        native_request = LocalAdapterExecutionRequest(
            adapter_id="comfyui_native",
            task_mode=request.task_mode,
            media_kinds=(MediaKind.IMAGE,),
            reference_count=len(request.selections),
            device=LocalDeviceSpec(LocalDeviceKind.CPU),
            deterministic_required=True,
            seed=7,
            input_value=generation,
        )
        native_result = self._native.run(native_request, guard)
        typed = cast(ModelGenerationResult, native_result.value)
        if not isinstance(typed.parsed_output, Mapping):
            raise TypeError("native fixture did not produce a structured object")
        guard.checkpoint()
        return _observation_batch(
            request, provider_version="comfyui.native.1", source_revision="fixture.qwen3vl.1"
        )

    def run(
        self, request: LocalAdapterExecutionRequest, guard: LocalBudgetGuard
    ) -> LocalAdapterResult:
        value = request.input_value
        if not isinstance(value, ImageObservationRequest):
            raise TypeError("native observation adapter requires an ImageObservationRequest")
        batch = self.observe(value, guard)
        return LocalAdapterResult(
            self.descriptor.adapter_id,
            self.descriptor.adapter_version,
            request.device,
            ObservationPayload(batch),
            output_bytes=128,
            output_items=len(batch.observations),
        )


def _media_admission(root: Path) -> tuple[MediaAdmissionDecision, ...]:
    limits = MediaLimits(
        max_bytes=1_000_000,
        max_duration_seconds=Decimal("60"),
        max_width=4096,
        max_height=4096,
        max_frame_rate=Decimal("120"),
        max_frames=512,
        max_sample_rate=96_000,
        max_channels=8,
        max_references=4,
        max_temp_bytes=2_000_000,
        max_decoded_bytes=4_000_000,
        max_probe_stdout_bytes=64_000,
        max_probe_stderr_bytes=8_000,
        max_wall_time_seconds=Decimal("10"),
        max_redirects=2,
    )
    allowlist = MediaAllowlist(
        protocols=("file", "https"),
        containers=("png", "mp4"),
        video_codecs=("h264", "png"),
        audio_codecs=("aac", "pcm_s16le"),
    )
    preprocessing = PreprocessingSpec(
        revision="fixture-prep-1",
        operations=("exif_orientation", "rgb24", "source_pts"),
        target_pixel_format="rgb24",
        target_audio_sample_rate=16_000,
        target_audio_channels=1,
    )
    policy = MediaTransferPolicy(
        allowed_hosts=("media.example.test",),
        allowed_url_path_prefixes=("/assets/",),
        allowed_local_roots=(root,),
        max_bytes=1_000_000,
        max_duration_seconds=60.0,
        max_references=4,
        timeouts=MediaTransferTimeouts(1.0, 2.0, 4.0),
        max_redirects=2,
    )
    decisions: list[MediaAdmissionDecision] = []
    for index, (source_letter, identity_letter) in enumerate((("a", "b"), ("c", "d"))):
        path = root / f"frame_{index}.png"
        path.write_bytes(b"x" * 1024)
        source = MediaSource(
            MediaSourceKind.LOCAL_PATH,
            path,
            MediaKind.IMAGE,
            "image/png",
            1024,
        )
        identity = SourceIdentity(_fp(source_letter), 1024, _fp(identity_letter))
        request = MediaAdmissionRequest(
            source=source,
            expected_kind=MediaKind.IMAGE,
            transfer_policy=policy,
            limits=limits,
            allowlist=allowlist,
            preprocessing=preprocessing,
            expected_identity=identity,
        )
        decisions.append(admit_media_source(request, opened_identity=identity))
    return tuple(decisions)


def _pipeline_request() -> tuple[RawContextRequest, ReferenceRegistry, ImageObservationRequest]:
    raw = H3ContextRequestNode().build_request(
        TaskMode.FL2VA,
        "A lantern moves through a quiet station.",
        duration_seconds=5.0,
    )[0]
    registry = H3ReferenceRegistryNode().build_registry(
        first_frame=[object()], last_frame=[object()]
    )[0]
    raw = replace(raw, reference_registry=registry)
    selections = tuple(
        ImageSelection(
            asset.asset_id,
            f"source.{asset.asset_id}",
            orientation=ImageOrientation.UP,
            declared_size_bytes=1024,
            declared_width=640,
            declared_height=360,
        )
        for asset in registry.assets
    )
    observation_request = ImageObservationRequest(TaskMode.FL2VA, registry, selections)
    return raw, registry, observation_request


def _coordinator_request(
    adapter_id: str,
    observation_request: ImageObservationRequest,
    *,
    model_digest: str,
    cancellation_required: bool = False,
) -> CoordinatorExecutionRequest:
    local = LocalAdapterExecutionRequest(
        adapter_id=adapter_id,
        task_mode=TaskMode.FL2VA,
        media_kinds=(MediaKind.IMAGE,),
        reference_count=len(observation_request.selections),
        device=LocalDeviceSpec(LocalDeviceKind.CPU),
        estimated_memory_bytes=1024,
        deterministic_required=True,
        seed=7,
        cancellation_required=cancellation_required,
        input_value=observation_request,
    )
    return CoordinatorExecutionRequest(
        profile=CoordinatorProfile.PROFILE_2,
        cache_identity=CoordinatorCacheIdentity(
            operation_id=f"h3.m10_08.{adapter_id}",
            contract_schema="h3.image.observation.v1",
            renderer_profile="h3.base.v1",
            prompt_profile="h3.base.v1",
            provider_id="local",
            adapter_id=adapter_id,
            adapter_version="1.0.0",
            server_version="none",
            model_digest=model_digest,
            tokenizer_revision="fixture.tokenizer.1",
            processor_revision="fixture.processor.1",
            preprocessing_revision="fixture-prep-1",
            settings_fingerprint=_fp("e"),
            source_fingerprints=(_fp("a"), _fp("c")),
        ),
        budget=CoordinatorResourceBudget(
            cpu_threads=1,
            gpu_slots=0,
            ram_bytes=2_000_000,
            vram_bytes=0,
            max_wall_time_seconds=5.0,
            max_concurrency=1,
            max_output_bytes=32_000,
            max_output_items=4,
            max_references=2,
        ),
        adapter_request=local,
        qualification=BackendRuntimeQualification(
            ModelBackendFamily.COMFYUI_NATIVE,
            ModelCapabilityState.QUALIFIED,
            ModelCapabilityState.QUALIFIED,
        ),
        ownership=ResourceOwnershipPolicy.for_backend(ModelBackendFamily.COMFYUI_NATIVE),
        required_capabilities=frozenset({CoordinatorCapability.LOCAL_MULTIMODAL}),
        artifact_policy=ArtifactCachePolicy(),
        cancellation_required=cancellation_required,
    )


def _run_pipeline(
    raw: RawContextRequest, registry: ReferenceRegistry, batch: ImageObservationBatch
) -> dict[str, object]:
    node_plan = H3ContextPlanNode().build_plan(raw, registry)[0]
    result = plan_base_timeline(raw, batch)
    if result.plan is None:
        raise RuntimeError("fixture Base planning did not produce a plan")
    plan = result.plan
    _, compiled_report, document = H3ContextCompilerNode().compile(plan)
    validation, validated_report = H3ContextValidatorNode().validate(plan, document)
    terminal = H3ContextPreviewNode().emit(
        validated_report,
        prompt_id="fixture.prompt",
        execution_node_id="fixture.preview",
    )
    native_prompt, wiring = H3ContextNativeH3AdapterNode().adapt(validated_report)
    stale_rejected = False
    try:
        H3ContextNativeH3AdapterNode().adapt(compiled_report)
    except NativeH3AdapterNodeError:
        stale_rejected = True
    projection = cast(dict[str, object], terminal["ui"])
    return {
        "node_plan": "complete",
        "plan_id_present": bool(node_plan.plan_id),
        "planned_evidence_count": len(plan.evidence.records),
        "rendered": document.status.value,
        "validated": validation.status.value,
        "validated_report": validated_report.validation.status.value,
        "ui_projection_schema": projection.get("schema"),
        "ui_has_result": "result" in terminal,
        "native_node_id": wiring.native_node_id,
        "native_validation": wiring.validation_status.value,
        "native_binding_count": len(wiring.bindings),
        "native_prompt_matches": native_prompt == validated_report.prompt_document.text,
        "native_prompt_fingerprint": wiring.prompt_fingerprint,
        "stale_report_rejected": stale_rejected,
    }


def _run_graph_fixture() -> dict[str, object]:
    manifest = build_public_manifest()
    base = manifest_to_graph(manifest, "workflow.m3_07.base")
    reference = manifest_to_graph(
        manifest,
        "workflow.m3_07.reference",
        anchors=(
            GraphAnchor(GraphAnchorRole.REFERENCE, "workflow.m3_07.reference.n1", "images", 1),
        ),
    )
    child = manifest_to_graph(
        manifest,
        "workflow.m3_07.base",
        node_ids=tuple(item.node_id for item in base.nodes[:2]),
        instance_prefix="fixture.child",
    )
    root = manifest_to_graph(
        manifest,
        "workflow.m3_07.base",
        node_ids=tuple(item.node_id for item in base.nodes[2:]),
        instance_prefix="fixture.root",
        children=(child,),
        transparent=True,
    )
    missing_rejected = False
    try:
        graph_to_manifest(reference, manifest, required_anchor_roles=(GraphAnchorRole.FIRST_FRAME,))
    except GraphBindingError:
        missing_rejected = True
    ambiguous_rejected = False
    try:
        graph_to_manifest(
            manifest_to_graph(
                manifest,
                "workflow.m3_07.reference",
                anchors=(
                    GraphAnchor(
                        GraphAnchorRole.REFERENCE, "workflow.m3_07.reference.n1", "images", 1
                    ),
                    GraphAnchor(
                        GraphAnchorRole.REFERENCE, "workflow.m3_07.reference.n2", "request", 2
                    ),
                ),
            ),
            manifest,
            required_anchor_roles=(GraphAnchorRole.REFERENCE,),
        )
    except GraphBindingError:
        ambiguous_rejected = True
    return {
        "base_round_trip": graph_to_manifest(base, manifest),
        "reference_round_trip": graph_to_manifest(reference, manifest),
        "nested_round_trip": graph_to_manifest(root, manifest),
        "nested_node_count": len(root.flatten_nodes()),
        "missing_anchor_rejected": missing_rejected,
        "ambiguous_anchor_rejected": ambiguous_rejected,
    }


def run() -> dict[str, object]:
    """Run the bounded offline integration lanes and return only redacted facts."""

    with tempfile.TemporaryDirectory(prefix="h3-m10-08-") as temporary:
        decisions = _media_admission(Path(temporary))
        raw, registry, observation_request = _pipeline_request()

        model_free = _FixtureObserver()
        model_free_batch = execute_image_observation(
            model_free,
            observation_request,
            device=LocalDeviceSpec(LocalDeviceKind.CPU),
            deterministic_required=True,
            seed=7,
        )
        native = _NativeObservationAdapter()
        native_batch = execute_image_observation(
            native,
            observation_request,
            device=LocalDeviceSpec(LocalDeviceKind.CPU),
            deterministic_required=True,
            seed=7,
        )
        coordinator = ExecutionCoordinator(CoordinatorCapacity(2, 0, 4_000_000, 0, 2))
        model_free_coordinated = coordinator.execute(
            model_free,
            _coordinator_request("fixture.observer", observation_request, model_digest=_fp("f")),
        )
        native_coordinated = coordinator.execute(
            native,
            _coordinator_request(
                "comfyui_native.fixture_vlm", observation_request, model_digest=_fp("c")
            ),
        )
        cancelled_scope = CancellationScope()
        cancelled_scope.cancel("fixture stop")
        cancelled = coordinator.execute(
            model_free,
            _coordinator_request("fixture.observer", observation_request, model_digest=_fp("f")),
            cancellation=cancelled_scope,
        )
        failed = coordinator.execute(
            _FixtureObserver(fail=True),
            _coordinator_request("fixture.observer", observation_request, model_digest=_fp("f")),
        )
        model_free_pipeline = _run_pipeline(raw, registry, model_free_batch)
        native_pipeline = _run_pipeline(raw, registry, native_batch)
        return {
            "schema": "h3.m10_08.runtime.fixture.v1",
            "media_admission": {
                "statuses": [getattr(item.status, "value", str(item.status)) for item in decisions],
                "fingerprints_present": all(
                    item.source_fingerprint is not None for item in decisions
                ),
            },
            "model_free": {
                "observation_status": model_free_batch.status.value,
                "coordinator_status": model_free_coordinated.receipt.status.value,
                "coordinator_value_present": model_free_coordinated.value is not None,
                "pipeline": model_free_pipeline,
            },
            "comfyui_native": {
                "clip_calls": list(native.clip.calls),
                "observation_status": native_batch.status.value,
                "coordinator_status": native_coordinated.receipt.status.value,
                "coordinator_value_present": native_coordinated.value is not None,
                "pipeline": native_pipeline,
            },
            "terminal_variants": {
                "cancelled": cancelled.receipt.status.value,
                "cancelled_value_absent": cancelled.value is None,
                "failed": failed.receipt.status.value,
                "failed_value_absent": failed.value is None,
                "reservations_released": cancelled.receipt.reservation_released
                and failed.receipt.reservation_released,
            },
            "graph": _run_graph_fixture(),
            "privacy": {
                "portable_outputs_are_redacted": model_free_coordinated.value is not None
                and isinstance(model_free_coordinated.value, ObservationPayload),
                "no_live_host_or_network": True,
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the JSON fixture summary")
    parser.parse_args()
    summary = run()
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
