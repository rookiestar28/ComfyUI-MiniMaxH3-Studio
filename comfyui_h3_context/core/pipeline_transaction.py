"""Canonical M17 identity and lifecycle around existing pipeline receipts."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum

from .canonical import canonical_fingerprint
from .recompute_closure import (
    RecomputeDisposition,
    RecomputePlan,
)
from .segment_workspace import (
    MAX_WORKSPACE_SEGMENTS,
    MultiSegmentWorkspace,
    SegmentContextManifest,
    derive_segment_manifests,
)

PIPELINE_TRANSACTION_SCHEMA = "h3.context.pipeline_transaction.v1"
MAX_PIPELINE_TRANSACTION_ATTEMPTS = 8
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")


class PipelineTransactionError(ValueError):
    """Raised when a transaction transition cannot preserve canonical truth."""


class PipelineTransactionState(str, Enum):
    NOOP_CLEAN = "noop_clean"
    BLOCKED = "blocked"
    PREPARED = "prepared"
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN_OWNERSHIP = "unknown_ownership"


_FORWARD_STATE_RANK = {
    PipelineTransactionState.PREPARED: 1,
    PipelineTransactionState.SUBMITTED: 2,
    PipelineTransactionState.RUNNING: 3,
    PipelineTransactionState.SUCCEEDED: 4,
    PipelineTransactionState.FAILED: 4,
    PipelineTransactionState.UNKNOWN_OWNERSHIP: 4,
}


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise PipelineTransactionError(f"{field}_identifier")
    return value


def _fingerprint(value: object, field: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise PipelineTransactionError(f"{field}_fingerprint")
    return value


def _optional_fingerprint(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _fingerprint(value, field)


@dataclass(frozen=True, slots=True)
class PipelineTransaction:
    transaction_id: str
    attempt: int
    state: PipelineTransactionState
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    manifest_fingerprints: tuple[str, ...]
    recompute_plan_fingerprint: str
    dirty_segment_ids: tuple[str, ...]
    graph_fingerprint: str | None = None
    compiled_prompt_fingerprint: str | None = None
    queue_prompt_id: str | None = None
    host_owner_id: str | None = None
    result_fingerprint: str | None = None
    cancellation_requested: bool = False
    transaction_fingerprint: str | None = None
    schema: str = PIPELINE_TRANSACTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PIPELINE_TRANSACTION_SCHEMA:
            raise PipelineTransactionError("unsupported_transaction_schema")
        _identifier(self.transaction_id, "transaction_id")
        if type(self.attempt) is not int or not (
            1 <= self.attempt <= MAX_PIPELINE_TRANSACTION_ATTEMPTS
        ):
            raise PipelineTransactionError("transaction_attempt")
        if type(self.state) is not PipelineTransactionState:
            raise PipelineTransactionError("transaction_state")
        _identifier(self.workspace_id, "workspace_id")
        if type(self.workspace_revision) is not int or self.workspace_revision <= 0:
            raise PipelineTransactionError("workspace_revision")
        _fingerprint(self.workspace_fingerprint, "workspace")
        if (
            type(self.manifest_fingerprints) is not tuple
            or not self.manifest_fingerprints
            or len(self.manifest_fingerprints) > MAX_WORKSPACE_SEGMENTS
        ):
            raise PipelineTransactionError("manifest_fingerprints")
        for value in self.manifest_fingerprints:
            _fingerprint(value, "manifest")
        _fingerprint(self.recompute_plan_fingerprint, "recompute_plan")
        if (
            type(self.dirty_segment_ids) is not tuple
            or len(self.dirty_segment_ids) > MAX_WORKSPACE_SEGMENTS
            or len(set(self.dirty_segment_ids)) != len(self.dirty_segment_ids)
        ):
            raise PipelineTransactionError("dirty_segment_ids")
        for value in self.dirty_segment_ids:
            _identifier(value, "dirty_segment_id")
        _optional_fingerprint(self.graph_fingerprint, "graph")
        _optional_fingerprint(self.compiled_prompt_fingerprint, "compiled_prompt")
        _optional_fingerprint(self.result_fingerprint, "result")
        if self.queue_prompt_id is not None:
            _identifier(self.queue_prompt_id, "queue_prompt_id")
        if self.host_owner_id is not None:
            _identifier(self.host_owner_id, "host_owner_id")
        if type(self.cancellation_requested) is not bool:
            raise PipelineTransactionError("cancellation_requested")
        self._validate_state_shape()
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.transaction_fingerprint is None:
            object.__setattr__(self, "transaction_fingerprint", expected)
        elif self.transaction_fingerprint != expected:
            raise PipelineTransactionError("transaction_fingerprint_mismatch")

    def _validate_state_shape(self) -> None:
        has_pipeline = (
            self.graph_fingerprint is not None and self.compiled_prompt_fingerprint is not None
        )
        if self.state in {
            PipelineTransactionState.NOOP_CLEAN,
            PipelineTransactionState.BLOCKED,
        }:
            if any(
                value is not None
                for value in (
                    self.graph_fingerprint,
                    self.compiled_prompt_fingerprint,
                    self.queue_prompt_id,
                    self.host_owner_id,
                    self.result_fingerprint,
                )
            ):
                raise PipelineTransactionError("non_pipeline_state_receipts")
            if self.state is PipelineTransactionState.NOOP_CLEAN and self.dirty_segment_ids:
                raise PipelineTransactionError("clean_state_dirty_segments")
        elif self.state in {
            PipelineTransactionState.PREPARED,
            PipelineTransactionState.CANCELLED,
        }:
            if not has_pipeline or any(
                value is not None
                for value in (
                    self.queue_prompt_id,
                    self.host_owner_id,
                    self.result_fingerprint,
                )
            ):
                raise PipelineTransactionError("prepared_state_receipts")
        elif not has_pipeline or self.queue_prompt_id is None:
            raise PipelineTransactionError("submitted_state_receipts")
        if self.state is PipelineTransactionState.RUNNING and self.host_owner_id is None:
            raise PipelineTransactionError("running_owner_missing")
        if (
            self.state
            in {
                PipelineTransactionState.SUCCEEDED,
                PipelineTransactionState.FAILED,
            }
            and self.result_fingerprint is None
        ):
            raise PipelineTransactionError("terminal_result_missing")
        if self.cancellation_requested and self.state not in {
            PipelineTransactionState.SUBMITTED,
            PipelineTransactionState.RUNNING,
            PipelineTransactionState.SUCCEEDED,
            PipelineTransactionState.FAILED,
            PipelineTransactionState.UNKNOWN_OWNERSHIP,
        }:
            raise PipelineTransactionError("cancellation_state")

    @property
    def fingerprint(self) -> str:
        if self.transaction_fingerprint is None:  # pragma: no cover - initialized above
            raise PipelineTransactionError("transaction_fingerprint_uninitialized")
        return self.transaction_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "transaction_id": self.transaction_id,
            "attempt": self.attempt,
            "state": self.state.value,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "manifest_fingerprints": list(self.manifest_fingerprints),
            "recompute_plan_fingerprint": self.recompute_plan_fingerprint,
            "dirty_segment_ids": list(self.dirty_segment_ids),
            "graph_fingerprint": self.graph_fingerprint,
            "compiled_prompt_fingerprint": self.compiled_prompt_fingerprint,
            "queue_prompt_id": self.queue_prompt_id,
            "host_owner_id": self.host_owner_id,
            "result_fingerprint": self.result_fingerprint,
            "cancellation_requested": self.cancellation_requested,
        }

    def to_public_dict(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["transaction_fingerprint"] = self.fingerprint
        return value


def prepare_pipeline_transaction(
    *,
    workspace: MultiSegmentWorkspace,
    manifests: tuple[SegmentContextManifest, ...],
    recompute_plan: RecomputePlan,
    transaction_id: str,
    graph_fingerprint: str | None = None,
    compiled_prompt_fingerprint: str | None = None,
) -> PipelineTransaction:
    """Join accepted M17 authorities to existing pipeline receipt identities."""

    if type(workspace) is not MultiSegmentWorkspace:
        raise PipelineTransactionError("workspace_type")
    if type(manifests) is not tuple or not all(
        type(item) is SegmentContextManifest for item in manifests
    ):
        raise PipelineTransactionError("manifest_type")
    expected_manifests = derive_segment_manifests(workspace)
    if tuple(item.fingerprint for item in manifests) != tuple(
        item.fingerprint for item in expected_manifests
    ):
        raise PipelineTransactionError("manifest_authority_mismatch")
    if type(recompute_plan) is not RecomputePlan:
        raise PipelineTransactionError("recompute_plan_type")
    manifest_ids = tuple(item.segment_id for item in manifests)
    if tuple(item.segment_id for item in recompute_plan.decisions) != manifest_ids:
        raise PipelineTransactionError("recompute_manifest_mismatch")
    _identifier(transaction_id, "transaction_id")

    blocked = recompute_plan.requires_full_recompute or any(
        item.disposition
        in {
            RecomputeDisposition.BLOCKED_MISSING_PREDECESSOR,
            RecomputeDisposition.REQUIRES_FULL_RECOMPUTE,
        }
        for item in recompute_plan.decisions
    )
    dirty_ids = recompute_plan.mandatory_segment_ids
    if not dirty_ids:
        if graph_fingerprint is not None or compiled_prompt_fingerprint is not None:
            raise PipelineTransactionError("clean_receipts_forbidden")
        state = PipelineTransactionState.NOOP_CLEAN
    elif blocked or not recompute_plan.selection_safe:
        if graph_fingerprint is not None or compiled_prompt_fingerprint is not None:
            raise PipelineTransactionError("blocked_receipts_forbidden")
        state = PipelineTransactionState.BLOCKED
    else:
        _fingerprint(graph_fingerprint, "graph")
        _fingerprint(compiled_prompt_fingerprint, "compiled_prompt")
        state = PipelineTransactionState.PREPARED

    return PipelineTransaction(
        transaction_id=transaction_id,
        attempt=1,
        state=state,
        workspace_id=workspace.workspace_id,
        workspace_revision=workspace.revision,
        workspace_fingerprint=workspace.fingerprint,
        manifest_fingerprints=tuple(item.fingerprint for item in manifests),
        recompute_plan_fingerprint=canonical_fingerprint(recompute_plan.to_public_dict()),
        dirty_segment_ids=dirty_ids,
        graph_fingerprint=graph_fingerprint,
        compiled_prompt_fingerprint=compiled_prompt_fingerprint,
    )


def record_pipeline_submission(
    transaction: PipelineTransaction,
    *,
    expected_transaction_fingerprint: str,
    queue_prompt_id: str,
) -> PipelineTransaction:
    _require_current(transaction, expected_transaction_fingerprint)
    if transaction.state is not PipelineTransactionState.PREPARED:
        if transaction.queue_prompt_id is not None:
            raise PipelineTransactionError("duplicate_submission")
        raise PipelineTransactionError("not_submittable")
    _identifier(queue_prompt_id, "queue_prompt_id")
    return replace(
        transaction,
        state=PipelineTransactionState.SUBMITTED,
        queue_prompt_id=queue_prompt_id,
        transaction_fingerprint=None,
    )


def record_pipeline_running(
    transaction: PipelineTransaction,
    *,
    expected_transaction_fingerprint: str,
    host_owner_id: str,
) -> PipelineTransaction:
    _require_current(transaction, expected_transaction_fingerprint)
    if transaction.state is not PipelineTransactionState.SUBMITTED:
        if transaction.state in {
            PipelineTransactionState.SUCCEEDED,
            PipelineTransactionState.FAILED,
            PipelineTransactionState.CANCELLED,
        }:
            raise PipelineTransactionError("terminal_transaction")
        raise PipelineTransactionError("running_transition")
    _identifier(host_owner_id, "host_owner_id")
    return replace(
        transaction,
        state=PipelineTransactionState.RUNNING,
        host_owner_id=host_owner_id,
        transaction_fingerprint=None,
    )


def record_pipeline_result(
    transaction: PipelineTransaction,
    *,
    expected_transaction_fingerprint: str,
    result_fingerprint: str,
    succeeded: bool,
) -> PipelineTransaction:
    _require_current(transaction, expected_transaction_fingerprint)
    if transaction.state not in {
        PipelineTransactionState.SUBMITTED,
        PipelineTransactionState.RUNNING,
        PipelineTransactionState.UNKNOWN_OWNERSHIP,
    }:
        raise PipelineTransactionError("result_transition")
    _fingerprint(result_fingerprint, "result")
    if type(succeeded) is not bool:
        raise PipelineTransactionError("result_disposition")
    return replace(
        transaction,
        state=(
            PipelineTransactionState.SUCCEEDED if succeeded else PipelineTransactionState.FAILED
        ),
        result_fingerprint=result_fingerprint,
        transaction_fingerprint=None,
    )


def cancel_pipeline_transaction(
    transaction: PipelineTransaction,
    *,
    expected_transaction_fingerprint: str,
) -> PipelineTransaction:
    _require_current(transaction, expected_transaction_fingerprint)
    if transaction.state is PipelineTransactionState.PREPARED:
        return replace(
            transaction,
            state=PipelineTransactionState.CANCELLED,
            transaction_fingerprint=None,
        )
    if transaction.state in {
        PipelineTransactionState.SUBMITTED,
        PipelineTransactionState.RUNNING,
        PipelineTransactionState.UNKNOWN_OWNERSHIP,
    }:
        if transaction.cancellation_requested:
            return transaction
        return replace(
            transaction,
            cancellation_requested=True,
            transaction_fingerprint=None,
        )
    raise PipelineTransactionError("cancel_transition")


def mark_pipeline_unknown_ownership(
    transaction: PipelineTransaction,
    *,
    expected_transaction_fingerprint: str,
) -> PipelineTransaction:
    _require_current(transaction, expected_transaction_fingerprint)
    if transaction.state not in {
        PipelineTransactionState.SUBMITTED,
        PipelineTransactionState.RUNNING,
    }:
        raise PipelineTransactionError("unknown_ownership_transition")
    return replace(
        transaction,
        state=PipelineTransactionState.UNKNOWN_OWNERSHIP,
        transaction_fingerprint=None,
    )


def retry_pipeline_transaction(
    transaction: PipelineTransaction,
    *,
    expected_transaction_fingerprint: str,
    transaction_id: str,
) -> PipelineTransaction:
    _require_current(transaction, expected_transaction_fingerprint)
    if transaction.state not in {
        PipelineTransactionState.CANCELLED,
        PipelineTransactionState.FAILED,
        PipelineTransactionState.UNKNOWN_OWNERSHIP,
    }:
        raise PipelineTransactionError("retry_transition")
    _identifier(transaction_id, "transaction_id")
    if transaction_id == transaction.transaction_id:
        raise PipelineTransactionError("retry_transaction_id")
    if transaction.attempt >= MAX_PIPELINE_TRANSACTION_ATTEMPTS:
        raise PipelineTransactionError("retry_limit")
    return replace(
        transaction,
        transaction_id=transaction_id,
        attempt=transaction.attempt + 1,
        state=PipelineTransactionState.PREPARED,
        queue_prompt_id=None,
        host_owner_id=None,
        result_fingerprint=None,
        cancellation_requested=False,
        transaction_fingerprint=None,
    )


def reconcile_pipeline_transaction(
    current: PipelineTransaction,
    incoming: PipelineTransaction,
) -> PipelineTransaction:
    if type(current) is not PipelineTransaction or type(incoming) is not PipelineTransaction:
        raise PipelineTransactionError("transaction_type")
    _verify_transaction_integrity(current)
    _verify_transaction_integrity(incoming)
    if current.fingerprint == incoming.fingerprint:
        return current
    same_authority = (
        current.workspace_fingerprint != incoming.workspace_fingerprint
        or current.recompute_plan_fingerprint != incoming.recompute_plan_fingerprint
        or current.manifest_fingerprints != incoming.manifest_fingerprints
        or current.dirty_segment_ids != incoming.dirty_segment_ids
        or current.graph_fingerprint != incoming.graph_fingerprint
        or current.compiled_prompt_fingerprint != incoming.compiled_prompt_fingerprint
    )
    if same_authority:
        raise PipelineTransactionError("stale_remount")
    if incoming.attempt == current.attempt:
        if current.transaction_id != incoming.transaction_id:
            raise PipelineTransactionError("stale_remount")
        current_rank = _FORWARD_STATE_RANK.get(current.state)
        incoming_rank = _FORWARD_STATE_RANK.get(incoming.state)
        forward = (
            current_rank is not None and incoming_rank is not None and incoming_rank > current_rank
        )
        cancellation_update = (
            current.state is incoming.state
            and not current.cancellation_requested
            and incoming.cancellation_requested
        )
        if forward or cancellation_update:
            return incoming
        raise PipelineTransactionError("stale_remount")
    if incoming.attempt != current.attempt + 1 or current.state not in {
        PipelineTransactionState.CANCELLED,
        PipelineTransactionState.FAILED,
        PipelineTransactionState.UNKNOWN_OWNERSHIP,
    }:
        raise PipelineTransactionError("stale_remount")
    return incoming


def _require_current(
    transaction: PipelineTransaction, expected_transaction_fingerprint: str
) -> None:
    if type(transaction) is not PipelineTransaction:
        raise PipelineTransactionError("transaction_type")
    _verify_transaction_integrity(transaction)
    _fingerprint(expected_transaction_fingerprint, "expected_transaction")
    if transaction.fingerprint != expected_transaction_fingerprint:
        raise PipelineTransactionError("stale_transaction")


def _verify_transaction_integrity(transaction: PipelineTransaction) -> None:
    if transaction.fingerprint != canonical_fingerprint(transaction._wire_without_fingerprint()):
        raise PipelineTransactionError("tampered_transaction")
