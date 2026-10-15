"""Pure M26 exact-duration Production assembly contracts.

This module owns only locator-free identities and deterministic arithmetic.  Original media,
derived media leases, executables, stores and worker scheduling remain adapter concerns.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol, cast

from .av_reconstruction import (
    AVMediaDescriptor,
    AVOperation,
    AVReconstructionApproval,
    AVReconstructionPlan,
    AVReconstructionReceipt,
    native_av_target_profile_rule_fingerprint,
    qualified_av_target_profile,
    qualified_ffmpeg_capability,
)
from .canonical import canonical_bytes, canonical_fingerprint
from .generation_sequence import (
    GenerationJobState,
    GenerationSequenceProjection,
    artifact_receipt_matches_job_contract,
)
from .production_import import (
    ProductionAutomaticPlanAuthorityV1,
    ProductionAutomaticPlanAuthorityV2,
    ProductionCutBoundaryReceiptV1,
)
from .production_storyboard import ProposalBlockerCodeV1
from .segment_artifacts import ArtifactLifecycleState, SegmentArtifactReceipt

DERIVED_AV_INPUT_RECEIPT_SCHEMA = "h3.context.m26.derived_av_input_receipt.v1"
SEGMENT_SOURCE_CONTRIBUTION_SCHEMA = "h3.context.m26.segment_source_contribution.v1"
PRODUCTION_ASSEMBLY_CAPABILITY_SCHEMA = "h3.context.m26.production_assembly_capability.v1"
M26_ASSEMBLY_AUTHORIZATION_SCHEMA = "h3.context.m26.assembly_authorization.v1"
M26_DERIVED_AV_INPUT_PLAN_SCHEMA = "h3.context.m26.derived_av_input_plan.v1"
PRODUCTION_ASSEMBLY_RECEIPT_SCHEMA = "h3.context.m26.production_assembly_receipt.v1"
PRODUCTION_ASSEMBLY_RECEIPT_V2_SCHEMA = "h3.context.m26.production_assembly_receipt.v2"
PRODUCTION_SEAM_REPORT_SCHEMA = "h3.context.m26.production_seam_report.v1"

M26_CROP_POLICY_ID = "h3_lattice_tail_cut_v1"
M26_OUTPUT_PROFILE_ID = "legacy_av_30fps_48khz_stereo"
M26_NATIVE_OUTPUT_PROFILE_ID = "h3_native_av_v1"
#: Every selectable assembly output profile; the first is the default for new assemblies.
M26_OUTPUT_PROFILE_IDS = (M26_NATIVE_OUTPUT_PROFILE_ID, M26_OUTPUT_PROFILE_ID)
M26_SOURCE_VIDEO_FPS = 24
M26_SOURCE_AUDIO_SAMPLE_RATE = 32_000
M26_SOURCE_AUDIO_CHANNELS = 2
M26_TARGET_VIDEO_FPS = 30
M26_TARGET_AUDIO_SAMPLE_RATE = 48_000
M26_TARGET_AUDIO_CHANNELS = 2
M26_ASSEMBLY_MAX_WORKERS = 1
MAX_M26_SEGMENTS = 64
MAX_M26_WIRE_BYTES = 262_144
#: Seam label thresholds accepted from the continuation route study: a seam is "smooth" when its
#: jump ratio S1 is below 4 and its luma step S2 is at most 0.015, measured in these integer units.
SEAM_SMOOTH_S1_MILLI_BELOW = 4_000
SEAM_SMOOTH_S2_MICRO_AT_MOST = 15_000
#: Why a seam carries no label: the bounded decode ran out of budget or failed, or a metric
#: is undefined for the seam (window past the film, a silent side).
SEAM_UNMEASURED_REASONS = ("decode_budget", "decode_failed", "metric_undefined")
SEAM_LABELS = ("smooth", "visible_change", "unmeasured")

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


class M26AssemblyError(ValueError):
    """A deterministic M26 authority or exact-duration invariant failed."""


def _identifier(value: object, field_name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise M26AssemblyError(f"bounded_identifier:{field_name}")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise M26AssemblyError(f"sha256_fingerprint:{field_name}")
    return value


def _positive(value: object, field_name: str, maximum: int = 9_999_999_999_999) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise M26AssemblyError(f"positive_integer:{field_name}")
    return value


def _nonnegative(value: object, field_name: str, maximum: int = 9_999_999_999_999) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise M26AssemblyError(f"nonnegative_integer:{field_name}")
    return value


def _fingerprints(
    values: object,
    field_name: str,
    *,
    maximum: int = MAX_M26_SEGMENTS,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > maximum or (not values and not allow_empty):
        raise M26AssemblyError(field_name)
    result = tuple(_fingerprint(value, field_name) for value in values)
    if len(result) != len(set(result)):
        raise M26AssemblyError(f"duplicate_{field_name}")
    return result


class _FingerprintedContract(Protocol):
    @property
    def fingerprint(self) -> str: ...

    def _wire_without_fingerprint(self) -> dict[str, object]: ...


def _fingerprinted_wire(
    instance: _FingerprintedContract,
    fingerprint_name: str,
) -> dict[str, object]:
    value = instance._wire_without_fingerprint()
    value[fingerprint_name] = instance.fingerprint
    return value


@dataclass(frozen=True, slots=True)
class DerivedAVInputReceiptV1:
    """Non-cyclic identity for one verified original-to-cropped executor input."""

    segment_id: str
    original_artifact_receipt_fingerprint: str
    original_artifact_output_fingerprint: str
    original_byte_length: int
    original_decoded_frame_count: int
    original_decoded_sample_count: int
    video_crop_start_frame: int
    video_crop_end_frame: int
    audio_crop_start_sample: int
    audio_crop_end_sample: int
    capability_fingerprint: str
    derived_byte_length: int
    derived_content_fingerprint: str
    crop_policy_id: str = M26_CROP_POLICY_ID
    receipt_fingerprint: str | None = None
    schema: str = DERIVED_AV_INPUT_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != DERIVED_AV_INPUT_RECEIPT_SCHEMA:
            raise M26AssemblyError("derived_input_schema")
        _identifier(self.segment_id, "derived_input.segment_id")
        for value, field_name in (
            (self.original_artifact_receipt_fingerprint, "derived_input.original_receipt"),
            (self.original_artifact_output_fingerprint, "derived_input.original_output"),
            (self.capability_fingerprint, "derived_input.capability"),
            (self.derived_content_fingerprint, "derived_input.derived_content"),
        ):
            _fingerprint(value, field_name)
        _positive(self.original_byte_length, "derived_input.original_byte_length", 4 << 30)
        _positive(
            self.original_decoded_frame_count,
            "derived_input.original_decoded_frames",
            10_000_000,
        )
        _positive(
            self.original_decoded_sample_count,
            "derived_input.original_decoded_samples",
            1_000_000_000,
        )
        _positive(self.derived_byte_length, "derived_input.derived_byte_length", 4 << 30)
        if (
            self.crop_policy_id != M26_CROP_POLICY_ID
            or self.video_crop_start_frame != 0
            or self.audio_crop_start_sample != 0
        ):
            raise M26AssemblyError("derived_input_crop_policy")
        _positive(self.video_crop_end_frame, "derived_input.video_crop_end", 10_000_000)
        _positive(self.audio_crop_end_sample, "derived_input.audio_crop_end", 1_000_000_000)
        if (
            self.video_crop_end_frame > self.original_decoded_frame_count
            or self.audio_crop_end_sample > self.original_decoded_sample_count
        ):
            raise M26AssemblyError("derived_input_short_source")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.receipt_fingerprint is None:
            object.__setattr__(self, "receipt_fingerprint", expected)
        elif self.receipt_fingerprint != expected:
            raise M26AssemblyError("derived_input_fingerprint_mismatch")

    @property
    def fingerprint(self) -> str:
        if self.receipt_fingerprint is None:  # pragma: no cover
            raise M26AssemblyError("derived_input_fingerprint_uninitialized")
        return self.receipt_fingerprint

    @property
    def contribution_frames(self) -> int:
        return self.video_crop_end_frame

    @property
    def contribution_samples(self) -> int:
        return self.audio_crop_end_sample

    @property
    def trimmed_tail_frames(self) -> int:
        return self.original_decoded_frame_count - self.video_crop_end_frame

    @property
    def trimmed_tail_samples(self) -> int:
        return self.original_decoded_sample_count - self.audio_crop_end_sample

    def _wire_without_fingerprint(self) -> dict[str, object]:
        # IMPORTANT: no media-descriptor fingerprint belongs here.  The descriptor is probed
        # after this receipt exists, so including it would create a circular content identity.
        return {
            "schema": self.schema,
            "segment_id": self.segment_id,
            "original_artifact_receipt_fingerprint": self.original_artifact_receipt_fingerprint,
            "original_artifact_output_fingerprint": self.original_artifact_output_fingerprint,
            "original_byte_length": self.original_byte_length,
            "original_decoded_frame_count": self.original_decoded_frame_count,
            "original_decoded_sample_count": self.original_decoded_sample_count,
            "video_crop_span": [self.video_crop_start_frame, self.video_crop_end_frame],
            "audio_crop_span": [self.audio_crop_start_sample, self.audio_crop_end_sample],
            "crop_policy_id": self.crop_policy_id,
            "capability_fingerprint": self.capability_fingerprint,
            "derived_byte_length": self.derived_byte_length,
            "derived_content_fingerprint": self.derived_content_fingerprint,
        }

    def to_wire(self) -> dict[str, object]:
        return _fingerprinted_wire(self, "receipt_fingerprint")


def m26_output_rates(output_profile_id: str) -> tuple[int, int]:
    """The (video fps, audio sample rate) an assembly output profile emits."""

    if output_profile_id == M26_OUTPUT_PROFILE_ID:
        return M26_TARGET_VIDEO_FPS, M26_TARGET_AUDIO_SAMPLE_RATE
    if output_profile_id == M26_NATIVE_OUTPUT_PROFILE_ID:
        return M26_SOURCE_VIDEO_FPS, M26_SOURCE_AUDIO_SAMPLE_RATE
    raise M26AssemblyError("output_profile_id")


def m26_output_operations(output_profile_id: str) -> tuple[str, ...]:
    """The exact per-segment operations each profile admits before the one aggregate encode."""

    if output_profile_id == M26_OUTPUT_PROFILE_ID:
        return (AVOperation.NORMALIZE_VIDEO.value, AVOperation.NORMALIZE_AUDIO.value)
    if output_profile_id == M26_NATIVE_OUTPUT_PROFILE_ID:
        return (AVOperation.PASSTHROUGH.value,)
    raise M26AssemblyError("output_profile_id")


def m26_output_profile_fingerprint(output_profile_id: str) -> str:
    """The profile identity a capability binds: the fixed legacy profile or the native rule."""

    if output_profile_id == M26_OUTPUT_PROFILE_ID:
        return qualified_av_target_profile().fingerprint
    if output_profile_id == M26_NATIVE_OUTPUT_PROFILE_ID:
        return native_av_target_profile_rule_fingerprint()
    raise M26AssemblyError("output_profile_id")


def _contribution_profile(target_video_fps: int, target_audio_sample_rate: int) -> str | None:
    for profile_id in M26_OUTPUT_PROFILE_IDS:
        if m26_output_rates(profile_id) == (target_video_fps, target_audio_sample_rate):
            return profile_id
    return None


@dataclass(frozen=True, slots=True)
class SegmentSourceContributionV1:
    """Exact source and normalized target accounting for one cut-only M26 segment."""

    segment_id: str
    disposition: str
    derived_input_receipt_fingerprint: str
    media_descriptor_fingerprint: str
    global_start_milliseconds: int
    global_end_milliseconds: int
    requested_seconds: int
    contribution_frames: int
    contribution_samples: int
    trimmed_tail_frames: int
    trimmed_tail_samples: int
    target_frames: int
    target_samples: int
    normalization_generated_frames: int
    normalization_inserted_samples: int
    source_video_fps: int = M26_SOURCE_VIDEO_FPS
    source_audio_sample_rate: int = M26_SOURCE_AUDIO_SAMPLE_RATE
    source_audio_channels: int = M26_SOURCE_AUDIO_CHANNELS
    target_video_fps: int = M26_TARGET_VIDEO_FPS
    target_audio_sample_rate: int = M26_TARGET_AUDIO_SAMPLE_RATE
    target_audio_channels: int = M26_TARGET_AUDIO_CHANNELS
    join_policy: str = "cut"
    continuity_prefix_frames: int = 0
    continuity_prefix_samples: int = 0
    burn_in_frames: int = 0
    overlap_frames: int = 0
    operations: tuple[str, ...] = (
        AVOperation.NORMALIZE_VIDEO.value,
        AVOperation.NORMALIZE_AUDIO.value,
    )
    contribution_fingerprint: str | None = None
    schema: str = SEGMENT_SOURCE_CONTRIBUTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SEGMENT_SOURCE_CONTRIBUTION_SCHEMA:
            raise M26AssemblyError("source_contribution_schema")
        _identifier(self.segment_id, "source_contribution.segment_id")
        if self.disposition not in {"reused", "generated"}:
            raise M26AssemblyError("source_contribution_disposition")
        _fingerprint(
            self.derived_input_receipt_fingerprint,
            "source_contribution.derived_input_receipt",
        )
        _fingerprint(self.media_descriptor_fingerprint, "source_contribution.descriptor")
        _nonnegative(self.global_start_milliseconds, "source_contribution.global_start")
        _positive(self.global_end_milliseconds, "source_contribution.global_end")
        _positive(self.requested_seconds, "source_contribution.requested_seconds", 60)
        # IMPORTANT: the target rates are self-describing and select the profile; both profiles'
        # arithmetic stays exact. A rate pair outside the two profiles is refused, never scaled.
        profile_id = (
            _contribution_profile(self.target_video_fps, self.target_audio_sample_rate)
            if type(self.target_video_fps) is int and type(self.target_audio_sample_rate) is int
            else None
        )
        if profile_id is None:
            raise M26AssemblyError("source_contribution_accounting")
        if (
            self.global_end_milliseconds - self.global_start_milliseconds
            != self.requested_seconds * 1000
            or self.source_video_fps != M26_SOURCE_VIDEO_FPS
            or self.source_audio_sample_rate != M26_SOURCE_AUDIO_SAMPLE_RATE
            or self.source_audio_channels != M26_SOURCE_AUDIO_CHANNELS
            or self.target_audio_channels != M26_TARGET_AUDIO_CHANNELS
            or self.contribution_frames != self.requested_seconds * M26_SOURCE_VIDEO_FPS
            or self.contribution_samples != self.requested_seconds * M26_SOURCE_AUDIO_SAMPLE_RATE
            or self.target_frames != self.requested_seconds * self.target_video_fps
            or self.target_samples != self.requested_seconds * self.target_audio_sample_rate
            or self.normalization_generated_frames != self.target_frames - self.contribution_frames
            or self.normalization_inserted_samples
            != self.target_samples - self.contribution_samples
        ):
            raise M26AssemblyError("source_contribution_accounting")
        for value, field_name in (
            (self.trimmed_tail_frames, "source_contribution.trimmed_tail_frames"),
            (self.trimmed_tail_samples, "source_contribution.trimmed_tail_samples"),
            (self.continuity_prefix_frames, "source_contribution.continuity_prefix_frames"),
            (self.continuity_prefix_samples, "source_contribution.continuity_prefix_samples"),
            (self.burn_in_frames, "source_contribution.burn_in_frames"),
            (self.overlap_frames, "source_contribution.overlap_frames"),
        ):
            _nonnegative(value, field_name, 1_000_000_000)
        if (
            self.join_policy != "cut"
            or any(
                (
                    self.continuity_prefix_frames,
                    self.continuity_prefix_samples,
                    self.burn_in_frames,
                    self.overlap_frames,
                )
            )
            or self.operations != m26_output_operations(profile_id)
        ):
            raise M26AssemblyError("source_contribution_cut_policy")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.contribution_fingerprint is None:
            object.__setattr__(self, "contribution_fingerprint", expected)
        elif self.contribution_fingerprint != expected:
            raise M26AssemblyError("source_contribution_fingerprint_mismatch")

    @property
    def fingerprint(self) -> str:
        if self.contribution_fingerprint is None:  # pragma: no cover
            raise M26AssemblyError("source_contribution_fingerprint_uninitialized")
        return self.contribution_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "segment_id": self.segment_id,
            "disposition": self.disposition,
            "derived_input_receipt_fingerprint": self.derived_input_receipt_fingerprint,
            "media_descriptor_fingerprint": self.media_descriptor_fingerprint,
            "global_requested_span_milliseconds": [
                self.global_start_milliseconds,
                self.global_end_milliseconds,
            ],
            "requested_seconds": self.requested_seconds,
            "source": {
                "video_fps": self.source_video_fps,
                "audio_sample_rate": self.source_audio_sample_rate,
                "audio_channels": self.source_audio_channels,
                "contribution_frames": self.contribution_frames,
                "contribution_samples": self.contribution_samples,
                "trimmed_tail_frames": self.trimmed_tail_frames,
                "trimmed_tail_samples": self.trimmed_tail_samples,
            },
            "target": {
                "video_fps": self.target_video_fps,
                "audio_sample_rate": self.target_audio_sample_rate,
                "audio_channels": self.target_audio_channels,
                "frames": self.target_frames,
                "samples": self.target_samples,
            },
            "normalization": {
                "operations": list(self.operations),
                "generated_frames": self.normalization_generated_frames,
                "inserted_samples": self.normalization_inserted_samples,
            },
            "cut": {
                "join_policy": self.join_policy,
                "continuity_prefix_frames": self.continuity_prefix_frames,
                "continuity_prefix_samples": self.continuity_prefix_samples,
                "burn_in_frames": self.burn_in_frames,
                "overlap_frames": self.overlap_frames,
            },
        }

    def to_wire(self) -> dict[str, object]:
        return _fingerprinted_wire(self, "contribution_fingerprint")


@dataclass(frozen=True, slots=True)
class ProductionAssemblyCapabilityV1:
    runtime_capability_fingerprint: str
    output_profile_fingerprint: str
    store_identity_fingerprint: str
    output_profile_id: str = M26_OUTPUT_PROFILE_ID
    crop_policy_id: str = M26_CROP_POLICY_ID
    max_workers: int = M26_ASSEMBLY_MAX_WORKERS
    capability_fingerprint: str | None = None
    schema: str = PRODUCTION_ASSEMBLY_CAPABILITY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PRODUCTION_ASSEMBLY_CAPABILITY_SCHEMA:
            raise M26AssemblyError("assembly_capability_schema")
        for value, field_name in (
            (self.runtime_capability_fingerprint, "assembly_capability.runtime"),
            (self.output_profile_fingerprint, "assembly_capability.output_profile"),
            (self.store_identity_fingerprint, "assembly_capability.store"),
        ):
            _fingerprint(value, field_name)
        if (
            self.output_profile_id not in M26_OUTPUT_PROFILE_IDS
            or self.crop_policy_id != M26_CROP_POLICY_ID
            or self.max_workers != M26_ASSEMBLY_MAX_WORKERS
            or self.runtime_capability_fingerprint != qualified_ffmpeg_capability().fingerprint
            or self.output_profile_fingerprint
            != m26_output_profile_fingerprint(self.output_profile_id)
        ):
            raise M26AssemblyError("assembly_capability_profile")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.capability_fingerprint is None:
            object.__setattr__(self, "capability_fingerprint", expected)
        elif self.capability_fingerprint != expected:
            raise M26AssemblyError("assembly_capability_fingerprint_mismatch")

    @property
    def fingerprint(self) -> str:
        if self.capability_fingerprint is None:  # pragma: no cover
            raise M26AssemblyError("assembly_capability_fingerprint_uninitialized")
        return self.capability_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "runtime_capability_fingerprint": self.runtime_capability_fingerprint,
            "output_profile_id": self.output_profile_id,
            "output_profile_fingerprint": self.output_profile_fingerprint,
            "store_identity_fingerprint": self.store_identity_fingerprint,
            "crop_policy_id": self.crop_policy_id,
            "max_workers": self.max_workers,
        }

    def to_wire(self) -> dict[str, object]:
        return _fingerprinted_wire(self, "capability_fingerprint")


def build_production_assembly_capability(
    *,
    store_identity_fingerprint: str,
    output_profile_id: str = M26_OUTPUT_PROFILE_ID,
) -> ProductionAssemblyCapabilityV1:
    """Project one closed backend-owned M26 execution profile."""

    return ProductionAssemblyCapabilityV1(
        runtime_capability_fingerprint=qualified_ffmpeg_capability().fingerprint,
        output_profile_fingerprint=m26_output_profile_fingerprint(output_profile_id),
        store_identity_fingerprint=store_identity_fingerprint,
        output_profile_id=output_profile_id,
    )


@dataclass(frozen=True, slots=True)
class M26AssemblyAuthorizationV1:
    authorization_id: str
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    automatic_plan_fingerprint: str
    managed_sequence_fingerprint: str
    generation_plan_fingerprint: str
    generation_state_fingerprint: str
    manifest_fingerprints: tuple[str, ...]
    artifact_receipt_fingerprints: tuple[str, ...]
    cut_boundary_receipt_fingerprints: tuple[str, ...]
    capability_fingerprint: str
    issued_at_ms: int
    expires_at_ms: int
    output_profile_id: str = M26_OUTPUT_PROFILE_ID
    crop_policy_id: str = M26_CROP_POLICY_ID
    authorization_fingerprint: str | None = None
    schema: str = M26_ASSEMBLY_AUTHORIZATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != M26_ASSEMBLY_AUTHORIZATION_SCHEMA:
            raise M26AssemblyError("assembly_authorization_schema")
        _identifier(self.authorization_id, "assembly_authorization.id")
        _identifier(self.workspace_id, "assembly_authorization.workspace_id")
        _positive(self.workspace_revision, "assembly_authorization.workspace_revision", 1_000_000)
        for value, field_name in (
            (self.workspace_fingerprint, "assembly_authorization.workspace"),
            (self.automatic_plan_fingerprint, "assembly_authorization.automatic_plan"),
            (self.managed_sequence_fingerprint, "assembly_authorization.managed_sequence"),
            (self.generation_plan_fingerprint, "assembly_authorization.generation_plan"),
            (self.generation_state_fingerprint, "assembly_authorization.generation_state"),
            (self.capability_fingerprint, "assembly_authorization.capability"),
        ):
            _fingerprint(value, field_name)
        manifests = _fingerprints(self.manifest_fingerprints, "assembly_authorization.manifests")
        artifacts = _fingerprints(
            self.artifact_receipt_fingerprints,
            "assembly_authorization.artifacts",
        )
        boundaries = _fingerprints(
            self.cut_boundary_receipt_fingerprints,
            "assembly_authorization.boundaries",
            maximum=MAX_M26_SEGMENTS - 1,
            allow_empty=True,
        )
        if len(artifacts) != len(manifests) or len(boundaries) != len(artifacts) - 1:
            raise M26AssemblyError("assembly_authorization_predecessor_count")
        _positive(self.issued_at_ms, "assembly_authorization.issued_at")
        _positive(self.expires_at_ms, "assembly_authorization.expires_at")
        if (
            self.expires_at_ms <= self.issued_at_ms
            or self.output_profile_id not in M26_OUTPUT_PROFILE_IDS
            or self.crop_policy_id != M26_CROP_POLICY_ID
        ):
            raise M26AssemblyError("assembly_authorization_policy")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.authorization_fingerprint is None:
            object.__setattr__(self, "authorization_fingerprint", expected)
        elif self.authorization_fingerprint != expected:
            raise M26AssemblyError("assembly_authorization_fingerprint_mismatch")

    @property
    def fingerprint(self) -> str:
        if self.authorization_fingerprint is None:  # pragma: no cover
            raise M26AssemblyError("assembly_authorization_fingerprint_uninitialized")
        return self.authorization_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "authorization_id": self.authorization_id,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "automatic_plan_fingerprint": self.automatic_plan_fingerprint,
            "managed_sequence_fingerprint": self.managed_sequence_fingerprint,
            "generation_plan_fingerprint": self.generation_plan_fingerprint,
            "generation_state_fingerprint": self.generation_state_fingerprint,
            "manifest_fingerprints": list(self.manifest_fingerprints),
            "artifact_receipt_fingerprints": list(self.artifact_receipt_fingerprints),
            "cut_boundary_receipt_fingerprints": list(self.cut_boundary_receipt_fingerprints),
            "capability_fingerprint": self.capability_fingerprint,
            "output_profile_id": self.output_profile_id,
            "crop_policy_id": self.crop_policy_id,
            "issued_at_ms": self.issued_at_ms,
            "expires_at_ms": self.expires_at_ms,
        }

    def to_wire(self) -> dict[str, object]:
        return _fingerprinted_wire(self, "authorization_fingerprint")


def assembly_start_holds_satisfied(
    automatic_plan: ProductionAutomaticPlanAuthorityV1 | ProductionAutomaticPlanAuthorityV2,
    generation_sequence: GenerationSequenceProjection,
    artifact_receipts: tuple[SegmentArtifactReceipt, ...],
) -> bool:
    """Interpret start holds against accepted completion without rewriting the proposal."""

    if automatic_plan.startable:
        return True
    if type(
        automatic_plan
    ) is not ProductionAutomaticPlanAuthorityV2 or automatic_plan.start_hold_codes != (
        ProposalBlockerCodeV1.MANAGED_EXECUTION_QUALIFICATION_PENDING,
    ):
        return False
    # CRITICAL: mode qualification occurs after immutable plan import. Only exact completed
    # originals discharge its historical pending hold; this never admits an unqualified Start.
    state = generation_sequence.state
    plan = state.plan
    workspace = automatic_plan.workspace
    if (
        state.cancellation_requested
        or not state.complete
        or plan.clean_segment_ids
        or generation_sequence.eligible_commands
        or plan.source_workspace_authority is not workspace
        or plan.manifest_fingerprints != automatic_plan.manifest_fingerprints
        or tuple(job.segment_id for job in plan.jobs) != automatic_plan.reconstruction_order
        or tuple(row.segment_id for row in artifact_receipts) != automatic_plan.reconstruction_order
        or len(state.runtimes) != len(artifact_receipts)
    ):
        return False
    return all(
        runtime.state is GenerationJobState.SUCCEEDED
        and receipt.state is ArtifactLifecycleState.COMPLETE
        and receipt.workspace_id == workspace.workspace_id
        and receipt.workspace_revision == workspace.revision
        and receipt.workspace_fingerprint == workspace.fingerprint
        and receipt.manifest_fingerprint == job.manifest_fingerprint
        and receipt.producer_fingerprint == job.producer_fingerprint
        and receipt.native_binding_fingerprint == job.native_binding_fingerprint
        and receipt.model_fingerprint == job.model_fingerprint
        and receipt.runtime_fingerprint == job.runtime_fingerprint
        and receipt.settings_fingerprint == job.settings_fingerprint
        and receipt.source_id == job.source_id
        and receipt.output_fingerprint is not None
        and runtime.artifact_receipt_fingerprint == receipt.fingerprint
        and runtime.artifact_output_fingerprint == receipt.output_fingerprint
        and artifact_receipt_matches_job_contract(job, receipt)
        for job, runtime, receipt in zip(plan.jobs, state.runtimes, artifact_receipts, strict=True)
    )


def mint_m26_assembly_authorization(
    *,
    authorization_id: str,
    automatic_plan: ProductionAutomaticPlanAuthorityV1 | ProductionAutomaticPlanAuthorityV2,
    generation_sequence: GenerationSequenceProjection,
    artifact_receipts: tuple[SegmentArtifactReceipt, ...],
    cut_boundary_receipts: tuple[ProductionCutBoundaryReceiptV1, ...],
    capability: ProductionAssemblyCapabilityV1,
    issued_at_ms: int,
    expires_at_ms: int,
) -> M26AssemblyAuthorizationV1:
    """Join exact accepted Production predecessors before any derived-file allocation."""

    if (
        type(automatic_plan)
        not in (ProductionAutomaticPlanAuthorityV1, ProductionAutomaticPlanAuthorityV2)
        or type(generation_sequence) is not GenerationSequenceProjection
        or type(capability) is not ProductionAssemblyCapabilityV1
        or type(artifact_receipts) is not tuple
        or not all(type(item) is SegmentArtifactReceipt for item in artifact_receipts)
        or type(cut_boundary_receipts) is not tuple
        or not all(type(item) is ProductionCutBoundaryReceiptV1 for item in cut_boundary_receipts)
    ):
        raise M26AssemblyError("assembly_authorization_predecessor_type")
    workspace = automatic_plan.workspace
    sequence = generation_sequence.state
    plan = sequence.plan
    segment_ids = automatic_plan.reconstruction_order
    if (
        not assembly_start_holds_satisfied(automatic_plan, generation_sequence, artifact_receipts)
        or plan.source_workspace_authority is not workspace
        or plan.workspace_id != workspace.workspace_id
        or plan.workspace_revision != workspace.revision
        or plan.workspace_fingerprint != workspace.fingerprint
        or plan.manifest_fingerprints != automatic_plan.manifest_fingerprints
        or tuple(item.segment_id for item in artifact_receipts) != segment_ids
        or tuple(item.fingerprint for item in cut_boundary_receipts)
        != tuple(item.fingerprint for item in automatic_plan.cut_boundary_receipts)
    ):
        raise M26AssemblyError("assembly_authorization_predecessor_mismatch")
    dirty_by_id = {item.segment_id: item for item in plan.jobs}
    runtime_by_id = {item.job_id: item for item in sequence.runtimes}
    for receipt, manifest_fingerprint in zip(
        artifact_receipts,
        automatic_plan.manifest_fingerprints,
        strict=True,
    ):
        if (
            receipt.state is not ArtifactLifecycleState.COMPLETE
            or receipt.workspace_id != workspace.workspace_id
            or receipt.workspace_revision != workspace.revision
            or receipt.workspace_fingerprint != workspace.fingerprint
            or receipt.manifest_fingerprint != manifest_fingerprint
            or receipt.output_fingerprint is None
        ):
            raise M26AssemblyError("assembly_authorization_artifact_mismatch")
        job = dirty_by_id.get(receipt.segment_id)
        if job is None:
            if receipt.segment_id not in plan.clean_segment_ids:
                raise M26AssemblyError("assembly_authorization_artifact_mismatch")
            continue
        runtime = runtime_by_id.get(job.job_id)
        if (
            runtime is None
            or runtime.state is not GenerationJobState.SUCCEEDED
            or runtime.artifact_receipt_fingerprint != receipt.fingerprint
            or runtime.artifact_output_fingerprint != receipt.output_fingerprint
        ):
            raise M26AssemblyError("assembly_authorization_sequence_incomplete")
    return M26AssemblyAuthorizationV1(
        authorization_id=authorization_id,
        workspace_id=workspace.workspace_id,
        workspace_revision=workspace.revision,
        workspace_fingerprint=workspace.fingerprint,
        automatic_plan_fingerprint=automatic_plan.fingerprint,
        managed_sequence_fingerprint=canonical_fingerprint(generation_sequence.to_wire()),
        generation_plan_fingerprint=plan.fingerprint,
        generation_state_fingerprint=sequence.fingerprint,
        manifest_fingerprints=plan.manifest_fingerprints,
        artifact_receipt_fingerprints=tuple(item.fingerprint for item in artifact_receipts),
        cut_boundary_receipt_fingerprints=tuple(item.fingerprint for item in cut_boundary_receipts),
        capability_fingerprint=capability.fingerprint,
        issued_at_ms=issued_at_ms,
        expires_at_ms=expires_at_ms,
        output_profile_id=capability.output_profile_id,
    )


def descriptor_fingerprint(descriptor: AVMediaDescriptor) -> str:
    if type(descriptor) is not AVMediaDescriptor:
        raise M26AssemblyError("derived_descriptor_type")
    return canonical_fingerprint(descriptor.to_wire())


def build_source_contribution(
    *,
    derived_receipt: DerivedAVInputReceiptV1,
    descriptor: AVMediaDescriptor,
    disposition: str,
    global_start_milliseconds: int,
    global_end_milliseconds: int,
    output_profile_id: str = M26_OUTPUT_PROFILE_ID,
) -> SegmentSourceContributionV1:
    """Bind the post-crop descriptor and the profile's exact 24/32 -> target arithmetic."""

    if (
        type(derived_receipt) is not DerivedAVInputReceiptV1
        or type(descriptor) is not AVMediaDescriptor
    ):
        raise M26AssemblyError("source_contribution_type")
    video = descriptor.video
    audio = descriptor.audio
    if (
        descriptor.segment_id != derived_receipt.segment_id
        or descriptor.artifact_receipt_fingerprint != derived_receipt.fingerprint
        or descriptor.artifact_output_fingerprint != derived_receipt.derived_content_fingerprint
        or descriptor.artifact_byte_length != derived_receipt.derived_byte_length
        or descriptor.capability_fingerprint != derived_receipt.capability_fingerprint
        or descriptor.container != "mp4"
        or descriptor.warning_codes
        or video is None
        or audio is None
        or video.frame_rate.numerator != M26_SOURCE_VIDEO_FPS
        or video.frame_rate.denominator != 1
        or video.decoded_frame_count != derived_receipt.contribution_frames
        or audio.sample_rate != M26_SOURCE_AUDIO_SAMPLE_RATE
        or audio.channels != M26_SOURCE_AUDIO_CHANNELS
        or audio.channel_layout != "stereo"
        or audio.decoded_sample_count != derived_receipt.contribution_samples
    ):
        raise M26AssemblyError("derived_descriptor_mismatch")
    duration_ms = global_end_milliseconds - global_start_milliseconds
    if duration_ms <= 0 or duration_ms % 1000:
        raise M26AssemblyError("source_contribution_non_integer_duration")
    seconds = duration_ms // 1000
    target_fps, target_rate = m26_output_rates(output_profile_id)
    return SegmentSourceContributionV1(
        segment_id=derived_receipt.segment_id,
        disposition=disposition,
        derived_input_receipt_fingerprint=derived_receipt.fingerprint,
        media_descriptor_fingerprint=descriptor_fingerprint(descriptor),
        global_start_milliseconds=global_start_milliseconds,
        global_end_milliseconds=global_end_milliseconds,
        requested_seconds=seconds,
        contribution_frames=derived_receipt.contribution_frames,
        contribution_samples=derived_receipt.contribution_samples,
        trimmed_tail_frames=derived_receipt.trimmed_tail_frames,
        trimmed_tail_samples=derived_receipt.trimmed_tail_samples,
        target_frames=seconds * target_fps,
        target_samples=seconds * target_rate,
        normalization_generated_frames=seconds * (target_fps - M26_SOURCE_VIDEO_FPS),
        normalization_inserted_samples=seconds * (target_rate - M26_SOURCE_AUDIO_SAMPLE_RATE),
        target_video_fps=target_fps,
        target_audio_sample_rate=target_rate,
        operations=m26_output_operations(output_profile_id),
    )


@dataclass(frozen=True, slots=True)
class M26DerivedAVInputPlanV1:
    authorization: M26AssemblyAuthorizationV1 = field(repr=False)
    original_artifact_receipt_fingerprints: tuple[str, ...]
    derived_input_receipts: tuple[DerivedAVInputReceiptV1, ...]
    source_contributions: tuple[SegmentSourceContributionV1, ...]
    embedded_plan: AVReconstructionPlan = field(repr=False)
    embedded_approval: AVReconstructionApproval = field(repr=False)
    created_at_ms: int = 0
    plan_fingerprint: str | None = None
    schema: str = M26_DERIVED_AV_INPUT_PLAN_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != M26_DERIVED_AV_INPUT_PLAN_SCHEMA:
            raise M26AssemblyError("derived_plan_schema")
        if (
            type(self.authorization) is not M26AssemblyAuthorizationV1
            or type(self.embedded_plan) is not AVReconstructionPlan
            or type(self.embedded_approval) is not AVReconstructionApproval
        ):
            raise M26AssemblyError("derived_plan_authority_type")
        originals = _fingerprints(
            self.original_artifact_receipt_fingerprints,
            "derived_plan.original_artifacts",
        )
        if (
            type(self.derived_input_receipts) is not tuple
            or type(self.source_contributions) is not tuple
            or not all(
                type(item) is DerivedAVInputReceiptV1 for item in self.derived_input_receipts
            )
            or not all(
                type(item) is SegmentSourceContributionV1 for item in self.source_contributions
            )
            or len(originals) != len(self.derived_input_receipts)
            or len(originals) != len(self.source_contributions)
        ):
            raise M26AssemblyError("derived_plan_predecessor_count")
        segment_ids = tuple(item.segment_id for item in self.derived_input_receipts)
        if (
            segment_ids != tuple(item.segment_id for item in self.source_contributions)
            or originals != self.authorization.artifact_receipt_fingerprints
            or tuple(item.fingerprint for item in self.derived_input_receipts)
            != tuple(item.derived_input_receipt_fingerprint for item in self.source_contributions)
            or self.embedded_plan.artifact_receipt_fingerprints
            != tuple(item.fingerprint for item in self.derived_input_receipts)
            or tuple(item.segment_id for item in self.embedded_plan.segments) != segment_ids
            or self.embedded_approval.plan_fingerprint != self.embedded_plan.fingerprint
        ):
            raise M26AssemblyError("derived_plan_predecessor_mismatch")
        try:
            self.embedded_approval.assert_executable(
                self.embedded_plan,
                now_ms=self.embedded_approval.approved_at_ms,
            )
        except Exception as exc:
            raise M26AssemblyError("derived_plan_approval_mismatch") from exc
        _positive(self.created_at_ms, "derived_plan.created_at")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.plan_fingerprint is None:
            object.__setattr__(self, "plan_fingerprint", expected)
        elif self.plan_fingerprint != expected:
            raise M26AssemblyError("derived_plan_fingerprint_mismatch")
        if len(canonical_bytes(self.to_wire())) > MAX_M26_WIRE_BYTES:
            raise M26AssemblyError("derived_plan_wire_limit")

    @property
    def fingerprint(self) -> str:
        if self.plan_fingerprint is None:  # pragma: no cover
            raise M26AssemblyError("derived_plan_fingerprint_uninitialized")
        return self.plan_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "authorization_fingerprint": self.authorization.fingerprint,
            "original_artifact_receipt_fingerprints": list(
                self.original_artifact_receipt_fingerprints
            ),
            "derived_input_receipts": [item.to_wire() for item in self.derived_input_receipts],
            "source_contributions": [item.to_wire() for item in self.source_contributions],
            "embedded_plan_fingerprint": self.embedded_plan.fingerprint,
            "embedded_approval_fingerprint": self.embedded_approval.fingerprint,
            "created_at_ms": self.created_at_ms,
        }

    def to_wire(self) -> dict[str, object]:
        return _fingerprinted_wire(self, "plan_fingerprint")


@dataclass(frozen=True, slots=True)
class ProductionAssemblyReceiptV1:
    transaction_id: str
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    authorization_fingerprint: str
    original_artifact_receipt_fingerprints: tuple[str, ...]
    derived_input_receipt_fingerprints: tuple[str, ...]
    source_contribution_fingerprints: tuple[str, ...]
    outer_plan_fingerprint: str
    embedded_plan_fingerprint: str
    embedded_approval_fingerprint: str
    reconstruction_receipt_fingerprint: str
    source_total_frames: int
    source_total_samples: int
    output_total_frames: int
    output_total_samples: int
    completed_at_ms: int
    receipt_fingerprint: str | None = None
    schema: str = PRODUCTION_ASSEMBLY_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PRODUCTION_ASSEMBLY_RECEIPT_SCHEMA:
            raise M26AssemblyError("assembly_receipt_schema")
        _identifier(self.transaction_id, "assembly_receipt.transaction_id")
        _identifier(self.workspace_id, "assembly_receipt.workspace_id")
        _positive(self.workspace_revision, "assembly_receipt.workspace_revision", 1_000_000)
        for value, field_name in (
            (self.workspace_fingerprint, "assembly_receipt.workspace"),
            (self.authorization_fingerprint, "assembly_receipt.authorization"),
            (self.outer_plan_fingerprint, "assembly_receipt.outer_plan"),
            (self.embedded_plan_fingerprint, "assembly_receipt.embedded_plan"),
            (self.embedded_approval_fingerprint, "assembly_receipt.embedded_approval"),
            (self.reconstruction_receipt_fingerprint, "assembly_receipt.reconstruction"),
        ):
            _fingerprint(value, field_name)
        originals = _fingerprints(
            self.original_artifact_receipt_fingerprints,
            "assembly_receipt.original_artifacts",
        )
        derived = _fingerprints(
            self.derived_input_receipt_fingerprints,
            "assembly_receipt.derived_inputs",
        )
        contributions = _fingerprints(
            self.source_contribution_fingerprints,
            "assembly_receipt.contributions",
        )
        if len(originals) != len(derived) or len(derived) != len(contributions):
            raise M26AssemblyError("assembly_receipt_predecessor_count")
        for count, count_field_name in (
            (self.source_total_frames, "assembly_receipt.source_frames"),
            (self.source_total_samples, "assembly_receipt.source_samples"),
            (self.output_total_frames, "assembly_receipt.output_frames"),
            (self.output_total_samples, "assembly_receipt.output_samples"),
        ):
            _positive(count, count_field_name, 1_000_000_000)
        # IMPORTANT: epoch milliseconds exceed the media-count ceiling; sharing that bound
        # rejects normal host completions after reconstruction has already succeeded.
        _positive(self.completed_at_ms, "assembly_receipt.completed_at")
        if (
            self.source_total_frames * M26_TARGET_VIDEO_FPS
            != self.output_total_frames * M26_SOURCE_VIDEO_FPS
            or self.source_total_samples * M26_TARGET_AUDIO_SAMPLE_RATE
            != self.output_total_samples * M26_SOURCE_AUDIO_SAMPLE_RATE
        ):
            raise M26AssemblyError("assembly_receipt_rate_accounting")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.receipt_fingerprint is None:
            object.__setattr__(self, "receipt_fingerprint", expected)
        elif self.receipt_fingerprint != expected:
            raise M26AssemblyError("assembly_receipt_fingerprint_mismatch")
        if len(canonical_bytes(self.to_wire())) > MAX_M26_WIRE_BYTES:
            raise M26AssemblyError("assembly_receipt_wire_limit")

    @property
    def fingerprint(self) -> str:
        if self.receipt_fingerprint is None:  # pragma: no cover
            raise M26AssemblyError("assembly_receipt_fingerprint_uninitialized")
        return self.receipt_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "transaction_id": self.transaction_id,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "authorization_fingerprint": self.authorization_fingerprint,
            "original_artifact_receipt_fingerprints": list(
                self.original_artifact_receipt_fingerprints
            ),
            "derived_input_receipt_fingerprints": list(self.derived_input_receipt_fingerprints),
            "source_contribution_fingerprints": list(self.source_contribution_fingerprints),
            "outer_plan_fingerprint": self.outer_plan_fingerprint,
            "embedded_plan_fingerprint": self.embedded_plan_fingerprint,
            "embedded_approval_fingerprint": self.embedded_approval_fingerprint,
            "reconstruction_receipt_fingerprint": self.reconstruction_receipt_fingerprint,
            "source_total_frames": self.source_total_frames,
            "source_total_samples": self.source_total_samples,
            "output_total_frames": self.output_total_frames,
            "output_total_samples": self.output_total_samples,
            "completed_at_ms": self.completed_at_ms,
        }

    def to_wire(self) -> dict[str, object]:
        return _fingerprinted_wire(self, "receipt_fingerprint")

    def to_wire_bytes(self) -> bytes:
        return canonical_bytes(self.to_wire())


_ASSEMBLY_RECEIPT_FIELDS = {
    "schema",
    "transaction_id",
    "workspace_id",
    "workspace_revision",
    "workspace_fingerprint",
    "authorization_fingerprint",
    "original_artifact_receipt_fingerprints",
    "derived_input_receipt_fingerprints",
    "source_contribution_fingerprints",
    "outer_plan_fingerprint",
    "embedded_plan_fingerprint",
    "embedded_approval_fingerprint",
    "reconstruction_receipt_fingerprint",
    "source_total_frames",
    "source_total_samples",
    "output_total_frames",
    "output_total_samples",
    "completed_at_ms",
    "receipt_fingerprint",
}


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise M26AssemblyError("duplicate_assembly_receipt_member")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> object:
    raise M26AssemblyError("nonfinite_assembly_receipt_value")


def decode_production_assembly_receipt(
    payload: str | bytes | bytearray,
) -> ProductionAssemblyReceiptV1:
    """Decode one canonical portable receipt without recovering private plan authority."""

    if type(payload) is str:
        try:
            encoded = payload.encode("utf-8", errors="strict")
        except UnicodeError:
            raise M26AssemblyError("assembly_receipt_utf8") from None
        text = payload
    elif isinstance(payload, (bytes, bytearray)):
        encoded = bytes(payload)
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeError:
            raise M26AssemblyError("assembly_receipt_utf8") from None
    else:
        raise M26AssemblyError("assembly_receipt_payload_type")
    if not encoded or len(encoded) > MAX_M26_WIRE_BYTES:
        raise M26AssemblyError("assembly_receipt_wire_limit")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite,
        )
    except M26AssemblyError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise M26AssemblyError("assembly_receipt_json") from None
    if type(value) is not dict or set(value) != _ASSEMBLY_RECEIPT_FIELDS:
        raise M26AssemblyError("assembly_receipt_members")
    typed = cast(dict[str, object], value)
    arrays = (
        typed["original_artifact_receipt_fingerprints"],
        typed["derived_input_receipt_fingerprints"],
        typed["source_contribution_fingerprints"],
    )
    if any(type(item) is not list for item in arrays):
        raise M26AssemblyError("assembly_receipt_fingerprint_arrays")
    receipt = ProductionAssemblyReceiptV1(
        transaction_id=typed["transaction_id"],  # type: ignore[arg-type]
        workspace_id=typed["workspace_id"],  # type: ignore[arg-type]
        workspace_revision=typed["workspace_revision"],  # type: ignore[arg-type]
        workspace_fingerprint=typed["workspace_fingerprint"],  # type: ignore[arg-type]
        authorization_fingerprint=typed["authorization_fingerprint"],  # type: ignore[arg-type]
        original_artifact_receipt_fingerprints=tuple(cast(list[str], arrays[0])),
        derived_input_receipt_fingerprints=tuple(cast(list[str], arrays[1])),
        source_contribution_fingerprints=tuple(cast(list[str], arrays[2])),
        outer_plan_fingerprint=typed["outer_plan_fingerprint"],  # type: ignore[arg-type]
        embedded_plan_fingerprint=typed["embedded_plan_fingerprint"],  # type: ignore[arg-type]
        embedded_approval_fingerprint=typed["embedded_approval_fingerprint"],  # type: ignore[arg-type]
        reconstruction_receipt_fingerprint=typed["reconstruction_receipt_fingerprint"],  # type: ignore[arg-type]
        source_total_frames=typed["source_total_frames"],  # type: ignore[arg-type]
        source_total_samples=typed["source_total_samples"],  # type: ignore[arg-type]
        output_total_frames=typed["output_total_frames"],  # type: ignore[arg-type]
        output_total_samples=typed["output_total_samples"],  # type: ignore[arg-type]
        completed_at_ms=typed["completed_at_ms"],  # type: ignore[arg-type]
        receipt_fingerprint=typed["receipt_fingerprint"],  # type: ignore[arg-type]
        schema=typed["schema"],  # type: ignore[arg-type]
    )
    if encoded != receipt.to_wire_bytes():
        raise M26AssemblyError("assembly_receipt_not_canonical")
    return receipt


def _closed_chain_totals(
    outer_plan: M26DerivedAVInputPlanV1,
    reconstruction_receipt: AVReconstructionReceipt,
) -> tuple[int, int, int, int]:
    """Verify the original/derived/embedded chain; return source and output frame/sample totals."""

    if (
        type(outer_plan) is not M26DerivedAVInputPlanV1
        or type(reconstruction_receipt) is not AVReconstructionReceipt
    ):
        raise M26AssemblyError("assembly_receipt_type")
    if (
        reconstruction_receipt.plan_fingerprint != outer_plan.embedded_plan.fingerprint
        or reconstruction_receipt.approval_fingerprint != outer_plan.embedded_approval.fingerprint
        or reconstruction_receipt.selective_plan_fingerprint
        != outer_plan.embedded_plan.selective_plan_fingerprint
        or reconstruction_receipt.generation_state_fingerprint
        != outer_plan.embedded_plan.generation_state_fingerprint
        or reconstruction_receipt.artifact_receipt_fingerprints
        != tuple(item.fingerprint for item in outer_plan.derived_input_receipts)
        or reconstruction_receipt.boundary_receipt_fingerprints
        != outer_plan.embedded_plan.boundary_receipt_fingerprints
        or reconstruction_receipt.capability_fingerprint
        != outer_plan.embedded_plan.capability.fingerprint
        or tuple(item.segment_id for item in reconstruction_receipt.segment_results)
        != tuple(item.segment_id for item in outer_plan.source_contributions)
    ):
        raise M26AssemblyError("assembly_receipt_reconstruction_mismatch")
    for result, contribution in zip(
        reconstruction_receipt.segment_results,
        outer_plan.source_contributions,
        strict=True,
    ):
        if (
            tuple(item.value for item in result.operations) != contribution.operations
            or result.accounting.decoded_frames != contribution.contribution_frames
            or result.accounting.carried_frames != contribution.contribution_frames
            or result.accounting.dropped_frames != 0
            or result.accounting.generated_frames != contribution.normalization_generated_frames
            or result.accounting.emitted_frames != contribution.target_frames
            or result.accounting.decoded_samples != contribution.contribution_samples
            or result.accounting.carried_samples != contribution.contribution_samples
            or result.accounting.dropped_samples != 0
            or result.accounting.inserted_samples != contribution.normalization_inserted_samples
            or result.accounting.emitted_samples != contribution.target_samples
        ):
            raise M26AssemblyError("assembly_receipt_normalization_mismatch")
    source_frames = sum(item.contribution_frames for item in outer_plan.source_contributions)
    source_samples = sum(item.contribution_samples for item in outer_plan.source_contributions)
    output_frames = sum(item.target_frames for item in outer_plan.source_contributions)
    output_samples = sum(item.target_samples for item in outer_plan.source_contributions)
    full_output = next(
        (
            item
            for item in reconstruction_receipt.outputs
            if item.kind.value == "reconstruction_full"
        ),
        None,
    )
    if (
        full_output is None
        or full_output.video_frame_count != output_frames
        or full_output.audio_sample_count != output_samples
    ):
        raise M26AssemblyError("assembly_receipt_final_duration_mismatch")
    return source_frames, source_samples, output_frames, output_samples


def _contribution_profile_id(outer_plan: M26DerivedAVInputPlanV1) -> str:
    profiles = {
        _contribution_profile(item.target_video_fps, item.target_audio_sample_rate)
        for item in outer_plan.source_contributions
    }
    if len(profiles) != 1 or outer_plan.authorization.output_profile_id not in profiles:
        raise M26AssemblyError("assembly_receipt_profile_mismatch")
    return outer_plan.authorization.output_profile_id


def build_production_assembly_receipt(
    *,
    transaction_id: str,
    outer_plan: M26DerivedAVInputPlanV1,
    reconstruction_receipt: AVReconstructionReceipt,
    completed_at_ms: int,
) -> ProductionAssemblyReceiptV1:
    """Close a legacy-profile chain as a V1 receipt (V1 has no profile member)."""

    if (
        type(outer_plan) is not M26DerivedAVInputPlanV1
        or type(reconstruction_receipt) is not AVReconstructionReceipt
    ):
        raise M26AssemblyError("assembly_receipt_type")
    if _contribution_profile_id(outer_plan) != M26_OUTPUT_PROFILE_ID:
        raise M26AssemblyError("assembly_receipt_profile_mismatch")
    source_frames, source_samples, output_frames, output_samples = _closed_chain_totals(
        outer_plan, reconstruction_receipt
    )
    authorization = outer_plan.authorization
    return ProductionAssemblyReceiptV1(
        transaction_id=transaction_id,
        workspace_id=authorization.workspace_id,
        workspace_revision=authorization.workspace_revision,
        workspace_fingerprint=authorization.workspace_fingerprint,
        authorization_fingerprint=authorization.fingerprint,
        original_artifact_receipt_fingerprints=(outer_plan.original_artifact_receipt_fingerprints),
        derived_input_receipt_fingerprints=tuple(
            item.fingerprint for item in outer_plan.derived_input_receipts
        ),
        source_contribution_fingerprints=tuple(
            item.fingerprint for item in outer_plan.source_contributions
        ),
        outer_plan_fingerprint=outer_plan.fingerprint,
        embedded_plan_fingerprint=outer_plan.embedded_plan.fingerprint,
        embedded_approval_fingerprint=outer_plan.embedded_approval.fingerprint,
        reconstruction_receipt_fingerprint=reconstruction_receipt.fingerprint,
        source_total_frames=source_frames,
        source_total_samples=source_samples,
        output_total_frames=output_frames,
        output_total_samples=output_samples,
        completed_at_ms=completed_at_ms,
    )


@dataclass(frozen=True, slots=True)
class SeamMeasurementV1:
    """Integer seam metrics: S1 x1000, S2 x1e6, S5 and S6 x1000, S9 x100 dB, S10 in ms."""

    s1_milli: int
    s2_micro: int
    s5_milli: int
    s6_milli: int
    s9_centi_db: int
    s10_ms: int

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.s1_milli, "seam_measurement.s1"),
            (self.s2_micro, "seam_measurement.s2"),
            (self.s5_milli, "seam_measurement.s5"),
            (self.s6_milli, "seam_measurement.s6"),
            (self.s9_centi_db, "seam_measurement.s9"),
            (self.s10_ms, "seam_measurement.s10"),
        ):
            _nonnegative(value, field_name, 1_000_000_000)

    @property
    def smooth(self) -> bool:
        return (
            self.s1_milli < SEAM_SMOOTH_S1_MILLI_BELOW
            and self.s2_micro <= SEAM_SMOOTH_S2_MICRO_AT_MOST
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "s1_milli": self.s1_milli,
            "s2_micro": self.s2_micro,
            "s5_milli": self.s5_milli,
            "s6_milli": self.s6_milli,
            "s9_centi_db": self.s9_centi_db,
            "s10_ms": self.s10_ms,
        }


_SEAM_MEASUREMENT_FIELDS = frozenset(
    {"s1_milli", "s2_micro", "s5_milli", "s6_milli", "s9_centi_db", "s10_ms"}
)


@dataclass(frozen=True, slots=True)
class ProductionSeamObservationV1:
    """One seam line: where it falls, how it was joined, and its label or why it is unmeasured."""

    boundary_index: int
    predecessor_segment_id: str
    successor_segment_id: str
    output_frame: int
    measurement: SeamMeasurementV1 | None
    unmeasured_reason: str | None
    boundary_policy: str = "cut"

    def __post_init__(self) -> None:
        _nonnegative(self.boundary_index, "seam.boundary_index", MAX_M26_SEGMENTS - 2)
        _identifier(self.predecessor_segment_id, "seam.predecessor")
        _identifier(self.successor_segment_id, "seam.successor")
        _positive(self.output_frame, "seam.output_frame", 1_000_000_000)
        if (
            self.boundary_policy != "cut"
            or self.predecessor_segment_id == self.successor_segment_id
        ):
            raise M26AssemblyError("seam_boundary_policy")
        # IMPORTANT: a seam is either measured with every metric or carries a closed reason; a
        # label is never inferred for a seam whose decode or metric failed.
        if self.measurement is None:
            if self.unmeasured_reason not in SEAM_UNMEASURED_REASONS:
                raise M26AssemblyError("seam_measurement")
        elif type(self.measurement) is not SeamMeasurementV1 or self.unmeasured_reason is not None:
            raise M26AssemblyError("seam_measurement")

    @property
    def label(self) -> str:
        if self.measurement is None:
            return "unmeasured"
        return "smooth" if self.measurement.smooth else "visible_change"

    def to_wire(self) -> dict[str, object]:
        return {
            "boundary_index": self.boundary_index,
            "predecessor_segment_id": self.predecessor_segment_id,
            "successor_segment_id": self.successor_segment_id,
            "boundary_policy": self.boundary_policy,
            "output_frame": self.output_frame,
            "label": self.label,
            "measurement": None if self.measurement is None else self.measurement.to_wire(),
            "unmeasured_reason": self.unmeasured_reason,
        }


_SEAM_OBSERVATION_FIELDS = frozenset(
    {
        "boundary_index",
        "predecessor_segment_id",
        "successor_segment_id",
        "boundary_policy",
        "output_frame",
        "label",
        "measurement",
        "unmeasured_reason",
    }
)


@dataclass(frozen=True, slots=True)
class ProductionSeamReportV1:
    """Per-seam observations of one completed assembly, bound to its reconstruction receipt."""

    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    reconstruction_receipt_fingerprint: str
    output_profile_id: str
    seams: tuple[ProductionSeamObservationV1, ...]
    smooth_s1_milli_below: int = SEAM_SMOOTH_S1_MILLI_BELOW
    smooth_s2_micro_at_most: int = SEAM_SMOOTH_S2_MICRO_AT_MOST
    report_fingerprint: str | None = None
    schema: str = PRODUCTION_SEAM_REPORT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PRODUCTION_SEAM_REPORT_SCHEMA:
            raise M26AssemblyError("seam_report_schema")
        _identifier(self.workspace_id, "seam_report.workspace_id")
        _positive(self.workspace_revision, "seam_report.workspace_revision", 1_000_000)
        _fingerprint(self.workspace_fingerprint, "seam_report.workspace")
        _fingerprint(self.reconstruction_receipt_fingerprint, "seam_report.reconstruction")
        if (
            self.output_profile_id not in M26_OUTPUT_PROFILE_IDS
            or self.smooth_s1_milli_below != SEAM_SMOOTH_S1_MILLI_BELOW
            or self.smooth_s2_micro_at_most != SEAM_SMOOTH_S2_MICRO_AT_MOST
        ):
            raise M26AssemblyError("seam_report_policy")
        if (
            type(self.seams) is not tuple
            or len(self.seams) > MAX_M26_SEGMENTS - 1
            or not all(type(item) is ProductionSeamObservationV1 for item in self.seams)
            or tuple(item.boundary_index for item in self.seams) != tuple(range(len(self.seams)))
            or any(
                left.output_frame >= right.output_frame
                or left.successor_segment_id != right.predecessor_segment_id
                for left, right in zip(self.seams, self.seams[1:], strict=False)
            )
        ):
            raise M26AssemblyError("seam_report_order")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.report_fingerprint is None:
            object.__setattr__(self, "report_fingerprint", expected)
        elif self.report_fingerprint != expected:
            raise M26AssemblyError("seam_report_fingerprint_mismatch")
        if len(canonical_bytes(self.to_wire())) > MAX_M26_WIRE_BYTES:
            raise M26AssemblyError("seam_report_wire_limit")

    @property
    def fingerprint(self) -> str:
        if self.report_fingerprint is None:  # pragma: no cover
            raise M26AssemblyError("seam_report_fingerprint_uninitialized")
        return self.report_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "reconstruction_receipt_fingerprint": self.reconstruction_receipt_fingerprint,
            "output_profile_id": self.output_profile_id,
            "smooth_s1_milli_below": self.smooth_s1_milli_below,
            "smooth_s2_micro_at_most": self.smooth_s2_micro_at_most,
            "seams": [item.to_wire() for item in self.seams],
        }

    def to_wire(self) -> dict[str, object]:
        return _fingerprinted_wire(self, "report_fingerprint")

    def to_wire_bytes(self) -> bytes:
        return canonical_bytes(self.to_wire())


_SEAM_REPORT_FIELDS = frozenset(
    {
        "schema",
        "workspace_id",
        "workspace_revision",
        "workspace_fingerprint",
        "reconstruction_receipt_fingerprint",
        "output_profile_id",
        "smooth_s1_milli_below",
        "smooth_s2_micro_at_most",
        "seams",
        "report_fingerprint",
    }
)


def _load_canonical_json(payload: str | bytes | bytearray, prefix: str) -> tuple[bytes, object]:
    if type(payload) is str:
        try:
            encoded = payload.encode("utf-8", errors="strict")
        except UnicodeError:
            raise M26AssemblyError(f"{prefix}_utf8") from None
        text = payload
    elif isinstance(payload, (bytes, bytearray)):
        encoded = bytes(payload)
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeError:
            raise M26AssemblyError(f"{prefix}_utf8") from None
    else:
        raise M26AssemblyError(f"{prefix}_payload_type")
    if not encoded or len(encoded) > MAX_M26_WIRE_BYTES:
        raise M26AssemblyError(f"{prefix}_wire_limit")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite,
        )
    except M26AssemblyError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise M26AssemblyError(f"{prefix}_json") from None
    return encoded, value


def _decode_seam(value: object) -> ProductionSeamObservationV1:
    if type(value) is not dict or set(value) != _SEAM_OBSERVATION_FIELDS:
        raise M26AssemblyError("seam_report_members")
    typed = cast(dict[str, object], value)
    raw_measurement = typed["measurement"]
    measurement: SeamMeasurementV1 | None = None
    if raw_measurement is not None:
        if type(raw_measurement) is not dict or set(raw_measurement) != _SEAM_MEASUREMENT_FIELDS:
            raise M26AssemblyError("seam_report_members")
        numbers = cast(dict[str, object], raw_measurement)
        measurement = SeamMeasurementV1(
            s1_milli=numbers["s1_milli"],  # type: ignore[arg-type]
            s2_micro=numbers["s2_micro"],  # type: ignore[arg-type]
            s5_milli=numbers["s5_milli"],  # type: ignore[arg-type]
            s6_milli=numbers["s6_milli"],  # type: ignore[arg-type]
            s9_centi_db=numbers["s9_centi_db"],  # type: ignore[arg-type]
            s10_ms=numbers["s10_ms"],  # type: ignore[arg-type]
        )
    seam = ProductionSeamObservationV1(
        boundary_index=typed["boundary_index"],  # type: ignore[arg-type]
        predecessor_segment_id=typed["predecessor_segment_id"],  # type: ignore[arg-type]
        successor_segment_id=typed["successor_segment_id"],  # type: ignore[arg-type]
        output_frame=typed["output_frame"],  # type: ignore[arg-type]
        measurement=measurement,
        unmeasured_reason=typed["unmeasured_reason"],  # type: ignore[arg-type]
        boundary_policy=typed["boundary_policy"],  # type: ignore[arg-type]
    )
    if typed["label"] != seam.label:
        raise M26AssemblyError("seam_report_label")
    return seam


def decode_production_seam_report(payload: str | bytes | bytearray) -> ProductionSeamReportV1:
    """Decode one canonical seam report; the label of every seam must follow from its numbers."""

    encoded, value = _load_canonical_json(payload, "seam_report")
    if type(value) is not dict or set(value) != _SEAM_REPORT_FIELDS:
        raise M26AssemblyError("seam_report_members")
    typed = cast(dict[str, object], value)
    if type(typed["seams"]) is not list:
        raise M26AssemblyError("seam_report_members")
    report = ProductionSeamReportV1(
        workspace_id=typed["workspace_id"],  # type: ignore[arg-type]
        workspace_revision=typed["workspace_revision"],  # type: ignore[arg-type]
        workspace_fingerprint=typed["workspace_fingerprint"],  # type: ignore[arg-type]
        reconstruction_receipt_fingerprint=typed["reconstruction_receipt_fingerprint"],  # type: ignore[arg-type]
        output_profile_id=typed["output_profile_id"],  # type: ignore[arg-type]
        seams=tuple(_decode_seam(item) for item in cast(list[object], typed["seams"])),
        smooth_s1_milli_below=typed["smooth_s1_milli_below"],  # type: ignore[arg-type]
        smooth_s2_micro_at_most=typed["smooth_s2_micro_at_most"],  # type: ignore[arg-type]
        report_fingerprint=typed["report_fingerprint"],  # type: ignore[arg-type]
        schema=typed["schema"],  # type: ignore[arg-type]
    )
    if encoded != report.to_wire_bytes():
        raise M26AssemblyError("seam_report_not_canonical")
    return report


def build_production_seam_report(
    *,
    outer_plan: M26DerivedAVInputPlanV1,
    reconstruction_receipt: AVReconstructionReceipt,
    measurements: tuple[SeamMeasurementV1 | str, ...],
) -> ProductionSeamReportV1:
    """Place one measurement or closed reason per cut seam at its exact output frame."""

    if (
        type(outer_plan) is not M26DerivedAVInputPlanV1
        or type(reconstruction_receipt) is not AVReconstructionReceipt
        or type(measurements) is not tuple
        or len(measurements) != len(outer_plan.source_contributions) - 1
    ):
        raise M26AssemblyError("seam_report_type")
    _closed_chain_totals(outer_plan, reconstruction_receipt)
    authorization = outer_plan.authorization
    contributions = outer_plan.source_contributions
    seams: list[ProductionSeamObservationV1] = []
    cursor = 0
    for index, item in enumerate(measurements):
        cursor += contributions[index].target_frames
        seams.append(
            ProductionSeamObservationV1(
                boundary_index=index,
                predecessor_segment_id=contributions[index].segment_id,
                successor_segment_id=contributions[index + 1].segment_id,
                output_frame=cursor,
                measurement=item if type(item) is SeamMeasurementV1 else None,
                unmeasured_reason=None if type(item) is SeamMeasurementV1 else cast(str, item),
            )
        )
    return ProductionSeamReportV1(
        workspace_id=authorization.workspace_id,
        workspace_revision=authorization.workspace_revision,
        workspace_fingerprint=authorization.workspace_fingerprint,
        reconstruction_receipt_fingerprint=reconstruction_receipt.fingerprint,
        output_profile_id=_contribution_profile_id(outer_plan),
        seams=tuple(seams),
    )


@dataclass(frozen=True, slots=True)
class ProductionAssemblyReceiptV2:
    """V1's closed chain plus the output profile and the seam report it was measured with."""

    transaction_id: str
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    authorization_fingerprint: str
    original_artifact_receipt_fingerprints: tuple[str, ...]
    derived_input_receipt_fingerprints: tuple[str, ...]
    source_contribution_fingerprints: tuple[str, ...]
    outer_plan_fingerprint: str
    embedded_plan_fingerprint: str
    embedded_approval_fingerprint: str
    reconstruction_receipt_fingerprint: str
    output_profile_id: str
    seam_report_fingerprint: str
    source_total_frames: int
    source_total_samples: int
    output_total_frames: int
    output_total_samples: int
    completed_at_ms: int
    receipt_fingerprint: str | None = None
    schema: str = PRODUCTION_ASSEMBLY_RECEIPT_V2_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PRODUCTION_ASSEMBLY_RECEIPT_V2_SCHEMA:
            raise M26AssemblyError("assembly_receipt_schema")
        _identifier(self.transaction_id, "assembly_receipt.transaction_id")
        _identifier(self.workspace_id, "assembly_receipt.workspace_id")
        _positive(self.workspace_revision, "assembly_receipt.workspace_revision", 1_000_000)
        for value, field_name in (
            (self.workspace_fingerprint, "assembly_receipt.workspace"),
            (self.authorization_fingerprint, "assembly_receipt.authorization"),
            (self.outer_plan_fingerprint, "assembly_receipt.outer_plan"),
            (self.embedded_plan_fingerprint, "assembly_receipt.embedded_plan"),
            (self.embedded_approval_fingerprint, "assembly_receipt.embedded_approval"),
            (self.reconstruction_receipt_fingerprint, "assembly_receipt.reconstruction"),
            (self.seam_report_fingerprint, "assembly_receipt.seam_report"),
        ):
            _fingerprint(value, field_name)
        originals = _fingerprints(
            self.original_artifact_receipt_fingerprints,
            "assembly_receipt.original_artifacts",
        )
        derived = _fingerprints(
            self.derived_input_receipt_fingerprints,
            "assembly_receipt.derived_inputs",
        )
        contributions = _fingerprints(
            self.source_contribution_fingerprints,
            "assembly_receipt.contributions",
        )
        if len(originals) != len(derived) or len(derived) != len(contributions):
            raise M26AssemblyError("assembly_receipt_predecessor_count")
        for count, count_field_name in (
            (self.source_total_frames, "assembly_receipt.source_frames"),
            (self.source_total_samples, "assembly_receipt.source_samples"),
            (self.output_total_frames, "assembly_receipt.output_frames"),
            (self.output_total_samples, "assembly_receipt.output_samples"),
        ):
            _positive(count, count_field_name, 1_000_000_000)
        _positive(self.completed_at_ms, "assembly_receipt.completed_at")
        if self.output_profile_id not in M26_OUTPUT_PROFILE_IDS:
            raise M26AssemblyError("assembly_receipt_profile")
        target_fps, target_rate = m26_output_rates(self.output_profile_id)
        if (
            self.source_total_frames * target_fps != self.output_total_frames * M26_SOURCE_VIDEO_FPS
            or self.source_total_samples * target_rate
            != self.output_total_samples * M26_SOURCE_AUDIO_SAMPLE_RATE
        ):
            raise M26AssemblyError("assembly_receipt_rate_accounting")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.receipt_fingerprint is None:
            object.__setattr__(self, "receipt_fingerprint", expected)
        elif self.receipt_fingerprint != expected:
            raise M26AssemblyError("assembly_receipt_fingerprint_mismatch")
        if len(canonical_bytes(self.to_wire())) > MAX_M26_WIRE_BYTES:
            raise M26AssemblyError("assembly_receipt_wire_limit")

    @property
    def fingerprint(self) -> str:
        if self.receipt_fingerprint is None:  # pragma: no cover
            raise M26AssemblyError("assembly_receipt_fingerprint_uninitialized")
        return self.receipt_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "transaction_id": self.transaction_id,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "authorization_fingerprint": self.authorization_fingerprint,
            "original_artifact_receipt_fingerprints": list(
                self.original_artifact_receipt_fingerprints
            ),
            "derived_input_receipt_fingerprints": list(self.derived_input_receipt_fingerprints),
            "source_contribution_fingerprints": list(self.source_contribution_fingerprints),
            "outer_plan_fingerprint": self.outer_plan_fingerprint,
            "embedded_plan_fingerprint": self.embedded_plan_fingerprint,
            "embedded_approval_fingerprint": self.embedded_approval_fingerprint,
            "reconstruction_receipt_fingerprint": self.reconstruction_receipt_fingerprint,
            "output_profile_id": self.output_profile_id,
            "seam_report_fingerprint": self.seam_report_fingerprint,
            "source_total_frames": self.source_total_frames,
            "source_total_samples": self.source_total_samples,
            "output_total_frames": self.output_total_frames,
            "output_total_samples": self.output_total_samples,
            "completed_at_ms": self.completed_at_ms,
        }

    def to_wire(self) -> dict[str, object]:
        return _fingerprinted_wire(self, "receipt_fingerprint")

    def to_wire_bytes(self) -> bytes:
        return canonical_bytes(self.to_wire())


_ASSEMBLY_RECEIPT_V2_FIELDS = _ASSEMBLY_RECEIPT_FIELDS | {
    "output_profile_id",
    "seam_report_fingerprint",
}


def decode_production_assembly_receipt_v2(
    payload: str | bytes | bytearray,
) -> ProductionAssemblyReceiptV2:
    """Decode one canonical V2 receipt without recovering private plan authority."""

    encoded, value = _load_canonical_json(payload, "assembly_receipt")
    if type(value) is not dict or set(value) != _ASSEMBLY_RECEIPT_V2_FIELDS:
        raise M26AssemblyError("assembly_receipt_members")
    typed = cast(dict[str, object], value)
    arrays = (
        typed["original_artifact_receipt_fingerprints"],
        typed["derived_input_receipt_fingerprints"],
        typed["source_contribution_fingerprints"],
    )
    if any(type(item) is not list for item in arrays):
        raise M26AssemblyError("assembly_receipt_fingerprint_arrays")
    receipt = ProductionAssemblyReceiptV2(
        transaction_id=typed["transaction_id"],  # type: ignore[arg-type]
        workspace_id=typed["workspace_id"],  # type: ignore[arg-type]
        workspace_revision=typed["workspace_revision"],  # type: ignore[arg-type]
        workspace_fingerprint=typed["workspace_fingerprint"],  # type: ignore[arg-type]
        authorization_fingerprint=typed["authorization_fingerprint"],  # type: ignore[arg-type]
        original_artifact_receipt_fingerprints=tuple(cast(list[str], arrays[0])),
        derived_input_receipt_fingerprints=tuple(cast(list[str], arrays[1])),
        source_contribution_fingerprints=tuple(cast(list[str], arrays[2])),
        outer_plan_fingerprint=typed["outer_plan_fingerprint"],  # type: ignore[arg-type]
        embedded_plan_fingerprint=typed["embedded_plan_fingerprint"],  # type: ignore[arg-type]
        embedded_approval_fingerprint=typed["embedded_approval_fingerprint"],  # type: ignore[arg-type]
        reconstruction_receipt_fingerprint=typed["reconstruction_receipt_fingerprint"],  # type: ignore[arg-type]
        output_profile_id=typed["output_profile_id"],  # type: ignore[arg-type]
        seam_report_fingerprint=typed["seam_report_fingerprint"],  # type: ignore[arg-type]
        source_total_frames=typed["source_total_frames"],  # type: ignore[arg-type]
        source_total_samples=typed["source_total_samples"],  # type: ignore[arg-type]
        output_total_frames=typed["output_total_frames"],  # type: ignore[arg-type]
        output_total_samples=typed["output_total_samples"],  # type: ignore[arg-type]
        completed_at_ms=typed["completed_at_ms"],  # type: ignore[arg-type]
        receipt_fingerprint=typed["receipt_fingerprint"],  # type: ignore[arg-type]
        schema=typed["schema"],  # type: ignore[arg-type]
    )
    if encoded != receipt.to_wire_bytes():
        raise M26AssemblyError("assembly_receipt_not_canonical")
    return receipt


def build_production_assembly_receipt_v2(
    *,
    transaction_id: str,
    outer_plan: M26DerivedAVInputPlanV1,
    reconstruction_receipt: AVReconstructionReceipt,
    seam_report: ProductionSeamReportV1,
    completed_at_ms: int,
) -> ProductionAssemblyReceiptV2:
    """Close either profile's chain and bind the seam report measured on its output."""

    if (
        type(outer_plan) is not M26DerivedAVInputPlanV1
        or type(reconstruction_receipt) is not AVReconstructionReceipt
        or type(seam_report) is not ProductionSeamReportV1
    ):
        raise M26AssemblyError("assembly_receipt_type")
    profile_id = _contribution_profile_id(outer_plan)
    source_frames, source_samples, output_frames, output_samples = _closed_chain_totals(
        outer_plan, reconstruction_receipt
    )
    authorization = outer_plan.authorization
    expected_report = build_production_seam_report(
        outer_plan=outer_plan,
        reconstruction_receipt=reconstruction_receipt,
        measurements=tuple(
            item.measurement if item.measurement is not None else cast(str, item.unmeasured_reason)
            for item in seam_report.seams
        ),
    )
    if expected_report != seam_report:
        raise M26AssemblyError("assembly_receipt_seam_report_mismatch")
    return ProductionAssemblyReceiptV2(
        transaction_id=transaction_id,
        workspace_id=authorization.workspace_id,
        workspace_revision=authorization.workspace_revision,
        workspace_fingerprint=authorization.workspace_fingerprint,
        authorization_fingerprint=authorization.fingerprint,
        original_artifact_receipt_fingerprints=outer_plan.original_artifact_receipt_fingerprints,
        derived_input_receipt_fingerprints=tuple(
            item.fingerprint for item in outer_plan.derived_input_receipts
        ),
        source_contribution_fingerprints=tuple(
            item.fingerprint for item in outer_plan.source_contributions
        ),
        outer_plan_fingerprint=outer_plan.fingerprint,
        embedded_plan_fingerprint=outer_plan.embedded_plan.fingerprint,
        embedded_approval_fingerprint=outer_plan.embedded_approval.fingerprint,
        reconstruction_receipt_fingerprint=reconstruction_receipt.fingerprint,
        output_profile_id=profile_id,
        seam_report_fingerprint=seam_report.fingerprint,
        source_total_frames=source_frames,
        source_total_samples=source_samples,
        output_total_frames=output_frames,
        output_total_samples=output_samples,
        completed_at_ms=completed_at_ms,
    )


__all__ = [
    "DERIVED_AV_INPUT_RECEIPT_SCHEMA",
    "M26_ASSEMBLY_AUTHORIZATION_SCHEMA",
    "M26_ASSEMBLY_MAX_WORKERS",
    "M26_CROP_POLICY_ID",
    "M26_DERIVED_AV_INPUT_PLAN_SCHEMA",
    "M26_NATIVE_OUTPUT_PROFILE_ID",
    "M26_OUTPUT_PROFILE_ID",
    "M26_OUTPUT_PROFILE_IDS",
    "M26_SOURCE_AUDIO_CHANNELS",
    "M26_SOURCE_AUDIO_SAMPLE_RATE",
    "M26_SOURCE_VIDEO_FPS",
    "M26_TARGET_AUDIO_CHANNELS",
    "M26_TARGET_AUDIO_SAMPLE_RATE",
    "M26_TARGET_VIDEO_FPS",
    "PRODUCTION_ASSEMBLY_CAPABILITY_SCHEMA",
    "PRODUCTION_ASSEMBLY_RECEIPT_SCHEMA",
    "PRODUCTION_ASSEMBLY_RECEIPT_V2_SCHEMA",
    "PRODUCTION_SEAM_REPORT_SCHEMA",
    "SEAM_LABELS",
    "SEAM_UNMEASURED_REASONS",
    "SEAM_SMOOTH_S1_MILLI_BELOW",
    "SEAM_SMOOTH_S2_MICRO_AT_MOST",
    "SEGMENT_SOURCE_CONTRIBUTION_SCHEMA",
    "DerivedAVInputReceiptV1",
    "M26AssemblyAuthorizationV1",
    "M26AssemblyError",
    "M26DerivedAVInputPlanV1",
    "ProductionAssemblyCapabilityV1",
    "ProductionAssemblyReceiptV1",
    "ProductionAssemblyReceiptV2",
    "ProductionSeamObservationV1",
    "ProductionSeamReportV1",
    "SegmentSourceContributionV1",
    "build_production_assembly_capability",
    "build_production_assembly_receipt",
    "build_production_assembly_receipt_v2",
    "build_production_seam_report",
    "build_source_contribution",
    "descriptor_fingerprint",
    "decode_production_assembly_receipt",
    "decode_production_assembly_receipt_v2",
    "decode_production_seam_report",
    "m26_output_operations",
    "m26_output_profile_fingerprint",
    "m26_output_rates",
    "mint_m26_assembly_authorization",
]
