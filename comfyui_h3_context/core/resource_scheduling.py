"""Pure-core resource scheduling, cache identity, and cancellation contracts.

The scheduler owns only bounded in-memory reservations and metadata. Runtime workers are injected
callables; they receive checkpoints and may return only explicit complete, partial, or stale
artifacts. No media, provider, filesystem, or model runtime is imported here.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from typing import Protocol, runtime_checkable

from .canonical import canonical_fingerprint
from .contracts import ValidationDiagnostic, ValidationSeverity
from .errors import (
    ContractValidationError,
    LocalAdapterBudgetError,
    LocalAdapterCancelledError,
    LocalAdapterConcurrencyError,
    LocalAdapterError,
    LocalAdapterMemoryError,
    LocalAdapterTimeoutError,
    ResourceSchedulingError,
)
from .local_adapters import (
    LocalBudgetGuard,
    LocalCancellationProbe,
    LocalDeviceSpec,
    LocalResourceBudget,
)

RESOURCE_RUNTIME_SCHEMA = "h3.resource.runtime.v1"
MAX_RESOURCE_CACHE_ENTRIES = 256
MAX_RESOURCE_CACHE_BYTES = 512 * 1024 * 1024
MAX_RESOURCE_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_RESOURCE_REASON_LENGTH = 1024

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SENSITIVE_MARKERS = (
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "private",
    "secret",
    "signed",
    "token",
    "url",
    "path",
)


class ResourceCacheState(str, Enum):
    """Observable cache lookup state for one execution."""

    DISABLED = "disabled"
    MISS = "miss"
    HIT = "hit"
    STALE = "stale"
    NOT_WRITTEN = "not_written"


class ResourceExecutionStatus(str, Enum):
    """Terminal status; only COMPLETE is renderable/cacheable."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    STALE = "stale"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    MEMORY_EXCEEDED = "memory_exceeded"
    BUDGET_EXCEEDED = "budget_exceeded"
    CONCURRENCY_EXCEEDED = "concurrency_exceeded"
    FAILED = "failed"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ResourceSchedulingError(f"{field_name} must be a bounded identifier")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise ResourceSchedulingError(f"{field_name} contains a sensitive marker")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise ResourceSchedulingError(f"{field_name} must be a lowercase SHA-256 fingerprint")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResourceSchedulingError(f"{field_name} must be a non-negative integer")
    return value


def _positive_int(value: object, field_name: str) -> int:
    result = _non_negative_int(value, field_name)
    if result <= 0:
        raise ResourceSchedulingError(f"{field_name} must be positive")
    return result


def _bounded_float(value: object, field_name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResourceSchedulingError(f"{field_name} must be a finite number")
    result = float(value)
    if not isfinite(result) or (result < 0 if allow_zero else result <= 0):
        raise ResourceSchedulingError(
            f"{field_name} must be finite and {'non-negative' if allow_zero else 'positive'}"
        )
    return result


def _reason(value: object, field_name: str = "reason") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_RESOURCE_REASON_LENGTH:
        raise ResourceSchedulingError(
            f"{field_name} must be a bounded non-empty string when supplied"
        )
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ResourceSchedulingError(f"{field_name} contains an unsafe wire code point")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise ResourceSchedulingError(f"{field_name} contains a sensitive marker")
    return value


@dataclass(frozen=True, slots=True)
class ResourceCacheKey:
    """Stable cache identity with no raw content or locator fields."""

    operation_id: str
    contract_schema: str
    provider_id: str
    provider_version: str
    settings_fingerprint: str
    source_fingerprints: tuple[str, ...]
    schema: str = RESOURCE_RUNTIME_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "operation_id")
        _identifier(self.contract_schema, "contract_schema")
        _identifier(self.provider_id, "provider_id")
        _identifier(self.provider_version, "provider_version")
        _fingerprint(self.settings_fingerprint, "settings_fingerprint")
        if (
            not isinstance(self.source_fingerprints, tuple)
            or not self.source_fingerprints
            or len(self.source_fingerprints) > 512
        ):
            raise ResourceSchedulingError("source_fingerprints must contain one to 512 values")
        values = tuple(
            _fingerprint(value, "source_fingerprint") for value in self.source_fingerprints
        )
        if len(values) != len(set(values)):
            raise ResourceSchedulingError("source_fingerprints must not contain duplicates")
        object.__setattr__(self, "source_fingerprints", values)
        if self.schema != RESOURCE_RUNTIME_SCHEMA:
            raise ResourceSchedulingError("unsupported resource runtime schema")

    @property
    def cache_key(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "operation_id": self.operation_id,
            "contract_schema": self.contract_schema,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "settings_fingerprint": self.settings_fingerprint,
            "source_fingerprints": list(self.source_fingerprints),
        }


@dataclass(frozen=True, slots=True)
class ResourceExecutionRequest:
    """Explicit worker input metadata; ``input_value`` never enters wire/cache identity."""

    cache_key: ResourceCacheKey
    budget: LocalResourceBudget
    device: LocalDeviceSpec = field(default_factory=LocalDeviceSpec)
    reference_count: int = 0
    cache_ttl_seconds: float = 3600.0
    cache_enabled: bool = True
    cancellation_required: bool = False
    input_value: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.cache_key, ResourceCacheKey):
            raise ResourceSchedulingError("cache_key must be a ResourceCacheKey")
        if not isinstance(self.budget, LocalResourceBudget):
            raise ResourceSchedulingError("budget must be a LocalResourceBudget")
        if not isinstance(self.device, LocalDeviceSpec):
            raise ResourceSchedulingError("device must be a LocalDeviceSpec")
        if isinstance(self.reference_count, bool) or not isinstance(self.reference_count, int):
            raise ResourceSchedulingError("reference_count must be a non-negative integer")
        if self.reference_count < 0 or self.reference_count > self.budget.max_references:
            raise ResourceSchedulingError("reference_count exceeds the resource budget")
        ttl = _bounded_float(
            self.cache_ttl_seconds, "cache_ttl_seconds", allow_zero=not self.cache_enabled
        )
        if ttl > MAX_RESOURCE_TTL_SECONDS:
            raise ResourceSchedulingError("cache_ttl_seconds exceeds its finite bound")
        object.__setattr__(self, "cache_ttl_seconds", ttl)
        if not isinstance(self.cache_enabled, bool):
            raise ResourceSchedulingError("cache_enabled must be a boolean")
        if not isinstance(self.cancellation_required, bool):
            raise ResourceSchedulingError("cancellation_required must be a boolean")

    @property
    def operation_id(self) -> str:
        return self.cache_key.operation_id

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": RESOURCE_RUNTIME_SCHEMA,
            "cache_key": self.cache_key.to_wire(),
            "budget": self.budget.to_public_dict(),
            "device": self.device.to_wire(),
            "reference_count": self.reference_count,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "cache_enabled": self.cache_enabled,
            "cancellation_required": self.cancellation_required,
        }


@dataclass(frozen=True, slots=True)
class ResourceExecutionArtifact:
    """Worker output whose status explicitly prevents partial/stale success."""

    value: object = field(repr=False, compare=False)
    status: ResourceExecutionStatus = ResourceExecutionStatus.COMPLETE
    output_bytes: int = 0
    output_items: int = 0
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {
            ResourceExecutionStatus.COMPLETE,
            ResourceExecutionStatus.PARTIAL,
            ResourceExecutionStatus.STALE,
        }:
            raise ResourceSchedulingError(
                "worker artifacts may only be COMPLETE, PARTIAL, or STALE"
            )
        _non_negative_int(self.output_bytes, "output_bytes")
        _non_negative_int(self.output_items, "output_items")
        _reason(self.reason)

    @property
    def complete(self) -> bool:
        return self.status is ResourceExecutionStatus.COMPLETE


@dataclass(frozen=True, slots=True)
class ResourceExecutionReceipt:
    """Redacted resource and cleanup evidence for one execution."""

    operation_id: str
    status: ResourceExecutionStatus
    cache_state: ResourceCacheState
    elapsed_seconds: float
    peak_memory_bytes: int
    output_bytes: int
    output_items: int
    reservation_released: bool
    cache_stored: bool
    worker_started: bool
    schema: str = RESOURCE_RUNTIME_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "receipt operation_id")
        if not isinstance(self.status, ResourceExecutionStatus):
            raise ResourceSchedulingError("receipt status must be a ResourceExecutionStatus")
        if not isinstance(self.cache_state, ResourceCacheState):
            raise ResourceSchedulingError("receipt cache_state must be a ResourceCacheState")
        _bounded_float(self.elapsed_seconds, "elapsed_seconds", allow_zero=True)
        _non_negative_int(self.peak_memory_bytes, "peak_memory_bytes")
        _non_negative_int(self.output_bytes, "output_bytes")
        _non_negative_int(self.output_items, "output_items")
        for value, field_name in (
            (self.reservation_released, "reservation_released"),
            (self.cache_stored, "cache_stored"),
            (self.worker_started, "worker_started"),
        ):
            if not isinstance(value, bool):
                raise ResourceSchedulingError(f"{field_name} must be a boolean")
        if self.status is not ResourceExecutionStatus.COMPLETE and self.cache_stored:
            raise ResourceSchedulingError("only complete results may be cached")
        if self.schema != RESOURCE_RUNTIME_SCHEMA:
            raise ResourceSchedulingError("unsupported resource runtime schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "operation_id": self.operation_id,
            "status": self.status.value,
            "cache_state": self.cache_state.value,
            "elapsed_seconds": self.elapsed_seconds,
            "peak_memory_bytes": self.peak_memory_bytes,
            "output_bytes": self.output_bytes,
            "output_items": self.output_items,
            "reservation_released": self.reservation_released,
            "cache_stored": self.cache_stored,
            "worker_started": self.worker_started,
        }


@dataclass(frozen=True, slots=True)
class ResourceExecutionResult:
    """Safe result envelope; runtime values remain available only in memory."""

    receipt: ResourceExecutionReceipt
    artifact: ResourceExecutionArtifact | None = field(default=None, repr=False, compare=False)
    diagnostics: tuple[ValidationDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, ResourceExecutionReceipt):
            raise ResourceSchedulingError("receipt must be a ResourceExecutionReceipt")
        if self.artifact is not None and not isinstance(self.artifact, ResourceExecutionArtifact):
            raise ResourceSchedulingError("artifact must be a ResourceExecutionArtifact or None")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(item, ValidationDiagnostic) for item in self.diagnostics
        ):
            raise ResourceSchedulingError("diagnostics must contain ValidationDiagnostic values")
        if self.receipt.status is ResourceExecutionStatus.COMPLETE:
            if self.artifact is None or not self.artifact.complete:
                raise ResourceSchedulingError("complete result requires a complete artifact")
        elif self.artifact is not None and self.artifact.status is not self.receipt.status:
            raise ResourceSchedulingError("artifact and receipt statuses must match")

    @property
    def status(self) -> ResourceExecutionStatus:
        return self.receipt.status

    @property
    def complete(self) -> bool:
        return self.status is ResourceExecutionStatus.COMPLETE

    @property
    def value(self) -> object | None:
        return None if self.artifact is None else self.artifact.value

    @property
    def has_errors(self) -> bool:
        return any(
            item.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
            for item in self.diagnostics
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": RESOURCE_RUNTIME_SCHEMA,
            "status": self.status.value,
            "complete": self.complete,
            "receipt": self.receipt.to_wire(),
            "diagnostics": [item.to_wire() for item in self.diagnostics],
        }


class ResourceCancellationToken:
    """Thread-safe explicit cancellation signal for one worker execution."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: str | None = None
        self._lock = threading.Lock()

    def cancel(self, reason: str | None = None) -> None:
        safe_reason = _reason(reason, "cancellation reason")
        with self._lock:
            if self._reason is None:
                self._reason = safe_reason
            self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason


class ResourceExecutionContext:
    """Worker-facing checkpoint and output-budget facade."""

    def __init__(
        self,
        request: ResourceExecutionRequest,
        guard: LocalBudgetGuard,
        peak_meter: _PeakMemoryMeter,
        progress: ResourceProgressSink | None = None,
    ) -> None:
        self.request = request
        self._guard = guard
        self._peak_meter = peak_meter
        self._progress = progress

    @property
    def input_value(self) -> object:
        return self.request.input_value

    @property
    def device(self) -> LocalDeviceSpec:
        return self.request.device

    @property
    def reference_count(self) -> int:
        return self.request.reference_count

    def checkpoint(self) -> None:
        self._guard.checkpoint()

    @property
    def budget_guard(self) -> LocalBudgetGuard:
        """Expose the scheduler-owned guard to an injected adapter without exposing internals."""

        return self._guard

    def record_output(self, *, byte_count: int, item_count: int = 1) -> None:
        self._guard.record_output(byte_count=byte_count, item_count=item_count)

    def report_progress(
        self,
        stage: object,
        completed_units: int,
        total_units: int,
        state: object = "running",
        message: str | None = None,
    ) -> None:
        """Publish one optional bounded progress event without weakening checkpoint enforcement."""

        if self._progress is not None:
            self._progress.publish(stage, completed_units, total_units, state, message)

    @property
    def elapsed_seconds(self) -> float:
        return self._guard.elapsed_seconds

    @property
    def peak_memory_bytes(self) -> int:
        return self._peak_meter.peak


@runtime_checkable
class ResourceWorker(Protocol):
    def __call__(self, context: ResourceExecutionContext) -> ResourceExecutionArtifact:
        """Execute bounded work and return an explicit artifact."""


@runtime_checkable
class ResourceProgressSink(Protocol):
    """Structural progress sink used by reliability-aware workers."""

    def publish(
        self,
        stage: object,
        completed_units: int,
        total_units: int,
        state: object = "running",
        message: str | None = None,
    ) -> object:
        """Record one progress event or raise a typed bounded-state error."""


class _PeakMemoryMeter:
    def __init__(self, meter: Callable[[], int] | None) -> None:
        self._meter = meter
        self.peak = 0

    def __call__(self) -> int:
        if self._meter is None:
            return 0
        value = self._meter()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContractValidationError("memory_meter must return a non-negative integer")
        self.peak = max(self.peak, value)
        return value


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    artifact: ResourceExecutionArtifact
    created_at: float
    size_bytes: int


class _ResourceLease:
    def __init__(self, scheduler: ResourceScheduler, operation_id: str) -> None:
        self._scheduler = scheduler
        self._operation_id = operation_id
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._scheduler._release(self._operation_id)


class ResourceScheduler:
    """Thread-safe bounded scheduler with deterministic in-memory cache eviction."""

    def __init__(
        self,
        *,
        max_concurrency: int = 1,
        max_cache_entries: int = 64,
        max_cache_bytes: int = 64 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
        memory_meter: Callable[[], int] | None = None,
    ) -> None:
        self._max_concurrency = _positive_int(max_concurrency, "max_concurrency")
        self._max_cache_entries = min(
            _positive_int(max_cache_entries, "max_cache_entries"), MAX_RESOURCE_CACHE_ENTRIES
        )
        self._max_cache_bytes = min(
            _positive_int(max_cache_bytes, "max_cache_bytes"), MAX_RESOURCE_CACHE_BYTES
        )
        if not callable(clock):
            raise ResourceSchedulingError("clock must be callable")
        if memory_meter is not None and not callable(memory_meter):
            raise ResourceSchedulingError("memory_meter must be callable")
        self._clock = clock
        self._memory_meter = memory_meter
        self._lock = threading.RLock()
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._cache_bytes = 0
        self._active = 0
        self._active_by_operation: dict[str, int] = {}
        self._peak_active = 0

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    @property
    def peak_active(self) -> int:
        with self._lock:
            return self._peak_active

    @property
    def cache_size(self) -> int:
        with self._lock:
            return len(self._cache)

    @property
    def cache_bytes(self) -> int:
        with self._lock:
            return self._cache_bytes

    def _finite_clock(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ResourceSchedulingError("clock must return a finite number")
        result = float(value)
        if not isfinite(result):
            raise ResourceSchedulingError("clock must return a finite number")
        return result

    def _reserve(self, operation_id: str, operation_limit: int) -> _ResourceLease:
        with self._lock:
            if self._active >= self._max_concurrency:
                raise LocalAdapterConcurrencyError("global resource concurrency budget exceeded")
            if self._active_by_operation.get(operation_id, 0) >= operation_limit:
                raise LocalAdapterConcurrencyError(
                    "per-operation resource concurrency budget exceeded"
                )
            self._active += 1
            self._active_by_operation[operation_id] = (
                self._active_by_operation.get(operation_id, 0) + 1
            )
            self._peak_active = max(self._peak_active, self._active)
        return _ResourceLease(self, operation_id)

    def _release(self, operation_id: str) -> None:
        with self._lock:
            if self._active <= 0:
                raise ResourceSchedulingError("resource reservation underflow")
            self._active -= 1
            count = self._active_by_operation.get(operation_id, 0)
            if count <= 1:
                self._active_by_operation.pop(operation_id, None)
            else:
                self._active_by_operation[operation_id] = count - 1

    def _cache_lookup(
        self, key: ResourceCacheKey, ttl: float, now: float
    ) -> tuple[ResourceExecutionArtifact | None, ResourceCacheState]:
        with self._lock:
            entry = self._cache.get(key.cache_key)
            if entry is None:
                return None, ResourceCacheState.MISS
            if now < entry.created_at or now - entry.created_at > ttl:
                self._cache.pop(key.cache_key, None)
                self._cache_bytes -= entry.size_bytes
                return None, ResourceCacheState.STALE
            self._cache.move_to_end(key.cache_key)
            return entry.artifact, ResourceCacheState.HIT

    def _cache_store(
        self, key: ResourceCacheKey, artifact: ResourceExecutionArtifact, now: float
    ) -> bool:
        size = max(1, artifact.output_bytes)
        if size > self._max_cache_bytes:
            return False
        with self._lock:
            previous = self._cache.pop(key.cache_key, None)
            if previous is not None:
                self._cache_bytes -= previous.size_bytes
            self._cache[key.cache_key] = _CacheEntry(artifact, now, size)
            self._cache_bytes += size
            while (
                len(self._cache) > self._max_cache_entries
                or self._cache_bytes > self._max_cache_bytes
            ):
                _, evicted = self._cache.popitem(last=False)
                self._cache_bytes -= evicted.size_bytes
            return key.cache_key in self._cache

    def invalidate(self, key: ResourceCacheKey) -> bool:
        """Delete one cache entry by its complete content-addressed key."""

        if not isinstance(key, ResourceCacheKey):
            raise ResourceSchedulingError("cache invalidation requires a ResourceCacheKey")
        with self._lock:
            entry = self._cache.pop(key.cache_key, None)
            if entry is None:
                return False
            self._cache_bytes -= entry.size_bytes
            return True

    @staticmethod
    def _diagnostic(
        code: str, message: str, severity: ValidationSeverity = ValidationSeverity.ERROR
    ) -> ValidationDiagnostic:
        return ValidationDiagnostic(severity, code, message, "resource_scheduler")

    def _terminal(
        self,
        request: ResourceExecutionRequest,
        status: ResourceExecutionStatus,
        cache_state: ResourceCacheState,
        code: str,
        message: str,
        *,
        worker_started: bool = False,
        elapsed_seconds: float = 0.0,
        peak_memory_bytes: int = 0,
    ) -> ResourceExecutionResult:
        receipt = ResourceExecutionReceipt(
            request.operation_id,
            status,
            cache_state,
            elapsed_seconds,
            peak_memory_bytes,
            0,
            0,
            True,
            False,
            worker_started,
        )
        return ResourceExecutionResult(
            receipt,
            None,
            (self._diagnostic(code, message),),
        )

    def execute(
        self,
        request: ResourceExecutionRequest,
        worker: ResourceWorker,
        *,
        cancellation_probe: LocalCancellationProbe | None = None,
        progress: ResourceProgressSink | None = None,
    ) -> ResourceExecutionResult:
        """Run one explicitly bounded worker and always release its reservation."""

        if not isinstance(request, ResourceExecutionRequest):
            raise ResourceSchedulingError("request must be a ResourceExecutionRequest")
        if not isinstance(worker, ResourceWorker):
            raise ResourceSchedulingError("worker must implement ResourceWorker")
        if request.cancellation_required and cancellation_probe is None:
            raise ResourceSchedulingError("request requires an explicit cancellation probe")
        if cancellation_probe is not None and not isinstance(
            cancellation_probe, LocalCancellationProbe
        ):
            raise ResourceSchedulingError(
                "cancellation_probe must implement LocalCancellationProbe"
            )
        if progress is not None and not isinstance(progress, ResourceProgressSink):
            raise ResourceSchedulingError("progress must implement ResourceProgressSink")
        if cancellation_probe is not None and cancellation_probe.is_cancelled():
            return self._terminal(
                request,
                ResourceExecutionStatus.CANCELLED,
                ResourceCacheState.DISABLED
                if not request.cache_enabled
                else ResourceCacheState.MISS,
                "resource.cancelled",
                "resource execution was cancelled before worker start",
            )

        cache_state = ResourceCacheState.DISABLED
        if request.cache_enabled:
            cached, cache_state = self._cache_lookup(
                request.cache_key, request.cache_ttl_seconds, self._finite_clock()
            )
            if cached is not None:
                receipt = ResourceExecutionReceipt(
                    request.operation_id,
                    ResourceExecutionStatus.COMPLETE,
                    ResourceCacheState.HIT,
                    0.0,
                    0,
                    cached.output_bytes,
                    cached.output_items,
                    True,
                    False,
                    False,
                )
                return ResourceExecutionResult(receipt, cached)

        try:
            lease = self._reserve(request.operation_id, request.budget.max_concurrency)
        except LocalAdapterConcurrencyError:
            return self._terminal(
                request,
                ResourceExecutionStatus.CONCURRENCY_EXCEEDED,
                cache_state,
                "resource.concurrency_limit",
                "resource concurrency budget was exhausted",
            )

        tracker = _PeakMemoryMeter(self._memory_meter)
        guard: LocalBudgetGuard | None = None
        worker_started = False
        artifact: ResourceExecutionArtifact | None = None
        status = ResourceExecutionStatus.FAILED
        diagnostics: tuple[ValidationDiagnostic, ...] = ()
        stored = False
        started_at = self._finite_clock()
        try:
            guard = LocalBudgetGuard(
                request.budget,
                cancellation_probe=cancellation_probe,
                clock=self._clock,
                memory_meter=tracker,
            )
            context = ResourceExecutionContext(request, guard, tracker, progress)
            worker_started = True
            context.checkpoint()
            artifact = worker(context)
            context.checkpoint()
            if not isinstance(artifact, ResourceExecutionArtifact):
                raise ResourceSchedulingError("worker returned an invalid resource artifact")
            if (
                artifact.output_bytes > request.budget.max_output_bytes
                or artifact.output_items > request.budget.max_output_items
            ):
                raise LocalAdapterBudgetError("worker artifact exceeds output budget")
            status = artifact.status
            if artifact.complete:
                stored = request.cache_enabled and self._cache_store(
                    request.cache_key, artifact, self._finite_clock()
                )
            else:
                diagnostics = (
                    self._diagnostic(
                        "resource.non_complete_result",
                        "worker returned an explicit partial or stale artifact",
                        ValidationSeverity.WARNING,
                    ),
                )
        except LocalAdapterCancelledError:
            status = ResourceExecutionStatus.CANCELLED
            diagnostics = (
                self._diagnostic("resource.cancelled", "resource execution was cancelled"),
            )
            artifact = None
        except LocalAdapterTimeoutError:
            status = ResourceExecutionStatus.TIMED_OUT
            diagnostics = (
                self._diagnostic("resource.timeout", "resource wall-time budget was exceeded"),
            )
            artifact = None
        except LocalAdapterMemoryError:
            status = ResourceExecutionStatus.MEMORY_EXCEEDED
            diagnostics = (
                self._diagnostic("resource.memory_limit", "resource memory budget was exceeded"),
            )
            artifact = None
        except LocalAdapterBudgetError:
            status = ResourceExecutionStatus.BUDGET_EXCEEDED
            diagnostics = (
                self._diagnostic("resource.output_limit", "resource output budget was exceeded"),
            )
            artifact = None
        except (LocalAdapterError, ContractValidationError, ResourceSchedulingError):
            status = ResourceExecutionStatus.FAILED
            diagnostics = (
                self._diagnostic("resource.worker_failure", "resource worker failed validation"),
            )
            artifact = None
        except Exception:
            status = ResourceExecutionStatus.FAILED
            diagnostics = (self._diagnostic("resource.worker_failure", "resource worker failed"),)
            artifact = None
        finally:
            lease.release()

        elapsed = 0.0 if guard is None else max(0.0, self._finite_clock() - started_at)
        receipt = ResourceExecutionReceipt(
            request.operation_id,
            status,
            cache_state if not stored else cache_state,
            elapsed,
            tracker.peak,
            0 if artifact is None else artifact.output_bytes,
            0 if artifact is None else artifact.output_items,
            True,
            stored,
            worker_started,
        )
        return ResourceExecutionResult(receipt, artifact, diagnostics)


__all__ = [
    "MAX_RESOURCE_CACHE_BYTES",
    "MAX_RESOURCE_CACHE_ENTRIES",
    "MAX_RESOURCE_REASON_LENGTH",
    "MAX_RESOURCE_TTL_SECONDS",
    "RESOURCE_RUNTIME_SCHEMA",
    "ResourceCacheKey",
    "ResourceCacheState",
    "ResourceCancellationToken",
    "ResourceExecutionArtifact",
    "ResourceExecutionContext",
    "ResourceExecutionReceipt",
    "ResourceExecutionRequest",
    "ResourceExecutionResult",
    "ResourceExecutionStatus",
    "ResourceProgressSink",
    "ResourceScheduler",
    "ResourceWorker",
]
