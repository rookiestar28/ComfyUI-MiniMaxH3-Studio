"""Factory-owned process-local authorities for bounded aggregate or segment previews."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import InitVar, dataclass, field
from typing import NoReturn

from ..core.av_reconstruction import (
    AVOutputKind,
    AVPublicationState,
    AVReconstructionPlan,
    AVReconstructionReceipt,
)
from ..core.generation_sequence import GenerationJobState, GenerationSequenceProjection
from ..core.segment_artifacts import ArtifactLifecycleState, SegmentArtifactReceipt
from .av_reconstruction_media import QualifiedAVMediaAdapter
from .av_reconstruction_store import PrivateAVReconstructionStore
from .media_subprocess import CancellationProbe
from .segment_artifact_store import (
    ArtifactInspectionStatus,
    ArtifactStoreError,
    PrivateSegmentArtifactStore,
)

MAX_MEDIA_PREVIEW_SOURCE_BYTES = 64 * 1024 * 1024
MAX_MEDIA_PREVIEW_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_MEDIA_PREVIEW_DURATION_MS = 30_000
MAX_GENERATED_MEDIA_PREVIEW_FRAMES = 900
MAX_GENERATED_MEDIA_PREVIEW_FRAME_PIXELS = 1920 * 1920
MAX_MEDIA_PREVIEW_AUTHORITY_BYTES = 4 * 1024 * 1024
MAX_MEDIA_PREVIEW_AUTHORITY_SET_BYTES = 4 * 1024 * 1024

_FACTORY_TOKEN = object()


class MediaPreviewSourceError(RuntimeError):
    """One content-free live-source failure safe for route disposition mapping."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _receipt_matches_plan(
    plan: AVReconstructionPlan,
    receipt: AVReconstructionReceipt,
) -> bool:
    return bool(
        receipt.publication_state is AVPublicationState.COMPLETE
        and receipt.plan_fingerprint == plan.fingerprint
        and receipt.selective_plan_fingerprint == plan.selective_plan_fingerprint
        and receipt.generation_state_fingerprint == plan.generation_state_fingerprint
        and receipt.artifact_receipt_fingerprints == plan.artifact_receipt_fingerprints
        and receipt.boundary_receipt_fingerprints == plan.boundary_receipt_fingerprints
        and receipt.capability_fingerprint == plan.capability.fingerprint
        and tuple(result.segment_id for result in receipt.segment_results)
        == tuple(segment.segment_id for segment in plan.segments)
    )


@dataclass(frozen=True, slots=True)
class MediaPreviewSourceAuthority:
    """Opaque live source; private objects and native handles never enter repr/wire/equality."""

    receipt_fingerprint: str
    plan_fingerprint: str
    opaque_output_handle: str
    byte_length: int
    duration_ms: int
    _native_output_handle: str = field(repr=False, compare=False)
    _render_strategy: Callable[..., bytearray] = field(repr=False, compare=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _FACTORY_TOKEN:
            raise TypeError("media preview authorities are factory-owned")

    def wire_bytes(self) -> int:
        return (
            len(self.receipt_fingerprint)
            + len(self.plan_fingerprint)
            + len(self.opaque_output_handle)
            + 32
        )

    def __reduce__(self) -> NoReturn:
        raise TypeError("media preview authorities are not serializable")

    def render(
        self,
        *,
        deadline: float,
        cancellation: CancellationProbe | None = None,
    ) -> bytearray:
        """Render through the factory-retained exact private strategy."""

        return self._render_strategy(
            deadline=deadline,
            cancellation=cancellation,
        )


def _reconstruction_render_strategy(
    *,
    store: PrivateAVReconstructionStore,
    receipt: AVReconstructionReceipt,
    adapter: QualifiedAVMediaAdapter,
    native_output_handle: str,
) -> Callable[..., bytearray]:
    def render(
        *,
        deadline: float,
        cancellation: CancellationProbe | None = None,
    ) -> bytearray:
        return adapter.execute_preview(
            store=store,
            receipt=receipt,
            output_handle=native_output_handle,
            deadline=deadline,
            cancellation=cancellation,
        )

    return render


def build_media_preview_source_authority(
    *,
    plan: AVReconstructionPlan,
    receipt: AVReconstructionReceipt,
    store: PrivateAVReconstructionStore,
    adapter: QualifiedAVMediaAdapter,
    opaque_output_handle: str,
    deadline: float,
    cancellation: CancellationProbe | None = None,
    clock: Callable[[], float],
) -> MediaPreviewSourceAuthority:
    """Mint only after exact receipt/accounting and selected-member verification."""

    if (
        type(plan) is not AVReconstructionPlan
        or type(receipt) is not AVReconstructionReceipt
        or type(store) is not PrivateAVReconstructionStore
        or type(adapter) is not QualifiedAVMediaAdapter
        or type(opaque_output_handle) is not str
        or not callable(clock)
    ):
        raise TypeError("media preview source type")
    aggregate = receipt.outputs[0] if receipt.outputs else None
    expected_frames = sum(item.accounting.emitted_frames for item in plan.segments)
    expected_samples = sum(item.accounting.emitted_samples for item in plan.segments)
    expected_duration = (
        plan.segments[-1].output_end.fraction - plan.segments[0].output_start.fraction
    )
    if (
        not _receipt_matches_plan(plan, receipt)
        or aggregate is None
        or aggregate.kind is not AVOutputKind.RECONSTRUCTION_FULL
        or sum(item.kind is AVOutputKind.RECONSTRUCTION_FULL for item in receipt.outputs) != 1
        or aggregate.video_frame_count != expected_frames
        or aggregate.audio_sample_count != expected_samples
        or aggregate.duration.fraction != expected_duration
        or aggregate.byte_length > MAX_MEDIA_PREVIEW_SOURCE_BYTES
        or aggregate.duration.fraction * 1000 > MAX_MEDIA_PREVIEW_DURATION_MS
    ):
        raise ValueError("media_preview_source_ineligible")
    verified = store.verify_selected_output(
        receipt,
        aggregate.handle,
        maximum_bytes=MAX_MEDIA_PREVIEW_SOURCE_BYTES,
        deadline=deadline,
        cancellation=cancellation,
        clock=clock,
    )
    if (
        verified.byte_length != aggregate.byte_length
        or verified.content_fingerprint != aggregate.content_fingerprint
    ):
        raise ValueError("media_preview_source_ineligible")
    duration = aggregate.duration.fraction * 1000
    if duration.denominator != 1:
        raise ValueError("media_preview_source_ineligible")
    authority = MediaPreviewSourceAuthority(
        receipt_fingerprint=receipt.fingerprint,
        plan_fingerprint=plan.fingerprint,
        opaque_output_handle=opaque_output_handle,
        byte_length=aggregate.byte_length,
        duration_ms=int(duration),
        _native_output_handle=aggregate.handle,
        _render_strategy=_reconstruction_render_strategy(
            store=store,
            receipt=receipt,
            adapter=adapter,
            native_output_handle=aggregate.handle,
        ),
        _token=_FACTORY_TOKEN,
    )
    if authority.wire_bytes() > MAX_MEDIA_PREVIEW_AUTHORITY_BYTES:
        raise ValueError("media_preview_source_ineligible")
    return authority


def build_segment_media_preview_source_authority(
    *,
    plan: AVReconstructionPlan,
    receipt: AVReconstructionReceipt,
    store: PrivateAVReconstructionStore,
    adapter: QualifiedAVMediaAdapter,
    segment_id: str,
    opaque_output_handle: str,
    deadline: float,
    cancellation: CancellationProbe | None = None,
    clock: Callable[[], float],
) -> MediaPreviewSourceAuthority:
    """Mint one exact segment-export preview authority after member-level verification."""

    if (
        type(plan) is not AVReconstructionPlan
        or type(receipt) is not AVReconstructionReceipt
        or type(store) is not PrivateAVReconstructionStore
        or type(adapter) is not QualifiedAVMediaAdapter
        or type(segment_id) is not str
        or type(opaque_output_handle) is not str
        or not callable(clock)
    ):
        raise TypeError("media preview source type")
    plan_segments = tuple(item for item in plan.segments if item.segment_id == segment_id)
    receipt_results = tuple(
        item for item in receipt.segment_results if item.segment_id == segment_id
    )
    if (
        not _receipt_matches_plan(plan, receipt)
        or len(plan_segments) != 1
        or len(receipt_results) != 1
    ):
        raise ValueError("media_preview_source_ineligible")
    segment = plan_segments[0]
    result = receipt_results[0]
    members = tuple(
        item
        for item in receipt.outputs
        if result.derived_output_handle is not None and item.handle == result.derived_output_handle
    )
    expected_duration = segment.output_end.fraction - segment.output_start.fraction
    member = members[0] if len(members) == 1 else None
    if (
        member is None
        or member.kind is not AVOutputKind.SEGMENT_EXPORT
        or result.artifact_receipt_fingerprint != segment.artifact_receipt_fingerprint
        or result.operations != segment.operations
        or result.accounting != segment.accounting
        or result.output_start != segment.output_start
        or result.output_end != segment.output_end
        or member.video_frame_count != segment.accounting.emitted_frames
        or member.audio_sample_count != segment.accounting.emitted_samples
        or member.duration.fraction != expected_duration
        or member.byte_length > MAX_MEDIA_PREVIEW_SOURCE_BYTES
        or member.duration.fraction * 1000 > MAX_MEDIA_PREVIEW_DURATION_MS
    ):
        raise ValueError("media_preview_source_ineligible")
    verified = store.verify_selected_output(
        receipt,
        member.handle,
        maximum_bytes=MAX_MEDIA_PREVIEW_SOURCE_BYTES,
        deadline=deadline,
        cancellation=cancellation,
        clock=clock,
    )
    if (
        verified.byte_length != member.byte_length
        or verified.content_fingerprint != member.content_fingerprint
    ):
        raise ValueError("media_preview_source_ineligible")
    duration = member.duration.fraction * 1000
    if duration.denominator != 1:
        raise ValueError("media_preview_source_ineligible")
    authority = MediaPreviewSourceAuthority(
        receipt_fingerprint=receipt.fingerprint,
        plan_fingerprint=plan.fingerprint,
        opaque_output_handle=opaque_output_handle,
        byte_length=member.byte_length,
        duration_ms=int(duration),
        _native_output_handle=member.handle,
        _render_strategy=_reconstruction_render_strategy(
            store=store,
            receipt=receipt,
            adapter=adapter,
            native_output_handle=member.handle,
        ),
        _token=_FACTORY_TOKEN,
    )
    if authority.wire_bytes() > MAX_MEDIA_PREVIEW_AUTHORITY_BYTES:
        raise ValueError("media_preview_source_ineligible")
    return authority


def _generated_receipt_matches_sequence(
    sequence: GenerationSequenceProjection,
    receipt: SegmentArtifactReceipt,
) -> tuple[bool, int]:
    state = sequence.state
    jobs = tuple(job for job in state.plan.jobs if job.segment_id == receipt.segment_id)
    if len(jobs) != 1:
        return False, 0
    job = jobs[0]
    runtime = state.runtime_for(job.job_id)
    duration_ms = job.duration.delivered_milliseconds
    expected_format_matches = job.expected_format in {"mp4", "observed_video"}
    expected_shape_matches = job.expected_shape == receipt.shape or job.expected_shape == (
        receipt.shape[0],
    )
    return (
        bool(
            runtime.state is GenerationJobState.SUCCEEDED
            and runtime.artifact_receipt_fingerprint == receipt.fingerprint
            and runtime.artifact_output_fingerprint == receipt.output_fingerprint
            and receipt.state is ArtifactLifecycleState.COMPLETE
            and receipt.workspace_id == state.plan.workspace_id
            and receipt.workspace_revision == state.plan.workspace_revision
            and receipt.workspace_fingerprint == state.plan.workspace_fingerprint
            and receipt.manifest_fingerprint == job.manifest_fingerprint
            and receipt.producer_fingerprint == job.producer_fingerprint
            and receipt.graph_fingerprint == job.graph_fingerprint
            and receipt.native_binding_fingerprint == job.native_binding_fingerprint
            and receipt.model_fingerprint == job.model_fingerprint
            and receipt.runtime_fingerprint == job.runtime_fingerprint
            and receipt.settings_fingerprint == job.settings_fingerprint
            and receipt.source_id == job.source_id
            and receipt.format_label == "mp4"
            and expected_format_matches
            and expected_shape_matches
            and len(receipt.shape) == 4
            and receipt.shape[0] == job.duration.frame_count
            and receipt.shape[3] == 3
        ),
        duration_ms,
    )


def _raise_if_direct_preview_stopped(
    *,
    deadline: float,
    cancellation: CancellationProbe | None,
    clock: Callable[[], float],
) -> None:
    try:
        if cancellation is not None and cancellation.is_cancelled():
            raise MediaPreviewSourceError("preview_source_unavailable")
        if clock() >= deadline:
            raise MediaPreviewSourceError("preview_deadline")
    except MediaPreviewSourceError:
        raise
    except Exception as exc:
        raise MediaPreviewSourceError("preview_source_unavailable") from exc


def build_generated_segment_media_preview_source_authority(
    *,
    generation_sequence: GenerationSequenceProjection,
    receipt: SegmentArtifactReceipt,
    store: PrivateSegmentArtifactStore,
    opaque_output_handle: str,
    deadline: float,
    cancellation: CancellationProbe | None = None,
    clock: Callable[[], float],
) -> MediaPreviewSourceAuthority:
    """Mint one exact bounded preview directly from a retained successful MP4 segment."""

    if (
        type(generation_sequence) is not GenerationSequenceProjection
        or type(receipt) is not SegmentArtifactReceipt
        or type(store) is not PrivateSegmentArtifactStore
        or type(opaque_output_handle) is not str
        or type(deadline) is not float
        or not callable(clock)
    ):
        raise TypeError("media preview source type")
    matches, duration_ms = _generated_receipt_matches_sequence(generation_sequence, receipt)
    frame_pixels = receipt.shape[1] * receipt.shape[2] if len(receipt.shape) == 4 else 0
    if (
        not matches
        or receipt.byte_length > MAX_MEDIA_PREVIEW_RESPONSE_BYTES
        or duration_ms > MAX_MEDIA_PREVIEW_DURATION_MS
        or receipt.shape[0] > MAX_GENERATED_MEDIA_PREVIEW_FRAMES
        or frame_pixels > MAX_GENERATED_MEDIA_PREVIEW_FRAME_PIXELS
    ):
        raise ValueError("media_preview_source_ineligible")
    _raise_if_direct_preview_stopped(
        deadline=deadline,
        cancellation=cancellation,
        clock=clock,
    )
    try:
        inspection = store.inspect(
            receipt,
            maximum_bytes=MAX_MEDIA_PREVIEW_RESPONSE_BYTES,
        )
    except ArtifactStoreError as exc:
        raise ValueError("media_preview_source_ineligible") from exc
    if inspection.status is not ArtifactInspectionStatus.REUSABLE:
        raise ValueError("media_preview_source_ineligible")

    def render(
        *,
        deadline: float,
        cancellation: CancellationProbe | None = None,
    ) -> bytearray:
        _raise_if_direct_preview_stopped(
            deadline=deadline,
            cancellation=cancellation,
            clock=clock,
        )
        try:
            # CRITICAL: re-read the exact receipt through the private store for every request;
            # caching bytes or a host locator would bypass tamper/expiry checks and leak identity.
            payload = store.read(
                receipt,
                maximum_bytes=MAX_MEDIA_PREVIEW_RESPONSE_BYTES,
            )
        except ArtifactStoreError as exc:
            raise MediaPreviewSourceError("preview_source_unavailable") from exc
        _raise_if_direct_preview_stopped(
            deadline=deadline,
            cancellation=cancellation,
            clock=clock,
        )
        return bytearray(payload)

    authority = MediaPreviewSourceAuthority(
        receipt_fingerprint=receipt.fingerprint,
        plan_fingerprint=generation_sequence.state.plan.fingerprint,
        opaque_output_handle=opaque_output_handle,
        byte_length=receipt.byte_length,
        duration_ms=duration_ms,
        _native_output_handle=receipt.artifact_id,
        _render_strategy=render,
        _token=_FACTORY_TOKEN,
    )
    if authority.wire_bytes() > MAX_MEDIA_PREVIEW_AUTHORITY_BYTES:
        raise ValueError("media_preview_source_ineligible")
    return authority


__all__ = [
    "MAX_MEDIA_PREVIEW_AUTHORITY_BYTES",
    "MAX_MEDIA_PREVIEW_AUTHORITY_SET_BYTES",
    "MAX_MEDIA_PREVIEW_DURATION_MS",
    "MAX_MEDIA_PREVIEW_RESPONSE_BYTES",
    "MAX_MEDIA_PREVIEW_SOURCE_BYTES",
    "MAX_GENERATED_MEDIA_PREVIEW_FRAMES",
    "MAX_GENERATED_MEDIA_PREVIEW_FRAME_PIXELS",
    "MediaPreviewSourceError",
    "MediaPreviewSourceAuthority",
    "build_generated_segment_media_preview_source_authority",
    "build_media_preview_source_authority",
    "build_segment_media_preview_source_authority",
]
