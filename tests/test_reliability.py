"""M7-05 bounded progress, cancellation, retry, and recovery regressions."""

from __future__ import annotations

import json
import unittest
from typing import cast

from comfyui_h3_context.core import (
    CancellationScope,
    CheckpointCompatibility,
    CheckpointState,
    LocalAdapterTimeoutError,
    LocalResourceBudget,
    ProgressLedger,
    RecoveryAction,
    RecoveryCheckpoint,
    RecoveryPolicy,
    RecoveryWorkerFactory,
    ReliabilityRunState,
    ReliabilityStage,
    ResourceCacheKey,
    ResourceExecutionArtifact,
    ResourceExecutionContext,
    ResourceExecutionRequest,
    ResourceExecutionStatus,
    ResourceScheduler,
    ResourceWorker,
    assess_checkpoint,
    execute_with_recovery,
)
from comfyui_h3_context.core.errors import ReliabilityError
from comfyui_h3_context.nodes import (
    RELIABILITY_NODE_ID,
    H3ContextReliabilityNode,
    ReliabilityNodeError,
)

SOURCE = "sha256:" + "1" * 64
SETTINGS = "sha256:" + "2" * 64
WORKFLOW = "sha256:" + "3" * 64


def cache_key(source: str = SOURCE) -> ResourceCacheKey:
    return ResourceCacheKey(
        operation_id="h3.context.planning",
        contract_schema="h3.context.plan.v1",
        provider_id="local.manual",
        provider_version="1.0.0",
        settings_fingerprint=SETTINGS,
        source_fingerprints=(source,),
    )


def request(key: ResourceCacheKey | None = None) -> ResourceExecutionRequest:
    return ResourceExecutionRequest(
        cache_key=cache_key() if key is None else key,
        budget=LocalResourceBudget(1024, 10.0, 4, 1024, 8, 1),
        cache_enabled=False,
        cancellation_required=True,
    )


class ReliabilityTests(unittest.TestCase):
    def test_progress_is_bounded_monotonic_and_json_safe(self) -> None:
        ledger = ProgressLedger("h3.context.planning", max_events=3)
        ledger.publish(ReliabilityStage.PLANNING, 0, 10, ReliabilityRunState.RUNNING)
        ledger.publish(ReliabilityStage.PLANNING, 4, 10, ReliabilityRunState.RUNNING)
        ledger.publish(ReliabilityStage.PLANNING, 10, 10, ReliabilityRunState.COMPLETED)
        self.assertEqual(ledger.latest.fraction, 1.0)
        self.assertEqual(len(ledger.events), 3)
        self.assertEqual(json.loads(json.dumps(ledger.to_wire())), ledger.to_wire())
        with self.assertRaises(ReliabilityError):
            ledger.publish(ReliabilityStage.PLANNING, 9, 10, ReliabilityRunState.RUNNING)
        with self.assertRaises(ReliabilityError):
            ledger.publish(ReliabilityStage.PLANNING, 10, 10, ReliabilityRunState.COMPLETED)

    def test_cancellation_scope_propagates_to_extraction_provider_and_planning(self) -> None:
        scope = CancellationScope()
        probes = tuple(scope.probe(stage) for stage in list(ReliabilityStage)[:3])
        self.assertTrue(all(not probe.is_cancelled() for probe in probes))
        scope.cancel("operator requested stop")
        self.assertTrue(all(probe.is_cancelled() for probe in probes))
        for probe in probes:
            with self.assertRaisesRegex(Exception, "cancelled"):
                probe.checkpoint()

    def test_checkpoint_compatibility_rejects_host_reload_and_source_drift(self) -> None:
        checkpoint = RecoveryCheckpoint(
            operation_id=cache_key().operation_id,
            cache_key=cache_key(),
            workflow_fingerprint=WORKFLOW,
            host_revision="host-a",
            stage=ReliabilityStage.PLANNING,
            completed_units=3,
            total_units=10,
            attempt=1,
            state=CheckpointState.PARTIAL,
        )
        compatible = assess_checkpoint(
            checkpoint,
            cache_key=cache_key(),
            workflow_fingerprint=WORKFLOW,
            host_revision="host-a",
        )
        self.assertEqual(compatible.status, CheckpointCompatibility.COMPATIBLE)
        stale = assess_checkpoint(
            checkpoint,
            cache_key=cache_key(),
            workflow_fingerprint=WORKFLOW,
            host_revision="host-reloaded",
        )
        self.assertEqual(stale.status, CheckpointCompatibility.STALE)
        incompatible = assess_checkpoint(
            checkpoint,
            cache_key=cache_key("sha256:" + "4" * 64),
            workflow_fingerprint=WORKFLOW,
            host_revision="host-a",
        )
        self.assertEqual(incompatible.status, CheckpointCompatibility.INCOMPATIBLE)
        self.assertTrue(stale.diagnostics)
        self.assertNotIn("host-a", json.dumps(stale.to_wire()))

    def test_recovery_retries_timeout_once_then_completes(self) -> None:
        scheduler = ResourceScheduler()
        calls = 0

        def worker_factory(_checkpoint: RecoveryCheckpoint | None) -> ResourceWorker:
            def worker(context: ResourceExecutionContext) -> ResourceExecutionArtifact:
                nonlocal calls
                calls += 1
                context.report_progress(ReliabilityStage.PLANNING, calls, 2)
                if calls == 1:
                    raise LocalAdapterTimeoutError("bounded timeout")
                context.report_progress(
                    ReliabilityStage.PLANNING, 2, 2, ReliabilityRunState.COMPLETED
                )
                return ResourceExecutionArtifact(value="ok", output_bytes=1)

            return worker

        result = execute_with_recovery(
            scheduler,
            request(),
            cast(RecoveryWorkerFactory, worker_factory),
            workflow_fingerprint=WORKFLOW,
            host_revision="host-a",
            policy=RecoveryPolicy(max_attempts=2),
        )
        self.assertEqual(calls, 2)
        self.assertEqual(result.result.status, ResourceExecutionStatus.COMPLETE)
        self.assertEqual(result.decision.action, RecoveryAction.COMPLETE)
        self.assertEqual(result.attempts, 2)
        self.assertTrue(result.progress)

    def test_recovery_never_retries_cancellation_or_incompatible_checkpoint(self) -> None:
        scheduler = ResourceScheduler()
        scope = CancellationScope()
        scope.cancel("stop")
        calls = 0

        def worker_factory(_checkpoint: RecoveryCheckpoint | None) -> ResourceWorker:
            nonlocal calls
            calls += 1

            def worker(_context: ResourceExecutionContext) -> ResourceExecutionArtifact:
                return ResourceExecutionArtifact(value="must-not-run")

            return cast(ResourceWorker, worker)

        cancelled = execute_with_recovery(
            scheduler,
            request(),
            cast(RecoveryWorkerFactory, worker_factory),
            workflow_fingerprint=WORKFLOW,
            host_revision="host-a",
            cancellation_scope=scope,
            policy=RecoveryPolicy(max_attempts=4),
        )
        self.assertEqual(cancelled.decision.action, RecoveryAction.CANCELLED)
        self.assertEqual(calls, 0)

        checkpoint = RecoveryCheckpoint(
            operation_id=cache_key().operation_id,
            cache_key=cache_key(),
            workflow_fingerprint=WORKFLOW,
            host_revision="host-old",
            stage=ReliabilityStage.PLANNING,
            completed_units=2,
            total_units=10,
            attempt=1,
            state=CheckpointState.PARTIAL,
        )
        incompatible = execute_with_recovery(
            scheduler,
            request(),
            cast(RecoveryWorkerFactory, worker_factory),
            workflow_fingerprint=WORKFLOW,
            host_revision="host-new",
            checkpoint=checkpoint,
            policy=RecoveryPolicy(max_attempts=4),
        )
        self.assertEqual(incompatible.decision.action, RecoveryAction.REJECT_STALE)
        self.assertEqual(calls, 0)

    def test_reliability_node_exposes_cancel_and_incompatible_state_without_execution(self) -> None:
        node = H3ContextReliabilityNode()
        status, progress, recovery, disclosure = node.describe(
            operation_id="h3.context",
            stage="planning",
            completed_units=3,
            total_units=10,
            run_state="cancel_requested",
            cancel_requested=True,
            attempt=1,
            max_attempts=3,
            checkpoint_status="incompatible",
            resume_requested=True,
        )
        self.assertEqual(node.NODE_ID, RELIABILITY_NODE_ID)
        self.assertEqual(progress.fraction, 0.3)
        self.assertEqual(recovery.action, RecoveryAction.CANCELLED)
        self.assertFalse(status.execution_allowed)
        self.assertIn(
            "recovery.checkpoint_incompatible",
            {item.code for item in recovery.diagnostics},
        )
        self.assertIn("cancel_requested=yes", disclosure)
        self.assertIsInstance(H3ContextReliabilityNode.VALIDATE_INPUTS(total_units=0), str)
        with self.assertRaises(ReliabilityNodeError):
            node.describe(completed_units="three")


if __name__ == "__main__":
    unittest.main()
