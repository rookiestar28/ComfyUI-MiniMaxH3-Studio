"""M6-06 bounded resource scheduling, cache, and cancellation contract tests."""

from __future__ import annotations

import ast
import json
import threading
import unittest
from pathlib import Path

from comfyui_h3_context.core import (
    LOCAL_ADAPTER_CONTRACT_SCHEMA,
    RESOURCE_RUNTIME_SCHEMA,
    LocalResourceBudget,
    ProgressLedger,
    ReliabilityRunState,
    ReliabilityStage,
    ResourceCacheKey,
    ResourceCacheState,
    ResourceCancellationToken,
    ResourceExecutionArtifact,
    ResourceExecutionContext,
    ResourceExecutionRequest,
    ResourceExecutionStatus,
    ResourceScheduler,
)
from comfyui_h3_context.core.errors import ResourceSchedulingError

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "sha256:" + "1" * 64
SETTINGS = "sha256:" + "2" * 64


def budget(
    *,
    memory: int = 1024,
    wall: float = 10.0,
    output_bytes: int = 1024,
    output_items: int = 8,
    concurrency: int = 1,
) -> LocalResourceBudget:
    return LocalResourceBudget(
        max_memory_bytes=memory,
        max_wall_time_seconds=wall,
        max_references=8,
        max_output_bytes=output_bytes,
        max_output_items=output_items,
        max_concurrency=concurrency,
    )


def cache_key(
    *,
    provider_id: str = "local.manual",
    provider_version: str = "1.0.0",
    settings: str = SETTINGS,
    sources: tuple[str, ...] = (SOURCE,),
) -> ResourceCacheKey:
    return ResourceCacheKey(
        operation_id="full_reference.timeline",
        contract_schema="h3.full_reference.timeline.v1",
        provider_id=provider_id,
        provider_version=provider_version,
        settings_fingerprint=settings,
        source_fingerprints=sources,
    )


def request(
    *,
    key: ResourceCacheKey | None = None,
    ttl: float = 60.0,
    limits: LocalResourceBudget | None = None,
    cancellation_required: bool = False,
) -> ResourceExecutionRequest:
    return ResourceExecutionRequest(
        cache_key=cache_key() if key is None else key,
        budget=budget() if limits is None else limits,
        cache_ttl_seconds=ttl,
        cancellation_required=cancellation_required,
        input_value={"opaque": "runtime-only"},
    )


class ResourceSchedulingTests(unittest.TestCase):
    def test_cache_key_is_composed_from_version_provider_settings_and_sources(self) -> None:
        first = cache_key()
        self.assertTrue(first.cache_key.startswith("sha256:"))
        self.assertEqual(first.to_wire()["schema"], RESOURCE_RUNTIME_SCHEMA)
        self.assertNotEqual(first.cache_key, cache_key(provider_version="1.0.1").cache_key)
        self.assertNotEqual(first.cache_key, cache_key(settings="sha256:" + "3" * 64).cache_key)
        self.assertNotEqual(first.cache_key, cache_key(sources=("sha256:" + "4" * 64,)).cache_key)
        with self.assertRaises(ResourceSchedulingError):
            cache_key(provider_id="local.password")
        with self.assertRaises(ResourceSchedulingError):
            cache_key(sources=("/private/media.mp4",))

    def test_complete_artifact_is_cached_and_hit_does_not_run_worker(self) -> None:
        scheduler = ResourceScheduler(max_concurrency=2)
        calls = 0

        def worker(context: ResourceExecutionContext) -> ResourceExecutionArtifact:
            nonlocal calls
            calls += 1
            context.record_output(byte_count=12)
            return ResourceExecutionArtifact(value={"ok": True}, output_bytes=12)

        first = scheduler.execute(request(), worker)
        second = scheduler.execute(request(), worker)
        self.assertEqual(calls, 1)
        self.assertEqual(first.status, ResourceExecutionStatus.COMPLETE)
        self.assertEqual(second.status, ResourceExecutionStatus.COMPLETE)
        self.assertEqual(first.receipt.cache_state, ResourceCacheState.MISS)
        self.assertEqual(second.receipt.cache_state, ResourceCacheState.HIT)
        self.assertFalse(second.receipt.worker_started)
        self.assertTrue(second.receipt.reservation_released)

    def test_progress_sink_is_forwarded_through_scheduler_context(self) -> None:
        scheduler = ResourceScheduler()
        ledger = ProgressLedger("full_reference.timeline")

        def worker(context: ResourceExecutionContext) -> ResourceExecutionArtifact:
            context.report_progress(ReliabilityStage.EXTRACTION, 0, 2)
            context.report_progress(
                ReliabilityStage.EXTRACTION, 2, 2, ReliabilityRunState.COMPLETED
            )
            return ResourceExecutionArtifact(value="ok", output_bytes=1)

        result = scheduler.execute(request(), worker, progress=ledger)
        self.assertEqual(result.status, ResourceExecutionStatus.COMPLETE)
        self.assertEqual([event.completed_units for event in ledger.events], [0, 2])
        self.assertTrue(result.receipt.reservation_released)

    def test_expired_cache_is_stale_and_fresh_complete_result_replaces_it(self) -> None:
        now = 0.0
        scheduler = ResourceScheduler(max_concurrency=1, clock=lambda: now)
        calls = 0

        def worker(context: ResourceExecutionContext) -> ResourceExecutionArtifact:
            nonlocal calls
            calls += 1
            context.checkpoint()
            return ResourceExecutionArtifact(value=calls, output_bytes=1)

        first = scheduler.execute(request(ttl=1.0), worker)
        now = 2.0
        second = scheduler.execute(request(ttl=1.0), worker)
        self.assertEqual(calls, 2)
        self.assertEqual(first.status, ResourceExecutionStatus.COMPLETE)
        self.assertEqual(second.status, ResourceExecutionStatus.COMPLETE)
        self.assertEqual(second.receipt.cache_state, ResourceCacheState.STALE)
        self.assertTrue(second.receipt.cache_stored)

    def test_partial_and_stale_worker_artifacts_are_not_cacheable_or_complete(self) -> None:
        scheduler = ResourceScheduler()
        calls = 0

        def partial(context: ResourceExecutionContext) -> ResourceExecutionArtifact:
            nonlocal calls
            calls += 1
            return ResourceExecutionArtifact(
                value={"partial": True},
                status=ResourceExecutionStatus.PARTIAL,
                reason="missing source span",
            )

        first = scheduler.execute(request(), partial)
        second = scheduler.execute(request(), partial)
        self.assertEqual(calls, 2)
        self.assertEqual(first.status, ResourceExecutionStatus.PARTIAL)
        self.assertEqual(second.status, ResourceExecutionStatus.PARTIAL)
        self.assertFalse(first.complete)
        self.assertFalse(first.receipt.cache_stored)

        stale = scheduler.execute(
            request(key=cache_key(settings="sha256:" + "9" * 64)),
            lambda _: ResourceExecutionArtifact(
                value={"stale": True},
                status=ResourceExecutionStatus.STALE,
                reason="source revision changed",
            ),
        )
        self.assertEqual(stale.status, ResourceExecutionStatus.STALE)
        self.assertFalse(stale.complete)

    def test_cancellation_before_and_during_execution_releases_resources(self) -> None:
        scheduler = ResourceScheduler()
        before = ResourceCancellationToken()
        before.cancel("caller stopped")
        result = scheduler.execute(
            request(cancellation_required=True),
            lambda _: ResourceExecutionArtifact(value="must-not-run"),
            cancellation_probe=before,
        )
        self.assertEqual(result.status, ResourceExecutionStatus.CANCELLED)
        self.assertIsNone(result.artifact)
        self.assertEqual(scheduler.active, 0)

        during = ResourceCancellationToken()

        def cancel_worker(context: ResourceExecutionContext) -> ResourceExecutionArtifact:
            context.checkpoint()
            during.cancel("worker checkpoint")
            context.checkpoint()
            return ResourceExecutionArtifact(value="must-not-complete")

        cancelled = scheduler.execute(
            request(cancellation_required=True), cancel_worker, cancellation_probe=during
        )
        self.assertEqual(cancelled.status, ResourceExecutionStatus.CANCELLED)
        self.assertIsNone(cancelled.artifact)
        self.assertTrue(cancelled.receipt.reservation_released)
        self.assertEqual(scheduler.active, 0)

    def test_timeout_memory_and_output_limits_are_distinguishable(self) -> None:
        timeout_now = 0.0

        def timeout_worker(context: ResourceExecutionContext) -> ResourceExecutionArtifact:
            nonlocal timeout_now
            timeout_now = 2.0
            context.checkpoint()
            return ResourceExecutionArtifact(value="nope")

        timeout = ResourceScheduler(clock=lambda: timeout_now).execute(
            request(limits=budget(wall=1.0)), timeout_worker
        )
        self.assertEqual(timeout.status, ResourceExecutionStatus.TIMED_OUT)

        memory_values = iter((0, 0, 512))
        memory = ResourceScheduler(memory_meter=lambda: next(memory_values)).execute(
            request(limits=budget(memory=128)),
            lambda context: (
                context.checkpoint(),
                ResourceExecutionArtifact(value="nope"),
            )[1],
        )
        self.assertEqual(memory.status, ResourceExecutionStatus.MEMORY_EXCEEDED)

        output = ResourceScheduler().execute(
            request(limits=budget(output_bytes=4)),
            lambda context: (
                context.record_output(byte_count=5),
                ResourceExecutionArtifact(value="nope"),
            )[1],
        )
        self.assertEqual(output.status, ResourceExecutionStatus.BUDGET_EXCEEDED)

    def test_concurrency_limit_and_cleanup_are_observable(self) -> None:
        scheduler = ResourceScheduler(max_concurrency=1)
        started = threading.Event()
        release = threading.Event()
        first_result: list[object] = []

        def blocking(context: ResourceExecutionContext) -> ResourceExecutionArtifact:
            started.set()
            release.wait(timeout=2.0)
            context.checkpoint()
            return ResourceExecutionArtifact(value="first")

        thread = threading.Thread(
            target=lambda: first_result.append(scheduler.execute(request(), blocking)), daemon=True
        )
        thread.start()
        self.assertTrue(started.wait(timeout=1.0))
        second = scheduler.execute(request(), lambda _: ResourceExecutionArtifact(value="second"))
        self.assertEqual(second.status, ResourceExecutionStatus.CONCURRENCY_EXCEEDED)
        release.set()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(first_result), 1)
        self.assertEqual(scheduler.active, 0)
        self.assertGreaterEqual(scheduler.peak_active, 1)

    def test_peak_memory_receipt_is_bounded_and_wire_excludes_runtime_value(self) -> None:
        memory_values = iter((10, 10, 200, 150))
        scheduler = ResourceScheduler(memory_meter=lambda: next(memory_values))
        result = scheduler.execute(
            request(limits=budget(memory=512)),
            lambda context: (
                context.checkpoint(),
                ResourceExecutionArtifact(value={"opaque": "runtime-only"}, output_bytes=3),
            )[1],
        )
        self.assertEqual(result.status, ResourceExecutionStatus.COMPLETE)
        self.assertEqual(result.receipt.peak_memory_bytes, 200)
        self.assertTrue(result.receipt.reservation_released)
        wire = result.to_wire()
        self.assertNotIn("runtime-only", json.dumps(wire, sort_keys=True))
        self.assertNotIn("artifact", wire)

    def test_repeatability_and_optional_runtime_boundary(self) -> None:
        first = ResourceScheduler(clock=lambda: 0.0).execute(
            request(), lambda _: ResourceExecutionArtifact(value={"ok": True}, output_bytes=1)
        )
        second = ResourceScheduler(clock=lambda: 0.0).execute(
            request(), lambda _: ResourceExecutionArtifact(value={"ok": True}, output_bytes=1)
        )
        self.assertEqual(first.to_wire(), second.to_wire())
        tree = ast.parse(
            (ROOT / "comfyui_h3_context" / "core" / "resource_scheduling.py").read_text()
        )
        modules = [node.module or "" for node in tree.body if isinstance(node, ast.ImportFrom)]
        self.assertFalse(any(name in {"torch", "transformers", "av", "cv2"} for name in modules))
        self.assertNotEqual(LOCAL_ADAPTER_CONTRACT_SCHEMA, RESOURCE_RUNTIME_SCHEMA)

    def test_schema_and_metadata_fixture_are_present(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "resource_runtime_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/resource_runtime_v1.schema.json"
        )
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "m6_resource_runtime.json").read_text(encoding="utf-8")
        )
        self.assertEqual(fixture["schema"], RESOURCE_RUNTIME_SCHEMA)


if __name__ == "__main__":
    unittest.main()
