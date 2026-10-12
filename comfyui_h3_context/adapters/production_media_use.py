"""Borrow operation media resources and prepare factory-owned Production previews."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, ExitStack
from types import MappingProxyType

from ..core.av_reconstruction import AVOutputKind
from .m26_production_assembly import M26ProductionAssemblyExecution, M26ProductionAssemblyRuntime
from .media_preview_authority import MediaPreviewSourceAuthority
from .media_subprocess import CancellationProbe


def run_with_media_runtime(
    *,
    lease: Callable[[], AbstractContextManager[object]],
    operation: Callable[[], None],
    on_busy: Callable[[], None],
) -> None:
    from .media_runtime_manager import MediaRuntimeBusy

    with ExitStack() as stack:
        try:
            stack.enter_context(lease())
        except MediaRuntimeBusy:
            # IMPORTANT: catch entry transition only. Catching operation exceptions here
            # changes their original disposition and can double-report a completed worker.
            on_busy()
            return
        operation()


def prepare_assembly_previews(
    *,
    execution: M26ProductionAssemblyExecution,
    runtime: M26ProductionAssemblyRuntime,
    original_previews: Mapping[str, MediaPreviewSourceAuthority],
    opaque_handle: Callable[[str], str],
    source_factory: Callable[..., MediaPreviewSourceAuthority],
    route_refusal: Callable[[], Exception],
    clock: Callable[[], float],
    admission_seconds: float,
    max_source_bytes: int,
    max_duration_ms: int,
    cancellation: CancellationProbe,
) -> Mapping[str, MediaPreviewSourceAuthority]:
    previews = dict(original_previews)
    aggregate = next(
        output
        for output in execution.reconstruction_receipt.outputs
        if output.kind is AVOutputKind.RECONSTRUCTION_FULL
    )
    # IMPORTANT: preview byte/duration bounds permit ready-without-preview, not larger
    # assembly bounds or an integrity bypass. Registry publication validates the full result.
    if (
        aggregate.byte_length <= max_source_bytes
        and aggregate.duration.fraction * 1000 <= max_duration_ms
    ):
        from .comfyui_media_preview import ensure_media_preview_route_registered

        if not ensure_media_preview_route_registered():
            raise route_refusal()
        handle = opaque_handle(aggregate.handle)
        previews[handle] = source_factory(
            plan=execution.embedded_plan,
            receipt=execution.reconstruction_receipt,
            store=runtime.reconstruction_store,
            adapter=runtime.low_level_adapter,
            opaque_output_handle=handle,
            deadline=clock() + admission_seconds,
            cancellation=cancellation,
            clock=clock,
        )
    return MappingProxyType(previews)
