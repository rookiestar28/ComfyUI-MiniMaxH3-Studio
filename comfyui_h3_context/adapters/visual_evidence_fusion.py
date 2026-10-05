"""Explicit injected visual-evidence fusion adapter seam for M11-07.

The adapter executes only a caller-supplied producer.  It never discovers models, launches a
process, contacts Ollama, opens media, or silently substitutes a provider.
"""

from __future__ import annotations

from collections.abc import Callable

from comfyui_h3_context.core.contracts import MediaKind, TaskMode
from comfyui_h3_context.core.errors import VisualEvidenceFusionError
from comfyui_h3_context.core.local_adapters import (
    LocalAdapterDescriptor,
    LocalAdapterKind,
    LocalBudgetGuard,
    LocalDeviceKind,
    LocalResourceBudget,
)
from comfyui_h3_context.core.visual_evidence_fusion import (
    FusionDocument,
    FusionRequest,
    VisualFusionAdapter,
)


class InjectedVisualEvidenceFusionAdapter(VisualFusionAdapter):
    """Return caller-injected fusion evidence without discovering a runtime."""

    def __init__(
        self,
        producer: Callable[[FusionRequest, LocalBudgetGuard], FusionDocument],
        *,
        adapter_id: str = "injected_visual_evidence_fusion",
    ) -> None:
        if not callable(producer):
            raise VisualEvidenceFusionError("injected fusion producer must be callable")
        descriptor = LocalAdapterDescriptor(
            adapter_id=adapter_id,
            kind=LocalAdapterKind.PERCEPTION,
            adapter_version="1.0.0",
            minimum_version="1.0.0",
            maximum_version="1.0.0",
            supported_task_modes=frozenset({TaskMode.REF2VA}),
            supported_media=frozenset({MediaKind.IMAGE, MediaKind.VIDEO}),
            supported_devices=frozenset(LocalDeviceKind),
            optional_dependencies=(),
            output_schema="h3.visual.evidence_fusion.v1",
            limits=LocalResourceBudget(16_384 * 1024 * 1024, 60.0, 3, 65_536, 1024, 1),
            supports_determinism=True,
            supports_seed=True,
            supports_cancellation=True,
        )
        self._descriptor = descriptor
        self._producer = producer

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._descriptor

    def analyze(self, request: FusionRequest, guard: LocalBudgetGuard) -> FusionDocument:
        if not isinstance(request, FusionRequest):
            raise VisualEvidenceFusionError("injected fusion adapter requires FusionRequest")
        guard.checkpoint()
        document = self._producer(request, guard)
        if not isinstance(document, FusionDocument):
            raise VisualEvidenceFusionError("injected fusion producer returned an invalid document")
        guard.checkpoint()
        return document


__all__ = ["InjectedVisualEvidenceFusionAdapter"]
