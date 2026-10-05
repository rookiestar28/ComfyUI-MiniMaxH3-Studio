"""Explicit injected temporal-visual adapter seam for M11-06.

This is a qualification fixture boundary, not an action, motion, camera, edit, or style model.
It accepts caller-injected source-owned claims and never launches a process or contacts a provider.
"""

from __future__ import annotations

from collections.abc import Callable

from comfyui_h3_context.core.contracts import MediaKind, TaskMode
from comfyui_h3_context.core.errors import TemporalVisualAnalysisError
from comfyui_h3_context.core.local_adapters import (
    LocalAdapterDescriptor,
    LocalAdapterKind,
    LocalBudgetGuard,
    LocalDeviceKind,
    LocalResourceBudget,
)
from comfyui_h3_context.core.temporal_visual_analysis import (
    TemporalVisualDocument,
    TemporalVisualRequest,
)


class InjectedTemporalVisualAdapter:
    """Return caller-injected temporal evidence without discovering a runtime."""

    def __init__(
        self,
        producer: Callable[[TemporalVisualRequest, LocalBudgetGuard], TemporalVisualDocument],
        *,
        adapter_id: str = "injected_temporal_analysis",
    ) -> None:
        if not callable(producer):
            raise TemporalVisualAnalysisError("injected temporal producer must be callable")
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
            output_schema="h3.visual.temporal_analysis.v1",
            limits=LocalResourceBudget(16_384 * 1024 * 1024, 60.0, 3, 65_536, 1024, 1),
            supports_determinism=True,
            supports_seed=True,
            supports_cancellation=True,
        )

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._descriptor

    def analyze(
        self, request: TemporalVisualRequest, guard: LocalBudgetGuard
    ) -> TemporalVisualDocument:
        if not isinstance(request, TemporalVisualRequest):
            raise TemporalVisualAnalysisError(
                "injected temporal adapter requires TemporalVisualRequest"
            )
        guard.checkpoint()
        document = self._producer(request, guard)
        if not isinstance(document, TemporalVisualDocument):
            raise TemporalVisualAnalysisError(
                "injected temporal producer returned an invalid document"
            )
        guard.checkpoint()
        return document


__all__ = ["InjectedTemporalVisualAdapter"]
