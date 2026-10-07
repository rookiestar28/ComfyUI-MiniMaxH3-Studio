"""Finite per-route worker admission; coroutine cancellation never frees a running job."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")
ROUTE_WORKER_CAPACITY = 2
_LANES = frozenset(
    {
        "sidebar",
        "production",
        "authoring",
        "planning",
        "coordinator",
        "coordinator_media",
        "retained_cleanup",
        "project_document",
        "editor_recovery",
    }
)


class RouteWorkerCapacityError(RuntimeError):
    """No bounded admission remains; the owning HTTP seam maps this to finite overload."""


class RouteWorker:
    def __init__(self, lane: str) -> None:
        if lane not in _LANES:
            raise ValueError("unknown owned route worker lane")
        self._slots = threading.BoundedSemaphore(ROUTE_WORKER_CAPACITY)
        self._executor = ThreadPoolExecutor(
            max_workers=ROUTE_WORKER_CAPACITY, thread_name_prefix="h3-owned-" + lane
        )

    async def run(self, operation: Callable[[], T]) -> T:
        if not self._slots.acquire(blocking=False):
            raise RouteWorkerCapacityError("route_worker_capacity")
        context = contextvars.copy_context()

        def invoke() -> T:
            return context.run(operation)

        try:
            submitted = self._executor.submit(invoke)
        except BaseException:
            self._slots.release()
            raise
        # CRITICAL: disconnect/cancel does not kill a thread. Bind admission to the completed
        # executor future; releasing in the coroutine admits unbounded unfinished work.
        submitted.add_done_callback(lambda _: self._slots.release())
        future = asyncio.wrap_future(submitted)

        def consume_abandoned_error(done: asyncio.Future[T]) -> None:
            if not done.cancelled():
                done.exception()

        future.add_done_callback(consume_abandoned_error)
        # Shield only worker lifetime. The caller still cancels promptly and receives no late
        # result; a previously admitted domain transaction settles under its own CAS/idempotency.
        return await asyncio.shield(future)


_WORKERS: dict[str, RouteWorker] = {}
_WORKERS_LOCK = threading.Lock()


async def run_route_work(lane: str, operation: Callable[[], T]) -> T:
    with _WORKERS_LOCK:
        if lane not in _LANES:
            raise ValueError("unknown owned route worker lane")
        worker = _WORKERS.get(lane)
        if worker is None:
            worker = RouteWorker(lane)
            _WORKERS[lane] = worker
    return await worker.run(operation)
