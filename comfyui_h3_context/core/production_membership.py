"""Content-free facts for one executed member of an accumulating Production project.

A project accumulates independently executed segments. Each attempt keeps the exact workspace it
was executed against: its generation plan and artifact receipt stay bound to that workspace, so the
project can gain, reorder or select segments without relabelling or re-keying earlier receipts.
This pure module validates those joins only. Registry ownership, stores, leases and lineage remain
adapter obligations.
"""

from __future__ import annotations

from dataclasses import dataclass

from .canonical import canonical_bytes
from .generation_sequence import (
    GenerationJobRuntime,
    GenerationJobState,
    GenerationSequenceJob,
    GenerationSequenceProjection,
    artifact_receipt_matches_job_contract,
)
from .segment_artifacts import ArtifactLifecycleState, SegmentArtifactReceipt
from .segment_workspace import (
    MultiSegmentWorkspace,
    SegmentContextManifest,
    SegmentDeclaration,
    derive_segment_manifests,
)

PRODUCTION_MEMBER_ATTEMPT_SCHEMA = "h3.context.production_member_attempt.v1"

#: Job states after which no further host event can change the attempt.
TERMINAL_MEMBER_JOB_STATES = frozenset(
    {
        GenerationJobState.SUCCEEDED,
        GenerationJobState.FAILED,
        GenerationJobState.TIMED_OUT,
        GenerationJobState.CANCELLED,
        GenerationJobState.UNKNOWN_OWNERSHIP,
    }
)


class ProductionMembershipError(ValueError):
    """Raised when member facts do not join their own execution authority."""


@dataclass(frozen=True, slots=True)
class ProductionMemberAttemptV1:
    """One attempt of one project segment, joined to the workspace it actually executed against."""

    authority_workspace: MultiSegmentWorkspace
    segment_id: str
    generation_sequence: GenerationSequenceProjection
    artifact_receipt: SegmentArtifactReceipt | None = None
    schema: str = PRODUCTION_MEMBER_ATTEMPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PRODUCTION_MEMBER_ATTEMPT_SCHEMA:
            raise ProductionMembershipError("member_attempt_schema")
        workspace = self.authority_workspace
        if type(workspace) is not MultiSegmentWorkspace:
            raise ProductionMembershipError("member_attempt_workspace")
        if type(self.generation_sequence) is not GenerationSequenceProjection:
            raise ProductionMembershipError("member_attempt_sequence")
        if self.artifact_receipt is not None and type(self.artifact_receipt) is not (
            SegmentArtifactReceipt
        ):
            raise ProductionMembershipError("member_attempt_receipt")
        manifest = self._manifest()
        plan = self.generation_sequence.state.plan
        # CRITICAL: the plan must name this exact retained workspace object. Comparing only ids or
        # fingerprints would let a sequence minted for a foreign project describe this member.
        if (
            plan.source_workspace_authority is not workspace
            or plan.workspace_id != workspace.workspace_id
            or plan.workspace_revision != workspace.revision
            or plan.workspace_fingerprint != workspace.fingerprint
            or plan.manifest_fingerprints
            != tuple(item.fingerprint for item in derive_segment_manifests(workspace))
        ):
            raise ProductionMembershipError("member_attempt_plan_authority")
        jobs = tuple(item for item in plan.jobs if item.segment_id == self.segment_id)
        if len(jobs) != 1 or jobs[0].manifest_fingerprint != manifest.fingerprint:
            raise ProductionMembershipError("member_attempt_job")
        runtime = self.runtime
        receipt = self.artifact_receipt
        if receipt is not None:
            job = jobs[0]
            # IMPORTANT: a receipt is valid only against the manifest it was minted for. Never
            # accept a receipt re-keyed to a later project revision; that fabricates provenance.
            if (
                receipt.workspace_id != workspace.workspace_id
                or receipt.workspace_revision != workspace.revision
                or receipt.workspace_fingerprint != workspace.fingerprint
                or receipt.segment_id != self.segment_id
                or receipt.manifest_fingerprint != manifest.fingerprint
                or receipt.producer_fingerprint != manifest.producer_fingerprint
                or receipt.native_binding_fingerprint != manifest.native_binding_fingerprint
                or receipt.settings_fingerprint != manifest.producer_settings_fingerprint
                or receipt.source_id != manifest.source_id
                or receipt.model_fingerprint != job.model_fingerprint
                or receipt.runtime_fingerprint != job.runtime_fingerprint
                or not artifact_receipt_matches_job_contract(job, receipt)
            ):
                raise ProductionMembershipError("member_attempt_receipt_identity")
        if runtime.state is GenerationJobState.SUCCEEDED and (
            receipt is None
            or receipt.state is not ArtifactLifecycleState.COMPLETE
            or receipt.output_fingerprint is None
            or runtime.artifact_receipt_fingerprint != receipt.fingerprint
            or runtime.artifact_output_fingerprint != receipt.output_fingerprint
        ):
            raise ProductionMembershipError("member_attempt_completion")

    def _manifest(self) -> SegmentContextManifest:
        if type(self.segment_id) is not str:
            raise ProductionMembershipError("member_attempt_segment")
        matches = tuple(
            item
            for item in derive_segment_manifests(self.authority_workspace)
            if item.segment_id == self.segment_id
        )
        if len(matches) != 1:
            raise ProductionMembershipError("member_attempt_segment")
        return matches[0]

    @property
    def job(self) -> GenerationSequenceJob:
        return self.generation_sequence.state.plan.job_for_segment(self.segment_id)

    @property
    def runtime(self) -> GenerationJobRuntime:
        return self.generation_sequence.state.runtime_for(self.job.job_id)

    @property
    def declaration(self) -> SegmentDeclaration:
        return next(
            item for item in self.authority_workspace.segments if item.segment_id == self.segment_id
        )

    @property
    def job_state(self) -> GenerationJobState:
        return self.runtime.state

    @property
    def terminal(self) -> bool:
        return self.job_state in TERMINAL_MEMBER_JOB_STATES

    @property
    def verified(self) -> bool:
        """Whether this attempt delivered a verified original that can be retained as output."""

        receipt = self.artifact_receipt
        return (
            self.job_state is GenerationJobState.SUCCEEDED
            and receipt is not None
            and receipt.state is ArtifactLifecycleState.COMPLETE
            and receipt.output_fingerprint is not None
        )

    @property
    def closure_state(self) -> str:
        return self.job.disposition.value

    def retained_objects(self) -> tuple[object, ...]:
        """The distinct retained authority objects, for byte accounting by identity."""

        values: tuple[object, ...] = (self.authority_workspace, self.generation_sequence)
        return values if self.artifact_receipt is None else (*values, self.artifact_receipt)


def retained_object_bytes(value: object) -> int:
    """Canonical wire size of one retained member authority object."""

    if type(value) is MultiSegmentWorkspace:
        return len(value.to_wire_bytes())
    if type(value) is GenerationSequenceProjection:
        return (
            len(canonical_bytes(value.state.plan.to_wire()))
            + len(canonical_bytes(value.state.to_wire()))
            + len(canonical_bytes(value.to_wire()))
        )
    if type(value) is SegmentArtifactReceipt:
        return len(canonical_bytes(value.to_wire()))
    raise ProductionMembershipError("member_retained_object")


__all__ = [
    "PRODUCTION_MEMBER_ATTEMPT_SCHEMA",
    "TERMINAL_MEMBER_JOB_STATES",
    "ProductionMemberAttemptV1",
    "ProductionMembershipError",
    "retained_object_bytes",
]
