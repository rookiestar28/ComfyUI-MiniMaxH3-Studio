"""Explicit injected perception adapter seam for M11-05.

This adapter is a qualification fixture boundary, not a detector/segmenter/re-ID implementation.
It accepts caller-injected source-owned values and never launches a process, loads a model, or
contacts ComfyUI/Ollama implicitly.
"""

from __future__ import annotations

from collections.abc import Callable

from comfyui_h3_context.core.contracts import MediaKind, TaskMode
from comfyui_h3_context.core.errors import PerceptionTrackingError
from comfyui_h3_context.core.local_adapters import (
    LocalAdapterDescriptor,
    LocalAdapterKind,
    LocalBudgetGuard,
    LocalDeviceKind,
    LocalResourceBudget,
)
from comfyui_h3_context.core.perception_tracking import (
    TrackingDocument,
    TrackingRequest,
)


class InjectedPerceptionTrackingAdapter:
    """Return caller-injected perception output without discovering a runtime."""

    def __init__(
        self,
        producer: Callable[[TrackingRequest, LocalBudgetGuard], TrackingDocument],
        *,
        adapter_id: str = "injected_tracking",
    ) -> None:
        if not callable(producer):
            raise PerceptionTrackingError("injected tracking producer must be callable")
        self._producer = producer
        self._descriptor = LocalAdapterDescriptor(
            adapter_id=adapter_id,
            kind=LocalAdapterKind.PERCEPTION,
            adapter_version="1.0.0",
            minimum_version="1.0.0",
            maximum_version="1.0.0",
            supported_task_modes=frozenset({TaskMode.REF2VA}),
            supported_media=frozenset({MediaKind.VIDEO}),
            supported_devices=frozenset(LocalDeviceKind),
            optional_dependencies=(),
            output_schema="h3.visual.perception_tracking.v1",
            limits=LocalResourceBudget(16_384 * 1024 * 1024, 60.0, 3, 65_536, 1024, 1),
            supports_determinism=True,
            supports_seed=True,
            supports_cancellation=True,
        )

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._descriptor

    def track(self, request: TrackingRequest, guard: LocalBudgetGuard) -> TrackingDocument:
        if not isinstance(request, TrackingRequest):
            raise PerceptionTrackingError("injected tracking requires TrackingRequest")
        guard.checkpoint()
        document = self._producer(request, guard)
        if not isinstance(document, TrackingDocument):
            raise PerceptionTrackingError("injected tracking producer returned an invalid document")
        guard.checkpoint()
        return document


__all__ = ["InjectedPerceptionTrackingAdapter"]
