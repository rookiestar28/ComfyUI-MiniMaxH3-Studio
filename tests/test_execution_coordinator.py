"""M10-04 coordinator, ownership, cache-scope, progress, and cleanup tests."""

from __future__ import annotations

import json
import threading
import unittest
from dataclasses import dataclass

from comfyui_h3_context.core import (
    ArtifactCachePolicy,
    ArtifactCacheScope,
    BackendRuntimeQualification,
    CancellationScope,
    CoordinatorCacheIdentity,
    CoordinatorCapability,
    CoordinatorCapacity,
    CoordinatorExecutionRequest,
    CoordinatorProfile,
    CoordinatorResourceBudget,
    CoordinatorStatus,
    ExecutionCoordinator,
    ExternalResourceObservation,
    LocalAdapterDescriptor,
    LocalAdapterExecutionRequest,
    LocalAdapterKind,
    LocalAdapterResult,
    LocalDeviceKind,
    LocalDeviceSpec,
    LocalResourceBudget,
    ModelBackendFamily,
    ModelCapabilityState,
    OwnershipKind,
    ResourceCacheState,
    ResourceOwnershipPolicy,
    TaskMode,
)
from comfyui_h3_context.core.errors import (
    ExecutionArtifactError,
    ExecutionCoordinatorError,
)
from comfyui_h3_context.core.local_adapters import LocalAdapter, LocalBudgetGuard


def fp(letter: str) -> str:
    return "sha256:" + letter * 64


@dataclass(frozen=True)
class FakePayload:
    value: str = "typed"

    def to_public_dict(self) -> dict[str, str]:
        return {"value": self.value}


class FakeAdapter(LocalAdapter):
    def __init__(self, *, adapter_id: str = "fake.native") -> None:
        self.calls = 0
        self.fail: str | None = None
        self.cancel_during_run = False
        self.started_event: threading.Event | None = None
        self.release_event: threading.Event | None = None
        self._descriptor = LocalAdapterDescriptor(
            adapter_id=adapter_id,
            kind=LocalAdapterKind.REASONING,
            adapter_version="1.0.0",
            minimum_version="1.0.0",
            maximum_version="1.0.0",
            supported_task_modes=frozenset(TaskMode),
            supported_media=frozenset(),
            supported_devices=frozenset(
                {LocalDeviceKind.AUTO, LocalDeviceKind.CPU, LocalDeviceKind.CUDA}
            ),
            optional_dependencies=(),
            output_schema="h3.fake.output.v1",
            limits=LocalResourceBudget(1_000_000, 10.0, 8, 4096, 4, 2),
            supports_determinism=True,
            supports_seed=True,
            supports_cancellation=True,
        )

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._descriptor

    def run(
        self, request: LocalAdapterExecutionRequest, guard: LocalBudgetGuard
    ) -> LocalAdapterResult:
        self.calls += 1
        if self.started_event is not None:
            self.started_event.set()
        if self.release_event is not None:
            self.release_event.wait(2.0)
        guard.checkpoint()
        if self.fail == "memory":
            raise MemoryError("fixture memory")
        if self.fail == "timeout":
            raise TimeoutError("fixture timeout")
        if self.fail == "error":
            raise RuntimeError("fixture error")
        if self.cancel_during_run:
            cancel = getattr(request.input_value, "cancel", None)
            if callable(cancel):
                cancel("fixture stop")
            guard.checkpoint()
        return LocalAdapterResult(
            self.descriptor.adapter_id,
            self.descriptor.adapter_version,
            request.device,
            FakePayload(),
            output_bytes=32,
            output_items=1,
        )


class FakeLifecycle:
    def __init__(self, backend: ModelBackendFamily, *, fail_close: bool = False) -> None:
        self.backend = backend
        self.observations = 0
        self.closed = 0
        self.fail_close = fail_close

    def observe(self) -> ExternalResourceObservation:
        self.observations += 1
        return ExternalResourceObservation(self.backend, True, 4_096, 8_192, 0.0)

    def close_owned(self) -> None:
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("fixture cleanup")


def identity(adapter_id: str = "fake.native", *, source: str = "a") -> CoordinatorCacheIdentity:
    return CoordinatorCacheIdentity(
        operation_id="h3.execution.fixture",
        contract_schema="h3.model.generation.request.v1",
        renderer_profile="h3.base.v1",
        prompt_profile="h3.base.prompt.v1",
        provider_id="local",
        adapter_id=adapter_id,
        adapter_version="1.0.0",
        server_version="none",
        model_digest=fp("c"),
        tokenizer_revision="tokenizer.v1",
        processor_revision="processor.v1",
        preprocessing_revision="media.v1",
        settings_fingerprint=fp("d"),
        source_fingerprints=(fp(source),),
    )


def budget(
    *, cpu: int = 1, gpu: int = 0, ram: int = 100_000, vram: int = 0
) -> CoordinatorResourceBudget:
    return CoordinatorResourceBudget(
        cpu_threads=cpu,
        gpu_slots=gpu,
        ram_bytes=ram,
        vram_bytes=vram,
        max_wall_time_seconds=5.0,
        max_concurrency=1,
        max_output_bytes=4096,
        max_output_items=4,
        max_references=4,
    )


def request(
    adapter: FakeAdapter,
    *,
    profile: CoordinatorProfile = CoordinatorProfile.PROFILE_1,
    cache: ArtifactCachePolicy | None = None,
    qualification: BackendRuntimeQualification | None = None,
    ownership: ResourceOwnershipPolicy | None = None,
    progress_required: bool = False,
    cancellation_required: bool = False,
    value: object = "runtime-only",
) -> CoordinatorExecutionRequest:
    local = LocalAdapterExecutionRequest(
        adapter_id=adapter.descriptor.adapter_id,
        task_mode=TaskMode.T2VA,
        device=LocalDeviceSpec(LocalDeviceKind.CPU),
        deterministic_required=True,
        cancellation_required=cancellation_required,
        input_value=value,
    )
    selected_qualification = qualification or BackendRuntimeQualification(
        ModelBackendFamily.COMFYUI_NATIVE,
        ModelCapabilityState.QUALIFIED,
        ModelCapabilityState.QUALIFIED,
    )
    return CoordinatorExecutionRequest(
        profile=profile,
        cache_identity=identity(adapter.descriptor.adapter_id),
        budget=budget(),
        adapter_request=local,
        qualification=selected_qualification,
        ownership=ownership
        or ResourceOwnershipPolicy.for_backend(selected_qualification.backend_family),
        required_capabilities=frozenset({CoordinatorCapability.LOCAL_TEXT_GENERATION}),
        artifact_policy=cache or ArtifactCachePolicy(),
        progress_required=progress_required,
        cancellation_required=cancellation_required,
    )


class ExecutionCoordinatorTests(unittest.TestCase):
    def test_profiles_are_explicit_and_profile_zero_blocks_model_execution(self) -> None:
        adapter = FakeAdapter()
        coordinator = ExecutionCoordinator(CoordinatorCapacity(2, 1, 1_000_000, 1_000_000, 2))
        result = coordinator.execute(
            adapter, request(adapter, profile=CoordinatorProfile.PROFILE_0)
        )
        self.assertEqual(result.receipt.status, CoordinatorStatus.UNSUPPORTED)
        self.assertEqual(adapter.calls, 0)

    def test_native_execution_uses_scheduler_cache_and_hash_only_artifact(self) -> None:
        adapter = FakeAdapter()
        policy = ArtifactCachePolicy(
            enabled=True,
            scope=ArtifactCacheScope.SESSION,
            ttl_seconds=60.0,
            consent_granted=True,
        )
        coordinator = ExecutionCoordinator(CoordinatorCapacity(2, 1, 1_000_000, 1_000_000, 2))
        first = coordinator.execute(adapter, request(adapter, cache=policy))
        second = coordinator.execute(adapter, request(adapter, cache=policy))
        self.assertEqual(first.receipt.status, CoordinatorStatus.COMPLETE)
        self.assertEqual(first.receipt.cache_state, ResourceCacheState.MISS)
        self.assertEqual(second.receipt.cache_state, ResourceCacheState.HIT)
        self.assertEqual(adapter.calls, 1)
        self.assertIsNotNone(first.receipt.portable_artifact)
        wire = json.dumps(first.to_wire())
        self.assertNotIn("runtime-only", wire)
        self.assertTrue(first.receipt.reservation_released)
        self.assertEqual(coordinator.active, 0)
        self.assertTrue(coordinator.delete_cached(identity(adapter.descriptor.adapter_id), policy))

    def test_cache_identity_contains_all_revision_inputs_and_changes_on_each(self) -> None:
        base = identity()
        self.assertNotEqual(base.fingerprint, identity(source="b").fingerprint)
        self.assertNotEqual(
            base.fingerprint,
            __import__("dataclasses").replace(base, tokenizer_revision="tokenizer.v2").fingerprint,
        )
        self.assertNotIn("/private", json.dumps(base.to_wire()))
        with self.assertRaises(ExecutionCoordinatorError):
            __import__("dataclasses").replace(base, provider_id="private.path")

    def test_content_cache_requires_consent_scope_and_tenant_fingerprint(self) -> None:
        with self.assertRaises(ExecutionArtifactError):
            ArtifactCachePolicy(enabled=True, scope=ArtifactCacheScope.SESSION, ttl_seconds=1.0)
        with self.assertRaises(ExecutionArtifactError):
            ArtifactCachePolicy(
                enabled=True,
                scope=ArtifactCacheScope.TENANT,
                ttl_seconds=1.0,
                consent_granted=True,
            )

    def test_external_ollama_resources_are_observed_but_never_closed(self) -> None:
        adapter = FakeAdapter(adapter_id="ollama")
        ownership = ResourceOwnershipPolicy.for_backend(ModelBackendFamily.OLLAMA)
        qualification = BackendRuntimeQualification(
            ModelBackendFamily.OLLAMA,
            ModelCapabilityState.UNQUALIFIED,
            ModelCapabilityState.UNQUALIFIED,
        )
        lifecycle = FakeLifecycle(ModelBackendFamily.OLLAMA)
        coordinator = ExecutionCoordinator(CoordinatorCapacity(2, 1, 1_000_000, 1_000_000, 2))
        result = coordinator.execute(
            adapter,
            request(
                adapter,
                qualification=qualification,
                ownership=ownership,
            ),
            lifecycle=lifecycle,
        )
        self.assertEqual(result.receipt.status, CoordinatorStatus.COMPLETE)
        self.assertTrue(result.receipt.external_observed)
        self.assertEqual(lifecycle.observations, 2)
        self.assertEqual(lifecycle.closed, 0)

    def test_unqualified_ollama_progress_is_rejected_when_required(self) -> None:
        adapter = FakeAdapter(adapter_id="ollama")
        qualification = BackendRuntimeQualification(
            ModelBackendFamily.OLLAMA,
            ModelCapabilityState.UNQUALIFIED,
            ModelCapabilityState.UNQUALIFIED,
        )
        result = ExecutionCoordinator(CoordinatorCapacity(2, 1, 1_000_000, 1_000_000, 2)).execute(
            adapter,
            request(adapter, qualification=qualification, progress_required=True),
            lifecycle=FakeLifecycle(ModelBackendFamily.OLLAMA),
        )
        self.assertEqual(result.receipt.status, CoordinatorStatus.UNSUPPORTED)
        self.assertEqual(adapter.calls, 0)

    def test_adapter_owned_cleanup_runs_on_memory_failure_and_never_returns_dummy(self) -> None:
        adapter = FakeAdapter()
        adapter.fail = "memory"
        ownership = ResourceOwnershipPolicy(
            ModelBackendFamily.COMFYUI_NATIVE,
            OwnershipKind.ADAPTER,
            OwnershipKind.ADAPTER,
            OwnershipKind.ADAPTER,
            OwnershipKind.ADAPTER,
            OwnershipKind.ADAPTER,
            OwnershipKind.ADAPTER,
        )
        lifecycle = FakeLifecycle(ModelBackendFamily.COMFYUI_NATIVE)
        result = ExecutionCoordinator(CoordinatorCapacity(2, 1, 1_000_000, 1_000_000, 2)).execute(
            adapter, request(adapter, ownership=ownership), lifecycle=lifecycle
        )
        self.assertEqual(result.receipt.status, CoordinatorStatus.OUT_OF_MEMORY)
        self.assertIsNone(result.value)
        self.assertEqual(lifecycle.closed, 1)
        self.assertTrue(result.receipt.cleanup_completed)
        self.assertEqual(result.progress[-1].to_wire()["state"], "failed")

    def test_timeout_and_cancellation_are_terminal_without_dummy_values(self) -> None:
        timed_out_adapter = FakeAdapter()
        timed_out_adapter.fail = "timeout"
        coordinator = ExecutionCoordinator(CoordinatorCapacity(2, 1, 1_000_000, 1_000_000, 2))
        timed_out = coordinator.execute(timed_out_adapter, request(timed_out_adapter))
        self.assertEqual(timed_out.receipt.status, CoordinatorStatus.TIMED_OUT)
        self.assertIsNone(timed_out.value)

        scope = CancellationScope()
        cancelled_adapter = FakeAdapter()
        cancelled_adapter.cancel_during_run = True
        cancelled = coordinator.execute(
            cancelled_adapter,
            request(
                cancelled_adapter,
                cancellation_required=True,
                value=scope,
            ),
            cancellation=scope,
        )
        self.assertEqual(cancelled.receipt.status, CoordinatorStatus.CANCELLED)
        self.assertIsNone(cancelled.value)

    def test_capacity_contention_is_bounded_and_released(self) -> None:
        adapter = FakeAdapter()
        adapter.started_event = threading.Event()
        adapter.release_event = threading.Event()
        coordinator = ExecutionCoordinator(CoordinatorCapacity(1, 0, 100_000, 0, 1))
        request_value = request(adapter)
        results: list[object] = []

        def run_first() -> None:
            results.append(coordinator.execute(adapter, request_value))

        thread = threading.Thread(target=run_first)
        thread.start()
        assert adapter.started_event.wait(1.0)
        second = coordinator.execute(adapter, request_value)
        self.assertEqual(second.receipt.status, CoordinatorStatus.RESOURCE_LIMIT)
        adapter.release_event.set()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(coordinator.active, 0)
        self.assertEqual(len(results), 1)

    def test_capacity_admission_releases_after_failure_and_blocks_oversized_request(self) -> None:
        adapter = FakeAdapter()
        adapter.fail = "error"
        coordinator = ExecutionCoordinator(CoordinatorCapacity(1, 0, 100_000, 0, 1))
        failed = coordinator.execute(adapter, request(adapter))
        self.assertEqual(failed.receipt.status, CoordinatorStatus.FAILED)
        self.assertEqual(coordinator.active, 0)
        oversized = CoordinatorResourceBudget(2, 0, 100_000, 0, 5.0, 1, 4096, 4, 4)
        rejected = coordinator.execute(
            adapter,
            CoordinatorExecutionRequest(
                profile=CoordinatorProfile.PROFILE_1,
                cache_identity=identity(adapter.descriptor.adapter_id),
                budget=oversized,
                adapter_request=LocalAdapterExecutionRequest(
                    adapter_id=adapter.descriptor.adapter_id,
                    task_mode=TaskMode.T2VA,
                    device=LocalDeviceSpec(LocalDeviceKind.CPU),
                    input_value="runtime",
                ),
                qualification=BackendRuntimeQualification(
                    ModelBackendFamily.COMFYUI_NATIVE,
                    ModelCapabilityState.QUALIFIED,
                    ModelCapabilityState.QUALIFIED,
                ),
                ownership=ResourceOwnershipPolicy.for_backend(ModelBackendFamily.COMFYUI_NATIVE),
            ),
        )
        self.assertEqual(rejected.receipt.status, CoordinatorStatus.RESOURCE_LIMIT)

    def test_schema_and_public_projection_are_json_safe(self) -> None:
        adapter = FakeAdapter()
        request_value = request(adapter)
        encoded = json.dumps(request_value.to_wire())
        self.assertIn("h3.execution.coordinator.v1", encoded)
        self.assertNotIn("runtime-only", encoded)


if __name__ == "__main__":
    unittest.main()
