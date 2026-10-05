"""M26 original-to-derived Production assembly application service."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..core.av_reconstruction import (
    AVBoundaryEvidence,
    AVMediaDescriptor,
    AVReconstructionApproval,
    AVReconstructionPlan,
    AVReconstructionReceipt,
    approve_av_reconstruction_plan,
    build_m26_derived_av_reconstruction_plan,
    qualified_av_limits,
    qualified_av_target_profile,
    qualified_ffmpeg_capability,
)
from ..core.canonical import canonical_fingerprint
from ..core.continuity_handoff import ContinuityBoundaryReceipt, ContinuityMode
from ..core.generation_sequence import GenerationSequenceProjection
from ..core.m26_assembly import (
    DerivedAVInputReceiptV1,
    M26AssemblyAuthorizationV1,
    M26DerivedAVInputPlanV1,
    ProductionAssemblyCapabilityV1,
    ProductionAssemblyReceiptV1,
    SegmentSourceContributionV1,
    build_production_assembly_receipt,
    build_source_contribution,
)
from ..core.production_import import (
    ProductionAutomaticPlanAuthorityV1,
    ProductionAutomaticPlanAuthorityV2,
    ProductionCutBoundaryReceiptV1,
)
from ..core.segment_artifacts import SegmentArtifactReceipt
from ..core.selective_rerun import SelectiveRerunDisposition
from .av_reconstruction_media import QualifiedAVMediaAdapter
from .av_reconstruction_store import PrivateAVReconstructionStore
from .av_reconstruction_transport import ArtifactStoreSegmentSource, AVSegmentSource
from .media_subprocess import CancellationProbe
from .segment_artifact_store import PrivateSegmentArtifactStore


class M26ProductionAssemblyError(RuntimeError):
    """One closed application-service failure; no private exception text crosses this boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class M26DerivedLease(Protocol):
    original_descriptor: AVMediaDescriptor
    byte_length: int
    content_fingerprint: str

    @property
    def source(self) -> AVSegmentSource: ...

    def inspect(
        self,
        *,
        segment_id: str,
        derived_receipt_fingerprint: str,
        cancellation: CancellationProbe | None = None,
    ) -> AVMediaDescriptor: ...

    def release(self) -> None: ...


@runtime_checkable
class M26MediaBridge(Protocol):
    def derive_m26_input(
        self,
        *,
        segment_id: str,
        original_receipt_fingerprint: str,
        original_source: AVSegmentSource,
        contribution_frames: int,
        contribution_samples: int,
        cancellation: CancellationProbe | None = None,
    ) -> M26DerivedLease: ...


LowLevelExecutor = Callable[..., AVReconstructionReceipt]
SourceFactory = Callable[[SegmentArtifactReceipt], AVSegmentSource]


def _default_low_level_executor(**kwargs: object) -> AVReconstructionReceipt:
    # CRITICAL: the legacy pipeline publishes through Production and therefore imports the
    # workspace adapter. Resolve it only after M26 authorization; a module-level import creates a
    # production_workspace -> M26 wrapper -> legacy pipeline cycle and breaks node registration.
    from .av_reconstruction_pipeline import execute_av_reconstruction

    return execute_av_reconstruction(**kwargs)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class M26ProductionAssemblyRuntime:
    """Private runtime bundle; filesystem roots and executable paths are never serialized."""

    artifact_store: PrivateSegmentArtifactStore
    reconstruction_store: PrivateAVReconstructionStore
    media_bridge: M26MediaBridge
    low_level_adapter: QualifiedAVMediaAdapter
    capability: ProductionAssemblyCapabilityV1

    def __post_init__(self) -> None:
        if (
            type(self.artifact_store) is not PrivateSegmentArtifactStore
            or type(self.reconstruction_store) is not PrivateAVReconstructionStore
            or not isinstance(self.media_bridge, M26MediaBridge)
            or type(self.low_level_adapter) is not QualifiedAVMediaAdapter
            or type(self.capability) is not ProductionAssemblyCapabilityV1
        ):
            raise M26ProductionAssemblyError("assembly_runtime_invalid")


@dataclass(frozen=True, slots=True)
class M26ProductionAssemblyExecution:
    authorization: M26AssemblyAuthorizationV1
    outer_plan: M26DerivedAVInputPlanV1
    embedded_plan: AVReconstructionPlan
    embedded_approval: AVReconstructionApproval
    reconstruction_receipt: AVReconstructionReceipt
    assembly_receipt: ProductionAssemblyReceiptV1


def _now_ms(clock_ms: Callable[[], int]) -> int:
    try:
        value = clock_ms()
    except Exception as exc:
        raise M26ProductionAssemblyError("assembly_clock_failed") from exc
    if type(value) is not int or value <= 0:
        raise M26ProductionAssemblyError("assembly_clock_failed")
    return value


def _assert_authorization_current(
    authorization: M26AssemblyAuthorizationV1,
    *,
    automatic_plan: ProductionAutomaticPlanAuthorityV1 | ProductionAutomaticPlanAuthorityV2,
    generation_sequence_fingerprint: str,
    generation_plan_fingerprint: str,
    generation_state_fingerprint: str,
    artifact_receipts: tuple[SegmentArtifactReceipt, ...],
    cut_boundary_receipts: tuple[ProductionCutBoundaryReceiptV1, ...],
    capability: ProductionAssemblyCapabilityV1,
    now_ms: int,
) -> None:
    if (
        type(authorization) is not M26AssemblyAuthorizationV1
        or type(automatic_plan)
        not in (ProductionAutomaticPlanAuthorityV1, ProductionAutomaticPlanAuthorityV2)
        or authorization.fingerprint
        != canonical_fingerprint(authorization._wire_without_fingerprint())
        or authorization.workspace_id != automatic_plan.workspace.workspace_id
        or authorization.workspace_revision != automatic_plan.workspace.revision
        or authorization.workspace_fingerprint != automatic_plan.workspace.fingerprint
        or authorization.automatic_plan_fingerprint != automatic_plan.fingerprint
        or authorization.managed_sequence_fingerprint != generation_sequence_fingerprint
        or authorization.generation_plan_fingerprint != generation_plan_fingerprint
        or authorization.generation_state_fingerprint != generation_state_fingerprint
        or authorization.manifest_fingerprints != automatic_plan.manifest_fingerprints
        or authorization.artifact_receipt_fingerprints
        != tuple(item.fingerprint for item in artifact_receipts)
        or authorization.cut_boundary_receipt_fingerprints
        != tuple(item.fingerprint for item in cut_boundary_receipts)
        or authorization.capability_fingerprint != capability.fingerprint
        or not authorization.issued_at_ms <= now_ms < authorization.expires_at_ms
    ):
        raise M26ProductionAssemblyError("assembly_authorization_stale")


def _cut_evidence(
    authorization: M26AssemblyAuthorizationV1,
    cuts: tuple[ProductionCutBoundaryReceiptV1, ...],
) -> tuple[AVBoundaryEvidence, ...]:
    result: list[AVBoundaryEvidence] = []
    for cut in cuts:
        # The embedded legacy plan needs its own continuity receipt type.  Its deterministic ID
        # binds the accepted Production cut receipt; the outer plan retains that original receipt.
        receipt = ContinuityBoundaryReceipt(
            boundary_id=(
                "m26.cut."
                + canonical_fingerprint(
                    {
                        "authorization": authorization.fingerprint,
                        "cut": cut.fingerprint,
                    }
                ).removeprefix("sha256:")[:40]
            ),
            mode=ContinuityMode.CUT,
            audio_not_carried=True,
        )
        result.append(
            AVBoundaryEvidence(
                workspace_id=authorization.workspace_id,
                workspace_revision=authorization.workspace_revision,
                workspace_fingerprint=authorization.workspace_fingerprint,
                predecessor_segment_id=cut.predecessor_segment_id,
                successor_segment_id=cut.successor_segment_id,
                receipt=receipt,
            )
        )
    return tuple(result)


def _default_source_factory(
    store: PrivateSegmentArtifactStore,
) -> SourceFactory:
    return lambda receipt: ArtifactStoreSegmentSource(store, receipt)


def execute_m26_production_assembly(
    *,
    runtime: M26ProductionAssemblyRuntime,
    transaction_id: str,
    authorization: M26AssemblyAuthorizationV1,
    automatic_plan: ProductionAutomaticPlanAuthorityV1 | ProductionAutomaticPlanAuthorityV2,
    generation_sequence: GenerationSequenceProjection,
    artifact_receipts: tuple[SegmentArtifactReceipt, ...],
    cut_boundary_receipts: tuple[ProductionCutBoundaryReceiptV1, ...],
    clock_ms: Callable[[], int],
    cancellation: CancellationProbe | None = None,
    low_level_executor: LowLevelExecutor = _default_low_level_executor,
    source_factory: SourceFactory | None = None,
) -> M26ProductionAssemblyExecution:
    """Sole M26 wrapper from original artifacts through derived inputs to the legacy executor."""

    if (
        type(runtime) is not M26ProductionAssemblyRuntime
        or type(generation_sequence) is not GenerationSequenceProjection
        or type(artifact_receipts) is not tuple
        or type(cut_boundary_receipts) is not tuple
        or not callable(clock_ms)
        or not callable(low_level_executor)
    ):
        raise M26ProductionAssemblyError("assembly_predecessor_type")
    sequence = generation_sequence
    plan = sequence.state.plan
    sequence_fingerprint = canonical_fingerprint(sequence.to_wire())
    now = _now_ms(clock_ms)
    _assert_authorization_current(
        authorization,
        automatic_plan=automatic_plan,
        generation_sequence_fingerprint=sequence_fingerprint,
        generation_plan_fingerprint=plan.fingerprint,
        generation_state_fingerprint=sequence.state.fingerprint,
        artifact_receipts=artifact_receipts,
        cut_boundary_receipts=cut_boundary_receipts,
        capability=runtime.capability,
        now_ms=now,
    )
    source_builder = (
        _default_source_factory(runtime.artifact_store)
        if source_factory is None
        else source_factory
    )
    if not callable(source_builder):
        raise M26ProductionAssemblyError("assembly_source_factory_invalid")

    leases: list[M26DerivedLease] = []
    derived_receipts: list[DerivedAVInputReceiptV1] = []
    descriptors: list[AVMediaDescriptor] = []
    contributions: list[SegmentSourceContributionV1] = []
    clean_ids = set(plan.clean_segment_ids)
    try:
        for proposal_row, artifact_receipt in zip(
            automatic_plan.proposal.segments,
            artifact_receipts,
            strict=True,
        ):
            requested_ms = proposal_row.duration.requested_milliseconds
            if requested_ms % 1000:
                raise M26ProductionAssemblyError("assembly_non_integer_duration")
            requested_seconds = requested_ms // 1000
            contribution_frames = requested_seconds * 24
            contribution_samples = requested_seconds * 32_000
            try:
                original_source = source_builder(artifact_receipt)
                lease = runtime.media_bridge.derive_m26_input(
                    segment_id=artifact_receipt.segment_id,
                    original_receipt_fingerprint=artifact_receipt.fingerprint,
                    original_source=original_source,
                    contribution_frames=contribution_frames,
                    contribution_samples=contribution_samples,
                    cancellation=cancellation,
                )
            except Exception as exc:
                raise M26ProductionAssemblyError("assembly_derived_input_failed") from exc
            leases.append(lease)
            original = lease.original_descriptor
            if (
                original.segment_id != artifact_receipt.segment_id
                or original.artifact_receipt_fingerprint != artifact_receipt.fingerprint
                or original.artifact_output_fingerprint != artifact_receipt.output_fingerprint
                or original.artifact_byte_length != artifact_receipt.byte_length
                or original.video is None
                or original.audio is None
                or original.video.decoded_frame_count != artifact_receipt.shape[0]
            ):
                raise M26ProductionAssemblyError("assembly_original_descriptor_mismatch")
            derived_receipt = DerivedAVInputReceiptV1(
                segment_id=artifact_receipt.segment_id,
                original_artifact_receipt_fingerprint=artifact_receipt.fingerprint,
                original_artifact_output_fingerprint=artifact_receipt.output_fingerprint,
                original_byte_length=artifact_receipt.byte_length,
                original_decoded_frame_count=original.video.decoded_frame_count,
                original_decoded_sample_count=original.audio.decoded_sample_count,
                video_crop_start_frame=0,
                video_crop_end_frame=contribution_frames,
                audio_crop_start_sample=0,
                audio_crop_end_sample=contribution_samples,
                capability_fingerprint=qualified_ffmpeg_capability().fingerprint,
                derived_byte_length=lease.byte_length,
                derived_content_fingerprint=lease.content_fingerprint,
            )
            try:
                descriptor = lease.inspect(
                    segment_id=artifact_receipt.segment_id,
                    derived_receipt_fingerprint=derived_receipt.fingerprint,
                    cancellation=cancellation,
                )
                contribution = build_source_contribution(
                    derived_receipt=derived_receipt,
                    descriptor=descriptor,
                    disposition=(
                        "reused" if artifact_receipt.segment_id in clean_ids else "generated"
                    ),
                    global_start_milliseconds=proposal_row.global_start_milliseconds,
                    global_end_milliseconds=proposal_row.global_end_milliseconds,
                )
            except Exception as exc:
                raise M26ProductionAssemblyError("assembly_derived_descriptor_mismatch") from exc
            derived_receipts.append(derived_receipt)
            descriptors.append(descriptor)
            contributions.append(contribution)

        _assert_authorization_current(
            authorization,
            automatic_plan=automatic_plan,
            generation_sequence_fingerprint=sequence_fingerprint,
            generation_plan_fingerprint=plan.fingerprint,
            generation_state_fingerprint=sequence.state.fingerprint,
            artifact_receipts=artifact_receipts,
            cut_boundary_receipts=cut_boundary_receipts,
            capability=runtime.capability,
            now_ms=_now_ms(clock_ms),
        )
        dispositions = tuple(
            SelectiveRerunDisposition.REUSE
            if item.segment_id in clean_ids
            else SelectiveRerunDisposition.QUEUE
            for item in artifact_receipts
        )
        embedded_plan = build_m26_derived_av_reconstruction_plan(
            workspace_id=authorization.workspace_id,
            workspace_revision=authorization.workspace_revision,
            workspace_fingerprint=authorization.workspace_fingerprint,
            assembly_authorization_fingerprint=authorization.fingerprint,
            generation_plan_fingerprint=authorization.generation_plan_fingerprint,
            generation_state_fingerprint=authorization.generation_state_fingerprint,
            manifest_fingerprints=authorization.manifest_fingerprints,
            derived_input_receipt_fingerprints=tuple(item.fingerprint for item in derived_receipts),
            source_contribution_fingerprints=tuple(item.fingerprint for item in contributions),
            segment_dispositions=dispositions,
            media_descriptors=tuple(descriptors),
            boundary_evidence=_cut_evidence(authorization, cut_boundary_receipts),
            capability=qualified_ffmpeg_capability(),
            limits=qualified_av_limits(),
            target_profile=qualified_av_target_profile(),
            planned_at_ms=_now_ms(clock_ms),
        )
        approved_at = _now_ms(clock_ms)
        expires_at = min(authorization.expires_at_ms, embedded_plan.approval_deadline_ms)
        if expires_at <= approved_at:
            raise M26ProductionAssemblyError("assembly_authorization_stale")
        embedded_approval = approve_av_reconstruction_plan(
            embedded_plan,
            approved_at_ms=approved_at,
            expires_at_ms=expires_at,
        )
        outer_plan = M26DerivedAVInputPlanV1(
            authorization=authorization,
            original_artifact_receipt_fingerprints=authorization.artifact_receipt_fingerprints,
            derived_input_receipts=tuple(derived_receipts),
            source_contributions=tuple(contributions),
            embedded_plan=embedded_plan,
            embedded_approval=embedded_approval,
            created_at_ms=approved_at,
        )
        reconstruction_receipt = low_level_executor(
            store=runtime.reconstruction_store,
            adapter=runtime.low_level_adapter,
            transaction_id=transaction_id,
            plan=embedded_plan,
            approval=embedded_approval,
            input_payloads=tuple(
                (item.segment_id, lease.source)
                for item, lease in zip(artifact_receipts, leases, strict=True)
            ),
            clock_ms=clock_ms,
            cancellation=cancellation,
        )
        assembly_receipt = build_production_assembly_receipt(
            transaction_id=transaction_id,
            outer_plan=outer_plan,
            reconstruction_receipt=reconstruction_receipt,
            completed_at_ms=_now_ms(clock_ms),
        )
        return M26ProductionAssemblyExecution(
            authorization=authorization,
            outer_plan=outer_plan,
            embedded_plan=embedded_plan,
            embedded_approval=embedded_approval,
            reconstruction_receipt=reconstruction_receipt,
            assembly_receipt=assembly_receipt,
        )
    except M26ProductionAssemblyError:
        raise
    except Exception as exc:
        raise M26ProductionAssemblyError("assembly_execution_failed") from exc
    finally:
        cleanup_failed = False
        # SECURITY: only job-owned derived leases are released. Original artifact-store members
        # are never locators here and cannot be deleted by success, failure or cancellation.
        for lease in reversed(leases):
            try:
                lease.release()
            except Exception:  # noqa: BLE001 - no private path or exception crosses the seam.
                cleanup_failed = True
        if cleanup_failed:
            raise M26ProductionAssemblyError("assembly_cleanup_failed")


__all__ = [
    "M26DerivedLease",
    "M26MediaBridge",
    "M26ProductionAssemblyError",
    "M26ProductionAssemblyExecution",
    "M26ProductionAssemblyRuntime",
    "execute_m26_production_assembly",
]
