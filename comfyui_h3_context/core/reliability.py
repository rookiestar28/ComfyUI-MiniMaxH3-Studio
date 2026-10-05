"""Pure-core progress, cancellation, and recovery contracts for long H3 stages.

This module contains only bounded state and injected execution seams. It never starts a provider,
touches media, persists a checkpoint, or mutates a host. Runtime workers remain responsible for
calling the scheduler context checkpoints and progress hook.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from typing import Protocol, cast, runtime_checkable

from .canonical import canonical_fingerprint
from .contracts import ValidationDiagnostic, ValidationSeverity
from .errors import (
    LocalAdapterCancelledError,
    ReliabilityError,
)
from .resource_scheduling import (
    ResourceCacheKey,
    ResourceCacheState,
    ResourceCancellationToken,
    ResourceExecutionArtifact,
    ResourceExecutionReceipt,
    ResourceExecutionRequest,
    ResourceExecutionResult,
    ResourceExecutionStatus,
    ResourceScheduler,
    ResourceWorker,
)

RELIABILITY_RUNTIME_SCHEMA = "h3.reliability.runtime.v1"
MAX_PROGRESS_EVENTS = 256
MAX_PROGRESS_MESSAGE_LENGTH = 256
MAX_RECOVERY_ATTEMPTS = 8

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REVISION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SENSITIVE_MARKERS = (
    "api_key",
    "authorization",
    "bearer ",
    "cookie",
    "credential",
    "password",
    "private",
    "secret",
    "signed",
    "token=",
    "url",
    "path",
)


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ReliabilityError(f"{field_name} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise ReliabilityError(f"{field_name} contains sensitive metadata")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise ReliabilityError(f"{field_name} must be a lowercase SHA-256 fingerprint")
    return value


def _revision(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _REVISION_PATTERN.fullmatch(value) is None:
        raise ReliabilityError(f"{field_name} must be a bounded host/revision identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise ReliabilityError(f"{field_name} contains sensitive metadata")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReliabilityError(f"{field_name} must be a non-negative integer")
    return value


def _positive_int(value: object, field_name: str) -> int:
    result = _non_negative_int(value, field_name)
    if result <= 0:
        raise ReliabilityError(f"{field_name} must be positive")
    return result


def _safe_message(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_PROGRESS_MESSAGE_LENGTH:
        raise ReliabilityError("progress message must be bounded when supplied")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ReliabilityError("progress message contains a control character")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ReliabilityError("progress message contains an unsafe surrogate")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise ReliabilityError("progress message contains sensitive metadata")
    return value


def _finite_float(value: object, field_name: str, *, allow_zero: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReliabilityError(f"{field_name} must be a finite number")
    result = float(value)
    if not isfinite(result) or (result < 0 if allow_zero else result <= 0):
        raise ReliabilityError(f"{field_name} must be a finite bounded number")
    return result


class ReliabilityStage(str, Enum):
    """Long-running H3 stages that can publish progress and accept cancellation."""

    EXTRACTION = "extraction"
    PROVIDER = "provider"
    PLANNING = "planning"
    RENDERING = "rendering"
    VALIDATION = "validation"
    HOST = "host"


class ReliabilityRunState(str, Enum):
    """Observable non-secret run states; no state implies successful output."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    RETRYABLE_FAILURE = "retryable_failure"
    FAILED = "failed"
    STALE = "stale"
    RECOVERED = "recovered"


class CheckpointState(str, Enum):
    """Only incomplete states may be represented as resumable checkpoints."""

    PARTIAL = "partial"
    STALE = "stale"


class CheckpointCompatibility(str, Enum):
    """Result of comparing a checkpoint with the current execution identity."""

    COMPATIBLE = "compatible"
    STALE = "stale"
    INCOMPATIBLE = "incompatible"


class RecoveryAction(str, Enum):
    """Explicit caller-visible recovery decision."""

    START = "start"
    RETRY = "retry"
    RESUME = "resume"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    REJECT_STALE = "reject_stale"
    REJECT_INCOMPATIBLE = "reject_incompatible"
    TERMINAL_FAILURE = "terminal_failure"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One finite, monotonic progress observation with no raw runtime values."""

    operation_id: str
    stage: ReliabilityStage | str
    sequence: int
    completed_units: int
    total_units: int
    state: ReliabilityRunState | str = ReliabilityRunState.RUNNING
    message: str | None = None
    elapsed_seconds: float = 0.0
    schema: str = RELIABILITY_RUNTIME_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "operation_id")
        try:
            stage = (
                self.stage
                if isinstance(self.stage, ReliabilityStage)
                else ReliabilityStage(self.stage)
            )
            state = (
                self.state
                if isinstance(self.state, ReliabilityRunState)
                else ReliabilityRunState(self.state)
            )
        except (TypeError, ValueError):
            raise ReliabilityError("progress stage/state is unsupported") from None
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "state", state)
        _non_negative_int(self.sequence, "progress sequence")
        completed = _non_negative_int(self.completed_units, "completed_units")
        total = _positive_int(self.total_units, "total_units")
        if completed > total:
            raise ReliabilityError("completed_units cannot exceed total_units")
        if state is ReliabilityRunState.COMPLETED and completed != total:
            raise ReliabilityError("completed progress must equal total_units")
        _safe_message(self.message)
        elapsed = _finite_float(self.elapsed_seconds, "elapsed_seconds")
        object.__setattr__(self, "elapsed_seconds", elapsed)
        if self.schema != RELIABILITY_RUNTIME_SCHEMA:
            raise ReliabilityError("unsupported reliability schema")

    @property
    def fraction(self) -> float:
        return self.completed_units / self.total_units

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "operation_id": self.operation_id,
            "stage": cast(ReliabilityStage, self.stage).value,
            "sequence": self.sequence,
            "completed_units": self.completed_units,
            "total_units": self.total_units,
            "fraction": self.fraction,
            "state": cast(ReliabilityRunState, self.state).value,
            "message": self.message,
            "elapsed_seconds": self.elapsed_seconds,
        }


class ProgressLedger:
    """Thread-safe bounded ledger enforcing stage order and per-stage monotonic progress."""

    def __init__(
        self,
        operation_id: str,
        *,
        max_events: int = MAX_PROGRESS_EVENTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.operation_id = _identifier(operation_id, "operation_id")
        self.max_events = min(_positive_int(max_events, "max_events"), MAX_PROGRESS_EVENTS)
        if not callable(clock):
            raise ReliabilityError("progress clock must be callable")
        self._clock = clock
        self._events: list[ProgressEvent] = []
        self._last_stage_index = -1
        self._last_completed = 0
        self._last_total = 0
        self._lock = threading.RLock()

    def publish(
        self,
        stage: object,
        completed_units: int,
        total_units: int,
        state: object = ReliabilityRunState.RUNNING,
        message: str | None = None,
    ) -> ProgressEvent:
        try:
            selected_stage = (
                stage if isinstance(stage, ReliabilityStage) else ReliabilityStage(stage)
            )
            selected_state = (
                state if isinstance(state, ReliabilityRunState) else ReliabilityRunState(state)
            )
        except (TypeError, ValueError):
            raise ReliabilityError("progress stage/state is unsupported") from None
        with self._lock:
            if len(self._events) >= self.max_events:
                raise ReliabilityError("progress event limit exceeded")
            stage_index = list(ReliabilityStage).index(selected_stage)
            completed = _non_negative_int(completed_units, "completed_units")
            total = _positive_int(total_units, "total_units")
            if stage_index < self._last_stage_index:
                raise ReliabilityError("progress stage order cannot move backwards")
            if stage_index == self._last_stage_index:
                if total != self._last_total or completed < self._last_completed:
                    raise ReliabilityError("progress must be monotonic within a stage")
            event = ProgressEvent(
                self.operation_id,
                selected_stage,
                len(self._events),
                completed,
                total,
                selected_state,
                message,
                max(0.0, _finite_float(self._clock(), "progress clock")),
            )
            self._events.append(event)
            self._last_stage_index = stage_index
            self._last_completed = completed
            self._last_total = total
            return event

    @property
    def events(self) -> tuple[ProgressEvent, ...]:
        with self._lock:
            return tuple(self._events)

    @property
    def latest(self) -> ProgressEvent:
        with self._lock:
            if not self._events:
                raise ReliabilityError("progress ledger has no events")
            return self._events[-1]

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": RELIABILITY_RUNTIME_SCHEMA,
            "operation_id": self.operation_id,
            "events": [event.to_wire() for event in self.events],
        }


class CancellationScope:
    """One cancellation signal shared by extraction, provider, and planning stage probes."""

    def __init__(self) -> None:
        self._token = ResourceCancellationToken()

    def cancel(self, reason: str | None = None) -> None:
        self._token.cancel(reason)

    def is_cancelled(self) -> bool:
        return self._token.is_cancelled()

    @property
    def reason(self) -> str | None:
        return self._token.reason

    def checkpoint(self, stage: ReliabilityStage | str) -> None:
        if self.is_cancelled():
            try:
                selected = stage if isinstance(stage, ReliabilityStage) else ReliabilityStage(stage)
            except (TypeError, ValueError):
                selected = ReliabilityStage.HOST
            raise LocalAdapterCancelledError(f"{selected.value} stage was cancelled")

    def probe(self, stage: ReliabilityStage | str) -> StageCancellationProbe:
        try:
            selected = stage if isinstance(stage, ReliabilityStage) else ReliabilityStage(stage)
        except (TypeError, ValueError):
            raise ReliabilityError("cancellation stage is unsupported") from None
        return StageCancellationProbe(self, selected)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": RELIABILITY_RUNTIME_SCHEMA,
            "cancelled": self.is_cancelled(),
            "reason_present": self.reason is not None,
        }


@dataclass(frozen=True, slots=True)
class StageCancellationProbe:
    """Stage-labelled view over one shared cancellation scope."""

    scope: CancellationScope
    stage: ReliabilityStage

    def is_cancelled(self) -> bool:
        return self.scope.is_cancelled()

    def checkpoint(self) -> None:
        self.scope.checkpoint(self.stage)


@dataclass(frozen=True, slots=True)
class RecoveryCheckpoint:
    """Redacted incomplete state that may be offered to an explicit resume call."""

    operation_id: str
    cache_key: ResourceCacheKey
    workflow_fingerprint: str
    host_revision: str
    stage: ReliabilityStage | str
    completed_units: int
    total_units: int
    attempt: int
    state: CheckpointState | str
    schema: str = RELIABILITY_RUNTIME_SCHEMA
    checkpoint_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "checkpoint operation_id")
        if not isinstance(self.cache_key, ResourceCacheKey):
            raise ReliabilityError("checkpoint cache_key must be a ResourceCacheKey")
        if self.operation_id != self.cache_key.operation_id:
            raise ReliabilityError("checkpoint operation_id must match cache key")
        _fingerprint(self.workflow_fingerprint, "workflow_fingerprint")
        _revision(self.host_revision, "host_revision")
        try:
            stage = (
                self.stage
                if isinstance(self.stage, ReliabilityStage)
                else ReliabilityStage(self.stage)
            )
            state = (
                self.state
                if isinstance(self.state, CheckpointState)
                else CheckpointState(self.state)
            )
        except (TypeError, ValueError):
            raise ReliabilityError("checkpoint stage/state is unsupported") from None
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "state", state)
        completed = _non_negative_int(self.completed_units, "checkpoint completed_units")
        total = _positive_int(self.total_units, "checkpoint total_units")
        if completed > total:
            raise ReliabilityError("checkpoint completed_units cannot exceed total_units")
        _positive_int(self.attempt, "checkpoint attempt")
        if self.schema != RELIABILITY_RUNTIME_SCHEMA:
            raise ReliabilityError("unsupported reliability schema")
        projection = self._identity_wire()
        object.__setattr__(self, "checkpoint_fingerprint", canonical_fingerprint(projection))

    def _identity_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "operation_id": self.operation_id,
            "cache_key": self.cache_key.to_wire(),
            "workflow_fingerprint": self.workflow_fingerprint,
            "host_revision": self.host_revision,
            "stage": cast(ReliabilityStage, self.stage).value,
            "completed_units": self.completed_units,
            "total_units": self.total_units,
            "attempt": self.attempt,
            "state": cast(CheckpointState, self.state).value,
        }

    def to_wire(self) -> dict[str, object]:
        result = self._identity_wire()
        result["checkpoint_fingerprint"] = self.checkpoint_fingerprint
        return result


@dataclass(frozen=True, slots=True)
class CheckpointAssessment:
    """Compatibility result with redacted actionable diagnostics."""

    status: CheckpointCompatibility
    diagnostics: tuple[ValidationDiagnostic, ...] = ()
    schema: str = RELIABILITY_RUNTIME_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.status, CheckpointCompatibility):
            raise ReliabilityError("checkpoint assessment status is invalid")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(item, ValidationDiagnostic) for item in self.diagnostics
        ):
            raise ReliabilityError("checkpoint assessment diagnostics are invalid")
        if self.schema != RELIABILITY_RUNTIME_SCHEMA:
            raise ReliabilityError("unsupported reliability schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": self.status.value,
            "diagnostics": [item.to_wire() for item in self.diagnostics],
        }


def _diagnostic(
    code: str, message: str, severity: ValidationSeverity = ValidationSeverity.FATAL
) -> ValidationDiagnostic:
    return ValidationDiagnostic(severity, code, message, "recovery")


def assess_checkpoint(
    checkpoint: RecoveryCheckpoint,
    *,
    cache_key: ResourceCacheKey,
    workflow_fingerprint: str,
    host_revision: str,
) -> CheckpointAssessment:
    """Reject host-reloaded, source-drifted, or otherwise incompatible checkpoints before work."""

    if not isinstance(checkpoint, RecoveryCheckpoint):
        raise ReliabilityError("checkpoint must be a RecoveryCheckpoint")
    if not isinstance(cache_key, ResourceCacheKey):
        raise ReliabilityError("cache_key must be a ResourceCacheKey")
    _fingerprint(workflow_fingerprint, "workflow_fingerprint")
    _revision(host_revision, "host_revision")
    if checkpoint.state is CheckpointState.STALE:
        return CheckpointAssessment(
            CheckpointCompatibility.STALE,
            (_diagnostic("recovery.checkpoint_stale", "checkpoint is explicitly stale"),),
        )
    if checkpoint.host_revision != host_revision:
        return CheckpointAssessment(
            CheckpointCompatibility.STALE,
            (_diagnostic("recovery.host_revision_mismatch", "host revision changed"),),
        )
    if checkpoint.workflow_fingerprint != workflow_fingerprint:
        return CheckpointAssessment(
            CheckpointCompatibility.INCOMPATIBLE,
            (_diagnostic("recovery.workflow_mismatch", "workflow fingerprint changed"),),
        )
    if checkpoint.cache_key.cache_key != cache_key.cache_key:
        return CheckpointAssessment(
            CheckpointCompatibility.INCOMPATIBLE,
            (
                _diagnostic(
                    "recovery.cache_identity_mismatch", "operation or source identity changed"
                ),
            ),
        )
    return CheckpointAssessment(CheckpointCompatibility.COMPATIBLE)


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    """Finite caller policy; statuses outside the set are terminal and never retried."""

    max_attempts: int = 2
    retryable_statuses: frozenset[ResourceExecutionStatus] = frozenset(
        {ResourceExecutionStatus.TIMED_OUT, ResourceExecutionStatus.FAILED}
    )
    resume_partial: bool = True
    schema: str = RELIABILITY_RUNTIME_SCHEMA

    def __post_init__(self) -> None:
        attempts = _positive_int(self.max_attempts, "max_attempts")
        if attempts > MAX_RECOVERY_ATTEMPTS:
            raise ReliabilityError("max_attempts exceeds the recovery bound")
        if not isinstance(self.retryable_statuses, frozenset) or not all(
            isinstance(item, ResourceExecutionStatus) for item in self.retryable_statuses
        ):
            raise ReliabilityError("retryable_statuses must be ResourceExecutionStatus values")
        if not isinstance(self.resume_partial, bool):
            raise ReliabilityError("resume_partial must be a boolean")
        if self.schema != RELIABILITY_RUNTIME_SCHEMA:
            raise ReliabilityError("unsupported reliability schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "max_attempts": self.max_attempts,
            "retryable_statuses": sorted(item.value for item in self.retryable_statuses),
            "resume_partial": self.resume_partial,
        }


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """Explicit decision that cannot itself execute a retry or fallback."""

    action: RecoveryAction
    attempt: int
    reason: str
    compatibility: CheckpointCompatibility | None = None
    next_attempt: int | None = None
    diagnostics: tuple[ValidationDiagnostic, ...] = ()
    checkpoint_fingerprint: str | None = None
    schema: str = RELIABILITY_RUNTIME_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.action, RecoveryAction):
            raise ReliabilityError("recovery action is invalid")
        _positive_int(self.attempt, "recovery attempt")
        _safe_message(self.reason)
        if self.next_attempt is not None:
            _positive_int(self.next_attempt, "next_attempt")
        if self.compatibility is not None and not isinstance(
            self.compatibility, CheckpointCompatibility
        ):
            raise ReliabilityError("recovery compatibility is invalid")
        if self.checkpoint_fingerprint is not None:
            _fingerprint(self.checkpoint_fingerprint, "checkpoint_fingerprint")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(item, ValidationDiagnostic) for item in self.diagnostics
        ):
            raise ReliabilityError("recovery diagnostics are invalid")
        if self.schema != RELIABILITY_RUNTIME_SCHEMA:
            raise ReliabilityError("unsupported reliability schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "action": self.action.value,
            "attempt": self.attempt,
            "reason": self.reason,
            "compatibility": None if self.compatibility is None else self.compatibility.value,
            "next_attempt": self.next_attempt,
            "diagnostics": [item.to_wire() for item in self.diagnostics],
            "checkpoint_fingerprint": self.checkpoint_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ReliabilityStatus:
    """Node-friendly projection joining latest progress and recovery decision."""

    operation_id: str
    progress: ProgressEvent
    decision: RecoveryDecision
    execution_allowed: bool
    cancel_requested: bool
    schema: str = RELIABILITY_RUNTIME_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "status operation_id")
        if self.progress.operation_id != self.operation_id:
            raise ReliabilityError("status progress operation_id mismatch")
        if not isinstance(self.decision, RecoveryDecision):
            raise ReliabilityError("status decision is invalid")
        if not isinstance(self.execution_allowed, bool) or not isinstance(
            self.cancel_requested, bool
        ):
            raise ReliabilityError("status booleans are invalid")
        if self.schema != RELIABILITY_RUNTIME_SCHEMA:
            raise ReliabilityError("unsupported reliability schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "operation_id": self.operation_id,
            "progress": self.progress.to_wire(),
            "decision": self.decision.to_wire(),
            "execution_allowed": self.execution_allowed,
            "cancel_requested": self.cancel_requested,
        }


def build_reliability_status(
    operation_id: str,
    *,
    stage: ReliabilityStage | str,
    completed_units: int,
    total_units: int,
    state: ReliabilityRunState | str,
    cancel_requested: bool,
    attempt: int,
    max_attempts: int,
    checkpoint_status: CheckpointCompatibility | str | None = None,
    resume_requested: bool = False,
) -> ReliabilityStatus:
    """Build a deterministic status projection for a node or an orchestration UI."""

    _identifier(operation_id, "operation_id")
    if not isinstance(cancel_requested, bool) or not isinstance(resume_requested, bool):
        raise ReliabilityError("reliability cancellation/resume controls must be booleans")
    _positive_int(attempt, "attempt")
    max_attempts_value = _positive_int(max_attempts, "max_attempts")
    if max_attempts_value > MAX_RECOVERY_ATTEMPTS or attempt > max_attempts_value:
        raise ReliabilityError("attempt must be within the bounded max_attempts")
    try:
        selected_state = (
            state if isinstance(state, ReliabilityRunState) else ReliabilityRunState(state)
        )
        selected_stage = stage if isinstance(stage, ReliabilityStage) else ReliabilityStage(stage)
        selected_checkpoint = (
            None
            if checkpoint_status is None or checkpoint_status == "none"
            else checkpoint_status
            if isinstance(checkpoint_status, CheckpointCompatibility)
            else CheckpointCompatibility(checkpoint_status)
        )
    except (TypeError, ValueError):
        raise ReliabilityError("reliability state/stage/checkpoint status is unsupported") from None
    progress = ProgressEvent(
        operation_id,
        selected_stage,
        0,
        completed_units,
        total_units,
        selected_state,
    )
    diagnostics: tuple[ValidationDiagnostic, ...] = ()
    if selected_checkpoint is CheckpointCompatibility.STALE:
        diagnostics = (
            _diagnostic("recovery.checkpoint_stale", "stale checkpoint requires explicit restart"),
        )
    elif selected_checkpoint is CheckpointCompatibility.INCOMPATIBLE:
        diagnostics = (
            _diagnostic(
                "recovery.checkpoint_incompatible",
                "incompatible checkpoint cannot be resumed",
            ),
        )
    if cancel_requested:
        action = RecoveryAction.CANCELLED
        reason = "cancellation requested; no successful artifact is allowed"
        execution_allowed = False
    elif selected_checkpoint is CheckpointCompatibility.STALE:
        action = RecoveryAction.REJECT_STALE
        reason = "stale checkpoint requires explicit restart"
        execution_allowed = False
    elif selected_checkpoint is CheckpointCompatibility.INCOMPATIBLE:
        action = RecoveryAction.REJECT_INCOMPATIBLE
        reason = "incompatible checkpoint cannot be resumed"
        execution_allowed = False
    elif selected_state is ReliabilityRunState.RETRYABLE_FAILURE:
        if attempt < max_attempts_value:
            action = RecoveryAction.RETRY
            reason = "declared retryable failure may be retried by the caller"
            execution_allowed = True
        else:
            action = RecoveryAction.TERMINAL_FAILURE
            reason = "retry attempt limit exhausted"
            execution_allowed = False
    elif resume_requested and selected_checkpoint is CheckpointCompatibility.COMPATIBLE:
        action = RecoveryAction.RESUME
        reason = "compatible checkpoint may be resumed by the caller"
        execution_allowed = True
    elif selected_state is ReliabilityRunState.COMPLETED:
        action = RecoveryAction.COMPLETE
        reason = "execution completed; no retry is needed"
        execution_allowed = False
    elif selected_state in {
        ReliabilityRunState.CANCELLED,
        ReliabilityRunState.FAILED,
        ReliabilityRunState.STALE,
    }:
        action = RecoveryAction.TERMINAL_FAILURE
        reason = "terminal state requires explicit caller handling"
        execution_allowed = False
    else:
        action = RecoveryAction.START
        reason = "execution may start under the declared bounded policy"
        execution_allowed = True
    decision = RecoveryDecision(
        action,
        attempt,
        reason,
        selected_checkpoint,
        next_attempt=attempt + 1 if action is RecoveryAction.RETRY else None,
        diagnostics=diagnostics,
    )
    return ReliabilityStatus(
        operation_id,
        progress,
        decision,
        execution_allowed,
        cancel_requested,
    )


@dataclass(frozen=True, slots=True)
class RecoveryExecutionResult:
    """Runtime result plus redacted recovery/progress evidence."""

    result: ResourceExecutionResult
    decision: RecoveryDecision
    progress: tuple[ProgressEvent, ...]
    checkpoint: RecoveryCheckpoint | None
    attempts: int
    schema: str = RELIABILITY_RUNTIME_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.result, ResourceExecutionResult):
            raise ReliabilityError("recovery result must contain a ResourceExecutionResult")
        if not isinstance(self.decision, RecoveryDecision):
            raise ReliabilityError("recovery result decision is invalid")
        if not isinstance(self.progress, tuple) or not all(
            isinstance(item, ProgressEvent) for item in self.progress
        ):
            raise ReliabilityError("recovery progress is invalid")
        if self.checkpoint is not None and not isinstance(self.checkpoint, RecoveryCheckpoint):
            raise ReliabilityError("recovery checkpoint is invalid")
        _positive_int(self.attempts, "recovery attempts")
        if self.schema != RELIABILITY_RUNTIME_SCHEMA:
            raise ReliabilityError("unsupported reliability schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "result": self.result.to_wire(),
            "decision": self.decision.to_wire(),
            "progress": [item.to_wire() for item in self.progress],
            "checkpoint": None if self.checkpoint is None else self.checkpoint.to_wire(),
            "attempts": self.attempts,
        }


@runtime_checkable
class RecoveryWorkerFactory(Protocol):
    def __call__(self, checkpoint: RecoveryCheckpoint | None) -> ResourceWorker:
        """Return a worker for one explicit attempt/resume checkpoint."""


def _checkpoint_from_progress(
    request: ResourceExecutionRequest,
    ledger: ProgressLedger,
    *,
    workflow_fingerprint: str,
    host_revision: str,
    attempt: int,
    stale: bool,
) -> RecoveryCheckpoint | None:
    if not ledger.events:
        return None
    latest = ledger.latest
    return RecoveryCheckpoint(
        operation_id=request.operation_id,
        cache_key=request.cache_key,
        workflow_fingerprint=workflow_fingerprint,
        host_revision=host_revision,
        stage=latest.stage,
        completed_units=latest.completed_units,
        total_units=latest.total_units,
        attempt=attempt,
        state=CheckpointState.STALE if stale else CheckpointState.PARTIAL,
    )


def execute_with_recovery(
    scheduler: ResourceScheduler,
    request: ResourceExecutionRequest,
    worker_factory: RecoveryWorkerFactory,
    *,
    workflow_fingerprint: str,
    host_revision: str,
    policy: RecoveryPolicy | None = None,
    checkpoint: RecoveryCheckpoint | None = None,
    cancellation_scope: CancellationScope | None = None,
    progress: ProgressLedger | None = None,
) -> RecoveryExecutionResult:
    """Run bounded attempts, rejecting incompatible state before invoking a worker."""

    if policy is None:
        policy = RecoveryPolicy()

    if not isinstance(scheduler, ResourceScheduler):
        raise ReliabilityError("scheduler must be a ResourceScheduler")
    if not isinstance(request, ResourceExecutionRequest):
        raise ReliabilityError("request must be a ResourceExecutionRequest")
    if not isinstance(worker_factory, RecoveryWorkerFactory):
        raise ReliabilityError("worker_factory must implement RecoveryWorkerFactory")
    if not isinstance(policy, RecoveryPolicy):
        raise ReliabilityError("policy must be a RecoveryPolicy")
    _fingerprint(workflow_fingerprint, "workflow_fingerprint")
    _revision(host_revision, "host_revision")
    scope = CancellationScope() if cancellation_scope is None else cancellation_scope
    ledger = ProgressLedger(request.operation_id) if progress is None else progress
    if ledger.operation_id != request.operation_id:
        raise ReliabilityError("progress operation_id must match request")

    current_checkpoint = checkpoint
    if current_checkpoint is not None:
        assessment = assess_checkpoint(
            current_checkpoint,
            cache_key=request.cache_key,
            workflow_fingerprint=workflow_fingerprint,
            host_revision=host_revision,
        )
        if assessment.status is not CheckpointCompatibility.COMPATIBLE:
            action = (
                RecoveryAction.REJECT_STALE
                if assessment.status is CheckpointCompatibility.STALE
                else RecoveryAction.REJECT_INCOMPATIBLE
            )
            decision = RecoveryDecision(
                action,
                current_checkpoint.attempt,
                "checkpoint compatibility rejected before worker start",
                assessment.status,
                diagnostics=assessment.diagnostics,
                checkpoint_fingerprint=current_checkpoint.checkpoint_fingerprint,
            )
            terminal = ResourceExecutionResult(
                receipt=ResourceExecutionReceipt(
                    request.operation_id,
                    ResourceExecutionStatus.STALE,
                    ResourceCacheState.DISABLED,
                    0.0,
                    0,
                    0,
                    0,
                    True,
                    False,
                    False,
                ),
                artifact=None,
                diagnostics=assessment.diagnostics,
            )
            return RecoveryExecutionResult(
                terminal,
                decision,
                ledger.events,
                current_checkpoint,
                current_checkpoint.attempt,
            )

    if scope.is_cancelled():
        terminal = scheduler.execute(
            request,
            lambda _: ResourceExecutionArtifact(value=None),
            cancellation_probe=scope,
            progress=ledger,
        )
        decision = RecoveryDecision(
            RecoveryAction.CANCELLED,
            max(1, 1 if current_checkpoint is None else current_checkpoint.attempt),
            "cancellation was requested before worker start",
        )
        return RecoveryExecutionResult(terminal, decision, ledger.events, current_checkpoint, 1)

    first_attempt = 1 if current_checkpoint is None else current_checkpoint.attempt + 1
    attempts = 0
    last_result: ResourceExecutionResult | None = None
    while attempts < policy.max_attempts:
        attempt = first_attempt + attempts
        attempts += 1
        worker = worker_factory(current_checkpoint)
        if not isinstance(worker, ResourceWorker):
            raise ReliabilityError("worker_factory returned an invalid worker")
        last_result = scheduler.execute(
            request,
            worker,
            cancellation_probe=scope,
            progress=ledger,
        )
        if last_result.status is ResourceExecutionStatus.COMPLETE:
            decision = RecoveryDecision(RecoveryAction.COMPLETE, attempt, "execution completed")
            return RecoveryExecutionResult(last_result, decision, ledger.events, None, attempts)
        if last_result.status is ResourceExecutionStatus.CANCELLED:
            decision = RecoveryDecision(RecoveryAction.CANCELLED, attempt, "execution cancelled")
            return RecoveryExecutionResult(
                last_result, decision, ledger.events, current_checkpoint, attempts
            )
        if last_result.status in {
            ResourceExecutionStatus.MEMORY_EXCEEDED,
            ResourceExecutionStatus.BUDGET_EXCEEDED,
            ResourceExecutionStatus.CONCURRENCY_EXCEEDED,
        }:
            decision = RecoveryDecision(
                RecoveryAction.TERMINAL_FAILURE,
                attempt,
                "resource limit or concurrency failure is not retryable",
            )
            return RecoveryExecutionResult(
                last_result, decision, ledger.events, current_checkpoint, attempts
            )
        stale = last_result.status is ResourceExecutionStatus.STALE
        if last_result.status is ResourceExecutionStatus.PARTIAL and policy.resume_partial:
            current_checkpoint = _checkpoint_from_progress(
                request,
                ledger,
                workflow_fingerprint=workflow_fingerprint,
                host_revision=host_revision,
                attempt=attempt,
                stale=False,
            )
            if current_checkpoint is not None and attempts < policy.max_attempts:
                continue
        elif stale:
            current_checkpoint = _checkpoint_from_progress(
                request,
                ledger,
                workflow_fingerprint=workflow_fingerprint,
                host_revision=host_revision,
                attempt=attempt,
                stale=True,
            )
        if last_result.status in policy.retryable_statuses and attempts < policy.max_attempts:
            decision = RecoveryDecision(
                RecoveryAction.RETRY,
                attempt,
                "declared retryable status; caller-visible next attempt",
                next_attempt=attempt + 1,
                checkpoint_fingerprint=None
                if current_checkpoint is None
                else current_checkpoint.checkpoint_fingerprint,
            )
            continue
        action = RecoveryAction.TERMINAL_FAILURE
        reason = "execution reached a non-retryable terminal status"
        if stale:
            action = RecoveryAction.REJECT_STALE
            reason = "worker returned stale state; explicit resume is required"
        decision = RecoveryDecision(
            action,
            attempt,
            reason,
            checkpoint_fingerprint=None
            if current_checkpoint is None
            else current_checkpoint.checkpoint_fingerprint,
        )
        return RecoveryExecutionResult(
            last_result, decision, ledger.events, current_checkpoint, attempts
        )
    if last_result is None:
        raise ReliabilityError("recovery coordinator produced no attempt")
    decision = RecoveryDecision(
        RecoveryAction.TERMINAL_FAILURE,
        first_attempt + attempts - 1,
        "recovery attempt limit exhausted",
        checkpoint_fingerprint=None
        if current_checkpoint is None
        else current_checkpoint.checkpoint_fingerprint,
    )
    return RecoveryExecutionResult(
        last_result, decision, ledger.events, current_checkpoint, attempts
    )


__all__ = [
    "MAX_PROGRESS_EVENTS",
    "MAX_PROGRESS_MESSAGE_LENGTH",
    "MAX_RECOVERY_ATTEMPTS",
    "RELIABILITY_RUNTIME_SCHEMA",
    "CancellationScope",
    "CheckpointAssessment",
    "CheckpointCompatibility",
    "CheckpointState",
    "ProgressEvent",
    "ProgressLedger",
    "RecoveryAction",
    "RecoveryCheckpoint",
    "RecoveryDecision",
    "RecoveryExecutionResult",
    "RecoveryPolicy",
    "RecoveryWorkerFactory",
    "ReliabilityRunState",
    "ReliabilityStage",
    "ReliabilityStatus",
    "StageCancellationProbe",
    "assess_checkpoint",
    "build_reliability_status",
    "execute_with_recovery",
]
