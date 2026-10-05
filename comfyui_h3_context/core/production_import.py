"""Pure contracts and builders for atomic M26 Production proposal import.

The durable public projection is content-free. Exact proposal text remains only in the typed,
process-local authority, and every segment declaration is derived from a canonical validated
Context report plus the exact issued native wiring.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

from .canonical import canonical_bytes, canonical_fingerprint
from .context_reporting import ContextReport, ValidationStatus
from .contracts import TaskMode
from .errors import NativeH3AdapterError
from .native_h3 import (
    NativeH3Wiring,
    assert_native_h3_wiring_authority,
)
from .native_mode_matrix import build_default_native_mode_matrix
from .production_storyboard import (
    MAX_PROMPT_LENGTH,
    ProductionPlanningContextV1,
    ProductionPlanningContextV2,
    ProposalBlockerCodeV1,
    ProposalSegmentV1,
    SegmentationProposalV1,
    SegmentationProposalV2,
    fingerprint_prompt_text,
    validate_current_segmentation_proposal,
)
from .segment_workspace import (
    AcceptedIntentAuthority,
    MultiSegmentWorkspace,
    SegmentDeclaration,
    SegmentDuration,
    SegmentRelationKind,
    derive_segment_manifests,
    revise_workspace,
)

PRODUCTION_IMPORT_REQUEST_SCHEMA = "h3.context.production_import_request.v1"
SEGMENT_CONTEXT_MATERIALIZATION_RECEIPT_SCHEMA = (
    "h3.context.segment_context_materialization_receipt.v1"
)
PRODUCTION_CUT_BOUNDARY_RECEIPT_SCHEMA = "h3.context.production_cut_boundary_receipt.v1"
PRODUCTION_AUTOMATIC_PLAN_AUTHORITY_SCHEMA = "h3.context.production_automatic_plan_authority.v1"
PRODUCTION_AUTOMATIC_PLAN_AUTHORITY_V2_SCHEMA = "h3.context.production_automatic_plan_authority.v2"
PRODUCTION_AUTOMATIC_PLAN_PROJECTION_SCHEMA = "h3.context.production_automatic_plan_projection.v1"

MAX_PRODUCTION_IMPORT_RECEIPTS = 15
MAX_PRODUCTION_IMPORT_WIRE_BYTES = 262_144

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_WORKSPACE_HANDLE = re.compile(r"pw_[A-Za-z0-9_-]{32,96}\Z")
_CONTEXT_HANDLE = re.compile(r"ws_[A-Za-z0-9_-]{32,96}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ProductionImportError(ValueError):
    """Closed M26-03 contract or derivation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _identifier(value: object, field_name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ProductionImportError(field_name)
    return value


def _workspace_handle(value: object) -> str:
    if type(value) is not str or _WORKSPACE_HANDLE.fullmatch(value) is None:
        raise ProductionImportError("production_import_workspace_handle")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise ProductionImportError(field_name)
    return value


def _revision(value: object, field_name: str) -> int:
    if type(value) is not int or not 1 <= value <= 1_000_000:
        raise ProductionImportError(field_name)
    return value


def _closed(value: object, keys: frozenset[str], field_name: str) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != keys:
        raise ProductionImportError(field_name)
    return cast(dict[str, object], value)


def _wire_tuple(value: object, field_name: str, maximum: int) -> tuple[object, ...]:
    if type(value) is not list or len(value) > maximum:
        raise ProductionImportError(field_name)
    return tuple(cast(list[object], value))


def _is_retained_legacy_proposal_valid(proposal: SegmentationProposalV1) -> bool:
    return (
        type(proposal) is SegmentationProposalV1
        and proposal.importable
        and (ProposalBlockerCodeV1.MANAGED_EXECUTION_UNSUPPORTED not in proposal.start_hold_codes)
    )


def is_production_proposal_importable(proposal: SegmentationProposalV2) -> bool:
    """Return whether the exact proposal may enter durable Production authority."""

    # IMPORTANT: new import authority requires the V2 semantic census. Accepting a structurally
    # valid V1 proposal here would silently mint current authority without semantic preservation.
    if type(proposal) is not SegmentationProposalV2:
        raise ProductionImportError("production_proposal_authority")
    return proposal.importable and (
        ProposalBlockerCodeV1.MANAGED_EXECUTION_UNSUPPORTED not in proposal.start_hold_codes
    )


@dataclass(frozen=True, slots=True)
class ProductionImportRequestV1:
    """Closed approval request bound to one exact live workspace and proposal authority."""

    request_id: str
    workspace_handle: str
    workspace_id: str
    expected_workspace_revision: int
    expected_workspace_fingerprint: str
    proposal_id: str
    proposal_revision: int
    proposal_fingerprint: str
    planning_context_fingerprint: str
    source_request_fingerprint: str
    source_profile_fingerprint: str
    schema: str = PRODUCTION_IMPORT_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PRODUCTION_IMPORT_REQUEST_SCHEMA:
            raise ProductionImportError("production_import_request_schema")
        _identifier(self.request_id, "production_import_request_id")
        _workspace_handle(self.workspace_handle)
        _identifier(self.workspace_id, "production_import_workspace_id")
        _revision(self.expected_workspace_revision, "production_import_workspace_revision")
        _fingerprint(
            self.expected_workspace_fingerprint,
            "production_import_workspace_fingerprint",
        )
        _identifier(self.proposal_id, "production_import_proposal_id")
        _revision(self.proposal_revision, "production_import_proposal_revision")
        for value, field_name in (
            (self.proposal_fingerprint, "production_import_proposal_fingerprint"),
            (self.planning_context_fingerprint, "production_import_context_fingerprint"),
            (self.source_request_fingerprint, "production_import_source_request_fingerprint"),
            (self.source_profile_fingerprint, "production_import_source_profile_fingerprint"),
        ):
            _fingerprint(value, field_name)

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "workspace_handle": self.workspace_handle,
            "workspace_id": self.workspace_id,
            "expected_workspace_revision": self.expected_workspace_revision,
            "expected_workspace_fingerprint": self.expected_workspace_fingerprint,
            "proposal_id": self.proposal_id,
            "proposal_revision": self.proposal_revision,
            "proposal_fingerprint": self.proposal_fingerprint,
            "planning_context_fingerprint": self.planning_context_fingerprint,
            "source_request_fingerprint": self.source_request_fingerprint,
            "source_profile_fingerprint": self.source_profile_fingerprint,
        }

    @classmethod
    def from_wire(cls, value: object) -> ProductionImportRequestV1:
        keys = frozenset(
            {
                "schema",
                "request_id",
                "workspace_handle",
                "workspace_id",
                "expected_workspace_revision",
                "expected_workspace_fingerprint",
                "proposal_id",
                "proposal_revision",
                "proposal_fingerprint",
                "planning_context_fingerprint",
                "source_request_fingerprint",
                "source_profile_fingerprint",
            }
        )
        wire = _closed(value, keys, "production_import_request_wire")
        return cls(
            schema=cast(str, wire["schema"]),
            request_id=cast(str, wire["request_id"]),
            workspace_handle=cast(str, wire["workspace_handle"]),
            workspace_id=cast(str, wire["workspace_id"]),
            expected_workspace_revision=cast(int, wire["expected_workspace_revision"]),
            expected_workspace_fingerprint=cast(str, wire["expected_workspace_fingerprint"]),
            proposal_id=cast(str, wire["proposal_id"]),
            proposal_revision=cast(int, wire["proposal_revision"]),
            proposal_fingerprint=cast(str, wire["proposal_fingerprint"]),
            planning_context_fingerprint=cast(str, wire["planning_context_fingerprint"]),
            source_request_fingerprint=cast(str, wire["source_request_fingerprint"]),
            source_profile_fingerprint=cast(str, wire["source_profile_fingerprint"]),
        )


class SegmentContextMaterializationClaim:
    """One short-lived exact Context authority with an explicit release obligation."""

    __slots__ = (
        "context_workspace_handle",
        "report",
        "wiring",
        "_release",
        "_released",
    )

    def __init__(
        self,
        *,
        context_workspace_handle: str,
        report: ContextReport,
        wiring: NativeH3Wiring,
        release: Callable[[], None],
    ) -> None:
        if _CONTEXT_HANDLE.fullmatch(context_workspace_handle) is None:
            raise ProductionImportError("segment_context_workspace_handle")
        if type(report) is not ContextReport or type(wiring) is not NativeH3Wiring:
            raise ProductionImportError("segment_context_materialization_authority")
        if not callable(release):
            raise ProductionImportError("segment_context_release")
        self.context_workspace_handle = context_workspace_handle
        self.report = report
        self.wiring = wiring
        self._release = release
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            raise ProductionImportError("segment_context_already_released")
        # IMPORTANT: mark the authority terminal before the callback. A failing cleanup must never
        # leave a claim that a caller can accidentally reuse as live Context authority.
        self._released = True
        self._release()


@dataclass(frozen=True, slots=True)
class SegmentContextMaterializationReceiptV1:
    """Stable content-free comparison receipt for one exact local segment Context."""

    segment_context_authority_id: str
    proposal_id: str
    proposal_fingerprint: str
    segment_id: str
    local_prompt_fingerprint: str
    task_mode: TaskMode
    duration_milliseconds: int
    frame_count: int
    intent_graph_fingerprint: str
    profile_fingerprint: str
    ordered_reference_ids: tuple[str, ...]
    reference_registry_fingerprint: str
    native_binding_fingerprint: str
    producer_settings_fingerprint: str
    capability_fingerprint: str
    receipt_fingerprint: str | None = None
    schema: str = SEGMENT_CONTEXT_MATERIALIZATION_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SEGMENT_CONTEXT_MATERIALIZATION_RECEIPT_SCHEMA:
            raise ProductionImportError("segment_context_receipt_schema")
        _identifier(self.segment_context_authority_id, "segment_context_authority_id")
        _identifier(self.proposal_id, "segment_context_proposal_id")
        _identifier(self.segment_id, "segment_context_segment_id")
        for value, field_name in (
            (self.proposal_fingerprint, "segment_context_proposal_fingerprint"),
            (self.local_prompt_fingerprint, "segment_context_local_prompt_fingerprint"),
            (self.intent_graph_fingerprint, "segment_context_intent_graph_fingerprint"),
            (self.profile_fingerprint, "segment_context_profile_fingerprint"),
            (
                self.reference_registry_fingerprint,
                "segment_context_reference_registry_fingerprint",
            ),
            (self.native_binding_fingerprint, "segment_context_native_binding_fingerprint"),
            (
                self.producer_settings_fingerprint,
                "segment_context_producer_settings_fingerprint",
            ),
            (self.capability_fingerprint, "segment_context_capability_fingerprint"),
        ):
            _fingerprint(value, field_name)
        if type(self.task_mode) is not TaskMode:
            raise ProductionImportError("segment_context_task_mode")
        if (
            type(self.duration_milliseconds) is not int
            or not 4_000 <= self.duration_milliseconds <= 15_000
        ):
            raise ProductionImportError("segment_context_duration")
        duration = SegmentDuration(self.duration_milliseconds)
        if self.frame_count != duration.frame_count:
            raise ProductionImportError("segment_context_frame_count")
        if (
            type(self.ordered_reference_ids) is not tuple
            or len(self.ordered_reference_ids) > 12
            or len(self.ordered_reference_ids) != len(set(self.ordered_reference_ids))
        ):
            raise ProductionImportError("segment_context_reference_ids")
        for value in self.ordered_reference_ids:
            _identifier(value, "segment_context_reference_id")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.receipt_fingerprint is None:
            object.__setattr__(self, "receipt_fingerprint", expected)
        elif self.receipt_fingerprint != expected:
            raise ProductionImportError("segment_context_receipt_fingerprint")

    @property
    def fingerprint(self) -> str:
        if self.receipt_fingerprint is None:  # pragma: no cover - initialized above
            raise ProductionImportError("segment_context_receipt_fingerprint")
        return self.receipt_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "segment_context_authority_id": self.segment_context_authority_id,
            "proposal_id": self.proposal_id,
            "proposal_fingerprint": self.proposal_fingerprint,
            "segment_id": self.segment_id,
            "local_prompt_fingerprint": self.local_prompt_fingerprint,
            "task_mode": self.task_mode.value,
            "duration_milliseconds": self.duration_milliseconds,
            "frame_count": self.frame_count,
            "intent_graph_fingerprint": self.intent_graph_fingerprint,
            "profile_fingerprint": self.profile_fingerprint,
            "ordered_reference_ids": list(self.ordered_reference_ids),
            "reference_registry_fingerprint": self.reference_registry_fingerprint,
            "native_binding_fingerprint": self.native_binding_fingerprint,
            "producer_settings_fingerprint": self.producer_settings_fingerprint,
            "capability_fingerprint": self.capability_fingerprint,
        }

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["receipt_fingerprint"] = self.fingerprint
        return value

    @classmethod
    def from_wire(cls, value: object) -> SegmentContextMaterializationReceiptV1:
        keys = frozenset(
            {
                "schema",
                "segment_context_authority_id",
                "proposal_id",
                "proposal_fingerprint",
                "segment_id",
                "local_prompt_fingerprint",
                "task_mode",
                "duration_milliseconds",
                "frame_count",
                "intent_graph_fingerprint",
                "profile_fingerprint",
                "ordered_reference_ids",
                "reference_registry_fingerprint",
                "native_binding_fingerprint",
                "producer_settings_fingerprint",
                "capability_fingerprint",
                "receipt_fingerprint",
            }
        )
        wire = _closed(value, keys, "segment_context_receipt_wire")
        references = _wire_tuple(
            wire["ordered_reference_ids"], "segment_context_reference_ids_wire", 12
        )
        try:
            task_mode = TaskMode(cast(str, wire["task_mode"]))
        except (TypeError, ValueError) as exc:
            raise ProductionImportError("segment_context_task_mode") from exc
        return cls(
            schema=cast(str, wire["schema"]),
            segment_context_authority_id=cast(str, wire["segment_context_authority_id"]),
            proposal_id=cast(str, wire["proposal_id"]),
            proposal_fingerprint=cast(str, wire["proposal_fingerprint"]),
            segment_id=cast(str, wire["segment_id"]),
            local_prompt_fingerprint=cast(str, wire["local_prompt_fingerprint"]),
            task_mode=task_mode,
            duration_milliseconds=cast(int, wire["duration_milliseconds"]),
            frame_count=cast(int, wire["frame_count"]),
            intent_graph_fingerprint=cast(str, wire["intent_graph_fingerprint"]),
            profile_fingerprint=cast(str, wire["profile_fingerprint"]),
            ordered_reference_ids=tuple(cast(tuple[str, ...], references)),
            reference_registry_fingerprint=cast(str, wire["reference_registry_fingerprint"]),
            native_binding_fingerprint=cast(str, wire["native_binding_fingerprint"]),
            producer_settings_fingerprint=cast(str, wire["producer_settings_fingerprint"]),
            capability_fingerprint=cast(str, wire["capability_fingerprint"]),
            receipt_fingerprint=cast(str, wire["receipt_fingerprint"]),
        )


@dataclass(frozen=True, slots=True)
class ProductionCutBoundaryReceiptV1:
    predecessor_segment_id: str
    successor_segment_id: str
    boundary_milliseconds: int
    join_policy: str = "cut"
    predecessor_artifact_required: bool = False
    schema: str = PRODUCTION_CUT_BOUNDARY_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PRODUCTION_CUT_BOUNDARY_RECEIPT_SCHEMA:
            raise ProductionImportError("production_cut_receipt_schema")
        _identifier(self.predecessor_segment_id, "production_cut_predecessor")
        _identifier(self.successor_segment_id, "production_cut_successor")
        if (
            self.predecessor_segment_id == self.successor_segment_id
            or type(self.boundary_milliseconds) is not int
            or not 4_000 <= self.boundary_milliseconds <= 56_000
            or self.join_policy != "cut"
            or self.predecessor_artifact_required is not False
        ):
            raise ProductionImportError("production_cut_receipt")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "predecessor_segment_id": self.predecessor_segment_id,
            "successor_segment_id": self.successor_segment_id,
            "boundary_milliseconds": self.boundary_milliseconds,
            "join_policy": self.join_policy,
            "predecessor_artifact_required": self.predecessor_artifact_required,
        }

    @classmethod
    def from_wire(cls, value: object) -> ProductionCutBoundaryReceiptV1:
        wire = _closed(
            value,
            frozenset(
                {
                    "schema",
                    "predecessor_segment_id",
                    "successor_segment_id",
                    "boundary_milliseconds",
                    "join_policy",
                    "predecessor_artifact_required",
                }
            ),
            "production_cut_receipt_wire",
        )
        return cls(
            schema=cast(str, wire["schema"]),
            predecessor_segment_id=cast(str, wire["predecessor_segment_id"]),
            successor_segment_id=cast(str, wire["successor_segment_id"]),
            boundary_milliseconds=cast(int, wire["boundary_milliseconds"]),
            join_policy=cast(str, wire["join_policy"]),
            predecessor_artifact_required=cast(bool, wire["predecessor_artifact_required"]),
        )


@dataclass(frozen=True, slots=True)
class ProductionAutomaticPlanAuthorityV1:
    """Exact process-local approved proposal plus its content-free derived authorities."""

    workspace: MultiSegmentWorkspace
    proposal: SegmentationProposalV1 = field(repr=False)
    planning_context_fingerprint: str = ""
    source_request_fingerprint: str = ""
    source_profile_fingerprint: str = ""
    materialization_receipts: tuple[SegmentContextMaterializationReceiptV1, ...] = ()
    cut_boundary_receipts: tuple[ProductionCutBoundaryReceiptV1, ...] = ()
    manifest_fingerprints: tuple[str, ...] = ()
    reconstruction_order: tuple[str, ...] = ()
    start_hold_codes: tuple[ProposalBlockerCodeV1, ...] = ()
    schema: str = PRODUCTION_AUTOMATIC_PLAN_AUTHORITY_SCHEMA

    def __post_init__(self) -> None:
        if type(self) is ProductionAutomaticPlanAuthorityV1:
            expected_schema = PRODUCTION_AUTOMATIC_PLAN_AUTHORITY_SCHEMA
            # Retained V1 work remains readable under its frozen contract. Routing it through the
            # current V2 import gate would revoke previously accepted process-local authority.
            proposal_valid = _is_retained_legacy_proposal_valid(self.proposal)
        elif type(self) is ProductionAutomaticPlanAuthorityV2:
            expected_schema = PRODUCTION_AUTOMATIC_PLAN_AUTHORITY_V2_SCHEMA
            proposal_valid = type(self.proposal) is SegmentationProposalV2 and (
                is_production_proposal_importable(self.proposal)
            )
        else:
            raise ProductionImportError("production_automatic_plan_authority_type")
        if self.schema != expected_schema:
            raise ProductionImportError("production_automatic_plan_schema")
        if type(self.workspace) is not MultiSegmentWorkspace:
            raise ProductionImportError("production_automatic_plan_workspace")
        if not proposal_valid:
            raise ProductionImportError("production_proposal_not_importable")
        for value, field_name in (
            (self.planning_context_fingerprint, "production_plan_context_fingerprint"),
            (self.source_request_fingerprint, "production_plan_source_request_fingerprint"),
            (self.source_profile_fingerprint, "production_plan_source_profile_fingerprint"),
        ):
            _fingerprint(value, field_name)
        proposal_ids = tuple(row.segment_id for row in self.proposal.segments)
        workspace_ids = tuple(row.segment_id for row in self.workspace.segments)
        receipt_ids = tuple(row.segment_id for row in self.materialization_receipts)
        if not proposal_ids or proposal_ids != workspace_ids or receipt_ids != proposal_ids:
            raise ProductionImportError("production_plan_segment_alignment")
        if len(self.materialization_receipts) > MAX_PRODUCTION_IMPORT_RECEIPTS:
            raise ProductionImportError("production_plan_segment_limit")
        if any(
            type(row) is not SegmentContextMaterializationReceiptV1
            or row.proposal_id != self.proposal.proposal_id
            or row.proposal_fingerprint != self.proposal.fingerprint
            for row in self.materialization_receipts
        ):
            raise ProductionImportError("production_plan_receipt_authority")
        expected_cuts = tuple(
            (
                self.proposal.segments[index - 1].segment_id,
                row.segment_id,
                row.global_start_milliseconds,
            )
            for index, row in enumerate(self.proposal.segments[1:], start=1)
        )
        actual_cuts = tuple(
            (row.predecessor_segment_id, row.successor_segment_id, row.boundary_milliseconds)
            for row in self.cut_boundary_receipts
        )
        if actual_cuts != expected_cuts or any(
            type(row) is not ProductionCutBoundaryReceiptV1 for row in self.cut_boundary_receipts
        ):
            raise ProductionImportError("production_plan_cut_alignment")
        manifests = derive_segment_manifests(self.workspace)
        if self.manifest_fingerprints != tuple(row.fingerprint for row in manifests):
            raise ProductionImportError("production_plan_manifest_alignment")
        if self.reconstruction_order != proposal_ids:
            raise ProductionImportError("production_plan_reconstruction_order")
        if self.start_hold_codes != self.proposal.start_hold_codes:
            raise ProductionImportError("production_plan_start_holds")
        for declaration, proposal_row, receipt in zip(
            self.workspace.segments,
            self.proposal.segments,
            self.materialization_receipts,
            strict=True,
        ):
            if (
                declaration.source_id != receipt.segment_context_authority_id
                or declaration.task_mode is not proposal_row.task_mode
                or receipt.local_prompt_fingerprint
                != fingerprint_prompt_text(proposal_row.local_prompt)
                or declaration.reference_ids != proposal_row.asset_ids
                or declaration.duration.duration_milliseconds
                != proposal_row.duration.requested_milliseconds
                or declaration.accepted_intent_fingerprint != receipt.intent_graph_fingerprint
                or declaration.profile_fingerprint != receipt.profile_fingerprint
                or declaration.reference_registry_fingerprint
                != receipt.reference_registry_fingerprint
                or declaration.native_binding_fingerprint != receipt.native_binding_fingerprint
                or declaration.producer_settings_fingerprint
                != receipt.producer_settings_fingerprint
                or declaration.predecessor_segment_id is not None
                or declaration.relation
                is not (
                    SegmentRelationKind.INDEPENDENT
                    if proposal_row.ordinal == 1
                    else SegmentRelationKind.CUT
                )
            ):
                raise ProductionImportError("production_plan_declaration_alignment")
        if len(self.private_wire_bytes()) > MAX_PRODUCTION_IMPORT_WIRE_BYTES:
            raise ProductionImportError("production_plan_wire_limit")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    @property
    def startable(self) -> bool:
        return not self.start_hold_codes

    def to_wire(self) -> dict[str, object]:
        """Return only the content-free retained comparison contract."""

        return {
            "schema": self.schema,
            "workspace_id": self.workspace.workspace_id,
            "workspace_revision": self.workspace.revision,
            "workspace_fingerprint": self.workspace.fingerprint,
            "proposal_id": self.proposal.proposal_id,
            "proposal_revision": self.proposal.revision,
            "proposal_fingerprint": self.proposal.fingerprint,
            "planning_context_fingerprint": self.planning_context_fingerprint,
            "source_request_fingerprint": self.source_request_fingerprint,
            "source_profile_fingerprint": self.source_profile_fingerprint,
            "segment_ids": list(self.reconstruction_order),
            "materialization_receipt_fingerprints": [
                row.fingerprint for row in self.materialization_receipts
            ],
            "cut_boundary_receipts": [row.to_wire() for row in self.cut_boundary_receipts],
            "manifest_fingerprints": list(self.manifest_fingerprints),
            "reconstruction_order": list(self.reconstruction_order),
            "start_hold_codes": [row.value for row in self.start_hold_codes],
            "startable": self.startable,
        }

    def private_wire_bytes(self) -> bytes:
        """Measure exact retained authority, including private prompts, without exposing it."""

        return canonical_bytes(
            {
                "projection": self.to_wire(),
                "proposal": self.proposal.to_wire(),
                "receipts": [row.to_wire() for row in self.materialization_receipts],
            }
        )


@dataclass(frozen=True, slots=True)
class ProductionAutomaticPlanAuthorityV2(ProductionAutomaticPlanAuthorityV1):
    """Current retained authority bound to a semantic-preserving V2 proposal."""

    proposal: SegmentationProposalV2 = field(repr=False)
    schema: str = PRODUCTION_AUTOMATIC_PLAN_AUTHORITY_V2_SCHEMA


@dataclass(frozen=True, slots=True)
class ProductionAutomaticPlanProjectionV1:
    request_id: str
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    proposal_id: str
    proposal_revision: int
    proposal_fingerprint: str
    plan_fingerprint: str
    segment_ids: tuple[str, ...]
    materialization_receipt_fingerprints: tuple[str, ...]
    cut_boundary_receipts: tuple[ProductionCutBoundaryReceiptV1, ...]
    manifest_fingerprints: tuple[str, ...]
    reconstruction_order: tuple[str, ...]
    start_hold_codes: tuple[str, ...]
    startable: bool
    schema: str = PRODUCTION_AUTOMATIC_PLAN_PROJECTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PRODUCTION_AUTOMATIC_PLAN_PROJECTION_SCHEMA:
            raise ProductionImportError("production_plan_projection_schema")
        _identifier(self.request_id, "production_plan_projection_request_id")
        _identifier(self.workspace_id, "production_plan_projection_workspace_id")
        _revision(self.workspace_revision, "production_plan_projection_workspace_revision")
        _identifier(self.proposal_id, "production_plan_projection_proposal_id")
        _revision(self.proposal_revision, "production_plan_projection_proposal_revision")
        for value, field_name in (
            (self.workspace_fingerprint, "production_plan_projection_workspace_fingerprint"),
            (self.proposal_fingerprint, "production_plan_projection_proposal_fingerprint"),
            (self.plan_fingerprint, "production_plan_projection_fingerprint"),
        ):
            _fingerprint(value, field_name)
        for values, field_name in (
            (self.segment_ids, "production_plan_projection_segment_ids"),
            (self.reconstruction_order, "production_plan_projection_reconstruction_order"),
        ):
            if type(values) is not tuple or len(values) > MAX_PRODUCTION_IMPORT_RECEIPTS:
                raise ProductionImportError(field_name)
            for value in values:
                _identifier(value, field_name)
        if not self.segment_ids or self.reconstruction_order != self.segment_ids:
            raise ProductionImportError("production_plan_projection_order")
        if (
            type(self.materialization_receipt_fingerprints) is not tuple
            or len(self.materialization_receipt_fingerprints) != len(self.segment_ids)
            or type(self.manifest_fingerprints) is not tuple
            or len(self.manifest_fingerprints) != len(self.segment_ids)
        ):
            raise ProductionImportError("production_plan_projection_receipts")
        for value in (*self.materialization_receipt_fingerprints, *self.manifest_fingerprints):
            _fingerprint(value, "production_plan_projection_member_fingerprint")
        if (
            type(self.cut_boundary_receipts) is not tuple
            or len(self.cut_boundary_receipts) != len(self.segment_ids) - 1
            or any(
                type(row) is not ProductionCutBoundaryReceiptV1
                for row in self.cut_boundary_receipts
            )
        ):
            raise ProductionImportError("production_plan_projection_cuts")
        if type(self.start_hold_codes) is not tuple or len(self.start_hold_codes) != len(
            set(self.start_hold_codes)
        ):
            raise ProductionImportError("production_plan_projection_start_holds")
        for value in self.start_hold_codes:
            _identifier(value, "production_plan_projection_start_hold")
        if type(self.startable) is not bool or self.startable is bool(self.start_hold_codes):
            raise ProductionImportError("production_plan_projection_startable")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "proposal_id": self.proposal_id,
            "proposal_revision": self.proposal_revision,
            "proposal_fingerprint": self.proposal_fingerprint,
            "plan_fingerprint": self.plan_fingerprint,
            "segment_ids": list(self.segment_ids),
            "materialization_receipt_fingerprints": list(self.materialization_receipt_fingerprints),
            "cut_boundary_receipts": [row.to_wire() for row in self.cut_boundary_receipts],
            "manifest_fingerprints": list(self.manifest_fingerprints),
            "reconstruction_order": list(self.reconstruction_order),
            "start_hold_codes": list(self.start_hold_codes),
            "startable": self.startable,
        }


@dataclass(frozen=True, slots=True)
class ProductionAutomaticPlanDispatchResultV1:
    status: int
    projection: ProductionAutomaticPlanProjectionV1
    replayed: bool = False

    def __post_init__(self) -> None:
        if self.status != 200 or type(self.projection) is not ProductionAutomaticPlanProjectionV1:
            raise ProductionImportError("production_plan_dispatch_result")
        if type(self.replayed) is not bool:
            raise ProductionImportError("production_plan_dispatch_replayed")


def _stable_native_binding_fingerprint(wiring: NativeH3Wiring) -> str:
    """Fingerprint native identity without the transient report/workspace identity."""

    return canonical_fingerprint(
        {
            "schema": "h3.context.production_segment.native_binding.v1",
            "host_version": wiring.host_version,
            "host_revision": wiring.host_revision,
            "native_source": wiring.native_source,
            "native_source_blob": wiring.native_source_blob,
            "native_node_id": wiring.native_node_id,
            "task_mode": wiring.task_mode.value,
            "profile": wiring.profile.to_wire(),
            "prompt_fingerprint": wiring.prompt_fingerprint,
            "native_length_frames": wiring.native_length_frames,
            "native_input_names": list(wiring.native_input_names),
            "bindings": [row.to_wire() for row in wiring.bindings],
            "validation_status": wiring.validation_status.value,
            "limitations": list(wiring.limitations),
        }
    )


def _derive_segment_context_materialization_receipt(
    planning_context: ProductionPlanningContextV1 | ProductionPlanningContextV2,
    proposal: SegmentationProposalV1 | SegmentationProposalV2,
    segment: ProposalSegmentV1,
    claim: SegmentContextMaterializationClaim,
) -> SegmentContextMaterializationReceiptV1:
    """Validate one exact canonical Context result and derive its stable receipt."""

    if (
        (type(planning_context), type(proposal))
        not in (
            (ProductionPlanningContextV1, SegmentationProposalV1),
            (ProductionPlanningContextV2, SegmentationProposalV2),
        )
        or type(segment) is not ProposalSegmentV1
        or type(claim) is not SegmentContextMaterializationClaim
        or claim.released
    ):
        raise ProductionImportError("segment_context_materialization_authority")
    if (
        proposal.planning_context_fingerprint != planning_context.fingerprint
        or proposal.duration_intent_fingerprint != planning_context.duration_intent_fingerprint
        or not any(row is segment or row == segment for row in proposal.segments)
    ):
        raise ProductionImportError("segment_context_proposal_mismatch")
    report = claim.report
    try:
        wiring = assert_native_h3_wiring_authority(claim.wiring, report)
    except NativeH3AdapterError as exc:
        raise ProductionImportError("segment_context_native_authority") from exc
    reference_ids = tuple(row.asset_id for row in report.request.reference_registry.assets)
    expected_asset_roles = tuple(
        (row.asset_id, row.role)
        for row in planning_context.assets
        if row.asset_id in segment.asset_ids
    )
    actual_asset_roles = tuple(
        (row.asset_id, row.role) for row in report.request.reference_registry.assets
    )
    profile_fingerprint = canonical_fingerprint(report.request.profile.to_wire())
    duration = SegmentDuration(segment.duration.requested_milliseconds)
    if (
        not report.is_successful
        or report.validation.status is not ValidationStatus.PASSED
        or not report.validation.is_valid
        or report.request.user_intent != segment.local_prompt
        or len(report.request.user_intent) > MAX_PROMPT_LENGTH
        or report.request.task_mode is not segment.task_mode
        or report.request.effective_frame_count != duration.frame_count
        or wiring.native_length_frames != duration.frame_count
        or wiring.task_mode is not segment.task_mode
        or reference_ids != segment.asset_ids
        # CRITICAL: asset IDs alone are not the M26 task authority. REF2VA accepts several image
        # roles, so a swapped role set can still produce valid native wiring while changing the
        # user-approved semantic assignment.
        or actual_asset_roles != expected_asset_roles
        or profile_fingerprint != planning_context.source_profile_fingerprint
    ):
        raise ProductionImportError("segment_context_materialization_mismatch")
    intent_graph_fingerprint = canonical_fingerprint(report.plan.intent_graph.to_wire())
    registry_fingerprint = canonical_fingerprint(report.request.reference_registry.to_wire())
    native_binding_fingerprint = _stable_native_binding_fingerprint(wiring)
    producer_settings_fingerprint = canonical_fingerprint(
        {
            "schema": "h3.context.production_segment.producer_settings.v1",
            "source": "validated_local_segment_context",
            "task_mode": segment.task_mode.value,
            "model_variant": report.request.model_variant.value,
            "duration_locked": True,
            "requested_milliseconds": duration.duration_milliseconds,
            "frame_count": duration.frame_count,
            "raw_media": False,
        }
    )
    capability_fingerprint = build_default_native_mode_matrix().fingerprint
    basis = canonical_fingerprint(
        {
            "schema": "h3.context.segment_context_authority_basis.v1",
            "proposal_id": proposal.proposal_id,
            "proposal_fingerprint": proposal.fingerprint,
            "segment_id": segment.segment_id,
            "local_prompt_fingerprint": fingerprint_prompt_text(segment.local_prompt),
            "intent_graph_fingerprint": intent_graph_fingerprint,
            "profile_fingerprint": profile_fingerprint,
            "ordered_reference_ids": list(reference_ids),
            "reference_registry_fingerprint": registry_fingerprint,
            "native_binding_fingerprint": native_binding_fingerprint,
            "producer_settings_fingerprint": producer_settings_fingerprint,
            "capability_fingerprint": capability_fingerprint,
        }
    )
    return SegmentContextMaterializationReceiptV1(
        segment_context_authority_id="segment_context." + basis.removeprefix("sha256:")[:48],
        proposal_id=proposal.proposal_id,
        proposal_fingerprint=proposal.fingerprint,
        segment_id=segment.segment_id,
        local_prompt_fingerprint=fingerprint_prompt_text(segment.local_prompt),
        task_mode=segment.task_mode,
        duration_milliseconds=duration.duration_milliseconds,
        frame_count=duration.frame_count,
        intent_graph_fingerprint=intent_graph_fingerprint,
        profile_fingerprint=profile_fingerprint,
        ordered_reference_ids=reference_ids,
        reference_registry_fingerprint=registry_fingerprint,
        native_binding_fingerprint=native_binding_fingerprint,
        producer_settings_fingerprint=producer_settings_fingerprint,
        capability_fingerprint=capability_fingerprint,
    )


def derive_segment_context_materialization_receipt(
    planning_context: ProductionPlanningContextV2,
    proposal: SegmentationProposalV2,
    segment: ProposalSegmentV1,
    claim: SegmentContextMaterializationClaim,
) -> SegmentContextMaterializationReceiptV1:
    """Derive a receipt for a new semantic-preserving V2 import or rematerialization."""

    if (
        type(planning_context) is not ProductionPlanningContextV2
        or type(proposal) is not SegmentationProposalV2
    ):
        raise ProductionImportError("segment_context_materialization_authority")
    # CRITICAL: exact V2 types do not prove that proposal semantics came from this context. Join
    # their source authority before inspecting a materializer claim or deriving any receipt.
    validate_current_segmentation_proposal(planning_context, proposal)
    return _derive_segment_context_materialization_receipt(
        planning_context, proposal, segment, claim
    )


def derive_legacy_segment_context_materialization_receipt(
    planning_context: ProductionPlanningContextV1,
    proposal: SegmentationProposalV1,
    segment: ProposalSegmentV1,
    claim: SegmentContextMaterializationClaim,
) -> SegmentContextMaterializationReceiptV1:
    """Rematerialize already retained V1 authority without minting current import authority."""

    # IMPORTANT: this exact V1-only seam preserves retained work. Do not widen it or route new
    # imports through it; V1 proposals do not carry the current semantic preservation census.
    if (
        type(planning_context) is not ProductionPlanningContextV1
        or type(proposal) is not SegmentationProposalV1
    ):
        raise ProductionImportError("segment_context_materialization_authority")
    return _derive_segment_context_materialization_receipt(
        planning_context, proposal, segment, claim
    )


def build_production_automatic_plan_authority(
    workspace: MultiSegmentWorkspace,
    planning_context: ProductionPlanningContextV2,
    proposal: SegmentationProposalV2,
    materialization_receipts: tuple[SegmentContextMaterializationReceiptV1, ...],
) -> ProductionAutomaticPlanAuthorityV2:
    """Build one complete successor workspace and its exact automatic-plan authority."""

    if (
        type(workspace) is not MultiSegmentWorkspace
        or type(planning_context) is not ProductionPlanningContextV2
        or type(proposal) is not SegmentationProposalV2
    ):
        raise ProductionImportError("production_import_authority_type")
    # CRITICAL: a self-consistent proposal fingerprint cannot replace the current context-to-
    # proposal semantic join. Validate it before workspace revision or authority construction.
    validate_current_segmentation_proposal(planning_context, proposal)
    if (
        proposal.planning_context_fingerprint != planning_context.fingerprint
        or proposal.duration_intent_fingerprint != planning_context.duration_intent_fingerprint
    ):
        raise ProductionImportError("production_import_source_binding_stale")
    if not is_production_proposal_importable(proposal):
        raise ProductionImportError("production_proposal_not_importable")
    if (
        type(materialization_receipts) is not tuple
        or len(materialization_receipts) != len(proposal.segments)
        or len(materialization_receipts) > MAX_PRODUCTION_IMPORT_RECEIPTS
    ):
        raise ProductionImportError("production_import_receipt_coverage")
    declarations = tuple(
        SegmentDeclaration(
            segment_id=segment.segment_id,
            task_mode=segment.task_mode,
            source_id=receipt.segment_context_authority_id,
            reference_ids=segment.asset_ids,
            duration=SegmentDuration(segment.duration.requested_milliseconds),
            relation=(
                SegmentRelationKind.INDEPENDENT if segment.ordinal == 1 else SegmentRelationKind.CUT
            ),
            predecessor_segment_id=None,
            accepted_intent_fingerprint=receipt.intent_graph_fingerprint,
            semantic_receipt_fingerprint=None,
            profile_fingerprint=receipt.profile_fingerprint,
            reference_registry_fingerprint=receipt.reference_registry_fingerprint,
            native_binding_fingerprint=receipt.native_binding_fingerprint,
            producer_settings_fingerprint=receipt.producer_settings_fingerprint,
        )
        for segment, receipt in zip(proposal.segments, materialization_receipts, strict=True)
    )
    if any(
        receipt.segment_id != segment.segment_id
        or receipt.proposal_id != proposal.proposal_id
        or receipt.proposal_fingerprint != proposal.fingerprint
        # CRITICAL: a receipt rebound to a new proposal fingerprint is not proof that its Context
        # was materialized from that proposal's private prompt. Bind the prompt fingerprint too.
        or receipt.local_prompt_fingerprint != fingerprint_prompt_text(segment.local_prompt)
        or receipt.task_mode is not segment.task_mode
        or receipt.duration_milliseconds != segment.duration.requested_milliseconds
        or receipt.frame_count != segment.duration.frame_count
        or receipt.ordered_reference_ids != segment.asset_ids
        for segment, receipt in zip(proposal.segments, materialization_receipts, strict=True)
    ):
        raise ProductionImportError("production_import_receipt_mismatch")
    try:
        next_workspace = revise_workspace(
            workspace,
            expected_workspace_fingerprint=workspace.fingerprint,
            accepted_intent_authorities=tuple(
                AcceptedIntentAuthority(row.segment_id, row.accepted_intent_fingerprint)
                for row in declarations
            ),
            segments=declarations,
            selected_segment_ids=tuple(row.segment_id for row in declarations),
        )
    except ValueError as exc:
        raise ProductionImportError("production_import_workspace_rejected") from exc
    cuts = tuple(
        ProductionCutBoundaryReceiptV1(
            predecessor_segment_id=proposal.segments[index - 1].segment_id,
            successor_segment_id=segment.segment_id,
            boundary_milliseconds=segment.global_start_milliseconds,
        )
        for index, segment in enumerate(proposal.segments[1:], start=1)
    )
    manifests = derive_segment_manifests(next_workspace)
    return ProductionAutomaticPlanAuthorityV2(
        workspace=next_workspace,
        proposal=proposal,
        planning_context_fingerprint=planning_context.fingerprint,
        source_request_fingerprint=planning_context.source_request_fingerprint,
        source_profile_fingerprint=planning_context.source_profile_fingerprint,
        materialization_receipts=materialization_receipts,
        cut_boundary_receipts=cuts,
        manifest_fingerprints=tuple(row.fingerprint for row in manifests),
        reconstruction_order=tuple(row.segment_id for row in proposal.segments),
        start_hold_codes=proposal.start_hold_codes,
    )


def project_production_automatic_plan(
    request_id: str,
    authority: ProductionAutomaticPlanAuthorityV1 | ProductionAutomaticPlanAuthorityV2,
) -> ProductionAutomaticPlanProjectionV1:
    if type(authority) not in (
        ProductionAutomaticPlanAuthorityV1,
        ProductionAutomaticPlanAuthorityV2,
    ):
        raise ProductionImportError("production_plan_projection_authority")
    return ProductionAutomaticPlanProjectionV1(
        request_id=request_id,
        workspace_id=authority.workspace.workspace_id,
        workspace_revision=authority.workspace.revision,
        workspace_fingerprint=authority.workspace.fingerprint,
        proposal_id=authority.proposal.proposal_id,
        proposal_revision=authority.proposal.revision,
        proposal_fingerprint=authority.proposal.fingerprint,
        plan_fingerprint=authority.fingerprint,
        segment_ids=authority.reconstruction_order,
        materialization_receipt_fingerprints=tuple(
            row.fingerprint for row in authority.materialization_receipts
        ),
        cut_boundary_receipts=authority.cut_boundary_receipts,
        manifest_fingerprints=authority.manifest_fingerprints,
        reconstruction_order=authority.reconstruction_order,
        start_hold_codes=tuple(row.value for row in authority.start_hold_codes),
        startable=authority.startable,
    )


MaterializeSegmentContext = Callable[
    [ProductionPlanningContextV2, SegmentationProposalV2, ProposalSegmentV1],
    SegmentContextMaterializationClaim,
]
LegacyMaterializeSegmentContext = Callable[
    [ProductionPlanningContextV1, SegmentationProposalV1, ProposalSegmentV1],
    SegmentContextMaterializationClaim,
]


__all__ = [
    "MAX_PRODUCTION_IMPORT_RECEIPTS",
    "MAX_PRODUCTION_IMPORT_WIRE_BYTES",
    "PRODUCTION_AUTOMATIC_PLAN_AUTHORITY_SCHEMA",
    "PRODUCTION_AUTOMATIC_PLAN_AUTHORITY_V2_SCHEMA",
    "PRODUCTION_AUTOMATIC_PLAN_PROJECTION_SCHEMA",
    "PRODUCTION_CUT_BOUNDARY_RECEIPT_SCHEMA",
    "PRODUCTION_IMPORT_REQUEST_SCHEMA",
    "SEGMENT_CONTEXT_MATERIALIZATION_RECEIPT_SCHEMA",
    "LegacyMaterializeSegmentContext",
    "MaterializeSegmentContext",
    "ProductionAutomaticPlanAuthorityV1",
    "ProductionAutomaticPlanAuthorityV2",
    "ProductionAutomaticPlanDispatchResultV1",
    "ProductionAutomaticPlanProjectionV1",
    "ProductionCutBoundaryReceiptV1",
    "ProductionImportError",
    "ProductionImportRequestV1",
    "SegmentContextMaterializationClaim",
    "SegmentContextMaterializationReceiptV1",
    "build_production_automatic_plan_authority",
    "derive_legacy_segment_context_materialization_receipt",
    "derive_segment_context_materialization_receipt",
    "is_production_proposal_importable",
    "project_production_automatic_plan",
]
