"""Content-free M17 recompute and transaction transparency projection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .canonical import canonical_bytes, canonical_fingerprint
from .pipeline_transaction import (
    MAX_PIPELINE_TRANSACTION_ATTEMPTS,
    PipelineTransaction,
    PipelineTransactionState,
)
from .recompute_closure import RecomputeDisposition, RecomputePlan, SegmentRecomputeDecision
from .segment_workspace import MAX_WORKSPACE_SEGMENTS
from .ui_projection import ExecutionCorrelation

TRANSACTION_TRANSPARENCY_SCHEMA = "h3.context.transaction_transparency.v1"
MAX_TRANSACTION_TRANSPARENCY_BYTES = 65_536
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


class TransactionTransparencyError(ValueError):
    """Raised when UI transparency would weaken backend authority."""


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise TransactionTransparencyError(field)
    return value


def _fingerprint(value: object, field: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise TransactionTransparencyError(field)
    return value


def _optional_fingerprint(value: object, field: str) -> None:
    if value is not None:
        _fingerprint(value, field)


def _identifier_tuple(value: object, field: str) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > MAX_WORKSPACE_SEGMENTS:
        raise TransactionTransparencyError(field)
    for item in value:
        _identifier(item, field)
    if len(set(value)) != len(value):
        raise TransactionTransparencyError(field)
    return value


class TransactionGuidance(str, Enum):
    NO_WORK_REQUIRED = "no_work_required"
    RESOLVE_BLOCKER = "resolve_blocker"
    REVIEW_BEFORE_NATIVE_QUEUE = "review_before_native_queue"
    INSPECT_QUEUE_HISTORY = "inspect_queue_history"
    OWNERSHIP_UNKNOWN = "ownership_unknown"
    GENERATION_COMPLETE = "generation_complete"
    RERUN_AVAILABLE = "rerun_available"


@dataclass(frozen=True, slots=True)
class TransactionTransparencyDecision:
    segment_id: str
    disposition: str
    reason_codes: tuple[str, ...]
    triggering_segment_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.segment_id, "decision_segment_id")
        if self.disposition not in {item.value for item in RecomputeDisposition}:
            raise TransactionTransparencyError("decision_disposition")
        _identifier_tuple(self.reason_codes, "decision_reason_codes")
        _identifier_tuple(self.triggering_segment_ids, "decision_triggering_segment_ids")

    @classmethod
    def from_decision(
        cls,
        decision: SegmentRecomputeDecision,
    ) -> TransactionTransparencyDecision:
        return cls(
            segment_id=decision.segment_id,
            disposition=decision.disposition.value,
            reason_codes=decision.reason_codes,
            triggering_segment_ids=decision.triggering_segment_ids,
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "disposition": self.disposition,
            "reason_codes": list(self.reason_codes),
            "triggering_segment_ids": list(self.triggering_segment_ids),
        }


@dataclass(frozen=True, slots=True)
class TransactionTransparencyActions:
    confirm_native_queue: bool
    inspect_queue_history: bool
    return_to_native: bool
    rerun: bool

    def __post_init__(self) -> None:
        if any(
            type(value) is not bool
            for value in (
                self.confirm_native_queue,
                self.inspect_queue_history,
                self.return_to_native,
                self.rerun,
            )
        ):
            raise TransactionTransparencyError("actions")

    def to_wire(self) -> dict[str, bool]:
        return {
            "confirm_native_queue": self.confirm_native_queue,
            "inspect_queue_history": self.inspect_queue_history,
            "return_to_native": self.return_to_native,
            "rerun": self.rerun,
        }


@dataclass(frozen=True, slots=True)
class TransactionTransparencySnapshot:
    transaction_id: str
    transaction_fingerprint: str
    attempt: int
    state: PipelineTransactionState
    graph_fingerprint: str | None
    compiled_prompt_fingerprint: str | None
    queue_prompt_id: str | None
    host_owner_id: str | None
    result_fingerprint: str | None
    cancellation_requested: bool

    def __post_init__(self) -> None:
        _identifier(self.transaction_id, "transaction_id")
        _fingerprint(self.transaction_fingerprint, "transaction_fingerprint")
        if type(self.attempt) is not int or not (
            1 <= self.attempt <= MAX_PIPELINE_TRANSACTION_ATTEMPTS
        ):
            raise TransactionTransparencyError("transaction_attempt")
        if type(self.state) is not PipelineTransactionState:
            raise TransactionTransparencyError("transaction_state")
        _optional_fingerprint(self.graph_fingerprint, "graph_fingerprint")
        _optional_fingerprint(
            self.compiled_prompt_fingerprint,
            "compiled_prompt_fingerprint",
        )
        _optional_fingerprint(self.result_fingerprint, "result_fingerprint")
        if self.queue_prompt_id is not None:
            _identifier(self.queue_prompt_id, "queue_prompt_id")
        if self.host_owner_id is not None:
            _identifier(self.host_owner_id, "host_owner_id")
        if type(self.cancellation_requested) is not bool:
            raise TransactionTransparencyError("cancellation_requested")

    def to_wire(self) -> dict[str, object]:
        return {
            "transaction_id": self.transaction_id,
            "transaction_fingerprint": self.transaction_fingerprint,
            "attempt": self.attempt,
            "state": self.state.value,
            "graph_fingerprint": self.graph_fingerprint,
            "compiled_prompt_fingerprint": self.compiled_prompt_fingerprint,
            "queue_prompt_id": self.queue_prompt_id,
            "host_owner_id": self.host_owner_id,
            "result_fingerprint": self.result_fingerprint,
            "cancellation_requested": self.cancellation_requested,
        }


@dataclass(frozen=True, slots=True)
class TransactionTransparencyProjection:
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    recompute_plan_fingerprint: str
    correlation: ExecutionCorrelation
    selection_safe: bool
    requires_full_recompute: bool
    mandatory_segment_ids: tuple[str, ...]
    requested_segment_ids: tuple[str, ...] | None
    missing_required_segment_ids: tuple[str, ...]
    decisions: tuple[TransactionTransparencyDecision, ...]
    transaction: TransactionTransparencySnapshot
    actions: TransactionTransparencyActions
    guidance: TransactionGuidance
    schema: str = TRANSACTION_TRANSPARENCY_SCHEMA

    def __post_init__(self) -> None:
        # CRITICAL: this public wire contract must reject direct construction that bypasses builder
        # authority; otherwise private text or forged action eligibility could reach the UI.
        if self.schema != TRANSACTION_TRANSPARENCY_SCHEMA:
            raise TransactionTransparencyError("projection_schema")
        _identifier(self.workspace_id, "workspace_id")
        if type(self.workspace_revision) is not int or not (
            1 <= self.workspace_revision <= 1_000_000
        ):
            raise TransactionTransparencyError("workspace_revision")
        _fingerprint(self.workspace_fingerprint, "workspace_fingerprint")
        _fingerprint(self.recompute_plan_fingerprint, "recompute_plan_fingerprint")
        if type(self.correlation) is not ExecutionCorrelation:
            raise TransactionTransparencyError("correlation")
        if type(self.selection_safe) is not bool or type(self.requires_full_recompute) is not bool:
            raise TransactionTransparencyError("selection_flags")
        _identifier_tuple(self.mandatory_segment_ids, "mandatory_segment_ids")
        if self.requested_segment_ids is not None:
            _identifier_tuple(self.requested_segment_ids, "requested_segment_ids")
        _identifier_tuple(self.missing_required_segment_ids, "missing_required_segment_ids")
        if (
            type(self.decisions) is not tuple
            or len(self.decisions) > MAX_WORKSPACE_SEGMENTS
            or any(type(item) is not TransactionTransparencyDecision for item in self.decisions)
            or len({item.segment_id for item in self.decisions}) != len(self.decisions)
        ):
            raise TransactionTransparencyError("decisions")
        if type(self.transaction) is not TransactionTransparencySnapshot:
            raise TransactionTransparencyError("transaction")
        if type(self.actions) is not TransactionTransparencyActions:
            raise TransactionTransparencyError("actions")
        if type(self.guidance) is not TransactionGuidance:
            raise TransactionTransparencyError("guidance")
        expected_actions, expected_guidance = _action_policy(self.transaction.state)
        if self.actions != expected_actions or self.guidance is not expected_guidance:
            raise TransactionTransparencyError("action_policy")

    def to_wire(self) -> dict[str, object]:
        value = {
            "schema": self.schema,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "recompute_plan_fingerprint": self.recompute_plan_fingerprint,
            "correlation": self.correlation.to_wire(),
            "selection_safe": self.selection_safe,
            "requires_full_recompute": self.requires_full_recompute,
            "mandatory_segment_ids": list(self.mandatory_segment_ids),
            "requested_segment_ids": (
                None if self.requested_segment_ids is None else list(self.requested_segment_ids)
            ),
            "missing_required_segment_ids": list(self.missing_required_segment_ids),
            "decisions": [item.to_wire() for item in self.decisions],
            "transaction": self.transaction.to_wire(),
            "actions": self.actions.to_wire(),
            "guidance": self.guidance.value,
        }
        if len(canonical_bytes(value)) > MAX_TRANSACTION_TRANSPARENCY_BYTES:
            raise TransactionTransparencyError("projection_limit")
        return value

    def to_public_dict(self) -> dict[str, object]:
        return self.to_wire()


def _action_policy(
    state: PipelineTransactionState,
) -> tuple[TransactionTransparencyActions, TransactionGuidance]:
    if state is PipelineTransactionState.NOOP_CLEAN:
        guidance = TransactionGuidance.NO_WORK_REQUIRED
    elif state is PipelineTransactionState.BLOCKED:
        guidance = TransactionGuidance.RESOLVE_BLOCKER
    elif state is PipelineTransactionState.PREPARED:
        guidance = TransactionGuidance.REVIEW_BEFORE_NATIVE_QUEUE
    elif state in {PipelineTransactionState.SUBMITTED, PipelineTransactionState.RUNNING}:
        guidance = TransactionGuidance.INSPECT_QUEUE_HISTORY
    elif state is PipelineTransactionState.UNKNOWN_OWNERSHIP:
        guidance = TransactionGuidance.OWNERSHIP_UNKNOWN
    elif state is PipelineTransactionState.SUCCEEDED:
        guidance = TransactionGuidance.GENERATION_COMPLETE
    else:
        guidance = TransactionGuidance.RERUN_AVAILABLE
    return (
        TransactionTransparencyActions(
            confirm_native_queue=state is PipelineTransactionState.PREPARED,
            inspect_queue_history=state
            in {
                PipelineTransactionState.SUBMITTED,
                PipelineTransactionState.RUNNING,
                PipelineTransactionState.SUCCEEDED,
                PipelineTransactionState.FAILED,
                PipelineTransactionState.UNKNOWN_OWNERSHIP,
            },
            return_to_native=True,
            rerun=state in {PipelineTransactionState.FAILED, PipelineTransactionState.CANCELLED},
        ),
        guidance,
    )


def _verify_transaction(transaction: PipelineTransaction) -> None:
    wire = transaction.to_public_dict()
    fingerprint = wire.pop("transaction_fingerprint")
    if fingerprint != transaction.fingerprint or canonical_fingerprint(wire) != fingerprint:
        raise TransactionTransparencyError("tampered_transaction")


def build_transaction_transparency_projection(
    recompute_plan: RecomputePlan,
    transaction: PipelineTransaction,
    correlation: ExecutionCorrelation,
) -> TransactionTransparencyProjection:
    """Project exact backend authority without prompt, media, path or credential content."""

    if type(recompute_plan) is not RecomputePlan:
        raise TransactionTransparencyError("recompute_plan_type")
    if type(transaction) is not PipelineTransaction:
        raise TransactionTransparencyError("transaction_type")
    if type(correlation) is not ExecutionCorrelation:
        raise TransactionTransparencyError("correlation_type")
    _verify_transaction(transaction)
    plan_fingerprint = canonical_fingerprint(recompute_plan.to_public_dict())
    if transaction.recompute_plan_fingerprint != plan_fingerprint:
        raise TransactionTransparencyError("recompute_authority_mismatch")
    if transaction.dirty_segment_ids != recompute_plan.mandatory_segment_ids:
        raise TransactionTransparencyError("dirty_segment_authority_mismatch")
    actions, guidance = _action_policy(transaction.state)
    projection = TransactionTransparencyProjection(
        workspace_id=transaction.workspace_id,
        workspace_revision=transaction.workspace_revision,
        workspace_fingerprint=transaction.workspace_fingerprint,
        recompute_plan_fingerprint=plan_fingerprint,
        correlation=correlation,
        selection_safe=recompute_plan.selection_safe,
        requires_full_recompute=recompute_plan.requires_full_recompute,
        mandatory_segment_ids=recompute_plan.mandatory_segment_ids,
        requested_segment_ids=recompute_plan.requested_segment_ids,
        missing_required_segment_ids=recompute_plan.missing_required_segment_ids,
        decisions=tuple(
            TransactionTransparencyDecision.from_decision(item) for item in recompute_plan.decisions
        ),
        transaction=TransactionTransparencySnapshot(
            transaction_id=transaction.transaction_id,
            transaction_fingerprint=transaction.fingerprint,
            attempt=transaction.attempt,
            state=transaction.state,
            graph_fingerprint=transaction.graph_fingerprint,
            compiled_prompt_fingerprint=transaction.compiled_prompt_fingerprint,
            queue_prompt_id=transaction.queue_prompt_id,
            host_owner_id=transaction.host_owner_id,
            result_fingerprint=transaction.result_fingerprint,
            cancellation_requested=transaction.cancellation_requested,
        ),
        actions=actions,
        guidance=guidance,
    )
    projection.to_wire()
    return projection


__all__ = [
    "MAX_TRANSACTION_TRANSPARENCY_BYTES",
    "TRANSACTION_TRANSPARENCY_SCHEMA",
    "TransactionGuidance",
    "TransactionTransparencyActions",
    "TransactionTransparencyDecision",
    "TransactionTransparencyError",
    "TransactionTransparencyProjection",
    "TransactionTransparencySnapshot",
    "build_transaction_transparency_projection",
]
