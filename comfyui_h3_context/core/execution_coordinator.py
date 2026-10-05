"""Pure execution coordination for qualified local model/media adapters.

The coordinator is deliberately narrower than a host runtime.  It admits one already-selected
adapter, reserves finite accounting capacity, delegates work to :class:`ResourceScheduler`, and
returns a redacted receipt.  It never imports ComfyUI, Ollama, Torch, a media runtime, or a
filesystem/cache implementation.  Runtime values remain in memory only; portable artifacts are
fingerprints and bounded metrics.
"""

from __future__ import annotations

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
    ContractValidationError,
    ExecutionArtifactError,
    ExecutionCoordinatorError,
    ExecutionResourceError,
    LocalAdapterError,
    LocalAdapterMemoryError,
    LocalAdapterTimeoutError,
)
from .local_adapters import (
    LocalAdapter,
    LocalAdapterExecutionRequest,
    LocalAdapterResult,
    LocalCancellationProbe,
    LocalDeviceKind,
    LocalResourceBudget,
    validate_local_adapter_capabilities,
)
from .model_manifest import ModelBackendFamily, ModelCapabilityState, ModelManifest
from .reliability import (
    ProgressEvent,
    ProgressLedger,
    ReliabilityRunState,
    ReliabilityStage,
)
from .resource_scheduling import (
    ResourceCacheKey,
    ResourceCacheState,
    ResourceExecutionArtifact,
    ResourceExecutionContext,
    ResourceExecutionRequest,
    ResourceExecutionResult,
    ResourceExecutionStatus,
    ResourceScheduler,
)

EXECUTION_COORDINATOR_SCHEMA = "h3.execution.coordinator.v1"
MAX_COORDINATOR_FINGERPRINTS = 512
MAX_COORDINATOR_PROFILE_CAPABILITIES = 16
MAX_COORDINATOR_TTL_SECONDS = 7 * 24 * 60 * 60

_IDENTIFIER_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"
_FINGERPRINT_PATTERN = r"sha256:[0-9a-f]{64}"
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
    import re

    if not isinstance(value, str) or re.fullmatch(_IDENTIFIER_PATTERN, value) is None:
        raise ExecutionCoordinatorError(f"{field_name} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise ExecutionCoordinatorError(f"{field_name} contains sensitive metadata")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    import re

    if not isinstance(value, str) or re.fullmatch(_FINGERPRINT_PATTERN, value) is None:
        raise ExecutionCoordinatorError(f"{field_name} must be a lowercase SHA-256 fingerprint")
    return value


def _revision(value: object, field_name: str) -> str:
    return _identifier(value, field_name)


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExecutionCoordinatorError(f"{field_name} must be a positive integer")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExecutionCoordinatorError(f"{field_name} must be a non-negative integer")
    return value


def _bounded_float(value: object, field_name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExecutionCoordinatorError(f"{field_name} must be a finite number")
    result = float(value)
    if not isfinite(result) or (result < 0 if allow_zero else result <= 0):
        raise ExecutionCoordinatorError(f"{field_name} must be a finite bounded number")
    return result


class CoordinatorProfile(str, Enum):
    """Explicit execution profiles; higher profiles are never implicit fallbacks."""

    PROFILE_0 = "profile_0"
    PROFILE_1 = "profile_1"
    PROFILE_2 = "profile_2"
    PROFILE_3 = "profile_3"


class CoordinatorCapability(str, Enum):
    """Capability groups used by the coordinator admission gate."""

    DETERMINISTIC_EVIDENCE = "deterministic_evidence"
    LOCAL_TEXT_GENERATION = "local_text_generation"
    LOCAL_MULTIMODAL = "local_multimodal"
    SPECIALIST_ANALYSIS = "specialist_analysis"
    RESEARCH_COMPARISON = "research_comparison"


PROFILE_CAPABILITIES: dict[CoordinatorProfile, frozenset[CoordinatorCapability]] = {
    CoordinatorProfile.PROFILE_0: frozenset({CoordinatorCapability.DETERMINISTIC_EVIDENCE}),
    CoordinatorProfile.PROFILE_1: frozenset(
        {
            CoordinatorCapability.DETERMINISTIC_EVIDENCE,
            CoordinatorCapability.LOCAL_TEXT_GENERATION,
        }
    ),
    CoordinatorProfile.PROFILE_2: frozenset(
        {
            CoordinatorCapability.DETERMINISTIC_EVIDENCE,
            CoordinatorCapability.LOCAL_TEXT_GENERATION,
            CoordinatorCapability.LOCAL_MULTIMODAL,
            CoordinatorCapability.SPECIALIST_ANALYSIS,
        }
    ),
    CoordinatorProfile.PROFILE_3: frozenset(
        {
            CoordinatorCapability.DETERMINISTIC_EVIDENCE,
            CoordinatorCapability.LOCAL_TEXT_GENERATION,
            CoordinatorCapability.LOCAL_MULTIMODAL,
            CoordinatorCapability.SPECIALIST_ANALYSIS,
            CoordinatorCapability.RESEARCH_COMPARISON,
        }
    ),
}


class OwnershipKind(str, Enum):
    """Who may release a runtime resource."""

    HOST = "host"
    ADAPTER = "adapter"
    EXTERNAL = "external"


class ArtifactCacheScope(str, Enum):
    """Explicit isolation scope for content-bearing cache entries."""

    NONE = "none"
    SESSION = "session"
    USER = "user"
    TENANT = "tenant"


class CoordinatorStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    OUT_OF_MEMORY = "out_of_memory"
    OUTPUT_LIMIT = "output_limit"
    RESOURCE_LIMIT = "resource_limit"
    CONCURRENCY_LIMIT = "concurrency_limit"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CoordinatorCapacity:
    """Finite host accounting capacity; values are not allocator handles."""

    cpu_threads: int
    gpu_slots: int
    ram_bytes: int
    vram_bytes: int
    max_concurrency: int
    schema: str = EXECUTION_COORDINATOR_SCHEMA

    def __post_init__(self) -> None:
        _positive_int(self.cpu_threads, "capacity cpu_threads")
        _non_negative_int(self.gpu_slots, "capacity gpu_slots")
        _positive_int(self.ram_bytes, "capacity ram_bytes")
        _non_negative_int(self.vram_bytes, "capacity vram_bytes")
        _positive_int(self.max_concurrency, "capacity max_concurrency")
        if self.schema != EXECUTION_COORDINATOR_SCHEMA:
            raise ExecutionCoordinatorError("unsupported execution coordinator schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "cpu_threads": self.cpu_threads,
            "gpu_slots": self.gpu_slots,
            "ram_bytes": self.ram_bytes,
            "vram_bytes": self.vram_bytes,
            "max_concurrency": self.max_concurrency,
        }


@dataclass(frozen=True, slots=True)
class CoordinatorResourceBudget:
    """One execution's requested CPU/GPU/RAM/VRAM/time/concurrency/output envelope."""

    cpu_threads: int
    gpu_slots: int
    ram_bytes: int
    vram_bytes: int
    max_wall_time_seconds: float
    max_concurrency: int
    max_output_bytes: int
    max_output_items: int
    max_references: int = 0
    max_context_tokens: int = 0
    schema: str = EXECUTION_COORDINATOR_SCHEMA

    def __post_init__(self) -> None:
        _positive_int(self.cpu_threads, "budget cpu_threads")
        _non_negative_int(self.gpu_slots, "budget gpu_slots")
        _positive_int(self.ram_bytes, "budget ram_bytes")
        _non_negative_int(self.vram_bytes, "budget vram_bytes")
        _bounded_float(self.max_wall_time_seconds, "budget max_wall_time_seconds")
        _positive_int(self.max_concurrency, "budget max_concurrency")
        _positive_int(self.max_output_bytes, "budget max_output_bytes")
        _positive_int(self.max_output_items, "budget max_output_items")
        _non_negative_int(self.max_references, "budget max_references")
        _non_negative_int(self.max_context_tokens, "budget max_context_tokens")
        if self.schema != EXECUTION_COORDINATOR_SCHEMA:
            raise ExecutionCoordinatorError("unsupported execution coordinator schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "cpu_threads": self.cpu_threads,
            "gpu_slots": self.gpu_slots,
            "ram_bytes": self.ram_bytes,
            "vram_bytes": self.vram_bytes,
            "max_wall_time_seconds": self.max_wall_time_seconds,
            "max_concurrency": self.max_concurrency,
            "max_output_bytes": self.max_output_bytes,
            "max_output_items": self.max_output_items,
            "max_references": self.max_references,
            "max_context_tokens": self.max_context_tokens,
        }

    def to_local_budget(self) -> LocalResourceBudget:
        return LocalResourceBudget(
            max_memory_bytes=self.ram_bytes,
            max_wall_time_seconds=self.max_wall_time_seconds,
            max_references=max(1, self.max_references),
            max_output_bytes=self.max_output_bytes,
            max_output_items=self.max_output_items,
            max_concurrency=self.max_concurrency,
        )


@dataclass(frozen=True, slots=True)
class CoordinatorCacheIdentity:
    """Complete content-addressed identity with no raw content or private locators."""

    operation_id: str
    contract_schema: str
    renderer_profile: str
    prompt_profile: str
    provider_id: str
    adapter_id: str
    adapter_version: str
    server_version: str
    model_digest: str
    tokenizer_revision: str
    processor_revision: str
    preprocessing_revision: str
    settings_fingerprint: str
    source_fingerprints: tuple[str, ...]
    scope_fingerprint: str | None = None
    schema: str = EXECUTION_COORDINATOR_SCHEMA

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.operation_id, "cache operation_id"),
            (self.contract_schema, "cache contract_schema"),
            (self.renderer_profile, "cache renderer_profile"),
            (self.prompt_profile, "cache prompt_profile"),
            (self.provider_id, "cache provider_id"),
            (self.adapter_id, "cache adapter_id"),
            (self.adapter_version, "cache adapter_version"),
            (self.server_version, "cache server_version"),
            (self.tokenizer_revision, "cache tokenizer_revision"),
            (self.processor_revision, "cache processor_revision"),
            (self.preprocessing_revision, "cache preprocessing_revision"),
        ):
            _revision(value, field_name)
        _fingerprint(self.model_digest, "cache model_digest")
        _fingerprint(self.settings_fingerprint, "cache settings_fingerprint")
        if not isinstance(self.source_fingerprints, tuple) or not self.source_fingerprints:
            raise ExecutionCoordinatorError("cache source_fingerprints must not be empty")
        if len(self.source_fingerprints) > MAX_COORDINATOR_FINGERPRINTS:
            raise ExecutionCoordinatorError("cache source_fingerprints exceed the finite bound")
        for value in self.source_fingerprints:
            _fingerprint(value, "cache source_fingerprint")
        if len(set(self.source_fingerprints)) != len(self.source_fingerprints):
            raise ExecutionCoordinatorError("cache source_fingerprints must be unique")
        if self.scope_fingerprint is not None:
            _fingerprint(self.scope_fingerprint, "cache scope_fingerprint")
        if self.schema != EXECUTION_COORDINATOR_SCHEMA:
            raise ExecutionCoordinatorError("unsupported execution coordinator schema")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "operation_id": self.operation_id,
            "contract_schema": self.contract_schema,
            "renderer_profile": self.renderer_profile,
            "prompt_profile": self.prompt_profile,
            "provider_id": self.provider_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "server_version": self.server_version,
            "model_digest": self.model_digest,
            "tokenizer_revision": self.tokenizer_revision,
            "processor_revision": self.processor_revision,
            "preprocessing_revision": self.preprocessing_revision,
            "settings_fingerprint": self.settings_fingerprint,
            "source_fingerprints": list(self.source_fingerprints),
            "scope_fingerprint": self.scope_fingerprint,
        }

    def to_resource_cache_key(self) -> ResourceCacheKey:
        return ResourceCacheKey(
            operation_id=self.operation_id,
            contract_schema=self.contract_schema,
            provider_id=self.provider_id,
            provider_version=self.adapter_version,
            settings_fingerprint=self.fingerprint,
            source_fingerprints=self.source_fingerprints,
        )


@dataclass(frozen=True, slots=True)
class ArtifactCachePolicy:
    """Explicit content-bearing cache policy; disabled means no runtime cache write."""

    enabled: bool = False
    scope: ArtifactCacheScope = ArtifactCacheScope.NONE
    scope_fingerprint: str | None = None
    ttl_seconds: float = 0.0
    consent_granted: bool = False
    delete_on_cancel: bool = True
    schema: str = EXECUTION_COORDINATOR_SCHEMA

    def __post_init__(self) -> None:
        try:
            scope = (
                self.scope
                if isinstance(self.scope, ArtifactCacheScope)
                else ArtifactCacheScope(self.scope)
            )
        except (TypeError, ValueError):
            raise ExecutionArtifactError("artifact cache scope is unsupported") from None
        object.__setattr__(self, "scope", scope)
        if not isinstance(self.enabled, bool) or not isinstance(self.consent_granted, bool):
            raise ExecutionArtifactError("artifact cache booleans are invalid")
        if not isinstance(self.delete_on_cancel, bool):
            raise ExecutionArtifactError("delete_on_cancel must be a boolean")
        if self.enabled:
            if scope is ArtifactCacheScope.NONE or not self.consent_granted:
                raise ExecutionArtifactError(
                    "content-bearing cache requires an explicit scope and consent"
                )
            _bounded_float(self.ttl_seconds, "artifact ttl_seconds")
            if self.ttl_seconds > MAX_COORDINATOR_TTL_SECONDS:
                raise ExecutionArtifactError("artifact ttl_seconds exceeds the finite bound")
            if scope in {ArtifactCacheScope.USER, ArtifactCacheScope.TENANT}:
                if self.scope_fingerprint is None:
                    raise ExecutionArtifactError("user/tenant cache scope requires a fingerprint")
                _fingerprint(self.scope_fingerprint, "artifact scope_fingerprint")
        else:
            if scope is not ArtifactCacheScope.NONE:
                raise ExecutionArtifactError("disabled artifact cache must use scope none")
            if self.scope_fingerprint is not None:
                _fingerprint(self.scope_fingerprint, "artifact scope_fingerprint")
            if self.ttl_seconds != 0:
                raise ExecutionArtifactError("disabled artifact cache must use zero TTL")
        if self.schema != EXECUTION_COORDINATOR_SCHEMA:
            raise ExecutionArtifactError("unsupported execution coordinator schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "enabled": self.enabled,
            "scope": self.scope.value,
            "scope_fingerprint": self.scope_fingerprint,
            "ttl_seconds": self.ttl_seconds,
            "consent_granted": self.consent_granted,
            "delete_on_cancel": self.delete_on_cancel,
        }


@dataclass(frozen=True, slots=True)
class ResourceOwnershipPolicy:
    """Backend-specific owner map; external resources are observed, never closed."""

    backend_family: ModelBackendFamily
    model_residency: OwnershipKind
    offload: OwnershipKind
    process: OwnershipKind
    vram: OwnershipKind
    context: OwnershipKind
    keep_alive: OwnershipKind
    close_model: bool = False
    close_process: bool = False
    observe_external: bool = False
    schema: str = EXECUTION_COORDINATOR_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.backend_family, ModelBackendFamily):
            raise ExecutionCoordinatorError("ownership backend_family is invalid")
        owners = (
            self.model_residency,
            self.offload,
            self.process,
            self.vram,
            self.context,
            self.keep_alive,
        )
        if not all(isinstance(item, OwnershipKind) for item in owners):
            raise ExecutionCoordinatorError("ownership resource owners are invalid")
        if not isinstance(self.close_model, bool) or not isinstance(self.close_process, bool):
            raise ExecutionCoordinatorError("ownership close flags are invalid")
        if not isinstance(self.observe_external, bool):
            raise ExecutionCoordinatorError("ownership observe_external must be a boolean")
        if self.close_model and self.model_residency is not OwnershipKind.ADAPTER:
            raise ExecutionCoordinatorError("only adapter-owned model residency may be closed")
        if self.close_process and self.process is not OwnershipKind.ADAPTER:
            raise ExecutionCoordinatorError("only adapter-owned processes may be closed")
        if OwnershipKind.EXTERNAL in owners and not self.observe_external:
            raise ExecutionCoordinatorError("external resources require observation")
        if self.schema != EXECUTION_COORDINATOR_SCHEMA:
            raise ExecutionCoordinatorError("unsupported execution coordinator schema")

    @property
    def has_adapter_owned_resources(self) -> bool:
        return any(
            owner is OwnershipKind.ADAPTER
            for owner in (
                self.model_residency,
                self.offload,
                self.process,
                self.vram,
                self.context,
                self.keep_alive,
            )
        )

    @property
    def has_external_resources(self) -> bool:
        return any(
            owner is OwnershipKind.EXTERNAL
            for owner in (
                self.model_residency,
                self.offload,
                self.process,
                self.vram,
                self.context,
                self.keep_alive,
            )
        )

    @classmethod
    def for_backend(cls, backend_family: ModelBackendFamily) -> ResourceOwnershipPolicy:
        if backend_family is ModelBackendFamily.COMFYUI_NATIVE:
            return cls(
                backend_family,
                OwnershipKind.HOST,
                OwnershipKind.HOST,
                OwnershipKind.HOST,
                OwnershipKind.HOST,
                OwnershipKind.HOST,
                OwnershipKind.HOST,
            )
        return cls(
            backend_family,
            OwnershipKind.EXTERNAL,
            OwnershipKind.EXTERNAL,
            OwnershipKind.EXTERNAL,
            OwnershipKind.EXTERNAL,
            OwnershipKind.EXTERNAL,
            OwnershipKind.EXTERNAL,
            observe_external=True,
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "backend_family": self.backend_family.value,
            "model_residency": self.model_residency.value,
            "offload": self.offload.value,
            "process": self.process.value,
            "vram": self.vram.value,
            "context": self.context.value,
            "keep_alive": self.keep_alive.value,
            "close_model": self.close_model,
            "close_process": self.close_process,
            "observe_external": self.observe_external,
        }


@dataclass(frozen=True, slots=True)
class ExternalResourceObservation:
    """Bounded metrics observed from an externally owned backend process."""

    backend_family: ModelBackendFamily
    process_present: bool
    vram_bytes: int
    context_tokens: int | None = None
    keep_alive_seconds: float | None = None
    schema: str = EXECUTION_COORDINATOR_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.backend_family, ModelBackendFamily):
            raise ExecutionCoordinatorError("external observation backend is invalid")
        if not isinstance(self.process_present, bool):
            raise ExecutionCoordinatorError("external observation process_present is invalid")
        _non_negative_int(self.vram_bytes, "external observation vram_bytes")
        if self.context_tokens is not None:
            _positive_int(self.context_tokens, "external observation context_tokens")
        if self.keep_alive_seconds is not None:
            _bounded_float(
                self.keep_alive_seconds, "external observation keep_alive_seconds", allow_zero=True
            )
        if self.schema != EXECUTION_COORDINATOR_SCHEMA:
            raise ExecutionCoordinatorError("unsupported execution coordinator schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "backend_family": self.backend_family.value,
            "process_present": self.process_present,
            "vram_bytes": self.vram_bytes,
            "context_tokens": self.context_tokens,
            "keep_alive_seconds": self.keep_alive_seconds,
        }


@runtime_checkable
class ResourceLifecycle(Protocol):
    """Injected observation/cleanup handle; coordinator gates ownership before close."""

    def observe(self) -> ExternalResourceObservation: ...

    def close_owned(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BackendRuntimeQualification:
    """Separate backend progress/cancellation capability qualification."""

    backend_family: ModelBackendFamily
    progress: ModelCapabilityState
    cancellation: ModelCapabilityState
    schema: str = EXECUTION_COORDINATOR_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.backend_family, ModelBackendFamily):
            raise ExecutionCoordinatorError("qualification backend is invalid")
        if not isinstance(self.progress, ModelCapabilityState) or not isinstance(
            self.cancellation, ModelCapabilityState
        ):
            raise ExecutionCoordinatorError("qualification state is invalid")
        if self.schema != EXECUTION_COORDINATOR_SCHEMA:
            raise ExecutionCoordinatorError("unsupported execution coordinator schema")

    @classmethod
    def from_manifest(cls, manifest: ModelManifest) -> BackendRuntimeQualification:
        if not isinstance(manifest, ModelManifest):
            raise ExecutionCoordinatorError("manifest is required for backend qualification")
        progress = (
            ModelCapabilityState.QUALIFIED
            if manifest.backend_family is ModelBackendFamily.COMFYUI_NATIVE
            else ModelCapabilityState.UNQUALIFIED
        )
        return cls(manifest.backend_family, progress, manifest.cancellation)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "backend_family": self.backend_family.value,
            "progress": self.progress.value,
            "cancellation": self.cancellation.value,
        }


@dataclass(frozen=True, slots=True)
class PortableArtifact:
    """Hash-only artifact projection; runtime content is intentionally absent."""

    artifact_fingerprint: str
    cache_fingerprint: str
    status: CoordinatorStatus
    scope: ArtifactCacheScope
    output_bytes: int
    output_items: int
    ttl_seconds: float
    schema: str = EXECUTION_COORDINATOR_SCHEMA

    def __post_init__(self) -> None:
        _fingerprint(self.artifact_fingerprint, "artifact_fingerprint")
        _fingerprint(self.cache_fingerprint, "cache_fingerprint")
        if self.status is not CoordinatorStatus.COMPLETE:
            raise ExecutionArtifactError("only complete results may produce portable artifacts")
        if not isinstance(self.scope, ArtifactCacheScope):
            raise ExecutionArtifactError("artifact scope is invalid")
        _non_negative_int(self.output_bytes, "artifact output_bytes")
        _non_negative_int(self.output_items, "artifact output_items")
        _bounded_float(self.ttl_seconds, "artifact ttl_seconds", allow_zero=True)
        if self.schema != EXECUTION_COORDINATOR_SCHEMA:
            raise ExecutionArtifactError("unsupported execution coordinator schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "artifact_fingerprint": self.artifact_fingerprint,
            "cache_fingerprint": self.cache_fingerprint,
            "status": self.status.value,
            "scope": self.scope.value,
            "output_bytes": self.output_bytes,
            "output_items": self.output_items,
            "ttl_seconds": self.ttl_seconds,
        }


@dataclass(frozen=True, slots=True)
class CoordinatorExecutionRequest:
    """Validated request joining a typed adapter call to coordinator policies."""

    profile: CoordinatorProfile
    cache_identity: CoordinatorCacheIdentity
    budget: CoordinatorResourceBudget
    adapter_request: LocalAdapterExecutionRequest
    qualification: BackendRuntimeQualification
    ownership: ResourceOwnershipPolicy
    required_capabilities: frozenset[CoordinatorCapability] = frozenset(
        {CoordinatorCapability.LOCAL_TEXT_GENERATION}
    )
    artifact_policy: ArtifactCachePolicy = field(default_factory=ArtifactCachePolicy)
    progress_required: bool = False
    cancellation_required: bool = False
    progress_stage: ReliabilityStage = ReliabilityStage.PROVIDER
    schema: str = EXECUTION_COORDINATOR_SCHEMA

    def __post_init__(self) -> None:
        try:
            profile = (
                self.profile
                if isinstance(self.profile, CoordinatorProfile)
                else CoordinatorProfile(self.profile)
            )
        except (TypeError, ValueError):
            raise ExecutionCoordinatorError("coordinator profile is unsupported") from None
        object.__setattr__(self, "profile", profile)
        if not isinstance(self.cache_identity, CoordinatorCacheIdentity):
            raise ExecutionCoordinatorError("cache_identity is invalid")
        if not isinstance(self.budget, CoordinatorResourceBudget):
            raise ExecutionCoordinatorError("coordinator budget is invalid")
        if not isinstance(self.adapter_request, LocalAdapterExecutionRequest):
            raise ExecutionCoordinatorError("adapter_request is invalid")
        if self.cache_identity.adapter_id != self.adapter_request.adapter_id:
            raise ExecutionCoordinatorError("cache and adapter identities do not match")
        if not isinstance(self.qualification, BackendRuntimeQualification):
            raise ExecutionCoordinatorError("backend qualification is invalid")
        if not isinstance(self.ownership, ResourceOwnershipPolicy):
            raise ExecutionCoordinatorError("ownership policy is invalid")
        if self.qualification.backend_family is not self.ownership.backend_family:
            raise ExecutionCoordinatorError("qualification and ownership backend mismatch")
        if (
            self.artifact_policy.scope_fingerprint is not None
            and self.cache_identity.scope_fingerprint != self.artifact_policy.scope_fingerprint
        ):
            raise ExecutionArtifactError("cache identity and artifact scope fingerprints differ")
        if not isinstance(self.required_capabilities, frozenset) or not self.required_capabilities:
            raise ExecutionCoordinatorError("required_capabilities must be non-empty")
        if len(self.required_capabilities) > MAX_COORDINATOR_PROFILE_CAPABILITIES or not all(
            isinstance(item, CoordinatorCapability) for item in self.required_capabilities
        ):
            raise ExecutionCoordinatorError("required_capabilities are invalid")
        if not isinstance(self.progress_required, bool) or not isinstance(
            self.cancellation_required, bool
        ):
            raise ExecutionCoordinatorError("progress/cancellation requirements are invalid")
        if not isinstance(self.progress_stage, ReliabilityStage):
            raise ExecutionCoordinatorError("progress_stage is invalid")
        if self.cancellation_required and not self.adapter_request.cancellation_required:
            raise ExecutionCoordinatorError(
                "adapter request must declare cancellation when coordinator requires it"
            )
        if self.schema != EXECUTION_COORDINATOR_SCHEMA:
            raise ExecutionCoordinatorError("unsupported execution coordinator schema")

    @property
    def operation_id(self) -> str:
        return self.cache_identity.operation_id

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "profile": self.profile.value,
            "cache_identity": self.cache_identity.to_wire(),
            "budget": self.budget.to_wire(),
            "adapter_request": self.adapter_request.to_public_dict(),
            "qualification": self.qualification.to_wire(),
            "ownership": self.ownership.to_wire(),
            "required_capabilities": sorted(item.value for item in self.required_capabilities),
            "artifact_policy": self.artifact_policy.to_wire(),
            "progress_required": self.progress_required,
            "cancellation_required": self.cancellation_required,
            "progress_stage": self.progress_stage.value,
        }


@dataclass(frozen=True, slots=True)
class CoordinatorReceipt:
    """Redacted lifecycle receipt for one coordinated execution."""

    operation_id: str
    status: CoordinatorStatus
    cache_state: ResourceCacheState
    profile: CoordinatorProfile
    backend_family: ModelBackendFamily
    elapsed_seconds: float
    reservation_released: bool
    cleanup_attempted: bool
    cleanup_completed: bool
    external_observed: bool
    progress_qualified: ModelCapabilityState
    cancellation_qualified: ModelCapabilityState
    portable_artifact: PortableArtifact | None = None
    diagnostics: tuple[ValidationDiagnostic, ...] = ()
    schema: str = EXECUTION_COORDINATOR_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "receipt operation_id")
        if not isinstance(self.status, CoordinatorStatus):
            raise ExecutionCoordinatorError("receipt status is invalid")
        if not isinstance(self.cache_state, ResourceCacheState):
            raise ExecutionCoordinatorError("receipt cache state is invalid")
        if not isinstance(self.profile, CoordinatorProfile) or not isinstance(
            self.backend_family, ModelBackendFamily
        ):
            raise ExecutionCoordinatorError("receipt profile/backend is invalid")
        _bounded_float(self.elapsed_seconds, "receipt elapsed_seconds", allow_zero=True)
        for value, field_name in (
            (self.reservation_released, "reservation_released"),
            (self.cleanup_attempted, "cleanup_attempted"),
            (self.cleanup_completed, "cleanup_completed"),
            (self.external_observed, "external_observed"),
        ):
            if not isinstance(value, bool):
                raise ExecutionCoordinatorError(f"receipt {field_name} is invalid")
        if not isinstance(self.progress_qualified, ModelCapabilityState) or not isinstance(
            self.cancellation_qualified, ModelCapabilityState
        ):
            raise ExecutionCoordinatorError("receipt qualification is invalid")
        if self.portable_artifact is not None and not isinstance(
            self.portable_artifact, PortableArtifact
        ):
            raise ExecutionCoordinatorError("receipt portable artifact is invalid")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(item, ValidationDiagnostic) for item in self.diagnostics
        ):
            raise ExecutionCoordinatorError("receipt diagnostics are invalid")
        if self.schema != EXECUTION_COORDINATOR_SCHEMA:
            raise ExecutionCoordinatorError("unsupported execution coordinator schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "operation_id": self.operation_id,
            "status": self.status.value,
            "cache_state": self.cache_state.value,
            "profile": self.profile.value,
            "backend_family": self.backend_family.value,
            "elapsed_seconds": self.elapsed_seconds,
            "reservation_released": self.reservation_released,
            "cleanup_attempted": self.cleanup_attempted,
            "cleanup_completed": self.cleanup_completed,
            "external_observed": self.external_observed,
            "progress_qualified": self.progress_qualified.value,
            "cancellation_qualified": self.cancellation_qualified.value,
            "portable_artifact": None
            if self.portable_artifact is None
            else self.portable_artifact.to_wire(),
            "diagnostics": [item.to_wire() for item in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class CoordinatorExecutionResult:
    """Runtime value plus redacted receipt; incomplete states never expose a dummy value."""

    receipt: CoordinatorReceipt
    value: object | None = field(default=None, repr=False, compare=False)
    resource_result: ResourceExecutionResult | None = field(default=None, repr=False, compare=False)
    progress: tuple[ProgressEvent, ...] = ()
    schema: str = EXECUTION_COORDINATOR_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, CoordinatorReceipt):
            raise ExecutionCoordinatorError("result receipt is invalid")
        if self.resource_result is not None and not isinstance(
            self.resource_result, ResourceExecutionResult
        ):
            raise ExecutionCoordinatorError("result resource_result is invalid")
        if not isinstance(self.progress, tuple) or not all(
            isinstance(item, ProgressEvent) for item in self.progress
        ):
            raise ExecutionCoordinatorError("result progress is invalid")
        if self.receipt.status is not CoordinatorStatus.COMPLETE and self.value is not None:
            raise ExecutionCoordinatorError("terminal non-complete result cannot carry a value")
        if self.schema != EXECUTION_COORDINATOR_SCHEMA:
            raise ExecutionCoordinatorError("unsupported execution coordinator schema")

    @property
    def complete(self) -> bool:
        return self.receipt.status is CoordinatorStatus.COMPLETE

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "complete": self.complete,
            "receipt": self.receipt.to_wire(),
            "resource_result": None
            if self.resource_result is None
            else self.resource_result.to_wire(),
            "progress": [event.to_wire() for event in self.progress],
        }


class _CapacityReservation:
    def __init__(self, pool: _CapacityPool, budget: CoordinatorResourceBudget) -> None:
        self._pool = pool
        self._budget = budget
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._pool._release(self._budget)


class _CapacityPool:
    def __init__(self, capacity: CoordinatorCapacity) -> None:
        self.capacity = capacity
        self._cpu = 0
        self._gpu = 0
        self._ram = 0
        self._vram = 0
        self._active = 0
        self._lock = threading.RLock()

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def reserve(self, budget: CoordinatorResourceBudget) -> _CapacityReservation:
        with self._lock:
            if self._active >= self.capacity.max_concurrency:
                raise ExecutionResourceError("coordinator concurrency capacity is exhausted")
            if self._cpu + budget.cpu_threads > self.capacity.cpu_threads:
                raise ExecutionResourceError("coordinator CPU capacity is exhausted")
            if self._gpu + budget.gpu_slots > self.capacity.gpu_slots:
                raise ExecutionResourceError("coordinator GPU capacity is exhausted")
            if self._ram + budget.ram_bytes > self.capacity.ram_bytes:
                raise ExecutionResourceError("coordinator RAM capacity is exhausted")
            if self._vram + budget.vram_bytes > self.capacity.vram_bytes:
                raise ExecutionResourceError("coordinator VRAM capacity is exhausted")
            self._cpu += budget.cpu_threads
            self._gpu += budget.gpu_slots
            self._ram += budget.ram_bytes
            self._vram += budget.vram_bytes
            self._active += 1
        return _CapacityReservation(self, budget)

    def _release(self, budget: CoordinatorResourceBudget) -> None:
        with self._lock:
            if self._active <= 0:
                raise ExecutionResourceError("coordinator capacity reservation underflow")
            self._cpu -= budget.cpu_threads
            self._gpu -= budget.gpu_slots
            self._ram -= budget.ram_bytes
            self._vram -= budget.vram_bytes
            self._active -= 1
            if min(self._cpu, self._gpu, self._ram, self._vram) < 0:
                raise ExecutionResourceError("coordinator capacity accounting underflow")


def _diagnostic(
    code: str,
    message: str,
    severity: ValidationSeverity = ValidationSeverity.ERROR,
) -> ValidationDiagnostic:
    return ValidationDiagnostic(severity, code, message, "execution_coordinator")


def _status_from_resource(status: ResourceExecutionStatus) -> CoordinatorStatus:
    return {
        ResourceExecutionStatus.COMPLETE: CoordinatorStatus.COMPLETE,
        ResourceExecutionStatus.PARTIAL: CoordinatorStatus.PARTIAL,
        ResourceExecutionStatus.STALE: CoordinatorStatus.FAILED,
        ResourceExecutionStatus.CANCELLED: CoordinatorStatus.CANCELLED,
        ResourceExecutionStatus.TIMED_OUT: CoordinatorStatus.TIMED_OUT,
        ResourceExecutionStatus.MEMORY_EXCEEDED: CoordinatorStatus.OUT_OF_MEMORY,
        ResourceExecutionStatus.BUDGET_EXCEEDED: CoordinatorStatus.OUTPUT_LIMIT,
        ResourceExecutionStatus.CONCURRENCY_EXCEEDED: CoordinatorStatus.CONCURRENCY_LIMIT,
        ResourceExecutionStatus.FAILED: CoordinatorStatus.FAILED,
    }[status]


def _artifact_fingerprint(value: object, cache_fingerprint: str) -> str:
    if hasattr(value, "to_public_dict"):
        projection = value.to_public_dict()
    elif isinstance(value, (str, int, bool, type(None), dict, list, tuple)):
        projection = value
    else:
        raise ExecutionArtifactError("adapter result has no bounded public projection")
    try:
        return canonical_fingerprint({"cache_fingerprint": cache_fingerprint, "output": projection})
    except ContractValidationError as exc:
        raise ExecutionArtifactError("adapter output cannot form a portable fingerprint") from exc


class ExecutionCoordinator:
    """Admission, scheduling, lifecycle ownership, and redacted artifact facade."""

    def __init__(
        self,
        capacity: CoordinatorCapacity,
        *,
        scheduler: ResourceScheduler | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(capacity, CoordinatorCapacity):
            raise ExecutionCoordinatorError("capacity must be CoordinatorCapacity")
        if scheduler is not None and not isinstance(scheduler, ResourceScheduler):
            raise ExecutionCoordinatorError("scheduler must be ResourceScheduler")
        if not callable(clock):
            raise ExecutionCoordinatorError("coordinator clock must be callable")
        self.capacity = capacity
        self.scheduler = (
            scheduler
            if scheduler is not None
            else ResourceScheduler(max_concurrency=capacity.max_concurrency)
        )
        self._clock = clock
        self._pool = _CapacityPool(capacity)

    @property
    def active(self) -> int:
        return self._pool.active

    @staticmethod
    def _validate_profile(request: CoordinatorExecutionRequest) -> tuple[ValidationDiagnostic, ...]:
        supported = PROFILE_CAPABILITIES[request.profile]
        missing = request.required_capabilities - supported
        if not missing:
            return ()
        return (
            _diagnostic(
                "coordinator.profile_capability",
                "selected profile does not declare every required capability",
            ),
        )

    def _blocked(
        self,
        request: CoordinatorExecutionRequest,
        status: CoordinatorStatus,
        code: str,
        message: str,
        *,
        cache_state: ResourceCacheState = ResourceCacheState.DISABLED,
        progress: tuple[ProgressEvent, ...] = (),
    ) -> CoordinatorExecutionResult:
        receipt = CoordinatorReceipt(
            request.operation_id,
            status,
            cache_state,
            request.profile,
            request.qualification.backend_family,
            0.0,
            True,
            False,
            True,
            False,
            request.qualification.progress,
            request.qualification.cancellation,
            None,
            (_diagnostic(code, message),),
        )
        return CoordinatorExecutionResult(receipt, None, None, progress)

    def execute(
        self,
        adapter: LocalAdapter,
        request: CoordinatorExecutionRequest,
        *,
        lifecycle: ResourceLifecycle | None = None,
        cancellation: object | None = None,
        progress: ProgressLedger | None = None,
    ) -> CoordinatorExecutionResult:
        """Execute one explicit adapter and release all coordinator-owned reservations."""

        if not isinstance(adapter, LocalAdapter):
            raise ExecutionCoordinatorError("adapter must implement LocalAdapter")
        if not isinstance(request, CoordinatorExecutionRequest):
            raise ExecutionCoordinatorError("request must be CoordinatorExecutionRequest")
        if cancellation is not None and not hasattr(cancellation, "is_cancelled"):
            raise ExecutionCoordinatorError("cancellation must expose is_cancelled")
        if progress is not None and not isinstance(progress, ProgressLedger):
            raise ExecutionCoordinatorError("progress must be ProgressLedger")
        profile_diagnostics = self._validate_profile(request)
        if profile_diagnostics:
            return self._blocked(
                request,
                CoordinatorStatus.UNSUPPORTED,
                "coordinator.profile_capability",
                "selected profile does not support the requested capability",
            )
        if request.progress_required and (
            request.qualification.progress is not ModelCapabilityState.QUALIFIED
        ):
            return self._blocked(
                request,
                CoordinatorStatus.UNSUPPORTED,
                "coordinator.progress_unqualified",
                "requested progress capability is not qualified for this backend",
            )
        if request.cancellation_required and (
            request.qualification.cancellation is not ModelCapabilityState.QUALIFIED
        ):
            return self._blocked(
                request,
                CoordinatorStatus.UNSUPPORTED,
                "coordinator.cancellation_unqualified",
                "requested cancellation capability is not qualified for this backend",
            )
        if (
            request.ownership.has_external_resources
            or request.ownership.has_adapter_owned_resources
        ):
            if lifecycle is None or not isinstance(lifecycle, ResourceLifecycle):
                return self._blocked(
                    request,
                    CoordinatorStatus.UNSUPPORTED,
                    "coordinator.lifecycle_missing",
                    "resource ownership requires an injected lifecycle handle",
                )
        if cancellation is not None and bool(cancellation.is_cancelled()):
            return self._blocked(
                request,
                CoordinatorStatus.CANCELLED,
                "coordinator.cancelled",
                "execution was cancelled before admission",
            )
        if (
            request.adapter_request.device.normalized_kind
            in {
                LocalDeviceKind.CUDA,
                LocalDeviceKind.MPS,
            }
            and request.budget.gpu_slots <= 0
        ):
            return self._blocked(
                request,
                CoordinatorStatus.RESOURCE_LIMIT,
                "coordinator.gpu_budget",
                "GPU device selection requires a positive GPU reservation",
            )
        if request.adapter_request.reference_count > request.budget.max_references:
            return self._blocked(
                request,
                CoordinatorStatus.RESOURCE_LIMIT,
                "coordinator.reference_budget",
                "reference count exceeds coordinator budget",
            )
        capability_diagnostics = validate_local_adapter_capabilities(
            adapter.descriptor, request.adapter_request
        )
        if capability_diagnostics:
            return self._blocked(
                request,
                CoordinatorStatus.UNSUPPORTED,
                capability_diagnostics[0].code,
                "selected adapter does not satisfy its declared capability envelope",
            )
        try:
            reservation = self._pool.reserve(request.budget)
        except ExecutionResourceError:
            return self._blocked(
                request,
                CoordinatorStatus.RESOURCE_LIMIT,
                "coordinator.capacity",
                "finite coordinator capacity is exhausted",
            )

        started = self._clock()
        ledger = progress
        if ledger is None and request.qualification.progress is ModelCapabilityState.QUALIFIED:
            ledger = ProgressLedger(request.operation_id, clock=self._clock)
        if ledger is not None:
            ledger.publish(request.progress_stage, 0, 1, ReliabilityRunState.RUNNING)
        external_before: ExternalResourceObservation | None = None
        cleanup_attempted = False
        cleanup_completed = True
        resource_result: ResourceExecutionResult | None = None
        value: object | None = None
        portable: PortableArtifact | None = None
        diagnostics: list[ValidationDiagnostic] = []
        status = CoordinatorStatus.FAILED
        cache_state = ResourceCacheState.DISABLED
        try:
            if request.ownership.observe_external:
                if lifecycle is None:
                    raise ExecutionCoordinatorError("external lifecycle is missing")
                external_before = lifecycle.observe()
                if external_before.backend_family is not request.ownership.backend_family:
                    raise ExecutionCoordinatorError("external observation backend mismatch")
                if (
                    request.budget.vram_bytes
                    and external_before.vram_bytes > request.budget.vram_bytes
                ):
                    raise ExecutionResourceError(
                        "observed external VRAM exceeds coordinator budget"
                    )

            def worker(context: ResourceExecutionContext) -> ResourceExecutionArtifact:
                try:
                    result = adapter.run(request.adapter_request, context.budget_guard)
                except MemoryError as exc:
                    raise LocalAdapterMemoryError("adapter exceeded memory") from exc
                except TimeoutError as exc:
                    raise LocalAdapterTimeoutError("adapter exceeded wall time") from exc
                if not isinstance(result, LocalAdapterResult):
                    raise LocalAdapterError("adapter returned an invalid result")
                return ResourceExecutionArtifact(
                    value=result,
                    output_bytes=result.output_bytes,
                    output_items=result.output_items,
                )

            resource_request = ResourceExecutionRequest(
                cache_key=request.cache_identity.to_resource_cache_key(),
                budget=request.budget.to_local_budget(),
                device=request.adapter_request.device,
                reference_count=request.adapter_request.reference_count,
                cache_ttl_seconds=request.artifact_policy.ttl_seconds
                if request.artifact_policy.enabled
                else 0.0,
                cache_enabled=request.artifact_policy.enabled,
                cancellation_required=request.cancellation_required,
                input_value=request.adapter_request.input_value,
            )
            resource_result = self.scheduler.execute(
                resource_request,
                worker,
                cancellation_probe=cast(LocalCancellationProbe | None, cancellation),
                progress=ledger,
            )
            cache_state = resource_result.receipt.cache_state
            status = _status_from_resource(resource_result.status)
            if status is CoordinatorStatus.COMPLETE:
                result_value = resource_result.value
                if not isinstance(result_value, LocalAdapterResult):
                    raise ExecutionArtifactError("complete resource result lacks adapter value")
                value = result_value.value
                artifact_fp = _artifact_fingerprint(value, request.cache_identity.fingerprint)
                portable = PortableArtifact(
                    artifact_fp,
                    request.cache_identity.fingerprint,
                    CoordinatorStatus.COMPLETE,
                    request.artifact_policy.scope,
                    result_value.output_bytes,
                    result_value.output_items,
                    request.artifact_policy.ttl_seconds,
                )
            elif resource_result.diagnostics:
                diagnostics.extend(resource_result.diagnostics)
        except ExecutionResourceError as exc:
            status = CoordinatorStatus.RESOURCE_LIMIT
            diagnostics.append(_diagnostic("coordinator.resource_limit", str(exc)))
            value = None
            portable = None
        except (ExecutionCoordinatorError, LocalAdapterError, ContractValidationError) as exc:
            status = CoordinatorStatus.FAILED
            diagnostics.append(_diagnostic("coordinator.execution_failure", str(exc)))
            value = None
            portable = None
        except MemoryError:
            status = CoordinatorStatus.OUT_OF_MEMORY
            diagnostics.append(_diagnostic("coordinator.out_of_memory", "adapter exceeded memory"))
            value = None
            portable = None
        except TimeoutError:
            status = CoordinatorStatus.TIMED_OUT
            diagnostics.append(_diagnostic("coordinator.timeout", "adapter exceeded wall time"))
            value = None
            portable = None
        finally:
            if request.ownership.has_adapter_owned_resources:
                cleanup_attempted = True
                try:
                    if lifecycle is None:
                        raise ExecutionCoordinatorError("adapter-owned lifecycle is missing")
                    lifecycle.close_owned()
                except Exception:
                    cleanup_completed = False
                    status = CoordinatorStatus.FAILED
                    value = None
                    portable = None
                    diagnostics.append(
                        _diagnostic("coordinator.cleanup_failure", "owned resource cleanup failed")
                    )
            if request.ownership.observe_external and lifecycle is not None:
                try:
                    lifecycle.observe()
                except Exception:
                    diagnostics.append(
                        _diagnostic(
                            "coordinator.external_observation_failure",
                            "external resource observation failed",
                            ValidationSeverity.WARNING,
                        )
                    )
            reservation.release()

        if status is CoordinatorStatus.COMPLETE and ledger is not None:
            ledger.publish(request.progress_stage, 1, 1, ReliabilityRunState.COMPLETED)
        elif status is CoordinatorStatus.CANCELLED and ledger is not None:
            ledger.publish(request.progress_stage, 0, 1, ReliabilityRunState.CANCELLED)
        elif status is not CoordinatorStatus.COMPLETE and ledger is not None:
            ledger.publish(request.progress_stage, 0, 1, ReliabilityRunState.FAILED)
        elapsed_value = self._clock() - started
        elapsed = float(elapsed_value) if isinstance(elapsed_value, (int, float)) else 0.0
        if not isfinite(elapsed) or elapsed < 0:
            elapsed = 0.0
        receipt = CoordinatorReceipt(
            request.operation_id,
            status,
            cache_state,
            request.profile,
            request.qualification.backend_family,
            elapsed,
            True,
            cleanup_attempted,
            cleanup_completed,
            external_before is not None,
            request.qualification.progress,
            request.qualification.cancellation,
            portable,
            tuple(diagnostics),
        )
        return CoordinatorExecutionResult(
            receipt,
            value,
            resource_result,
            () if ledger is None else ledger.events,
        )

    def delete_cached(
        self, identity: CoordinatorCacheIdentity, policy: ArtifactCachePolicy
    ) -> bool:
        """Delete a content-bearing cache entry only with the same explicit policy scope."""

        if not isinstance(identity, CoordinatorCacheIdentity):
            raise ExecutionArtifactError("cache identity is invalid")
        if not isinstance(policy, ArtifactCachePolicy) or not policy.enabled:
            raise ExecutionArtifactError("deletion requires an enabled artifact cache policy")
        if policy.scope_fingerprint != identity.scope_fingerprint:
            raise ExecutionArtifactError("cache deletion scope does not match identity")
        return self.scheduler.invalidate(identity.to_resource_cache_key())


__all__ = [
    "ARTIFACT_CACHE_SCOPE",  # compatibility alias populated below
    "EXECUTION_COORDINATOR_SCHEMA",
    "PROFILE_CAPABILITIES",
    "ArtifactCachePolicy",
    "ArtifactCacheScope",
    "BackendRuntimeQualification",
    "CoordinatorCacheIdentity",
    "CoordinatorCapability",
    "CoordinatorCapacity",
    "CoordinatorExecutionRequest",
    "CoordinatorExecutionResult",
    "CoordinatorProfile",
    "CoordinatorReceipt",
    "CoordinatorResourceBudget",
    "CoordinatorStatus",
    "ExecutionCoordinator",
    "ExternalResourceObservation",
    "OwnershipKind",
    "PortableArtifact",
    "ResourceLifecycle",
    "ResourceOwnershipPolicy",
]

ARTIFACT_CACHE_SCOPE = ArtifactCacheScope
