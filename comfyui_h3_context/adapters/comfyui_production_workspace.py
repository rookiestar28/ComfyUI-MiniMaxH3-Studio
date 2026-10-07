"""Bounded process-local Production workspace authority and strict ComfyUI route."""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from hashlib import sha256
from types import MappingProxyType
from typing import NoReturn, SupportsIndex, cast

from ..core.av_reconstruction import (
    AVOutputKind,
    AVPublicationState,
    AVReconstructionPlan,
    AVReconstructionReceipt,
)
from ..core.canonical import canonical_bytes, canonical_fingerprint
from ..core.continuity_handoff import ContinuityBoundaryReceipt, ContinuityMode
from ..core.durable_workspace_state import RecordIdentityMap, StateRecord, decode_record
from ..core.generation_sequence import (
    GENERATION_SEQUENCE_PROJECTION_SCHEMA,
    GenerationJobState,
    GenerationSequenceProjection,
)
from ..core.length import resolve_milliseconds
from ..core.m26_assembly import (
    M26_OUTPUT_PROFILE_ID,
    M26AssemblyAuthorizationV1,
    ProductionAssemblyReceiptV1,
    assembly_start_holds_satisfied,
    build_production_assembly_receipt,
    mint_m26_assembly_authorization,
)
from ..core.production_accumulation import (
    PRODUCTION_ACCUMULATION_ACTION_VERSION,
    ProductionAccumulatedProjectProjection,
    ProductionAccumulationAttemptProjection,
)
from ..core.production_import import (
    LegacyMaterializeSegmentContext,
    MaterializeSegmentContext,
    ProductionAutomaticPlanAuthorityV1,
    ProductionAutomaticPlanAuthorityV2,
    ProductionAutomaticPlanDispatchResultV1,
    ProductionImportError,
    ProductionImportRequestV1,
    SegmentContextMaterializationClaim,
    SegmentContextMaterializationReceiptV1,
    build_production_automatic_plan_authority,
    derive_legacy_segment_context_materialization_receipt,
    derive_segment_context_materialization_receipt,
    is_production_proposal_importable,
    project_production_automatic_plan,
)
from ..core.production_membership import (
    TERMINAL_MEMBER_JOB_STATES,
    ProductionMemberAttemptV1,
    ProductionMembershipError,
    retained_object_bytes,
)
from ..core.production_storyboard import (
    ProductionPlanningContextV1,
    ProductionPlanningContextV2,
    SegmentationProposalV1,
    SegmentationProposalV2,
    validate_current_segmentation_proposal,
)
from ..core.production_workbench import (
    MAX_PRODUCTION_OUTPUTS,
    ProductionAssemblyProjection,
    ProductionDeliveredVideoProjection,
    ProductionGenerationSequenceSummary,
    ProductionOutputProjection,
    ProductionSegmentProjection,
    ProductionWorkbenchProjection,
    build_production_workbench_projection,
)
from ..core.project_document import (
    ProjectDocument,
    decode_project_value,
    encode_project_document,
    project_bytes,
)
from ..core.segment_artifacts import ArtifactLifecycleState, SegmentArtifactReceipt
from ..core.segment_workspace import (
    MAX_WORKSPACE_SEGMENTS,
    AcceptedIntentAuthority,
    MultiSegmentWorkspace,
    SegmentDeclaration,
    SegmentRelationKind,
    SegmentWorkspaceError,
    create_workspace,
    derive_segment_manifests,
    revise_workspace,
)
from .av_reconstruction_media import QualifiedAVMediaAdapter
from .av_reconstruction_store import AVStoreError, PrivateAVReconstructionStore
from .comfyui_route_seam import (
    RoutePolicy,
    RouteResult,
    offload_route_handler,
    register_owned_route,
)
from .comfyui_sidebar_workspace import (
    SidebarAuthoringSeed,
    SidebarProductionSeed,
    claim_sidebar_production_seed,
)
from .composition_root import PRODUCTION_WORKSPACE, component
from .m26_production_assembly import (
    M26ProductionAssemblyError,
    M26ProductionAssemblyExecution,
    M26ProductionAssemblyRuntime,
    execute_m26_production_assembly,
)
from .media_preview_authority import (
    MAX_MEDIA_PREVIEW_AUTHORITY_BYTES,
    MAX_MEDIA_PREVIEW_AUTHORITY_SET_BYTES,
    MAX_MEDIA_PREVIEW_DURATION_MS,
    MAX_MEDIA_PREVIEW_SOURCE_BYTES,
    MediaPreviewSourceAuthority,
    build_generated_segment_media_preview_source_authority,
    build_media_preview_source_authority,
    build_segment_media_preview_source_authority,
)
from .segment_artifact_store import PrivateSegmentArtifactStore

PRODUCTION_ACTION_SCHEMA = "h3.context.production_workbench.action.v1"
PRODUCTION_ACTION_ROUTE = "/h3-context/v1/production/action"
MAX_PRODUCTION_ACTION_BYTES = 65_536
MAX_PRODUCTION_ACTION_DEPTH = 4
MAX_PRODUCTION_ACTION_NODES = 256
MAX_PRODUCTION_ACTION_ARRAY = 64
MAX_PRODUCTION_WORKSPACES = 16
PRODUCTION_WORKSPACE_TTL_SECONDS = 900
MAX_PRODUCTION_REQUEST_LEDGER = 256
MAX_PRODUCTION_TOMBSTONES = 256
PRODUCTION_TOMBSTONE_TTL_SECONDS = 900
PRODUCTION_STAGE_TTL_SECONDS = 900
MAX_PRODUCTION_AUTHORITY_BYTES = 4_194_304
MAX_PRODUCTION_REGISTRY_BYTES = 16_777_216
PRODUCTION_MUTATION_CLAIM_SECONDS = 0.250
PRODUCTION_GENERATED_PREVIEW_ADMISSION_SECONDS = 2.0
MAX_PRODUCTION_DESTINATION_ADMISSIONS = 16
MAX_PRODUCTION_ADMISSION_RECORDS = 256
PRODUCTION_DESTINATION_ADMISSION_TTL_SECONDS = 300
#: Authority bytes an admission reserves for the one attempt it may publish.
PRODUCTION_MEMBER_ATTEMPT_RESERVE_BYTES = 65_536

_ROOT_KEYS = {"schema", "request_id", "action", "payload"}
_ADMISSION_ACTIONS = {
    "admit_generation_destination",
    "release_generation_destination",
    "admit_generation_destination_v2",
    "release_generation_destination_v2",
    "settle_generation_destination_v2",
    "read_accumulated_project",
}
_ACTIONS = {
    "admit_generation_destination",
    "release_generation_destination",
    "admit_generation_destination_v2",
    "release_generation_destination_v2",
    "settle_generation_destination_v2",
    "read_accumulated_project",
    "create_workspace_from_context",
    "add_segment_from_context",
    "replace_segment_from_context",
    "set_segment_relation",
    "delete_segment",
    "reorder_segments",
    "set_selection",
    "read_projection",
    "release_workspace",
    "assemble_sequence",
    "cancel_assembly",
    "retry_assembly",
}
_MUTATIONS = _ACTIONS - {
    "create_workspace_from_context",
    "read_projection",
    "release_workspace",
    *_ADMISSION_ACTIONS,
}
_PAYLOAD_KEYS = {
    "admit_generation_destination": {"workspace_handle", "workspace_id", "segment_id"},
    "release_generation_destination": {"admission_request_id"},
    "admit_generation_destination_v2": {
        "version",
        "workspace_handle",
        "workspace_id",
        "segment_id",
    },
    "release_generation_destination_v2": {"version", "admission_request_id"},
    "settle_generation_destination_v2": {
        "version",
        "admission_request_id",
        "terminal",
    },
    "read_accumulated_project": {"version", "workspace_handle", "workspace_id"},
    "create_workspace_from_context": {"context_workspace_handle"},
    "add_segment_from_context": {
        "workspace_handle",
        "expected_workspace_revision",
        "expected_workspace_fingerprint",
        "context_workspace_handle",
        "relation",
        "predecessor_segment_id",
    },
    "replace_segment_from_context": {
        "workspace_handle",
        "expected_workspace_revision",
        "expected_workspace_fingerprint",
        "segment_id",
        "context_workspace_handle",
    },
    "set_segment_relation": {
        "workspace_handle",
        "expected_workspace_revision",
        "expected_workspace_fingerprint",
        "segment_id",
        "relation",
        "predecessor_segment_id",
    },
    "delete_segment": {
        "workspace_handle",
        "expected_workspace_revision",
        "expected_workspace_fingerprint",
        "segment_id",
    },
    "reorder_segments": {
        "workspace_handle",
        "expected_workspace_revision",
        "expected_workspace_fingerprint",
        "segment_ids",
    },
    "set_selection": {
        "workspace_handle",
        "expected_workspace_revision",
        "expected_workspace_fingerprint",
        "segment_ids",
    },
    "read_projection": {"workspace_handle"},
    "release_workspace": {
        "workspace_handle",
        "expected_workspace_revision",
        "expected_workspace_fingerprint",
    },
    "assemble_sequence": {
        "workspace_handle",
        "workspace_id",
        "expected_workspace_revision",
        "expected_workspace_fingerprint",
        "managed_sequence_fingerprint",
        "artifact_receipt_fingerprints",
        "cut_boundary_receipt_fingerprints",
        "assembly_capability_fingerprint",
        "output_profile_id",
    },
    "cancel_assembly": {
        "workspace_handle",
        "workspace_id",
        "expected_workspace_revision",
        "expected_workspace_fingerprint",
        "expected_assembly_fingerprint",
    },
    "retry_assembly": {
        "workspace_handle",
        "workspace_id",
        "expected_workspace_revision",
        "expected_workspace_fingerprint",
        "expected_assembly_fingerprint",
    },
}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SIDEBAR_HANDLE = re.compile(r"ws_[A-Za-z0-9_-]{32,96}\Z")
_WORKSPACE_HANDLE = re.compile(r"pw_[A-Za-z0-9_-]{32,96}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OPAQUE_OUTPUT_HANDLE = re.compile(r"out_[0-9a-f]{40}\Z")
_CONTINUITY_MODE_BY_RELATION = {
    SegmentRelationKind.INDEPENDENT: ContinuityMode.CUT,
    SegmentRelationKind.CUT: ContinuityMode.CUT,
    SegmentRelationKind.RESET: ContinuityMode.RESTART,
    SegmentRelationKind.PREDECESSOR: ContinuityMode.NATIVE_FRAME_HANDOFF,
    SegmentRelationKind.ADJACENT_PAIR: ContinuityMode.NATIVE_FRAME_HANDOFF,
}
_ROUTE_OWNER_ATTRIBUTE = "__h3_context_production_action_v1__"
_ROUTE_REGISTERED = False


class ProductionWorkbenchError(ValueError):
    """Closed internal failure with an HTTP disposition and optional safe projection."""

    def __init__(
        self,
        code: str,
        status: int,
        *,
        projection: (
            ProductionWorkbenchProjection | ProductionAccumulatedProjectProjection | None
        ) = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.projection = projection


@dataclass(frozen=True, slots=True)
class ProductionDispatchResult:
    status: int
    projection: ProductionWorkbenchProjection | ProductionAccumulatedProjectProjection | None = None
    code: str | None = None


@dataclass(frozen=True, slots=True)
class ProductionPreviewRowPublication:
    """One content-free output-row result from an atomic completion publication."""

    output_handle: str
    segment_id: str | None
    disposition: str

    def __post_init__(self) -> None:
        if type(self.output_handle) is not str or not _OPAQUE_OUTPUT_HANDLE.fullmatch(
            self.output_handle
        ):
            raise ValueError("preview_publication_output_handle")
        if self.segment_id is not None and (
            type(self.segment_id) is not str or not _IDENTIFIER.fullmatch(self.segment_id)
        ):
            raise ValueError("preview_publication_segment_id")
        if self.disposition not in {"published", "attached_without_preview"}:
            raise ValueError("preview_publication_disposition")


@dataclass(frozen=True, slots=True)
class ProductionPreviewPublication:
    """Content-free completion-publisher disposition."""

    disposition: str
    projection: ProductionWorkbenchProjection | None = None
    output_journal: tuple[ProductionPreviewRowPublication, ...] = ()

    def __post_init__(self) -> None:
        if type(self.output_journal) is not tuple or not all(
            type(item) is ProductionPreviewRowPublication for item in self.output_journal
        ):
            raise TypeError("preview_publication_journal")
        if len(self.output_journal) > MAX_PRODUCTION_OUTPUTS or len(
            {item.output_handle for item in self.output_journal}
        ) != len(self.output_journal):
            raise ValueError("preview_publication_journal")
        if self.output_journal:
            if self.projection is None or tuple(
                (item.output_handle, item.segment_id, item.preview)
                for item in self.projection.outputs
            ) != tuple(
                (
                    item.output_handle,
                    item.segment_id,
                    item.disposition == "published",
                )
                for item in self.output_journal
            ):
                raise ValueError("preview_publication_journal_mismatch")


@dataclass(frozen=True, slots=True)
class ProductionAcceptedAuthorities:
    """Exact typed predecessor authorities retained only by the process-local registry."""

    generation_sequence: GenerationSequenceProjection | None = None
    artifact_receipts: tuple[SegmentArtifactReceipt, ...] = ()
    continuity_receipts: tuple[ContinuityBoundaryReceipt, ...] = ()
    reconstruction_receipt: AVReconstructionReceipt | None = None
    assembly_execution: M26ProductionAssemblyExecution | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        if (
            self.generation_sequence is not None
            and type(self.generation_sequence) is not GenerationSequenceProjection
        ):
            raise TypeError("generation_sequence authority is invalid")
        if type(self.artifact_receipts) is not tuple or not all(
            type(item) is SegmentArtifactReceipt for item in self.artifact_receipts
        ):
            raise TypeError("artifact_receipts authority is invalid")
        if type(self.continuity_receipts) is not tuple or not all(
            type(item) is ContinuityBoundaryReceipt for item in self.continuity_receipts
        ):
            raise TypeError("continuity_receipts authority is invalid")
        if (
            self.reconstruction_receipt is not None
            and type(self.reconstruction_receipt) is not AVReconstructionReceipt
        ):
            raise TypeError("reconstruction_receipt authority is invalid")
        if (
            self.assembly_execution is not None
            and type(self.assembly_execution) is not M26ProductionAssemblyExecution
        ):
            raise TypeError("assembly execution authority is invalid")
        if self.reconstruction_receipt is not None and self.assembly_execution is not None:
            raise TypeError("duplicate reconstruction authority")

    def wire_bytes(self) -> int:
        values: list[dict[str, object]] = []
        if self.generation_sequence is not None:
            values.append(self.generation_sequence.state.plan.to_wire())
            values.append(self.generation_sequence.state.to_wire())
            values.append(self.generation_sequence.to_wire())
        values.extend(item.to_wire() for item in self.artifact_receipts)
        values.extend(item.to_wire() for item in self.continuity_receipts)
        if self.reconstruction_receipt is not None:
            values.append(self.reconstruction_receipt.to_wire())
        if self.assembly_execution is not None:
            execution = self.assembly_execution
            values.extend(
                (
                    execution.authorization.to_wire(),
                    execution.outer_plan.to_wire(),
                    execution.reconstruction_receipt.to_wire(),
                    execution.assembly_receipt.to_wire(),
                )
            )
        return sum(len(canonical_bytes(item)) for item in values)


_EMPTY_ACCEPTED_AUTHORITIES = ProductionAcceptedAuthorities()
_EMPTY_PREVIEW_SOURCES: Mapping[str, MediaPreviewSourceAuthority] = MappingProxyType({})


ProductionAutomaticPlanAuthority = (
    ProductionAutomaticPlanAuthorityV1 | ProductionAutomaticPlanAuthorityV2
)


def _automatic_plan_wire_bytes(authority: ProductionAutomaticPlanAuthority | None) -> int:
    if authority is None:
        return 0
    if type(authority) not in (
        ProductionAutomaticPlanAuthorityV1,
        ProductionAutomaticPlanAuthorityV2,
    ):
        raise TypeError("automatic plan authority is invalid")
    return len(authority.private_wire_bytes())


def _retained_authority_bytes(
    workspace: MultiSegmentWorkspace,
    accepted: ProductionAcceptedAuthorities,
    preview_sources: Mapping[str, MediaPreviewSourceAuthority],
    automatic_plan: ProductionAutomaticPlanAuthority | None,
    completed_assembly: ProductionAssemblyProjection | None = None,
    members: Mapping[str, _MemberRow] | None = None,
) -> int:
    return (
        len(workspace.to_wire_bytes())
        + accepted.wire_bytes()
        + _preview_sources_wire_bytes(preview_sources)
        + _automatic_plan_wire_bytes(automatic_plan)
        + (0 if completed_assembly is None else len(canonical_bytes(completed_assembly.to_wire())))
        + _member_rows_bytes(members)
    )


def _preview_sources_wire_bytes(
    preview_sources: Mapping[str, MediaPreviewSourceAuthority],
) -> int:
    if type(preview_sources) is not type(_EMPTY_PREVIEW_SOURCES):
        raise ValueError("preview_source_set_not_read_only")
    if len(preview_sources) > MAX_PRODUCTION_OUTPUTS:
        raise ValueError("preview_source_set_limit")
    total = 0
    for key, source in preview_sources.items():
        if (
            type(key) is not str
            or type(source) is not MediaPreviewSourceAuthority
            or key != source.opaque_output_handle
        ):
            raise ValueError("preview_source_set_mismatch")
        member_bytes = source.wire_bytes()
        if member_bytes > MAX_MEDIA_PREVIEW_AUTHORITY_BYTES:
            raise ValueError("preview_source_authority_limit")
        total += member_bytes
    if total > MAX_MEDIA_PREVIEW_AUTHORITY_SET_BYTES:
        raise ValueError("preview_source_set_capacity")
    return total


def _bounded_preview_sources(
    sources: tuple[MediaPreviewSourceAuthority, ...],
) -> Mapping[str, MediaPreviewSourceAuthority]:
    """Retain one deterministic authority prefix within the closed set budget."""

    if type(sources) is not tuple or not all(
        type(source) is MediaPreviewSourceAuthority for source in sources
    ):
        raise TypeError("preview_source_set_type")
    if len(sources) > MAX_PRODUCTION_OUTPUTS:
        raise ValueError("preview_source_set_limit")
    handles = tuple(source.opaque_output_handle for source in sources)
    if len(handles) != len(set(handles)):
        raise ValueError("preview_source_set_duplicate")
    retained: dict[str, MediaPreviewSourceAuthority] = {}
    total = 0
    for source in sources:
        member_bytes = source.wire_bytes()
        if member_bytes > MAX_MEDIA_PREVIEW_AUTHORITY_BYTES:
            raise ValueError("preview_source_authority_limit")
        # IMPORTANT: callers order aggregate first and then segment ordinal. Keep a prefix instead
        # of skipping an oversized member, or identical authority sets could admit different rows.
        if total + member_bytes > MAX_MEDIA_PREVIEW_AUTHORITY_SET_BYTES:
            break
        retained[source.opaque_output_handle] = source
        total += member_bytes
    return MappingProxyType(retained)


def _try_preview_source(
    *,
    plan: AVReconstructionPlan,
    receipt: AVReconstructionReceipt,
    store: PrivateAVReconstructionStore,
    adapter: QualifiedAVMediaAdapter,
    opaque_output_handle: str,
    deadline: float,
    clock: Callable[[], float],
    segment_id: str | None = None,
) -> MediaPreviewSourceAuthority | None:
    try:
        if segment_id is None:
            return build_media_preview_source_authority(
                plan=plan,
                receipt=receipt,
                store=store,
                adapter=adapter,
                opaque_output_handle=opaque_output_handle,
                deadline=deadline,
                clock=clock,
            )
        return build_segment_media_preview_source_authority(
            plan=plan,
            receipt=receipt,
            store=store,
            adapter=adapter,
            segment_id=segment_id,
            opaque_output_handle=opaque_output_handle,
            deadline=deadline,
            clock=clock,
        )
    except Exception:
        # SECURITY: private store/adapter exceptions can contain host paths; member eligibility is
        # recorded only by the closed content-free row disposition.
        return None


def _try_generated_preview_source(
    *,
    generation_sequence: GenerationSequenceProjection,
    receipt: SegmentArtifactReceipt,
    store: PrivateSegmentArtifactStore,
    opaque_output_handle: str,
    deadline: float,
    clock: Callable[[], float],
) -> MediaPreviewSourceAuthority | None:
    try:
        return build_generated_segment_media_preview_source_authority(
            generation_sequence=generation_sequence,
            receipt=receipt,
            store=store,
            opaque_output_handle=opaque_output_handle,
            deadline=deadline,
            clock=clock,
        )
    except Exception:
        # SECURITY: private store failures can retain host paths; generated preview eligibility is
        # exposed only as the row's closed boolean and never as exception text.
        return None


@dataclass(slots=True)
class _AutomaticSegmentMaterializer:
    planning_context: ProductionPlanningContextV1 | ProductionPlanningContextV2
    proposal: SegmentationProposalV1 | SegmentationProposalV2
    materialize: LegacyMaterializeSegmentContext | MaterializeSegmentContext = field(repr=False)


@dataclass(slots=True)
class _AssemblyCancellation:
    event: threading.Event = field(default_factory=threading.Event, repr=False)

    def is_cancelled(self) -> bool:
        return self.event.is_set()


@dataclass(slots=True)
class _ProductionAssemblyJob:
    job_id: str
    authorization: M26AssemblyAuthorizationV1
    runtime: M26ProductionAssemblyRuntime = field(repr=False)
    automatic_plan: ProductionAutomaticPlanAuthority = field(repr=False)
    generation_sequence: GenerationSequenceProjection = field(repr=False)
    artifact_receipts: tuple[SegmentArtifactReceipt, ...] = field(repr=False)
    state: str = "planned"
    completed: int = 0
    failure_code: str | None = None
    execution: M26ProductionAssemblyExecution | None = field(default=None, repr=False)
    cancellation: _AssemblyCancellation = field(default_factory=_AssemblyCancellation, repr=False)

    def projection(self) -> ProductionAssemblyProjection:
        return ProductionAssemblyProjection(
            state=self.state,
            completed=self.completed,
            total=len(self.artifact_receipts),
            capability_fingerprint=self.runtime.capability.fingerprint,
            managed_sequence_fingerprint=self.authorization.managed_sequence_fingerprint,
            artifact_receipt_fingerprints=self.authorization.artifact_receipt_fingerprints,
            cut_boundary_receipt_fingerprints=(
                self.authorization.cut_boundary_receipt_fingerprints
            ),
            assembly_job_id=self.job_id,
            authorization_fingerprint=self.authorization.fingerprint,
            receipt_fingerprint=(
                None if self.execution is None else self.execution.assembly_receipt.fingerprint
            ),
            failure_code=self.failure_code,
        )


@dataclass(frozen=True, slots=True)
class _MemberRow:
    """Retained facts for one project segment that has been generated at least once."""

    segment_id: str
    latest: ProductionMemberAttemptV1
    completed: ProductionMemberAttemptV1 | None
    #: Registry-wide publication order; the newest attempt decides the project run state.
    order: int
    run_handle: str | None = None
    #: False only between publication and the coordinator creating the run that owns it.
    run_bound: bool = True

    def __post_init__(self) -> None:
        if (
            type(self.latest) is not ProductionMemberAttemptV1
            or self.latest.segment_id != self.segment_id
            or (
                self.completed is not None
                and (
                    type(self.completed) is not ProductionMemberAttemptV1
                    or self.completed.segment_id != self.segment_id
                    or not self.completed.verified
                )
            )
            or type(self.order) is not int
            or self.order < 0
            or (self.run_handle is not None and type(self.run_handle) is not str)
            or type(self.run_bound) is not bool
        ):
            raise ValueError("member_row_authority_mismatch")


_EMPTY_MEMBERS: Mapping[str, _MemberRow] = MappingProxyType({})


def _member_rows_bytes(members: Mapping[str, _MemberRow] | None) -> int:
    if members is None:
        return 0
    # IMPORTANT: count retained objects once by identity. A converted serial project shares one
    # composed sequence and workspace across every member; counting per row refuses valid projects.
    seen: set[int] = set()
    total = 0
    for row in members.values():
        for attempt in (row.latest, row.completed):
            if attempt is None:
                continue
            for value in attempt.retained_objects():
                if id(value) in seen:
                    continue
                seen.add(id(value))
                total += retained_object_bytes(value)
    return total


@dataclass(slots=True)
class _ProductionEntry:
    workspace: MultiSegmentWorkspace
    touched_at: float
    authority_bytes: int
    accepted_authorities: ProductionAcceptedAuthorities = _EMPTY_ACCEPTED_AUTHORITIES
    preview_sources: Mapping[str, MediaPreviewSourceAuthority] = field(
        default_factory=lambda: _EMPTY_PREVIEW_SOURCES
    )
    automatic_plan: ProductionAutomaticPlanAuthority | None = None
    automatic_materializer: _AutomaticSegmentMaterializer | None = field(
        default=None,
        repr=False,
    )
    artifact_store: PrivateSegmentArtifactStore | None = field(default=None, repr=False)
    assembly_runtime: M26ProductionAssemblyRuntime | None = field(default=None, repr=False)
    assembly_job: _ProductionAssemblyJob | None = field(default=None, repr=False)
    completed_assembly: ProductionAssemblyProjection | None = field(default=None, repr=False)
    mutation_claim: threading.Lock = field(default_factory=threading.Lock)
    lineage_token: object | None = field(default=None, repr=False)
    #: Member mode when not None: each independently executed segment keeps its own authority.
    members: Mapping[str, _MemberRow] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _preview_sources_wire_bytes(self.preview_sources)
        if self.members is not None:
            segment_ids = {item.segment_id for item in self.workspace.segments}
            # CRITICAL: member mode owns generation per member. A whole-project sequence, plan or
            # assembly next to member rows would give one segment two contradictory authorities.
            if (
                type(self.members) is not type(_EMPTY_MEMBERS)
                or self.accepted_authorities is not _EMPTY_ACCEPTED_AUTHORITIES
                or self.automatic_plan is not None
                or self.assembly_job is not None
                or self.completed_assembly is not None
                or any(
                    type(row) is not _MemberRow
                    or key != row.segment_id
                    or (key not in segment_ids and row.completed is not None)
                    for key, row in self.members.items()
                )
            ):
                raise ValueError("member_mode_authority_mismatch")
        if self.completed_assembly is not None:
            execution = self.accepted_authorities.assembly_execution
            if (
                type(self.completed_assembly) is not ProductionAssemblyProjection
                or self.completed_assembly.state != "succeeded"
                or execution is None
                or self.completed_assembly.receipt_fingerprint
                != execution.assembly_receipt.fingerprint
                or self.completed_assembly.authorization_fingerprint
                != execution.authorization.fingerprint
            ):
                raise ValueError("completed_assembly_authority_mismatch")
        if self.automatic_plan is not None and (
            type(self.automatic_plan)
            not in (ProductionAutomaticPlanAuthorityV1, ProductionAutomaticPlanAuthorityV2)
            or self.automatic_plan.workspace is not self.workspace
        ):
            raise ValueError("automatic_plan_authority_mismatch")
        if (self.automatic_plan is None) != (self.automatic_materializer is None):
            raise ValueError("automatic_plan_materializer_mismatch")
        automatic_plan = self.automatic_plan
        if (
            self.automatic_materializer is not None
            and automatic_plan is not None
            and (
                self.automatic_materializer.proposal is not automatic_plan.proposal
                or self.automatic_materializer.planning_context.fingerprint
                != automatic_plan.planning_context_fingerprint
            )
        ):
            raise ValueError("automatic_plan_materializer_mismatch")
        if (
            self.artifact_store is not None
            and type(self.artifact_store) is not PrivateSegmentArtifactStore
        ):
            raise ValueError("artifact_store_authority_mismatch")
        if self.assembly_runtime is not None and (
            type(self.assembly_runtime) is not M26ProductionAssemblyRuntime
            or self.artifact_store is None
            or self.assembly_runtime.artifact_store is not self.artifact_store
        ):
            raise ValueError("assembly_runtime_authority_mismatch")
        if self.assembly_job is not None and (
            type(self.assembly_job) is not _ProductionAssemblyJob
            or self.assembly_runtime is not self.assembly_job.runtime
        ):
            raise ValueError("assembly_job_authority_mismatch")


@dataclass(slots=True)
class _ProjectAuthoringAnchor:
    """Bounded content-free seed and optional editor association for one live project."""

    seed: SidebarAuthoringSeed
    target_handle: str | None = None


def _project_authoring_seed(seed: SidebarProductionSeed) -> SidebarAuthoringSeed | None:
    retained = seed.authoring_seed
    if retained is not None:
        if (
            retained.lineage_token is not seed.lineage_token
            or retained.source_id != seed.source_id
            or retained.registry_fingerprint != seed.reference_registry_fingerprint
            or frozenset(row.asset_id for row in retained.sources) != frozenset(seed.reference_ids)
            or len(retained.sources) != len(seed.reference_ids)
        ):
            return None
        return retained
    if seed.reference_ids:
        return None
    return SidebarAuthoringSeed(
        source_id=seed.source_id,
        task_mode=seed.task_mode,
        registry_fingerprint=seed.reference_registry_fingerprint,
        sources=(),
        lineage_token=seed.lineage_token,
    )


@dataclass(slots=True)
class _EmptyAccumulatedProject:
    """Stable destination identity retained before its first verified automatic member."""

    workspace_id: str
    touched_at: float
    lineage_token: object | None = field(default=None, repr=False)
    pending_authoring_seed: SidebarAuthoringSeed | None = field(default=None, repr=False)
    revision: int = 1
    members: Mapping[str, _MemberRow] = field(
        default_factory=lambda: _EMPTY_MEMBERS,
        repr=False,
    )
    mutation_claim: threading.Lock = field(default_factory=threading.Lock)


@dataclass(slots=True)
class _EditableProjectEntry:
    """Imported editable data; deliberately not a MultiSegmentWorkspace authority."""

    workspace_id: str
    document: ProjectDocument
    document_bytes: int
    touched_at: float
    lineage_token: object = field(repr=False)
    revision: int = 1

    @property
    def fingerprint(self) -> str:
        return (
            "sha256:"
            + sha256(
                project_bytes(
                    {
                        "segments": [row.to_wire() for row in self.document.segments],
                        "selection": list(self.document.selection),
                    }
                )
            ).hexdigest()
        )


@dataclass(frozen=True, slots=True)
class _LedgerEntry:
    request_digest: str
    action: str
    workspace_handle: str
    committed_revision: int
    committed_fingerprint: str
    touched_at: float


@dataclass(frozen=True, slots=True)
class _Tombstone:
    disposition: str
    terminated_at: float
    release_request_id: str | None = None
    release_request_digest: str | None = None


@dataclass(frozen=True, slots=True)
class _StagedEntry:
    workspace: MultiSegmentWorkspace
    generation_sequence: GenerationSequenceProjection
    staged_at: float
    authority_bytes: int
    lineage_token: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True, repr=False)
class ProductionAuthoringOutputClaim:
    """Opaque exact raw-output authority for one Authoring import staging operation."""

    workspace_handle: str
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    segment_id: str
    output_handle: str
    receipt: SegmentArtifactReceipt = field(repr=False, compare=False)
    store: PrivateSegmentArtifactStore = field(repr=False, compare=False)
    lineage_token: object = field(repr=False, compare=False)
    _registry: ProductionWorkspaceRegistry = field(repr=False, compare=False)

    def current(self) -> bool:
        return self._registry.authoring_output_claim_is_current(self)

    def __repr__(self) -> str:
        return "<ProductionAuthoringOutputClaim opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("production authoring output claims are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("production authoring output claims are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("production authoring output claims are not serializable")


@dataclass(slots=True, eq=False)
class _DestinationAdmission:
    """One bounded pre-queue reservation of a generation destination."""

    request_id: str
    request_digest: str
    workspace_handle: str | None
    workspace_id: str | None
    segment_id: str | None
    expires_at: float
    protocol: str = "v1"
    intent_kind: str | None = None
    candidate_id: str | None = None
    attempt_id: str | None = None
    state: str = "open"
    closed_at: float | None = None
    published_handle: str | None = None
    settlement_request_id: str | None = None
    settlement_request_digest: str | None = None

    @property
    def intent(self) -> str:
        if self.intent_kind is not None:
            return self.intent_kind
        if self.workspace_handle is None:
            return "create"
        return "append" if self.segment_id is None else "regenerate"


@dataclass(frozen=True, slots=True)
class _PreprepareTerminal:
    """Content-free terminal for an accepted queue that ended before member publication."""

    candidate_id: str
    attempt_id: str
    segment_id: str
    status: str
    order: int

    def __post_init__(self) -> None:
        if self.status not in {"failed", "cancelled", "unknown_ownership"}:
            raise ValueError("preprepare_terminal_status_invalid")


class _OpaqueAuthority:
    """Internal authorities are identities, never values a caller may copy or serialize."""

    __slots__ = ()

    def __copy__(self) -> NoReturn:
        raise TypeError("production member authorities are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("production member authorities are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("production member authorities are not serializable")


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ProductionDestinationAdmissionClaim(_OpaqueAuthority):
    """The exact open admission a managed prepare consumes."""

    request_id: str
    intent: str
    workspace_handle: str | None
    workspace_id: str | None
    segment_id: str | None
    _admission: _DestinationAdmission
    _registry: ProductionWorkspaceRegistry

    def __repr__(self) -> str:
        return "<ProductionDestinationAdmissionClaim opaque>"


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ProductionMemberExecution(_OpaqueAuthority):
    """The single-segment workspace one admitted member executes against."""

    claim: ProductionDestinationAdmissionClaim
    workspace: MultiSegmentWorkspace
    segment_id: str
    lineage_token: object | None
    authoring_seed: SidebarAuthoringSeed | None

    def __repr__(self) -> str:
        return "<ProductionMemberExecution opaque>"


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ProductionMemberPublication(_OpaqueAuthority):
    """Exact rollback authority for one member attempt published before its run exists."""

    workspace_handle: str
    workspace_id: str
    segment_id: str
    attempt: ProductionMemberAttemptV1
    run_handle: str
    created: bool
    appended: bool
    previous_row: _MemberRow | None
    previous_entry: _ProductionEntry | None
    published_entry: _ProductionEntry | None
    _registry: ProductionWorkspaceRegistry
    protocol: str = "v1"

    def __repr__(self) -> str:
        return "<ProductionMemberPublication opaque>"


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ValueError("non-finite JSON number")


def _shape(value: object, *, depth: int = 0) -> int:
    if depth > MAX_PRODUCTION_ACTION_DEPTH:
        raise ValueError("action JSON exceeds the depth bound")
    if type(value) is dict:
        if len(value) > MAX_PRODUCTION_ACTION_NODES:
            raise ValueError("action object exceeds its bound")
        count = 1
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("action object key is invalid")
            count += 1 + _shape(item, depth=depth + 1)
        return count
    if type(value) is list:
        if len(value) > MAX_PRODUCTION_ACTION_ARRAY:
            raise ValueError("action array exceeds its bound")
        return 1 + sum(_shape(item, depth=depth + 1) for item in value)
    if value is None or type(value) in {str, int, bool}:
        if type(value) is str and len(value) > MAX_PRODUCTION_ACTION_BYTES:
            raise ValueError("action string exceeds its bound")
        return 1
    raise ValueError("action JSON contains an unsupported value")


def _bounded_identifier(value: object, field_name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _workspace_handle(value: object) -> str:
    if type(value) is not str or _WORKSPACE_HANDLE.fullmatch(value) is None:
        raise ValueError("workspace_handle is invalid")
    return value


def _sidebar_handle(value: object) -> str:
    if type(value) is not str or _SIDEBAR_HANDLE.fullmatch(value) is None:
        raise ValueError("context_workspace_handle is invalid")
    return value


def _expected_identity(payload: dict[str, object]) -> tuple[int, str]:
    revision = payload["expected_workspace_revision"]
    fingerprint = payload["expected_workspace_fingerprint"]
    if type(revision) is not int or not 1 <= revision <= 1_000_000:
        raise ValueError("expected_workspace_revision is invalid")
    if type(fingerprint) is not str or _FINGERPRINT.fullmatch(fingerprint) is None:
        raise ValueError("expected_workspace_fingerprint is invalid")
    return revision, fingerprint


def _fingerprint_value(value: object, field_name: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _fingerprint_list(value: object, field_name: str, *, allow_empty: bool) -> tuple[str, ...]:
    if (
        type(value) is not list
        or len(value) > MAX_WORKSPACE_SEGMENTS
        or (not allow_empty and not value)
    ):
        raise ValueError(f"{field_name} is invalid")
    fingerprints = tuple(
        _fingerprint_value(item, f"{field_name}[{index}]") for index, item in enumerate(value)
    )
    if len(fingerprints) != len(set(fingerprints)):
        raise ValueError(f"{field_name} contains duplicates")
    return fingerprints


def _validate_action(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _ROOT_KEYS:
        raise ValueError("production action must be one closed object")
    if value["schema"] != PRODUCTION_ACTION_SCHEMA:
        raise ValueError("production action schema is unsupported")
    _bounded_identifier(value["request_id"], "request_id")
    action = value["action"]
    if type(action) is not str or action not in _ACTIONS:
        raise ValueError("production action is unsupported")
    payload = value["payload"]
    if type(payload) is not dict or set(payload) != _PAYLOAD_KEYS[action]:
        raise ValueError("production action payload is not closed")
    if action in {"admit_generation_destination", "admit_generation_destination_v2"}:
        if action.endswith("_v2") and payload["version"] != PRODUCTION_ACCUMULATION_ACTION_VERSION:
            raise ValueError("production accumulation version is unsupported")
        # The three members are nullable together: all null is a create intent, a handle and id
        # append to that project, and a segment id additionally names the member to regenerate.
        if payload["workspace_handle"] is None:
            if payload["workspace_id"] is not None or payload["segment_id"] is not None:
                raise ValueError("admission destination is incomplete")
            return value
        _workspace_handle(payload["workspace_handle"])
        _bounded_identifier(payload["workspace_id"], "workspace_id")
        if payload["segment_id"] is not None:
            _bounded_identifier(payload["segment_id"], "segment_id")
        return value
    if action in {"release_generation_destination", "release_generation_destination_v2"}:
        if action.endswith("_v2") and payload["version"] != PRODUCTION_ACCUMULATION_ACTION_VERSION:
            raise ValueError("production accumulation version is unsupported")
        _bounded_identifier(payload["admission_request_id"], "admission_request_id")
        return value
    if action == "settle_generation_destination_v2":
        if payload["version"] != PRODUCTION_ACCUMULATION_ACTION_VERSION:
            raise ValueError("production accumulation version is unsupported")
        _bounded_identifier(payload["admission_request_id"], "admission_request_id")
        if payload["terminal"] not in {"failed", "cancelled", "unknown_ownership"}:
            raise ValueError("production admission terminal is unsupported")
        return value
    if action == "read_accumulated_project":
        if payload["version"] != PRODUCTION_ACCUMULATION_ACTION_VERSION:
            raise ValueError("production accumulation version is unsupported")
        _workspace_handle(payload["workspace_handle"])
        _bounded_identifier(payload["workspace_id"], "workspace_id")
        return value
    if "workspace_handle" in payload:
        _workspace_handle(payload["workspace_handle"])
    if "context_workspace_handle" in payload:
        _sidebar_handle(payload["context_workspace_handle"])
    if action not in {"create_workspace_from_context", "read_projection"}:
        _expected_identity(payload)
    if "segment_id" in payload:
        _bounded_identifier(payload["segment_id"], "segment_id")
    if "segment_ids" in payload:
        segment_ids = payload["segment_ids"]
        if type(segment_ids) is not list or len(segment_ids) > MAX_WORKSPACE_SEGMENTS:
            raise ValueError("segment_ids is invalid")
        identifiers = tuple(
            _bounded_identifier(item, f"segment_ids[{index}]")
            for index, item in enumerate(segment_ids)
        )
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("segment_ids contains duplicates")
    if "workspace_id" in payload:
        _bounded_identifier(payload["workspace_id"], "workspace_id")
    if "managed_sequence_fingerprint" in payload:
        _fingerprint_value(
            payload["managed_sequence_fingerprint"],
            "managed_sequence_fingerprint",
        )
    if "assembly_capability_fingerprint" in payload:
        _fingerprint_value(
            payload["assembly_capability_fingerprint"],
            "assembly_capability_fingerprint",
        )
    if "expected_assembly_fingerprint" in payload:
        _fingerprint_value(
            payload["expected_assembly_fingerprint"],
            "expected_assembly_fingerprint",
        )
    if "artifact_receipt_fingerprints" in payload:
        artifacts = _fingerprint_list(
            payload["artifact_receipt_fingerprints"],
            "artifact_receipt_fingerprints",
            allow_empty=False,
        )
        cuts = _fingerprint_list(
            payload["cut_boundary_receipt_fingerprints"],
            "cut_boundary_receipt_fingerprints",
            allow_empty=True,
        )
        if len(cuts) != len(artifacts) - 1:
            raise ValueError("assembly predecessor count is invalid")
    if "output_profile_id" in payload and payload["output_profile_id"] != M26_OUTPUT_PROFILE_ID:
        raise ValueError("output_profile_id is invalid")
    if "relation" in payload:
        relation = payload["relation"]
        predecessor = payload["predecessor_segment_id"]
        if type(relation) is not str:
            raise ValueError("relation is invalid")
        try:
            relation_kind = SegmentRelationKind(relation)
        except ValueError as exc:
            raise ValueError("relation is invalid") from exc
        if predecessor is not None:
            _bounded_identifier(predecessor, "predecessor_segment_id")
        requires_predecessor = relation_kind in {
            SegmentRelationKind.PREDECESSOR,
            SegmentRelationKind.ADJACENT_PAIR,
        }
        if requires_predecessor != (predecessor is not None):
            raise ValueError("predecessor_segment_id does not match relation")
    return value


def decode_production_action_json(data: bytes) -> dict[str, object]:
    """Decode the only action wire with strict bytes, UTF-8, JSON, depth and node bounds."""

    if type(data) is not bytes or not data or len(data) > MAX_PRODUCTION_ACTION_BYTES:
        raise ValueError("production action bytes are invalid")
    try:
        text = data.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("production action JSON is invalid") from exc
    if _shape(value) > MAX_PRODUCTION_ACTION_NODES:
        raise ValueError("production action exceeds the node bound")
    return _validate_action(value)


def _segment_from_seed(
    seed: SidebarProductionSeed,
    *,
    segment_id: str,
    relation: SegmentRelationKind,
    predecessor_segment_id: str | None,
) -> SegmentDeclaration:
    return SegmentDeclaration(
        segment_id=segment_id,
        task_mode=seed.task_mode,
        source_id=seed.source_id,
        reference_ids=seed.reference_ids,
        duration=seed.duration,
        relation=relation,
        predecessor_segment_id=predecessor_segment_id,
        accepted_intent_fingerprint=seed.accepted_intent_fingerprint,
        semantic_receipt_fingerprint=None,
        profile_fingerprint=seed.profile_fingerprint,
        reference_registry_fingerprint=seed.reference_registry_fingerprint,
        native_binding_fingerprint=seed.native_binding_fingerprint,
        producer_settings_fingerprint=seed.producer_settings_fingerprint,
    )


def _authorities(segments: tuple[SegmentDeclaration, ...]) -> tuple[AcceptedIntentAuthority, ...]:
    return tuple(
        AcceptedIntentAuthority(segment.segment_id, segment.accepted_intent_fingerprint)
        for segment in segments
    )


def _is_structural_successor(
    live: MultiSegmentWorkspace,
    source: MultiSegmentWorkspace,
) -> bool:
    """Return whether live can still consume authority produced from source."""

    return (
        live.workspace_id == source.workspace_id
        and live.segments == source.segments
        and (
            live.revision > source.revision
            or (live.revision == source.revision and live.fingerprint == source.fingerprint)
        )
    )


def _validate_accepted_authorities(
    workspace: MultiSegmentWorkspace,
    authorities: ProductionAcceptedAuthorities,
) -> None:
    sequence = authorities.generation_sequence
    authority_workspace = workspace
    if sequence is not None:
        source = sequence.state.plan.source_workspace_authority
        if source is not None:
            if not _is_structural_successor(workspace, source):
                raise ProductionWorkbenchError("accepted_authority_mismatch", 422)
            authority_workspace = source
    manifests = derive_segment_manifests(authority_workspace)
    manifest_by_id = {item.segment_id: item for item in manifests}
    segment_ids = tuple(item.segment_id for item in authority_workspace.segments)
    if sequence is not None:
        plan = sequence.state.plan
        dirty_segment_ids = {item.segment_id for item in plan.jobs}
        expected_job_ids = tuple(item for item in segment_ids if item in dirty_segment_ids)
        expected_clean_ids = tuple(item for item in segment_ids if item not in dirty_segment_ids)
        if (
            plan.workspace_id != authority_workspace.workspace_id
            or plan.workspace_revision != authority_workspace.revision
            or plan.workspace_fingerprint != authority_workspace.fingerprint
            or plan.manifest_fingerprints != tuple(item.fingerprint for item in manifests)
            or set(plan.clean_segment_ids) | {item.segment_id for item in plan.jobs}
            != set(segment_ids)
            or tuple(item.segment_id for item in plan.jobs) != expected_job_ids
            or plan.clean_segment_ids != expected_clean_ids
        ):
            raise ProductionWorkbenchError("accepted_authority_mismatch", 422)
        for job in plan.jobs:
            manifest = manifest_by_id.get(job.segment_id)
            if manifest is None or (
                job.ordinal != manifest.ordinal
                or job.task_mode is not manifest.task_mode
                or job.source_id != manifest.source_id
                or job.reference_ids != manifest.reference_ids
                or job.duration != manifest.duration
                or job.dependency_segment_ids
                != tuple(
                    item for item in manifest.dependency_segment_ids if item in dirty_segment_ids
                )
                or job.manifest_fingerprint != manifest.fingerprint
                or job.producer_fingerprint != manifest.producer_fingerprint
                or job.native_binding_fingerprint != manifest.native_binding_fingerprint
                or job.settings_fingerprint != manifest.producer_settings_fingerprint
            ):
                raise ProductionWorkbenchError("accepted_authority_mismatch", 422)

    artifact_by_id: dict[str, SegmentArtifactReceipt] = {}
    for artifact_receipt in authorities.artifact_receipts:
        manifest = manifest_by_id.get(artifact_receipt.segment_id)
        if (
            artifact_receipt.segment_id in artifact_by_id
            or manifest is None
            or (
                artifact_receipt.workspace_id != authority_workspace.workspace_id
                or artifact_receipt.workspace_revision != authority_workspace.revision
                or artifact_receipt.workspace_fingerprint != authority_workspace.fingerprint
                or artifact_receipt.manifest_fingerprint != manifest.fingerprint
                or artifact_receipt.producer_fingerprint != manifest.producer_fingerprint
                or artifact_receipt.native_binding_fingerprint
                != manifest.native_binding_fingerprint
                or artifact_receipt.settings_fingerprint != manifest.producer_settings_fingerprint
                or artifact_receipt.source_id != manifest.source_id
            )
        ):
            raise ProductionWorkbenchError("accepted_authority_mismatch", 422)
        artifact_by_id[artifact_receipt.segment_id] = artifact_receipt

    boundaries = authorities.continuity_receipts
    if boundaries and len(boundaries) != max(0, len(segment_ids) - 1):
        raise ProductionWorkbenchError("accepted_authority_mismatch", 422)
    for index, boundary_receipt in enumerate(boundaries, start=1):
        successor_id = segment_ids[index]
        successor = authority_workspace.segments[index]
        if boundary_receipt.mode is not _CONTINUITY_MODE_BY_RELATION[successor.relation]:
            raise ProductionWorkbenchError("accepted_authority_mismatch", 422)
        if boundary_receipt.mode is ContinuityMode.RESTART:
            continue
        if boundary_receipt.mode is ContinuityMode.CUT:
            continue
        predecessor_id = successor.predecessor_segment_id
        if predecessor_id is None:
            raise ProductionWorkbenchError("accepted_authority_mismatch", 422)
        predecessor_artifact = artifact_by_id.get(predecessor_id)
        successor_job = (
            None
            if sequence is None
            else next(
                (job for job in sequence.state.plan.jobs if job.segment_id == successor_id),
                None,
            )
        )
        if (
            predecessor_artifact is None
            or predecessor_artifact.output_fingerprint is None
            or successor_job is None
            or boundary_receipt.predecessor_segment_id != predecessor_id
            or boundary_receipt.predecessor_receipt_fingerprint != predecessor_artifact.fingerprint
            or boundary_receipt.predecessor_output_fingerprint
            != predecessor_artifact.output_fingerprint
            or boundary_receipt.successor_segment_id != successor_id
            or boundary_receipt.successor_job_id != successor_job.job_id
            or boundary_receipt.successor_graph_fingerprint != successor_job.graph_fingerprint
        ):
            raise ProductionWorkbenchError("accepted_authority_mismatch", 422)

    assembly_execution = authorities.assembly_execution
    reconstruction = (
        authorities.reconstruction_receipt
        if assembly_execution is None
        else assembly_execution.reconstruction_receipt
    )
    if reconstruction is not None:
        if (
            sequence is None
            or reconstruction.generation_state_fingerprint != sequence.state.fingerprint
            or tuple(item.segment_id for item in reconstruction.segment_results) != segment_ids
        ):
            raise ProductionWorkbenchError("accepted_authority_mismatch", 422)
        if assembly_execution is None:
            if reconstruction.artifact_receipt_fingerprints != tuple(
                item.fingerprint for item in authorities.artifact_receipts
            ) or reconstruction.boundary_receipt_fingerprints != tuple(
                item.fingerprint for item in boundaries
            ):
                raise ProductionWorkbenchError("accepted_authority_mismatch", 422)
        # IMPORTANT: M26's embedded legacy executor consumes cropped derived receipts and
        # synthetic cut receipts. Comparing it to the original artifact/continuity identities
        # rejects every valid assembly publication even though the outer authority retains them.
        elif (
            reconstruction.artifact_receipt_fingerprints
            != tuple(
                item.fingerprint for item in assembly_execution.outer_plan.derived_input_receipts
            )
            or reconstruction.boundary_receipt_fingerprints
            != assembly_execution.embedded_plan.boundary_receipt_fingerprints
        ):
            raise ProductionWorkbenchError("accepted_authority_mismatch", 422)

    execution = authorities.assembly_execution
    if execution is not None:
        authorization = execution.authorization
        outer_plan = execution.outer_plan
        assembly_receipt = execution.assembly_receipt
        embedded_receipt = execution.reconstruction_receipt
        if (
            sequence is None
            or type(authorization) is not M26AssemblyAuthorizationV1
            or type(assembly_receipt) is not ProductionAssemblyReceiptV1
            or outer_plan.authorization is not authorization
            or outer_plan.embedded_plan is not execution.embedded_plan
            or outer_plan.embedded_approval is not execution.embedded_approval
            or authorization.workspace_id != authority_workspace.workspace_id
            or authorization.workspace_revision != authority_workspace.revision
            or authorization.workspace_fingerprint != authority_workspace.fingerprint
            or authorization.managed_sequence_fingerprint
            != canonical_fingerprint(sequence.to_wire())
            or authorization.generation_plan_fingerprint != sequence.state.plan.fingerprint
            or authorization.generation_state_fingerprint != sequence.state.fingerprint
            or authorization.manifest_fingerprints != sequence.state.plan.manifest_fingerprints
            or authorization.artifact_receipt_fingerprints
            != tuple(item.fingerprint for item in authorities.artifact_receipts)
            or outer_plan.original_artifact_receipt_fingerprints
            != authorization.artifact_receipt_fingerprints
            or assembly_receipt.authorization_fingerprint != authorization.fingerprint
            or assembly_receipt.outer_plan_fingerprint != outer_plan.fingerprint
            or assembly_receipt.reconstruction_receipt_fingerprint != embedded_receipt.fingerprint
        ):
            raise ProductionWorkbenchError("accepted_authority_mismatch", 422)
        try:
            execution.embedded_approval.assert_executable(
                execution.embedded_plan,
                now_ms=execution.embedded_approval.approved_at_ms,
            )
            rebuilt = build_production_assembly_receipt(
                transaction_id=assembly_receipt.transaction_id,
                outer_plan=outer_plan,
                reconstruction_receipt=embedded_receipt,
                completed_at_ms=assembly_receipt.completed_at_ms,
            )
        except Exception as exc:
            raise ProductionWorkbenchError("accepted_authority_mismatch", 422) from exc
        if rebuilt != assembly_receipt:
            raise ProductionWorkbenchError("accepted_authority_mismatch", 422)


def _opaque_output_handle(value: str) -> str:
    digest = sha256(f"m17-12-output:{value}".encode()).hexdigest()
    return f"out_{digest[:40]}"


def _opaque_artifact_output_handle(receipt: SegmentArtifactReceipt) -> str:
    return _opaque_output_handle(f"segment-artifact:{receipt.fingerprint}")


def _preview_publication_journal(
    receipt: AVReconstructionReceipt,
    preview_sources: Mapping[str, MediaPreviewSourceAuthority],
) -> tuple[ProductionPreviewRowPublication, ...]:
    segment_by_output = {
        item.derived_output_handle: item.segment_id
        for item in receipt.segment_results
        if item.derived_output_handle is not None
    }
    journal: list[ProductionPreviewRowPublication] = []
    for output in receipt.outputs:
        opaque_handle = _opaque_output_handle(output.handle)
        if output.kind is AVOutputKind.RECONSTRUCTION_FULL:
            segment_id = None
        else:
            segment_id = segment_by_output.get(output.handle)
            if segment_id is None:
                raise ProductionWorkbenchError("accepted_authority_mismatch", 422)
        journal.append(
            ProductionPreviewRowPublication(
                output_handle=opaque_handle,
                segment_id=segment_id,
                disposition=(
                    "published" if opaque_handle in preview_sources else "attached_without_preview"
                ),
            )
        )
    return tuple(journal)


@dataclass(frozen=True, slots=True)
class _AcceptedProjectionFields:
    run_state: str
    run_completed: int
    run_total: int
    generation_sequence: ProductionGenerationSequenceSummary | None
    reconstruction_state: str
    authority_versions: tuple[str, ...]
    outputs: tuple[ProductionOutputProjection, ...]
    generation_action_available: bool
    closure_states: dict[str, str]
    job_states: dict[str, str]
    artifact_states: dict[str, str]
    continuity_states: dict[str, str]
    delivered_geometry: dict[str, ProductionDeliveredVideoProjection]


def _accepted_projection_fields(
    workspace: MultiSegmentWorkspace,
    authorities: ProductionAcceptedAuthorities,
    preview_sources: Mapping[str, MediaPreviewSourceAuthority],
) -> _AcceptedProjectionFields:
    sequence = authorities.generation_sequence
    closure_states: dict[str, str] = {}
    job_states: dict[str, str] = {}
    run_state = "unavailable"
    run_completed = 0
    run_total = 0
    generation_action_available = False
    generation_sequence = None
    versions: list[str] = []
    if sequence is not None:
        versions.append(GENERATION_SEQUENCE_PROJECTION_SCHEMA)
        plan = sequence.state.plan
        generation_sequence = ProductionGenerationSequenceSummary(
            sequence_id=plan.sequence_id,
            sequence_fingerprint=plan.fingerprint,
            state_fingerprint=sequence.state.fingerprint,
            workspace_id=plan.workspace_id,
            workspace_revision=plan.workspace_revision,
            workspace_fingerprint=plan.workspace_fingerprint,
            correlation=sequence.correlation,
        )
        for segment_id in plan.clean_segment_ids:
            closure_states[segment_id] = "clean"
            job_states[segment_id] = "clean"
        for job, progress in zip(plan.jobs, sequence.progress, strict=True):
            closure_states[job.segment_id] = job.disposition.value
            job_states[job.segment_id] = progress.state.value
        run_total = len(plan.clean_segment_ids) + len(sequence.progress)
        terminal = {
            GenerationJobState.OUTPUT_VERIFICATION_FAILED,
            GenerationJobState.SUCCEEDED,
            GenerationJobState.FAILED,
            GenerationJobState.TIMED_OUT,
            GenerationJobState.CANCELLED,
        }
        run_completed = len(plan.clean_segment_ids) + sum(
            item.state in terminal for item in sequence.progress
        )
        states = {item.state for item in sequence.progress}
        if states & {
            GenerationJobState.OUTPUT_VERIFICATION_FAILED,
            GenerationJobState.FAILED,
            GenerationJobState.TIMED_OUT,
        }:
            run_state = "failed"
        elif sequence.state.cancellation_requested or GenerationJobState.CANCELLED in states:
            run_state = "cancelled"
        elif run_completed == run_total:
            run_state = "succeeded"
        elif states & {
            GenerationJobState.PROJECTED,
            GenerationJobState.SUBMITTED,
            GenerationJobState.RUNNING,
            GenerationJobState.UNKNOWN_OWNERSHIP,
        }:
            run_state = "running"
        else:
            run_state = "ready"
        generation_action_available = bool(sequence.eligible_commands)

    artifact_states = {item.segment_id: item.state.value for item in authorities.artifact_receipts}
    delivered_geometry = {
        item.segment_id: ProductionDeliveredVideoProjection(
            format_label=item.format_label,
            frame_count=item.shape[0],
            height=item.shape[1],
            width=item.shape[2],
        )
        for item in authorities.artifact_receipts
        if item.state.value == "complete"
        and item.format_label in {"mp4", "mkv", "webm"}
        and len(item.shape) == 4
        and item.shape[3] == 3
    }
    if authorities.artifact_receipts:
        versions.append("h3.context.segment_artifact_receipt.v1")
    continuity_states: dict[str, str] = {}
    if authorities.continuity_receipts:
        versions.append("h3.context.continuity_boundary_receipt.v1")
        # Boundary receipts are ordered between canonical adjacent segments. The adapter validates
        # native IDs before this content-free map is created.
        ordered_ids = tuple(item.segment_id for item in workspace.segments)
        for index, receipt in enumerate(authorities.continuity_receipts, start=1):
            if receipt.mode is ContinuityMode.NATIVE_FRAME_HANDOFF:
                successor_id = cast(str, receipt.successor_segment_id)
            elif index < len(ordered_ids):
                successor_id = ordered_ids[index]
            else:
                continue
            continuity_states[successor_id] = receipt.mode.value

    outputs: tuple[ProductionOutputProjection, ...] = ()
    reconstruction_state = "unavailable"
    assembly_execution = authorities.assembly_execution
    reconstruction = (
        authorities.reconstruction_receipt
        if assembly_execution is None
        else assembly_execution.reconstruction_receipt
    )
    projected_outputs: list[ProductionOutputProjection] = []
    # IMPORTANT: automatic assembly consumes private cropped inputs, but its public originals
    # retain their receipt handles. Exposing those derived inputs revokes existing NLE sources.
    if reconstruction is None or assembly_execution is not None:
        complete_by_segment = {
            item.segment_id: item
            for item in authorities.artifact_receipts
            if item.state.value == "complete" and item.format_label in {"mp4", "mkv", "webm"}
        }
        for segment in workspace.segments:
            artifact = complete_by_segment.get(segment.segment_id)
            if artifact is None:
                continue
            opaque_handle = _opaque_artifact_output_handle(artifact)
            projected_outputs.append(
                ProductionOutputProjection(
                    output_handle=opaque_handle,
                    ordinal=len(projected_outputs) + 1,
                    state="ready",
                    segment_id=segment.segment_id,
                    preview=opaque_handle in preview_sources,
                )
            )
        outputs = tuple(projected_outputs)
    if reconstruction is not None:
        versions.append("h3.context.av_reconstruction_receipt.v1")
        if assembly_execution is not None:
            versions.append("h3.context.m26.production_assembly_receipt.v1")
        if reconstruction.publication_state is AVPublicationState.COMPLETE:
            reconstruction_state = "complete"
            segment_by_output = {
                item.derived_output_handle: item.segment_id
                for item in reconstruction.segment_results
                if item.derived_output_handle is not None
            }
            for item in reconstruction.outputs:
                if (
                    assembly_execution is not None
                    and item.kind is not AVOutputKind.RECONSTRUCTION_FULL
                ):
                    continue
                opaque_handle = _opaque_output_handle(item.handle)
                output_segment_id: str | None = (
                    None
                    if item.kind is AVOutputKind.RECONSTRUCTION_FULL
                    else segment_by_output[item.handle]
                )
                projected_outputs.append(
                    ProductionOutputProjection(
                        output_handle=opaque_handle,
                        ordinal=len(projected_outputs) + 1,
                        state="ready",
                        segment_id=output_segment_id,
                        preview=opaque_handle in preview_sources,
                    )
                )
            outputs = tuple(projected_outputs)
    # CRITICAL: every authority key must join one exact projected output; otherwise Preview could
    # be advertised for a stale reconstruction member or a foreign generated artifact.
    if not set(preview_sources).issubset(output.output_handle for output in outputs):
        raise ProductionWorkbenchError("accepted_authority_mismatch", 422)
    return _AcceptedProjectionFields(
        run_state=run_state,
        run_completed=run_completed,
        run_total=run_total,
        generation_sequence=generation_sequence,
        reconstruction_state=reconstruction_state,
        authority_versions=tuple(versions),
        outputs=outputs,
        generation_action_available=generation_action_available,
        closure_states=closure_states,
        job_states=job_states,
        artifact_states=artifact_states,
        continuity_states=continuity_states,
        delivered_geometry=delivered_geometry,
    )


AssemblyRuntimeProvider = Callable[
    [PrivateSegmentArtifactStore], M26ProductionAssemblyRuntime | None
]
AssemblySubmitter = Callable[[Callable[[], None]], object]
MediaLease = Callable[[], AbstractContextManager[object]]


class ProductionWorkspaceRegistry:
    """Finite process-local owner of immutable canonical Production workspaces."""

    def __init__(
        self,
        *,
        seed_claim: Callable[[str], SidebarProductionSeed],
        max_entries: int = MAX_PRODUCTION_WORKSPACES,
        ttl_seconds: int = PRODUCTION_WORKSPACE_TTL_SECONDS,
        max_ledger_entries: int = MAX_PRODUCTION_REQUEST_LEDGER,
        max_tombstones: int = MAX_PRODUCTION_TOMBSTONES,
        terminal_ttl_seconds: int = PRODUCTION_TOMBSTONE_TTL_SECONDS,
        mutation_timeout_seconds: float = PRODUCTION_MUTATION_CLAIM_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        assembly_runtime_provider: AssemblyRuntimeProvider | None = None,
        assembly_submitter: AssemblySubmitter | None = None,
        media_lease: MediaLease = nullcontext,
    ) -> None:
        if type(max_entries) is not int or not 1 <= max_entries <= MAX_PRODUCTION_WORKSPACES:
            raise ValueError("production entry limit is invalid")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86_400:
            raise ValueError("production TTL is invalid")
        if type(max_ledger_entries) is not int or not 1 <= max_ledger_entries <= 256:
            raise ValueError("production ledger limit is invalid")
        if type(max_tombstones) is not int or not 1 <= max_tombstones <= 256:
            raise ValueError("production tombstone limit is invalid")
        if type(terminal_ttl_seconds) is not int or not 1 <= terminal_ttl_seconds <= 86_400:
            raise ValueError("production terminal TTL is invalid")
        if type(mutation_timeout_seconds) is not float or not 0 < mutation_timeout_seconds <= 1:
            raise ValueError("production mutation timeout is invalid")
        if not callable(clock_ms):
            raise ValueError("production millisecond clock is invalid")
        if assembly_runtime_provider is not None and not callable(assembly_runtime_provider):
            raise ValueError("assembly runtime provider is invalid")
        if assembly_submitter is not None and not callable(assembly_submitter):
            raise ValueError("assembly submitter is invalid")
        if not callable(media_lease):
            raise ValueError("media lease is invalid")
        self._seed_claim = seed_claim
        self._live_sequence_probe: Callable[[str], bool] | None = None
        self._live_run_probe: Callable[[str], bool] | None = None
        self._admissions: OrderedDict[str, _DestinationAdmission] = OrderedDict()
        self._member_order = 0
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._max_ledger_entries = max_ledger_entries
        self._max_tombstones = max_tombstones
        self._terminal_ttl_seconds = terminal_ttl_seconds
        self._mutation_timeout_seconds = mutation_timeout_seconds
        self._clock = clock
        self._clock_ms = clock_ms
        self._assembly_runtime_provider = assembly_runtime_provider
        self._assembly_submitter = assembly_submitter
        self._media_lease = media_lease
        self._assembly_executor: ThreadPoolExecutor | None = None
        self._entries: dict[str, _ProductionEntry] = {}
        self._editable_projects: dict[str, _EditableProjectEntry] = {}
        self._authoring_anchors: dict[str, _ProjectAuthoringAnchor] = {}
        self._empty_projects: dict[str, _EmptyAccumulatedProject] = {}
        self._project_revisions: dict[str, int] = {}
        self._preprepare_terminals: dict[str, _PreprepareTerminal] = {}
        self._staged: dict[str, _StagedEntry] = {}
        self._ledger: OrderedDict[str, _LedgerEntry] = OrderedDict()
        self._tombstones: OrderedDict[str, _Tombstone] = OrderedDict()
        self._lock = threading.RLock()
        self._recovery_ids = RecordIdentityMap()
        self._editor_recovery_changed: Callable[[str, bool], None] | None = None
        self._editor_recovery_protected: Callable[[str], bool] | None = None

    def configure_editor_recovery(
        self, changed: Callable[[str, bool], None] | None, protected: Callable[[str], bool] | None
    ) -> None:
        with self._lock:
            self._editor_recovery_changed, self._editor_recovery_protected = changed, protected

    def _notify_editor_recovery(self, handle: str) -> None:
        if self._editor_recovery_changed is not None:
            self._editor_recovery_changed(
                handle,
                handle not in self._entries
                and handle not in self._editable_projects
                and handle not in self._empty_projects,
            )

    def recovery_metadata(self) -> tuple[StateRecord, ...]:
        """Copy only no-prose facts; the caller writes after releasing this business lock."""
        with self._lock:
            keys = tuple(self._entries) + tuple(self._empty_projects)
            identities = self._recovery_ids.reconcile(keys)
            rows = []
            # CRITICAL: never use the public projection here; it contains live receipts and
            # source authority. Replacement entries share a metadata ID through this owner map.
            for handle in keys:
                identifier, created = identities[handle]
                entry = self._entries.get(handle)
                revision = (
                    entry.workspace.revision
                    if entry is not None
                    else self._empty_projects[handle].revision
                )
                count = len(entry.workspace.segments) if entry is not None else 0
                rows.append(
                    decode_record(
                        {
                            "record_id": identifier,
                            "kind": "production",
                            "created_at_ms": created,
                            "closed_at_ms": None,
                            "segment_count": count,
                            "state": "workspace_active",
                            "last_transition": None,
                            "revisions": {
                                "workspace": revision,
                                "reference": None,
                                "timeline": None,
                                "context": None,
                                "transition": 0,
                            },
                        }
                    )
                )
            return tuple(rows)

    def _record_tombstone(self, handle: str, tombstone: _Tombstone) -> None:
        self._preprepare_terminals.pop(handle, None)
        self._authoring_anchors.pop(handle, None)
        self._tombstones[handle] = tombstone
        self._tombstones.move_to_end(handle)
        while len(self._tombstones) > self._max_tombstones:
            self._tombstones.popitem(last=False)

    def bind_live_sequence_probe(
        self,
        probe: Callable[[str], bool],
        *,
        run_probe: Callable[[str], bool] | None = None,
    ) -> None:
        """Register the authority that says whether a run still depends on a workspace.

        CRITICAL: this registry cannot answer that question itself -- it has no run identity -- and
        before M23-31 nothing answered it at all, so `release_workspace` could remove a workspace a
        live generation sequence still depended on and strand it. That is the invariant
        `comfyui_h3_context.core.managed_run.NO_LIVE_SEQUENCE_GUARD` names. The probe is the
        ManagedRun aggregate, supplied by the coordinator that owns it, so the rule has exactly one
        definition rather than a copy here that could drift. Binding twice is a defect: two probes
        would mean two authorities, which is what this item removes.

        `run_probe` asks the same aggregate the same question joined by run handle. A project with
        members can retain a finished detached run and a lost attempt at once, so only the member's
        own run can say whether that member is still live.
        """

        if self._live_sequence_probe is not None:
            raise ProductionWorkbenchError("live_sequence_probe_bound", 500)
        if not callable(probe) or (run_probe is not None and not callable(run_probe)):
            raise ProductionWorkbenchError("live_sequence_probe_invalid", 500)
        self._live_sequence_probe = probe
        self._live_run_probe = run_probe

    @staticmethod
    def _original_outputs_supported(entry: _ProductionEntry) -> bool:
        if entry.members is not None:
            return True
        authorities = entry.accepted_authorities
        execution = authorities.assembly_execution
        # CRITICAL: completing assembly adds a derived result; it does not replace the original
        # receipts. Keep this metadata-only so authoring's commit guard never takes media locks.
        return authorities.reconstruction_receipt is None and (
            execution is None
            or (
                execution.reconstruction_receipt.publication_state is AVPublicationState.COMPLETE
                and execution.authorization.artifact_receipt_fingerprints
                == execution.assembly_receipt.original_artifact_receipt_fingerprints
                == tuple(item.fingerprint for item in authorities.artifact_receipts)
            )
        )

    @staticmethod
    def _retained_original_receipts(
        entry: _ProductionEntry,
    ) -> tuple[SegmentArtifactReceipt, ...]:
        if entry.members is None:
            return entry.accepted_authorities.artifact_receipts
        return tuple(
            receipt
            for segment in entry.workspace.segments
            if (row := entry.members.get(segment.segment_id)) is not None
            and row.completed is not None
            and (receipt := row.completed.artifact_receipt) is not None
        )

    @staticmethod
    def _original_output_is_eligible(
        entry: _ProductionEntry, receipt: SegmentArtifactReceipt, *, now_ms: int
    ) -> bool:
        return (
            ProductionWorkspaceRegistry._original_outputs_supported(entry)
            and entry.lineage_token is not None
            and type(entry.artifact_store) is PrivateSegmentArtifactStore
            and any(
                item is receipt
                for item in ProductionWorkspaceRegistry._retained_original_receipts(entry)
            )
            and any(item.segment_id == receipt.segment_id for item in entry.workspace.segments)
            and receipt.state is ArtifactLifecycleState.COMPLETE
            and receipt.format_label in {"mp4", "mkv", "webm"}
            and receipt.expires_at_ms > now_ms
        )

    @staticmethod
    def _raw_claim_matches_entry(
        claim: ProductionAuthoringOutputClaim,
        entry: _ProductionEntry,
        *,
        now_ms: int,
    ) -> bool:
        receipt = claim.receipt
        if entry.members is not None:
            # CRITICAL: a member's output stays current while its own retained completed receipt
            # does, not while the project revision does. Appends, selection and reorder must not
            # revoke imported clips; the exact receipt object, lineage and store still must match,
            # so a deleted, replaced, regenerated, released or expired member revokes them.
            row = entry.members.get(claim.segment_id)
            return (
                row is not None
                and row.completed is not None
                and row.completed.artifact_receipt is receipt
                and entry.lineage_token is claim.lineage_token
                and entry.artifact_store is claim.store
                and entry.workspace.workspace_id == claim.workspace_id
                and ProductionWorkspaceRegistry._original_output_is_eligible(
                    entry, receipt, now_ms=now_ms
                )
                and receipt.segment_id == claim.segment_id
                and _opaque_artifact_output_handle(receipt) == claim.output_handle
            )
        return (
            entry.lineage_token is claim.lineage_token
            and entry.artifact_store is claim.store
            and entry.workspace.workspace_id == claim.workspace_id
            and entry.workspace.revision == claim.workspace_revision
            and entry.workspace.fingerprint == claim.workspace_fingerprint
            and ProductionWorkspaceRegistry._original_output_is_eligible(
                entry, receipt, now_ms=now_ms
            )
            and receipt.segment_id == claim.segment_id
            and _opaque_artifact_output_handle(receipt) == claim.output_handle
        )

    def claim_authoring_output_batch(
        self,
        *,
        workspace_handle: str,
        workspace_id: str,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
        pairs: tuple[tuple[str, str], ...],
    ) -> tuple[ProductionAuthoringOutputClaim, ...]:
        """Resolve only selected current raw per-segment outputs; never an aggregate."""

        handle = _workspace_handle(workspace_handle)
        if (
            type(workspace_id) is not str
            or type(expected_workspace_revision) is not int
            or type(expected_workspace_fingerprint) is not str
            or type(pairs) is not tuple
            or not pairs
            or not all(
                type(pair) is tuple and len(pair) == 2 and all(type(value) is str for value in pair)
                for pair in pairs
            )
        ):
            raise ProductionWorkbenchError("production_request_invalid", 400)
        with self._lock:
            self._prune(self._clock())
            entry = self._entries.get(handle)
            if entry is None:
                self._terminal_or_missing(handle)
            if (
                entry.workspace.workspace_id != workspace_id
                or entry.workspace.revision != expected_workspace_revision
                or entry.workspace.fingerprint != expected_workspace_fingerprint
            ):
                raise ProductionWorkbenchError(
                    "production_stale", 409, projection=self._projection(handle, entry)
                )
            if entry.lineage_token is None:
                raise ProductionWorkbenchError("lineage_mismatch", 409)
            if not self._original_outputs_supported(entry):
                raise ProductionWorkbenchError("output_unsupported", 422)
            store = entry.artifact_store
            if type(store) is not PrivateSegmentArtifactStore:
                raise ProductionWorkbenchError("artifact_not_ready", 422)
            segment_order = {
                row.segment_id: index for index, row in enumerate(entry.workspace.segments)
            }
            requested_ids = tuple(pair[0] for pair in pairs)
            if any(
                segment_id not in entry.workspace.selected_segment_ids
                for segment_id in requested_ids
            ):
                raise ProductionWorkbenchError("output_unknown", 404)
            if tuple(sorted(requested_ids, key=segment_order.__getitem__)) != requested_ids:
                raise ProductionWorkbenchError("output_unknown", 404)
            receipts = {row.segment_id: row for row in self._retained_original_receipts(entry)}
            now_ms = self._clock_ms()
            claims: list[ProductionAuthoringOutputClaim] = []
            for segment_id, output_handle in pairs:
                receipt = receipts.get(segment_id)
                if receipt is None or _opaque_artifact_output_handle(receipt) != output_handle:
                    raise ProductionWorkbenchError("output_unknown", 404)
                if not self._original_output_is_eligible(entry, receipt, now_ms=now_ms):
                    raise ProductionWorkbenchError("artifact_not_ready", 422)
                claims.append(
                    ProductionAuthoringOutputClaim(
                        workspace_handle=handle,
                        workspace_id=entry.workspace.workspace_id,
                        workspace_revision=entry.workspace.revision,
                        workspace_fingerprint=entry.workspace.fingerprint,
                        segment_id=segment_id,
                        output_handle=output_handle,
                        receipt=receipt,
                        store=store,
                        lineage_token=entry.lineage_token,
                        _registry=self,
                    )
                )
            return tuple(claims)

    @contextmanager
    def authoring_output_commit_guard(
        self, claims: tuple[ProductionAuthoringOutputClaim, ...]
    ) -> Iterator[bool]:
        """Retain exact batch authority through the caller's metadata-only publication."""

        # CRITICAL: a per-claim check releases this lock too early and lets Production revoke
        # the source before Authoring publishes it. Keep Authoring -> Production ordering;
        # callers must not acquire generated-source locks or perform media work inside this guard.
        with self._lock:
            self._prune(self._clock())
            now_ms = self._clock_ms()
            yield bool(claims) and all(
                type(claim) is ProductionAuthoringOutputClaim
                and claim._registry is self
                and (entry := self._entries.get(claim.workspace_handle)) is not None
                and self._raw_claim_matches_entry(claim, entry, now_ms=now_ms)
                for claim in claims
            )

    def authoring_output_claim_is_current(self, claim: ProductionAuthoringOutputClaim) -> bool:
        if type(claim) is not ProductionAuthoringOutputClaim or claim._registry is not self:
            return False
        with self._lock:
            self._prune(self._clock())
            entry = self._entries.get(claim.workspace_handle)
            return entry is not None and self._raw_claim_matches_entry(
                claim, entry, now_ms=self._clock_ms()
            )

    def _rollback_unpublished_creation(
        self,
        *,
        request_id: str,
        workspace_handle: str,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
    ) -> None:
        """Remove one exact create that never became a published managed run."""

        with self._lock:
            entry = self._entries.get(workspace_handle)
            ledger = self._ledger.get(request_id)
            if entry is None or ledger is None:
                raise ProductionWorkbenchError("rollback_authority_mismatch", 409)
            projection = self._projection(workspace_handle, entry)
            if (
                ledger.action != "create_workspace_from_context"
                or ledger.workspace_handle != workspace_handle
                or ledger.committed_revision != expected_workspace_revision
                or ledger.committed_fingerprint != expected_workspace_fingerprint
                or projection.workspace_revision != expected_workspace_revision
                or projection.workspace_fingerprint != expected_workspace_fingerprint
                or projection.generation_sequence is not None
                or (
                    self._live_sequence_probe is not None
                    and self._live_sequence_probe(workspace_handle)
                )
            ):
                raise ProductionWorkbenchError("rollback_authority_mismatch", 409)
            # CRITICAL: this is transaction rollback, not user-visible release. Remove the exact
            # create ledger with the unpublished workspace and do not leave a tombstone; retaining
            # either makes the same aggregate retry replay a handle that no longer exists.
            self._cancel_entry_assembly(entry)
            self._entries.pop(workspace_handle)
            self._ledger.pop(request_id)

    def _rollback_unpublished_managed_creation(
        self,
        *,
        request_id: str,
        workspace_handle: str,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
        expected_sequence_state_fingerprint: str,
        release_live_sequence: Callable[[], None],
    ) -> None:
        """Undo one exact child bind that never became parent-visible authority."""

        if not callable(release_live_sequence):
            raise ProductionWorkbenchError("rollback_authority_mismatch", 409)
        with self._lock:
            entry = self._entries.get(workspace_handle)
            ledger = self._ledger.get(request_id)
            if entry is None or ledger is None:
                raise ProductionWorkbenchError("rollback_authority_mismatch", 409)
            projection = self._projection(workspace_handle, entry)
            sequence = entry.accepted_authorities.generation_sequence
            if (
                ledger.action != "create_workspace_from_context"
                or ledger.workspace_handle != workspace_handle
                or ledger.committed_revision != expected_workspace_revision
                or ledger.committed_fingerprint != expected_workspace_fingerprint
                or projection.workspace_revision != expected_workspace_revision
                or projection.workspace_fingerprint != expected_workspace_fingerprint
                or sequence is None
                or sequence.state.fingerprint != expected_sequence_state_fingerprint
                or projection.outputs
            ):
                raise ProductionWorkbenchError("rollback_authority_mismatch", 409)
            # CRITICAL: release the exact reserved child while Production owns the lock. This
            # preserves coordinator -> Production -> ManagedRun ordering and lets the live probe
            # prove no sequence remains before the workspace is removed.
            release_live_sequence()
            if self._live_sequence_probe is not None and self._live_sequence_probe(
                workspace_handle
            ):
                raise ProductionWorkbenchError("rollback_authority_mismatch", 409)
            self._cancel_entry_assembly(entry)
            self._entries.pop(workspace_handle)
            self._ledger.pop(request_id)

    def _release_managed_child_workspace(
        self,
        *,
        creation_request_id: str,
        workspace_handle: str,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
        expected_sequence_state_fingerprint: str,
        release_live_sequence: Callable[[], None],
    ) -> None:
        """Atomically release one exact M26 child run and its private Production row."""

        if not callable(release_live_sequence):
            raise ProductionWorkbenchError("managed_child_release_authority_mismatch", 409)
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(workspace_handle)
            if entry is None:
                self._terminal_or_missing(workspace_handle)
        if not entry.mutation_claim.acquire(timeout=self._mutation_timeout_seconds):
            raise ProductionWorkbenchError("workspace_busy", 423)
        try:
            now = self._clock()
            with self._lock:
                self._prune(now)
                current = self._entries.get(workspace_handle)
                ledger = self._ledger.get(creation_request_id)
                if current is None or current is not entry or ledger is None:
                    raise ProductionWorkbenchError("managed_child_release_authority_mismatch", 409)
                projection = self._projection(workspace_handle, entry)
                sequence = entry.accepted_authorities.generation_sequence
                if (
                    ledger.action != "create_workspace_from_context"
                    or ledger.workspace_handle != workspace_handle
                    or projection.workspace_revision != expected_workspace_revision
                    or projection.workspace_fingerprint != expected_workspace_fingerprint
                    or sequence is None
                    or sequence.state.fingerprint != expected_sequence_state_fingerprint
                ):
                    raise ProductionWorkbenchError("managed_child_release_authority_mismatch", 409)
                # CRITICAL: Production owns the mutation claim before the release service drops
                # the exact ManagedRun. No caller may release these two authorities separately;
                # doing so leaks one workspace per serial child or strands a live run without its
                # workspace. The callback is supplied only by ManagedRunReleaseService after its
                # centralized M23-51 decision.
                if self._editor_recovery_protected is not None and self._editor_recovery_protected(
                    workspace_handle
                ):
                    raise ProductionWorkbenchError("dirty_recovery_protected", 409)
                release_live_sequence()
                if self._live_sequence_probe is not None and self._live_sequence_probe(
                    workspace_handle
                ):
                    raise ProductionWorkbenchError("managed_child_release_authority_mismatch", 409)
                self._cancel_entry_assembly(entry)
                self._entries.pop(workspace_handle)
                self._notify_editor_recovery(workspace_handle)
                self._record_tombstone(workspace_handle, _Tombstone("released", now))
        finally:
            entry.mutation_claim.release()

    def _prune(self, now: float) -> None:
        for request_id, admission in tuple(self._admissions.items()):
            expired_open = admission.state == "open" and now >= admission.expires_at
            if expired_open or (
                admission.closed_at is not None
                and now - admission.closed_at >= PRODUCTION_DESTINATION_ADMISSION_TTL_SECONDS
            ):
                self._admissions.pop(request_id, None)
                if (
                    expired_open
                    and admission.protocol == "v2"
                    and admission.workspace_handle is not None
                    and (
                        admission.workspace_handle in self._entries
                        or admission.workspace_handle in self._empty_projects
                    )
                ):
                    # Expiry removes a visible admitted attempt just like explicit release. Keep
                    # the surviving envelope revision aligned with its new fingerprint.
                    self._advance_project_revision(admission.workspace_handle)
        leased = {
            admission.workspace_handle
            for admission in self._admissions.values()
            if admission.state == "open" and admission.workspace_handle is not None
        }
        # CRITICAL: an admitted destination or a live run is a lease on its project. Expiring the
        # entry by touch time alone deletes a project while a Start targets it or a detached run
        # can still publish into it. Both leases are bounded: admissions by their TTL, runs by the
        # ManagedRun registry's retention. The probe keeps Production -> ManagedRun ordering and is
        # asked only for entries already past their TTL.
        expired_handles = [
            handle
            for handle, entry in self._entries.items()
            if now - entry.touched_at >= self._ttl_seconds
            and handle not in leased
            and not (self._live_sequence_probe is not None and self._live_sequence_probe(handle))
            # CRITICAL: dirty content/pending IO owns a bounded recovery lease before TTL prune.
            and not (
                self._editor_recovery_protected is not None
                and self._editor_recovery_protected(handle)
            )
        ]
        for handle in expired_handles:
            entry = self._entries.pop(handle, None)
            self._notify_editor_recovery(handle)
            if entry is not None:
                self._cancel_entry_assembly(entry)
            self._project_revisions.pop(handle, None)
            self._record_tombstone(handle, _Tombstone("expired", now))
        expired_empty_handles = [
            handle
            for handle, project in self._empty_projects.items()
            if now - project.touched_at >= self._ttl_seconds
            and handle not in leased
            # CRITICAL: the first live generation owns an empty destination after its admission
            # is consumed. Dropping that lease here deletes the project before verified promotion.
            and not (self._live_sequence_probe is not None and self._live_sequence_probe(handle))
            and not (
                self._editor_recovery_protected is not None
                and self._editor_recovery_protected(handle)
            )
        ]
        for handle in expired_empty_handles:
            self._empty_projects.pop(handle, None)
            self._notify_editor_recovery(handle)
            self._project_revisions.pop(handle, None)
            self._record_tombstone(handle, _Tombstone("expired", now))
        expired_stages = [
            handle
            for handle, item in self._staged.items()
            if now - item.staged_at >= PRODUCTION_STAGE_TTL_SECONDS
        ]
        for handle in expired_stages:
            self._staged.pop(handle, None)
        expired_tombstones = [
            handle
            for handle, item in self._tombstones.items()
            if now - item.terminated_at >= self._terminal_ttl_seconds
        ]
        for handle in expired_tombstones:
            self._tombstones.pop(handle, None)
        for handle, project in tuple(self._editable_projects.items()):
            if now - project.touched_at >= self._ttl_seconds and not (
                self._editor_recovery_protected is not None
                and self._editor_recovery_protected(handle)
            ):
                self._editable_projects.pop(handle)
                self._notify_editor_recovery(handle)
                self._record_tombstone(handle, _Tombstone("expired", now))
        retained_handles = set(self._entries) | set(self._editable_projects) | set(self._tombstones)
        for handle in tuple(self._authoring_anchors):
            if (
                handle not in self._entries
                and handle not in self._empty_projects
                and handle not in self._editable_projects
            ):
                self._authoring_anchors.pop(handle, None)
        for request_id in tuple(self._ledger):
            if self._ledger[request_id].workspace_handle not in retained_handles:
                self._ledger.pop(request_id, None)

    @staticmethod
    def _assembly_predecessors_ready(entry: _ProductionEntry) -> bool:
        automatic = entry.automatic_plan
        sequence = entry.accepted_authorities.generation_sequence
        artifacts = entry.accepted_authorities.artifact_receipts
        if (
            automatic is None
            or sequence is None
            or entry.artifact_store is None
            or entry.assembly_runtime is None
            or entry.accepted_authorities.reconstruction_receipt is not None
            or entry.accepted_authorities.assembly_execution is not None
            or not assembly_start_holds_satisfied(automatic, sequence, artifacts)
            or automatic.workspace is not entry.workspace
            or sequence.state.plan.source_workspace_authority is not automatic.workspace
            or sequence.state.plan.manifest_fingerprints != automatic.manifest_fingerprints
            or tuple(item.segment_id for item in artifacts) != automatic.reconstruction_order
            or tuple(item.manifest_fingerprint for item in artifacts)
            != automatic.manifest_fingerprints
            or len(automatic.cut_boundary_receipts) != len(artifacts) - 1
        ):
            return False
        terminal_by_job = {item.job_id: item.state for item in sequence.state.runtimes}
        return all(
            item.output_fingerprint is not None
            and (
                item.segment_id in sequence.state.plan.clean_segment_ids
                or terminal_by_job.get(
                    next(
                        (
                            job.job_id
                            for job in sequence.state.plan.jobs
                            if job.segment_id == item.segment_id
                        ),
                        "",
                    )
                )
                is GenerationJobState.SUCCEEDED
            )
            for item in artifacts
        )

    def _assembly_projection(
        self,
        entry: _ProductionEntry,
    ) -> ProductionAssemblyProjection | None:
        if entry.automatic_plan is None:
            return entry.completed_assembly
        if entry.assembly_job is not None:
            return entry.assembly_job.projection()
        runtime = entry.assembly_runtime
        if runtime is None:
            return ProductionAssemblyProjection(failure_code="media_runtime_not_authorized")
        if not self._assembly_predecessors_ready(entry):
            return ProductionAssemblyProjection(failure_code="predecessor_not_ready")
        sequence = entry.accepted_authorities.generation_sequence
        if sequence is None:  # pragma: no cover - proved by readiness
            return ProductionAssemblyProjection(failure_code="predecessor_not_ready")
        return ProductionAssemblyProjection(
            capability_fingerprint=runtime.capability.fingerprint,
            managed_sequence_fingerprint=canonical_fingerprint(sequence.to_wire()),
            artifact_receipt_fingerprints=tuple(
                item.fingerprint for item in entry.accepted_authorities.artifact_receipts
            ),
            cut_boundary_receipt_fingerprints=tuple(
                item.fingerprint for item in entry.automatic_plan.cut_boundary_receipts
            ),
            total=len(entry.accepted_authorities.artifact_receipts),
            failure_code=None,
        )

    def _member_row_live(self, row: _MemberRow) -> bool:
        """Whether a member attempt can still be advanced by a run that owns it."""

        if row.latest.terminal:
            return False
        if not row.run_bound:
            return True
        if row.run_handle is None:
            return False
        probe = self._live_run_probe
        return True if probe is None else bool(probe(row.run_handle))

    def _member_projection_fields(self, entry: _ProductionEntry) -> _AcceptedProjectionFields:
        members = _EMPTY_MEMBERS if entry.members is None else entry.members
        closure_states: dict[str, str] = {}
        job_states: dict[str, str] = {}
        artifact_states: dict[str, str] = {}
        delivered_geometry: dict[str, ProductionDeliveredVideoProjection] = {}
        outputs: list[ProductionOutputProjection] = []
        run_total = 0
        run_completed = 0
        any_live = False
        newest: tuple[_MemberRow, bool] | None = None
        # A provisional automatic attempt deliberately has no workspace segment. It still owns
        # the aggregate run status, but must never leak into duration, selection or output rows.
        for row in members.values():
            live = self._member_row_live(row)
            run_total += 1
            if live:
                any_live = True
            else:
                run_completed += 1
            if newest is None or row.order > newest[0].order:
                newest = (row, live)
        for segment in entry.workspace.segments:
            member_row = members.get(segment.segment_id)
            if member_row is None:
                continue
            latest = member_row.latest
            live = self._member_row_live(member_row)
            closure_states[member_row.segment_id] = latest.closure_state
            # IMPORTANT: a non-terminal attempt whose run no longer exists can never be advanced.
            # Projecting its stale job state would show a Start as running forever and block retry.
            job_states[member_row.segment_id] = (
                latest.job_state.value if latest.terminal or live else "unknown_ownership"
            )
            completed = member_row.completed
            receipt = None if completed is None else completed.artifact_receipt
            if receipt is None:
                continue
            artifact_states[member_row.segment_id] = receipt.state.value
            if (
                receipt.state is ArtifactLifecycleState.COMPLETE
                and receipt.format_label in {"mp4", "mkv", "webm"}
                and len(receipt.shape) == 4
                and receipt.shape[3] == 3
            ):
                delivered_geometry[member_row.segment_id] = ProductionDeliveredVideoProjection(
                    format_label=receipt.format_label,
                    frame_count=receipt.shape[0],
                    height=receipt.shape[1],
                    width=receipt.shape[2],
                )
            if receipt.state is ArtifactLifecycleState.COMPLETE and receipt.format_label in {
                "mp4",
                "mkv",
                "webm",
            }:
                output_handle = _opaque_artifact_output_handle(receipt)
                outputs.append(
                    ProductionOutputProjection(
                        output_handle=output_handle,
                        ordinal=len(outputs) + 1,
                        state="ready",
                        segment_id=member_row.segment_id,
                        preview=output_handle in entry.preview_sources,
                    )
                )
        run_state = "unavailable"
        if any_live:
            run_state = "running"
        elif newest is not None:
            state = newest[0].latest.job_state
            if state is GenerationJobState.SUCCEEDED:
                run_state = "succeeded"
            elif (
                state is GenerationJobState.CANCELLED
                or newest[0].latest.generation_sequence.state.cancellation_requested
            ):
                run_state = "cancelled"
            else:
                run_state = "failed"
        if not set(entry.preview_sources).issubset(item.output_handle for item in outputs):
            raise ProductionWorkbenchError("accepted_authority_mismatch", 422)
        return _AcceptedProjectionFields(
            run_state=run_state,
            run_completed=run_completed,
            run_total=run_total,
            generation_sequence=None,
            reconstruction_state="unavailable",
            authority_versions=(
                ("h3.context.segment_artifact_receipt.v1",) if artifact_states else ()
            ),
            outputs=tuple(outputs),
            generation_action_available=False,
            closure_states=closure_states,
            job_states=job_states,
            artifact_states=artifact_states,
            continuity_states={},
            delivered_geometry=delivered_geometry,
        )

    def _projection(self, handle: str, entry: _ProductionEntry) -> ProductionWorkbenchProjection:
        fields = (
            self._member_projection_fields(entry)
            if entry.members is not None
            else _accepted_projection_fields(
                entry.workspace,
                entry.accepted_authorities,
                entry.preview_sources,
            )
        )
        authoring_import_available = (
            entry.lineage_token is not None
            and type(entry.artifact_store) is PrivateSegmentArtifactStore
            and any(
                self._original_output_is_eligible(entry, receipt, now_ms=self._clock_ms())
                and any(
                    output.segment_id == receipt.segment_id
                    and output.output_handle == _opaque_artifact_output_handle(receipt)
                    and output.state == "ready"
                    for output in fields.outputs
                )
                for receipt in self._retained_original_receipts(entry)
            )
        )
        return build_production_workbench_projection(
            entry.workspace,
            workspace_handle=handle,
            run_state=fields.run_state,
            run_completed=fields.run_completed,
            run_total=fields.run_total,
            generation_sequence=fields.generation_sequence,
            reconstruction_state=fields.reconstruction_state,
            authority_versions=fields.authority_versions,
            outputs=fields.outputs,
            preview_available=bool(entry.preview_sources),
            generation_action_available=fields.generation_action_available,
            authoring_import_available=authoring_import_available,
            closure_states=fields.closure_states,
            job_states=fields.job_states,
            artifact_states=fields.artifact_states,
            continuity_states=fields.continuity_states,
            delivered_geometry=fields.delivered_geometry,
            assembly=self._assembly_projection(entry),
        )

    @staticmethod
    def _editable_projection(
        handle: str, entry: _EditableProjectEntry
    ) -> ProductionWorkbenchProjection | None:
        if not entry.document.segments:
            return None
        boundaries = {
            "independent": "independent",
            "predecessor": "native_handoff",
            "adjacent_pair": "adjacent",
            "cut": "cut",
            "reset": "reset",
        }
        rows = []
        for ordinal, segment in enumerate(entry.document.segments, 1):
            duration = resolve_milliseconds(segment.duration_milliseconds)
            rows.append(
                ProductionSegmentProjection(
                    segment.segment_id,
                    ordinal,
                    segment.task_mode,
                    segment.duration_milliseconds,
                    duration.delivered_milliseconds,
                    duration.frame_count,
                    duration.snapped,
                    segment.relation,
                    segment.predecessor_segment_id,
                    boundaries[segment.relation],
                )
            )
        actions: tuple[str, ...] = (
            "read_projection",
            "release_workspace",
            "set_selection",
            "set_segment_relation",
        )
        if len(rows) > 1:
            actions += ("delete_segment", "reorder_segments")
        return ProductionWorkbenchProjection(
            handle,
            entry.workspace_id,
            entry.revision,
            entry.fingerprint,
            tuple(rows),
            entry.document.selection,
            "unavailable",
            0,
            0,
            None,
            "unavailable",
            (),
            (),
            actions,
            ("source_relink_required",),
        )

    def _dispatch_editable(
        self, action: dict[str, object], digest: str, handle: str, entry: _EditableProjectEntry
    ) -> ProductionDispatchResult:
        name = str(action["action"])
        payload = cast(dict[str, object], action["payload"])
        projection = self._editable_projection(handle, entry)
        # SECURITY: refuse before claiming Context/member sources; imported bytes are never grants.
        permitted = {
            "read_projection",
            "release_workspace",
            "set_selection",
            "set_segment_relation",
            "delete_segment",
            "reorder_segments",
        }
        if name not in permitted or projection is None:
            raise ProductionWorkbenchError("editable_project_requires_context", 409)
        now = self._clock()
        if name == "read_projection":
            entry.touched_at = now
            return ProductionDispatchResult(200, projection)
        replay = self._replay(request_id=str(action["request_id"]), request_digest=digest)
        if replay is not None:
            return replay
        if (
            payload["expected_workspace_revision"] != entry.revision
            or payload["expected_workspace_fingerprint"] != entry.fingerprint
        ):
            return ProductionDispatchResult(409, projection, "workspace_conflict")
        if name == "release_workspace":
            if self._editor_recovery_protected is not None and self._editor_recovery_protected(
                handle
            ):
                raise ProductionWorkbenchError("dirty_recovery_protected", 409)
            self._editable_projects.pop(handle)
            self._notify_editor_recovery(handle)
            self._record_tombstone(
                handle, _Tombstone("released", now, str(action["request_id"]), digest)
            )
            return ProductionDispatchResult(204)
        wire = entry.document.to_wire()
        production = cast(dict[str, object], wire["production"])
        rows = cast(list[dict[str, object]], production["segments"])
        if name == "set_selection":
            production["selection"] = payload["segment_ids"]
        elif name == "reorder_segments":
            ordered = cast(list[str], payload["segment_ids"])
            if len(ordered) != len(rows) or set(ordered) != {row["segment_id"] for row in rows}:
                raise ProductionWorkbenchError("invalid_order", 400)
            by_id = {row["segment_id"]: row for row in rows}
            production["segments"] = [by_id[key] for key in ordered]
        else:
            row = next((row for row in rows if row["segment_id"] == payload["segment_id"]), None)
            if row is None:
                raise ProductionWorkbenchError("segment_unavailable", 404)
            if name == "delete_segment":
                if len(rows) == 1:
                    raise ProductionWorkbenchError("last_segment", 409)
                rows.remove(row)
                production["selection"] = [
                    key for key in entry.document.selection if key != row["segment_id"]
                ]
            else:
                row["relation"] = payload["relation"]
                row["predecessor_segment_id"] = payload["predecessor_segment_id"]
        try:
            segments = cast(list[dict[str, object]], production["segments"])
            used: set[object] = {
                key for row in segments for key in cast(list[str], row["reference_asset_ids"])
            }
            used.update(
                row["source_asset_id"] for row in segments if row["source_asset_id"] is not None
            )
            if wire["editor"] is not None:
                editor = cast(dict[str, object], wire["editor"])
                used.update(
                    row["asset_id"]
                    for row in cast(list[dict[str, object]], editor["assets"])
                    if row["kind"] != "font"
                )
                reference = cast(dict[str, object], editor["reference"])
                used.update(
                    row["source_id"] for row in cast(list[dict[str, object]], reference["sources"])
                )
            wire["media"] = [
                row
                for row in cast(list[dict[str, object]], wire["media"])
                if row["asset_id"] in used
            ]
            candidate = decode_project_value(wire)
        except ValueError:
            raise ProductionWorkbenchError("invalid_project_edit", 409) from None
        self._ensure_ledger_capacity(str(action["request_id"]))
        updated = replace(
            entry,
            document=candidate,
            document_bytes=len(encode_project_document(candidate)),
            revision=entry.revision + 1,
            touched_at=now,
        )
        if (
            self._reserved_registry_bytes() - entry.document_bytes + updated.document_bytes
            > MAX_PRODUCTION_REGISTRY_BYTES
        ):
            raise ProductionWorkbenchError("workspace_capacity", 429)
        projection = self._editable_projection(handle, updated)
        self._editable_projects[handle] = updated
        self._notify_editor_recovery(handle)
        self._ledger[str(action["request_id"])] = _LedgerEntry(
            digest,
            name,
            handle,
            updated.revision,
            updated.fingerprint,
            now,
        )
        return ProductionDispatchResult(200, projection)

    def _accumulated_projection(
        self,
        handle: str,
        admission: _DestinationAdmission | None = None,
    ) -> ProductionAccumulatedProjectProjection:
        entry = self._entries.get(handle)
        empty = self._empty_projects.get(handle)
        if entry is None and empty is None:
            self._terminal_or_missing(handle)
        # IMPORTANT: reads and replays must project the same server-owned open attempt. Requiring
        # the caller to pass it makes one revision produce two fingerprints after admission.
        if admission is None or admission.state != "open":
            admission = self._open_admission_for_destination(handle)
        if entry is None:
            if empty is None:
                self._terminal_or_missing(handle)
            workspace_id = empty.workspace_id
        else:
            workspace_id = entry.workspace.workspace_id
        members = (
            entry.members
            if entry is not None and entry.members is not None
            else (_EMPTY_MEMBERS if empty is None else empty.members)
        )
        committed_ids = (
            set() if entry is None else {item.segment_id for item in entry.workspace.segments}
        )
        terminal = self._preprepare_terminals.get(handle)
        ordered: list[tuple[int, str, _MemberRow | _PreprepareTerminal]] = [
            (row.order, "member", row) for row in members.values()
        ]
        if terminal is not None:
            ordered.append((terminal.order, "preprepare", terminal))
        ordered.sort(key=lambda item: item[0], reverse=True)
        attempts: list[ProductionAccumulationAttemptProjection] = []
        if admission is not None and admission.state == "open":
            if (
                admission.candidate_id is None
                or admission.attempt_id is None
                or admission.segment_id is None
            ):
                raise ProductionWorkbenchError("member_publication_authority", 409)
            attempts.append(
                ProductionAccumulationAttemptProjection(
                    candidate_id=admission.candidate_id,
                    attempt_id=admission.attempt_id,
                    member_segment_id=admission.segment_id,
                    status="admitted",
                    recovery=None,
                )
            )
        for _order, kind, item in ordered:
            if kind == "preprepare":
                preprepare = cast(_PreprepareTerminal, item)
                if admission is not None and admission.candidate_id == preprepare.candidate_id:
                    continue
                attempts.append(
                    ProductionAccumulationAttemptProjection(
                        candidate_id=preprepare.candidate_id,
                        attempt_id=preprepare.attempt_id,
                        member_segment_id=preprepare.segment_id,
                        status=preprepare.status,
                        recovery=(
                            "retry" if preprepare.status in {"failed", "cancelled"} else None
                        ),
                    )
                )
                if len(attempts) == 2:
                    break
                continue
            row = cast(_MemberRow, item)
            if admission is not None and admission.candidate_id == row.segment_id:
                continue
            live = self._member_row_live(row)
            status = (
                row.latest.job_state.value if row.latest.terminal or live else "unknown_ownership"
            )
            # IMPORTANT: the coordinator retains confirmed host interruption as FAILED with a
            # closed failure code. Preserve that execution contract, but project it as cancelled
            # here or Production offers the wrong recovery explanation for an interrupted attempt.
            if status == "failed" and row.latest.runtime.failure_code == "host_interrupted":
                status = "cancelled"
            committed = (
                row.segment_id in committed_ids
                and row.completed is row.latest
                and status == "succeeded"
            )
            # The committed workspace already carries older successful members. Once a newer
            # attempt is visible, repeating an older success would displace the bounded recovery
            # candidate and make the latest status ambiguous.
            if committed and attempts:
                continue
            recovery = None
            # IMPORTANT: output verification retry reuses the coordinator-owned run and must stay
            # visible while that run is live; hiding it would force a second generation queue.
            if not committed and status == "output_verification_failed":
                recovery = "verify_output"
            elif not committed and not live:
                if status in {"failed", "timed_out", "cancelled"}:
                    recovery = "retry"
            attempts.append(
                ProductionAccumulationAttemptProjection(
                    candidate_id=row.segment_id,
                    attempt_id=row.run_handle or row.segment_id,
                    member_segment_id=row.segment_id,
                    status=status,
                    recovery=recovery,
                    committed=committed,
                )
            )
            if len(attempts) == 2:
                break
        revision = self._project_revisions.get(
            handle,
            1 if entry is None else entry.workspace.revision,
        )
        return ProductionAccumulatedProjectProjection(
            workspace_handle=handle,
            workspace_id=workspace_id,
            project_revision=revision,
            workspace=None if entry is None else self._projection(handle, entry),
            attempts=tuple(attempts),
        )

    def _advance_project_revision(self, handle: str) -> None:
        current = self._project_revisions.get(handle, 1)
        self._project_revisions[handle] = min(current + 1, 1_000_000)
        self._notify_editor_recovery(handle)

    def _refuse_member_mode(self, handle: str, entry: _ProductionEntry) -> None:
        # CRITICAL: whole-project writers replace every segment's generation authority at once.
        # Letting one run over member rows discards the earlier independent outputs they retain.
        if entry.members is not None:
            raise ProductionWorkbenchError(
                "member_mode_authority", 409, projection=self._projection(handle, entry)
            )

    def attach_accepted_authorities(
        self,
        handle: str,
        *,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
        authorities: ProductionAcceptedAuthorities,
    ) -> ProductionWorkbenchProjection:
        """Attach exact accepted M17 authority objects without creating a browser mutation wire."""

        if type(authorities) is not ProductionAcceptedAuthorities:
            raise TypeError("accepted authorities are invalid")
        handle = _workspace_handle(handle)
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(handle)
            if entry is None:
                self._terminal_or_missing(handle)
        if not entry.mutation_claim.acquire(timeout=self._mutation_timeout_seconds):
            raise ProductionWorkbenchError("workspace_busy", 423)
        try:
            with self._lock:
                current = self._entries.get(handle)
                if current is not entry:
                    self._terminal_or_missing(handle)
                self._refuse_member_mode(handle, entry)
                if (
                    expected_workspace_revision != entry.workspace.revision
                    or expected_workspace_fingerprint != entry.workspace.fingerprint
                ):
                    raise ProductionWorkbenchError(
                        "stale_workspace", 409, projection=self._projection(handle, entry)
                    )
                _validate_accepted_authorities(entry.workspace, authorities)
                completed_assembly = (
                    entry.completed_assembly
                    if authorities.assembly_execution
                    is entry.accepted_authorities.assembly_execution
                    else None
                )
                authority_bytes = _retained_authority_bytes(
                    entry.workspace,
                    authorities,
                    entry.preview_sources,
                    entry.automatic_plan,
                    completed_assembly,
                )
                aggregate = (
                    sum(item.authority_bytes for item in self._entries.values())
                    + sum(item.authority_bytes for item in self._staged.values())
                    - entry.authority_bytes
                    + authority_bytes
                )
                if (
                    authority_bytes > MAX_PRODUCTION_AUTHORITY_BYTES
                    or aggregate > MAX_PRODUCTION_REGISTRY_BYTES
                ):
                    raise ProductionWorkbenchError("workspace_capacity", 429)
                candidate = _ProductionEntry(
                    workspace=entry.workspace,
                    touched_at=now,
                    authority_bytes=authority_bytes,
                    accepted_authorities=authorities,
                    preview_sources=entry.preview_sources,
                    automatic_plan=entry.automatic_plan,
                    automatic_materializer=entry.automatic_materializer,
                    artifact_store=entry.artifact_store,
                    assembly_runtime=entry.assembly_runtime,
                    assembly_job=entry.assembly_job,
                    completed_assembly=completed_assembly,
                    mutation_claim=entry.mutation_claim,
                    lineage_token=entry.lineage_token,
                )
                projection = self._projection(handle, candidate)
                self._entries[handle] = candidate
                return projection
        finally:
            entry.mutation_claim.release()

    def _refuse_planned_import(self, handle: str, entry: _ProductionEntry) -> None:
        # IMPORTANT: a plan import replaces the whole workspace with the proposal. Over retained
        # members that would silently discard every accumulated output.
        if entry.members:
            raise ProductionWorkbenchError(
                "planned_project_required", 409, projection=self._projection(handle, entry)
            )

    def _automatic_import_replay(
        self,
        request: ProductionImportRequestV1,
        request_digest: str,
    ) -> ProductionAutomaticPlanDispatchResultV1 | None:
        previous = self._ledger.get(request.request_id)
        if previous is None:
            return None
        if previous.request_digest != request_digest or previous.action != "import_automatic_plan":
            raise ProductionWorkbenchError("request_id_conflict", 409)
        entry = self._entries.get(previous.workspace_handle)
        if entry is None:
            self._terminal_or_missing(previous.workspace_handle)
        if (
            entry.workspace.revision != previous.committed_revision
            or entry.workspace.fingerprint != previous.committed_fingerprint
            or entry.automatic_plan is None
        ):
            raise ProductionWorkbenchError(
                "replay_superseded",
                409,
                projection=self._projection(previous.workspace_handle, entry),
            )
        return ProductionAutomaticPlanDispatchResultV1(
            status=200,
            projection=project_production_automatic_plan(
                request.request_id,
                entry.automatic_plan,
            ),
            replayed=True,
        )

    def replay_automatic_plan_import(
        self,
        request: ProductionImportRequestV1,
    ) -> ProductionAutomaticPlanDispatchResultV1 | None:
        """Resolve a broker-owned exact import retry before claiming a possibly expired source."""
        if type(request) is not ProductionImportRequestV1:
            raise ProductionWorkbenchError("invalid_automatic_plan_import", 400)
        with self._lock:
            self._prune(self._clock())
            return self._automatic_import_replay(request, request.fingerprint)

    def import_automatic_plan(
        self,
        request: ProductionImportRequestV1,
        *,
        planning_context: ProductionPlanningContextV1 | ProductionPlanningContextV2,
        proposal: SegmentationProposalV1 | SegmentationProposalV2,
        materialize: MaterializeSegmentContext,
        source_guard: Callable[[], AbstractContextManager[None]] | None = None,
    ) -> ProductionAutomaticPlanDispatchResultV1:
        """Approve and atomically import one exact current M26 segmentation proposal.

        Materialization runs while this workspace's existing mutation claim is held, but the live
        entry is replaced only after every claim has been derived and released. Reads therefore
        see the old complete revision or the new complete revision, never partial segment rows.
        """

        if type(request) is not ProductionImportRequestV1 or not callable(materialize):
            raise ProductionWorkbenchError("invalid_automatic_plan_import", 400)
        request_digest = request.fingerprint
        handle = _workspace_handle(request.workspace_handle)
        now = self._clock()
        with self._lock:
            self._prune(now)
            # IMPORTANT: a request ID is a global idempotency key. Resolve its exact replay or
            # conflict before validating newly supplied authorities, or later source drift can
            # hide a deterministic request_id_conflict behind an unrelated stale-source error.
            replay = self._automatic_import_replay(request, request_digest)
            if replay is not None:
                return replay
        # IMPORTANT: legacy request IDs retain exact replay, but a new import must bind the
        # semantic V2 planning/proposal authority before materialization or workspace mutation.
        if (
            type(planning_context) is not ProductionPlanningContextV2
            or type(proposal) is not SegmentationProposalV2
        ):
            raise ProductionWorkbenchError("automatic_plan_semantic_authority_stale", 409)
        if (
            request.proposal_id != proposal.proposal_id
            or request.proposal_revision != proposal.revision
            or request.proposal_fingerprint != proposal.fingerprint
            or request.planning_context_fingerprint != planning_context.fingerprint
            or request.source_request_fingerprint != planning_context.source_request_fingerprint
            or request.source_profile_fingerprint != planning_context.source_profile_fingerprint
            or proposal.planning_context_fingerprint != planning_context.fingerprint
        ):
            raise ProductionWorkbenchError("automatic_plan_source_stale", 409)
        if not is_production_proposal_importable(proposal):
            raise ProductionWorkbenchError("automatic_plan_not_importable", 422)
        try:
            validate_current_segmentation_proposal(planning_context, proposal)
        except ValueError:
            raise ProductionWorkbenchError("automatic_plan_source_stale", 409) from None
        with self._lock:
            self._prune(now)
            entry = self._entries.get(handle)
            if entry is None:
                self._terminal_or_missing(handle)
            self._refuse_planned_import(handle, entry)
            if (
                request.workspace_id != entry.workspace.workspace_id
                or request.expected_workspace_revision != entry.workspace.revision
                or request.expected_workspace_fingerprint != entry.workspace.fingerprint
            ):
                raise ProductionWorkbenchError(
                    "stale_workspace",
                    409,
                    projection=self._projection(handle, entry),
                )

        if not entry.mutation_claim.acquire(timeout=self._mutation_timeout_seconds):
            raise ProductionWorkbenchError("workspace_busy", 423)
        try:
            with self._lock:
                self._prune(self._clock())
                current = self._entries.get(handle)
                if current is not entry:
                    self._terminal_or_missing(handle)
                replay = self._automatic_import_replay(request, request_digest)
                if replay is not None:
                    return replay
                self._refuse_planned_import(handle, entry)
                if (
                    request.workspace_id != entry.workspace.workspace_id
                    or request.expected_workspace_revision != entry.workspace.revision
                    or request.expected_workspace_fingerprint != entry.workspace.fingerprint
                ):
                    raise ProductionWorkbenchError(
                        "stale_workspace",
                        409,
                        projection=self._projection(handle, entry),
                    )
                self._ensure_ledger_capacity(request.request_id)

            receipts = []
            for segment in proposal.segments:
                claim = None
                derivation_error: ProductionWorkbenchError | None = None
                try:
                    claim = materialize(planning_context, proposal, segment)
                    receipt = derive_segment_context_materialization_receipt(
                        planning_context,
                        proposal,
                        segment,
                        claim,
                    )
                    receipts.append(receipt)
                except ProductionImportError:
                    derivation_error = ProductionWorkbenchError(
                        "segment_context_materialization_mismatch",
                        422,
                    )
                except Exception:
                    # SECURITY: materializer errors may contain prompts, media locators, paths or
                    # provider details. Collapse them to one content-free category.
                    derivation_error = ProductionWorkbenchError(
                        "segment_context_materialization_failed",
                        422,
                    )
                finally:
                    if type(claim) is SegmentContextMaterializationClaim:
                        try:
                            claim.release()
                        except Exception:
                            derivation_error = ProductionWorkbenchError(
                                "segment_context_release_failed",
                                422,
                            )
                if derivation_error is not None:
                    raise derivation_error

            try:
                authority = build_production_automatic_plan_authority(
                    entry.workspace,
                    planning_context,
                    proposal,
                    tuple(receipts),
                )
            except ProductionImportError as exc:
                raise ProductionWorkbenchError("automatic_plan_rejected", 422) from exc
            accepted = _EMPTY_ACCEPTED_AUTHORITIES
            previews = _EMPTY_PREVIEW_SOURCES
            authority_bytes = _retained_authority_bytes(
                authority.workspace,
                accepted,
                previews,
                authority,
            )
            now = self._clock()
            # CRITICAL: take the source guard before the final Production lock. Checking only
            # before materialization admits a source edit during preflight; reversing this lock
            # order deadlocks against source-owned publication. Retained replay bypasses it.
            with source_guard() if source_guard is not None else nullcontext(), self._lock:
                self._prune(now)
                current = self._entries.get(handle)
                if current is not entry:
                    self._terminal_or_missing(handle)
                self._refuse_planned_import(handle, entry)
                # CRITICAL: the mutation claim alone is not the commit authority. Recheck the
                # exact retained object and CAS immediately before publishing, or a prune/rebind
                # path can make a fully materialized plan overwrite a different workspace.
                if (
                    request.workspace_id != entry.workspace.workspace_id
                    or request.expected_workspace_revision != entry.workspace.revision
                    or request.expected_workspace_fingerprint != entry.workspace.fingerprint
                ):
                    raise ProductionWorkbenchError(
                        "stale_workspace",
                        409,
                        projection=self._projection(handle, entry),
                    )
                # IMPORTANT: materialization intentionally releases the registry lock. Recheck the
                # global ledger bound here because another workspace may fill the last slot while
                # receipts are derived; publishing first would make a capacity refusal non-atomic.
                self._ensure_ledger_capacity(request.request_id)
                aggregate = (
                    sum(item.authority_bytes for item in self._entries.values())
                    + sum(item.authority_bytes for item in self._staged.values())
                    - entry.authority_bytes
                    + authority_bytes
                )
                if (
                    authority_bytes > MAX_PRODUCTION_AUTHORITY_BYTES
                    or aggregate > MAX_PRODUCTION_REGISTRY_BYTES
                ):
                    raise ProductionWorkbenchError("workspace_capacity", 429)
                candidate = _ProductionEntry(
                    workspace=authority.workspace,
                    touched_at=now,
                    authority_bytes=authority_bytes,
                    accepted_authorities=accepted,
                    preview_sources=previews,
                    automatic_plan=authority,
                    automatic_materializer=_AutomaticSegmentMaterializer(
                        planning_context,
                        proposal,
                        materialize,
                    ),
                    artifact_store=None,
                    assembly_runtime=None,
                    assembly_job=None,
                    mutation_claim=entry.mutation_claim,
                    lineage_token=entry.lineage_token,
                )
                # Build both projections before publication so serialization/validation failure is
                # mutation-free and cannot expose a half-imported workspace.
                self._projection(handle, candidate)
                plan_projection = project_production_automatic_plan(
                    request.request_id,
                    authority,
                )
                self._entries[handle] = candidate
                self._publish_ledger(
                    request_id=request.request_id,
                    request_digest=request_digest,
                    action="import_automatic_plan",
                    handle=handle,
                    workspace=authority.workspace,
                    now=now,
                )
                return ProductionAutomaticPlanDispatchResultV1(
                    status=200,
                    projection=plan_projection,
                )
        finally:
            entry.mutation_claim.release()

    def claim_automatic_plan_authority(
        self,
        handle: str,
        *,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
        expected_plan_fingerprint: str,
    ) -> ProductionAutomaticPlanAuthority:
        """Return the exact immutable M26 authority to a typed internal successor only."""

        handle = _workspace_handle(handle)
        if (
            type(expected_plan_fingerprint) is not str
            or _FINGERPRINT.fullmatch(expected_plan_fingerprint) is None
        ):
            raise ProductionWorkbenchError("invalid_automatic_plan_fingerprint", 400)
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(handle)
            if entry is None:
                self._terminal_or_missing(handle)
            if (
                expected_workspace_revision != entry.workspace.revision
                or expected_workspace_fingerprint != entry.workspace.fingerprint
            ):
                raise ProductionWorkbenchError(
                    "stale_workspace",
                    409,
                    projection=self._projection(handle, entry),
                )
            if entry.automatic_plan is None:
                raise ProductionWorkbenchError("automatic_plan_unavailable", 404)
            if entry.automatic_plan.fingerprint != expected_plan_fingerprint:
                raise ProductionWorkbenchError("stale_automatic_plan", 409)
            return entry.automatic_plan

    @contextmanager
    def guard_automatic_plan_authority(
        self,
        handle: str,
        *,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
        expected_plan_fingerprint: str,
    ) -> Iterator[ProductionAutomaticPlanAuthority]:
        """Keep the current plan CAS stable during a bounded qualification publication."""
        # CRITICAL: callers acquire this guard before their qualification lock, and perform
        # no materialization under either lock. Reversing that order deadlocks a concurrent read.
        with self._lock:
            yield self.claim_automatic_plan_authority(
                handle,
                expected_workspace_revision=expected_workspace_revision,
                expected_workspace_fingerprint=expected_workspace_fingerprint,
                expected_plan_fingerprint=expected_plan_fingerprint,
            )

    def materialize_automatic_plan_segment(
        self,
        authority: ProductionAutomaticPlanAuthority,
        segment_id: str,
    ) -> tuple[SegmentContextMaterializationClaim, SegmentContextMaterializationReceiptV1]:
        """Re-materialize one exact retained segment when it becomes eligible.

        The returned Context claim stays live for the managed child binder. The caller owns its
        single release after bind success or compensation.
        """

        if type(authority) not in (
            ProductionAutomaticPlanAuthorityV1,
            ProductionAutomaticPlanAuthorityV2,
        ):
            raise ProductionWorkbenchError("invalid_automatic_plan_authority", 400)
        if type(segment_id) is not str or _IDENTIFIER.fullmatch(segment_id) is None:
            raise ProductionWorkbenchError("invalid_automatic_plan_segment_id", 400)
        now = self._clock()
        with self._lock:
            self._prune(now)
            retained = next(
                (
                    (handle, entry)
                    for handle, entry in self._entries.items()
                    if entry.automatic_plan is authority
                ),
                None,
            )
            if retained is None:
                raise ProductionWorkbenchError("automatic_plan_unavailable", 404)
            handle, entry = retained
            materializer = entry.automatic_materializer
            segment_rows = tuple(
                (segment, receipt)
                for segment, receipt in zip(
                    authority.proposal.segments,
                    authority.materialization_receipts,
                    strict=True,
                )
                if segment.segment_id == segment_id
            )
            if len(segment_rows) != 1:
                raise ProductionWorkbenchError("automatic_plan_segment_unavailable", 404)
            segment, retained_receipt = segment_rows[0]
            if materializer is None:
                raise ProductionWorkbenchError("automatic_plan_unavailable", 404)

        if not entry.mutation_claim.acquire(timeout=self._mutation_timeout_seconds):
            raise ProductionWorkbenchError("workspace_busy", 423)
        claim: SegmentContextMaterializationClaim | None = None
        failure: ProductionWorkbenchError | None = None
        receipt: SegmentContextMaterializationReceiptV1 | None = None
        try:
            with self._lock:
                self._prune(self._clock())
                current = self._entries.get(handle)
                if (
                    current is not entry
                    or entry.automatic_plan is not authority
                    or entry.automatic_materializer is not materializer
                ):
                    raise ProductionWorkbenchError("automatic_plan_unavailable", 409)
            try:
                # SECURITY: this callback owns private prompts and media locators. Never execute it
                # under the registry lock and never propagate its exception text.
                if (
                    type(authority) is ProductionAutomaticPlanAuthorityV1
                    and type(materializer.planning_context) is ProductionPlanningContextV1
                    and type(materializer.proposal) is SegmentationProposalV1
                ):
                    legacy_materialize = cast(
                        LegacyMaterializeSegmentContext,
                        materializer.materialize,
                    )
                    claim = legacy_materialize(
                        materializer.planning_context,
                        materializer.proposal,
                        segment,
                    )
                    receipt = derive_legacy_segment_context_materialization_receipt(
                        materializer.planning_context,
                        materializer.proposal,
                        segment,
                        claim,
                    )
                elif (
                    type(authority) is ProductionAutomaticPlanAuthorityV2
                    and type(materializer.planning_context) is ProductionPlanningContextV2
                    and type(materializer.proposal) is SegmentationProposalV2
                ):
                    claim = materializer.materialize(
                        materializer.planning_context,
                        materializer.proposal,
                        segment,
                    )
                    receipt = derive_segment_context_materialization_receipt(
                        materializer.planning_context,
                        materializer.proposal,
                        segment,
                        claim,
                    )
                else:
                    raise ProductionImportError("segment_context_materialization_authority")
            except ProductionImportError:
                failure = ProductionWorkbenchError(
                    "segment_context_materialization_mismatch",
                    422,
                )
            except Exception:
                failure = ProductionWorkbenchError(
                    "segment_context_materialization_failed",
                    422,
                )
            if failure is None:
                with self._lock:
                    self._prune(self._clock())
                    current = self._entries.get(handle)
                    if (
                        current is not entry
                        or entry.automatic_plan is not authority
                        or entry.automatic_materializer is not materializer
                    ):
                        failure = ProductionWorkbenchError("automatic_plan_unavailable", 409)
                    elif receipt != retained_receipt:
                        failure = ProductionWorkbenchError(
                            "segment_context_materialization_mismatch",
                            422,
                        )
                    else:
                        entry.touched_at = self._clock()
            if failure is not None:
                if type(claim) is SegmentContextMaterializationClaim and not claim.released:
                    try:
                        claim.release()
                    except Exception:
                        failure = ProductionWorkbenchError(
                            "segment_context_release_failed",
                            422,
                        )
                raise failure
            if type(claim) is not SegmentContextMaterializationClaim or receipt is None:
                raise ProductionWorkbenchError("segment_context_materialization_failed", 422)
            return claim, receipt
        finally:
            entry.mutation_claim.release()

    def claim_workspace_authority(
        self,
        handle: str,
        *,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
    ) -> MultiSegmentWorkspace:
        """Return one exact retained workspace for an internal typed producer.

        This is deliberately not a browser action. The caller receives the immutable canonical
        authority only after the same handle/CAS checks used by Production mutations.
        """

        handle = _workspace_handle(handle)
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(handle)
            if entry is None:
                self._terminal_or_missing(handle)
            if (
                expected_workspace_revision != entry.workspace.revision
                or expected_workspace_fingerprint != entry.workspace.fingerprint
            ):
                raise ProductionWorkbenchError(
                    "stale_workspace",
                    409,
                    projection=self._projection(handle, entry),
                )
            return entry.workspace

    def assert_planning_context_source(
        self, workspace: MultiSegmentWorkspace, context_handle: str
    ) -> None:
        """Join the planning source to an exact Context seed already present in Production."""
        try:
            seed = self._seed_claim(_sidebar_handle(context_handle))
        except KeyError:
            raise ProductionWorkbenchError("planning_source_unavailable", 404) from None
        # CRITICAL: two live handles are not a provenance join. Compare the full retained
        # declaration so a foreign Context cannot replace this workspace's accepted source.
        if not self._source_matches_seed(workspace, seed):
            raise ProductionWorkbenchError("planning_source_mismatch", 409)

    @staticmethod
    def project_managed_child_generation(
        workspace: MultiSegmentWorkspace,
        handle: str,
        sequence: GenerationSequenceProjection,
        receipt: SegmentArtifactReceipt | None = None,
    ) -> ProductionWorkbenchProjection:
        """Derive a child response without installing partial progress in its parent."""
        authorities = ProductionAcceptedAuthorities(
            generation_sequence=sequence,
            artifact_receipts=() if receipt is None else (receipt,),
        )
        _validate_accepted_authorities(workspace, authorities)
        fields = _accepted_projection_fields(workspace, authorities, _EMPTY_PREVIEW_SOURCES)
        # CRITICAL: clean siblings describe this child's execution closure, not completed parent
        # work. This matching nested response must never replace the original registry entry.
        return build_production_workbench_projection(
            workspace,
            workspace_handle=handle,
            run_state=fields.run_state,
            run_completed=fields.run_completed,
            run_total=fields.run_total,
            generation_sequence=fields.generation_sequence,
            reconstruction_state=fields.reconstruction_state,
            authority_versions=fields.authority_versions,
            outputs=fields.outputs,
            generation_action_available=fields.generation_action_available,
            closure_states=fields.closure_states,
            job_states=fields.job_states,
            artifact_states=fields.artifact_states,
            continuity_states=fields.continuity_states,
            delivered_geometry=fields.delivered_geometry,
        )

    def prepare_generated_preview_sources(
        self,
        sequence: GenerationSequenceProjection,
        receipts: tuple[SegmentArtifactReceipt, ...],
        store: PrivateSegmentArtifactStore,
    ) -> Mapping[str, MediaPreviewSourceAuthority]:
        """Admit private bytes before the caller enters its lifecycle/CAS lock."""
        try:
            from .comfyui_media_preview import ensure_media_preview_route_registered

            available = ensure_media_preview_route_registered()
        except Exception:
            available = False
        if not available:
            return _EMPTY_PREVIEW_SOURCES
        deadline = self._clock() + PRODUCTION_GENERATED_PREVIEW_ADMISSION_SECONDS
        candidates = tuple(
            preview
            for receipt in receipts
            if (
                preview := _try_generated_preview_source(
                    generation_sequence=sequence,
                    receipt=receipt,
                    store=store,
                    opaque_output_handle=_opaque_artifact_output_handle(receipt),
                    deadline=deadline,
                    clock=self._clock,
                )
            )
            is not None
        )
        return _bounded_preview_sources(candidates)

    @staticmethod
    def bind_prepared_preview_sources(
        sequence: GenerationSequenceProjection,
        receipts: tuple[SegmentArtifactReceipt, ...],
        source: MediaPreviewSourceAuthority | None,
    ) -> Mapping[str, MediaPreviewSourceAuthority]:
        if source is None:
            return _EMPTY_PREVIEW_SOURCES
        matching = tuple(
            item for item in receipts if item.fingerprint == source.receipt_fingerprint
        )
        if not matching or not any(
            runtime.state is GenerationJobState.SUCCEEDED
            and runtime.artifact_receipt_fingerprint == source.receipt_fingerprint
            for runtime in sequence.state.runtimes
        ):
            return _EMPTY_PREVIEW_SOURCES
        # CRITICAL: only factory-owned preview authority for this exact plan/receipt may bypass
        # repeated disk verification during coordinator CAS. Render still re-verifies stored bytes.
        if (
            type(source) is not MediaPreviewSourceAuthority
            or source.plan_fingerprint != sequence.state.plan.fingerprint
            or source.opaque_output_handle != _opaque_artifact_output_handle(matching[0])
        ):
            raise ProductionWorkbenchError("accepted_authority_mismatch", 422)
        return _bounded_preview_sources((source,))

    def _checked_prepared_preview_sources(
        self,
        sequence: GenerationSequenceProjection,
        receipts: tuple[SegmentArtifactReceipt, ...],
        sources: Mapping[str, MediaPreviewSourceAuthority],
    ) -> Mapping[str, MediaPreviewSourceAuthority]:
        _preview_sources_wire_bytes(sources)
        checked = _bounded_preview_sources(
            tuple(
                bound
                for source in sources.values()
                for bound in self.bind_prepared_preview_sources(sequence, receipts, source).values()
            )
        )
        if dict(checked) != dict(sources):
            raise ProductionWorkbenchError("accepted_authority_mismatch", 422)
        return checked

    def replace_generation_authority(
        self,
        handle: str,
        *,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
        expected_sequence_state_fingerprint: str | None,
        generation_sequence: GenerationSequenceProjection,
        artifact_receipts: tuple[SegmentArtifactReceipt, ...] = (),
        artifact_store: PrivateSegmentArtifactStore | None = None,
        expected_automatic_authority: ProductionAutomaticPlanAuthorityV2 | None = None,
        _prepared_preview_sources: Mapping[str, MediaPreviewSourceAuthority] | None = None,
    ) -> ProductionWorkbenchProjection:
        """CAS-replace only the managed generation/segment-artifact authority pair.

        The coordinator may also replace only the preview sources derived from that exact
        generation/artifact pair. It cannot overwrite continuity or reconstruction ownership; a
        workspace that crossed those later boundaries belongs to their owning lifecycle.
        """

        if type(generation_sequence) is not GenerationSequenceProjection:
            raise TypeError("generation sequence authority is invalid")
        if type(artifact_receipts) is not tuple or not all(
            type(item) is SegmentArtifactReceipt for item in artifact_receipts
        ):
            raise TypeError("artifact receipt authority is invalid")
        if artifact_store is not None and type(artifact_store) is not PrivateSegmentArtifactStore:
            raise TypeError("artifact store authority is invalid")
        handle = _workspace_handle(handle)
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(handle)
            if entry is None:
                self._terminal_or_missing(handle)
        if not entry.mutation_claim.acquire(timeout=self._mutation_timeout_seconds):
            raise ProductionWorkbenchError("workspace_busy", 423)
        try:
            with self._lock:
                current = self._entries.get(handle)
                if current is not entry:
                    self._terminal_or_missing(handle)
                self._refuse_member_mode(handle, entry)
                if (
                    expected_workspace_revision != entry.workspace.revision
                    or expected_workspace_fingerprint != entry.workspace.fingerprint
                ):
                    raise ProductionWorkbenchError(
                        "stale_workspace",
                        409,
                        projection=self._projection(handle, entry),
                    )
                retained = entry.accepted_authorities
                retained_sequence = retained.generation_sequence
                if expected_automatic_authority is not None:
                    if (
                        entry.automatic_plan is not expected_automatic_authority
                        or entry.workspace is not expected_automatic_authority.workspace
                        or generation_sequence.state.plan.source_workspace_authority
                        is not entry.workspace
                    ):
                        raise ProductionWorkbenchError("stale_automatic_plan", 409)
                    # CRITICAL: parent publication may succeed before reservation release fails.
                    # Accept only this exact owned replay; never overwrite later assembly state.
                    if (
                        retained_sequence == generation_sequence
                        and retained.artifact_receipts == artifact_receipts
                        and entry.artifact_store is artifact_store
                    ):
                        return self._projection(handle, entry)
                retained_state_fingerprint = (
                    None if retained_sequence is None else retained_sequence.state.fingerprint
                )
                if retained_state_fingerprint != expected_sequence_state_fingerprint:
                    raise ProductionWorkbenchError(
                        "stale_generation_sequence",
                        409,
                        projection=self._projection(handle, entry),
                    )
                if retained.continuity_receipts or retained.reconstruction_receipt is not None:
                    raise ProductionWorkbenchError("generation_authority_closed", 422)
                if entry.assembly_job is not None:
                    raise ProductionWorkbenchError("generation_authority_closed", 422)
                authorities = replace(
                    retained,
                    generation_sequence=generation_sequence,
                    artifact_receipts=artifact_receipts,
                )
                _validate_accepted_authorities(entry.workspace, authorities)

            preview_sources = _EMPTY_PREVIEW_SOURCES
            if _prepared_preview_sources is not None:
                preview_sources = self._checked_prepared_preview_sources(
                    generation_sequence,
                    artifact_receipts,
                    _prepared_preview_sources,
                )
            elif artifact_store is not None and artifact_receipts:
                preview_sources = self.prepare_generated_preview_sources(
                    generation_sequence,
                    artifact_receipts,
                    artifact_store,
                )

            assembly_runtime = None
            if (
                artifact_store is not None
                and artifact_receipts
                and entry.automatic_plan is not None
                and self._assembly_runtime_provider is not None
            ):
                try:
                    assembly_runtime = self._assembly_runtime_provider(artifact_store)
                except Exception:
                    assembly_runtime = None

            with self._lock:
                self._prune(self._clock())
                current = self._entries.get(handle)
                if current is not entry:
                    self._terminal_or_missing(handle)
                authority_bytes = _retained_authority_bytes(
                    entry.workspace,
                    authorities,
                    preview_sources,
                    entry.automatic_plan,
                )
                aggregate = (
                    sum(item.authority_bytes for item in self._entries.values())
                    + sum(item.authority_bytes for item in self._staged.values())
                    - entry.authority_bytes
                    + authority_bytes
                )
                if (
                    authority_bytes > MAX_PRODUCTION_AUTHORITY_BYTES
                    or aggregate > MAX_PRODUCTION_REGISTRY_BYTES
                ):
                    raise ProductionWorkbenchError("workspace_capacity", 429)
                candidate = _ProductionEntry(
                    workspace=entry.workspace,
                    touched_at=now,
                    authority_bytes=authority_bytes,
                    accepted_authorities=authorities,
                    preview_sources=preview_sources,
                    automatic_plan=entry.automatic_plan,
                    automatic_materializer=entry.automatic_materializer,
                    artifact_store=artifact_store,
                    assembly_runtime=assembly_runtime,
                    assembly_job=None,
                    mutation_claim=entry.mutation_claim,
                    lineage_token=entry.lineage_token,
                )
                projection = self._projection(handle, candidate)
                self._entries[handle] = candidate
                return projection
        finally:
            entry.mutation_claim.release()

    @staticmethod
    def _validate_preview_predecessors(
        *,
        plan: AVReconstructionPlan,
        generation_sequence: GenerationSequenceProjection,
        artifact_receipts: tuple[SegmentArtifactReceipt, ...],
        continuity_receipts: tuple[ContinuityBoundaryReceipt, ...],
    ) -> None:
        if (
            type(plan) is not AVReconstructionPlan
            or type(generation_sequence) is not GenerationSequenceProjection
            or type(artifact_receipts) is not tuple
            or not all(type(item) is SegmentArtifactReceipt for item in artifact_receipts)
            or type(continuity_receipts) is not tuple
            or not all(type(item) is ContinuityBoundaryReceipt for item in continuity_receipts)
        ):
            raise ProductionWorkbenchError("accepted_authority_mismatch", 422)
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
            raise ProductionWorkbenchError("accepted_authority_mismatch", 422)

    def publish_reconstruction_authority(
        self,
        *,
        plan: AVReconstructionPlan,
        generation_sequence: GenerationSequenceProjection,
        artifact_receipts: tuple[SegmentArtifactReceipt, ...],
        continuity_receipts: tuple[ContinuityBoundaryReceipt, ...],
        reconstruction_receipt: AVReconstructionReceipt,
        store: PrivateAVReconstructionStore,
        adapter: QualifiedAVMediaAdapter,
        deadline: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> ProductionPreviewPublication:
        """CAS-attach one exact all-current receipt and its eligible preview-source set."""

        self._validate_preview_predecessors(
            plan=plan,
            generation_sequence=generation_sequence,
            artifact_receipts=artifact_receipts,
            continuity_receipts=continuity_receipts,
        )
        if (
            type(reconstruction_receipt) is not AVReconstructionReceipt
            or type(store) is not PrivateAVReconstructionStore
            or type(adapter) is not QualifiedAVMediaAdapter
            or type(deadline) is not float
            or not callable(clock)
            or reconstruction_receipt.plan_fingerprint != plan.fingerprint
            or reconstruction_receipt.artifact_receipt_fingerprints
            != plan.artifact_receipt_fingerprints
            or reconstruction_receipt.boundary_receipt_fingerprints
            != plan.boundary_receipt_fingerprints
        ):
            raise ProductionWorkbenchError("accepted_authority_mismatch", 422)
        try:
            if clock() >= deadline:
                return ProductionPreviewPublication("publication_deadline")
        except Exception:
            return ProductionPreviewPublication("publication_deadline")
        now = self._clock()
        with self._lock:
            self._prune(now)
            source = generation_sequence.state.plan.source_workspace_authority
            if source is None:
                return ProductionPreviewPublication("owner_unavailable")
            same_id_owners = tuple(
                (handle, entry)
                for handle, entry in self._entries.items()
                if entry.workspace.workspace_id == plan.workspace_id
            )
            owners = tuple(
                (handle, entry)
                for handle, entry in same_id_owners
                if _is_structural_successor(entry.workspace, source)
            )
            if len(same_id_owners) != 1 or len(owners) != 1:
                return ProductionPreviewPublication("owner_unavailable")
            handle, entry = owners[0]
            if entry.assembly_job is not None or entry.members is not None:
                return ProductionPreviewPublication("owner_stale")

        all_current = all(
            receipt.workspace_id == plan.workspace_id
            and receipt.workspace_revision == plan.workspace_revision
            and receipt.workspace_fingerprint == plan.workspace_fingerprint
            and receipt.manifest_fingerprint == manifest
            for receipt, manifest in zip(
                artifact_receipts,
                plan.manifest_fingerprints,
                strict=True,
            )
        )
        if not all_current:
            return ProductionPreviewPublication("selective_reuse_unavailable")
        authorities = ProductionAcceptedAuthorities(
            generation_sequence=generation_sequence,
            artifact_receipts=artifact_receipts,
            continuity_receipts=continuity_receipts,
            reconstruction_receipt=reconstruction_receipt,
        )
        preview_candidates: list[MediaPreviewSourceAuthority] = []
        preview_sources = _EMPTY_PREVIEW_SOURCES
        route_available = False
        try:
            # CRITICAL: never advertise the opaque action unless this package owns the route.
            from .comfyui_media_preview import ensure_media_preview_route_registered

            route_available = ensure_media_preview_route_registered()
        except Exception:
            route_available = False
        if route_available:
            aggregate = next(
                (
                    output
                    for output in reconstruction_receipt.outputs
                    if output.kind is AVOutputKind.RECONSTRUCTION_FULL
                ),
                None,
            )
            if aggregate is not None:
                aggregate_source = _try_preview_source(
                    plan=plan,
                    receipt=reconstruction_receipt,
                    store=store,
                    adapter=adapter,
                    opaque_output_handle=_opaque_output_handle(aggregate.handle),
                    deadline=deadline,
                    clock=clock,
                )
                if aggregate_source is not None:
                    preview_candidates.append(aggregate_source)
            result_by_segment = {
                result.segment_id: result for result in reconstruction_receipt.segment_results
            }
            for segment in plan.segments:
                result = result_by_segment.get(segment.segment_id)
                if result is None or result.derived_output_handle is None:
                    continue
                # IMPORTANT: segment eligibility is member-scoped; one bad export must not
                # suppress the accepted receipt, aggregate preview, or eligible siblings.
                segment_source = _try_preview_source(
                    plan=plan,
                    receipt=reconstruction_receipt,
                    store=store,
                    adapter=adapter,
                    segment_id=segment.segment_id,
                    opaque_output_handle=_opaque_output_handle(result.derived_output_handle),
                    deadline=deadline,
                    clock=clock,
                )
                if segment_source is not None:
                    preview_candidates.append(segment_source)
            try:
                preview_sources = _bounded_preview_sources(tuple(preview_candidates))
            except (TypeError, ValueError):
                preview_sources = _EMPTY_PREVIEW_SOURCES
        try:
            output_journal = _preview_publication_journal(
                reconstruction_receipt,
                preview_sources,
            )
        except ProductionWorkbenchError:
            return ProductionPreviewPublication("accepted_authority_mismatch")

        remaining = deadline - clock()
        if remaining <= 0 or not entry.mutation_claim.acquire(
            timeout=min(remaining, self._mutation_timeout_seconds)
        ):
            return ProductionPreviewPublication("publication_busy")
        try:
            with self._lock:
                self._prune(self._clock())
                current = self._entries.get(handle)
                if (
                    current is not entry
                    or entry.assembly_job is not None
                    or entry.members is not None
                ):
                    return ProductionPreviewPublication("owner_stale")
                try:
                    _validate_accepted_authorities(entry.workspace, authorities)
                except ProductionWorkbenchError:
                    return ProductionPreviewPublication("accepted_authority_mismatch")
                authority_bytes = _retained_authority_bytes(
                    entry.workspace,
                    authorities,
                    preview_sources,
                    entry.automatic_plan,
                )
                aggregate_bytes = (
                    sum(item.authority_bytes for item in self._entries.values())
                    + sum(item.authority_bytes for item in self._staged.values())
                    - entry.authority_bytes
                    + authority_bytes
                )
                if (
                    authority_bytes > MAX_PRODUCTION_AUTHORITY_BYTES
                    or aggregate_bytes > MAX_PRODUCTION_REGISTRY_BYTES
                ):
                    return ProductionPreviewPublication("workspace_capacity")
                candidate = _ProductionEntry(
                    workspace=entry.workspace,
                    touched_at=entry.touched_at,
                    authority_bytes=authority_bytes,
                    accepted_authorities=authorities,
                    preview_sources=preview_sources,
                    automatic_plan=entry.automatic_plan,
                    automatic_materializer=entry.automatic_materializer,
                    artifact_store=entry.artifact_store,
                    assembly_runtime=entry.assembly_runtime,
                    assembly_job=entry.assembly_job,
                    mutation_claim=entry.mutation_claim,
                    lineage_token=entry.lineage_token,
                )
                projection = self._projection(handle, candidate)
                self._entries[handle] = candidate
                return ProductionPreviewPublication(
                    "published" if preview_sources else "attached_without_preview",
                    projection,
                    output_journal,
                )
        finally:
            entry.mutation_claim.release()

    def admit_media_preview_source(
        self,
        *,
        workspace_handle: str,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
        output_handle: str,
    ) -> MediaPreviewSourceAuthority:
        """Resolve one exact current source before the route acquires its global execution claim."""

        handle = _workspace_handle(workspace_handle)
        now = self._clock()
        # CRITICAL: this lookup runs on the host event loop; registry contention must fail busy
        # instead of blocking Preview admission beyond its absolute work deadline.
        if not self._lock.acquire(blocking=False):
            raise ProductionWorkbenchError("workspace_busy", 423)
        try:
            self._prune(now)
            entry = self._entries.get(handle)
            if entry is None:
                self._terminal_or_missing(handle)
            if (
                entry.workspace.revision != expected_workspace_revision
                or entry.workspace.fingerprint != expected_workspace_fingerprint
            ):
                raise ProductionWorkbenchError("stale_workspace", 409)
            if not entry.preview_sources:
                raise ProductionWorkbenchError("preview_unavailable", 422)
            source = entry.preview_sources.get(output_handle)
            if source is None:
                raise ProductionWorkbenchError("preview_output_unknown", 404)
            return source
        finally:
            self._lock.release()

    def media_preview_source_is_current(
        self,
        *,
        workspace_handle: str,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
        source: MediaPreviewSourceAuthority,
    ) -> bool:
        # CRITICAL: late validation shares the event-loop nonblocking rule with source admission.
        if not self._lock.acquire(blocking=False):
            raise ProductionWorkbenchError("workspace_busy", 423)
        try:
            self._prune(self._clock())
            entry = self._entries.get(workspace_handle)
            if entry is not None and entry.members is not None:
                # IMPORTANT: a member's preview source is retained exactly as long as its completed
                # receipt is. A later project revision (another member, selection, reorder) keeps
                # the object, while delete, replace or regeneration removes it; never compare the
                # revision here or every append revokes earlier previews.
                return bool(
                    entry.workspace.revision >= expected_workspace_revision
                    and entry.preview_sources.get(source.opaque_output_handle) is source
                )
            return bool(
                entry is not None
                and entry.workspace.revision == expected_workspace_revision
                and entry.workspace.fingerprint == expected_workspace_fingerprint
                and entry.preview_sources.get(source.opaque_output_handle) is source
            )
        finally:
            self._lock.release()

    def publish_generation_sequence_authority(
        self,
        sequence: GenerationSequenceProjection,
        context_workspace_handle: str | None = None,
    ) -> ProductionWorkbenchProjection | None:
        """Attach or stage one exact builder-owned M17-08 source for a Context handle.

        This is an internal publisher seam, not a browser action. Missing, foreign or ambiguous
        ownership is deliberately non-retaining so a later unrelated workspace cannot inherit it.
        """

        if type(sequence) is not GenerationSequenceProjection:
            raise TypeError("generation sequence authority is invalid")
        plan = sequence.state.plan
        source = plan.source_workspace_authority
        if context_workspace_handle is None or source is None:
            return None
        context_handle = _sidebar_handle(context_workspace_handle)
        try:
            seed = self._seed_claim(context_handle)
        except KeyError:
            return None
        if not self._source_matches_seed(source, seed):
            return None
        authorities = ProductionAcceptedAuthorities(generation_sequence=sequence)
        try:
            _validate_accepted_authorities(source, authorities)
        except ProductionWorkbenchError:
            return None
        authority_bytes = len(source.to_wire_bytes()) + authorities.wire_bytes()
        if authority_bytes > MAX_PRODUCTION_AUTHORITY_BYTES:
            return None
        now = self._clock()
        with self._lock:
            self._prune(now)
            same_id_live = tuple(
                (handle, entry)
                for handle, entry in self._entries.items()
                if entry.workspace.workspace_id == source.workspace_id
            )
            compatible_live = tuple(
                (handle, entry)
                for handle, entry in same_id_live
                if _is_structural_successor(entry.workspace, source)
            )
            if same_id_live:
                if len(same_id_live) != 1 or len(compatible_live) != 1:
                    return None
                handle, entry = compatible_live[0]
            else:
                handle = None
                entry = None

            existing_stage = self._staged.get(context_handle)
            if existing_stage is not None:
                if (
                    existing_stage.workspace is source
                    and existing_stage.generation_sequence == sequence
                ):
                    return None
                return None
            if any(
                staged.workspace.workspace_id == source.workspace_id
                for staged_handle, staged in self._staged.items()
                if staged_handle != context_handle
            ):
                return None
            if entry is None:
                if self._occupied_entry_slots() >= self._max_entries:
                    return None
                if (
                    self._reserved_registry_bytes() + authority_bytes
                    > MAX_PRODUCTION_REGISTRY_BYTES
                ):
                    return None
                self._staged[context_handle] = _StagedEntry(
                    workspace=source,
                    generation_sequence=sequence,
                    staged_at=now,
                    authority_bytes=authority_bytes,
                    lineage_token=seed.lineage_token,
                )
                return None
            attached_authorities = replace(
                entry.accepted_authorities,
                generation_sequence=sequence,
            )
        try:
            return self.attach_accepted_authorities(
                cast(str, handle),
                expected_workspace_revision=entry.workspace.revision,
                expected_workspace_fingerprint=entry.workspace.fingerprint,
                authorities=attached_authorities,
            )
        except ProductionWorkbenchError:
            # A concurrent mutation/release wins ownership. The source publication is never queued
            # for later attachment and must not break the independent ProductShell projection.
            return None

    @staticmethod
    def _source_matches_seed(
        source: MultiSegmentWorkspace,
        seed: SidebarProductionSeed,
    ) -> bool:
        matches = 0
        for segment in source.segments:
            expected = _segment_from_seed(
                seed,
                segment_id=segment.segment_id,
                relation=segment.relation,
                predecessor_segment_id=segment.predecessor_segment_id,
            )
            if (
                segment == expected
                and segment.accepted_intent_fingerprint == seed.accepted_intent_fingerprint
            ):
                matches += 1
        return matches == 1

    def _terminal_or_missing(self, handle: str) -> NoReturn:
        if handle in self._tombstones:
            raise ProductionWorkbenchError("workspace_gone", 410)
        raise ProductionWorkbenchError("workspace_unavailable", 404)

    def _replay(
        self,
        *,
        request_id: str,
        request_digest: str,
    ) -> ProductionDispatchResult | None:
        previous = self._ledger.get(request_id)
        if previous is None:
            return None
        if previous.request_digest != request_digest:
            raise ProductionWorkbenchError("request_id_conflict", 409)
        entry = self._entries.get(previous.workspace_handle)
        editable = self._editable_projects.get(previous.workspace_handle)
        if editable is not None:
            projection = self._editable_projection(previous.workspace_handle, editable)
            if (
                editable.revision == previous.committed_revision
                and editable.fingerprint == previous.committed_fingerprint
            ):
                editable.touched_at = self._clock()
                return ProductionDispatchResult(200, projection)
            return ProductionDispatchResult(409, projection, "replay_superseded")
        if entry is None:
            self._terminal_or_missing(previous.workspace_handle)
        projection = self._projection(previous.workspace_handle, entry)
        if (
            projection.workspace_revision == previous.committed_revision
            and projection.workspace_fingerprint == previous.committed_fingerprint
        ):
            entry.touched_at = self._clock()
            return ProductionDispatchResult(200, projection)
        return ProductionDispatchResult(409, projection, "replay_superseded")

    def _ensure_ledger_capacity(self, request_id: str) -> None:
        if request_id not in self._ledger and len(self._ledger) >= self._max_ledger_entries:
            raise ProductionWorkbenchError("request_capacity", 429)

    def _publish_ledger(
        self,
        *,
        request_id: str,
        request_digest: str,
        action: str,
        handle: str,
        workspace: MultiSegmentWorkspace,
        now: float,
    ) -> None:
        self._ledger[request_id] = _LedgerEntry(
            request_digest=request_digest,
            action=action,
            workspace_handle=handle,
            committed_revision=workspace.revision,
            committed_fingerprint=workspace.fingerprint,
            touched_at=now,
        )
        self._notify_editor_recovery(handle)

    def _check_expected(
        self,
        payload: dict[str, object],
        handle: str,
        entry: _ProductionEntry,
    ) -> None:
        expected_revision, expected_fingerprint = _expected_identity(payload)
        if (
            expected_revision != entry.workspace.revision
            or expected_fingerprint != entry.workspace.fingerprint
        ):
            raise ProductionWorkbenchError(
                "stale_workspace",
                409,
                projection=self._projection(handle, entry),
            )

    def _claim_seed(self, action: dict[str, object]) -> SidebarProductionSeed | None:
        kind = action["action"]
        if kind not in {
            "create_workspace_from_context",
            "add_segment_from_context",
            "replace_segment_from_context",
        }:
            return None
        payload = action["payload"]
        if type(payload) is not dict:  # pragma: no cover - admitted above
            raise ProductionWorkbenchError("invalid_action", 400)
        try:
            return self._seed_claim(_sidebar_handle(payload["context_workspace_handle"]))
        except KeyError as exc:
            raise ProductionWorkbenchError("workspace_unavailable", 404) from exc

    def _new_workspace_id(self) -> str:
        retained_ids = {entry.workspace.workspace_id for entry in self._entries.values()} | {
            entry.workspace.workspace_id for entry in self._staged.values()
        }
        retained_ids.update(entry.workspace_id for entry in self._editable_projects.values())
        retained_ids.update(entry.workspace_id for entry in self._empty_projects.values())
        for _ in range(8):
            candidate = "workspace_" + secrets.token_urlsafe(18)
            if candidate not in retained_ids:
                return candidate
        raise ProductionWorkbenchError("workspace_capacity", 429)

    def _new_workspace_handle(self) -> str:
        for _ in range(8):
            candidate = "pw_" + secrets.token_urlsafe(32)
            if (
                candidate not in self._entries
                and candidate not in self._tombstones
                and candidate not in self._editable_projects
                and candidate not in self._empty_projects
            ):
                return candidate
        raise ProductionWorkbenchError("workspace_capacity", 429)

    def _create(
        self,
        action: dict[str, object],
        seed: SidebarProductionSeed,
        request_digest: str,
    ) -> ProductionDispatchResult:
        request_id = str(action["request_id"])
        payload = cast(dict[str, object], action["payload"])
        context_handle = _sidebar_handle(payload["context_workspace_handle"])
        now = self._clock()
        with self._lock:
            self._prune(now)
            replay = self._replay(request_id=request_id, request_digest=request_digest)
            if replay is not None:
                return replay
            stage = self._staged.get(context_handle)
            if stage is not None:
                try:
                    _validate_accepted_authorities(
                        stage.workspace,
                        ProductionAcceptedAuthorities(
                            generation_sequence=stage.generation_sequence
                        ),
                    )
                except ProductionWorkbenchError as exc:
                    raise ProductionWorkbenchError("action_rejected", 422) from exc
                if (
                    stage.generation_sequence.state.plan.source_workspace_authority
                    is not stage.workspace
                    or not self._source_matches_seed(stage.workspace, seed)
                    or stage.lineage_token is not seed.lineage_token
                    or any(
                        entry.workspace.workspace_id == stage.workspace.workspace_id
                        for entry in self._entries.values()
                    )
                    or any(
                        other.workspace.workspace_id == stage.workspace.workspace_id
                        for handle, other in self._staged.items()
                        if handle != context_handle
                    )
                ):
                    raise ProductionWorkbenchError("action_rejected", 422)
                self._ensure_ledger_capacity(request_id)
                handle = self._new_workspace_handle()
                entry = _ProductionEntry(
                    workspace=stage.workspace,
                    touched_at=now,
                    authority_bytes=stage.authority_bytes,
                    accepted_authorities=ProductionAcceptedAuthorities(
                        generation_sequence=stage.generation_sequence
                    ),
                    lineage_token=stage.lineage_token,
                )
                projection = self._projection(handle, entry)
                self._staged.pop(context_handle, None)
                self._entries[handle] = entry
                authoring_seed = _project_authoring_seed(seed)
                if authoring_seed is not None:
                    self._authoring_anchors[handle] = _ProjectAuthoringAnchor(authoring_seed)
                self._publish_ledger(
                    request_id=request_id,
                    request_digest=request_digest,
                    action="create_workspace_from_context",
                    handle=handle,
                    workspace=stage.workspace,
                    now=now,
                )
                return ProductionDispatchResult(201, projection)
            self._ensure_ledger_capacity(request_id)
            if self._occupied_entry_slots() >= self._max_entries:
                raise ProductionWorkbenchError("workspace_capacity", 429)
            segment_id = "segment_" + secrets.token_urlsafe(18)
            declaration = _segment_from_seed(
                seed,
                segment_id=segment_id,
                relation=SegmentRelationKind.INDEPENDENT,
                predecessor_segment_id=None,
            )
            workspace = create_workspace(
                self._new_workspace_id(),
                (declaration,),
                accepted_intent_authorities=(
                    AcceptedIntentAuthority(segment_id, seed.accepted_intent_fingerprint),
                ),
                selected_segment_ids=(segment_id,),
            )
            authority_bytes = len(workspace.to_wire_bytes())
            if authority_bytes > MAX_PRODUCTION_AUTHORITY_BYTES or (
                self._reserved_registry_bytes() + authority_bytes > MAX_PRODUCTION_REGISTRY_BYTES
            ):
                raise ProductionWorkbenchError("workspace_capacity", 429)
            handle = self._new_workspace_handle()
            entry = _ProductionEntry(
                workspace,
                now,
                authority_bytes,
                lineage_token=seed.lineage_token,
            )
            projection = self._projection(handle, entry)
            self._entries[handle] = entry
            authoring_seed = _project_authoring_seed(seed)
            if authoring_seed is not None:
                self._authoring_anchors[handle] = _ProjectAuthoringAnchor(authoring_seed)
            self._publish_ledger(
                request_id=request_id,
                request_digest=request_digest,
                action="create_workspace_from_context",
                handle=handle,
                workspace=workspace,
                now=now,
            )
            return ProductionDispatchResult(201, projection)

    def claim_authoring_seed_for_project(
        self, workspace_handle: str, workspace_id: str
    ) -> SidebarAuthoringSeed:
        """Return the original content-free seed only for its exact live Production owner."""

        with self._lock:
            self._prune(self._clock())
            entry = self._entries.get(workspace_handle)
            editable = self._editable_projects.get(workspace_handle)
            if entry is None and editable is None:
                self._terminal_or_missing(workspace_handle)
            if entry is not None:
                identity, lineage = entry.workspace.workspace_id, entry.lineage_token
            else:
                portable = cast(_EditableProjectEntry, editable)
                identity, lineage = portable.workspace_id, portable.lineage_token
            if identity != workspace_id:
                raise ProductionWorkbenchError("destination_mismatch", 409)
            anchor = self._authoring_anchors.get(workspace_handle)
            if anchor is None or anchor.seed.lineage_token is not lineage:
                raise ProductionWorkbenchError("authoring_seed_unavailable", 422)
            return anchor.seed

    @contextmanager
    def authoring_anchor_for_project(
        self, workspace_handle: str, workspace_id: str
    ) -> Iterator[_ProjectAuthoringAnchor]:
        """Hold the exact live owner while Authoring resolves its single target."""

        # CRITICAL: the caller already holds the Authoring lock; Authoring then Production is the
        # import lock order. Never call back into the Authoring registry while this lock is held:
        # Authoring checks Production claim currentness under its own lock, so the reverse order
        # deadlocks.
        with self._lock:
            self._prune(self._clock())
            entry = self._entries.get(workspace_handle)
            if entry is None:
                self._terminal_or_missing(workspace_handle)
            if entry.workspace.workspace_id != workspace_id:
                raise ProductionWorkbenchError("destination_mismatch", 409)
            anchor = self._authoring_anchors.get(workspace_handle)
            if anchor is None or anchor.seed.lineage_token is not entry.lineage_token:
                raise ProductionWorkbenchError("authoring_seed_unavailable", 422)
            yield anchor

    def _next_workspace(
        self,
        entry: _ProductionEntry,
        *,
        segments: tuple[SegmentDeclaration, ...] | None = None,
        selected_segment_ids: tuple[str, ...] | None = None,
    ) -> MultiSegmentWorkspace:
        candidate_segments = entry.workspace.segments if segments is None else segments
        return revise_workspace(
            entry.workspace,
            expected_workspace_fingerprint=entry.workspace.fingerprint,
            accepted_intent_authorities=_authorities(candidate_segments),
            segments=segments,
            selected_segment_ids=selected_segment_ids,
        )

    def _mutate_workspace(
        self,
        *,
        action: str,
        payload: dict[str, object],
        entry: _ProductionEntry,
        seed: SidebarProductionSeed | None,
    ) -> MultiSegmentWorkspace:
        segments = entry.workspace.segments
        if action == "add_segment_from_context":
            if seed is None or len(segments) >= MAX_WORKSPACE_SEGMENTS:
                raise ProductionWorkbenchError("action_rejected", 422)
            relation = SegmentRelationKind(str(payload["relation"]))
            predecessor = payload["predecessor_segment_id"]
            segment_id = "segment_" + secrets.token_urlsafe(18)
            next_segments = segments + (
                _segment_from_seed(
                    seed,
                    segment_id=segment_id,
                    relation=relation,
                    predecessor_segment_id=(None if predecessor is None else str(predecessor)),
                ),
            )
            return self._next_workspace(entry, segments=next_segments)
        if action == "replace_segment_from_context":
            if seed is None:
                raise ProductionWorkbenchError("action_rejected", 422)
            segment_id = str(payload["segment_id"])
            current = next((item for item in segments if item.segment_id == segment_id), None)
            if current is None:
                raise ProductionWorkbenchError("action_rejected", 422)
            replacement = _segment_from_seed(
                seed,
                segment_id=segment_id,
                relation=current.relation,
                predecessor_segment_id=current.predecessor_segment_id,
            )
            return self._next_workspace(
                entry,
                segments=tuple(
                    replacement if item.segment_id == segment_id else item for item in segments
                ),
            )
        if action == "set_segment_relation":
            segment_id = str(payload["segment_id"])
            relation = SegmentRelationKind(str(payload["relation"]))
            predecessor_value = payload["predecessor_segment_id"]
            predecessor = None if predecessor_value is None else str(predecessor_value)
            if not any(item.segment_id == segment_id for item in segments):
                raise ProductionWorkbenchError("action_rejected", 422)
            next_segments = tuple(
                replace(
                    item,
                    relation=relation,
                    predecessor_segment_id=predecessor,
                )
                if item.segment_id == segment_id
                else item
                for item in segments
            )
            return self._next_workspace(entry, segments=next_segments)
        if action == "delete_segment":
            segment_id = str(payload["segment_id"])
            if len(segments) == 1 or not any(item.segment_id == segment_id for item in segments):
                raise ProductionWorkbenchError("action_rejected", 422)
            if any(item.predecessor_segment_id == segment_id for item in segments):
                raise ProductionWorkbenchError("action_rejected", 422)
            next_segments = tuple(item for item in segments if item.segment_id != segment_id)
            next_selection = tuple(
                item for item in entry.workspace.selected_segment_ids if item != segment_id
            )
            return self._next_workspace(
                entry,
                segments=next_segments,
                selected_segment_ids=next_selection,
            )
        if action == "reorder_segments":
            requested = tuple(str(item) for item in cast(list[object], payload["segment_ids"]))
            current_by_id = {item.segment_id: item for item in segments}
            if len(requested) != len(segments) or set(requested) != set(current_by_id):
                raise ProductionWorkbenchError("action_rejected", 422)
            return self._next_workspace(
                entry,
                segments=tuple(current_by_id[item] for item in requested),
            )
        if action == "set_selection":
            requested = tuple(str(item) for item in cast(list[object], payload["segment_ids"]))
            if not set(requested).issubset({item.segment_id for item in segments}):
                raise ProductionWorkbenchError("action_rejected", 422)
            return self._next_workspace(entry, selected_segment_ids=requested)
        raise ProductionWorkbenchError("invalid_action", 400)

    @staticmethod
    def _assembly_job_bytes(job: _ProductionAssemblyJob | None) -> int:
        if job is None:
            return 0
        return len(canonical_bytes(job.authorization.to_wire()))

    @staticmethod
    def _cancel_entry_assembly(entry: _ProductionEntry) -> None:
        job = entry.assembly_job
        if job is not None and job.state in {"planned", "running", "cancelling"}:
            job.cancellation.event.set()
            job.state = "cancelling"

    def _validate_assemble_payload(
        self,
        payload: dict[str, object],
        entry: _ProductionEntry,
        projection: ProductionAssemblyProjection,
    ) -> None:
        if (
            payload["workspace_id"] != entry.workspace.workspace_id
            or payload["managed_sequence_fingerprint"] != projection.managed_sequence_fingerprint
            or tuple(cast(list[str], payload["artifact_receipt_fingerprints"]))
            != projection.artifact_receipt_fingerprints
            or tuple(cast(list[str], payload["cut_boundary_receipt_fingerprints"]))
            != projection.cut_boundary_receipt_fingerprints
            or payload["assembly_capability_fingerprint"] != projection.capability_fingerprint
            or payload["output_profile_id"] != projection.output_profile_id
        ):
            raise ProductionWorkbenchError("assembly_authority_mismatch", 409)

    def _new_assembly_job(
        self,
        *,
        entry: _ProductionEntry,
        request_digest: str,
    ) -> _ProductionAssemblyJob:
        runtime = entry.assembly_runtime
        automatic = entry.automatic_plan
        sequence = entry.accepted_authorities.generation_sequence
        if (
            runtime is None
            or automatic is None
            or sequence is None
            or not self._assembly_predecessors_ready(entry)
        ):
            raise ProductionWorkbenchError("assembly_unavailable", 422)
        issued_at_ms = self._clock_ms()
        if type(issued_at_ms) is not int or issued_at_ms <= 0:
            raise ProductionWorkbenchError("assembly_clock_failed", 503)
        digest_suffix = request_digest.removeprefix("sha256:")
        try:
            authorization = mint_m26_assembly_authorization(
                authorization_id="m26auth_" + digest_suffix[:40],
                automatic_plan=automatic,
                generation_sequence=sequence,
                artifact_receipts=entry.accepted_authorities.artifact_receipts,
                cut_boundary_receipts=automatic.cut_boundary_receipts,
                capability=runtime.capability,
                issued_at_ms=issued_at_ms,
                expires_at_ms=issued_at_ms + 600_000,
            )
        except Exception as exc:
            raise ProductionWorkbenchError("assembly_authority_mismatch", 409) from exc
        return _ProductionAssemblyJob(
            job_id="m26job_" + digest_suffix[:40],
            authorization=authorization,
            runtime=runtime,
            automatic_plan=automatic,
            generation_sequence=sequence,
            artifact_receipts=entry.accepted_authorities.artifact_receipts,
        )

    def _fail_assembly_job(
        self,
        handle: str,
        entry: _ProductionEntry,
        job: _ProductionAssemblyJob,
        failure_code: str,
    ) -> None:
        with self._lock:
            current = self._entries.get(handle)
            if current is not entry or entry.assembly_job is not job:
                return
            job.state = "failed"
            job.failure_code = failure_code
            job.execution = None
            entry.touched_at = self._clock()

    def _live_reconstruction_transaction_ids(self) -> frozenset[str]:
        """Reconstruction transactions any retained entry or assembly job can still reach.

        The caller holds the registry lock.
        """

        live: set[str] = set()
        for entry in self._entries.values():
            if entry.assembly_job is not None:
                live.add("m26tx_" + entry.assembly_job.job_id.removeprefix("m26job_"))
                if entry.assembly_job.execution is not None:
                    live.add(entry.assembly_job.execution.reconstruction_receipt.transaction_id)
            authorities = entry.accepted_authorities
            if authorities.assembly_execution is not None:
                live.add(authorities.assembly_execution.reconstruction_receipt.transaction_id)
            if authorities.reconstruction_receipt is not None:
                live.add(authorities.reconstruction_receipt.transaction_id)
        return frozenset(live)

    def _run_assembly_job(
        self,
        handle: str,
        entry: _ProductionEntry,
        job: _ProductionAssemblyJob,
    ) -> None:
        from .media_runtime_manager import MediaRuntimeBusy

        with ExitStack() as stack:
            try:
                stack.enter_context(self._media_lease())
            except MediaRuntimeBusy:
                # A binding transition is in progress: fail fast rather than queue behind it.
                self._fail_assembly_job(
                    handle,
                    entry,
                    job,
                    "cancelled"
                    if job.cancellation.is_cancelled()
                    else "assembly_worker_unavailable",
                )
                return
            self._run_leased_assembly_job(handle, entry, job)

    def _run_leased_assembly_job(
        self,
        handle: str,
        entry: _ProductionEntry,
        job: _ProductionAssemblyJob,
    ) -> None:
        with self._lock:
            current = self._entries.get(handle)
            # IMPORTANT: cancellation may win before the queued callback starts. Admit that
            # state here so it terminalizes as failed/cancelled instead of remaining stuck and
            # making the retry action permanently unreachable.
            if (
                current is not entry
                or entry.assembly_job is not job
                or job.state not in {"planned", "cancelling"}
            ):
                return
            if job.cancellation.is_cancelled():
                self._fail_assembly_job(handle, entry, job, "cancelled")
                return
            job.state = "running"
            entry.touched_at = self._clock()
            live_reconstructions = self._live_reconstruction_transaction_ids()
            snapshot_ms = self._clock_ms()
        # IMPORTANT (B-M2522-STORE-01): retire complete reconstructions no live workspace can
        # reach before this job needs quota; committed receipts otherwise fill the persistent store
        # after `max_transactions` assemblies and every later assembly fails. The reference set
        # and its instant are taken under the registry lock and include every job, in any state,
        # so a publication racing this one is kept. Retirement is best effort: a store that
        # refuses it reports its own typed failure through this job's execution.
        try:
            job.runtime.reconstruction_store.retire_unreferenced(
                keep_transaction_ids=live_reconstructions,
                completed_before_ms=snapshot_ms,
            )
        except AVStoreError:
            pass
        transaction_id = "m26tx_" + job.job_id.removeprefix("m26job_")
        try:
            execution = execute_m26_production_assembly(
                runtime=job.runtime,
                transaction_id=transaction_id,
                authorization=job.authorization,
                automatic_plan=job.automatic_plan,
                generation_sequence=job.generation_sequence,
                artifact_receipts=job.artifact_receipts,
                cut_boundary_receipts=job.automatic_plan.cut_boundary_receipts,
                clock_ms=self._clock_ms,
                cancellation=job.cancellation,
            )
        except M26ProductionAssemblyError as exc:
            self._fail_assembly_job(
                handle,
                entry,
                job,
                "cancelled" if job.cancellation.is_cancelled() else exc.code,
            )
            return
        except Exception:
            self._fail_assembly_job(handle, entry, job, "assembly_execution_failed")
            return

        with self._lock:
            original_authorities = entry.accepted_authorities
            original_previews = entry.preview_sources
            workspace = entry.workspace
        authorities = replace(original_authorities, assembly_execution=execution)
        try:
            _validate_accepted_authorities(workspace, authorities)
            preview_sources = dict(original_previews)
            aggregate_output = next(
                output
                for output in execution.reconstruction_receipt.outputs
                if output.kind is AVOutputKind.RECONSTRUCTION_FULL
            )
            # IMPORTANT: the frozen preview size/duration bounds do not limit assembly. Only
            # these validated receipt bounds permit ready-without-preview; integrity failures do
            # not. Prepare eligible authority outside registry locks before exposing success.
            if (
                aggregate_output.byte_length <= MAX_MEDIA_PREVIEW_SOURCE_BYTES
                and aggregate_output.duration.fraction * 1000 <= MAX_MEDIA_PREVIEW_DURATION_MS
            ):
                from .comfyui_media_preview import ensure_media_preview_route_registered

                if not ensure_media_preview_route_registered():
                    raise ProductionWorkbenchError("assembly_preview_unavailable", 422)
                aggregate_handle = _opaque_output_handle(aggregate_output.handle)
                preview_sources[aggregate_handle] = build_media_preview_source_authority(
                    plan=execution.embedded_plan,
                    receipt=execution.reconstruction_receipt,
                    store=job.runtime.reconstruction_store,
                    adapter=job.runtime.low_level_adapter,
                    opaque_output_handle=aggregate_handle,
                    deadline=self._clock() + PRODUCTION_GENERATED_PREVIEW_ADMISSION_SECONDS,
                    cancellation=job.cancellation,
                    clock=self._clock,
                )
            retained_previews = MappingProxyType(preview_sources)
            _preview_sources_wire_bytes(retained_previews)
        except Exception:
            self._fail_assembly_job(
                handle,
                entry,
                job,
                "cancelled" if job.cancellation.is_cancelled() else "assembly_publication_mismatch",
            )
            return

        if not entry.mutation_claim.acquire(timeout=self._mutation_timeout_seconds):
            self._fail_assembly_job(handle, entry, job, "assembly_publication_busy")
            return
        try:
            with self._lock:
                self._prune(self._clock())
                current = self._entries.get(handle)
                if (
                    current is not entry
                    or entry.assembly_job is not job
                    or job.state not in {"running", "cancelling"}
                    or job.cancellation.is_cancelled()
                    or execution.authorization is not job.authorization
                    or entry.automatic_plan is not job.automatic_plan
                    or entry.accepted_authorities.generation_sequence is not job.generation_sequence
                    or entry.accepted_authorities.artifact_receipts != job.artifact_receipts
                    or entry.accepted_authorities is not original_authorities
                    or entry.preview_sources is not original_previews
                ):
                    # IMPORTANT: cancellation can win after preview preparation succeeds; that
                    # remains cancellation, not evidence of stale publication authority.
                    self._fail_assembly_job(
                        handle,
                        entry,
                        job,
                        "cancelled"
                        if job.cancellation.is_cancelled()
                        else "assembly_publication_stale",
                    )
                    return
                authority_bytes = _retained_authority_bytes(
                    entry.workspace,
                    authorities,
                    retained_previews,
                    entry.automatic_plan,
                )
                aggregate = (
                    sum(item.authority_bytes for item in self._entries.values())
                    + sum(item.authority_bytes for item in self._staged.values())
                    - entry.authority_bytes
                    + authority_bytes
                )
                if (
                    authority_bytes > MAX_PRODUCTION_AUTHORITY_BYTES
                    or aggregate > MAX_PRODUCTION_REGISTRY_BYTES
                ):
                    self._fail_assembly_job(handle, entry, job, "workspace_capacity")
                    return
                # CRITICAL: validate the complete candidate before changing the live job or
                # accepted authority. A projection/preview failure must leave originals usable
                # and report non-success, never a succeeded job with half-published outputs.
                candidate_job = replace(
                    job,
                    execution=execution,
                    completed=len(job.artifact_receipts),
                    failure_code=None,
                    state="succeeded",
                )
                candidate = replace(
                    entry,
                    accepted_authorities=authorities,
                    preview_sources=retained_previews,
                    authority_bytes=authority_bytes,
                    assembly_job=candidate_job,
                )
                try:
                    self._projection(handle, candidate)
                except Exception:
                    self._fail_assembly_job(handle, entry, job, "assembly_publication_mismatch")
                    return
                job.execution = execution
                job.completed = len(job.artifact_receipts)
                job.failure_code = None
                job.state = "succeeded"
                entry.accepted_authorities = authorities
                entry.preview_sources = retained_previews
                entry.authority_bytes = authority_bytes
                entry.touched_at = self._clock()
        finally:
            entry.mutation_claim.release()

    def _submit_assembly_job(
        self,
        handle: str,
        entry: _ProductionEntry,
        job: _ProductionAssemblyJob,
    ) -> None:
        def callback() -> None:
            self._run_assembly_job(handle, entry, job)

        try:
            if self._assembly_submitter is not None:
                self._assembly_submitter(callback)
                return
            with self._lock:
                if self._assembly_executor is None:
                    # IMPORTANT: the executor is allocated only after exact authorization. Missing
                    # media runtime therefore creates no thread, worker, output or temporary file.
                    self._assembly_executor = ThreadPoolExecutor(
                        max_workers=1,
                        thread_name_prefix="h3-m26-assembly",
                    )
                executor = self._assembly_executor
            executor.submit(callback)
        except Exception:
            self._fail_assembly_job(handle, entry, job, "assembly_worker_unavailable")

    def _dispatch_assembly(
        self,
        action: dict[str, object],
        request_digest: str,
        handle: str,
        entry: _ProductionEntry,
    ) -> ProductionDispatchResult:
        payload = cast(dict[str, object], action["payload"])
        request_id = str(action["request_id"])
        kind = str(action["action"])
        pending_job: _ProductionAssemblyJob | None = None
        if not entry.mutation_claim.acquire(timeout=self._mutation_timeout_seconds):
            raise ProductionWorkbenchError("workspace_busy", 423)
        try:
            with self._lock:
                self._prune(self._clock())
                current = self._entries.get(handle)
                if current is not entry:
                    self._terminal_or_missing(handle)
                replay = self._replay(
                    request_id=request_id,
                    request_digest=request_digest,
                )
                if replay is not None:
                    return replay
                self._check_expected(payload, handle, entry)
                if payload["workspace_id"] != entry.workspace.workspace_id:
                    raise ProductionWorkbenchError("assembly_authority_mismatch", 409)
                self._ensure_ledger_capacity(request_id)
                self._refuse_member_mode(handle, entry)
                current_projection = self._assembly_projection(entry)
                if current_projection is None:
                    raise ProductionWorkbenchError("assembly_unavailable", 422)
                if kind == "cancel_assembly":
                    job = entry.assembly_job
                    if (
                        job is None
                        or job.state not in {"planned", "running"}
                        or payload["expected_assembly_fingerprint"]
                        != current_projection.fingerprint
                    ):
                        raise ProductionWorkbenchError("assembly_authority_mismatch", 409)
                    job.cancellation.event.set()
                    job.state = "cancelling"
                else:
                    if kind == "assemble_sequence":
                        if entry.assembly_job is not None:
                            raise ProductionWorkbenchError("assembly_already_started", 409)
                        self._validate_assemble_payload(payload, entry, current_projection)
                    elif (
                        kind != "retry_assembly"
                        or entry.assembly_job is None
                        or entry.assembly_job.state != "failed"
                        or payload["expected_assembly_fingerprint"]
                        != current_projection.fingerprint
                    ):
                        raise ProductionWorkbenchError("assembly_authority_mismatch", 409)
                    pending_job = self._new_assembly_job(
                        entry=entry,
                        request_digest=request_digest,
                    )
                    old_bytes = self._assembly_job_bytes(entry.assembly_job)
                    next_bytes = (
                        entry.authority_bytes - old_bytes + self._assembly_job_bytes(pending_job)
                    )
                    aggregate = (
                        sum(item.authority_bytes for item in self._entries.values())
                        + sum(item.authority_bytes for item in self._staged.values())
                        - entry.authority_bytes
                        + next_bytes
                    )
                    if (
                        next_bytes > MAX_PRODUCTION_AUTHORITY_BYTES
                        or aggregate > MAX_PRODUCTION_REGISTRY_BYTES
                    ):
                        raise ProductionWorkbenchError("workspace_capacity", 429)
                    entry.assembly_job = pending_job
                    entry.authority_bytes = next_bytes
                entry.touched_at = self._clock()
                self._publish_ledger(
                    request_id=request_id,
                    request_digest=request_digest,
                    action=kind,
                    handle=handle,
                    workspace=entry.workspace,
                    now=entry.touched_at,
                )
                projection = self._projection(handle, entry)
        finally:
            entry.mutation_claim.release()
        if pending_job is not None:
            self._submit_assembly_job(handle, entry, pending_job)
            with self._lock:
                current = self._entries.get(handle)
                if current is entry:
                    projection = self._projection(handle, entry)
        return ProductionDispatchResult(200, projection)

    # Member mode --------------------------------------------------------------------------------

    def _open_admissions(self) -> tuple[_DestinationAdmission, ...]:
        return tuple(item for item in self._admissions.values() if item.state == "open")

    def _open_admission_for_destination(
        self,
        handle: str,
        *,
        excluding: _DestinationAdmission | None = None,
    ) -> _DestinationAdmission | None:
        return next(
            (
                item
                for item in self._open_admissions()
                if item.workspace_handle == handle and item is not excluding
            ),
            None,
        )

    def _occupied_entry_slots(self) -> int:
        # IMPORTANT: an open create admission holds an entry slot that no other writer may take;
        # a manual create that ignored it would refuse the admitted Start after its queue decision.
        pending_creates = sum(item.workspace_handle is None for item in self._open_admissions())
        return (
            len(self._entries)
            + len(self._editable_projects)
            + len(self._empty_projects)
            + len(self._staged)
            + pending_creates
        )

    def _reserved_registry_bytes(self) -> int:
        return (
            sum(item.authority_bytes for item in self._entries.values())
            + sum(item.document_bytes for item in self._editable_projects.values())
            + sum(item.authority_bytes for item in self._staged.values())
            + sum(_member_rows_bytes(item.members) for item in self._empty_projects.values())
            + len(self._open_admissions()) * PRODUCTION_MEMBER_ATTEMPT_RESERVE_BYTES
        )

    def _record_admission(self, admission: _DestinationAdmission) -> None:
        self._admissions[admission.request_id] = admission
        while len(self._admissions) > MAX_PRODUCTION_ADMISSION_RECORDS:
            closed = next(
                (key for key, item in self._admissions.items() if item.state != "open"),
                None,
            )
            if closed is None:  # pragma: no cover - open admissions are bounded far lower
                break
            self._admissions.pop(closed)

    def _destination_busy(
        self,
        handle: str,
        entry: _ProductionEntry,
        *,
        excluding: _DestinationAdmission | None = None,
    ) -> bool:
        if self._open_admission_for_destination(handle, excluding=excluding) is not None:
            return True
        if entry.members is not None:
            return any(self._member_row_live(row) for row in entry.members.values())
        if entry.assembly_job is not None and entry.assembly_job.state in {
            "planned",
            "running",
            "cancelling",
        }:
            return True
        if self._live_sequence_probe is None or not self._live_sequence_probe(handle):
            return False
        sequence = entry.accepted_authorities.generation_sequence
        return sequence is None or any(
            runtime.state not in TERMINAL_MEMBER_JOB_STATES for runtime in sequence.state.runtimes
        )

    def _member_conversion(
        self,
        entry: _ProductionEntry,
    ) -> tuple[Mapping[str, _MemberRow], Mapping[str, MediaPreviewSourceAuthority]]:
        """Derive member rows from a legacy entry without changing it."""

        if entry.members is not None:
            return entry.members, entry.preview_sources
        authorities = entry.accepted_authorities
        if authorities.continuity_receipts or authorities.reconstruction_receipt is not None:
            raise ProductionWorkbenchError("project_append_unsupported", 422)
        rows: dict[str, _MemberRow] = {}
        sequence = authorities.generation_sequence
        receipts = {item.segment_id: item for item in authorities.artifact_receipts}
        if sequence is not None:
            source = sequence.state.plan.source_workspace_authority
            segment_ids = {item.segment_id for item in entry.workspace.segments}
            if source is None:
                raise ProductionWorkbenchError("project_append_unsupported", 422)
            try:
                # CRITICAL: keep the exact retained sequence, receipt and source objects. Rebuilding
                # or relabelling them to the live revision would forge provenance and change the
                # output handles every earlier import and preview is bound to.
                for job in sequence.state.plan.jobs:
                    if job.segment_id not in segment_ids:
                        raise ProductionMembershipError("member_conversion_segment")
                    attempt = ProductionMemberAttemptV1(
                        source, job.segment_id, sequence, receipts.get(job.segment_id)
                    )
                    rows[job.segment_id] = _MemberRow(
                        job.segment_id,
                        latest=attempt,
                        completed=attempt if attempt.verified else None,
                        order=0,
                    )
            except (ProductionMembershipError, ValueError) as exc:
                raise ProductionWorkbenchError("project_append_unsupported", 422) from exc
        if not set(receipts).issubset(rows):
            raise ProductionWorkbenchError("project_append_unsupported", 422)
        handles = {
            _opaque_artifact_output_handle(row.completed.artifact_receipt)
            for row in rows.values()
            if row.completed is not None and row.completed.artifact_receipt is not None
        }
        previews = MappingProxyType(
            {key: value for key, value in entry.preview_sources.items() if key in handles}
        )
        return MappingProxyType(rows), previews

    @staticmethod
    def _member_previews(
        rows: Mapping[str, _MemberRow],
        preview_sources: Mapping[str, MediaPreviewSourceAuthority],
    ) -> Mapping[str, MediaPreviewSourceAuthority]:
        handles = {
            _opaque_artifact_output_handle(row.completed.artifact_receipt)
            for row in rows.values()
            if row.completed is not None and row.completed.artifact_receipt is not None
        }
        return MappingProxyType(
            {key: value for key, value in preview_sources.items() if key in handles}
        )

    def _checked_member_bytes(
        self,
        entry: _ProductionEntry | None,
        workspace: MultiSegmentWorkspace,
        previews: Mapping[str, MediaPreviewSourceAuthority],
        rows: Mapping[str, _MemberRow],
    ) -> int:
        authority_bytes = _retained_authority_bytes(
            workspace, _EMPTY_ACCEPTED_AUTHORITIES, previews, None, None, rows
        )
        aggregate = (
            sum(item.authority_bytes for item in self._entries.values())
            + sum(item.authority_bytes for item in self._staged.values())
            - (0 if entry is None else entry.authority_bytes)
            + authority_bytes
        )
        if (
            authority_bytes > MAX_PRODUCTION_AUTHORITY_BYTES
            or aggregate > MAX_PRODUCTION_REGISTRY_BYTES
        ):
            raise ProductionWorkbenchError("workspace_capacity", 429)
        return authority_bytes

    def _admission_result(self, admission: _DestinationAdmission) -> ProductionDispatchResult:
        if admission.protocol == "v2":
            if admission.workspace_handle is None:
                raise ProductionWorkbenchError("member_publication_authority", 409)
            return ProductionDispatchResult(
                201 if admission.intent == "create" else 200,
                self._accumulated_projection(admission.workspace_handle, admission),
            )
        if admission.workspace_handle is None:
            return ProductionDispatchResult(202)
        entry = self._entries.get(admission.workspace_handle)
        if entry is None:
            self._terminal_or_missing(admission.workspace_handle)
        return ProductionDispatchResult(200, self._projection(admission.workspace_handle, entry))

    def _admit_destination_v2(
        self,
        action: dict[str, object],
        request_digest: str,
    ) -> ProductionDispatchResult:
        """Create or reserve a versioned accumulated project before any host effect."""

        request_id = str(action["request_id"])
        payload = cast(dict[str, object], action["payload"])
        now = self._clock()
        with self._lock:
            self._prune(now)
            previous = self._admissions.get(request_id)
            if previous is not None:
                if previous.request_digest != request_digest or previous.protocol != "v2":
                    raise ProductionWorkbenchError("request_id_conflict", 409)
                if previous.state == "released":
                    raise ProductionWorkbenchError("admission_closed", 409)
                return self._admission_result(previous)
            if request_id in self._ledger:
                raise ProductionWorkbenchError("request_id_conflict", 409)
            open_admissions = self._open_admissions()
            if len(open_admissions) >= MAX_PRODUCTION_DESTINATION_ADMISSIONS:
                raise ProductionWorkbenchError("admission_capacity", 429)
            if (
                self._reserved_registry_bytes() + PRODUCTION_MEMBER_ATTEMPT_RESERVE_BYTES
                > MAX_PRODUCTION_REGISTRY_BYTES
            ):
                raise ProductionWorkbenchError("workspace_capacity", 429)

            target_handle = payload["workspace_handle"]
            target_segment = payload["segment_id"]
            if target_handle is None:
                if self._occupied_entry_slots() >= self._max_entries:
                    raise ProductionWorkbenchError("workspace_capacity", 429)
                handle = self._new_workspace_handle()
                workspace_id = self._new_workspace_id()
                segment_id = "segment_" + secrets.token_urlsafe(18)
                project = _EmptyAccumulatedProject(workspace_id=workspace_id, touched_at=now)
                self._empty_projects[handle] = project
                self._project_revisions[handle] = 1
                intent = "create"
            else:
                handle = str(target_handle)
                workspace_id = str(payload["workspace_id"])
                entry = self._entries.get(handle)
                empty = self._empty_projects.get(handle)
                if entry is None and empty is None:
                    self._terminal_or_missing(handle)
                if handle not in self._project_revisions:
                    raise ProductionWorkbenchError("accumulation_version_mismatch", 409)
                if entry is None:
                    if empty is None:
                        self._terminal_or_missing(handle)
                    actual_id = empty.workspace_id
                else:
                    actual_id = entry.workspace.workspace_id
                if actual_id != workspace_id:
                    raise ProductionWorkbenchError("destination_mismatch", 409)
                if entry is not None and self._destination_busy(handle, entry):
                    raise ProductionWorkbenchError(
                        "destination_busy", 409, projection=self._accumulated_projection(handle)
                    )
                # CRITICAL: an open admission reserves an empty destination before prepare. A
                # second Start here can otherwise queue a conflicting host effect for one project.
                if empty is not None and (
                    self._open_admission_for_destination(handle) is not None
                    or any(self._member_row_live(row) for row in empty.members.values())
                ):
                    raise ProductionWorkbenchError(
                        "destination_busy", 409, projection=self._accumulated_projection(handle)
                    )
                if target_segment is None:
                    committed_count = 0 if entry is None else len(entry.workspace.segments)
                    if committed_count >= MAX_WORKSPACE_SEGMENTS:
                        raise ProductionWorkbenchError("project_capacity", 429)
                    segment_id = "segment_" + secrets.token_urlsafe(18)
                    intent = "append"
                else:
                    segment_id = str(target_segment)
                    rows = (
                        empty.members
                        if empty is not None
                        else (
                            _EMPTY_MEMBERS
                            if entry is None or entry.members is None
                            else entry.members
                        )
                    )
                    committed = bool(
                        entry is not None
                        and any(item.segment_id == segment_id for item in entry.workspace.segments)
                    )
                    provisional = segment_id in rows and rows[segment_id].completed is None
                    preprepare = self._preprepare_terminals.get(handle)
                    settled_retry = bool(
                        preprepare is not None
                        and preprepare.candidate_id == segment_id
                        and preprepare.status in {"failed", "cancelled"}
                    )
                    if not committed and not provisional and not settled_retry:
                        raise ProductionWorkbenchError("destination_mismatch", 409)
                    if provisional and self._member_row_live(rows[segment_id]):
                        raise ProductionWorkbenchError("member_not_retryable", 409)
                    intent = "regenerate" if committed else "retry"
            admission = _DestinationAdmission(
                request_id,
                request_digest,
                handle,
                workspace_id,
                segment_id,
                now + PRODUCTION_DESTINATION_ADMISSION_TTL_SECONDS,
                protocol="v2",
                intent_kind=intent,
                candidate_id=segment_id,
                attempt_id="attempt_" + secrets.token_urlsafe(18),
            )
            self._record_admission(admission)
            self._advance_project_revision(handle)
            return self._admission_result(admission)

    def _admit_destination(
        self,
        action: dict[str, object],
        request_digest: str,
    ) -> ProductionDispatchResult:
        """Reserve one generation destination before the host queue call; mutate no project."""

        request_id = str(action["request_id"])
        payload = cast(dict[str, object], action["payload"])
        now = self._clock()
        with self._lock:
            self._prune(now)
            previous = self._admissions.get(request_id)
            if previous is not None:
                if previous.request_digest != request_digest:
                    raise ProductionWorkbenchError("request_id_conflict", 409)
                if previous.state == "released":
                    raise ProductionWorkbenchError("admission_closed", 409)
                return self._admission_result(previous)
            if request_id in self._ledger:
                raise ProductionWorkbenchError("request_id_conflict", 409)
            open_admissions = self._open_admissions()
            reserved_bytes = self._reserved_registry_bytes()
            if payload["workspace_handle"] is None:
                if (
                    self._occupied_entry_slots() >= self._max_entries
                    or reserved_bytes + PRODUCTION_MEMBER_ATTEMPT_RESERVE_BYTES
                    > MAX_PRODUCTION_REGISTRY_BYTES
                ):
                    raise ProductionWorkbenchError("workspace_capacity", 429)
                if len(open_admissions) >= MAX_PRODUCTION_DESTINATION_ADMISSIONS:
                    raise ProductionWorkbenchError("admission_capacity", 429)
                self._record_admission(
                    _DestinationAdmission(
                        request_id,
                        request_digest,
                        None,
                        None,
                        None,
                        now + PRODUCTION_DESTINATION_ADMISSION_TTL_SECONDS,
                    )
                )
                return ProductionDispatchResult(202)
            handle = str(payload["workspace_handle"])
            workspace_id = str(payload["workspace_id"])
            segment_value = payload["segment_id"]
            segment_id = None if segment_value is None else str(segment_value)
            if handle in self._empty_projects or handle in self._project_revisions:
                raise ProductionWorkbenchError("accumulation_version_mismatch", 409)
            entry = self._entries.get(handle)
            if entry is None:
                self._terminal_or_missing(handle)
            if entry.workspace.workspace_id != workspace_id:
                raise ProductionWorkbenchError("destination_mismatch", 409)
            if self._destination_busy(handle, entry):
                raise ProductionWorkbenchError(
                    "destination_busy", 409, projection=self._projection(handle, entry)
                )
            if segment_id is None:
                if len(entry.workspace.segments) >= MAX_WORKSPACE_SEGMENTS:
                    raise ProductionWorkbenchError("project_capacity", 429)
            else:
                if not any(item.segment_id == segment_id for item in entry.workspace.segments):
                    raise ProductionWorkbenchError("destination_mismatch", 409)
                row = None if entry.members is None else entry.members.get(segment_id)
                if row is not None and self._member_row_live(row):
                    raise ProductionWorkbenchError(
                        "member_not_retryable", 409, projection=self._projection(handle, entry)
                    )
            self._member_conversion(entry)
            if (
                entry.authority_bytes + PRODUCTION_MEMBER_ATTEMPT_RESERVE_BYTES
                > MAX_PRODUCTION_AUTHORITY_BYTES
                or reserved_bytes + PRODUCTION_MEMBER_ATTEMPT_RESERVE_BYTES
                > MAX_PRODUCTION_REGISTRY_BYTES
            ):
                raise ProductionWorkbenchError("workspace_capacity", 429)
            if len(open_admissions) >= MAX_PRODUCTION_DESTINATION_ADMISSIONS:
                raise ProductionWorkbenchError("admission_capacity", 429)
            self._record_admission(
                _DestinationAdmission(
                    request_id,
                    request_digest,
                    handle,
                    workspace_id,
                    segment_id,
                    now + PRODUCTION_DESTINATION_ADMISSION_TTL_SECONDS,
                )
            )
            return ProductionDispatchResult(200, self._projection(handle, entry))

    def _release_destination(self, action: dict[str, object]) -> ProductionDispatchResult:
        payload = cast(dict[str, object], action["payload"])
        request_id = str(payload["admission_request_id"])
        now = self._clock()
        with self._lock:
            self._prune(now)
            admission = self._admissions.get(request_id)
            requested_protocol = "v2" if str(action["action"]).endswith("_v2") else "v1"
            if admission is not None and admission.protocol != requested_protocol:
                raise ProductionWorkbenchError("accumulation_version_mismatch", 409)
            # A consumed admission already became a member; releasing it never undoes that.
            if admission is not None and admission.state == "open":
                admission.state = "released"
                admission.closed_at = now
                if admission.protocol == "v2" and admission.workspace_handle is not None:
                    # Releasing removes the admitted attempt from the owned envelope, so its CAS
                    # revision must change with the fingerprint exposed by the next ordinary read.
                    self._advance_project_revision(admission.workspace_handle)
        return ProductionDispatchResult(204)

    def _settle_destination(
        self,
        action: dict[str, object],
        request_digest: str,
    ) -> ProductionDispatchResult:
        """Retain one accepted pre-prepare terminal without creating project content."""

        payload = cast(dict[str, object], action["payload"])
        admission_request_id = str(payload["admission_request_id"])
        settlement_request_id = str(action["request_id"])
        terminal = str(payload["terminal"])
        now = self._clock()
        with self._lock:
            self._prune(now)
            admission = self._admissions.get(admission_request_id)
            if admission is None:
                raise ProductionWorkbenchError("admission_unavailable", 409)
            if admission.protocol != "v2":
                raise ProductionWorkbenchError("accumulation_version_mismatch", 409)
            if admission.state == "settled":
                if (
                    admission.settlement_request_id != settlement_request_id
                    or admission.settlement_request_digest != request_digest
                ):
                    raise ProductionWorkbenchError("request_id_conflict", 409)
                if admission.workspace_handle is None:
                    raise ProductionWorkbenchError("member_publication_authority", 409)
                return ProductionDispatchResult(
                    200, self._accumulated_projection(admission.workspace_handle)
                )
            if admission.state != "open" or now >= admission.expires_at:
                raise ProductionWorkbenchError("admission_closed", 409)
            if (
                admission.workspace_handle is None
                or admission.candidate_id is None
                or admission.attempt_id is None
                or admission.segment_id is None
            ):
                raise ProductionWorkbenchError("member_publication_authority", 409)
            handle = admission.workspace_handle
            entry = self._entries.get(handle)
            empty = self._empty_projects.get(handle)
            if entry is None and empty is None:
                self._terminal_or_missing(handle)
            # IMPORTANT: a host terminal can precede ProductShell bootstrap and coordinator
            # prepare. Retain only the admission's server-minted identities; fabricating a run or
            # segment here would count failed video and make a later Retry target the wrong owner.
            self._member_order += 1
            self._preprepare_terminals[handle] = _PreprepareTerminal(
                admission.candidate_id,
                admission.attempt_id,
                admission.segment_id,
                terminal,
                self._member_order,
            )
            admission.state = "settled"
            admission.closed_at = now
            admission.settlement_request_id = settlement_request_id
            admission.settlement_request_digest = request_digest
            if entry is not None:
                entry.touched_at = now
            elif empty is not None:
                empty.touched_at = now
            self._advance_project_revision(handle)
            return ProductionDispatchResult(200, self._accumulated_projection(handle))

    def _require_open_admission(
        self,
        claim: ProductionDestinationAdmissionClaim,
        now: float,
    ) -> _DestinationAdmission:
        admission = claim._admission
        if (
            type(claim) is not ProductionDestinationAdmissionClaim
            or claim._registry is not self
            or self._admissions.get(claim.request_id) is not admission
            or admission.state != "open"
            or now >= admission.expires_at
        ):
            raise ProductionWorkbenchError("admission_unavailable", 409)
        return admission

    def claim_destination_admission(self, request_id: str) -> ProductionDestinationAdmissionClaim:
        """Resolve one open admission for a managed prepare; consuming it is publication's job."""

        if type(request_id) is not str or _IDENTIFIER.fullmatch(request_id) is None:
            raise ProductionWorkbenchError("invalid_admission_request_id", 400)
        now = self._clock()
        with self._lock:
            self._prune(now)
            admission = self._admissions.get(request_id)
            if admission is None or admission.state != "open" or now >= admission.expires_at:
                raise ProductionWorkbenchError("admission_unavailable", 409)
            return ProductionDestinationAdmissionClaim(
                request_id=request_id,
                intent=admission.intent,
                workspace_handle=admission.workspace_handle,
                workspace_id=admission.workspace_id,
                segment_id=admission.segment_id,
                _admission=admission,
                _registry=self,
            )

    def release_destination_claim(self, claim: ProductionDestinationAdmissionClaim) -> None:
        """Release the claimed admission after a prepare failed before publishing its member.

        A consumed admission already became a member and stays consumed; only its exact open
        record is released, so a stale claim can never close a newer admission.
        """

        if type(claim) is not ProductionDestinationAdmissionClaim or claim._registry is not self:
            return
        now = self._clock()
        with self._lock:
            admission = claim._admission
            if self._admissions.get(claim.request_id) is admission and admission.state == "open":
                admission.state = "released"
                admission.closed_at = now

    def build_member_execution(
        self,
        claim: ProductionDestinationAdmissionClaim,
        context_workspace_handle: str,
    ) -> ProductionMemberExecution:
        """Build the independent single-segment workspace one admitted member executes against."""

        context_handle = _sidebar_handle(context_workspace_handle)
        try:
            seed = self._seed_claim(context_handle)
        except KeyError as exc:
            raise ProductionWorkbenchError("workspace_unavailable", 404) from exc
        now = self._clock()
        with self._lock:
            self._prune(now)
            self._require_open_admission(claim, now)
            if claim._admission.protocol == "v2":
                if claim.segment_id is None or claim.workspace_handle is None:
                    raise ProductionWorkbenchError("destination_mismatch", 409)
                entry = self._entries.get(claim.workspace_handle)
                empty = self._empty_projects.get(claim.workspace_handle)
                if entry is None and empty is None:
                    self._terminal_or_missing(claim.workspace_handle)
                if claim.intent == "regenerate" and (
                    entry is None
                    or not any(
                        item.segment_id == claim.segment_id for item in entry.workspace.segments
                    )
                ):
                    raise ProductionWorkbenchError("destination_mismatch", 409)
                segment_id = claim.segment_id
            elif claim.workspace_handle is None or claim.segment_id is None:
                segment_id = "segment_" + secrets.token_urlsafe(18)
            else:
                entry = self._entries.get(claim.workspace_handle)
                if entry is None:
                    self._terminal_or_missing(claim.workspace_handle)
                if not any(
                    item.segment_id == claim.segment_id for item in entry.workspace.segments
                ):
                    raise ProductionWorkbenchError("destination_mismatch", 409)
                segment_id = claim.segment_id
            workspace_id = self._new_workspace_id()
        declaration = _segment_from_seed(
            seed,
            segment_id=segment_id,
            relation=SegmentRelationKind.INDEPENDENT,
            predecessor_segment_id=None,
        )
        try:
            # IMPORTANT: every attempt executes against its own workspace so its receipt never
            # depends on a project revision that later appends, reorders and selections advance.
            workspace = create_workspace(
                workspace_id,
                (declaration,),
                accepted_intent_authorities=(
                    AcceptedIntentAuthority(segment_id, seed.accepted_intent_fingerprint),
                ),
                selected_segment_ids=(segment_id,),
            )
        except SegmentWorkspaceError as exc:
            raise ProductionWorkbenchError("action_rejected", 422) from exc
        return ProductionMemberExecution(
            claim=claim,
            workspace=workspace,
            segment_id=segment_id,
            lineage_token=seed.lineage_token,
            authoring_seed=_project_authoring_seed(seed),
        )

    def publish_member_admission(
        self,
        execution: ProductionMemberExecution,
        generation_sequence: GenerationSequenceProjection,
        *,
        run_handle: str,
    ) -> ProductionMemberPublication:
        """Atomically consume an admission and install its planned member attempt."""

        if (
            type(execution) is not ProductionMemberExecution
            or execution.claim._registry is not self
            or type(run_handle) is not str
            or type(generation_sequence) is not GenerationSequenceProjection
        ):
            raise ProductionWorkbenchError("member_publication_authority", 409)
        try:
            attempt = ProductionMemberAttemptV1(
                execution.workspace, execution.segment_id, generation_sequence
            )
        except ProductionMembershipError as exc:
            raise ProductionWorkbenchError("member_attempt_invalid", 422) from exc
        if attempt.terminal:
            raise ProductionWorkbenchError("member_attempt_invalid", 422)
        claim = execution.claim
        if claim._admission.protocol == "v2" and claim.workspace_handle in self._empty_projects:
            return self._publish_empty_member(execution, attempt, run_handle)
        if claim.workspace_handle is None:
            return self._publish_created_member(execution, attempt, run_handle)
        handle = claim.workspace_handle
        with self._lock:
            self._prune(self._clock())
            entry = self._entries.get(handle)
            if entry is None:
                self._terminal_or_missing(handle)
        mutation_claim = entry.mutation_claim
        if not mutation_claim.acquire(timeout=self._mutation_timeout_seconds):
            raise ProductionWorkbenchError("workspace_busy", 423)
        try:
            with self._lock:
                now = self._clock()
                self._prune(now)
                current = self._entries.get(handle)
                if current is None or current.mutation_claim is not mutation_claim:
                    self._terminal_or_missing(handle)
                entry = current
                admission = self._require_open_admission(claim, now)
                if entry.workspace.workspace_id != claim.workspace_id:
                    raise ProductionWorkbenchError("destination_mismatch", 409)
                if self._destination_busy(handle, entry, excluding=admission):
                    raise ProductionWorkbenchError(
                        "destination_busy", 409, projection=self._projection(handle, entry)
                    )
                rows, previews = self._member_conversion(entry)
                segments = entry.workspace.segments
                segment_id = execution.segment_id
                previous_row = rows.get(segment_id)
                workspace = entry.workspace
                appending = claim.segment_id is None or claim.intent in {"append", "retry"}
                if appending:
                    if len(segments) >= MAX_WORKSPACE_SEGMENTS:
                        raise ProductionWorkbenchError("project_capacity", 429)
                    if any(item.segment_id == segment_id for item in segments):
                        raise ProductionWorkbenchError("destination_mismatch", 409)
                    if claim._admission.protocol == "v2":
                        # CRITICAL: automatic declarations stay provisional until their exact
                        # output verifies. Publishing here creates phantom duration/selection.
                        rows = MappingProxyType(
                            {
                                key: row
                                for key, row in rows.items()
                                if key in {item.segment_id for item in segments}
                            }
                        )
                    else:
                        try:
                            workspace = self._next_workspace(
                                entry,
                                segments=segments + (attempt.declaration,),
                                selected_segment_ids=(
                                    entry.workspace.selected_segment_ids + (segment_id,)
                                ),
                            )
                        except SegmentWorkspaceError as exc:
                            raise ProductionWorkbenchError("action_rejected", 422) from exc
                else:
                    if segment_id != claim.segment_id or not any(
                        item.segment_id == segment_id for item in segments
                    ):
                        raise ProductionWorkbenchError("destination_mismatch", 409)
                    if previous_row is not None and self._member_row_live(previous_row):
                        raise ProductionWorkbenchError("member_not_retryable", 409)
                self._member_order += 1
                next_rows = MappingProxyType(
                    {
                        **rows,
                        segment_id: _MemberRow(
                            segment_id,
                            latest=attempt,
                            completed=None if previous_row is None else previous_row.completed,
                            order=self._member_order,
                            run_handle=run_handle,
                            run_bound=False,
                        ),
                    }
                )
                authority_bytes = self._checked_member_bytes(entry, workspace, previews, next_rows)
                candidate = _ProductionEntry(
                    workspace=workspace,
                    touched_at=now,
                    authority_bytes=authority_bytes,
                    preview_sources=previews,
                    artifact_store=(
                        entry.artifact_store
                        if any(row.completed is not None for row in next_rows.values())
                        else None
                    ),
                    mutation_claim=mutation_claim,
                    lineage_token=entry.lineage_token,
                    members=next_rows,
                )
                self._projection(handle, candidate)
                self._entries[handle] = candidate
                if claim._admission.protocol == "v2":
                    retained_terminal = self._preprepare_terminals.get(handle)
                    if (
                        retained_terminal is not None
                        and retained_terminal.candidate_id == segment_id
                    ):
                        self._preprepare_terminals.pop(handle, None)
                    self._advance_project_revision(handle)
                admission.state = "consumed"
                admission.closed_at = now
                admission.published_handle = handle
                return ProductionMemberPublication(
                    workspace_handle=handle,
                    workspace_id=workspace.workspace_id,
                    segment_id=segment_id,
                    attempt=attempt,
                    run_handle=run_handle,
                    created=False,
                    appended=appending,
                    previous_row=previous_row,
                    previous_entry=entry,
                    published_entry=candidate,
                    _registry=self,
                    protocol=claim._admission.protocol,
                )
        finally:
            mutation_claim.release()

    def _publish_empty_member(
        self,
        execution: ProductionMemberExecution,
        attempt: ProductionMemberAttemptV1,
        run_handle: str,
    ) -> ProductionMemberPublication:
        claim = execution.claim
        handle = claim.workspace_handle
        if handle is None:
            raise ProductionWorkbenchError("member_publication_authority", 409)
        with self._lock:
            now = self._clock()
            self._prune(now)
            admission = self._require_open_admission(claim, now)
            project = self._empty_projects.get(handle)
            if project is None or project.workspace_id != claim.workspace_id:
                raise ProductionWorkbenchError("destination_mismatch", 409)
            if any(self._member_row_live(row) for row in project.members.values()):
                raise ProductionWorkbenchError("destination_busy", 409)
            previous_row = project.members.get(execution.segment_id)
            self._member_order += 1
            # Keep only the committed candidate being retried or the newest provisional candidate.
            rows = MappingProxyType(
                {
                    execution.segment_id: _MemberRow(
                        execution.segment_id,
                        latest=attempt,
                        completed=None,
                        order=self._member_order,
                        run_handle=run_handle,
                        run_bound=False,
                    )
                }
            )
            project.members = rows
            project.lineage_token = execution.lineage_token
            project.pending_authoring_seed = execution.authoring_seed
            project.touched_at = now
            retained_terminal = self._preprepare_terminals.get(handle)
            if (
                retained_terminal is not None
                and retained_terminal.candidate_id == execution.segment_id
            ):
                self._preprepare_terminals.pop(handle, None)
            admission.state = "consumed"
            admission.closed_at = now
            admission.published_handle = handle
            self._advance_project_revision(handle)
            return ProductionMemberPublication(
                workspace_handle=handle,
                workspace_id=project.workspace_id,
                segment_id=execution.segment_id,
                attempt=attempt,
                run_handle=run_handle,
                created=claim.intent == "create",
                appended=True,
                previous_row=previous_row,
                previous_entry=None,
                published_entry=None,
                _registry=self,
                protocol="v2",
            )

    def _publish_created_member(
        self,
        execution: ProductionMemberExecution,
        attempt: ProductionMemberAttemptV1,
        run_handle: str,
    ) -> ProductionMemberPublication:
        claim = execution.claim
        now = self._clock()
        with self._lock:
            self._prune(now)
            admission = self._require_open_admission(claim, now)
            # The admission already reserved one entry slot; count only the other pending creates.
            pending_creates = sum(
                item.workspace_handle is None and item is not admission
                for item in self._open_admissions()
            )
            if (
                len(self._entries)
                + len(self._editable_projects)
                + len(self._empty_projects)
                + len(self._staged)
                + pending_creates
                >= self._max_entries
            ):
                raise ProductionWorkbenchError("workspace_capacity", 429)
            segment_id = execution.segment_id
            declaration = attempt.declaration
            try:
                workspace = create_workspace(
                    self._new_workspace_id(),
                    (declaration,),
                    accepted_intent_authorities=(
                        AcceptedIntentAuthority(
                            segment_id, declaration.accepted_intent_fingerprint
                        ),
                    ),
                    selected_segment_ids=(segment_id,),
                )
            except SegmentWorkspaceError as exc:
                raise ProductionWorkbenchError("action_rejected", 422) from exc
            self._member_order += 1
            rows = MappingProxyType(
                {
                    segment_id: _MemberRow(
                        segment_id,
                        latest=attempt,
                        completed=None,
                        order=self._member_order,
                        run_handle=run_handle,
                        run_bound=False,
                    )
                }
            )
            authority_bytes = self._checked_member_bytes(
                None, workspace, _EMPTY_PREVIEW_SOURCES, rows
            )
            handle = self._new_workspace_handle()
            entry = _ProductionEntry(
                workspace,
                now,
                authority_bytes,
                lineage_token=execution.lineage_token,
                members=rows,
            )
            self._projection(handle, entry)
            self._entries[handle] = entry
            if execution.authoring_seed is not None:
                self._authoring_anchors[handle] = _ProjectAuthoringAnchor(execution.authoring_seed)
            admission.state = "consumed"
            admission.closed_at = now
            admission.published_handle = handle
            return ProductionMemberPublication(
                workspace_handle=handle,
                workspace_id=workspace.workspace_id,
                segment_id=segment_id,
                attempt=attempt,
                run_handle=run_handle,
                created=True,
                appended=True,
                previous_row=None,
                previous_entry=None,
                published_entry=entry,
                _registry=self,
            )

    def _published_member_row(
        self,
        publication: ProductionMemberPublication,
        code: str,
    ) -> tuple[_ProductionEntry, _MemberRow]:
        if (
            type(publication) is not ProductionMemberPublication
            or publication._registry is not self
        ):
            raise ProductionWorkbenchError(code, 409)
        entry = self._entries.get(publication.workspace_handle)
        row = (
            None
            if entry is None or entry.members is None
            else entry.members.get(publication.segment_id)
        )
        if (
            entry is None
            or row is None
            or row.latest is not publication.attempt
            or row.run_handle != publication.run_handle
        ):
            raise ProductionWorkbenchError(code, 409)
        return entry, row

    def confirm_member_run(self, publication: ProductionMemberPublication) -> None:
        """Record that the run owning a published member attempt now exists."""

        with self._lock:
            if (
                publication.protocol == "v2"
                and publication.workspace_handle in self._empty_projects
            ):
                project = self._empty_projects[publication.workspace_handle]
                row = project.members.get(publication.segment_id)
                if (
                    row is None
                    or row.latest is not publication.attempt
                    or row.run_handle != publication.run_handle
                    or row.run_bound
                ):
                    raise ProductionWorkbenchError("member_publication_authority", 409)
                project.members = MappingProxyType(
                    {**project.members, publication.segment_id: replace(row, run_bound=True)}
                )
                project.touched_at = self._clock()
                self._advance_project_revision(publication.workspace_handle)
                return
            entry, row = self._published_member_row(publication, "member_publication_authority")
            if row.run_bound:
                raise ProductionWorkbenchError("member_publication_authority", 409)
            members = cast(Mapping[str, _MemberRow], entry.members)
            self._entries[publication.workspace_handle] = replace(
                entry,
                members=MappingProxyType(
                    {**members, publication.segment_id: replace(row, run_bound=True)}
                ),
            )
            if publication.protocol == "v2":
                self._advance_project_revision(publication.workspace_handle)

    def _rollback_unpublished_member_attempt(
        self,
        publication: ProductionMemberPublication,
        *,
        release_live_sequence: Callable[[], None],
    ) -> None:
        """Undo exactly one member publication whose managed run never became visible."""

        if not callable(release_live_sequence):
            raise ProductionWorkbenchError("rollback_authority_mismatch", 409)
        with self._lock:
            if (
                publication.protocol == "v2"
                and publication.workspace_handle in self._empty_projects
            ):
                project = self._empty_projects[publication.workspace_handle]
                row = project.members.get(publication.segment_id)
                if row is None or row.latest is not publication.attempt:
                    raise ProductionWorkbenchError("rollback_authority_mismatch", 409)
                release_live_sequence()
                if self._live_run_probe is not None and self._live_run_probe(
                    publication.run_handle
                ):
                    raise ProductionWorkbenchError("rollback_authority_mismatch", 409)
                project.members = _EMPTY_MEMBERS
                project.touched_at = self._clock()
                self._advance_project_revision(publication.workspace_handle)
                return
            entry, row = self._published_member_row(publication, "rollback_authority_mismatch")
            handle = publication.workspace_handle
            segment_id = publication.segment_id
            release_live_sequence()
            if self._live_run_probe is not None and self._live_run_probe(publication.run_handle):
                raise ProductionWorkbenchError("rollback_authority_mismatch", 409)
            # CRITICAL: this is transaction rollback, not release. It restores only what this
            # publication changed: a created project disappears without a tombstone, and an append
            # or regeneration returns the project's earlier members, outputs and claims intact.
            if publication.created:
                if tuple(item.segment_id for item in entry.workspace.segments) != (segment_id,):
                    raise ProductionWorkbenchError("rollback_authority_mismatch", 409)
                self._entries.pop(handle)
                return
            if entry is publication.published_entry and publication.previous_entry is not None:
                previous = publication.previous_entry
                previous.touched_at = self._clock()
                self._entries[handle] = previous
                if publication.protocol == "v2":
                    self._advance_project_revision(handle)
                return
            members = dict(cast(Mapping[str, _MemberRow], entry.members))
            if publication.previous_row is None:
                members.pop(segment_id)
            else:
                members[segment_id] = publication.previous_row
            workspace = entry.workspace
            if publication.appended:
                segments = workspace.segments
                if any(item.segment_id == segment_id for item in segments):
                    if len(segments) == 1 or any(
                        item.predecessor_segment_id == segment_id for item in segments
                    ):
                        raise ProductionWorkbenchError("rollback_authority_mismatch", 409)
                    try:
                        workspace = self._next_workspace(
                            entry,
                            segments=tuple(
                                item for item in segments if item.segment_id != segment_id
                            ),
                            selected_segment_ids=tuple(
                                item
                                for item in workspace.selected_segment_ids
                                if item != segment_id
                            ),
                        )
                    except SegmentWorkspaceError as exc:
                        raise ProductionWorkbenchError("rollback_authority_mismatch", 409) from exc
            rows = MappingProxyType(members)
            previews = self._member_previews(rows, entry.preview_sources)
            candidate = _ProductionEntry(
                workspace=workspace,
                touched_at=self._clock(),
                authority_bytes=_retained_authority_bytes(
                    workspace, _EMPTY_ACCEPTED_AUTHORITIES, previews, None, None, rows
                ),
                preview_sources=previews,
                artifact_store=entry.artifact_store,
                mutation_claim=entry.mutation_claim,
                lineage_token=entry.lineage_token,
                members=rows,
            )
            self._projection(handle, candidate)
            self._entries[handle] = candidate
            if publication.protocol == "v2":
                self._advance_project_revision(handle)

    def publish_member_attempt(
        self,
        handle: str,
        *,
        segment_id: str,
        previous_attempt: ProductionMemberAttemptV1,
        generation_sequence: GenerationSequenceProjection,
        artifact_receipt: SegmentArtifactReceipt | None = None,
        artifact_store: PrivateSegmentArtifactStore | None = None,
        _prepared_preview_sources: Mapping[str, MediaPreviewSourceAuthority] | None = None,
    ) -> ProductionMemberAttemptV1:
        """CAS one member's latest attempt; never the project revision other members advance."""

        handle = _workspace_handle(handle)
        if (
            type(previous_attempt) is not ProductionMemberAttemptV1
            or previous_attempt.segment_id != segment_id
            or type(generation_sequence) is not GenerationSequenceProjection
            or generation_sequence.state.plan.fingerprint
            != previous_attempt.generation_sequence.state.plan.fingerprint
        ):
            raise ProductionWorkbenchError("stale_generation_sequence", 409)
        if artifact_store is not None and type(artifact_store) is not PrivateSegmentArtifactStore:
            raise TypeError("artifact store authority is invalid")
        try:
            attempt = ProductionMemberAttemptV1(
                previous_attempt.authority_workspace,
                segment_id,
                generation_sequence,
                artifact_receipt,
            )
        except ProductionMembershipError as exc:
            raise ProductionWorkbenchError("member_attempt_invalid", 422) from exc
        if attempt.artifact_receipt is not None and artifact_store is None:
            raise ProductionWorkbenchError("member_attempt_invalid", 422)
        with self._lock:
            empty = self._empty_projects.get(handle)
        if empty is not None:
            return self._publish_empty_member_attempt(
                handle,
                segment_id=segment_id,
                previous_attempt=previous_attempt,
                attempt=attempt,
                generation_sequence=generation_sequence,
                artifact_store=artifact_store,
                _prepared_preview_sources=_prepared_preview_sources,
            )
        with self._lock:
            self._prune(self._clock())
            entry = self._entries.get(handle)
            if entry is None:
                self._terminal_or_missing(handle)
            self._require_member_latest(entry, segment_id, previous_attempt)
        mutation_claim = entry.mutation_claim
        if not mutation_claim.acquire(timeout=self._mutation_timeout_seconds):
            raise ProductionWorkbenchError("workspace_busy", 423)
        try:
            receipt = attempt.artifact_receipt
            preview_source = None
            if _prepared_preview_sources is not None:
                previews = self._checked_prepared_preview_sources(
                    generation_sequence,
                    (() if receipt is None else (receipt,)),
                    _prepared_preview_sources,
                )
                preview_source = next(iter(previews.values()), None)
            elif attempt.verified and receipt is not None and artifact_store is not None:
                previews = self.prepare_generated_preview_sources(
                    generation_sequence,
                    (receipt,),
                    artifact_store,
                )
                preview_source = next(iter(previews.values()), None)
            with self._lock:
                now = self._clock()
                self._prune(now)
                current = self._entries.get(handle)
                if current is None or current.mutation_claim is not mutation_claim:
                    self._terminal_or_missing(handle)
                entry = current
                row = self._require_member_latest(entry, segment_id, previous_attempt)
                members = dict(cast(Mapping[str, _MemberRow], entry.members))
                workspace = entry.workspace
                previews = dict(entry.preview_sources)
                store = entry.artifact_store
                completed = row.completed
                if attempt.verified and receipt is not None:
                    retained_elsewhere = any(
                        other.completed is not None
                        for key, other in members.items()
                        if key != segment_id
                    )
                    if store is not None and store is not artifact_store and retained_elsewhere:
                        raise ProductionWorkbenchError("member_publication_authority", 409)
                    store = artifact_store
                    if completed is not None and completed.artifact_receipt is not None:
                        previews.pop(
                            _opaque_artifact_output_handle(completed.artifact_receipt), None
                        )
                    completed = attempt
                    if preview_source is not None:
                        previews[preview_source.opaque_output_handle] = preview_source
                    current_declaration = next(
                        (item for item in workspace.segments if item.segment_id == segment_id),
                        None,
                    )
                    if current_declaration is None:
                        if len(workspace.segments) >= MAX_WORKSPACE_SEGMENTS:
                            raise ProductionWorkbenchError("project_capacity", 429)
                        # CRITICAL: membership, delivered output and default selection publish in
                        # one validated workspace revision. Splitting these writes can expose a
                        # segment without authority or an output bound to a missing declaration.
                        try:
                            workspace = self._next_workspace(
                                entry,
                                segments=workspace.segments + (attempt.declaration,),
                                selected_segment_ids=workspace.selected_segment_ids + (segment_id,),
                            )
                        except SegmentWorkspaceError as exc:
                            raise ProductionWorkbenchError("action_rejected", 422) from exc
                    else:
                        replacement = replace(
                            attempt.declaration,
                            relation=current_declaration.relation,
                            predecessor_segment_id=current_declaration.predecessor_segment_id,
                        )
                    if current_declaration is not None and replacement != current_declaration:
                        # A regeneration from a changed Context replaces the declaration only
                        # after its output verified; failures keep the earlier declaration.
                        try:
                            workspace = self._next_workspace(
                                entry,
                                segments=tuple(
                                    replacement if item.segment_id == segment_id else item
                                    for item in workspace.segments
                                ),
                            )
                        except SegmentWorkspaceError as exc:
                            raise ProductionWorkbenchError("action_rejected", 422) from exc
                members[segment_id] = replace(row, latest=attempt, completed=completed)
                rows = MappingProxyType(members)
                retained_previews: Mapping[str, MediaPreviewSourceAuthority] = MappingProxyType(
                    previews
                )
                try:
                    _preview_sources_wire_bytes(retained_previews)
                except ValueError:
                    retained_previews = self._member_previews(rows, entry.preview_sources)
                authority_bytes = self._checked_member_bytes(
                    entry, workspace, retained_previews, rows
                )
                candidate = _ProductionEntry(
                    workspace=workspace,
                    touched_at=now,
                    authority_bytes=authority_bytes,
                    preview_sources=retained_previews,
                    artifact_store=store,
                    mutation_claim=mutation_claim,
                    lineage_token=entry.lineage_token,
                    members=rows,
                )
                self._projection(handle, candidate)
                self._entries[handle] = candidate
                if handle in self._project_revisions:
                    self._advance_project_revision(handle)
                return attempt
        finally:
            mutation_claim.release()

    def _publish_empty_member_attempt(
        self,
        handle: str,
        *,
        segment_id: str,
        previous_attempt: ProductionMemberAttemptV1,
        attempt: ProductionMemberAttemptV1,
        generation_sequence: GenerationSequenceProjection,
        artifact_store: PrivateSegmentArtifactStore | None,
        _prepared_preview_sources: Mapping[str, MediaPreviewSourceAuthority] | None = None,
    ) -> ProductionMemberAttemptV1:
        preview_source = None
        receipt = attempt.artifact_receipt
        if _prepared_preview_sources is not None:
            admitted_previews = self._checked_prepared_preview_sources(
                generation_sequence,
                (() if receipt is None else (receipt,)),
                _prepared_preview_sources,
            )
            preview_source = next(iter(admitted_previews.values()), None)
        elif attempt.verified and receipt is not None and artifact_store is not None:
            admitted_previews = self.prepare_generated_preview_sources(
                generation_sequence,
                (receipt,),
                artifact_store,
            )
            preview_source = next(iter(admitted_previews.values()), None)
        with self._lock:
            self._prune(self._clock())
            project = self._empty_projects.get(handle)
            row = None if project is None else project.members.get(segment_id)
            if project is None or row is None or row.latest is not previous_attempt:
                raise ProductionWorkbenchError("stale_generation_sequence", 409)
            if not attempt.verified or receipt is None or artifact_store is None:
                project.members = MappingProxyType({segment_id: replace(row, latest=attempt)})
                project.touched_at = self._clock()
                self._advance_project_revision(handle)
                return attempt
            try:
                workspace = create_workspace(
                    project.workspace_id,
                    (attempt.declaration,),
                    accepted_intent_authorities=(
                        AcceptedIntentAuthority(
                            segment_id,
                            attempt.declaration.accepted_intent_fingerprint,
                        ),
                    ),
                    selected_segment_ids=(segment_id,),
                )
            except SegmentWorkspaceError as exc:
                raise ProductionWorkbenchError("action_rejected", 422) from exc
            completed_row = replace(row, latest=attempt, completed=attempt)
            rows = MappingProxyType({segment_id: completed_row})
            previews: Mapping[str, MediaPreviewSourceAuthority] = MappingProxyType(
                {}
                if preview_source is None
                else {preview_source.opaque_output_handle: preview_source}
            )
            authority_bytes = _retained_authority_bytes(
                workspace,
                _EMPTY_ACCEPTED_AUTHORITIES,
                previews,
                None,
                None,
                rows,
            )
            aggregate_bytes = (
                sum(item.authority_bytes for item in self._entries.values())
                + sum(item.authority_bytes for item in self._staged.values())
                + sum(
                    _member_rows_bytes(item.members)
                    for key, item in self._empty_projects.items()
                    if key != handle
                )
                + authority_bytes
            )
            if (
                authority_bytes > MAX_PRODUCTION_AUTHORITY_BYTES
                or aggregate_bytes > MAX_PRODUCTION_REGISTRY_BYTES
            ):
                raise ProductionWorkbenchError("workspace_capacity", 429)
            candidate = _ProductionEntry(
                workspace=workspace,
                touched_at=self._clock(),
                authority_bytes=authority_bytes,
                preview_sources=previews,
                artifact_store=artifact_store,
                mutation_claim=project.mutation_claim,
                lineage_token=project.lineage_token,
                members=rows,
            )
            # CRITICAL: validate the complete committed projection before swapping the empty
            # envelope. A failed validation must leave zero segments and a retryable attempt.
            self._projection(handle, candidate)
            self._entries[handle] = candidate
            self._empty_projects.pop(handle, None)
            if project.pending_authoring_seed is not None:
                self._authoring_anchors[handle] = _ProjectAuthoringAnchor(
                    project.pending_authoring_seed
                )
            self._advance_project_revision(handle)
            return attempt

    @staticmethod
    def _require_member_latest(
        entry: _ProductionEntry,
        segment_id: str,
        previous_attempt: ProductionMemberAttemptV1,
    ) -> _MemberRow:
        row = None if entry.members is None else entry.members.get(segment_id)
        if row is None or row.latest is not previous_attempt:
            raise ProductionWorkbenchError("stale_generation_sequence", 409)
        return row

    def release_member_run(
        self,
        handle: str,
        *,
        segment_id: str,
        previous_attempt: ProductionMemberAttemptV1,
        settled_sequence: GenerationSequenceProjection | None,
        release_live_sequence: Callable[[], None],
    ) -> None:
        """Release one member run inside its removal transaction, settling its attempt first."""

        if not callable(release_live_sequence):
            raise ProductionWorkbenchError("member_release_authority", 409)
        settled = None
        if settled_sequence is not None:
            try:
                settled = ProductionMemberAttemptV1(
                    previous_attempt.authority_workspace,
                    segment_id,
                    settled_sequence,
                    previous_attempt.artifact_receipt,
                )
            except ProductionMembershipError as exc:
                raise ProductionWorkbenchError("member_attempt_invalid", 422) from exc
        with self._lock:
            empty = self._empty_projects.get(handle)
            empty_row = None if empty is None else empty.members.get(segment_id)
            if (
                empty is not None
                and empty_row is not None
                and empty_row.latest is previous_attempt
                and settled is not None
            ):
                empty.members = MappingProxyType({segment_id: replace(empty_row, latest=settled)})
                empty.touched_at = self._clock()
                # Settle the authority before the coordinator removes the run owner.
                release_live_sequence()
                self._advance_project_revision(handle)
                return
            entry = self._entries.get(handle)
            row = None if entry is None or entry.members is None else entry.members.get(segment_id)
            if (
                entry is None
                or row is None
                or row.latest is not previous_attempt
                or settled is None
            ):
                release_live_sequence()
                return
            members = cast(Mapping[str, _MemberRow], entry.members)
            rows = MappingProxyType({**members, segment_id: replace(row, latest=settled)})
            candidate = replace(
                entry,
                members=rows,
                authority_bytes=_retained_authority_bytes(
                    entry.workspace,
                    _EMPTY_ACCEPTED_AUTHORITIES,
                    entry.preview_sources,
                    None,
                    None,
                    rows,
                ),
                touched_at=self._clock(),
            )
            self._projection(handle, candidate)
            # CRITICAL: settle before the run disappears, inside the one release transaction. A
            # pre-submit release that dropped the run first would leave a planned attempt that no
            # run can ever cancel, reported as unknown ownership instead of cancelled.
            release_live_sequence()
            self._entries[handle] = candidate
            if handle in self._project_revisions:
                self._advance_project_revision(handle)

    def _commit_member_mode_mutation(
        self,
        *,
        action: dict[str, object],
        request_digest: str,
        handle: str,
        entry: _ProductionEntry,
        workspace: MultiSegmentWorkspace,
        seed: SidebarProductionSeed | None,
        now: float,
    ) -> ProductionDispatchResult:
        members = cast(Mapping[str, _MemberRow], entry.members)
        before = {item.segment_id: item for item in entry.workspace.segments}
        after = {item.segment_id: item for item in workspace.segments}
        retained: dict[str, _MemberRow] = {}
        for segment_id, row in members.items():
            if after.get(segment_id) == before.get(segment_id):
                retained[segment_id] = row
                continue
            # IMPORTANT: deleting or redefining a member drops its facts. A live attempt would then
            # publish a result for a declaration the project no longer has, so refuse instead.
            if self._member_row_live(row):
                raise ProductionWorkbenchError(
                    "member_live", 409, projection=self._projection(handle, entry)
                )
        rows = MappingProxyType(retained)
        previews = self._member_previews(rows, entry.preview_sources)
        authority_bytes = self._checked_member_bytes(entry, workspace, previews, rows)
        candidate = _ProductionEntry(
            workspace=workspace,
            touched_at=now,
            authority_bytes=authority_bytes,
            preview_sources=previews,
            artifact_store=(
                entry.artifact_store
                if any(row.completed is not None for row in rows.values())
                else None
            ),
            mutation_claim=entry.mutation_claim,
            lineage_token=(
                entry.lineage_token
                if seed is None or seed.lineage_token is entry.lineage_token
                else None
            ),
            members=rows,
        )
        projection = self._projection(handle, candidate)
        self._entries[handle] = candidate
        self._publish_ledger(
            request_id=str(action["request_id"]),
            request_digest=request_digest,
            action=str(action["action"]),
            handle=handle,
            workspace=workspace,
            now=now,
        )
        return ProductionDispatchResult(200, projection)

    def dispatch(self, action_value: object) -> ProductionDispatchResult:
        action = _validate_action(action_value)
        request_digest = canonical_fingerprint(action)
        payload = cast(dict[str, object], action["payload"])
        with self._lock:
            handle = str(payload.get("workspace_handle", ""))
            editable = self._editable_projects.get(handle)
            if editable is not None:
                # IMPORTANT: this early data-only branch must honor the same TTL as Context owners.
                self._prune(self._clock())
                editable = self._editable_projects.get(handle)
                if editable is None:
                    self._terminal_or_missing(handle)
                return self._dispatch_editable(action, request_digest, handle, editable)
        if action["action"] == "admit_generation_destination_v2":
            return self._admit_destination_v2(action, request_digest)
        if action["action"] == "release_generation_destination_v2":
            return self._release_destination(action)
        if action["action"] == "settle_generation_destination_v2":
            return self._settle_destination(action, request_digest)
        if action["action"] == "read_accumulated_project":
            accumulated_payload = cast(dict[str, object], action["payload"])
            handle = str(accumulated_payload["workspace_handle"])
            with self._lock:
                self._prune(self._clock())
                if (
                    handle in self._entries or handle in self._empty_projects
                ) and handle not in self._project_revisions:
                    raise ProductionWorkbenchError("accumulation_version_mismatch", 409)
                accumulated_projection = self._accumulated_projection(handle)
                if accumulated_projection.workspace_id != accumulated_payload["workspace_id"]:
                    raise ProductionWorkbenchError("destination_mismatch", 409)
                entry = self._entries.get(handle)
                empty = self._empty_projects.get(handle)
                if entry is not None:
                    entry.touched_at = self._clock()
                elif empty is not None:
                    empty.touched_at = self._clock()
                return ProductionDispatchResult(200, accumulated_projection)
        if action["action"] == "admit_generation_destination":
            return self._admit_destination(action, request_digest)
        if action["action"] == "release_generation_destination":
            return self._release_destination(action)

        # IMPORTANT: replay is owned by the Production ledger, not by the source
        # Sidebar entry. Check it before a seed claim so an exact retry stays
        # idempotent after the source handle expires or otherwise becomes unavailable.
        if action["action"] in {
            "create_workspace_from_context",
            "add_segment_from_context",
            "replace_segment_from_context",
        }:
            with self._lock:
                self._prune(self._clock())
                replay = self._replay(
                    request_id=str(action["request_id"]),
                    request_digest=request_digest,
                )
                if replay is not None:
                    return replay
        seed = self._claim_seed(action)
        if action["action"] == "create_workspace_from_context":
            if seed is None:  # pragma: no cover - guarded by _claim_seed
                raise ProductionWorkbenchError("workspace_unavailable", 404)
            return self._create(action, seed, request_digest)

        payload = cast(dict[str, object], action["payload"])
        if type(payload) is not dict:  # pragma: no cover - admitted above
            raise ProductionWorkbenchError("invalid_action", 400)
        handle = _workspace_handle(payload["workspace_handle"])
        now = self._clock()
        with self._lock:
            self._prune(now)
            if action["action"] == "release_workspace" and handle in self._tombstones:
                tombstone = self._tombstones[handle]
                if tombstone.disposition == "released" and (
                    tombstone.release_request_id == action["request_id"]
                ):
                    if tombstone.release_request_digest == request_digest:
                        return ProductionDispatchResult(204)
                    raise ProductionWorkbenchError("request_id_conflict", 409)
                raise ProductionWorkbenchError("workspace_gone", 410)
            entry = self._entries.get(handle)
            if entry is None:
                self._terminal_or_missing(handle)
            if action["action"] == "read_projection":
                entry.touched_at = now
                return ProductionDispatchResult(200, self._projection(handle, entry))

        if action["action"] in {
            "assemble_sequence",
            "cancel_assembly",
            "retry_assembly",
        }:
            return self._dispatch_assembly(action, request_digest, handle, entry)

        if not entry.mutation_claim.acquire(timeout=self._mutation_timeout_seconds):
            raise ProductionWorkbenchError("workspace_busy", 423)
        try:
            now = self._clock()
            with self._lock:
                self._prune(now)
                current = self._entries.get(handle)
                if current is None or current is not entry:
                    self._terminal_or_missing(handle)
                replay = self._replay(
                    request_id=str(action["request_id"]), request_digest=request_digest
                )
                if replay is not None:
                    return replay
                self._check_expected(payload, handle, entry)
                if action["action"] == "release_workspace":
                    if (
                        self._editor_recovery_protected is not None
                        and self._editor_recovery_protected(handle)
                    ):
                        raise ProductionWorkbenchError("dirty_recovery_protected", 409)
                    if (
                        self._live_sequence_probe is not None and self._live_sequence_probe(handle)
                    ) or (
                        entry.members is not None
                        and any(self._member_row_live(row) for row in entry.members.values())
                    ):
                        raise ProductionWorkbenchError("workspace_sequence_live", 409)
                    self._cancel_entry_assembly(entry)
                    self._entries.pop(handle, None)
                    self._notify_editor_recovery(handle)
                    self._project_revisions.pop(handle, None)
                    self._record_tombstone(
                        handle,
                        _Tombstone(
                            "released",
                            now,
                            str(action["request_id"]),
                            request_digest,
                        ),
                    )
                    return ProductionDispatchResult(204)

                self._ensure_ledger_capacity(str(action["request_id"]))
                try:
                    workspace = self._mutate_workspace(
                        action=str(action["action"]),
                        payload=payload,
                        entry=entry,
                        seed=seed,
                    )
                except SegmentWorkspaceError as exc:
                    raise ProductionWorkbenchError("action_rejected", 422) from exc
                if entry.members is not None:
                    return self._commit_member_mode_mutation(
                        action=action,
                        request_digest=request_digest,
                        handle=handle,
                        entry=entry,
                        workspace=workspace,
                        seed=seed,
                        now=now,
                    )
                retained_authorities = _EMPTY_ACCEPTED_AUTHORITIES
                retained_preview_sources = _EMPTY_PREVIEW_SOURCES
                completed_assembly = None
                # IMPORTANT: selection advances the workspace CAS identity but not generation
                # structure. Compare typed declarations and revalidate the historical source;
                # action names or revision-derived manifest fingerprints would drop valid runs.
                if _is_structural_successor(workspace, entry.workspace):
                    try:
                        _validate_accepted_authorities(workspace, entry.accepted_authorities)
                    except ProductionWorkbenchError:
                        pass
                    else:
                        retained_authorities = entry.accepted_authorities
                        retained_preview_sources = entry.preview_sources
                        if retained_authorities.assembly_execution is not None:
                            completed_assembly = self._assembly_projection(entry)
                # IMPORTANT: every manual workspace revision invalidates the exact automatic-plan
                # CAS, even when it only changes selection. Retaining the old plan under a new
                # workspace fingerprint would let managed start borrow stale user approval.
                # Preserve only the actual immutable completed-job projection for subset import;
                # retaining a live job/runtime would carry executable authority across that CAS.
                authority_bytes = _retained_authority_bytes(
                    workspace,
                    retained_authorities,
                    retained_preview_sources,
                    None,
                    completed_assembly,
                )
                aggregate = (
                    sum(item.authority_bytes for item in self._entries.values())
                    + sum(item.authority_bytes for item in self._staged.values())
                    - entry.authority_bytes
                    + authority_bytes
                )
                if (
                    authority_bytes > MAX_PRODUCTION_AUTHORITY_BYTES
                    or aggregate > MAX_PRODUCTION_REGISTRY_BYTES
                ):
                    raise ProductionWorkbenchError("workspace_capacity", 429)
                candidate = _ProductionEntry(
                    workspace=workspace,
                    touched_at=now,
                    authority_bytes=authority_bytes,
                    accepted_authorities=retained_authorities,
                    preview_sources=retained_preview_sources,
                    automatic_plan=None,
                    artifact_store=(
                        entry.artifact_store if retained_authorities.artifact_receipts else None
                    ),
                    assembly_runtime=None,
                    assembly_job=None,
                    completed_assembly=completed_assembly,
                    mutation_claim=entry.mutation_claim,
                    lineage_token=(
                        entry.lineage_token
                        if seed is None or seed.lineage_token is entry.lineage_token
                        else None
                    ),
                )
                projection = self._projection(handle, candidate)
                self._cancel_entry_assembly(entry)
                self._entries[handle] = candidate
                self._publish_ledger(
                    request_id=str(action["request_id"]),
                    request_digest=request_digest,
                    action=str(action["action"]),
                    handle=handle,
                    workspace=workspace,
                    now=now,
                )
                return ProductionDispatchResult(200, projection)
        finally:
            entry.mutation_claim.release()


def build_registry() -> ProductionWorkspaceRegistry:
    """Construct this adapter's process registry. Called only by the composition root."""

    from .comfyui_media_runtime import authorized_m26_assembly_runtime, media_runtime_lease

    return ProductionWorkspaceRegistry(
        seed_claim=claim_sidebar_production_seed,
        assembly_runtime_provider=authorized_m26_assembly_runtime,
        media_lease=media_runtime_lease,
    )


_registry = component(PRODUCTION_WORKSPACE, ProductionWorkspaceRegistry)


def dispatch_production_action(action: object) -> ProductionDispatchResult:
    return _registry().dispatch(action)


def publish_generation_sequence_authority(
    sequence: GenerationSequenceProjection,
    context_workspace_handle: str | None = None,
) -> ProductionWorkbenchProjection | None:
    """Publish from the accepted backend producer without adding a public route or wire."""

    return _registry().publish_generation_sequence_authority(
        sequence,
        context_workspace_handle,
    )


def publish_reconstruction_authority(
    *,
    plan: AVReconstructionPlan,
    generation_sequence: GenerationSequenceProjection,
    artifact_receipts: tuple[SegmentArtifactReceipt, ...],
    continuity_receipts: tuple[ContinuityBoundaryReceipt, ...],
    reconstruction_receipt: AVReconstructionReceipt,
    store: PrivateAVReconstructionStore,
    adapter: QualifiedAVMediaAdapter,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> ProductionPreviewPublication:
    """Publish from the sole typed reconstruction completion wrapper."""

    return _registry().publish_reconstruction_authority(
        plan=plan,
        generation_sequence=generation_sequence,
        artifact_receipts=artifact_receipts,
        continuity_receipts=continuity_receipts,
        reconstruction_receipt=reconstruction_receipt,
        store=store,
        adapter=adapter,
        deadline=deadline,
        clock=clock,
    )


def admit_media_preview_source(
    *,
    workspace_handle: str,
    expected_workspace_revision: int,
    expected_workspace_fingerprint: str,
    output_handle: str,
) -> MediaPreviewSourceAuthority:
    return _registry().admit_media_preview_source(
        workspace_handle=workspace_handle,
        expected_workspace_revision=expected_workspace_revision,
        expected_workspace_fingerprint=expected_workspace_fingerprint,
        output_handle=output_handle,
    )


def media_preview_source_is_current(
    *,
    workspace_handle: str,
    expected_workspace_revision: int,
    expected_workspace_fingerprint: str,
    source: MediaPreviewSourceAuthority,
) -> bool:
    return _registry().media_preview_source_is_current(
        workspace_handle=workspace_handle,
        expected_workspace_revision=expected_workspace_revision,
        expected_workspace_fingerprint=expected_workspace_fingerprint,
        source=source,
    )


_ROUTE_POLICY = RoutePolicy(
    path=PRODUCTION_ACTION_ROUTE,
    owner=PRODUCTION_ACTION_SCHEMA,
    owner_attribute=_ROUTE_OWNER_ATTRIBUTE,
    max_bytes=MAX_PRODUCTION_ACTION_BYTES,
    refusals=(ProductionWorkbenchError,),
)


def _refusal_body(_status: int, _reason: str) -> None:
    """Production failures expose only the frozen empty-body status surface."""

    return None


def _refuse(error: BaseException) -> RouteResult:
    status = getattr(error, "status", 400)
    projection = getattr(error, "projection", None)
    # CRITICAL: a projection rides along only on a 409 conflict, because that is the one refusal the
    # client must reconcile against. Every other refusal stays bodiless by design.
    if status == 409 and projection is not None:
        return RouteResult(409, projection.to_wire())
    return RouteResult(status if isinstance(status, int) else 400)


def _dispatch_body(payload: bytes) -> RouteResult:
    result = dispatch_production_action(decode_production_action_json(payload))
    if result.status == 204 or result.projection is None:
        return RouteResult(result.status)
    return RouteResult(result.status, result.projection.to_wire())


async def _act(payload: bytes, _context: object) -> RouteResult:
    return await offload_route_handler("production", lambda: _dispatch_body(payload))


def ensure_production_route_registered() -> bool:
    """Register the strict action route only against modules already owned by the active host."""

    global _ROUTE_REGISTERED
    _ROUTE_REGISTERED = register_owned_route(
        _ROUTE_POLICY,
        __name__,
        _act,
        _refusal_body,
        refusal_mapper=_refuse,
    )
    return _ROUTE_REGISTERED


__all__ = [
    "MAX_PRODUCTION_ACTION_ARRAY",
    "MAX_PRODUCTION_ACTION_BYTES",
    "MAX_PRODUCTION_ACTION_DEPTH",
    "MAX_PRODUCTION_ACTION_NODES",
    "MAX_PRODUCTION_ADMISSION_RECORDS",
    "MAX_PRODUCTION_AUTHORITY_BYTES",
    "MAX_PRODUCTION_DESTINATION_ADMISSIONS",
    "PRODUCTION_DESTINATION_ADMISSION_TTL_SECONDS",
    "PRODUCTION_MEMBER_ATTEMPT_RESERVE_BYTES",
    "ProductionDestinationAdmissionClaim",
    "ProductionMemberExecution",
    "ProductionMemberPublication",
    "MAX_PRODUCTION_REGISTRY_BYTES",
    "MAX_PRODUCTION_REQUEST_LEDGER",
    "MAX_PRODUCTION_TOMBSTONES",
    "MAX_PRODUCTION_WORKSPACES",
    "PRODUCTION_ACTION_ROUTE",
    "PRODUCTION_ACTION_SCHEMA",
    "PRODUCTION_MUTATION_CLAIM_SECONDS",
    "PRODUCTION_TOMBSTONE_TTL_SECONDS",
    "PRODUCTION_WORKSPACE_TTL_SECONDS",
    "ProductionDispatchResult",
    "ProductionPreviewPublication",
    "ProductionPreviewRowPublication",
    "ProductionWorkbenchError",
    "ProductionWorkspaceRegistry",
    "decode_production_action_json",
    "admit_media_preview_source",
    "dispatch_production_action",
    "ensure_production_route_registered",
    "publish_generation_sequence_authority",
    "publish_reconstruction_authority",
    "media_preview_source_is_current",
]
