"""Offline/mock-only official-oracle capture lifecycle.

This module is a deliberately narrow research seam. It accepts an injected transport that
declares itself offline, composes with the M9-04 execution gate, and retains only bounded hashes
and lifecycle metadata. It never opens a socket, resolves a credential, reads media, or returns
oracle output as evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from math import isfinite
from typing import Protocol, cast, runtime_checkable

from .canonical import canonical_fingerprint
from .context_reporting import ProviderOutcome
from .contracts import ProviderIdentity
from .errors import ContractValidationError
from .official_context_ir import (
    DEFAULT_OFFICIAL_CONTEXT_IR_LIFECYCLE_POLICY,
    OFFICIAL_CONTEXT_IR_ENDPOINT_REVISION,
    OFFICIAL_CONTEXT_IR_PROVIDER_VERSION,
    OfficialContextIRLifecyclePolicy,
    OfficialContextIRRequest,
    OfficialContextIRTaskStatus,
    OfficialContextIRTransportResponse,
)
from .official_oracle_governance import (
    OfficialOracleClaimCeiling,
    OfficialOracleExecutionGate,
    OfficialOracleExecutionMode,
    OfficialOracleMediaPrivacy,
    OfficialOracleOperation,
    OfficialOracleRetentionPolicy,
)

OFFICIAL_ORACLE_CAPTURE_SCHEMA = "h3.context.ir.capture.v1"
MAX_CAPTURE_LIST_ITEMS = 64
MAX_CAPTURE_CURSOR_LENGTH = 128
MAX_CAPTURE_TOKENS = 10**9
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f\\]")
_SENSITIVE_SEGMENTS = frozenset(
    {
        "api_key",
        "authorization",
        "credential",
        "password",
        "path",
        "secret",
        "signed",
        "token",
        "url",
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        "succeeded",
        "failed",
        "cancelled",
        "timeout",
    }
)


class OfficialOracleCaptureStatus(str, Enum):
    """Bounded capture lifecycle state independent of raw provider payloads."""

    SUBMITTED = "submitted"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


def _sensitive_identifier(value: str) -> bool:
    segments = tuple(part for part in re.split(r"[_.:-]+", value.casefold()) if part)
    return any(segment in _SENSITIVE_SEGMENTS for segment in segments)


def _require_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field} must be a bounded identifier")
    if _sensitive_identifier(value):
        raise ContractValidationError(f"{field} must not contain sensitive markers")
    return value


def _require_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _require_optional_hash(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _require_hash(value, field)


def _require_optional_tokens(value: object, field: str) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_CAPTURE_TOKENS
    ):
        raise ContractValidationError(f"{field} must be a bounded non-negative integer")
    return value


def _require_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ContractValidationError(f"{field} must be a bounded non-empty string")
    if _CONTROL_PATTERN.search(value) or any(
        marker in value.casefold()
        for marker in ("http://", "https://", "bearer ", "token=", "api_key", "password")
    ):
        raise ContractValidationError(f"{field} contains unsafe metadata")
    return value


def _fingerprint(value: object) -> str:
    try:
        return canonical_fingerprint(value).removeprefix("sha256:")
    except (TypeError, ValueError, UnicodeError):
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _clock_value(clock: Callable[[], float] | None) -> float:
    value = time.monotonic() if clock is None else clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise ContractValidationError("capture clock must return a finite number")
    return float(value)


@runtime_checkable
class OfficialOracleCaptureTransport(Protocol):
    """Injected lifecycle transport; implementations must be explicitly offline for this item."""

    def is_offline(self) -> bool:
        """Return true only for an offline/mock transport with no network side effect."""
        ...

    def create(self, payload: dict[str, object]) -> OfficialContextIRTransportResponse:
        """Create one mocked/recorded task."""
        ...

    def query(self, task_id: str) -> OfficialContextIRTransportResponse:
        """Query one bounded task handle."""
        ...

    def list(self, cursor: str | None = None) -> OfficialContextIRTransportResponse:
        """List bounded task metadata."""
        ...

    def cancel(self, task_id: str) -> OfficialContextIRTransportResponse:
        """Cancel one task handle."""
        ...


@dataclass(frozen=True, slots=True)
class OfficialOracleCaptureManifest:
    """Hash-only capture receipt suitable for reports and offline fixtures."""

    capture_id: str
    lane_id: str
    execution_mode: OfficialOracleExecutionMode
    status: OfficialOracleCaptureStatus
    outcome: ProviderOutcome
    error_code: str | None
    source_ledger_id: str
    source_ledger_fingerprint: str
    terms_revision: str
    policy_revision: str
    provider_version: str = OFFICIAL_CONTEXT_IR_PROVIDER_VERSION
    source_revision: str = OFFICIAL_CONTEXT_IR_ENDPOINT_REVISION
    provider: ProviderIdentity = ProviderIdentity.OFFICIAL_MINIMAX
    claim_ceiling: OfficialOracleClaimCeiling = OfficialOracleClaimCeiling.NO_CLAIM
    retention_policy: OfficialOracleRetentionPolicy = OfficialOracleRetentionPolicy.NONE
    task_id: str | None = None
    input_fingerprint: str | None = None
    output_fingerprint: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    redacted_message: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.capture_id, "capture_id")
        _require_identifier(self.lane_id, "lane_id")
        if not isinstance(self.execution_mode, OfficialOracleExecutionMode):
            raise ContractValidationError("capture execution_mode is invalid")
        if not isinstance(self.status, OfficialOracleCaptureStatus):
            raise ContractValidationError("capture status is invalid")
        if not isinstance(self.outcome, ProviderOutcome):
            raise ContractValidationError("capture outcome is invalid")
        if self.provider is not ProviderIdentity.OFFICIAL_MINIMAX:
            raise ContractValidationError("capture provider must be official_minimax")
        if self.error_code is not None:
            _require_identifier(self.error_code, "capture error_code")
        _require_identifier(self.source_ledger_id, "capture source_ledger_id")
        _require_hash(self.source_ledger_fingerprint, "capture source_ledger_fingerprint")
        _require_identifier(self.terms_revision, "capture terms_revision")
        _require_identifier(self.policy_revision, "capture policy_revision")
        _require_identifier(self.provider_version, "capture provider_version")
        _require_identifier(self.source_revision, "capture source_revision")
        if not isinstance(self.claim_ceiling, OfficialOracleClaimCeiling):
            raise ContractValidationError("capture claim ceiling is invalid")
        if not isinstance(self.retention_policy, OfficialOracleRetentionPolicy):
            raise ContractValidationError("capture retention policy is invalid")
        if self.task_id is not None:
            _require_identifier(self.task_id, "capture task_id")
        _require_optional_hash(self.input_fingerprint, "capture input_fingerprint")
        _require_optional_hash(self.output_fingerprint, "capture output_fingerprint")
        _require_optional_tokens(self.input_tokens, "capture input_tokens")
        _require_optional_tokens(self.output_tokens, "capture output_tokens")
        if self.redacted_message is not None:
            _require_text(self.redacted_message, "capture redacted_message")
        if self.status is OfficialOracleCaptureStatus.SUCCEEDED:
            if self.outcome is not ProviderOutcome.SUCCEEDED or self.output_fingerprint is None:
                raise ContractValidationError("successful capture must have an output fingerprint")
        elif self.status is OfficialOracleCaptureStatus.CANCELLED:
            if self.outcome is not ProviderOutcome.CANCELLED:
                raise ContractValidationError("cancelled capture outcome is inconsistent")
        elif self.status is OfficialOracleCaptureStatus.TIMEOUT:
            if self.outcome is not ProviderOutcome.TIMEOUT:
                raise ContractValidationError("timeout capture outcome is inconsistent")
        elif (
            self.status
            in {
                OfficialOracleCaptureStatus.SUBMITTED,
                OfficialOracleCaptureStatus.QUEUED,
                OfficialOracleCaptureStatus.RUNNING,
            }
            and self.outcome is not ProviderOutcome.NOT_REQUESTED
        ):
            raise ContractValidationError("non-terminal capture must not claim an outcome")

    @property
    def terminal(self) -> bool:
        return self.status.value in _TERMINAL_STATUSES

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": OFFICIAL_ORACLE_CAPTURE_SCHEMA,
            "capture_id": self.capture_id,
            "lane_id": self.lane_id,
            "execution_mode": self.execution_mode.value,
            "status": self.status.value,
            "outcome": self.outcome.value,
            "error_code": self.error_code,
            "provider": self.provider.value,
            "provider_version": self.provider_version,
            "source_revision": self.source_revision,
            "source_ledger_id": self.source_ledger_id,
            "source_ledger_fingerprint": self.source_ledger_fingerprint,
            "terms_revision": self.terms_revision,
            "policy_revision": self.policy_revision,
            "claim_ceiling": self.claim_ceiling.value,
            "retention_policy": self.retention_policy.value,
            "task_id": self.task_id,
            "input_fingerprint": self.input_fingerprint,
            "output_fingerprint": self.output_fingerprint,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "redacted_message": self.redacted_message,
        }


@dataclass(frozen=True, slots=True)
class OfficialOracleCaptureHandle:
    """Immutable task handle carried between capture lifecycle calls."""

    capture_id: str
    task_id: str
    lane_id: str
    execution_mode: OfficialOracleExecutionMode
    gate: OfficialOracleExecutionGate
    manifest: OfficialOracleCaptureManifest
    poll_count: int
    started_at: float
    lifecycle: OfficialContextIRLifecyclePolicy

    def __post_init__(self) -> None:
        _require_identifier(self.capture_id, "handle capture_id")
        _require_identifier(self.task_id, "handle task_id")
        _require_identifier(self.lane_id, "handle lane_id")
        if not isinstance(self.execution_mode, OfficialOracleExecutionMode):
            raise ContractValidationError("handle execution_mode is invalid")
        if not isinstance(self.gate, OfficialOracleExecutionGate):
            raise ContractValidationError("handle gate is invalid")
        if not isinstance(self.manifest, OfficialOracleCaptureManifest):
            raise ContractValidationError("handle manifest is invalid")
        if self.manifest.capture_id != self.capture_id or self.manifest.task_id != self.task_id:
            raise ContractValidationError("handle and manifest IDs must match")
        if (
            isinstance(self.poll_count, bool)
            or not isinstance(self.poll_count, int)
            or self.poll_count < 0
        ):
            raise ContractValidationError("handle poll_count must be non-negative")
        if not isfinite(float(self.started_at)) or self.started_at < 0:
            raise ContractValidationError("handle started_at must be finite and non-negative")
        if not isinstance(self.lifecycle, OfficialContextIRLifecyclePolicy):
            raise ContractValidationError("handle lifecycle is invalid")

    @property
    def terminal(self) -> bool:
        return self.manifest.terminal

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": OFFICIAL_ORACLE_CAPTURE_SCHEMA,
            "capture_id": self.capture_id,
            "task_id": self.task_id,
            "lane_id": self.lane_id,
            "execution_mode": self.execution_mode.value,
            "poll_count": self.poll_count,
            "terminal": self.terminal,
            "manifest": self.manifest.to_public_dict(),
        }


class OfficialOracleCaptureError(ContractValidationError):
    """Typed capture failure with redacted manifest and the next immutable gate state."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        manifest: OfficialOracleCaptureManifest | None = None,
        next_gate: OfficialOracleExecutionGate | None = None,
    ) -> None:
        self.code = _require_identifier(code, "capture error code")
        self.manifest = manifest
        self.next_gate = next_gate
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class OfficialOracleCaptureListItem:
    """Redacted task listing entry."""

    task_id: str
    status: OfficialOracleCaptureStatus
    output_fingerprint: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.task_id, "list task_id")
        if not isinstance(self.status, OfficialOracleCaptureStatus):
            raise ContractValidationError("list status is invalid")
        _require_optional_hash(self.output_fingerprint, "list output_fingerprint")
        if self.error_code is not None:
            _require_identifier(self.error_code, "list error_code")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "output_fingerprint": self.output_fingerprint,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class OfficialOracleCaptureList:
    """Bounded list response and the immutable gate state after the list operation."""

    items: tuple[OfficialOracleCaptureListItem, ...]
    next_cursor: str | None
    next_gate: OfficialOracleExecutionGate

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or len(self.items) > MAX_CAPTURE_LIST_ITEMS:
            raise ContractValidationError("capture list items are unbounded")
        if not all(isinstance(item, OfficialOracleCaptureListItem) for item in self.items):
            raise ContractValidationError("capture list contains an invalid item")
        if self.next_cursor is not None:
            _require_identifier(self.next_cursor, "capture list next_cursor")
        if not isinstance(self.next_gate, OfficialOracleExecutionGate):
            raise ContractValidationError("capture list next_gate is invalid")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": OFFICIAL_ORACLE_CAPTURE_SCHEMA,
            "items": [item.to_public_dict() for item in self.items],
            "next_cursor": self.next_cursor,
        }


def _status(value: object) -> OfficialOracleCaptureStatus:
    try:
        return OfficialOracleCaptureStatus(value)
    except ValueError:
        try:
            return OfficialOracleCaptureStatus(OfficialContextIRTaskStatus(value).value)
        except (TypeError, ValueError):
            raise OfficialOracleCaptureError(
                "malformed_response", "capture status is undocumented"
            ) from None


def _body_error_marker(body: Mapping[str, object]) -> str:
    error = body.get("error")
    if isinstance(error, Mapping) and isinstance(error.get("type"), str):
        return cast(str, error["type"]).casefold()
    return ""


def _http_error_code(response: OfficialContextIRTransportResponse) -> tuple[str, ProviderOutcome]:
    marker = _body_error_marker(response.body)
    if "unsupported" in marker or ("media" in marker and response.status_code in {400, 422}):
        return "unsupported_media", ProviderOutcome.UNSUPPORTED_MEDIA
    if "moder" in marker or "sensitive" in marker:
        return "moderated", ProviderOutcome.MODERATED
    return {
        400: ("bad_request", ProviderOutcome.FAILED),
        401: ("authentication", ProviderOutcome.AUTHENTICATION),
        402: ("quota", ProviderOutcome.QUOTA),
        422: ("moderated", ProviderOutcome.MODERATED),
        429: ("rate_limit", ProviderOutcome.RETRYABLE_TRANSPORT),
        500: ("server_error", ProviderOutcome.FAILED),
    }.get(response.status_code, ("transport", ProviderOutcome.RETRYABLE_TRANSPORT))


def _task_failure_code(task: Mapping[str, object]) -> tuple[str, ProviderOutcome]:
    marker = _body_error_marker(task)
    if "unsupported" in marker or "media" in marker:
        return "unsupported_media", ProviderOutcome.UNSUPPORTED_MEDIA
    if "moder" in marker or "sensitive" in marker:
        return "moderated", ProviderOutcome.MODERATED
    if "auth" in marker:
        return "authentication", ProviderOutcome.AUTHENTICATION
    if "quota" in marker or "balance" in marker:
        return "quota", ProviderOutcome.QUOTA
    return "failed", ProviderOutcome.FAILED


def _usage(body: Mapping[str, object]) -> tuple[int | None, int | None]:
    value = body.get("usage")
    if not isinstance(value, Mapping):
        return None, None
    return (
        _require_optional_tokens(value.get("input_tokens"), "input_tokens"),
        _require_optional_tokens(value.get("output_tokens"), "output_tokens"),
    )


def _task_body(response: OfficialContextIRTransportResponse, task_id: str) -> Mapping[str, object]:
    task = response.body.get("task")
    if not isinstance(task, Mapping):
        raise OfficialOracleCaptureError(
            "malformed_response", "capture response has no task object"
        )
    returned_id = task.get("id")
    if returned_id is not None and returned_id != task_id:
        raise OfficialOracleCaptureError("malformed_response", "capture task ID changed")
    return task


@dataclass(frozen=True, slots=True)
class OfficialOracleCaptureRunner:
    """Compose the M9-04 gate with an explicitly offline injected lifecycle transport."""

    gate: OfficialOracleExecutionGate
    lifecycle: OfficialContextIRLifecyclePolicy = DEFAULT_OFFICIAL_CONTEXT_IR_LIFECYCLE_POLICY

    def __post_init__(self) -> None:
        if not isinstance(self.gate, OfficialOracleExecutionGate):
            raise ContractValidationError("capture runner gate is invalid")
        if not isinstance(self.lifecycle, OfficialContextIRLifecyclePolicy):
            raise ContractValidationError("capture runner lifecycle is invalid")

    def with_gate(self, gate: OfficialOracleExecutionGate) -> OfficialOracleCaptureRunner:
        return replace(self, gate=gate)

    @staticmethod
    def _validate_transport(transport: OfficialOracleCaptureTransport) -> None:
        if not isinstance(transport, OfficialOracleCaptureTransport):
            raise ContractValidationError(
                "capture transport must implement the offline lifecycle protocol"
            )
        try:
            offline = transport.is_offline()
        except Exception:
            raise OfficialOracleCaptureError(
                "transport", "capture transport offline declaration failed"
            ) from None
        if offline is not True:
            raise OfficialOracleCaptureError(
                "offline_transport_required", "M9-05 requires an explicitly offline transport"
            )

    @staticmethod
    def _validate_mode(execution_mode: OfficialOracleExecutionMode) -> None:
        if not isinstance(execution_mode, OfficialOracleExecutionMode):
            raise ContractValidationError("capture execution mode is invalid")
        if execution_mode is OfficialOracleExecutionMode.LIVE:
            raise OfficialOracleCaptureError(
                "live_transport_unavailable",
                "M9-05 has no live transport under the current M9-04 disposition",
            )

    def _admit(
        self,
        gate: OfficialOracleExecutionGate,
        *,
        lane_id: str,
        execution_mode: OfficialOracleExecutionMode,
        operation_id: str,
        now: str,
        rate_clock: float,
        media_present: bool = False,
        media_class: OfficialOracleMediaPrivacy | None = None,
    ) -> OfficialOracleExecutionGate:
        admission = gate.admit(
            lane_id=lane_id,
            execution_mode=execution_mode,
            operation=OfficialOracleOperation.API_CALL,
            operation_id=operation_id,
            now=now,
            rate_clock=rate_clock,
            media_present=media_present,
            media_class=media_class,
        )
        return admission.next_gate

    @staticmethod
    def _base_manifest(
        gate: OfficialOracleExecutionGate,
        *,
        capture_id: str,
        lane_id: str,
        execution_mode: OfficialOracleExecutionMode,
        input_fingerprint: str | None = None,
        task_id: str | None = None,
    ) -> OfficialOracleCaptureManifest:
        disposition = gate.terms.disposition_for(lane_id)
        return OfficialOracleCaptureManifest(
            capture_id=capture_id,
            lane_id=lane_id,
            execution_mode=execution_mode,
            status=OfficialOracleCaptureStatus.SUBMITTED,
            outcome=ProviderOutcome.NOT_REQUESTED,
            error_code=None,
            source_ledger_id=gate.source_ledger.ledger_id,
            source_ledger_fingerprint=gate.source_ledger.fingerprint,
            terms_revision=gate.terms.terms_revision,
            policy_revision=gate.policy.policy_revision,
            claim_ceiling=disposition.claim_ceiling,
            retention_policy=disposition.retention_policy,
            task_id=task_id,
            input_fingerprint=input_fingerprint,
        )

    @staticmethod
    def _error_manifest(
        manifest: OfficialOracleCaptureManifest,
        *,
        code: str,
        outcome: ProviderOutcome,
        status: OfficialOracleCaptureStatus = OfficialOracleCaptureStatus.FAILED,
    ) -> OfficialOracleCaptureManifest:
        return replace(
            manifest,
            status=status,
            outcome=outcome,
            error_code=code,
            redacted_message=f"capture lifecycle {code}",
        )

    def submit(
        self,
        request: OfficialContextIRRequest,
        transport: OfficialOracleCaptureTransport,
        *,
        lane_id: str = "official_mocked_contract",
        execution_mode: OfficialOracleExecutionMode = OfficialOracleExecutionMode.MOCKED,
        operation_id: str = "capture.create",
        capture_id: str = "capture-1",
        now: str,
        rate_clock: float,
        media_class: OfficialOracleMediaPrivacy | None = None,
        clock: Callable[[], float] | None = None,
    ) -> OfficialOracleCaptureHandle:
        if not isinstance(request, OfficialContextIRRequest):
            raise ContractValidationError("capture request must be an OfficialContextIRRequest")
        _require_identifier(capture_id, "capture_id")
        self._validate_mode(execution_mode)
        self._validate_transport(transport)
        payload = request.to_payload()
        input_fingerprint = _fingerprint(payload)
        next_gate = self._admit(
            self.gate,
            lane_id=lane_id,
            execution_mode=execution_mode,
            operation_id=operation_id,
            now=now,
            rate_clock=rate_clock,
            media_present=bool(request.media),
            media_class=media_class,
        )
        manifest = self._base_manifest(
            self.gate,
            capture_id=capture_id,
            lane_id=lane_id,
            execution_mode=execution_mode,
            input_fingerprint=input_fingerprint,
        )
        try:
            response = transport.create(payload)
        except Exception:
            failed = self._error_manifest(
                manifest, code="transport", outcome=ProviderOutcome.RETRYABLE_TRANSPORT
            )
            raise OfficialOracleCaptureError(
                "transport", "capture create failed", manifest=failed, next_gate=next_gate
            ) from None
        if not 200 <= response.status_code < 300:
            code, outcome = _http_error_code(response)
            failed = self._error_manifest(manifest, code=code, outcome=outcome)
            raise OfficialOracleCaptureError(
                code,
                "capture create returned a terminal error",
                manifest=failed,
                next_gate=next_gate,
            )
        task_id = response.body.get("task_id")
        if not isinstance(task_id, str) or _IDENTIFIER_PATTERN.fullmatch(task_id) is None:
            failed = self._error_manifest(
                manifest, code="malformed_response", outcome=ProviderOutcome.FAILED
            )
            raise OfficialOracleCaptureError(
                "malformed_response",
                "capture create has no valid task ID",
                manifest=failed,
                next_gate=next_gate,
            )
        try:
            _require_identifier(task_id, "capture task_id")
        except ContractValidationError:
            failed = self._error_manifest(
                manifest, code="malformed_response", outcome=ProviderOutcome.FAILED
            )
            raise OfficialOracleCaptureError(
                "malformed_response",
                "capture create task ID is unsafe",
                manifest=failed,
                next_gate=next_gate,
            ) from None
        submitted = replace(manifest, task_id=task_id)
        return OfficialOracleCaptureHandle(
            capture_id=capture_id,
            task_id=task_id,
            lane_id=lane_id,
            execution_mode=execution_mode,
            gate=next_gate,
            manifest=submitted,
            poll_count=0,
            started_at=_clock_value(clock),
            lifecycle=self.lifecycle,
        )

    def poll(
        self,
        handle: OfficialOracleCaptureHandle,
        transport: OfficialOracleCaptureTransport,
        *,
        now: str,
        rate_clock: float,
        elapsed_seconds: float | None = None,
        clock: Callable[[], float] | None = None,
        operation_id: str = "capture.query",
    ) -> OfficialOracleCaptureHandle:
        if not isinstance(handle, OfficialOracleCaptureHandle):
            raise ContractValidationError("capture handle is invalid")
        if handle.terminal:
            raise OfficialOracleCaptureError(
                "already_terminal",
                "capture handle is terminal",
                manifest=handle.manifest,
                next_gate=handle.gate,
            )
        self._validate_mode(handle.execution_mode)
        self._validate_transport(transport)
        elapsed = (
            _clock_value(clock) - handle.started_at if elapsed_seconds is None else elapsed_seconds
        )
        if (
            isinstance(elapsed, bool)
            or not isinstance(elapsed, (int, float))
            or not isfinite(float(elapsed))
            or elapsed < 0
        ):
            raise ContractValidationError("elapsed_seconds must be finite and non-negative")
        if (
            float(elapsed) >= self.lifecycle.max_wall_time_seconds
            or handle.poll_count >= self.lifecycle.max_polls
        ):
            timeout_manifest = self._error_manifest(
                replace(
                    handle.manifest,
                    status=OfficialOracleCaptureStatus.TIMEOUT,
                    outcome=ProviderOutcome.TIMEOUT,
                    error_code="timeout",
                ),
                code="timeout",
                outcome=ProviderOutcome.TIMEOUT,
                status=OfficialOracleCaptureStatus.TIMEOUT,
            )
            raise OfficialOracleCaptureError(
                "timeout",
                "capture polling budget exhausted",
                manifest=timeout_manifest,
                next_gate=handle.gate,
            )
        next_gate = self._admit(
            handle.gate,
            lane_id=handle.lane_id,
            execution_mode=handle.execution_mode,
            operation_id=operation_id,
            now=now,
            rate_clock=rate_clock,
        )
        try:
            response = transport.query(handle.task_id)
        except Exception:
            failed = self._error_manifest(
                handle.manifest, code="transport", outcome=ProviderOutcome.RETRYABLE_TRANSPORT
            )
            raise OfficialOracleCaptureError(
                "transport", "capture query failed", manifest=failed, next_gate=next_gate
            ) from None
        if not 200 <= response.status_code < 300:
            code, outcome = _http_error_code(response)
            failed = self._error_manifest(handle.manifest, code=code, outcome=outcome)
            raise OfficialOracleCaptureError(
                code,
                "capture query returned a terminal error",
                manifest=failed,
                next_gate=next_gate,
            )
        try:
            task_value = _task_body(response, handle.task_id)
            status = _status(task_value.get("status"))
        except OfficialOracleCaptureError as exc:
            failed = self._error_manifest(
                handle.manifest, code=exc.code, outcome=ProviderOutcome.FAILED
            )
            raise OfficialOracleCaptureError(
                exc.code, str(exc).split(": ", 1)[-1], manifest=failed, next_gate=next_gate
            ) from None
        poll_count = handle.poll_count + 1
        if status in {OfficialOracleCaptureStatus.QUEUED, OfficialOracleCaptureStatus.RUNNING}:
            running = replace(handle.manifest, status=status, outcome=ProviderOutcome.NOT_REQUESTED)
            return replace(handle, gate=next_gate, manifest=running, poll_count=poll_count)
        try:
            input_tokens, output_tokens = _usage(task_value)
        except ContractValidationError:
            failed = self._error_manifest(
                handle.manifest, code="malformed_response", outcome=ProviderOutcome.FAILED
            )
            raise OfficialOracleCaptureError(
                "malformed_response",
                "capture token usage is malformed",
                manifest=failed,
                next_gate=next_gate,
            ) from None
        if status is OfficialOracleCaptureStatus.SUCCEEDED:
            content = task_value.get("content")
            prompt = content.get("prompt") if isinstance(content, Mapping) else None
            if (
                task_value.get("task_type") != "h3_context_ir"
                or not isinstance(prompt, str)
                or not prompt.strip()
            ):
                failed = self._error_manifest(
                    handle.manifest, code="malformed_response", outcome=ProviderOutcome.FAILED
                )
                raise OfficialOracleCaptureError(
                    "malformed_response",
                    "capture success payload is incomplete",
                    manifest=failed,
                    next_gate=next_gate,
                )
            succeeded = replace(
                handle.manifest,
                status=OfficialOracleCaptureStatus.SUCCEEDED,
                outcome=ProviderOutcome.SUCCEEDED,
                output_fingerprint=_fingerprint(prompt),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                redacted_message="capture succeeded; output retained as a fingerprint",
            )
            return replace(handle, gate=next_gate, manifest=succeeded, poll_count=poll_count)
        if status is OfficialOracleCaptureStatus.CANCELLED:
            cancelled = self._error_manifest(
                handle.manifest,
                code="cancelled",
                outcome=ProviderOutcome.CANCELLED,
                status=OfficialOracleCaptureStatus.CANCELLED,
            )
            raise OfficialOracleCaptureError(
                "cancelled", "capture task was cancelled", manifest=cancelled, next_gate=next_gate
            )
        code, outcome = _task_failure_code(task_value)
        failed = self._error_manifest(handle.manifest, code=code, outcome=outcome)
        raise OfficialOracleCaptureError(
            code, "capture task failed", manifest=failed, next_gate=next_gate
        )

    def poll_until_terminal(
        self,
        handle: OfficialOracleCaptureHandle,
        transport: OfficialOracleCaptureTransport,
        *,
        now: Callable[[], str],
        rate_clock: Callable[[], float],
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        cancellation_probe: Callable[[], bool] | None = None,
    ) -> OfficialOracleCaptureHandle:
        current = handle
        clock_fn = time.monotonic if clock is None else clock
        sleep_fn = time.sleep if sleep is None else sleep
        while not current.terminal:
            if cancellation_probe is not None:
                try:
                    cancelled = cancellation_probe()
                except Exception:
                    raise OfficialOracleCaptureError(
                        "cancellation_probe", "capture cancellation probe failed"
                    ) from None
                if not isinstance(cancelled, bool):
                    raise ContractValidationError(
                        "capture cancellation probe must return a boolean"
                    )
                if cancelled:
                    return self.cancel(
                        current,
                        transport,
                        now=now(),
                        rate_clock=rate_clock(),
                        clock=clock_fn,
                    )
            elapsed = _clock_value(clock_fn) - current.started_at
            current = self.poll(
                current,
                transport,
                now=now(),
                rate_clock=rate_clock(),
                elapsed_seconds=elapsed,
                clock=clock_fn,
            )
            if not current.terminal:
                sleep_fn(
                    min(
                        self.lifecycle.poll_interval_seconds,
                        self.lifecycle.max_wall_time_seconds - elapsed,
                    )
                )
        return current

    def list(
        self,
        transport: OfficialOracleCaptureTransport,
        *,
        lane_id: str = "official_mocked_contract",
        execution_mode: OfficialOracleExecutionMode = OfficialOracleExecutionMode.MOCKED,
        cursor: str | None = None,
        now: str,
        rate_clock: float,
        operation_id: str = "capture.list",
    ) -> OfficialOracleCaptureList:
        self._validate_mode(execution_mode)
        self._validate_transport(transport)
        if cursor is not None:
            _require_identifier(cursor, "capture list cursor")
            if len(cursor) > MAX_CAPTURE_CURSOR_LENGTH:
                raise ContractValidationError("capture list cursor is too long")
        next_gate = self._admit(
            self.gate,
            lane_id=lane_id,
            execution_mode=execution_mode,
            operation_id=operation_id,
            now=now,
            rate_clock=rate_clock,
        )
        try:
            response = transport.list(cursor)
        except Exception:
            raise OfficialOracleCaptureError(
                "transport", "capture list failed", next_gate=next_gate
            ) from None
        if not 200 <= response.status_code < 300:
            code, _ = _http_error_code(response)
            raise OfficialOracleCaptureError(
                code, "capture list returned a terminal error", next_gate=next_gate
            )
        raw_items = response.body.get("items")
        if not isinstance(raw_items, list) or len(raw_items) > MAX_CAPTURE_LIST_ITEMS:
            raise OfficialOracleCaptureError(
                "malformed_response", "capture list items are invalid", next_gate=next_gate
            )
        items: list[OfficialOracleCaptureListItem] = []
        try:
            for value in raw_items:
                if not isinstance(value, Mapping):
                    raise ValueError
                task_id = _require_identifier(value.get("task_id"), "list task_id")
                status = _status(value.get("status"))
                output = _require_optional_hash(
                    value.get("output_fingerprint"), "list output_fingerprint"
                )
                error = value.get("error_code")
                if error is not None:
                    error = _require_identifier(error, "list error_code")
                items.append(OfficialOracleCaptureListItem(task_id, status, output, error))
            next_cursor_value = response.body.get("next_cursor")
            next_cursor = (
                None
                if next_cursor_value is None
                else _require_identifier(next_cursor_value, "next_cursor")
            )
        except (ContractValidationError, ValueError):
            raise OfficialOracleCaptureError(
                "malformed_response", "capture list is malformed", next_gate=next_gate
            ) from None
        return OfficialOracleCaptureList(tuple(items), next_cursor, next_gate)

    def cancel(
        self,
        handle: OfficialOracleCaptureHandle,
        transport: OfficialOracleCaptureTransport,
        *,
        now: str,
        rate_clock: float,
        operation_id: str = "capture.cancel",
        clock: Callable[[], float] | None = None,
    ) -> OfficialOracleCaptureHandle:
        if not isinstance(handle, OfficialOracleCaptureHandle):
            raise ContractValidationError("capture handle is invalid")
        if handle.terminal:
            raise OfficialOracleCaptureError(
                "already_terminal",
                "capture handle is terminal",
                manifest=handle.manifest,
                next_gate=handle.gate,
            )
        self._validate_mode(handle.execution_mode)
        self._validate_transport(transport)
        next_gate = self._admit(
            handle.gate,
            lane_id=handle.lane_id,
            execution_mode=handle.execution_mode,
            operation_id=operation_id,
            now=now,
            rate_clock=rate_clock,
        )
        try:
            response = transport.cancel(handle.task_id)
        except Exception:
            failed = self._error_manifest(
                handle.manifest, code="transport", outcome=ProviderOutcome.RETRYABLE_TRANSPORT
            )
            raise OfficialOracleCaptureError(
                "transport", "capture cancel failed", manifest=failed, next_gate=next_gate
            ) from None
        if not 200 <= response.status_code < 300:
            code, outcome = _http_error_code(response)
            failed = self._error_manifest(handle.manifest, code=code, outcome=outcome)
            raise OfficialOracleCaptureError(
                code,
                "capture cancel returned a terminal error",
                manifest=failed,
                next_gate=next_gate,
            )
        cancelled = self._error_manifest(
            handle.manifest,
            code="cancelled",
            outcome=ProviderOutcome.CANCELLED,
            status=OfficialOracleCaptureStatus.CANCELLED,
        )
        return replace(
            handle,
            gate=next_gate.abort("capture_cancelled"),
            manifest=cancelled,
        )


__all__ = [
    "MAX_CAPTURE_LIST_ITEMS",
    "OFFICIAL_ORACLE_CAPTURE_SCHEMA",
    "OfficialOracleCaptureError",
    "OfficialOracleCaptureHandle",
    "OfficialOracleCaptureList",
    "OfficialOracleCaptureListItem",
    "OfficialOracleCaptureManifest",
    "OfficialOracleCaptureRunner",
    "OfficialOracleCaptureStatus",
    "OfficialOracleCaptureTransport",
]
