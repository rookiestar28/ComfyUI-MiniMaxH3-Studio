"""Versioned, provider-neutral capability contracts.

This module describes what an explicitly selected provider can do. It performs no execution,
network, credential, or media operation; adapters remain responsible for enforcing the declared
limits at their runtime boundary.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import Enum
from math import isfinite
from typing import Protocol, TypeVar, runtime_checkable

from .contracts import (
    MediaKind,
    ProviderIdentity,
    SchemaVersion,
    TaskMode,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .errors import ContractValidationError
from .security import ResourceLimits

ProviderProtocolVersion = SchemaVersion
CURRENT_PROVIDER_PROTOCOL_VERSION = SchemaVersion(1, 0)

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FIELD_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_MAX_CAPABILITY_ITEMS = 32
_SENSITIVE_FIELD_MARKERS = (
    "api_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "signed",
    "token",
    "url",
    "path",
)
_EnumT = TypeVar("_EnumT", bound=Enum)


class PrivacyLocation(str, Enum):
    """Where provider processing occurs relative to the caller's workspace."""

    LOCAL = "local"
    REMOTE = "remote"
    MIXED = "mixed"


class NetworkRequirement(str, Enum):
    """The strongest network access an adapter requires to execute."""

    NONE = "none"
    LOOPBACK = "loopback"
    INTERNET = "internet"


class CredentialRequirement(str, Enum):
    """Whether an adapter needs credentials resolved outside serialized workflow state."""

    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


def _require_enum(value: object, expected: type[Enum], field: str) -> None:
    if not isinstance(value, expected):
        raise ContractValidationError(f"{field} must be a {expected.__name__}")


def _require_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ContractValidationError(f"{field} must be a boolean")
    return value


def _require_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field} must be a bounded identifier")
    return value


def _require_field(value: object, field: str) -> str:
    if not isinstance(value, str) or _FIELD_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field} must be a bounded output field")
    if any(marker in value for marker in _SENSITIVE_FIELD_MARKERS):
        raise ContractValidationError(f"{field} must not declare a sensitive resource field")
    return value


def _require_enum_set(value: object, expected: type[_EnumT], field: str) -> frozenset[_EnumT]:
    if not isinstance(value, frozenset):
        raise ContractValidationError(f"{field} must be a frozenset")
    if len(value) > _MAX_CAPABILITY_ITEMS or not all(isinstance(item, expected) for item in value):
        raise ContractValidationError(f"{field} contains an invalid capability")
    return value


def _require_field_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not value or len(value) > _MAX_CAPABILITY_ITEMS:
        raise ContractValidationError(
            f"{field} must be a non-empty tuple of at most {_MAX_CAPABILITY_ITEMS} fields"
        )
    fields = tuple(_require_field(item, f"{field} item") for item in value)
    if len(fields) != len(set(fields)):
        raise ContractValidationError(f"{field} must not contain duplicates")
    return fields


@dataclass(frozen=True, slots=True)
class ProviderOutputContract:
    """Versioned, bounded output shape advertised by a provider adapter."""

    schema_version: SchemaVersion
    output_schema: str
    required_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != CURRENT_PROVIDER_PROTOCOL_VERSION:
            raise ContractValidationError(
                f"unsupported provider output schema version {self.schema_version}; "
                "current version is "
                f"{CURRENT_PROVIDER_PROTOCOL_VERSION}"
            )
        _require_identifier(self.output_schema, "output_schema")
        _require_field_tuple(self.required_fields, "required_fields")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": str(self.schema_version),
            "output_schema": self.output_schema,
            "required_fields": sorted(self.required_fields),
        }


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Explicit execution capabilities and finite resource limits for one provider."""

    supported_task_modes: frozenset[TaskMode]
    supported_media: frozenset[MediaKind]
    privacy_location: PrivacyLocation
    network_requirement: NetworkRequirement
    credential_requirement: CredentialRequirement
    limits: ResourceLimits
    supports_determinism: bool
    supports_seed: bool
    supports_cancellation: bool

    def __post_init__(self) -> None:
        _require_enum_set(self.supported_task_modes, TaskMode, "supported_task_modes")
        _require_enum_set(self.supported_media, MediaKind, "supported_media")
        _require_enum(self.privacy_location, PrivacyLocation, "privacy_location")
        _require_enum(self.network_requirement, NetworkRequirement, "network_requirement")
        _require_enum(self.credential_requirement, CredentialRequirement, "credential_requirement")
        if not isinstance(self.limits, ResourceLimits):
            raise ContractValidationError("limits must be a ResourceLimits value")
        _require_bool(self.supports_determinism, "supports_determinism")
        _require_bool(self.supports_seed, "supports_seed")
        _require_bool(self.supports_cancellation, "supports_cancellation")
        if self.supports_seed and not self.supports_determinism:
            raise ContractValidationError("supports_seed requires supports_determinism")
        if self.privacy_location is PrivacyLocation.LOCAL and (
            self.network_requirement is NetworkRequirement.INTERNET
        ):
            raise ContractValidationError("local privacy cannot require internet access")
        if self.privacy_location is PrivacyLocation.REMOTE and (
            self.network_requirement is not NetworkRequirement.INTERNET
        ):
            raise ContractValidationError("remote privacy requires internet access")
        if self.privacy_location is PrivacyLocation.MIXED and (
            self.network_requirement is NetworkRequirement.NONE
        ):
            raise ContractValidationError("mixed privacy requires an explicit network boundary")

    def to_wire(self) -> dict[str, object]:
        return {
            "supported_task_modes": sorted(mode.value for mode in self.supported_task_modes),
            "supported_media": sorted(media.value for media in self.supported_media),
            "privacy_location": self.privacy_location.value,
            "network_requirement": self.network_requirement.value,
            "credential_requirement": self.credential_requirement.value,
            "limits": asdict(self.limits),
            "supports_determinism": self.supports_determinism,
            "supports_seed": self.supports_seed,
            "supports_cancellation": self.supports_cancellation,
        }


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Identity, protocol version, capabilities, and output contract for one provider."""

    protocol_version: SchemaVersion
    identity: ProviderIdentity
    provider_version: str
    capabilities: ProviderCapabilities
    output_contract: ProviderOutputContract

    def __post_init__(self) -> None:
        if self.protocol_version != CURRENT_PROVIDER_PROTOCOL_VERSION:
            raise ContractValidationError(
                f"unsupported provider protocol version {self.protocol_version}; "
                "current version is "
                f"{CURRENT_PROVIDER_PROTOCOL_VERSION}"
            )
        _require_enum(self.identity, ProviderIdentity, "provider identity")
        _require_identifier(self.provider_version, "provider_version")
        if not isinstance(self.capabilities, ProviderCapabilities):
            raise ContractValidationError("capabilities must be a ProviderCapabilities value")
        if not isinstance(self.output_contract, ProviderOutputContract):
            raise ContractValidationError("output_contract must be a ProviderOutputContract value")

    def to_wire(self) -> dict[str, object]:
        return {
            "protocol_version": str(self.protocol_version),
            "identity": self.identity.value,
            "provider_version": self.provider_version,
            "capabilities": self.capabilities.to_wire(),
            "output_contract": self.output_contract.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class ProviderCapabilityRequest:
    """The execution requirements checked before an adapter is allowed to run."""

    task_mode: TaskMode
    media_kinds: tuple[MediaKind, ...] = ()
    reference_count: int = 0
    duration_seconds: float | None = None
    deterministic_required: bool = False
    seed: int | None = None
    cancellation_required: bool = False
    required_output_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_enum(self.task_mode, TaskMode, "requested task mode")
        if not isinstance(self.media_kinds, tuple) or len(self.media_kinds) > _MAX_CAPABILITY_ITEMS:
            raise ContractValidationError("media_kinds must be a bounded tuple")
        if not all(isinstance(kind, MediaKind) for kind in self.media_kinds):
            raise ContractValidationError("media_kinds contains an invalid media kind")
        if len(self.media_kinds) != len(set(self.media_kinds)):
            raise ContractValidationError("media_kinds must not contain duplicates")
        if (
            isinstance(self.reference_count, bool)
            or not isinstance(self.reference_count, int)
            or self.reference_count < 0
        ):
            raise ContractValidationError("reference_count must be a non-negative integer")
        if self.duration_seconds is not None and (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, (int, float))
            or not isfinite(float(self.duration_seconds))
            or self.duration_seconds <= 0
        ):
            raise ContractValidationError("duration_seconds must be finite and positive")
        _require_bool(self.deterministic_required, "deterministic_required")
        _require_bool(self.cancellation_required, "cancellation_required")
        if self.seed is not None and (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not -(2**63) <= self.seed <= 2**63 - 1
        ):
            raise ContractValidationError("seed must be a signed 64-bit integer")
        if not isinstance(self.required_output_fields, tuple):
            raise ContractValidationError("required_output_fields must be a tuple")
        if self.required_output_fields:
            _require_field_tuple(self.required_output_fields, "required_output_fields")


@runtime_checkable
class ProviderProtocol(Protocol):
    """Structural adapter seam; execution and fallback remain outside this item."""

    @property
    def descriptor(self) -> ProviderDescriptor:
        """Return the immutable descriptor for this explicitly selected provider."""


def _fatal(code: str, message: str, location: str | None = None) -> ValidationDiagnostic:
    return ValidationDiagnostic(ValidationSeverity.FATAL, code, message, location)


def validate_provider_capabilities(
    descriptor: ProviderDescriptor,
    request: ProviderCapabilityRequest,
) -> tuple[ValidationDiagnostic, ...]:
    """Return fail-closed diagnostics before any provider execution can begin."""

    if not isinstance(descriptor, ProviderDescriptor):
        raise ContractValidationError("descriptor must be a ProviderDescriptor")
    if not isinstance(request, ProviderCapabilityRequest):
        raise ContractValidationError("request must be a ProviderCapabilityRequest")

    capabilities = descriptor.capabilities
    diagnostics: list[ValidationDiagnostic] = []
    if request.task_mode not in capabilities.supported_task_modes:
        diagnostics.append(
            _fatal(
                "provider.unsupported_task_mode",
                f"provider {descriptor.identity.value} does not support task mode "
                f"{request.task_mode.value}",
                "task_mode",
            )
        )
    for media_kind in request.media_kinds:
        if media_kind not in capabilities.supported_media:
            diagnostics.append(
                _fatal(
                    "provider.unsupported_media_kind",
                    f"provider {descriptor.identity.value} does not support media kind "
                    f"{media_kind.value}",
                    "media_kinds",
                )
            )
    if request.reference_count > capabilities.limits.max_references:
        diagnostics.append(
            _fatal(
                "provider.reference_limit_exceeded",
                f"reference_count exceeds provider limit {capabilities.limits.max_references}",
                "reference_count",
            )
        )
    if request.duration_seconds is not None and (
        request.duration_seconds > capabilities.limits.max_duration_seconds
    ):
        diagnostics.append(
            _fatal(
                "provider.duration_limit_exceeded",
                f"duration_seconds exceeds provider limit "
                f"{capabilities.limits.max_duration_seconds}",
                "duration_seconds",
            )
        )
    if request.deterministic_required and not capabilities.supports_determinism:
        diagnostics.append(
            _fatal(
                "provider.determinism_unsupported",
                "provider does not support deterministic execution",
                "deterministic_required",
            )
        )
    if request.seed is not None and not capabilities.supports_seed:
        diagnostics.append(
            _fatal(
                "provider.seed_unsupported",
                "provider does not support caller-provided seeds",
                "seed",
            )
        )
    if request.cancellation_required and not capabilities.supports_cancellation:
        diagnostics.append(
            _fatal(
                "provider.cancellation_unsupported",
                "provider does not support cancellation",
                "cancellation_required",
            )
        )
    unsupported_output = set(request.required_output_fields).difference(
        descriptor.output_contract.required_fields
    )
    for field in sorted(unsupported_output):
        diagnostics.append(
            _fatal(
                "provider.output_field_unsupported",
                f"provider output contract does not declare field {field!r}",
                "required_output_fields",
            )
        )
    return tuple(diagnostics)


__all__ = [
    "CURRENT_PROVIDER_PROTOCOL_VERSION",
    "CredentialRequirement",
    "NetworkRequirement",
    "PrivacyLocation",
    "ProviderCapabilities",
    "ProviderCapabilityRequest",
    "ProviderDescriptor",
    "ProviderOutputContract",
    "ProviderProtocol",
    "ProviderProtocolVersion",
    "ResourceLimits",
    "validate_provider_capabilities",
]
