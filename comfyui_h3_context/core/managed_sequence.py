"""Pure bounded parent authority for M26 managed serial generation."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum

from .canonical import binary64_token, canonical_bytes, canonical_fingerprint
from .contracts import TaskMode
from .guide_conformance import GuideReadiness

MANAGED_SEQUENCE_SCHEMA = "h3.context.managed_sequence.v1"
MANAGED_SEQUENCE_AUTHORIZATION_SCHEMA = "h3.context.managed_sequence_authorization.v1"
MANAGED_SEQUENCE_SEGMENT_AUTHORIZATION_SCHEMA = (
    "h3.context.managed_sequence_segment_authorization.v1"
)
MANAGED_MODE_QUALIFICATION_SCHEMA = "h3.context.managed_mode_qualification.v1"
MANAGED_SEQUENCE_SLOT_SCHEMA = "h3.context.managed_sequence_slot.v1"
ELIGIBLE_SEGMENT_EXECUTION_SCHEMA = "h3.context.eligible_segment_execution.v1"
MANAGED_SEQUENCE_LEASE_SCHEMA = "h3.context.managed_sequence_lease.v1"
COORDINATOR_LEDGER_RESERVATION_SCHEMA = "h3.context.coordinator_ledger_reservation.v1"
ARTIFACT_CAPACITY_RESERVATION_SCHEMA = "h3.context.artifact_capacity_reservation.v1"
SEQUENCE_CANVAS_AUTHORITY_SCHEMA = "h3.context.sequence_canvas_authority.v1"
MANAGED_SEQUENCE_CURRENT_PROJECTION_SCHEMA = "h3.context.managed_sequence_projection.v1"

MAX_MANAGED_SEQUENCE_SEGMENTS = 15
MAX_SEQUENCE_RECOVERY_ACTIONS = 8
MAX_COORDINATOR_RESPONSE_BYTES = 8 * 1024
MAX_MANAGED_SEQUENCE_PROJECTION_BYTES = 64 * 1024
MANAGED_SEQUENCE_IDLE_SECONDS = 900.0
MANAGED_SEQUENCE_ABSOLUTE_SECONDS = 24.0 * 60.0 * 60.0
MANAGED_SEQUENCE_RECONCILIATION_GRACE_SECONDS = 300.0
ARTIFACT_STORE_ENTRY_BYTES = 128 * 1024 * 1024
ARTIFACT_STORE_TOTAL_BYTES = 1024 * 1024 * 1024

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RUN_HANDLE = re.compile(r"mc_[A-Za-z0-9_-]{32,96}\Z")
_REQUIRED_GLOBAL_MODES = tuple(TaskMode)


class ManagedSequenceError(ValueError):
    """Closed failure at a managed-sequence contract boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ManagedSequenceStateV1(str, Enum):
    AUTHORIZED = "authorized"
    ACTIVE = "active"
    PAUSED_CLIENT_ABSENT = "paused_client_absent"
    PAUSED_UNKNOWN_OWNERSHIP = "paused_unknown_ownership"
    PAUSED_CANVAS_DRIFT = "paused_canvas_drift"
    PAUSED_FAILURE = "paused_failure"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"


class SequenceSlotStateV1(str, Enum):
    PENDING = "pending"
    ELIGIBLE = "eligible"
    PREPARED = "prepared"
    BOUND = "bound"
    SUBMITTED = "submitted"
    RUNNING = "running"
    ARTIFACT_VERIFIED = "artifact_verified"
    SUCCEEDED = "succeeded"
    REUSED = "reused"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    UNKNOWN_OWNERSHIP = "unknown_ownership"


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ManagedSequenceError(f"invalid_{field}")
    return value


def _fingerprint(value: object, field: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise ManagedSequenceError(f"invalid_{field}")
    return value


def _revision(value: object, field: str = "revision") -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise ManagedSequenceError(f"invalid_{field}")
    return value


def _time(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManagedSequenceError(f"invalid_{field}")
    result = float(value)
    if not 0.0 <= result <= 10_000_000_000.0:
        raise ManagedSequenceError(f"invalid_{field}")
    return result


def coordinator_ledger_rows(segment_count: int) -> int:
    if type(segment_count) is not int or not 1 <= segment_count <= MAX_MANAGED_SEQUENCE_SEGMENTS:
        raise ManagedSequenceError("segment_limit")
    return 2 + (7 * segment_count) + MAX_SEQUENCE_RECOVERY_ACTIONS


@dataclass(frozen=True, slots=True)
class ManagedSequenceSegmentAuthorizationV1:
    segment_id: str
    ordinal: int
    task_mode: TaskMode
    duration_milliseconds: int
    frame_count: int
    manifest_fingerprint: str
    materialization_receipt_fingerprint: str
    local_prompt_fingerprint: str
    intent_graph_fingerprint: str
    profile_fingerprint: str
    capability_fingerprint: str
    predecessor_segment_id: str | None
    predecessor_cut_receipt_fingerprint: str | None
    schema: str = MANAGED_SEQUENCE_SEGMENT_AUTHORIZATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_SEQUENCE_SEGMENT_AUTHORIZATION_SCHEMA:
            raise ManagedSequenceError("segment_authorization_schema")
        _identifier(self.segment_id, "segment_id")
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= MAX_MANAGED_SEQUENCE_SEGMENTS:
            raise ManagedSequenceError("segment_order")
        if type(self.task_mode) is not TaskMode:
            raise ManagedSequenceError("segment_task_mode")
        if (
            type(self.duration_milliseconds) is not int
            or not 4_000 <= self.duration_milliseconds <= 15_000
            or type(self.frame_count) is not int
            or self.frame_count <= 0
        ):
            raise ManagedSequenceError("segment_duration")
        for value, field in (
            (self.manifest_fingerprint, "manifest_fingerprint"),
            (self.materialization_receipt_fingerprint, "materialization_receipt_fingerprint"),
            (self.local_prompt_fingerprint, "local_prompt_fingerprint"),
            (self.intent_graph_fingerprint, "intent_graph_fingerprint"),
            (self.profile_fingerprint, "profile_fingerprint"),
            (self.capability_fingerprint, "capability_fingerprint"),
        ):
            _fingerprint(value, field)
        if self.predecessor_segment_id is None:
            if self.predecessor_cut_receipt_fingerprint is not None:
                raise ManagedSequenceError("segment_predecessor")
        else:
            _identifier(self.predecessor_segment_id, "predecessor_segment_id")
            _fingerprint(
                self.predecessor_cut_receipt_fingerprint,
                "predecessor_cut_receipt_fingerprint",
            )

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "segment_id": self.segment_id,
            "ordinal": self.ordinal,
            "task_mode": self.task_mode.value,
            "duration_milliseconds": self.duration_milliseconds,
            "frame_count": self.frame_count,
            "manifest_fingerprint": self.manifest_fingerprint,
            "materialization_receipt_fingerprint": self.materialization_receipt_fingerprint,
            "local_prompt_fingerprint": self.local_prompt_fingerprint,
            "intent_graph_fingerprint": self.intent_graph_fingerprint,
            "profile_fingerprint": self.profile_fingerprint,
            "capability_fingerprint": self.capability_fingerprint,
            "predecessor_segment_id": self.predecessor_segment_id,
            "predecessor_cut_receipt_fingerprint": self.predecessor_cut_receipt_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ManagedSequenceAuthorizationV1:
    sequence_id: str
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    production_plan_fingerprint: str
    proposal_fingerprint: str
    generation_plan_fingerprint: str
    compiler_fingerprint: str
    host_capability_fingerprint: str
    segments: tuple[ManagedSequenceSegmentAuthorizationV1, ...]
    max_concurrency: int = 1
    explicit_intent: str = "generate_approved_sequence"
    schema: str = MANAGED_SEQUENCE_AUTHORIZATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_SEQUENCE_AUTHORIZATION_SCHEMA:
            raise ManagedSequenceError("authorization_schema")
        _identifier(self.sequence_id, "sequence_id")
        _identifier(self.workspace_id, "workspace_id")
        _revision(self.workspace_revision, "workspace_revision")
        for value, field in (
            (self.workspace_fingerprint, "workspace_fingerprint"),
            (self.production_plan_fingerprint, "production_plan_fingerprint"),
            (self.proposal_fingerprint, "proposal_fingerprint"),
            (self.generation_plan_fingerprint, "generation_plan_fingerprint"),
            (self.compiler_fingerprint, "compiler_fingerprint"),
            (self.host_capability_fingerprint, "host_capability_fingerprint"),
        ):
            _fingerprint(value, field)
        if self.max_concurrency != 1:
            raise ManagedSequenceError("concurrency")
        if self.explicit_intent != "generate_approved_sequence":
            raise ManagedSequenceError("explicit_intent")
        if (
            type(self.segments) is not tuple
            or not 1 <= len(self.segments) <= MAX_MANAGED_SEQUENCE_SEGMENTS
            or any(type(row) is not ManagedSequenceSegmentAuthorizationV1 for row in self.segments)
        ):
            raise ManagedSequenceError("segment_limit")
        ids = tuple(row.segment_id for row in self.segments)
        if len(ids) != len(set(ids)):
            raise ManagedSequenceError("segment_duplicate")
        for index, row in enumerate(self.segments):
            expected_predecessor = None if index == 0 else self.segments[index - 1].segment_id
            if row.ordinal != index + 1 or row.predecessor_segment_id != expected_predecessor:
                raise ManagedSequenceError("segment_order")
            if row.capability_fingerprint != self.host_capability_fingerprint:
                raise ManagedSequenceError("authorization_drift")
        if len(canonical_bytes(self.to_wire())) > 262_144:
            raise ManagedSequenceError("authorization_size")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "sequence_id": self.sequence_id,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "production_plan_fingerprint": self.production_plan_fingerprint,
            "proposal_fingerprint": self.proposal_fingerprint,
            "generation_plan_fingerprint": self.generation_plan_fingerprint,
            "compiler_fingerprint": self.compiler_fingerprint,
            "host_capability_fingerprint": self.host_capability_fingerprint,
            "segments": [row.to_wire() for row in self.segments],
            "max_concurrency": self.max_concurrency,
            "explicit_intent": self.explicit_intent,
        }


@dataclass(frozen=True, slots=True)
class ManagedModeQualificationV1:
    host_capability_fingerprint: str
    compiler_fingerprint: str
    qualified_global_modes: tuple[TaskMode, ...]
    qualified_materialization_receipts: tuple[str, ...]
    observed_at: float
    expires_at: float
    schema: str = MANAGED_MODE_QUALIFICATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_MODE_QUALIFICATION_SCHEMA:
            raise ManagedSequenceError("qualification_schema")
        _fingerprint(self.host_capability_fingerprint, "qualification_host_capability")
        _fingerprint(self.compiler_fingerprint, "qualification_compiler")
        if type(self.qualified_global_modes) is not tuple or any(
            type(mode) is not TaskMode for mode in self.qualified_global_modes
        ):
            raise ManagedSequenceError("qualification_modes")
        if len(self.qualified_global_modes) != len(set(self.qualified_global_modes)):
            raise ManagedSequenceError("qualification_modes")
        if type(self.qualified_materialization_receipts) is not tuple:
            raise ManagedSequenceError("qualification_receipts")
        for value in self.qualified_materialization_receipts:
            _fingerprint(value, "qualification_receipt")
        if len(self.qualified_materialization_receipts) != len(
            set(self.qualified_materialization_receipts)
        ):
            raise ManagedSequenceError("qualification_receipts")
        observed = _time(self.observed_at, "qualification_observed_at")
        expires = _time(self.expires_at, "qualification_expires_at")
        if expires <= observed:
            raise ManagedSequenceError("qualification_expiry")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "host_capability_fingerprint": self.host_capability_fingerprint,
            "compiler_fingerprint": self.compiler_fingerprint,
            "qualified_global_modes": [mode.value for mode in self.qualified_global_modes],
            "qualified_materialization_receipts": list(self.qualified_materialization_receipts),
            "observed_at": binary64_token(self.observed_at),
            "expires_at": binary64_token(self.expires_at),
        }

    def covers(self, authority: ManagedSequenceAuthorizationV1, *, now: float) -> None:
        if float(now) > self.expires_at:
            raise ManagedSequenceError("qualification_expired")
        if (
            self.host_capability_fingerprint != authority.host_capability_fingerprint
            or self.compiler_fingerprint != authority.compiler_fingerprint
        ):
            raise ManagedSequenceError("authorization_drift")
        if set(self.qualified_global_modes) != set(_REQUIRED_GLOBAL_MODES):
            raise ManagedSequenceError("qualification_incomplete")
        expected = tuple(row.materialization_receipt_fingerprint for row in authority.segments)
        if self.qualified_materialization_receipts != expected:
            raise ManagedSequenceError("qualification_incomplete")


@dataclass(frozen=True, slots=True)
class ManagedModeQualificationV2:
    """Exact plan and ordered composition evidence above the original five-mode guard."""

    baseline: ManagedModeQualificationV1
    production_plan_fingerprint: str
    composition_fingerprints: tuple[str, ...]
    guide_readiness: tuple[GuideReadiness, ...]
    host_profile_fingerprint: str
    asset_resolution_fingerprint: str
    schema: str = "h3.context.managed_mode_qualification.v2"

    def __post_init__(self) -> None:
        if (
            type(self.baseline) is not ManagedModeQualificationV1
            or self.schema != "h3.context.managed_mode_qualification.v2"
        ):
            raise ManagedSequenceError("qualification_schema")
        _fingerprint(self.production_plan_fingerprint, "qualification_plan")
        _fingerprint(self.host_profile_fingerprint, "qualification_host_profile")
        _fingerprint(self.asset_resolution_fingerprint, "qualification_asset_resolution")
        if self.expires_at - self.observed_at > 60:
            raise ManagedSequenceError("qualification_expiry")
        if (
            type(self.composition_fingerprints) is not tuple
            or not 1 <= len(self.composition_fingerprints) <= MAX_MANAGED_SEQUENCE_SEGMENTS
            or len(self.composition_fingerprints)
            != len(self.baseline.qualified_materialization_receipts)
        ):
            raise ManagedSequenceError("qualification_compositions")
        for value in self.composition_fingerprints:
            _fingerprint(value, "qualification_composition")
        if (
            type(self.guide_readiness) is not tuple
            or len(self.guide_readiness) != len(self.composition_fingerprints)
            or any(type(value) is not GuideReadiness for value in self.guide_readiness)
        ):
            raise ManagedSequenceError("qualification_guide")

    @property
    def observed_at(self) -> float:
        return self.baseline.observed_at

    @property
    def expires_at(self) -> float:
        return self.baseline.expires_at

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "baseline": self.baseline.to_wire(),
            "production_plan_fingerprint": self.production_plan_fingerprint,
            "composition_fingerprints": list(self.composition_fingerprints),
            "guide_readiness": [value.value for value in self.guide_readiness],
            "host_profile_fingerprint": self.host_profile_fingerprint,
            "asset_resolution_fingerprint": self.asset_resolution_fingerprint,
        }

    def covers(self, authority: ManagedSequenceAuthorizationV1, *, now: float) -> None:
        if float(now) >= self.expires_at:
            raise ManagedSequenceError("qualification_expired")
        self.baseline.covers(authority, now=now)
        if self.production_plan_fingerprint != authority.production_plan_fingerprint:
            raise ManagedSequenceError("authorization_drift")


@dataclass(frozen=True, slots=True)
class SequenceLeaseV1:
    created_at: float
    idle_deadline: float
    absolute_deadline: float
    active_deadline: float | None = None
    idle_expiry_handled: bool = False
    schema: str = MANAGED_SEQUENCE_LEASE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_SEQUENCE_LEASE_SCHEMA:
            raise ManagedSequenceError("lease_schema")
        created = _time(self.created_at, "lease_created_at")
        idle = _time(self.idle_deadline, "lease_idle_deadline")
        absolute = _time(self.absolute_deadline, "lease_absolute_deadline")
        if idle <= created or absolute <= idle:
            raise ManagedSequenceError("lease_deadline")
        if self.active_deadline is not None:
            active = _time(self.active_deadline, "lease_active_deadline")
            if active > absolute:
                raise ManagedSequenceError("lease_deadline")
        if type(self.idle_expiry_handled) is not bool:
            raise ManagedSequenceError("lease_idle_expiry_state")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "created_at": binary64_token(self.created_at),
            "idle_deadline": binary64_token(self.idle_deadline),
            "active_deadline": (
                None if self.active_deadline is None else binary64_token(self.active_deadline)
            ),
            "absolute_deadline": binary64_token(self.absolute_deadline),
            "idle_expiry_handled": self.idle_expiry_handled,
        }


@dataclass(frozen=True, slots=True)
class CoordinatorLedgerReservationV1:
    parent_sequence_id: str
    parent_authorization_fingerprint: str
    reserved_rows: int
    reserved_bytes: int
    consumed_rows: int
    recovery_rows_consumed: int
    released: bool
    expires_at: float
    schema: str = COORDINATOR_LEDGER_RESERVATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != COORDINATOR_LEDGER_RESERVATION_SCHEMA:
            raise ManagedSequenceError("ledger_reservation_schema")
        _identifier(self.parent_sequence_id, "ledger_parent_sequence_id")
        _fingerprint(
            self.parent_authorization_fingerprint,
            "ledger_parent_authorization_fingerprint",
        )
        if (
            type(self.reserved_rows) is not int
            or type(self.reserved_bytes) is not int
            or type(self.consumed_rows) is not int
            or type(self.recovery_rows_consumed) is not int
            or self.reserved_bytes != self.reserved_rows * MAX_COORDINATOR_RESPONSE_BYTES
            or not 0 <= self.consumed_rows <= self.reserved_rows
            or not 0 <= self.recovery_rows_consumed <= MAX_SEQUENCE_RECOVERY_ACTIONS
            or self.consumed_rows + self.recovery_rows_consumed > self.reserved_rows
            or type(self.released) is not bool
        ):
            raise ManagedSequenceError("ledger_reservation_capacity")
        _time(self.expires_at, "ledger_reservation_expiry")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "parent_sequence_id": self.parent_sequence_id,
            "parent_authorization_fingerprint": self.parent_authorization_fingerprint,
            "reserved_rows": self.reserved_rows,
            "reserved_bytes": self.reserved_bytes,
            "consumed_rows": self.consumed_rows,
            "recovery_rows_consumed": self.recovery_rows_consumed,
            "released": self.released,
            "expires_at": binary64_token(self.expires_at),
        }


@dataclass(frozen=True, slots=True)
class ArtifactCapacityReservationV1:
    parent_sequence_id: str
    parent_authorization_fingerprint: str
    segment_count: int
    reserved_entries: int
    per_artifact_bytes: int
    reserved_bytes: int
    consumed_entries: int
    consumed_bytes: int
    released: bool
    expires_at: float
    schema: str = ARTIFACT_CAPACITY_RESERVATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ARTIFACT_CAPACITY_RESERVATION_SCHEMA:
            raise ManagedSequenceError("artifact_reservation_schema")
        _identifier(self.parent_sequence_id, "artifact_parent_sequence_id")
        _fingerprint(
            self.parent_authorization_fingerprint,
            "artifact_parent_authorization_fingerprint",
        )
        expected = min(ARTIFACT_STORE_ENTRY_BYTES, ARTIFACT_STORE_TOTAL_BYTES // self.segment_count)
        if (
            type(self.segment_count) is not int
            or not 1 <= self.segment_count <= MAX_MANAGED_SEQUENCE_SEGMENTS
            or self.reserved_entries != self.segment_count
            or self.per_artifact_bytes != expected
            or self.reserved_bytes != expected * self.segment_count
            or not 0 <= self.consumed_entries <= self.reserved_entries
            or not 0 <= self.consumed_bytes <= self.reserved_bytes
            or type(self.released) is not bool
        ):
            raise ManagedSequenceError("artifact_reservation_capacity")
        _time(self.expires_at, "artifact_reservation_expiry")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "parent_sequence_id": self.parent_sequence_id,
            "parent_authorization_fingerprint": self.parent_authorization_fingerprint,
            "segment_count": self.segment_count,
            "reserved_entries": self.reserved_entries,
            "per_artifact_bytes": self.per_artifact_bytes,
            "reserved_bytes": self.reserved_bytes,
            "consumed_entries": self.consumed_entries,
            "consumed_bytes": self.consumed_bytes,
            "released": self.released,
            "expires_at": binary64_token(self.expires_at),
        }


@dataclass(frozen=True, slots=True)
class SequenceCanvasAuthorityV1:
    active_workflow_fingerprint: str
    owned_projection_fingerprint: str
    owned_write_count: int = 0
    rollback_write_count: int = 0
    schema: str = SEQUENCE_CANVAS_AUTHORITY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SEQUENCE_CANVAS_AUTHORITY_SCHEMA:
            raise ManagedSequenceError("canvas_authority_schema")
        _fingerprint(self.active_workflow_fingerprint, "active_workflow_fingerprint")
        _fingerprint(self.owned_projection_fingerprint, "owned_projection_fingerprint")
        if (
            type(self.owned_write_count) is not int
            or not 0 <= self.owned_write_count <= MAX_MANAGED_SEQUENCE_SEGMENTS
            or type(self.rollback_write_count) is not int
            or not 0 <= self.rollback_write_count <= 1
        ):
            raise ManagedSequenceError("canvas_write_count")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "active_workflow_fingerprint": self.active_workflow_fingerprint,
            "owned_projection_fingerprint": self.owned_projection_fingerprint,
            "owned_write_count": self.owned_write_count,
            "rollback_write_count": self.rollback_write_count,
        }


@dataclass(frozen=True, slots=True)
class SequenceSlotV1:
    segment_id: str
    ordinal: int
    state: SequenceSlotStateV1
    attempt_epoch: int = 1
    eligible_execution_fingerprint: str | None = None
    child_run_handle: str | None = None
    child_state_fingerprint: str | None = None
    graph_fingerprint: str | None = None
    compiled_prompt_fingerprint: str | None = None
    canvas_write_fingerprint: str | None = None
    queue_prompt_id: str | None = None
    artifact_receipt_fingerprint: str | None = None
    terminal_fingerprint: str | None = None
    schema: str = MANAGED_SEQUENCE_SLOT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_SEQUENCE_SLOT_SCHEMA:
            raise ManagedSequenceError("slot_schema")
        _identifier(self.segment_id, "slot_segment_id")
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= MAX_MANAGED_SEQUENCE_SEGMENTS:
            raise ManagedSequenceError("slot_order")
        if type(self.state) is not SequenceSlotStateV1:
            raise ManagedSequenceError("slot_state")
        if type(self.attempt_epoch) is not int or not 1 <= self.attempt_epoch <= 2:
            raise ManagedSequenceError("slot_attempt")
        for value, field in (
            (self.eligible_execution_fingerprint, "eligible_execution_fingerprint"),
            (self.child_state_fingerprint, "child_state_fingerprint"),
            (self.graph_fingerprint, "graph_fingerprint"),
            (self.compiled_prompt_fingerprint, "compiled_prompt_fingerprint"),
            (self.canvas_write_fingerprint, "canvas_write_fingerprint"),
            (self.artifact_receipt_fingerprint, "artifact_receipt_fingerprint"),
            (self.terminal_fingerprint, "terminal_fingerprint"),
        ):
            if value is not None:
                _fingerprint(value, field)
        if (
            self.child_run_handle is not None
            and _RUN_HANDLE.fullmatch(self.child_run_handle) is None
        ):
            raise ManagedSequenceError("invalid_child_run_handle")
        if self.queue_prompt_id is not None:
            _identifier(self.queue_prompt_id, "queue_prompt_id")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "segment_id": self.segment_id,
            "ordinal": self.ordinal,
            "state": self.state.value,
            "attempt_epoch": self.attempt_epoch,
            "eligible_execution_fingerprint": self.eligible_execution_fingerprint,
            "child_run_handle": self.child_run_handle,
            "child_state_fingerprint": self.child_state_fingerprint,
            "graph_fingerprint": self.graph_fingerprint,
            "compiled_prompt_fingerprint": self.compiled_prompt_fingerprint,
            "canvas_write_fingerprint": self.canvas_write_fingerprint,
            "queue_prompt_id": self.queue_prompt_id,
            "artifact_receipt_fingerprint": self.artifact_receipt_fingerprint,
            "terminal_fingerprint": self.terminal_fingerprint,
        }


def _successful_slot(slot: SequenceSlotV1) -> bool:
    return (
        slot.state in {SequenceSlotStateV1.SUCCEEDED, SequenceSlotStateV1.REUSED}
        and slot.terminal_fingerprint is not None
        and slot.artifact_receipt_fingerprint is not None
    )


@dataclass(frozen=True, slots=True)
class EligibleSegmentExecutionV1:
    parent_sequence_id: str
    parent_authorization_fingerprint: str
    segment_id: str
    slot_revision: int
    attempt_epoch: int
    materialization_receipt_fingerprint: str
    predecessor_terminal_fingerprint: str | None
    request_id: str
    job_count: int = 1
    schema: str = ELIGIBLE_SEGMENT_EXECUTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ELIGIBLE_SEGMENT_EXECUTION_SCHEMA or self.job_count != 1:
            raise ManagedSequenceError("eligible_execution_schema")
        _identifier(self.parent_sequence_id, "eligible_parent_sequence_id")
        _fingerprint(
            self.parent_authorization_fingerprint,
            "eligible_parent_authorization_fingerprint",
        )
        _identifier(self.segment_id, "eligible_segment_id")
        _revision(self.slot_revision, "eligible_slot_revision")
        if type(self.attempt_epoch) is not int or not 1 <= self.attempt_epoch <= 2:
            raise ManagedSequenceError("eligible_attempt")
        _fingerprint(
            self.materialization_receipt_fingerprint,
            "eligible_materialization_receipt_fingerprint",
        )
        if self.predecessor_terminal_fingerprint is not None:
            _fingerprint(
                self.predecessor_terminal_fingerprint,
                "eligible_predecessor_terminal_fingerprint",
            )
        _identifier(self.request_id, "eligible_request_id")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        # Deliberately no predecessor artifact locator, graph or media field. A predecessor proves
        # serial eligibility only; making it a successor input would silently change cut-v1.
        return {
            "schema": self.schema,
            "parent_sequence_id": self.parent_sequence_id,
            "parent_authorization_fingerprint": self.parent_authorization_fingerprint,
            "segment_id": self.segment_id,
            "slot_revision": self.slot_revision,
            "attempt_epoch": self.attempt_epoch,
            "materialization_receipt_fingerprint": self.materialization_receipt_fingerprint,
            "predecessor_terminal_fingerprint": self.predecessor_terminal_fingerprint,
            "request_id": self.request_id,
            "job_count": self.job_count,
        }


@dataclass(frozen=True, slots=True)
class ManagedSequenceV1:
    authorization: ManagedSequenceAuthorizationV1
    state: ManagedSequenceStateV1
    revision: int
    slots: tuple[SequenceSlotV1, ...]
    client_attached: bool
    qualification_fingerprint: str | None = None
    lease: SequenceLeaseV1 | None = None
    ledger_reservation: CoordinatorLedgerReservationV1 | None = None
    artifact_reservation: ArtifactCapacityReservationV1 | None = None
    canvas_authority: SequenceCanvasAuthorityV1 | None = None
    active_segment_id: str | None = None
    retry_consumed: bool = False
    schema: str = MANAGED_SEQUENCE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_SEQUENCE_SCHEMA:
            raise ManagedSequenceError("managed_sequence_schema")
        if type(self.authorization) is not ManagedSequenceAuthorizationV1:
            raise ManagedSequenceError("managed_sequence_authorization")
        if type(self.state) is not ManagedSequenceStateV1:
            raise ManagedSequenceError("managed_sequence_state")
        _revision(self.revision)
        if type(self.slots) is not tuple or len(self.slots) != len(self.authorization.segments):
            raise ManagedSequenceError("slot_alignment")
        if tuple((row.segment_id, row.ordinal) for row in self.slots) != tuple(
            (row.segment_id, row.ordinal) for row in self.authorization.segments
        ):
            raise ManagedSequenceError("slot_alignment")
        if type(self.client_attached) is not bool or type(self.retry_consumed) is not bool:
            raise ManagedSequenceError("managed_sequence_flag")
        # CRITICAL: exhaustion of pending work is not success; failed or unproved slots must
        # never release a parent's reservations through an attachment/recovery transition.
        if self.state is ManagedSequenceStateV1.SUCCEEDED and not all(
            _successful_slot(row) for row in self.slots
        ):
            raise ManagedSequenceError("successful_slot_proof")
        if self.qualification_fingerprint is not None:
            _fingerprint(self.qualification_fingerprint, "qualification_fingerprint")
        if self.active_segment_id is not None:
            _identifier(self.active_segment_id, "active_segment_id")
            live = tuple(
                row.segment_id
                for row in self.slots
                if row.state
                in {
                    SequenceSlotStateV1.PREPARED,
                    SequenceSlotStateV1.BOUND,
                    SequenceSlotStateV1.SUBMITTED,
                    SequenceSlotStateV1.RUNNING,
                    SequenceSlotStateV1.ARTIFACT_VERIFIED,
                    SequenceSlotStateV1.UNKNOWN_OWNERSHIP,
                }
            )
            if live != (self.active_segment_id,):
                raise ManagedSequenceError("one_live_child")
        if len(canonical_bytes(self.to_wire())) > MAX_MANAGED_SEQUENCE_PROJECTION_BYTES:
            raise ManagedSequenceError("managed_sequence_size")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "authorization": self.authorization.to_wire(),
            "state": self.state.value,
            "revision": self.revision,
            "slots": [row.to_wire() for row in self.slots],
            "client_attached": self.client_attached,
            "qualification_fingerprint": self.qualification_fingerprint,
            "lease": None if self.lease is None else self.lease.to_wire(),
            "ledger_reservation": (
                None if self.ledger_reservation is None else self.ledger_reservation.to_wire()
            ),
            "artifact_reservation": (
                None if self.artifact_reservation is None else self.artifact_reservation.to_wire()
            ),
            "canvas_authority": (
                None if self.canvas_authority is None else self.canvas_authority.to_wire()
            ),
            "active_segment_id": self.active_segment_id,
            "retry_consumed": self.retry_consumed,
        }


@dataclass(frozen=True, slots=True)
class PreparedManagedSequenceChildV1:
    sequence: ManagedSequenceV1
    execution: EligibleSegmentExecutionV1


@dataclass(frozen=True, slots=True)
class ManagedSequenceCurrentProjectionV1:
    parent_sequence_id: str
    authorization_fingerprint: str
    state: ManagedSequenceStateV1
    revision: int
    etag: str
    lease: SequenceLeaseV1
    active_segment_id: str | None
    slots: tuple[SequenceSlotV1, ...]
    schema: str = MANAGED_SEQUENCE_CURRENT_PROJECTION_SCHEMA

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "parent_sequence_id": self.parent_sequence_id,
            "authorization_fingerprint": self.authorization_fingerprint,
            "state": self.state.value,
            "revision": self.revision,
            "etag": self.etag,
            "lease": self.lease.to_wire(),
            "active_segment_id": self.active_segment_id,
            "slots": [row.to_wire() for row in self.slots],
        }


def authorize_managed_sequence(
    authorization: ManagedSequenceAuthorizationV1, *, now: float
) -> ManagedSequenceV1:
    if type(authorization) is not ManagedSequenceAuthorizationV1:
        raise ManagedSequenceError("managed_sequence_authorization")
    _time(now, "authorization_time")
    slots = tuple(
        SequenceSlotV1(row.segment_id, row.ordinal, SequenceSlotStateV1.PENDING)
        for row in authorization.segments
    )
    return ManagedSequenceV1(
        authorization=authorization,
        state=ManagedSequenceStateV1.AUTHORIZED,
        revision=0,
        slots=slots,
        client_attached=True,
    )


def start_managed_sequence(
    sequence: ManagedSequenceV1,
    qualification: ManagedModeQualificationV1 | ManagedModeQualificationV2,
    *,
    now: float,
) -> ManagedSequenceV1:
    if sequence.state is not ManagedSequenceStateV1.AUTHORIZED:
        if (
            sequence.qualification_fingerprint == qualification.fingerprint
            and sequence.state is ManagedSequenceStateV1.ACTIVE
        ):
            return sequence
        raise ManagedSequenceError("sequence_already_started")
    now_value = _time(now, "start_time")
    qualification.covers(sequence.authorization, now=now_value)
    absolute = now_value + MANAGED_SEQUENCE_ABSOLUTE_SECONDS
    rows = coordinator_ledger_rows(len(sequence.slots))
    per_artifact = min(
        ARTIFACT_STORE_ENTRY_BYTES,
        ARTIFACT_STORE_TOTAL_BYTES // len(sequence.slots),
    )
    slots = (replace(sequence.slots[0], state=SequenceSlotStateV1.ELIGIBLE),) + sequence.slots[1:]
    return replace(
        sequence,
        state=ManagedSequenceStateV1.ACTIVE,
        revision=sequence.revision + 1,
        slots=slots,
        qualification_fingerprint=qualification.fingerprint,
        lease=SequenceLeaseV1(
            created_at=now_value,
            idle_deadline=now_value + MANAGED_SEQUENCE_IDLE_SECONDS,
            absolute_deadline=absolute,
        ),
        ledger_reservation=CoordinatorLedgerReservationV1(
            parent_sequence_id=sequence.authorization.sequence_id,
            parent_authorization_fingerprint=sequence.authorization.fingerprint,
            reserved_rows=rows,
            reserved_bytes=rows * MAX_COORDINATOR_RESPONSE_BYTES,
            consumed_rows=2,
            recovery_rows_consumed=0,
            released=False,
            expires_at=absolute,
        ),
        artifact_reservation=ArtifactCapacityReservationV1(
            parent_sequence_id=sequence.authorization.sequence_id,
            parent_authorization_fingerprint=sequence.authorization.fingerprint,
            segment_count=len(sequence.slots),
            reserved_entries=len(sequence.slots),
            per_artifact_bytes=per_artifact,
            reserved_bytes=per_artifact * len(sequence.slots),
            consumed_entries=0,
            consumed_bytes=0,
            released=False,
            expires_at=absolute,
        ),
    )


def _require_revision(sequence: ManagedSequenceV1, expected_revision: int) -> None:
    if sequence.revision != expected_revision:
        raise ManagedSequenceError("stale_parent")


def _slot_index(sequence: ManagedSequenceV1, segment_id: str) -> int:
    for index, row in enumerate(sequence.slots):
        if row.segment_id == segment_id:
            return index
    raise ManagedSequenceError("segment_unavailable")


class _Unchanged:
    __slots__ = ()


_UNCHANGED = _Unchanged()


def _replace_slot(
    sequence: ManagedSequenceV1,
    index: int,
    slot: SequenceSlotV1,
    *,
    state: ManagedSequenceStateV1 | None = None,
    client_attached: bool | None = None,
    lease: SequenceLeaseV1 | None | _Unchanged = _UNCHANGED,
    ledger_reservation: CoordinatorLedgerReservationV1 | None | _Unchanged = _UNCHANGED,
    artifact_reservation: ArtifactCapacityReservationV1 | None | _Unchanged = _UNCHANGED,
    canvas_authority: SequenceCanvasAuthorityV1 | None | _Unchanged = _UNCHANGED,
    active_segment_id: str | None | _Unchanged = _UNCHANGED,
    retry_consumed: bool | None = None,
) -> ManagedSequenceV1:
    slots = sequence.slots[:index] + (slot,) + sequence.slots[index + 1 :]
    return replace(
        sequence,
        state=sequence.state if state is None else state,
        revision=sequence.revision + 1,
        slots=slots,
        client_attached=sequence.client_attached if client_attached is None else client_attached,
        lease=(sequence.lease if isinstance(lease, _Unchanged) else lease),
        ledger_reservation=(
            sequence.ledger_reservation
            if isinstance(ledger_reservation, _Unchanged)
            else ledger_reservation
        ),
        artifact_reservation=(
            sequence.artifact_reservation
            if isinstance(artifact_reservation, _Unchanged)
            else artifact_reservation
        ),
        canvas_authority=(
            sequence.canvas_authority
            if isinstance(canvas_authority, _Unchanged)
            else canvas_authority
        ),
        active_segment_id=(
            sequence.active_segment_id
            if isinstance(active_segment_id, _Unchanged)
            else active_segment_id
        ),
        retry_consumed=(sequence.retry_consumed if retry_consumed is None else retry_consumed),
    )


def _consume_regular(sequence: ManagedSequenceV1, count: int = 1) -> CoordinatorLedgerReservationV1:
    reservation = sequence.ledger_reservation
    if reservation is None or reservation.released:
        raise ManagedSequenceError("ledger_reservation_unavailable")
    consumed = reservation.consumed_rows + count
    cancel_rows = int(sequence.state is not ManagedSequenceStateV1.CANCELLED)
    if consumed + reservation.recovery_rows_consumed + cancel_rows > reservation.reserved_rows:
        raise ManagedSequenceError("ledger_reservation_exhausted")
    return replace(reservation, consumed_rows=consumed)


def _consume_child_transition(
    sequence: ManagedSequenceV1,
    slot: SequenceSlotV1,
    count: int = 1,
    *,
    retry_prepare: bool = False,
) -> CoordinatorLedgerReservationV1:
    if slot.attempt_epoch == 1:
        return _consume_regular(sequence, count)
    reservation = sequence.ledger_reservation
    if reservation is None or reservation.released:
        raise ManagedSequenceError("ledger_reservation_unavailable")
    # IMPORTANT: retry_segment replaces prepare_sequence_child for epoch two. Charging prepare a
    # second time double-counts the seven-row recovery path and rejects the admitted 15-slot case.
    if retry_prepare:
        return reservation
    consumed = reservation.recovery_rows_consumed + count
    cancel_rows = int(sequence.state is not ManagedSequenceStateV1.CANCELLED)
    if (
        consumed + cancel_rows > MAX_SEQUENCE_RECOVERY_ACTIONS
        or reservation.consumed_rows + consumed + cancel_rows > reservation.reserved_rows
    ):
        raise ManagedSequenceError("ledger_reservation_exhausted")
    return replace(reservation, recovery_rows_consumed=consumed)


def _consume_attachment_control(
    sequence: ManagedSequenceV1,
    *,
    needs_resume: bool = False,
) -> CoordinatorLedgerReservationV1:
    ledger = _consume_regular(sequence)
    cleanup_rows = 2 if sequence.active_segment_id is not None else 0
    if any(slot.state is SequenceSlotStateV1.BOUND for slot in sequence.slots):
        cleanup_rows = 3
    if sequence.retry_consumed and any(
        slot.attempt_epoch == 2
        and slot.state
        in {
            SequenceSlotStateV1.ELIGIBLE,
            SequenceSlotStateV1.PREPARED,
            SequenceSlotStateV1.BOUND,
            SequenceSlotStateV1.SUBMITTED,
            SequenceSlotStateV1.RUNNING,
            SequenceSlotStateV1.ARTIFACT_VERIFIED,
            SequenceSlotStateV1.UNKNOWN_OWNERSHIP,
        }
        for slot in sequence.slots
    ):
        cleanup_rows = max(cleanup_rows, 7 - ledger.recovery_rows_consumed)
    # CRITICAL: retry owns seven recovery rows plus cancel, not only the rows consumed so far.
    # Charge attachment separately and reserve its remaining retry tail; otherwise resume before
    # prepare spends row eight later at terminal and leaves cancel failing after a complete retry.
    resume_rows = int(needs_resume and sequence.retry_consumed)
    if (
        ledger.consumed_rows + ledger.recovery_rows_consumed + cleanup_rows + resume_rows + 1
        > ledger.reserved_rows
    ):
        raise ManagedSequenceError("recovery_capacity")
    return ledger


def _consume_recovery_control(sequence: ManagedSequenceV1) -> CoordinatorLedgerReservationV1:
    reservation = sequence.ledger_reservation
    if reservation is None or reservation.released:
        raise ManagedSequenceError("ledger_reservation_unavailable")
    consumed = reservation.recovery_rows_consumed + 1
    if (
        # IMPORTANT: optional recovery controls must leave the final recovery row for cancel.
        consumed >= MAX_SEQUENCE_RECOVERY_ACTIONS
        or reservation.consumed_rows + consumed >= reservation.reserved_rows
    ):
        raise ManagedSequenceError("recovery_capacity")
    return replace(reservation, recovery_rows_consumed=consumed)


def record_managed_sequence_reuse(
    sequence: ManagedSequenceV1,
    *,
    segment_id: str,
    expected_revision: int,
    artifact_receipt_fingerprint: str,
    artifact_bytes: int,
    terminal_fingerprint: str,
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    if sequence.state is not ManagedSequenceStateV1.ACTIVE or not sequence.client_attached:
        raise ManagedSequenceError("sequence_not_active")
    if sequence.active_segment_id is not None:
        raise ManagedSequenceError("one_live_child")
    index = _slot_index(sequence, _identifier(segment_id, "segment_id"))
    slot = sequence.slots[index]
    if slot.state is not SequenceSlotStateV1.ELIGIBLE:
        raise ManagedSequenceError("segment_not_eligible")
    reservation = sequence.artifact_reservation
    if reservation is None or reservation.released:
        raise ManagedSequenceError("artifact_reservation_unavailable")
    if (
        type(artifact_bytes) is not int
        or not 1 <= artifact_bytes <= reservation.per_artifact_bytes
        or reservation.consumed_entries >= reservation.reserved_entries
        or reservation.consumed_bytes + artifact_bytes > reservation.reserved_bytes
    ):
        raise ManagedSequenceError("artifact_reservation_exhausted")
    reused = replace(
        slot,
        state=SequenceSlotStateV1.REUSED,
        artifact_receipt_fingerprint=_fingerprint(
            artifact_receipt_fingerprint,
            "artifact_receipt_fingerprint",
        ),
        terminal_fingerprint=_fingerprint(terminal_fingerprint, "terminal_fingerprint"),
    )
    slots = sequence.slots[:index] + (reused,) + sequence.slots[index + 1 :]
    parent_state = ManagedSequenceStateV1.ACTIVE
    if index + 1 == len(slots):
        parent_state = ManagedSequenceStateV1.SUCCEEDED
    else:
        slots = (
            slots[: index + 1]
            + (replace(slots[index + 1], state=SequenceSlotStateV1.ELIGIBLE),)
            + slots[index + 2 :]
        )
    artifact = replace(
        reservation,
        consumed_entries=reservation.consumed_entries + 1,
        consumed_bytes=reservation.consumed_bytes + artifact_bytes,
        released=parent_state is ManagedSequenceStateV1.SUCCEEDED,
    )
    ledger = _consume_child_transition(sequence, slot)
    if parent_state is ManagedSequenceStateV1.SUCCEEDED:
        ledger = replace(ledger, released=True)
    return replace(
        sequence,
        state=parent_state,
        revision=sequence.revision + 1,
        slots=slots,
        lease=(
            None
            if sequence.lease is None
            else replace(sequence.lease, active_deadline=None, idle_expiry_handled=True)
        ),
        artifact_reservation=artifact,
        ledger_reservation=ledger,
    )


def prepare_sequence_child(
    sequence: ManagedSequenceV1,
    *,
    segment_id: str,
    expected_revision: int,
    materialization_receipt_fingerprint: str,
    predecessor_terminal_fingerprint: str | None,
    request_id: str,
) -> PreparedManagedSequenceChildV1:
    _require_revision(sequence, expected_revision)
    if sequence.state is not ManagedSequenceStateV1.ACTIVE or not sequence.client_attached:
        raise ManagedSequenceError("sequence_not_active")
    if sequence.active_segment_id is not None:
        raise ManagedSequenceError("one_live_child")
    index = _slot_index(sequence, _identifier(segment_id, "segment_id"))
    slot = sequence.slots[index]
    if slot.state is not SequenceSlotStateV1.ELIGIBLE:
        raise ManagedSequenceError("segment_not_eligible")
    authority = sequence.authorization.segments[index]
    if materialization_receipt_fingerprint != authority.materialization_receipt_fingerprint:
        raise ManagedSequenceError("materialization_drift")
    if index == 0:
        if predecessor_terminal_fingerprint is not None:
            raise ManagedSequenceError("predecessor_evidence")
    else:
        previous = sequence.slots[index - 1]
        if (
            previous.state not in {SequenceSlotStateV1.SUCCEEDED, SequenceSlotStateV1.REUSED}
            or previous.terminal_fingerprint is None
            or predecessor_terminal_fingerprint != previous.terminal_fingerprint
        ):
            raise ManagedSequenceError("predecessor_evidence")
    execution = EligibleSegmentExecutionV1(
        parent_sequence_id=sequence.authorization.sequence_id,
        parent_authorization_fingerprint=sequence.authorization.fingerprint,
        segment_id=slot.segment_id,
        # CRITICAL: the execution is consumed against the prepared parent, whose revision
        # _replace_slot advances below. Stamping the old revision makes the real resolver
        # reject every child before its canonical Context can be read.
        slot_revision=sequence.revision + 1,
        attempt_epoch=slot.attempt_epoch,
        materialization_receipt_fingerprint=materialization_receipt_fingerprint,
        predecessor_terminal_fingerprint=predecessor_terminal_fingerprint,
        request_id=request_id,
    )
    prepared_slot = replace(
        slot,
        state=SequenceSlotStateV1.PREPARED,
        eligible_execution_fingerprint=execution.fingerprint,
    )
    prepared = _replace_slot(
        sequence,
        index,
        prepared_slot,
        active_segment_id=slot.segment_id,
        ledger_reservation=_consume_child_transition(
            sequence,
            slot,
            retry_prepare=True,
        ),
    )
    return PreparedManagedSequenceChildV1(prepared, execution)


def bind_prepared_child(
    sequence: ManagedSequenceV1,
    *,
    segment_id: str,
    expected_revision: int,
    eligible_execution_fingerprint: str,
    child_run_handle: str,
    child_state_fingerprint: str,
    graph_fingerprint: str,
    compiled_prompt_fingerprint: str,
    previous_owned_projection_fingerprint: str,
    active_workflow_fingerprint: str,
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    index = _slot_index(sequence, segment_id)
    slot = sequence.slots[index]
    if slot.state is not SequenceSlotStateV1.PREPARED:
        raise ManagedSequenceError("child_not_prepared")
    if slot.eligible_execution_fingerprint != eligible_execution_fingerprint:
        raise ManagedSequenceError("eligible_execution_drift")
    bound = replace(
        slot,
        state=SequenceSlotStateV1.BOUND,
        child_run_handle=child_run_handle,
        child_state_fingerprint=_fingerprint(child_state_fingerprint, "child_state_fingerprint"),
        graph_fingerprint=_fingerprint(graph_fingerprint, "graph_fingerprint"),
        compiled_prompt_fingerprint=_fingerprint(
            compiled_prompt_fingerprint, "compiled_prompt_fingerprint"
        ),
    )
    previous_canvas = sequence.canvas_authority
    if previous_canvas is None:
        canvas = SequenceCanvasAuthorityV1(
            active_workflow_fingerprint=active_workflow_fingerprint,
            owned_projection_fingerprint=previous_owned_projection_fingerprint,
        )
    else:
        if (
            previous_canvas.active_workflow_fingerprint != active_workflow_fingerprint
            or previous_canvas.owned_projection_fingerprint != previous_owned_projection_fingerprint
        ):
            raise ManagedSequenceError("canvas_drift")
        # Preserve the sequence-wide write counters and expected owned projection. Replacing the
        # authority for each child would make an N-segment run appear to have only its final write.
        canvas = previous_canvas
    return _replace_slot(
        sequence,
        index,
        bound,
        canvas_authority=canvas,
        ledger_reservation=_consume_child_transition(sequence, slot),
    )


def fail_prepared_sequence_child(
    sequence: ManagedSequenceV1,
    *,
    segment_id: str,
    expected_revision: int,
    eligible_execution_fingerprint: str,
    failure_fingerprint: str,
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    index = _slot_index(sequence, segment_id)
    slot = sequence.slots[index]
    if slot.state is not SequenceSlotStateV1.PREPARED:
        raise ManagedSequenceError("child_not_prepared")
    if slot.eligible_execution_fingerprint != eligible_execution_fingerprint:
        raise ManagedSequenceError("eligible_execution_drift")
    failed = replace(
        slot,
        state=SequenceSlotStateV1.FAILED,
        terminal_fingerprint=_fingerprint(failure_fingerprint, "failure_fingerprint"),
    )
    return _replace_slot(
        sequence,
        index,
        failed,
        state=ManagedSequenceStateV1.PAUSED_FAILURE,
        active_segment_id=None,
        ledger_reservation=_consume_child_transition(sequence, slot),
    )


def fail_bound_sequence_child(
    sequence: ManagedSequenceV1,
    *,
    segment_id: str,
    expected_revision: int,
    child_run_handle: str,
    failure_fingerprint: str,
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    index = _slot_index(sequence, segment_id)
    slot = sequence.slots[index]
    handle = _identifier(child_run_handle, "child_run_handle")
    if _RUN_HANDLE.fullmatch(handle) is None:
        raise ManagedSequenceError("invalid_child_run_handle")
    if (
        slot.state is not SequenceSlotStateV1.BOUND
        or slot.child_run_handle != handle
        or sequence.active_segment_id != segment_id
    ):
        raise ManagedSequenceError("child_authority_mismatch")
    failed = replace(
        slot,
        state=SequenceSlotStateV1.FAILED,
        terminal_fingerprint=_fingerprint(failure_fingerprint, "failure_fingerprint"),
    )
    cancelled = sequence.state is ManagedSequenceStateV1.CANCELLED
    ledger = _consume_child_transition(sequence, slot)
    artifact = sequence.artifact_reservation
    if cancelled:
        ledger = replace(ledger, released=True)
        if artifact is not None:
            artifact = replace(artifact, released=True)
    return _replace_slot(
        sequence,
        index,
        failed,
        # IMPORTANT: late proof of non-ownership retires the child, not the user's cancellation.
        state=(
            ManagedSequenceStateV1.CANCELLED if cancelled else ManagedSequenceStateV1.PAUSED_FAILURE
        ),
        active_segment_id=None,
        ledger_reservation=ledger,
        artifact_reservation=artifact,
    )


def check_managed_sequence_canvas_authority(
    sequence: ManagedSequenceV1,
    *,
    expected_revision: int,
    active_workflow_fingerprint: str,
    owned_projection_fingerprint: str,
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    workflow = _fingerprint(active_workflow_fingerprint, "active_workflow_fingerprint")
    owned = _fingerprint(owned_projection_fingerprint, "owned_projection_fingerprint")
    canvas = sequence.canvas_authority
    if canvas is None:
        raise ManagedSequenceError("canvas_authority_unavailable")
    if (
        canvas.active_workflow_fingerprint == workflow
        and canvas.owned_projection_fingerprint == owned
    ):
        return sequence
    if sequence.active_segment_id is None:
        raise ManagedSequenceError("active_segment_unavailable")
    slot = sequence.slots[_slot_index(sequence, sequence.active_segment_id)]
    # IMPORTANT: this transition is the pre-effect CAS. A caller that receives it must perform no
    # canvas write or queue call; reporting drift after the write would make the pause decorative.
    return replace(
        sequence,
        state=ManagedSequenceStateV1.PAUSED_CANVAS_DRIFT,
        revision=sequence.revision + 1,
        ledger_reservation=_consume_child_transition(sequence, slot),
    )


def record_managed_sequence_canvas_rollback(
    sequence: ManagedSequenceV1,
    *,
    segment_id: str,
    expected_revision: int,
    active_workflow_fingerprint: str,
    drifted_owned_projection_fingerprint: str,
    restored_owned_projection_fingerprint: str,
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    if sequence.state is not ManagedSequenceStateV1.PAUSED_CANVAS_DRIFT:
        raise ManagedSequenceError("canvas_rollback_unavailable")
    index = _slot_index(sequence, _identifier(segment_id, "segment_id"))
    slot = sequence.slots[index]
    if sequence.active_segment_id != segment_id or slot.state is not SequenceSlotStateV1.PREPARED:
        raise ManagedSequenceError("canvas_rollback_child_state")
    if slot.attempt_epoch != 1 or sequence.retry_consumed:
        raise ManagedSequenceError("retry_exhausted")
    canvas = sequence.canvas_authority
    if canvas is None or canvas.rollback_write_count != 0:
        raise ManagedSequenceError("canvas_rollback_exhausted")
    workflow = _fingerprint(active_workflow_fingerprint, "active_workflow_fingerprint")
    drifted = _fingerprint(
        drifted_owned_projection_fingerprint,
        "drifted_owned_projection_fingerprint",
    )
    restored = _fingerprint(
        restored_owned_projection_fingerprint,
        "restored_owned_projection_fingerprint",
    )
    if canvas.active_workflow_fingerprint != workflow:
        raise ManagedSequenceError("active_workflow_drift")
    if drifted == canvas.owned_projection_fingerprint:
        raise ManagedSequenceError("canvas_rollback_unobserved")
    if restored != canvas.owned_projection_fingerprint:
        raise ManagedSequenceError("canvas_rollback_incomplete")
    reservation = sequence.ledger_reservation
    if (
        reservation is None
        or reservation.released
        or reservation.recovery_rows_consumed != 0
        or reservation.consumed_rows + 7 > reservation.reserved_rows
    ):
        raise ManagedSequenceError("recovery_capacity")
    # IMPORTANT: rollback replaces the abandoned epoch-one prepare/bind path with the single
    # fully reserved epoch-two recovery path. Recording a partial rollback would strand a child
    # after a canvas write while leaving no budget for its terminal release.
    rolled_back_slot = SequenceSlotV1(
        segment_id=slot.segment_id,
        ordinal=slot.ordinal,
        state=SequenceSlotStateV1.ELIGIBLE,
        attempt_epoch=2,
    )
    return _replace_slot(
        sequence,
        index,
        rolled_back_slot,
        state=ManagedSequenceStateV1.ACTIVE,
        active_segment_id=None,
        retry_consumed=True,
        canvas_authority=replace(
            canvas,
            rollback_write_count=canvas.rollback_write_count + 1,
        ),
        ledger_reservation=_consume_recovery_control(sequence),
    )


def record_managed_sequence_canvas_write(
    sequence: ManagedSequenceV1,
    *,
    segment_id: str,
    expected_revision: int,
    active_workflow_fingerprint: str,
    previous_owned_projection_fingerprint: str,
    written_owned_projection_fingerprint: str,
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    # IMPORTANT: cancellation can race a bound child's browser invocation. Accept its late
    # observation without authorizing another child or resurrecting the cancelled parent.
    if sequence.state not in {ManagedSequenceStateV1.ACTIVE, ManagedSequenceStateV1.CANCELLED}:
        raise ManagedSequenceError("sequence_not_active")
    index = _slot_index(sequence, segment_id)
    slot = sequence.slots[index]
    if slot.state is not SequenceSlotStateV1.BOUND or sequence.active_segment_id != segment_id:
        raise ManagedSequenceError("child_not_bound")
    canvas = sequence.canvas_authority
    if canvas is None:
        raise ManagedSequenceError("canvas_authority_unavailable")
    workflow = _fingerprint(active_workflow_fingerprint, "active_workflow_fingerprint")
    previous = _fingerprint(
        previous_owned_projection_fingerprint,
        "previous_owned_projection_fingerprint",
    )
    written = _fingerprint(
        written_owned_projection_fingerprint,
        "written_owned_projection_fingerprint",
    )
    if (
        canvas.active_workflow_fingerprint != workflow
        or canvas.owned_projection_fingerprint != previous
    ):
        raise ManagedSequenceError("canvas_drift")
    # IMPORTANT (B-M2522-WRITE-01): do not refuse `written == previous`. Adjacent segments inside
    # one long shot render the same local prompt and allocation, so a real dirty child writes the
    # owned content its predecessor left; refusing it stranded a submitted child with no recorded
    # write. The fingerprints are content, not write events: one write per slot is enforced below,
    # and execution identity is bound separately by the graph and compiled-prompt fingerprints.
    if slot.canvas_write_fingerprint is not None:
        raise ManagedSequenceError("canvas_write_already_recorded")
    return _replace_slot(
        sequence,
        index,
        replace(slot, canvas_write_fingerprint=written),
        canvas_authority=replace(
            canvas,
            owned_projection_fingerprint=written,
            owned_write_count=canvas.owned_write_count + 1,
        ),
    )


def record_managed_sequence_submission(
    sequence: ManagedSequenceV1,
    *,
    segment_id: str,
    expected_revision: int,
    child_run_handle: str,
    child_state_fingerprint: str,
    queue_prompt_id: str,
    timeout_ms: int,
    now: float,
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    index = _slot_index(sequence, segment_id)
    slot = sequence.slots[index]
    if slot.state in {
        SequenceSlotStateV1.SUBMITTED,
        SequenceSlotStateV1.RUNNING,
        SequenceSlotStateV1.ARTIFACT_VERIFIED,
        SequenceSlotStateV1.UNKNOWN_OWNERSHIP,
    }:
        raise ManagedSequenceError("duplicate_submission")
    if slot.state is not SequenceSlotStateV1.BOUND or slot.child_run_handle != child_run_handle:
        raise ManagedSequenceError("child_authority_mismatch")
    if slot.canvas_write_fingerprint is None:
        raise ManagedSequenceError("canvas_write_required")
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 86_400_000:
        raise ManagedSequenceError("invalid_timeout")
    if sequence.lease is None:
        raise ManagedSequenceError("lease_unavailable")
    submitted_at = _time(now, "submission_time")
    active_deadline = min(
        sequence.lease.absolute_deadline,
        submitted_at + (timeout_ms / 1000.0) + MANAGED_SEQUENCE_RECONCILIATION_GRACE_SECONDS,
    )
    submitted = replace(
        slot,
        state=SequenceSlotStateV1.SUBMITTED,
        child_state_fingerprint=_fingerprint(child_state_fingerprint, "child_state_fingerprint"),
        queue_prompt_id=_identifier(queue_prompt_id, "queue_prompt_id"),
    )
    return _replace_slot(
        sequence,
        index,
        submitted,
        lease=replace(
            sequence.lease,
            active_deadline=active_deadline,
            idle_expiry_handled=True,
        ),
        ledger_reservation=_consume_child_transition(sequence, slot),
    )


def _matching_prompt(slot: SequenceSlotV1, queue_prompt_id: str) -> None:
    if slot.queue_prompt_id != queue_prompt_id:
        raise ManagedSequenceError("foreign_prompt")


def record_managed_sequence_running(
    sequence: ManagedSequenceV1,
    *,
    segment_id: str,
    expected_revision: int,
    queue_prompt_id: str,
    child_state_fingerprint: str,
    now: float,
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    _time(now, "running_observation_time")
    index = _slot_index(sequence, segment_id)
    slot = sequence.slots[index]
    _matching_prompt(slot, queue_prompt_id)
    if slot.state is SequenceSlotStateV1.RUNNING:
        return sequence
    if slot.state is not SequenceSlotStateV1.SUBMITTED:
        raise ManagedSequenceError("invalid_child_transition")
    running = replace(
        slot,
        state=SequenceSlotStateV1.RUNNING,
        child_state_fingerprint=_fingerprint(child_state_fingerprint, "child_state_fingerprint"),
    )
    # IMPORTANT: progress observations never renew active or absolute leases. Sliding this deadline
    # lets a noisy host retain capacity forever and prevents deterministic absent-client recovery.
    return _replace_slot(
        sequence,
        index,
        running,
        ledger_reservation=_consume_child_transition(sequence, slot),
    )


def record_managed_sequence_artifact(
    sequence: ManagedSequenceV1,
    *,
    segment_id: str,
    expected_revision: int,
    queue_prompt_id: str,
    artifact_receipt_fingerprint: str,
    artifact_bytes: int,
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    index = _slot_index(sequence, segment_id)
    slot = sequence.slots[index]
    _matching_prompt(slot, queue_prompt_id)
    if slot.state not in {SequenceSlotStateV1.SUBMITTED, SequenceSlotStateV1.RUNNING}:
        raise ManagedSequenceError("invalid_child_transition")
    reservation = sequence.artifact_reservation
    if reservation is None or reservation.released:
        raise ManagedSequenceError("artifact_reservation_unavailable")
    if (
        type(artifact_bytes) is not int
        or not 0 <= artifact_bytes <= reservation.per_artifact_bytes
        or reservation.consumed_entries >= reservation.reserved_entries
        or reservation.consumed_bytes + artifact_bytes > reservation.reserved_bytes
    ):
        raise ManagedSequenceError("artifact_reservation_exhausted")
    artifact = replace(
        slot,
        state=SequenceSlotStateV1.ARTIFACT_VERIFIED,
        artifact_receipt_fingerprint=_fingerprint(
            artifact_receipt_fingerprint, "artifact_receipt_fingerprint"
        ),
    )
    return _replace_slot(
        sequence,
        index,
        artifact,
        artifact_reservation=replace(
            reservation,
            consumed_entries=reservation.consumed_entries + 1,
            consumed_bytes=reservation.consumed_bytes + artifact_bytes,
        ),
        ledger_reservation=_consume_child_transition(sequence, slot),
    )


def record_managed_sequence_terminal(
    sequence: ManagedSequenceV1,
    *,
    segment_id: str,
    expected_revision: int,
    queue_prompt_id: str,
    kind: str,
    terminal_fingerprint: str,
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    index = _slot_index(sequence, segment_id)
    slot = sequence.slots[index]
    _matching_prompt(slot, queue_prompt_id)
    target = {
        "success": SequenceSlotStateV1.SUCCEEDED,
        "error": SequenceSlotStateV1.FAILED,
        "interrupted": SequenceSlotStateV1.INTERRUPTED,
    }.get(kind)
    if target is None:
        raise ManagedSequenceError("invalid_terminal_kind")
    if kind == "success" and slot.state is not SequenceSlotStateV1.ARTIFACT_VERIFIED:
        raise ManagedSequenceError("artifact_not_verified")
    if kind != "success" and slot.state not in {
        SequenceSlotStateV1.SUBMITTED,
        SequenceSlotStateV1.RUNNING,
        SequenceSlotStateV1.ARTIFACT_VERIFIED,
    }:
        raise ManagedSequenceError("invalid_child_transition")
    terminal = replace(
        slot,
        state=target,
        terminal_fingerprint=_fingerprint(terminal_fingerprint, "terminal_fingerprint"),
    )
    slots = sequence.slots[:index] + (terminal,) + sequence.slots[index + 1 :]
    parent_state = sequence.state
    cancelled_parent = sequence.state is ManagedSequenceStateV1.CANCELLED
    if cancelled_parent:
        parent_state = ManagedSequenceStateV1.CANCELLED
    elif kind == "success":
        if index + 1 == len(slots):
            parent_state = (
                ManagedSequenceStateV1.SUCCEEDED
                if sequence.client_attached
                else ManagedSequenceStateV1.PAUSED_CLIENT_ABSENT
            )
        elif sequence.client_attached:
            slots = (
                slots[: index + 1]
                + (replace(slots[index + 1], state=SequenceSlotStateV1.ELIGIBLE),)
                + slots[index + 2 :]
            )
            parent_state = ManagedSequenceStateV1.ACTIVE
        else:
            parent_state = ManagedSequenceStateV1.PAUSED_CLIENT_ABSENT
    else:
        parent_state = ManagedSequenceStateV1.PAUSED_FAILURE
    artifact_reservation = sequence.artifact_reservation
    ledger_reservation = _consume_child_transition(sequence, slot, 2)
    if parent_state in {
        ManagedSequenceStateV1.SUCCEEDED,
        ManagedSequenceStateV1.CANCELLED,
    }:
        if artifact_reservation is not None:
            artifact_reservation = replace(artifact_reservation, released=True)
        ledger_reservation = replace(ledger_reservation, released=True)
    return replace(
        sequence,
        state=parent_state,
        revision=sequence.revision + 1,
        slots=slots,
        active_segment_id=None,
        lease=(
            None
            if sequence.lease is None
            else replace(sequence.lease, active_deadline=None, idle_expiry_handled=True)
        ),
        artifact_reservation=artifact_reservation,
        ledger_reservation=ledger_reservation,
    )


def detach_managed_sequence_client(
    sequence: ManagedSequenceV1, *, expected_revision: int
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    if sequence.state in {ManagedSequenceStateV1.CANCELLED, ManagedSequenceStateV1.SUCCEEDED}:
        return sequence
    if not sequence.client_attached:
        raise ManagedSequenceError("client_already_detached")
    ledger = _consume_attachment_control(sequence, needs_resume=True)
    return replace(
        sequence,
        # IMPORTANT: attachment is orthogonal to failure, drift and ownership holds. Replacing
        # a hold with client-absent lets resume bypass the explicit retry/reconciliation action.
        state=(
            ManagedSequenceStateV1.PAUSED_CLIENT_ABSENT
            if sequence.state is ManagedSequenceStateV1.ACTIVE
            else sequence.state
        ),
        revision=sequence.revision + 1,
        client_attached=False,
        ledger_reservation=ledger,
    )


def resume_managed_sequence(
    sequence: ManagedSequenceV1,
    *,
    expected_revision: int,
    active_workflow_fingerprint: str,
    owned_projection_fingerprint: str,
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    if sequence.state is ManagedSequenceStateV1.PAUSED_UNKNOWN_OWNERSHIP or any(
        row.state is SequenceSlotStateV1.UNKNOWN_OWNERSHIP for row in sequence.slots
    ):
        raise ManagedSequenceError("ownership_unresolved")
    if sequence.state is not ManagedSequenceStateV1.PAUSED_CLIENT_ABSENT:
        raise ManagedSequenceError("sequence_not_resumable")
    if any(
        row.state
        in {
            SequenceSlotStateV1.FAILED,
            SequenceSlotStateV1.INTERRUPTED,
            SequenceSlotStateV1.CANCELLED,
        }
        for row in sequence.slots
    ):
        raise ManagedSequenceError("sequence_not_resumable")
    if sequence.active_segment_id is not None:
        raise ManagedSequenceError("current_child_not_terminal")
    canvas = sequence.canvas_authority
    if canvas is not None and (
        canvas.active_workflow_fingerprint != active_workflow_fingerprint
        or canvas.owned_projection_fingerprint != owned_projection_fingerprint
    ):
        raise ManagedSequenceError("canvas_drift")
    slots = sequence.slots
    # IMPORTANT: a child may finish before client absence is recorded, so its successor can
    # already be eligible. Resume must preserve that single slot instead of unlocking another one.
    next_index = next(
        (index for index, row in enumerate(slots) if row.state is SequenceSlotStateV1.ELIGIBLE),
        None,
    )
    if next_index is None:
        next_index = next(
            (index for index, row in enumerate(slots) if row.state is SequenceSlotStateV1.PENDING),
            None,
        )
    # IMPORTANT: resume may unlock only the first uncompleted slot after a proved-success
    # prefix. No eligible/pending slot is not evidence that the whole sequence succeeded.
    if not all(_successful_slot(row) for row in slots[:next_index]):
        raise ManagedSequenceError("successful_slot_proof")
    ledger = (
        _consume_attachment_control(sequence)
        if sequence.retry_consumed
        else _consume_recovery_control(sequence)
    )
    if next_index is None:
        artifact = sequence.artifact_reservation
        return replace(
            sequence,
            state=ManagedSequenceStateV1.SUCCEEDED,
            revision=sequence.revision + 1,
            client_attached=True,
            ledger_reservation=replace(ledger, released=True),
            artifact_reservation=(None if artifact is None else replace(artifact, released=True)),
        )
    if slots[next_index].state is SequenceSlotStateV1.PENDING:
        slots = (
            slots[:next_index]
            + (replace(slots[next_index], state=SequenceSlotStateV1.ELIGIBLE),)
            + slots[next_index + 1 :]
        )
    return replace(
        sequence,
        state=ManagedSequenceStateV1.ACTIVE,
        revision=sequence.revision + 1,
        slots=slots,
        client_attached=True,
        ledger_reservation=ledger,
    )


def mark_managed_sequence_unknown_ownership(
    sequence: ManagedSequenceV1,
    *,
    segment_id: str,
    expected_revision: int,
    queue_prompt_id: str,
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    index = _slot_index(sequence, segment_id)
    slot = sequence.slots[index]
    _matching_prompt(slot, queue_prompt_id)
    if slot.state not in {SequenceSlotStateV1.SUBMITTED, SequenceSlotStateV1.RUNNING}:
        raise ManagedSequenceError("invalid_child_transition")
    unknown = replace(slot, state=SequenceSlotStateV1.UNKNOWN_OWNERSHIP)
    return _replace_slot(
        sequence,
        index,
        unknown,
        state=ManagedSequenceStateV1.PAUSED_UNKNOWN_OWNERSHIP,
    )


def mark_managed_sequence_invocation_unknown(
    sequence: ManagedSequenceV1,
    *,
    segment_id: str,
    expected_revision: int,
    child_run_handle: str,
    timeout_ms: int,
    now: float,
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    index = _slot_index(sequence, segment_id)
    slot = sequence.slots[index]
    handle = _identifier(child_run_handle, "child_run_handle")
    if _RUN_HANDLE.fullmatch(handle) is None:
        raise ManagedSequenceError("invalid_child_run_handle")
    if (
        slot.state is not SequenceSlotStateV1.BOUND
        or slot.child_run_handle != handle
        or slot.canvas_write_fingerprint is None
        or sequence.active_segment_id != segment_id
    ):
        raise ManagedSequenceError("child_authority_mismatch")
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 86_400_000:
        raise ManagedSequenceError("invalid_timeout")
    lease = sequence.lease
    if lease is None:
        raise ManagedSequenceError("lease_unavailable")
    observed = _time(now, "invocation_observation_time")
    unknown = replace(slot, state=SequenceSlotStateV1.UNKNOWN_OWNERSHIP)
    return _replace_slot(
        sequence,
        index,
        unknown,
        state=ManagedSequenceStateV1.PAUSED_UNKNOWN_OWNERSHIP,
        lease=replace(
            lease,
            active_deadline=min(
                lease.absolute_deadline,
                observed + (timeout_ms / 1000.0) + MANAGED_SEQUENCE_RECONCILIATION_GRACE_SECONDS,
            ),
            idle_expiry_handled=True,
        ),
        ledger_reservation=_consume_child_transition(sequence, slot),
    )


def expire_managed_sequence_lease(
    sequence: ManagedSequenceV1,
    *,
    expected_revision: int,
    now: float,
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    if sequence.state in {ManagedSequenceStateV1.CANCELLED, ManagedSequenceStateV1.SUCCEEDED}:
        return sequence
    lease = sequence.lease
    if lease is None:
        raise ManagedSequenceError("lease_unavailable")
    observed = _time(now, "lease_expiry_time")
    phase_deadline = (
        lease.active_deadline
        if lease.active_deadline is not None
        else lease.absolute_deadline
        if lease.idle_expiry_handled
        else lease.idle_deadline
    )
    deadline = min(lease.absolute_deadline, phase_deadline)
    if observed <= deadline:
        raise ManagedSequenceError("lease_active")
    if (
        sequence.state is ManagedSequenceStateV1.PAUSED_UNKNOWN_OWNERSHIP
        and not sequence.client_attached
    ):
        return sequence
    slots = sequence.slots
    state = ManagedSequenceStateV1.PAUSED_CLIENT_ABSENT
    active_segment_id = sequence.active_segment_id
    if sequence.active_segment_id is not None:
        index = _slot_index(sequence, sequence.active_segment_id)
        slot = slots[index]
        if slot.state in {
            SequenceSlotStateV1.SUBMITTED,
            SequenceSlotStateV1.RUNNING,
            SequenceSlotStateV1.ARTIFACT_VERIFIED,
            SequenceSlotStateV1.UNKNOWN_OWNERSHIP,
        }:
            slots = (
                slots[:index]
                + (replace(slot, state=SequenceSlotStateV1.UNKNOWN_OWNERSHIP),)
                + slots[index + 1 :]
            )
            state = ManagedSequenceStateV1.PAUSED_UNKNOWN_OWNERSHIP
        elif slot.state in {SequenceSlotStateV1.PREPARED, SequenceSlotStateV1.BOUND}:
            # CRITICAL: no prompt exists in these states. The lease-transition owner releases the
            # exact Context/Production/ManagedRun resources before publishing this reset; retaining
            # their identifiers here would fabricate host ownership and strand the only child slot.
            slots = (
                slots[:index]
                + (
                    SequenceSlotV1(
                        segment_id=slot.segment_id,
                        ordinal=slot.ordinal,
                        state=SequenceSlotStateV1.ELIGIBLE,
                        attempt_epoch=slot.attempt_epoch,
                    ),
                )
                + slots[index + 1 :]
            )
            active_segment_id = None
    # CRITICAL: retain active_segment_id and its exact prompt/run fields only when the host may own
    # the submitted child. Pre-submit authority is reset only after its owner releases resources.
    return replace(
        sequence,
        state=state,
        revision=sequence.revision + 1,
        slots=slots,
        client_attached=False,
        active_segment_id=active_segment_id,
        lease=replace(lease, idle_expiry_handled=True),
    )


def retry_managed_sequence_segment(
    sequence: ManagedSequenceV1,
    *,
    segment_id: str,
    expected_revision: int,
    active_workflow_fingerprint: str,
    owned_projection_fingerprint: str,
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    if sequence.retry_consumed:
        raise ManagedSequenceError("retry_exhausted")
    if sequence.state is not ManagedSequenceStateV1.PAUSED_FAILURE:
        raise ManagedSequenceError("retry_unavailable")
    index = _slot_index(sequence, segment_id)
    slot = sequence.slots[index]
    if slot.state not in {
        SequenceSlotStateV1.FAILED,
        SequenceSlotStateV1.INTERRUPTED,
        SequenceSlotStateV1.CANCELLED,
    }:
        raise ManagedSequenceError("retry_unavailable")
    active_workflow = _fingerprint(
        active_workflow_fingerprint,
        "active_workflow_fingerprint",
    )
    owned_projection = _fingerprint(
        owned_projection_fingerprint,
        "owned_projection_fingerprint",
    )
    canvas = sequence.canvas_authority
    if canvas is not None and (
        canvas.active_workflow_fingerprint != active_workflow
        or canvas.owned_projection_fingerprint != owned_projection
    ):
        raise ManagedSequenceError("canvas_drift")
    reservation = sequence.ledger_reservation
    if reservation is None or reservation.released or reservation.recovery_rows_consumed != 0:
        raise ManagedSequenceError("retry_exhausted")
    if reservation.consumed_rows + 7 > reservation.reserved_rows:
        raise ManagedSequenceError("retry_capacity")
    retried = SequenceSlotV1(
        segment_id=slot.segment_id,
        ordinal=slot.ordinal,
        state=SequenceSlotStateV1.ELIGIBLE,
        attempt_epoch=slot.attempt_epoch + 1,
    )
    return _replace_slot(
        sequence,
        index,
        retried,
        state=ManagedSequenceStateV1.ACTIVE,
        active_segment_id=None,
        client_attached=True,
        retry_consumed=True,
        ledger_reservation=replace(reservation, recovery_rows_consumed=1),
    )


def cancel_managed_sequence(
    sequence: ManagedSequenceV1, *, expected_revision: int
) -> ManagedSequenceV1:
    _require_revision(sequence, expected_revision)
    if sequence.state is ManagedSequenceStateV1.CANCELLED:
        return sequence
    host_owned_states = {
        # CRITICAL: a published binding may already be invoked before submission is reported.
        # Preserve it until explicit non-ownership cleanup or the exact late host observation.
        SequenceSlotStateV1.BOUND,
        SequenceSlotStateV1.SUBMITTED,
        SequenceSlotStateV1.RUNNING,
        SequenceSlotStateV1.ARTIFACT_VERIFIED,
        SequenceSlotStateV1.UNKNOWN_OWNERSHIP,
    }
    host_owned = any(row.state in host_owned_states for row in sequence.slots)
    slots = tuple(
        (
            row
            if row.state
            in {
                *host_owned_states,
                SequenceSlotStateV1.SUCCEEDED,
                SequenceSlotStateV1.REUSED,
            }
            else replace(row, state=SequenceSlotStateV1.CANCELLED)
        )
        for row in sequence.slots
    )
    artifact = sequence.artifact_reservation
    ledger = sequence.ledger_reservation
    if ledger is not None and not ledger.released:
        recovery_consumed = ledger.recovery_rows_consumed + 1
        if (
            recovery_consumed > MAX_SEQUENCE_RECOVERY_ACTIONS
            or ledger.consumed_rows + recovery_consumed > ledger.reserved_rows
        ):
            raise ManagedSequenceError("cancel_capacity")
        ledger = replace(
            ledger,
            recovery_rows_consumed=recovery_consumed,
            released=not host_owned,
        )
    if artifact is not None and not host_owned:
        artifact = replace(artifact, released=True)
    return replace(
        sequence,
        state=ManagedSequenceStateV1.CANCELLED,
        revision=sequence.revision + 1,
        slots=slots,
        active_segment_id=sequence.active_segment_id if host_owned else None,
        artifact_reservation=artifact,
        ledger_reservation=ledger,
    )


def read_managed_sequence_projection(
    sequence: ManagedSequenceV1,
) -> ManagedSequenceCurrentProjectionV1:
    if sequence.lease is None:
        raise ManagedSequenceError("sequence_not_started")
    # IMPORTANT: projection reads never consume replay rows or touch lease timestamps. Polling must
    # observe current state without extending authority or exhausting the coordinator ledger.
    projection = ManagedSequenceCurrentProjectionV1(
        parent_sequence_id=sequence.authorization.sequence_id,
        authorization_fingerprint=sequence.authorization.fingerprint,
        state=sequence.state,
        revision=sequence.revision,
        etag=sequence.fingerprint,
        lease=sequence.lease,
        active_segment_id=sequence.active_segment_id,
        slots=sequence.slots,
    )
    if len(canonical_bytes(projection.to_wire())) > MAX_MANAGED_SEQUENCE_PROJECTION_BYTES:
        raise ManagedSequenceError("projection_size")
    return projection


__all__ = [
    "ARTIFACT_STORE_TOTAL_BYTES",
    "MANAGED_SEQUENCE_ABSOLUTE_SECONDS",
    "MANAGED_SEQUENCE_IDLE_SECONDS",
    "MANAGED_SEQUENCE_RECONCILIATION_GRACE_SECONDS",
    "MAX_MANAGED_SEQUENCE_SEGMENTS",
    "ArtifactCapacityReservationV1",
    "CoordinatorLedgerReservationV1",
    "EligibleSegmentExecutionV1",
    "ManagedModeQualificationV1",
    "ManagedSequenceAuthorizationV1",
    "ManagedSequenceCurrentProjectionV1",
    "ManagedSequenceError",
    "ManagedSequenceSegmentAuthorizationV1",
    "ManagedSequenceStateV1",
    "ManagedSequenceV1",
    "PreparedManagedSequenceChildV1",
    "SequenceCanvasAuthorityV1",
    "SequenceLeaseV1",
    "SequenceSlotStateV1",
    "SequenceSlotV1",
    "authorize_managed_sequence",
    "bind_prepared_child",
    "cancel_managed_sequence",
    "check_managed_sequence_canvas_authority",
    "coordinator_ledger_rows",
    "detach_managed_sequence_client",
    "expire_managed_sequence_lease",
    "fail_prepared_sequence_child",
    "mark_managed_sequence_unknown_ownership",
    "prepare_sequence_child",
    "read_managed_sequence_projection",
    "record_managed_sequence_canvas_rollback",
    "record_managed_sequence_canvas_write",
    "record_managed_sequence_artifact",
    "record_managed_sequence_running",
    "record_managed_sequence_reuse",
    "record_managed_sequence_submission",
    "record_managed_sequence_terminal",
    "resume_managed_sequence",
    "retry_managed_sequence_segment",
    "start_managed_sequence",
]
