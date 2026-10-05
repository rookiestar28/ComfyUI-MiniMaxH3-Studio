"""Exact M17-11 plan-to-media-to-private-store integration boundary."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import cast

from ..core.av_reconstruction import (
    AVMediaDescriptor,
    AVOperation,
    AVOutputKind,
    AVPublicationState,
    AVRational,
    AVReceiptOutput,
    AVReceiptSegmentResult,
    AVReconstructionApproval,
    AVReconstructionError,
    AVReconstructionPlan,
    AVReconstructionReceipt,
)
from ..core.av_seam_policy import (
    AV_SEAM_RECEIPT_SCHEMA,
    AVSeamApproval,
    AVSeamPlan,
    AVSeamPolicyError,
    AVSeamReconstructionReceipt,
)
from ..core.canonical import canonical_fingerprint
from ..core.continuity_handoff import ContinuityBoundaryReceipt
from ..core.generation_sequence import GenerationSequenceProjection
from ..core.segment_artifacts import SegmentArtifactReceipt
from .av_reconstruction_media import (
    AVMediaExecutionResult,
    AVSeamExecutionResult,
    QualifiedAVMediaAdapter,
)
from .av_reconstruction_store import (
    AVStoreError,
    AVStoreTransaction,
    PrivateAVReconstructionStore,
)
from .av_reconstruction_transport import AVSegmentSource
from .comfyui_production_workspace import publish_reconstruction_authority
from .media_subprocess import CancellationProbe, OwnedOutputLease


class AVReconstructionPipelineError(RuntimeError):
    """Content-free integration failure with one stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _now_ms(clock_ms: Callable[[], int]) -> int:
    try:
        value = clock_ms()
    except Exception as exc:
        raise AVReconstructionPipelineError("pipeline_clock") from exc
    if type(value) is not int or value <= 0:
        raise AVReconstructionPipelineError("pipeline_clock")
    return value


def _output_handle(
    *,
    transaction_id: str,
    plan_fingerprint: str,
    role: str,
    segment_id: str | None = None,
) -> str:
    fingerprint = canonical_fingerprint(
        {
            "domain": "h3.context.av_reconstruction.output_handle.v1",
            "transaction_id": transaction_id,
            "plan_fingerprint": plan_fingerprint,
            "role": role,
            "segment_id": segment_id,
        }
    )
    return "avout_" + fingerprint.removeprefix("sha256:")


def _assert_exact_input_payloads(
    plan: AVReconstructionPlan,
    input_payloads: tuple[tuple[str, bytes | AVSegmentSource], ...],
) -> None:
    if not all(type(item) is tuple and len(item) == 2 for item in input_payloads):
        raise AVReconstructionPipelineError("input_identity_mismatch")
    if tuple(item[0] for item in input_payloads) != tuple(
        segment.segment_id for segment in plan.segments
    ):
        raise AVReconstructionPipelineError("input_identity_mismatch")
    for segment, (_, payload) in zip(plan.segments, input_payloads, strict=True):
        if type(payload) is bytes:
            if (
                len(payload) != segment.descriptor.artifact_byte_length
                or "sha256:" + hashlib.sha256(payload).hexdigest()
                != segment.descriptor.artifact_output_fingerprint
            ):
                raise AVReconstructionPipelineError("input_identity_mismatch")
            continue
        # M20-08: an incremental source declares its content identity up front; byte
        # verification happens inside the adapter's bounded stream, which hashes every
        # chunk and fails closed on any divergence before the copy is used.
        if (
            not isinstance(payload, AVSegmentSource)
            or payload.byte_length != segment.descriptor.artifact_byte_length
            or payload.content_fingerprint != segment.descriptor.artifact_output_fingerprint
        ):
            raise AVReconstructionPipelineError("input_identity_mismatch")


def _raise_if_cancelled(cancellation: CancellationProbe | None) -> None:
    if cancellation is None:
        return
    try:
        cancelled = cancellation.is_cancelled()
    except Exception as exc:
        raise AVReconstructionPipelineError("cancellation_failed") from exc
    if type(cancelled) is not bool:
        raise AVReconstructionPipelineError("cancellation_failed")
    if cancelled:
        raise AVReconstructionPipelineError("cancelled")


def _assert_plan_approval_identity(
    plan: AVReconstructionPlan,
    approval: AVReconstructionApproval,
) -> None:
    approval_wire = {
        "plan_fingerprint": approval.plan_fingerprint,
        "decision_fingerprints": list(approval.decision_fingerprints),
        "approved_at_ms": approval.approved_at_ms,
        "expires_at_ms": approval.expires_at_ms,
    }
    if canonical_fingerprint(approval_wire) != approval.fingerprint:
        raise AVReconstructionPipelineError("approval_identity_mismatch")
    try:
        # Exact completed retries may outlive approval freshness, but never plan/approval identity.
        approval.assert_executable(plan, now_ms=approval.approved_at_ms)
    except AVReconstructionError as exc:
        raise AVReconstructionPipelineError("approval_identity_mismatch") from exc


def _assert_exact_output_descriptor(
    descriptor: AVMediaDescriptor,
    *,
    plan: AVReconstructionPlan,
    segment_id: str,
    expected_frames: int,
    expected_samples: int,
    expected_duration: AVRational,
    authority_fingerprint: str,
) -> None:
    target = plan.target_profile
    video = descriptor.video
    audio = descriptor.audio
    if not (
        type(descriptor) is AVMediaDescriptor
        and descriptor.segment_id == segment_id
        and descriptor.artifact_receipt_fingerprint == authority_fingerprint
        and descriptor.capability_fingerprint == plan.capability.fingerprint
        and descriptor.container == target.container
        and descriptor.stream_count == 2
        and descriptor.subtitle_stream_count == 0
        and descriptor.data_stream_count == 0
        and descriptor.attachment_stream_count == 0
        and descriptor.warning_codes == ()
        and video is not None
        and video.codec == target.video_codec
        and video.width == target.width
        and video.height == target.height
        and video.pixel_format == target.pixel_format
        and video.color_range == target.color_range
        and video.color_space == target.color_space
        and video.color_primaries == target.color_primaries
        and video.color_transfer == target.color_transfer
        and video.rotation_degrees == target.rotation_degrees
        and video.frame_rate == target.frame_rate
        and video.time_base == target.video_time_base
        and video.start_time == AVRational(0, 1)
        and video.end_time == expected_duration
        and video.decoded_frame_count == expected_frames
        and audio is not None
        and audio.codec == target.audio_codec
        and audio.sample_format == target.sample_format
        and audio.sample_rate == target.sample_rate
        and audio.channels == target.channels
        and audio.channel_layout == target.channel_layout
        and audio.time_base == target.audio_time_base
        and audio.start_time == AVRational(0, 1)
        and audio.end_time == expected_duration
        and audio.decoded_sample_count == expected_samples
    ):
        raise AVReconstructionPipelineError("execution_identity_mismatch")


def _build_receipt(
    *,
    transaction_id: str,
    plan: AVReconstructionPlan,
    approval: AVReconstructionApproval,
    execution: AVMediaExecutionResult,
    aggregate_handle: str,
    derived_handles: tuple[tuple[str, str], ...],
    completed_at_ms: int,
) -> AVReconstructionReceipt:
    if type(execution) is not AVMediaExecutionResult:
        raise AVReconstructionPipelineError("execution_identity_mismatch")
    derived_by_segment = dict(derived_handles)
    if len(derived_by_segment) != len(derived_handles):
        raise AVReconstructionPipelineError("execution_identity_mismatch")
    expected_derived_segments = tuple(
        segment.segment_id
        for segment in plan.segments
        if segment.operations != (AVOperation.PASSTHROUGH,)
    )
    if tuple(derived_by_segment) != expected_derived_segments or len(
        execution.derived_descriptors
    ) != len(expected_derived_segments):
        raise AVReconstructionPipelineError("execution_identity_mismatch")
    expected_execution_fingerprint = canonical_fingerprint(
        {
            "plan_fingerprint": plan.fingerprint,
            "approval_fingerprint": approval.fingerprint,
            "capability_fingerprint": plan.capability.fingerprint,
            "aggregate": execution.aggregate_descriptor.to_wire(),
            "derived": [item.to_wire() for item in execution.derived_descriptors],
        }
    )
    if execution.execution_fingerprint != expected_execution_fingerprint:
        raise AVReconstructionPipelineError("execution_identity_mismatch")

    total_frames = sum(segment.accounting.emitted_frames for segment in plan.segments)
    total_samples = sum(segment.accounting.emitted_samples for segment in plan.segments)
    aggregate_duration = AVRational(
        plan.segments[-1].output_end.fraction.numerator,
        plan.segments[-1].output_end.fraction.denominator,
    )
    _assert_exact_output_descriptor(
        execution.aggregate_descriptor,
        plan=plan,
        segment_id="reconstruction.aggregate",
        expected_frames=total_frames,
        expected_samples=total_samples,
        expected_duration=aggregate_duration,
        authority_fingerprint=plan.fingerprint,
    )

    outputs: list[AVReceiptOutput] = [
        AVReceiptOutput(
            handle=aggregate_handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
            byte_length=execution.aggregate_descriptor.artifact_byte_length,
            content_fingerprint=execution.aggregate_descriptor.artifact_output_fingerprint,
            video_frame_count=total_frames,
            audio_sample_count=total_samples,
            duration=aggregate_duration,
        )
    ]
    descriptors_by_segment: dict[str, AVMediaDescriptor] = {}
    for segment_id, descriptor in zip(
        expected_derived_segments,
        execution.derived_descriptors,
        strict=True,
    ):
        segment = next(item for item in plan.segments if item.segment_id == segment_id)
        duration_fraction = segment.output_end.fraction - segment.output_start.fraction
        duration = AVRational(duration_fraction.numerator, duration_fraction.denominator)
        _assert_exact_output_descriptor(
            descriptor,
            plan=plan,
            segment_id=segment_id,
            expected_frames=segment.accounting.emitted_frames,
            expected_samples=segment.accounting.emitted_samples,
            expected_duration=duration,
            authority_fingerprint=plan.fingerprint,
        )
        descriptors_by_segment[segment_id] = descriptor
        outputs.append(
            AVReceiptOutput(
                handle=derived_by_segment[segment_id],
                kind=AVOutputKind.SEGMENT_EXPORT,
                byte_length=descriptor.artifact_byte_length,
                content_fingerprint=descriptor.artifact_output_fingerprint,
                video_frame_count=segment.accounting.emitted_frames,
                audio_sample_count=segment.accounting.emitted_samples,
                duration=duration,
            )
        )

    segment_results = tuple(
        AVReceiptSegmentResult(
            segment_id=segment.segment_id,
            artifact_receipt_fingerprint=segment.artifact_receipt_fingerprint,
            operations=segment.operations,
            accounting=segment.accounting,
            output_start=segment.output_start,
            output_end=segment.output_end,
            derived_output_handle=(
                derived_by_segment.get(segment.segment_id)
                if segment.segment_id in descriptors_by_segment
                else None
            ),
        )
        for segment in plan.segments
    )
    try:
        return AVReconstructionReceipt(
            transaction_id=transaction_id,
            plan_fingerprint=plan.fingerprint,
            approval_fingerprint=approval.fingerprint,
            selective_plan_fingerprint=plan.selective_plan_fingerprint,
            generation_state_fingerprint=plan.generation_state_fingerprint,
            artifact_receipt_fingerprints=plan.artifact_receipt_fingerprints,
            boundary_receipt_fingerprints=plan.boundary_receipt_fingerprints,
            capability_fingerprint=plan.capability.fingerprint,
            execution_fingerprint=execution.execution_fingerprint,
            segment_results=segment_results,
            outputs=tuple(outputs),
            publication_state=AVPublicationState.COMPLETE,
            completed_at_ms=completed_at_ms,
        )
    except AVReconstructionError as exc:
        raise AVReconstructionPipelineError("receipt_invalid") from exc


def execute_av_reconstruction(
    *,
    store: PrivateAVReconstructionStore,
    adapter: QualifiedAVMediaAdapter,
    transaction_id: str,
    plan: AVReconstructionPlan,
    approval: AVReconstructionApproval,
    input_payloads: tuple[tuple[str, bytes | AVSegmentSource], ...],
    clock_ms: Callable[[], int],
    cancellation: CancellationProbe | None = None,
) -> AVReconstructionReceipt:
    """Execute one exact approved plan and publish its receipt atomically."""

    if (
        type(store) is not PrivateAVReconstructionStore
        or type(adapter) is not QualifiedAVMediaAdapter
        or type(plan) is not AVReconstructionPlan
        or type(approval) is not AVReconstructionApproval
        or type(input_payloads) is not tuple
        or not callable(clock_ms)
    ):
        raise AVReconstructionPipelineError("pipeline_type")
    _assert_plan_approval_identity(plan, approval)
    _assert_exact_input_payloads(plan, input_payloads)

    existing = store.load_complete(
        transaction_id=transaction_id,
        plan_fingerprint=plan.fingerprint,
        approval_fingerprint=approval.fingerprint,
    )
    if existing is not None:
        # This entry point always runs over the default v1 receipt codec.
        return cast(AVReconstructionReceipt, existing)

    now = _now_ms(clock_ms)
    _raise_if_cancelled(cancellation)
    try:
        approval.assert_executable(plan, now_ms=now)
    except AVReconstructionError as exc:
        raise AVReconstructionPipelineError("approval_stale") from exc

    transaction: AVStoreTransaction = store.begin(
        transaction_id=transaction_id,
        plan_fingerprint=plan.fingerprint,
        approval_fingerprint=approval.fingerprint,
        expires_at_ms=approval.expires_at_ms,
    )
    with store.claim_execution(transaction) as claim:
        if claim.completed_receipt is not None:
            return cast(AVReconstructionReceipt, claim.completed_receipt)
        try:
            aggregate_handle = _output_handle(
                transaction_id=transaction_id,
                plan_fingerprint=plan.fingerprint,
                role=AVOutputKind.RECONSTRUCTION_FULL.value,
            )
            derived_handles = tuple(
                (
                    segment.segment_id,
                    _output_handle(
                        transaction_id=transaction_id,
                        plan_fingerprint=plan.fingerprint,
                        role=AVOutputKind.SEGMENT_EXPORT.value,
                        segment_id=segment.segment_id,
                    ),
                )
                for segment in plan.segments
                if segment.operations != (AVOperation.PASSTHROUGH,)
            )
            if len({aggregate_handle, *(item[1] for item in derived_handles)}) != (
                1 + len(derived_handles)
            ):
                raise AVReconstructionPipelineError("output_identity_collision")

            aggregate_lease = store.allocate_output(
                transaction,
                handle=aggregate_handle,
                kind=AVOutputKind.RECONSTRUCTION_FULL,
                claim=claim,
            )
            derived_leases: list[tuple[str, OwnedOutputLease]] = []
            for segment_id, handle in derived_handles:
                lease = store.allocate_output(
                    transaction,
                    handle=handle,
                    kind=AVOutputKind.SEGMENT_EXPORT,
                    claim=claim,
                )
                derived_leases.append((segment_id, lease))

            execution = adapter.execute_plan(
                plan=plan,
                approval=approval,
                input_payloads=input_payloads,
                aggregate_output=aggregate_lease,
                derived_outputs=tuple(derived_leases),
                cancellation=cancellation,
            )
            _raise_if_cancelled(cancellation)
            receipt = _build_receipt(
                transaction_id=transaction_id,
                plan=plan,
                approval=approval,
                execution=execution,
                aggregate_handle=aggregate_handle,
                derived_handles=derived_handles,
                completed_at_ms=_now_ms(clock_ms),
            )
            _raise_if_cancelled(cancellation)
            output_leases = (
                (aggregate_handle, aggregate_lease),
                *(
                    (handle, lease)
                    for (_, handle), (_, lease) in zip(
                        derived_handles,
                        derived_leases,
                        strict=True,
                    )
                ),
            )
            return cast(
                AVReconstructionReceipt,
                store.commit(
                    transaction,
                    receipt,
                    output_leases=output_leases,
                    claim=claim,
                ),
            )
        except Exception:
            try:
                store.abort(transaction, claim=claim)
            except AVStoreError as cleanup_exc:
                raise AVReconstructionPipelineError("cleanup_failed") from cleanup_exc
            raise


def _assert_seam_identity(
    *,
    plan: AVReconstructionPlan,
    approval: AVReconstructionApproval,
    seam_plan: AVSeamPlan,
    seam_approval: AVSeamApproval,
) -> None:
    seam_approval_wire = {
        "seam_plan_fingerprint": seam_approval.seam_plan_fingerprint,
        "seam_decision_fingerprints": list(seam_approval.seam_decision_fingerprints),
        "approved_at_ms": seam_approval.approved_at_ms,
        "expires_at_ms": seam_approval.expires_at_ms,
    }
    if canonical_fingerprint(seam_approval_wire) != seam_approval.fingerprint:
        raise AVReconstructionPipelineError("seam_approval_identity_mismatch")
    try:
        # Exact completed retries may outlive freshness, but never identity.
        seam_approval.assert_executable(
            seam_plan,
            base_plan=plan,
            base_approval=approval,
            now_ms=seam_approval.approved_at_ms,
        )
    except AVSeamPolicyError as exc:
        raise AVReconstructionPipelineError("seam_approval_identity_mismatch") from exc


def _build_seam_receipt(
    *,
    transaction_id: str,
    plan: AVReconstructionPlan,
    approval: AVReconstructionApproval,
    seam_plan: AVSeamPlan,
    seam_approval: AVSeamApproval,
    execution: AVSeamExecutionResult,
    aggregate_handle: str,
    completed_at_ms: int,
) -> AVSeamReconstructionReceipt:
    if type(execution) is not AVSeamExecutionResult:
        raise AVReconstructionPipelineError("execution_identity_mismatch")
    expected_execution_fingerprint = canonical_fingerprint(
        {
            "plan_fingerprint": plan.fingerprint,
            "approval_fingerprint": approval.fingerprint,
            "seam_plan_fingerprint": seam_plan.fingerprint,
            "seam_approval_fingerprint": seam_approval.fingerprint,
            "capability_fingerprint": plan.capability.fingerprint,
            "seam_capability_fingerprint": seam_plan.seam_capability_fingerprint,
            "aggregate": execution.aggregate_descriptor.to_wire(),
        }
    )
    if execution.execution_fingerprint != expected_execution_fingerprint:
        raise AVReconstructionPipelineError("execution_identity_mismatch")
    _assert_exact_output_descriptor(
        execution.aggregate_descriptor,
        plan=plan,
        segment_id="seam_reconstruction.aggregate",
        expected_frames=seam_plan.total_output_frames,
        expected_samples=seam_plan.total_output_samples,
        expected_duration=seam_plan.output_duration,
        authority_fingerprint=seam_plan.fingerprint,
    )
    outputs = (
        AVReceiptOutput(
            handle=aggregate_handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
            byte_length=execution.aggregate_descriptor.artifact_byte_length,
            content_fingerprint=execution.aggregate_descriptor.artifact_output_fingerprint,
            video_frame_count=seam_plan.total_output_frames,
            audio_sample_count=seam_plan.total_output_samples,
            duration=seam_plan.output_duration,
        ),
    )
    try:
        return AVSeamReconstructionReceipt(
            transaction_id=transaction_id,
            plan_fingerprint=seam_plan.fingerprint,
            approval_fingerprint=seam_approval.fingerprint,
            base_plan_fingerprint=plan.fingerprint,
            base_approval_fingerprint=approval.fingerprint,
            capability_fingerprint=plan.capability.fingerprint,
            seam_capability_fingerprint=seam_plan.seam_capability_fingerprint,
            execution_fingerprint=execution.execution_fingerprint,
            seams=seam_plan.seams,
            total_output_frames=seam_plan.total_output_frames,
            total_output_samples=seam_plan.total_output_samples,
            outputs=outputs,
            publication_state=AVPublicationState.COMPLETE,
            completed_at_ms=completed_at_ms,
        )
    except AVSeamPolicyError as exc:
        raise AVReconstructionPipelineError("receipt_invalid") from exc


def execute_seamed_av_reconstruction(
    *,
    store: PrivateAVReconstructionStore,
    adapter: QualifiedAVMediaAdapter,
    transaction_id: str,
    plan: AVReconstructionPlan,
    approval: AVReconstructionApproval,
    seam_plan: AVSeamPlan,
    seam_approval: AVSeamApproval,
    input_payloads: tuple[tuple[str, bytes | AVSegmentSource], ...],
    clock_ms: Callable[[], int],
    cancellation: CancellationProbe | None = None,
) -> AVSeamReconstructionReceipt:
    """Execute one approved seam plan and publish its seam receipt atomically.

    The store must be constructed with the seam receipt codec over its own
    caller-owned root; the default v1 store refuses seam receipts fail-closed.
    """

    if (
        type(store) is not PrivateAVReconstructionStore
        or type(adapter) is not QualifiedAVMediaAdapter
        or type(plan) is not AVReconstructionPlan
        or type(approval) is not AVReconstructionApproval
        or type(seam_plan) is not AVSeamPlan
        or type(seam_approval) is not AVSeamApproval
        or type(input_payloads) is not tuple
        or not callable(clock_ms)
    ):
        raise AVReconstructionPipelineError("pipeline_type")
    if store.receipt_schema != AV_SEAM_RECEIPT_SCHEMA:
        raise AVReconstructionPipelineError("seam_store_codec")
    _assert_plan_approval_identity(plan, approval)
    _assert_seam_identity(
        plan=plan,
        approval=approval,
        seam_plan=seam_plan,
        seam_approval=seam_approval,
    )
    _assert_exact_input_payloads(plan, input_payloads)

    existing = store.load_complete(
        transaction_id=transaction_id,
        plan_fingerprint=seam_plan.fingerprint,
        approval_fingerprint=seam_approval.fingerprint,
    )
    if existing is not None:
        return cast(AVSeamReconstructionReceipt, existing)

    now = _now_ms(clock_ms)
    _raise_if_cancelled(cancellation)
    try:
        approval.assert_executable(plan, now_ms=now)
    except AVReconstructionError as exc:
        raise AVReconstructionPipelineError("approval_stale") from exc
    try:
        seam_approval.assert_executable(
            seam_plan,
            base_plan=plan,
            base_approval=approval,
            now_ms=now,
        )
    except AVSeamPolicyError as exc:
        raise AVReconstructionPipelineError("approval_stale") from exc

    transaction: AVStoreTransaction = store.begin(
        transaction_id=transaction_id,
        plan_fingerprint=seam_plan.fingerprint,
        approval_fingerprint=seam_approval.fingerprint,
        expires_at_ms=seam_approval.expires_at_ms,
    )
    with store.claim_execution(transaction) as claim:
        if claim.completed_receipt is not None:
            return cast(AVSeamReconstructionReceipt, claim.completed_receipt)
        try:
            aggregate_handle = _output_handle(
                transaction_id=transaction_id,
                plan_fingerprint=seam_plan.fingerprint,
                role="seam_reconstruction_full",
            )
            aggregate_lease = store.allocate_output(
                transaction,
                handle=aggregate_handle,
                kind=AVOutputKind.RECONSTRUCTION_FULL,
                claim=claim,
            )
            execution = adapter.execute_seam_plan(
                plan=plan,
                approval=approval,
                seam_plan=seam_plan,
                seam_approval=seam_approval,
                input_payloads=input_payloads,
                aggregate_output=aggregate_lease,
                cancellation=cancellation,
            )
            _raise_if_cancelled(cancellation)
            receipt = _build_seam_receipt(
                transaction_id=transaction_id,
                plan=plan,
                approval=approval,
                seam_plan=seam_plan,
                seam_approval=seam_approval,
                execution=execution,
                aggregate_handle=aggregate_handle,
                completed_at_ms=_now_ms(clock_ms),
            )
            _raise_if_cancelled(cancellation)
            return cast(
                AVSeamReconstructionReceipt,
                store.commit(
                    transaction,
                    receipt,
                    output_leases=((aggregate_handle, aggregate_lease),),
                    claim=claim,
                ),
            )
        except Exception:
            try:
                store.abort(transaction, claim=claim)
            except AVStoreError as cleanup_exc:
                raise AVReconstructionPipelineError("cleanup_failed") from cleanup_exc
            raise


def _assert_production_predecessors(
    *,
    plan: AVReconstructionPlan,
    generation_sequence: GenerationSequenceProjection,
    artifact_receipts: tuple[SegmentArtifactReceipt, ...],
    continuity_receipts: tuple[ContinuityBoundaryReceipt, ...],
) -> None:
    if (
        type(generation_sequence) is not GenerationSequenceProjection
        or type(artifact_receipts) is not tuple
        or not all(type(item) is SegmentArtifactReceipt for item in artifact_receipts)
        or type(continuity_receipts) is not tuple
        or not all(type(item) is ContinuityBoundaryReceipt for item in continuity_receipts)
    ):
        raise AVReconstructionPipelineError("production_authority_type")
    sequence_plan = generation_sequence.state.plan
    if (
        sequence_plan.fingerprint != plan.generation_plan_fingerprint
        or generation_sequence.state.fingerprint != plan.generation_state_fingerprint
        or sequence_plan.workspace_id != plan.workspace_id
        or sequence_plan.workspace_revision != plan.workspace_revision
        or sequence_plan.workspace_fingerprint != plan.workspace_fingerprint
        or sequence_plan.manifest_fingerprints != plan.manifest_fingerprints
        or tuple(item.fingerprint for item in artifact_receipts)
        != plan.artifact_receipt_fingerprints
        or tuple(item.segment_id for item in artifact_receipts)
        != tuple(item.segment_id for item in plan.segments)
        or tuple(item.fingerprint for item in continuity_receipts)
        != plan.boundary_receipt_fingerprints
        or continuity_receipts != tuple(item.receipt for item in plan.boundaries)
    ):
        raise AVReconstructionPipelineError("production_authority_mismatch")


def execute_production_av_reconstruction(
    *,
    store: PrivateAVReconstructionStore,
    adapter: QualifiedAVMediaAdapter,
    transaction_id: str,
    plan: AVReconstructionPlan,
    approval: AVReconstructionApproval,
    input_payloads: tuple[tuple[str, bytes | AVSegmentSource], ...],
    clock_ms: Callable[[], int],
    generation_sequence: GenerationSequenceProjection,
    artifact_receipts: tuple[SegmentArtifactReceipt, ...],
    continuity_receipts: tuple[ContinuityBoundaryReceipt, ...],
    cancellation: CancellationProbe | None = None,
) -> AVReconstructionReceipt:
    """Execute through the sole shipped Production completion/publication seam."""

    _assert_production_predecessors(
        plan=plan,
        generation_sequence=generation_sequence,
        artifact_receipts=artifact_receipts,
        continuity_receipts=continuity_receipts,
    )
    receipt = execute_av_reconstruction(
        store=store,
        adapter=adapter,
        transaction_id=transaction_id,
        plan=plan,
        approval=approval,
        input_payloads=input_payloads,
        clock_ms=clock_ms,
        cancellation=cancellation,
    )
    publication_started = time.monotonic()
    try:
        publish_reconstruction_authority(
            plan=plan,
            generation_sequence=generation_sequence,
            artifact_receipts=artifact_receipts,
            continuity_receipts=continuity_receipts,
            reconstruction_receipt=receipt,
            store=store,
            adapter=adapter,
            deadline=publication_started + 60.0,
        )
    except Exception:
        # SECURITY: a valid private receipt must never be suppressed or expanded by UI publication.
        return receipt
    return receipt


__all__ = [
    "AVReconstructionPipelineError",
    "execute_av_reconstruction",
    "execute_production_av_reconstruction",
    "execute_seamed_av_reconstruction",
]
