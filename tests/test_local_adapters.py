"""M5-01 optional local adapter boundary, lazy loading, and budget tests."""

from __future__ import annotations

import json
import sys
import threading
import types
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    LOCAL_ADAPTER_CONTRACT_SCHEMA,
    AssetRole,
    LocalAdapterCapabilityError,
    LocalAdapterCatalog,
    LocalAdapterDescriptor,
    LocalAdapterExecutionRequest,
    LocalAdapterKind,
    LocalAdapterResult,
    LocalAdapterRuntime,
    LocalAdapterUnavailableError,
    LocalAdapterVersionError,
    LocalDeviceKind,
    LocalDeviceSpec,
    LocalResourceBudget,
    MediaKind,
    RawContextRequest,
    ReferenceAsset,
    TaskMode,
    build_reference_registry,
    normalize_request,
    run_local_adapter,
)
from comfyui_h3_context.core.errors import (
    ContractValidationError,
    LocalAdapterBudgetError,
    LocalAdapterCancelledError,
    LocalAdapterConcurrencyError,
    LocalAdapterError,
    LocalAdapterMemoryError,
    LocalAdapterTimeoutError,
)

ROOT = Path(__file__).resolve().parents[1]


def budget(
    *,
    memory: int = 1_000,
    wall: float = 10.0,
    references: int = 2,
    output_bytes: int = 2_000,
    concurrency: int = 1,
) -> LocalResourceBudget:
    return LocalResourceBudget(
        max_memory_bytes=memory,
        max_wall_time_seconds=wall,
        max_references=references,
        max_output_bytes=output_bytes,
        max_output_items=4,
        max_concurrency=concurrency,
    )


def descriptor(
    *,
    adapter_id: str = "local.test",
    version: str = "1.0.0",
    limits: LocalResourceBudget | None = None,
    devices: frozenset[LocalDeviceKind] | None = None,
) -> LocalAdapterDescriptor:
    return LocalAdapterDescriptor(
        adapter_id=adapter_id,
        kind=LocalAdapterKind.PERCEPTION,
        adapter_version=version,
        minimum_version="1.0.0",
        maximum_version="2.0.0",
        supported_task_modes=frozenset({TaskMode.T2VA, TaskMode.I2VA}),
        supported_media=frozenset({MediaKind.IMAGE}),
        supported_devices=(
            frozenset({LocalDeviceKind.CPU, LocalDeviceKind.AUTO}) if devices is None else devices
        ),
        optional_dependencies=("test.backend",),
        output_schema="h3.local.observation.v1",
        limits=budget() if limits is None else limits,
        supports_determinism=True,
        supports_seed=True,
        supports_cancellation=True,
    )


def request(
    *,
    descriptor_id: str = "local.test",
    mode: TaskMode = TaskMode.T2VA,
    references: int = 0,
    device: LocalDeviceSpec | None = None,
    memory: int | None = None,
    wall: float | None = None,
    output_bytes: int | None = None,
) -> LocalAdapterExecutionRequest:
    return LocalAdapterExecutionRequest(
        adapter_id=descriptor_id,
        task_mode=mode,
        media_kinds=(MediaKind.IMAGE,) if references else (),
        reference_count=references,
        device=LocalDeviceSpec(LocalDeviceKind.CPU) if device is None else device,
        estimated_memory_bytes=memory,
        estimated_wall_time_seconds=wall,
        estimated_output_bytes=output_bytes,
        deterministic_required=True,
        seed=7,
    )


class Adapter:
    def __init__(self, descriptor_value: LocalAdapterDescriptor | None = None) -> None:
        self.descriptor_value = descriptor() if descriptor_value is None else descriptor_value
        self.run_count = 0

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self.descriptor_value

    def run(self, request_value: LocalAdapterExecutionRequest, guard: object) -> LocalAdapterResult:
        del request_value, guard
        self.run_count += 1
        return LocalAdapterResult(
            adapter_id=self.descriptor.adapter_id,
            adapter_version=self.descriptor.adapter_version,
            device=LocalDeviceSpec(LocalDeviceKind.CPU),
            value={"observations": []},
        )


class LocalAdapterBoundaryTests(unittest.TestCase):
    def test_public_schema_matches_descriptor_projection(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "local_adapter_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/local_adapter_v1.schema.json"
        )
        self.assertEqual(LOCAL_ADAPTER_CONTRACT_SCHEMA, "h3.local.adapter.v1")
        self.assertEqual(set(schema["required"]), set(descriptor().to_public_dict()))

    def test_descriptor_and_request_are_bounded_and_public(self) -> None:
        item = descriptor()
        wire = item.to_public_dict()
        self.assertEqual(wire["adapter_id"], "local.test")
        limits = cast(dict[str, object], wire["limits"])
        self.assertEqual(limits["max_memory_bytes"], 1_000)
        self.assertNotIn("credential", str(wire).casefold())
        self.assertEqual(request().to_public_dict()["device"], "cpu")
        with self.assertRaises(ContractValidationError):
            replace(item, adapter_version="secret-token")

    def test_capability_and_budget_preflight_fail_before_adapter_run(self) -> None:
        adapter = Adapter()
        with self.assertRaises(LocalAdapterCapabilityError):
            run_local_adapter(
                adapter,
                request(device=LocalDeviceSpec(LocalDeviceKind.CUDA)),
            )
        with self.assertRaises(LocalAdapterBudgetError):
            run_local_adapter(adapter, request(memory=1_001))
        with self.assertRaises(LocalAdapterBudgetError):
            run_local_adapter(adapter, request(references=3))
        with self.assertRaises(LocalAdapterBudgetError):
            run_local_adapter(adapter, request(output_bytes=2_001))
        self.assertEqual(adapter.run_count, 0)

    def test_lazy_catalog_never_imports_until_explicit_resolution(self) -> None:
        module_name = "h3_m501.missing_optional_backend"
        sys.modules.pop(module_name, None)
        catalog = LocalAdapterCatalog.from_lazy(
            descriptor(), module_name=module_name, attribute="build_adapter"
        )
        self.assertNotIn(module_name, sys.modules)
        with self.assertRaises(LocalAdapterUnavailableError):
            catalog.resolve("local.test")
        self.assertNotIn(module_name, sys.modules)

    def test_lazy_catalog_rejects_adapter_version_drift_without_fallback(self) -> None:
        module_name = "h3_m501.versioned_backend"
        module = types.ModuleType(module_name)
        drifted = Adapter(
            replace(
                descriptor(),
                adapter_version="3.0.0",
                minimum_version="3.0.0",
                maximum_version="4.0.0",
            )
        )
        module.build_adapter = lambda: drifted  # type: ignore[attr-defined]
        sys.modules[module_name] = module
        try:
            catalog = LocalAdapterCatalog.from_lazy(
                descriptor(), module_name=module_name, attribute="build_adapter"
            )
            with self.assertRaises(LocalAdapterVersionError):
                catalog.resolve("local.test")
        finally:
            sys.modules.pop(module_name, None)

    def test_cancellation_timeout_memory_and_incomplete_result_are_distinct(self) -> None:
        class Probe:
            def is_cancelled(self) -> bool:
                return True

        with self.assertRaises(LocalAdapterCancelledError):
            run_local_adapter(Adapter(), request(), cancellation_probe=Probe())

        now = [0.0]

        def clock() -> float:
            return now[0]

        class SlowAdapter(Adapter):
            def run(
                self, request_value: LocalAdapterExecutionRequest, guard: object
            ) -> LocalAdapterResult:
                del request_value, guard
                now[0] = 11.0
                return LocalAdapterResult(
                    adapter_id=self.descriptor.adapter_id,
                    adapter_version=self.descriptor.adapter_version,
                    device=LocalDeviceSpec(LocalDeviceKind.CPU),
                    value={},
                )

        with self.assertRaises(LocalAdapterTimeoutError):
            run_local_adapter(SlowAdapter(), request(), clock=clock)

        with self.assertRaises(LocalAdapterMemoryError):
            run_local_adapter(Adapter(), request(), memory_meter=lambda: 1_001)

        class Incomplete(Adapter):
            def run(
                self, request_value: LocalAdapterExecutionRequest, guard: object
            ) -> LocalAdapterResult:
                del request_value, guard
                return LocalAdapterResult(
                    adapter_id=self.descriptor.adapter_id,
                    adapter_version=self.descriptor.adapter_version,
                    device=LocalDeviceSpec(LocalDeviceKind.CPU),
                    value={},
                    complete=False,
                )

        with self.assertRaises(LocalAdapterError):
            run_local_adapter(Incomplete(), request())

    def test_runtime_concurrency_releases_after_failure(self) -> None:
        runtime = LocalAdapterRuntime()
        entered = threading.Event()
        release = threading.Event()

        class Blocking(Adapter):
            def run(
                self, request_value: LocalAdapterExecutionRequest, guard: object
            ) -> LocalAdapterResult:
                del request_value, guard
                entered.set()
                release.wait(timeout=2)
                return super().run(request(), object())

        adapter = Blocking()
        failures: list[BaseException] = []

        def first() -> None:
            try:
                runtime.execute(adapter, request())
            except BaseException as exc:  # pragma: no cover - diagnostic capture
                failures.append(exc)

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(entered.wait(timeout=2))
        with self.assertRaises(LocalAdapterConcurrencyError):
            runtime.execute(adapter, request())
        release.set()
        thread.join(timeout=2)
        self.assertEqual(failures, [])
        runtime.execute(adapter, request())

    def test_core_manual_workflow_runs_with_empty_catalog(self) -> None:
        self.assertEqual(LocalAdapterCatalog().descriptors, ())
        registry = build_reference_registry(
            (ReferenceAsset("image_1", MediaKind.IMAGE, AssetRole.REFERENCE, 1),)
        )
        result = normalize_request(
            RawContextRequest(
                mode=TaskMode.REF2VA,
                user_intent="manual workflow remains available",
                duration_seconds=5,
                assets=registry.to_asset_descriptors(),
                reference_registry=registry,
            )
        )
        self.assertIsNotNone(result.request)


if __name__ == "__main__":
    unittest.main()
