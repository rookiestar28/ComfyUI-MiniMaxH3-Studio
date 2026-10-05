"""Explicit injected media-decode adapter seam for M11-04.

This adapter is a qualification fixture boundary, not a decoder implementation.  A later caller
may replace the producer with a bounded ffprobe/ffmpeg integration after host and media gates pass;
the contract and source-PTS checks remain unchanged.
"""

from __future__ import annotations

from collections.abc import Callable

from comfyui_h3_context.core.contracts import MediaKind, TaskMode
from comfyui_h3_context.core.errors import VideoDecodeError
from comfyui_h3_context.core.local_adapters import (
    LocalAdapterDescriptor,
    LocalAdapterKind,
    LocalBudgetGuard,
    LocalDeviceKind,
    LocalResourceBudget,
)
from comfyui_h3_context.core.video_decode import (
    VideoDecodeDocument,
    VideoDecodeRequest,
)


class InjectedVideoDecodeAdapter:
    """Return a caller-injected decoded document without launching a media process."""

    def __init__(
        self,
        producer: Callable[[VideoDecodeRequest, LocalBudgetGuard], VideoDecodeDocument],
        *,
        adapter_id: str = "injected_media_decode",
    ) -> None:
        if not callable(producer):
            raise VideoDecodeError("injected decoder producer must be callable")
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
            output_schema="h3.video.decode.v1",
            limits=LocalResourceBudget(16_384 * 1024 * 1024, 60.0, 3, 65_536, 1024, 1),
            supports_determinism=True,
            supports_seed=True,
            supports_cancellation=True,
        )

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._descriptor

    def decode(self, request: VideoDecodeRequest, guard: LocalBudgetGuard) -> VideoDecodeDocument:
        if not isinstance(request, VideoDecodeRequest):
            raise VideoDecodeError("injected decoder requires VideoDecodeRequest")
        guard.checkpoint()
        document = self._producer(request, guard)
        if not isinstance(document, VideoDecodeDocument):
            raise VideoDecodeError("injected decoder producer returned an invalid document")
        guard.checkpoint()
        return document


__all__ = ["InjectedVideoDecodeAdapter"]
