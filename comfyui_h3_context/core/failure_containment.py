"""Pure-core quarantine and terminal-failure contracts for untrusted H3 inputs.

Pixels, OCR, metadata, filenames, and provider text are data, never instructions. This module
keeps those fragments behind an explicit policy and validates only typed, redacted output metadata.
It does not execute, interpret, retry, upload, or fall back after malformed or failed work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .canonical import canonical_fingerprint
from .context_reporting import ProviderOutcome
from .contracts import ProviderIdentity, ValidationDiagnostic, ValidationSeverity
from .errors import (
    FailureContainmentError as ContractValidationError,
)
from .errors import (
    LocalAdapterCancelledError,
    LocalAdapterError,
    LocalAdapterMemoryError,
    LocalAdapterTimeoutError,
    OfficialContextIRError,
)
from .provider_policy import ProviderExecutionPolicy
from .providers import ProviderOutputContract

FAILURE_CONTAINMENT_SCHEMA = "h3.failure.containment.v1"
MAX_CONTAINMENT_FRAGMENTS = 128
MAX_CONTAINMENT_TEXT_LENGTH = 8192
MAX_CONTAINMENT_FIELDS = 64
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FIELD_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SENSITIVE_MARKERS = (
    "api_key",
    "authorization",
    "bearer ",
    "password",
    "secret",
    "token=",
)


class UntrustedSourceKind(str, Enum):
    """Where an untrusted instruction-like fragment was observed."""

    PIXELS = "pixels"
    OCR = "ocr"
    METADATA = "metadata"
    FILENAME = "filename"
    PROVIDER_TEXT = "provider_text"


class ContainmentAction(str, Enum):
    """Explicit handling for ordinary malformed input or non-terminal provider failure."""

    REJECT = "reject"
    DEGRADE = "degrade"


class ContainmentStatus(str, Enum):
    """Terminal containment result state; only ACCEPTED may be complete."""

    ACCEPTED = "accepted"
    DEGRADED = "degraded"
    REJECTED = "rejected"


class ContainmentFailureKind(str, Enum):
    """Redacted failure classes that cannot be promoted to successful output."""

    INVALID_INPUT = "invalid_input"
    INVALID_OUTPUT = "invalid_output"
    TIMEOUT = "timeout"
    OUT_OF_MEMORY = "out_of_memory"
    CANCELLED = "cancelled"
    PROVIDER_FAILURE = "provider_failure"
    ADAPTER_FAILURE = "adapter_failure"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a bounded identifier")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise ContractValidationError(f"{field_name} must not contain sensitive markers")
    return value


def _field(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FIELD_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a bounded output field")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise ContractValidationError(f"{field_name} must not contain sensitive markers")
    return value


def _text(value: object, field_name: str, maximum: int = MAX_CONTAINMENT_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ContractValidationError(
            f"{field_name} must be a non-empty string of at most {maximum} characters"
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ContractValidationError(f"{field_name} contains an unsafe control character")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ContractValidationError(f"{field_name} contains an unsafe surrogate")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise ContractValidationError(f"{field_name} contains sensitive material")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a SHA-256 fingerprint")
    return value


@dataclass(frozen=True, slots=True)
class ContainmentPolicy:
    """Frozen non-secret policy and output contract used by every containment operation."""

    system_policy_id: str
    provider_policy: ProviderExecutionPolicy
    output_contract: ProviderOutputContract
    invalid_action: ContainmentAction = ContainmentAction.REJECT
    max_fragments: int = MAX_CONTAINMENT_FRAGMENTS
    max_text_length: int = MAX_CONTAINMENT_TEXT_LENGTH
    max_output_fields: int = MAX_CONTAINMENT_FIELDS
    schema: str = FAILURE_CONTAINMENT_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.system_policy_id, "system_policy_id")
        if not isinstance(self.provider_policy, ProviderExecutionPolicy):
            raise ContractValidationError("provider_policy must be a ProviderExecutionPolicy")
        if not isinstance(self.output_contract, ProviderOutputContract):
            raise ContractValidationError("output_contract must be a ProviderOutputContract")
        if not isinstance(self.invalid_action, ContainmentAction):
            raise ContractValidationError("invalid_action must be a ContainmentAction")
        for value, field_name in (
            (self.max_fragments, "max_fragments"),
            (self.max_text_length, "max_text_length"),
            (self.max_output_fields, "max_output_fields"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ContractValidationError(f"{field_name} must be a positive integer")
        if self.max_fragments > MAX_CONTAINMENT_FRAGMENTS:
            raise ContractValidationError("max_fragments exceeds the containment limit")
        if self.max_text_length > MAX_CONTAINMENT_TEXT_LENGTH:
            raise ContractValidationError("max_text_length exceeds the containment limit")
        if self.max_output_fields > MAX_CONTAINMENT_FIELDS:
            raise ContractValidationError("max_output_fields exceeds the containment limit")
        if self.schema != FAILURE_CONTAINMENT_SCHEMA:
            raise ContractValidationError("unsupported failure-containment schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "system_policy_id": self.system_policy_id,
            "provider_policy": self.provider_policy.to_wire(),
            "output_contract": self.output_contract.to_wire(),
            "invalid_action": self.invalid_action.value,
            "max_fragments": self.max_fragments,
            "max_text_length": self.max_text_length,
            "max_output_fields": self.max_output_fields,
        }


@dataclass(frozen=True, slots=True)
class UntrustedFragment:
    """Bounded source text that is retained as inert, explicitly untrusted data."""

    fragment_id: str
    source_kind: UntrustedSourceKind
    source_id: str
    text: str
    trusted: bool = False
    schema: str = FAILURE_CONTAINMENT_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.fragment_id, "fragment_id")
        if not isinstance(self.source_kind, UntrustedSourceKind):
            raise ContractValidationError("source_kind must be an UntrustedSourceKind")
        _identifier(self.source_id, "source_id")
        _text(self.text, "fragment text")
        if not isinstance(self.trusted, bool) or self.trusted:
            raise ContractValidationError("untrusted fragments must remain untrusted")
        if self.schema != FAILURE_CONTAINMENT_SCHEMA:
            raise ContractValidationError("unsupported fragment schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "fragment_id": self.fragment_id,
            "source_kind": self.source_kind.value,
            "source_id": self.source_id,
            "text": self.text,
            "trusted": self.trusted,
        }


@dataclass(frozen=True, slots=True)
class ProviderOutputEnvelope:
    """Redacted provider output metadata; raw payloads are intentionally not representable."""

    output_id: str
    provider: ProviderIdentity
    output_schema: str
    outcome: ProviderOutcome
    received_fields: tuple[str, ...]
    output_fingerprint: str | None = None
    complete: bool = False
    schema: str = FAILURE_CONTAINMENT_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.output_id, "output_id")
        if not isinstance(self.provider, ProviderIdentity):
            raise ContractValidationError("provider must be a ProviderIdentity")
        _identifier(self.output_schema, "output_schema")
        if not isinstance(self.outcome, ProviderOutcome):
            raise ContractValidationError("outcome must be a ProviderOutcome")
        if (
            not isinstance(self.received_fields, tuple)
            or len(self.received_fields) > MAX_CONTAINMENT_FIELDS
        ):
            raise ContractValidationError("received_fields must be a bounded tuple")
        fields = tuple(_field(value, "received field") for value in self.received_fields)
        if len(fields) != len(set(fields)):
            raise ContractValidationError("received_fields must not contain duplicates")
        if self.output_fingerprint is not None:
            _fingerprint(self.output_fingerprint, "output_fingerprint")
        if not isinstance(self.complete, bool):
            raise ContractValidationError("complete must be a boolean")
        if self.outcome is ProviderOutcome.SUCCEEDED:
            if not self.complete or not fields or self.output_fingerprint is None:
                raise ContractValidationError(
                    "successful output must be complete and fingerprinted"
                )
        elif self.complete:
            raise ContractValidationError("non-successful output cannot be complete")
        if self.schema != FAILURE_CONTAINMENT_SCHEMA:
            raise ContractValidationError("unsupported output envelope schema")
        object.__setattr__(self, "received_fields", fields)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "output_id": self.output_id,
            "provider": self.provider.value,
            "output_schema": self.output_schema,
            "outcome": self.outcome.value,
            "received_fields": list(self.received_fields),
            "output_fingerprint": self.output_fingerprint,
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class ContainmentFailure:
    """Redacted terminal failure; exception/provider text is never retained."""

    failure_id: str
    kind: ContainmentFailureKind
    code: str
    message: str
    schema: str = FAILURE_CONTAINMENT_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.failure_id, "failure_id")
        if not isinstance(self.kind, ContainmentFailureKind):
            raise ContractValidationError("failure kind must be a ContainmentFailureKind")
        _identifier(self.code, "failure code")
        _text(self.message, "failure message", 1024)
        if self.schema != FAILURE_CONTAINMENT_SCHEMA:
            raise ContractValidationError("unsupported failure schema")

    def to_wire(self) -> dict[str, str]:
        return {
            "schema": self.schema,
            "failure_id": self.failure_id,
            "kind": self.kind.value,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ContainmentResult:
    """Immutable quarantined data or explicit output/failure outcome."""

    policy: ContainmentPolicy
    status: ContainmentStatus
    fragments: tuple[UntrustedFragment, ...] = ()
    output: ProviderOutputEnvelope | None = None
    failure: ContainmentFailure | None = None
    diagnostics: tuple[ValidationDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.policy, ContainmentPolicy):
            raise ContractValidationError("policy must be a ContainmentPolicy")
        if not isinstance(self.status, ContainmentStatus):
            raise ContractValidationError("status must be a ContainmentStatus")
        if not isinstance(self.fragments, tuple) or len(self.fragments) > self.policy.max_fragments:
            raise ContractValidationError("fragments exceed the policy limit")
        if not all(isinstance(item, UntrustedFragment) for item in self.fragments):
            raise ContractValidationError("fragments must contain UntrustedFragment values")
        if self.output is not None and not isinstance(self.output, ProviderOutputEnvelope):
            raise ContractValidationError("output must be a ProviderOutputEnvelope or None")
        if self.failure is not None and not isinstance(self.failure, ContainmentFailure):
            raise ContractValidationError("failure must be a ContainmentFailure or None")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(item, ValidationDiagnostic) for item in self.diagnostics
        ):
            raise ContractValidationError("diagnostics must be ValidationDiagnostic values")
        if self.status is ContainmentStatus.ACCEPTED:
            if self.failure is not None:
                raise ContractValidationError("accepted result cannot contain a failure")
            if self.output is not None and not self.output.complete:
                raise ContractValidationError("accepted output must be complete")
        elif self.failure is None:
            raise ContractValidationError("degraded or rejected result requires a failure")
        if self.output is not None and self.status is not ContainmentStatus.ACCEPTED:
            raise ContractValidationError("degraded or rejected result cannot carry output")

    @property
    def is_complete(self) -> bool:
        return self.status is ContainmentStatus.ACCEPTED and self.failure is None

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": FAILURE_CONTAINMENT_SCHEMA,
            "policy": self.policy.to_wire(),
            "status": self.status.value,
            "fragments": [item.to_wire() for item in self.fragments],
            "output": None if self.output is None else self.output.to_wire(),
            "failure": None if self.failure is None else self.failure.to_wire(),
            "diagnostics": [item.to_wire() for item in self.diagnostics],
        }


def _diagnostic(status: ContainmentStatus, code: str, message: str) -> ValidationDiagnostic:
    severity = (
        ValidationSeverity.WARNING
        if status is ContainmentStatus.DEGRADED
        else ValidationSeverity.ERROR
    )
    return ValidationDiagnostic(severity, f"containment.{code}", message, "containment")


def _failure_result(
    policy: ContainmentPolicy,
    kind: ContainmentFailureKind,
    code: str,
    message: str,
    *,
    allow_degrade: bool,
    fragments: tuple[UntrustedFragment, ...] = (),
) -> ContainmentResult:
    status = (
        ContainmentStatus.DEGRADED
        if allow_degrade and policy.invalid_action is ContainmentAction.DEGRADE
        else ContainmentStatus.REJECTED
    )
    failure = ContainmentFailure(f"failure.{code}", kind, code, message)
    return ContainmentResult(
        policy,
        status,
        fragments=fragments,
        failure=failure,
        diagnostics=(_diagnostic(status, code, message),),
    )


def contain_untrusted_fragments(
    policy: ContainmentPolicy,
    fragments: tuple[object, ...],
) -> ContainmentResult:
    """Quarantine source fragments without interpreting or executing their text."""

    if not isinstance(policy, ContainmentPolicy):
        raise ContractValidationError("policy must be a ContainmentPolicy")
    if not isinstance(fragments, tuple) or len(fragments) > policy.max_fragments:
        return _failure_result(
            policy,
            ContainmentFailureKind.INVALID_INPUT,
            "invalid_input",
            "fragment input is outside the bounded containment policy",
            allow_degrade=True,
        )
    valid: list[UntrustedFragment] = []
    for value in fragments:
        if not isinstance(value, UntrustedFragment) or len(value.text) > policy.max_text_length:
            return _failure_result(
                policy,
                ContainmentFailureKind.INVALID_INPUT,
                "invalid_fragment",
                "one or more untrusted fragments failed bounded validation",
                allow_degrade=True,
                fragments=tuple(valid)
                if policy.invalid_action is ContainmentAction.DEGRADE
                else (),
            )
        valid.append(value)
    return ContainmentResult(policy, ContainmentStatus.ACCEPTED, fragments=tuple(valid))


def _validate_output(policy: ContainmentPolicy, output: ProviderOutputEnvelope) -> str | None:
    if output.provider is not policy.provider_policy.provider:
        return "provider identity does not match the frozen execution policy"
    if output.output_schema != policy.output_contract.output_schema:
        return "provider output schema does not match the frozen contract"
    if len(output.received_fields) > policy.max_output_fields:
        return "provider output fields exceed the containment limit"
    missing = set(policy.output_contract.required_fields).difference(output.received_fields)
    if output.outcome is ProviderOutcome.SUCCEEDED and missing:
        return "successful provider output is missing required contract fields"
    return None


def contain_provider_output(
    policy: ContainmentPolicy,
    output: object,
) -> ContainmentResult:
    """Accept only a complete contract-matching output envelope; never expose raw payloads."""

    if not isinstance(policy, ContainmentPolicy):
        raise ContractValidationError("policy must be a ContainmentPolicy")
    if not isinstance(output, ProviderOutputEnvelope):
        return _failure_result(
            policy,
            ContainmentFailureKind.INVALID_OUTPUT,
            "invalid_output",
            "provider output is not a typed containment envelope",
            allow_degrade=True,
        )
    validation_error = _validate_output(policy, output)
    if validation_error is not None:
        return _failure_result(
            policy,
            ContainmentFailureKind.INVALID_OUTPUT,
            "invalid_output",
            validation_error,
            allow_degrade=False,
        )
    if output.outcome is ProviderOutcome.SUCCEEDED:
        return ContainmentResult(policy, ContainmentStatus.ACCEPTED, output=output)
    if output.outcome is ProviderOutcome.TIMEOUT:
        return _failure_result(
            policy,
            ContainmentFailureKind.TIMEOUT,
            "timeout",
            "provider execution exceeded its bounded lifecycle",
            allow_degrade=False,
        )
    if output.outcome is ProviderOutcome.CANCELLED:
        return _failure_result(
            policy,
            ContainmentFailureKind.CANCELLED,
            "cancelled",
            "provider execution was cancelled",
            allow_degrade=False,
        )
    return _failure_result(
        policy,
        ContainmentFailureKind.PROVIDER_FAILURE,
        "provider_failure",
        "provider did not produce a successful output",
        allow_degrade=True,
    )


def _exception_failure(failure: object) -> tuple[ContainmentFailureKind, str, str]:
    if isinstance(failure, LocalAdapterTimeoutError):
        return (
            ContainmentFailureKind.TIMEOUT,
            "timeout",
            "local execution exceeded its bounded budget",
        )
    if isinstance(failure, LocalAdapterMemoryError):
        return (
            ContainmentFailureKind.OUT_OF_MEMORY,
            "out_of_memory",
            "local execution exceeded its memory budget",
        )
    if isinstance(failure, LocalAdapterCancelledError):
        return ContainmentFailureKind.CANCELLED, "cancelled", "local execution was cancelled"
    if isinstance(failure, OfficialContextIRError):
        category = failure.category.casefold()
        if "timeout" in category:
            return (
                ContainmentFailureKind.TIMEOUT,
                "timeout",
                "provider execution exceeded its bounded lifecycle",
            )
        if "cancel" in category:
            return ContainmentFailureKind.CANCELLED, "cancelled", "provider execution was cancelled"
        return (
            ContainmentFailureKind.PROVIDER_FAILURE,
            "provider_failure",
            "provider execution failed",
        )
    if isinstance(failure, LocalAdapterError):
        return (
            ContainmentFailureKind.ADAPTER_FAILURE,
            "adapter_failure",
            "local adapter execution failed",
        )
    return (
        ContainmentFailureKind.ADAPTER_FAILURE,
        "adapter_failure",
        "execution failed before a valid output existed",
    )


def contain_execution_failure(policy: ContainmentPolicy, failure: object) -> ContainmentResult:
    """Convert known execution failures to redacted terminal rejection without fake output."""

    if not isinstance(policy, ContainmentPolicy):
        raise ContractValidationError("policy must be a ContainmentPolicy")
    kind, code, message = _exception_failure(failure)
    return _failure_result(policy, kind, code, message, allow_degrade=False)


def fingerprint_containment_result(result: ContainmentResult) -> str:
    """Return a deterministic identity for a redacted containment result."""

    if not isinstance(result, ContainmentResult):
        raise ContractValidationError("result must be a ContainmentResult")
    return canonical_fingerprint(result.to_wire())


__all__ = [
    "FAILURE_CONTAINMENT_SCHEMA",
    "MAX_CONTAINMENT_FIELDS",
    "MAX_CONTAINMENT_FRAGMENTS",
    "MAX_CONTAINMENT_TEXT_LENGTH",
    "ContainmentAction",
    "ContainmentFailure",
    "ContainmentFailureKind",
    "ContainmentPolicy",
    "ContainmentResult",
    "ContainmentStatus",
    "ProviderOutputEnvelope",
    "UntrustedFragment",
    "UntrustedSourceKind",
    "contain_execution_failure",
    "contain_provider_output",
    "contain_untrusted_fragments",
    "fingerprint_containment_result",
]
