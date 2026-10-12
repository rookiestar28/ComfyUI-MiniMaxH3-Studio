"""Execution and component-scoped scheduling for already-authorized Production work."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol

from ..core.generation_sequence import GenerationSequenceProjection
from ..core.m26_assembly import M26AssemblyAuthorizationV1
from ..core.production_import import (
    ProductionAutomaticPlanAuthorityV1,
    ProductionAutomaticPlanAuthorityV2,
)
from ..core.segment_artifacts import SegmentArtifactReceipt
from .av_reconstruction_store import AVStoreError
from .m26_production_assembly import M26ProductionAssemblyExecution, M26ProductionAssemblyRuntime
from .media_subprocess import CancellationProbe


class AssemblyExecutor(Protocol):
    def submit(self, callback: Callable[[], None], /) -> object: ...


class ProductionAssemblyExecutionService:
    """Own execution machinery, without Production entries, job state or publication rights."""

    def __init__(self) -> None:
        self._executor: AssemblyExecutor | None = None

    def submit(
        self,
        callback: Callable[[], None],
        *,
        submitter: Callable[[Callable[[], None]], object] | None,
        allocation_lock: AbstractContextManager[object],
        executor_factory: Callable[..., AssemblyExecutor],
    ) -> None:
        if submitter is not None:
            submitter(callback)
            return
        with allocation_lock:
            if self._executor is None:
                # IMPORTANT: serialize lazy allocation after authorization; submit outside the
                # business lock. Eager allocation starts workers for refused/missing runtimes.
                self._executor = executor_factory(
                    max_workers=1, thread_name_prefix="h3-m26-assembly"
                )
            executor = self._executor
        # Component replacement does not close the old executor or cancel retained callbacks.
        executor.submit(callback)

    @staticmethod
    def retire_reconstructions(
        *,
        runtime: M26ProductionAssemblyRuntime,
        live_transaction_ids: frozenset[str],
        snapshot_ms: int,
    ) -> None:
        # IMPORTANT: use the registry's locked reachability/time snapshot outside its lock.
        # Only AVStoreError is best effort; folding other errors into execution changes outcomes.
        try:
            runtime.reconstruction_store.retire_unreferenced(
                keep_transaction_ids=live_transaction_ids, completed_before_ms=snapshot_ms
            )
        except AVStoreError:
            pass

    @staticmethod
    def execute(
        *,
        runtime: M26ProductionAssemblyRuntime,
        transaction_id: str,
        authorization: M26AssemblyAuthorizationV1,
        automatic_plan: ProductionAutomaticPlanAuthorityV1 | ProductionAutomaticPlanAuthorityV2,
        generation_sequence: GenerationSequenceProjection,
        artifact_receipts: tuple[SegmentArtifactReceipt, ...],
        clock_ms: Callable[[], int],
        cancellation: CancellationProbe,
        execute_factory: Callable[..., M26ProductionAssemblyExecution],
    ) -> M26ProductionAssemblyExecution:
        # IMPORTANT: pass the issued objects unchanged. Reminting/serializing breaks the
        # registry's identity-bound publication check even when fingerprints happen to match.
        return execute_factory(
            runtime=runtime,
            transaction_id=transaction_id,
            authorization=authorization,
            automatic_plan=automatic_plan,
            generation_sequence=generation_sequence,
            artifact_receipts=artifact_receipts,
            cut_boundary_receipts=automatic_plan.cut_boundary_receipts,
            clock_ms=clock_ms,
            cancellation=cancellation,
        )
