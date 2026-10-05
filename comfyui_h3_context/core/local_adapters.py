"""Explicit, lazy, and resource-bounded local adapter contracts.

The pure core declares what an optional local perception or reasoning adapter may do, but never
imports a model/runtime package or discovers an adapter implicitly. A caller selects one catalog
entry, resolves it lazily, and executes it through :func:`run_local_adapter`. Concrete adapters own
their model/runtime operations and must call the supplied budget guard at safe checkpoints.
"""

from __future__ import annotations

import importlib
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from typing import Protocol, TypeVar, runtime_checkable

from .contracts import MediaKind, TaskMode, ValidationDiagnostic, ValidationSeverity
from .errors import (
    ContractValidationError,
    LocalAdapterBudgetError,
    LocalAdapterCancelledError,
    LocalAdapterCapabilityError,
    LocalAdapterConcurrencyError,
    LocalAdapterError,
    LocalAdapterMemoryError,
    LocalAdapterTimeoutError,
    LocalAdapterUnavailableError,
    LocalAdapterVersionError,
)

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_VERSION_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}\Z")
_DEPENDENCY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_MODULE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_ATTRIBUTE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SENSITIVE_MARKERS = (
    "api_key",
    "authorization",
    "credential",
    "password",
    "path",
    "secret",
    "signed",
    "token",
    "url",
)
_EnumT = TypeVar("_EnumT", bound=Enum)
LOCAL_ADAPTER_CONTRACT_SCHEMA = "h3.local.adapter.v1"


class LocalAdapterKind(str, Enum):
    """High-level local assistance family advertised by an adapter."""

    PERCEPTION = "perception"
    REASONING = "reasoning"


class LocalDeviceKind(str, Enum):
    """Explicit device choices; no device fallback is implicit."""

    AUTO = "auto"
    CPU = "cpu"
    CUDA = "cuda"
    MPS = "mps"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a bounded identifier")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise ContractValidationError(f"{field_name} must not contain sensitive markers")
    return value


def _version(value: object, field_name: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or _VERSION_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a numeric version")
    parts = tuple(int(part) for part in value.split("."))
    if any(part < 0 for part in parts):
        raise ContractValidationError(f"{field_name} must not contain negative components")
    padded = parts + (0, 0, 0)
    return padded[0], padded[1], padded[2]


def _enum_set(value: object, expected: type[_EnumT], field_name: str) -> frozenset[_EnumT]:
    if (
        not isinstance(value, frozenset)
        or not value
        or not all(isinstance(item, expected) for item in value)
    ):
        raise ContractValidationError(f"{field_name} must be a non-empty frozenset")
    if len(value) > 32:
        raise ContractValidationError(f"{field_name} is too large")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractValidationError(f"{field_name} must be a non-negative integer")
    return value


def _positive_int(value: object, field_name: str) -> int:
    integer = _non_negative_int(value, field_name)
    if integer <= 0:
        raise ContractValidationError(f"{field_name} must be positive")
    return integer


def _positive_float(value: object, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(float(value))
        or float(value) <= 0
    ):
        raise ContractValidationError(f"{field_name} must be a finite positive number")
    return float(value)


@dataclass(frozen=True, slots=True)
class LocalDeviceSpec:
    """Runtime device selection, represented without importing a device framework."""

    kind: LocalDeviceKind | str = LocalDeviceKind.AUTO
    index: int | None = None

    def __post_init__(self) -> None:
        try:
            kind = (
                self.kind if isinstance(self.kind, LocalDeviceKind) else LocalDeviceKind(self.kind)
            )
        except (TypeError, ValueError):
            raise ContractValidationError("device kind is unsupported") from None
        if self.index is not None and (
            isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0
        ):
            raise ContractValidationError("device index must be a non-negative integer")
        if kind in {LocalDeviceKind.CPU, LocalDeviceKind.MPS, LocalDeviceKind.AUTO} and self.index:
            raise ContractValidationError("selected device kind does not accept a non-zero index")
        object.__setattr__(self, "kind", kind)

    @property
    def normalized_kind(self) -> LocalDeviceKind:
        """Return the validated enum even when a wire string was supplied."""

        return self.kind if isinstance(self.kind, LocalDeviceKind) else LocalDeviceKind(self.kind)

    def to_public_dict(self) -> dict[str, object]:
        return {"device": self.normalized_kind.value, "index": self.index}

    def to_wire(self) -> str:
        kind = self.normalized_kind.value
        return kind if self.index is None else f"{kind}:{self.index}"


@dataclass(frozen=True, slots=True)
class LocalResourceBudget:
    """Finite per-execution resource limits for a local adapter."""

    max_memory_bytes: int
    max_wall_time_seconds: float
    max_references: int
    max_output_bytes: int
    max_output_items: int
    max_concurrency: int

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.max_memory_bytes, "max_memory_bytes"),
            (self.max_output_bytes, "max_output_bytes"),
            (self.max_output_items, "max_output_items"),
            (self.max_concurrency, "max_concurrency"),
        ):
            _positive_int(value, field_name)
        _non_negative_int(self.max_references, "max_references")
        _positive_float(self.max_wall_time_seconds, "max_wall_time_seconds")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "max_memory_bytes": self.max_memory_bytes,
            "max_wall_time_seconds": self.max_wall_time_seconds,
            "max_references": self.max_references,
            "max_output_bytes": self.max_output_bytes,
            "max_output_items": self.max_output_items,
            "max_concurrency": self.max_concurrency,
        }


@dataclass(frozen=True, slots=True)
class LocalAdapterDescriptor:
    """Versioned capability declaration for one explicitly selected local adapter."""

    adapter_id: str
    kind: LocalAdapterKind
    adapter_version: str
    minimum_version: str
    maximum_version: str
    supported_task_modes: frozenset[TaskMode]
    supported_media: frozenset[MediaKind]
    supported_devices: frozenset[LocalDeviceKind]
    optional_dependencies: tuple[str, ...]
    output_schema: str
    limits: LocalResourceBudget
    supports_determinism: bool
    supports_seed: bool
    supports_cancellation: bool

    def __post_init__(self) -> None:
        _identifier(self.adapter_id, "adapter_id")
        if not isinstance(self.kind, LocalAdapterKind):
            raise ContractValidationError("kind must be a LocalAdapterKind")
        current = _version(self.adapter_version, "adapter_version")
        minimum = _version(self.minimum_version, "minimum_version")
        maximum = _version(self.maximum_version, "maximum_version")
        if minimum > maximum or not minimum <= current <= maximum:
            raise ContractValidationError(
                "adapter_version must be inside its declared version range"
            )
        _enum_set(self.supported_task_modes, TaskMode, "supported_task_modes")
        if not isinstance(self.supported_media, frozenset) or not all(
            isinstance(item, MediaKind) for item in self.supported_media
        ):
            raise ContractValidationError("supported_media must be a frozenset of MediaKind")
        _enum_set(self.supported_devices, LocalDeviceKind, "supported_devices")
        if (
            not isinstance(self.optional_dependencies, tuple)
            or len(self.optional_dependencies) > 32
        ):
            raise ContractValidationError("optional_dependencies must be a bounded tuple")
        dependencies: list[str] = []
        for dependency in self.optional_dependencies:
            if (
                not isinstance(dependency, str)
                or _DEPENDENCY_PATTERN.fullmatch(dependency) is None
                or any(marker in dependency.casefold() for marker in _SENSITIVE_MARKERS)
            ):
                raise ContractValidationError("optional dependency labels must be safe identifiers")
            dependencies.append(dependency)
        if len(dependencies) != len(set(dependencies)):
            raise ContractValidationError("optional_dependencies must not contain duplicates")
        _identifier(self.output_schema, "output_schema")
        if not isinstance(self.limits, LocalResourceBudget):
            raise ContractValidationError("limits must be a LocalResourceBudget")
        for value, field_name in (
            (self.supports_determinism, "supports_determinism"),
            (self.supports_seed, "supports_seed"),
            (self.supports_cancellation, "supports_cancellation"),
        ):
            if not isinstance(value, bool):
                raise ContractValidationError(f"{field_name} must be a boolean")
        if self.supports_seed and not self.supports_determinism:
            raise ContractValidationError("supports_seed requires supports_determinism")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "kind": self.kind.value,
            "adapter_version": self.adapter_version,
            "minimum_version": self.minimum_version,
            "maximum_version": self.maximum_version,
            "supported_task_modes": sorted(mode.value for mode in self.supported_task_modes),
            "supported_media": sorted(media.value for media in self.supported_media),
            "supported_devices": sorted(device.value for device in self.supported_devices),
            "optional_dependencies": list(self.optional_dependencies),
            "output_schema": self.output_schema,
            "limits": self.limits.to_public_dict(),
            "supports_determinism": self.supports_determinism,
            "supports_seed": self.supports_seed,
            "supports_cancellation": self.supports_cancellation,
        }


@dataclass(frozen=True, slots=True)
class LocalAdapterExecutionRequest:
    """Bounded execution requirements; ``input_value`` stays runtime-only."""

    adapter_id: str
    task_mode: TaskMode
    media_kinds: tuple[MediaKind, ...] = ()
    reference_count: int = 0
    device: LocalDeviceSpec = field(default_factory=LocalDeviceSpec)
    estimated_memory_bytes: int | None = None
    estimated_wall_time_seconds: float | None = None
    estimated_output_bytes: int | None = None
    deterministic_required: bool = False
    seed: int | None = None
    cancellation_required: bool = False
    input_value: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _identifier(self.adapter_id, "adapter_id")
        if not isinstance(self.task_mode, TaskMode):
            raise ContractValidationError("task_mode must be a TaskMode")
        if not isinstance(self.media_kinds, tuple) or not all(
            isinstance(item, MediaKind) for item in self.media_kinds
        ):
            raise ContractValidationError("media_kinds must be a tuple of MediaKind")
        if len(self.media_kinds) != len(set(self.media_kinds)):
            raise ContractValidationError("media_kinds must not contain duplicates")
        _non_negative_int(self.reference_count, "reference_count")
        if not isinstance(self.device, LocalDeviceSpec):
            raise ContractValidationError("device must be a LocalDeviceSpec")
        for value, field_name in (
            (self.estimated_memory_bytes, "estimated_memory_bytes"),
            (self.estimated_output_bytes, "estimated_output_bytes"),
        ):
            if value is not None:
                _positive_int(value, field_name)
        if self.estimated_wall_time_seconds is not None:
            _positive_float(self.estimated_wall_time_seconds, "estimated_wall_time_seconds")
        for value, field_name in (
            (self.deterministic_required, "deterministic_required"),
            (self.cancellation_required, "cancellation_required"),
        ):
            if not isinstance(value, bool):
                raise ContractValidationError(f"{field_name} must be a boolean")
        if self.seed is not None and (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not -(2**63) <= self.seed <= 2**63 - 1
        ):
            raise ContractValidationError("seed must be a signed 64-bit integer")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "task_mode": self.task_mode.value,
            "media_kinds": [kind.value for kind in self.media_kinds],
            "reference_count": self.reference_count,
            "device": self.device.to_wire(),
            "estimated_memory_bytes": self.estimated_memory_bytes,
            "estimated_wall_time_seconds": self.estimated_wall_time_seconds,
            "estimated_output_bytes": self.estimated_output_bytes,
            "deterministic_required": self.deterministic_required,
            "seed": self.seed,
            "cancellation_required": self.cancellation_required,
        }


@dataclass(frozen=True, slots=True)
class LocalAdapterResult:
    """A complete, identity-bound result returned by a local adapter."""

    adapter_id: str
    adapter_version: str
    device: LocalDeviceSpec
    value: object = field(repr=False, compare=False)
    complete: bool = True
    output_bytes: int = 0
    output_items: int = 0

    def __post_init__(self) -> None:
        _identifier(self.adapter_id, "adapter_id")
        _version(self.adapter_version, "adapter_version")
        if not isinstance(self.device, LocalDeviceSpec):
            raise ContractValidationError("result device must be a LocalDeviceSpec")
        if not isinstance(self.complete, bool) or not self.complete:
            raise LocalAdapterError("local adapter returned an incomplete result")
        _non_negative_int(self.output_bytes, "output_bytes")
        _non_negative_int(self.output_items, "output_items")


@runtime_checkable
class LocalAdapter(Protocol):
    """Structural seam implemented by an optional local adapter."""

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        """Return the immutable capability descriptor."""

    def run(
        self, request: LocalAdapterExecutionRequest, guard: LocalBudgetGuard
    ) -> LocalAdapterResult:
        """Run one bounded operation, calling ``guard.checkpoint()`` during long work."""


@runtime_checkable
class LocalCancellationProbe(Protocol):
    """Injected local cancellation signal."""

    def is_cancelled(self) -> bool:
        """Return whether the operation was cancelled."""


def _fatal(code: str, message: str, location: str | None = None) -> ValidationDiagnostic:
    return ValidationDiagnostic(ValidationSeverity.FATAL, code, message, location)


def validate_local_adapter_capabilities(
    descriptor: LocalAdapterDescriptor,
    request: LocalAdapterExecutionRequest,
) -> tuple[ValidationDiagnostic, ...]:
    """Return fail-closed diagnostics without loading or running an adapter."""

    if not isinstance(descriptor, LocalAdapterDescriptor):
        raise ContractValidationError("descriptor must be a LocalAdapterDescriptor")
    if not isinstance(request, LocalAdapterExecutionRequest):
        raise ContractValidationError("request must be a LocalAdapterExecutionRequest")
    diagnostics: list[ValidationDiagnostic] = []
    if request.adapter_id != descriptor.adapter_id:
        diagnostics.append(
            _fatal(
                "local.adapter_id_mismatch",
                "request adapter ID does not match descriptor",
                "adapter_id",
            )
        )
    if request.task_mode not in descriptor.supported_task_modes:
        diagnostics.append(
            _fatal("local.unsupported_task_mode", "adapter does not support task mode", "task_mode")
        )
    for media_kind in request.media_kinds:
        if media_kind not in descriptor.supported_media:
            diagnostics.append(
                _fatal(
                    "local.unsupported_media", "adapter does not support media kind", "media_kinds"
                )
            )
    if request.device.normalized_kind not in descriptor.supported_devices:
        diagnostics.append(
            _fatal("local.unsupported_device", "adapter does not support selected device", "device")
        )
    limits = descriptor.limits
    if request.reference_count > limits.max_references:
        diagnostics.append(
            _fatal(
                "local.reference_limit", "reference count exceeds adapter budget", "reference_count"
            )
        )
    if (
        request.estimated_memory_bytes is not None
        and request.estimated_memory_bytes > limits.max_memory_bytes
    ):
        diagnostics.append(
            _fatal(
                "local.memory_limit",
                "estimated memory exceeds adapter budget",
                "estimated_memory_bytes",
            )
        )
    if (
        request.estimated_wall_time_seconds is not None
        and request.estimated_wall_time_seconds > limits.max_wall_time_seconds
    ):
        diagnostics.append(
            _fatal(
                "local.time_limit",
                "estimated wall time exceeds adapter budget",
                "estimated_wall_time_seconds",
            )
        )
    if (
        request.estimated_output_bytes is not None
        and request.estimated_output_bytes > limits.max_output_bytes
    ):
        diagnostics.append(
            _fatal(
                "local.output_limit",
                "estimated output exceeds adapter budget",
                "estimated_output_bytes",
            )
        )
    if request.deterministic_required and not descriptor.supports_determinism:
        diagnostics.append(
            _fatal(
                "local.determinism_unsupported",
                "adapter does not support deterministic execution",
                "deterministic_required",
            )
        )
    if request.seed is not None and not descriptor.supports_seed:
        diagnostics.append(
            _fatal("local.seed_unsupported", "adapter does not support a caller seed", "seed")
        )
    if request.cancellation_required and not descriptor.supports_cancellation:
        diagnostics.append(
            _fatal(
                "local.cancellation_unsupported",
                "adapter does not support cancellation",
                "cancellation_required",
            )
        )
    return tuple(diagnostics)


class LocalBudgetGuard:
    """Checkpoint-based wall-time, memory, cancellation, and output enforcement."""

    def __init__(
        self,
        limits: LocalResourceBudget,
        *,
        cancellation_probe: LocalCancellationProbe | None = None,
        clock: Callable[[], float] = time.monotonic,
        memory_meter: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(limits, LocalResourceBudget):
            raise ContractValidationError("limits must be a LocalResourceBudget")
        if not callable(clock):
            raise ContractValidationError("clock must be callable")
        if cancellation_probe is not None and not isinstance(
            cancellation_probe, LocalCancellationProbe
        ):
            raise ContractValidationError(
                "cancellation_probe must implement LocalCancellationProbe"
            )
        if memory_meter is not None and not callable(memory_meter):
            raise ContractValidationError("memory_meter must be callable")
        self.limits = limits
        self._cancellation_probe = cancellation_probe
        self._clock = clock
        self._memory_meter = memory_meter
        self._started = self._finite_clock()
        self._output_bytes = 0
        self._output_items = 0

    def _finite_clock(self) -> float:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(float(value))
        ):
            raise ContractValidationError("local budget clock must return a finite number")
        return float(value)

    def checkpoint(self) -> None:
        """Raise a typed terminal error if cancellation or any runtime budget is exceeded."""

        if self._cancellation_probe is not None and self._cancellation_probe.is_cancelled():
            raise LocalAdapterCancelledError("local adapter execution was cancelled")
        if self._finite_clock() - self._started > self.limits.max_wall_time_seconds:
            raise LocalAdapterTimeoutError("local adapter wall-time budget exceeded")
        if self._memory_meter is not None:
            value = self._memory_meter()
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError("memory_meter must return a non-negative integer")
            if value > self.limits.max_memory_bytes:
                raise LocalAdapterMemoryError("local adapter memory budget exceeded")
        if (
            self._output_bytes > self.limits.max_output_bytes
            or self._output_items > self.limits.max_output_items
        ):
            raise LocalAdapterBudgetError("local adapter output budget exceeded")

    def record_output(self, *, byte_count: int, item_count: int = 1) -> None:
        _non_negative_int(byte_count, "byte_count")
        _non_negative_int(item_count, "item_count")
        self._output_bytes += byte_count
        self._output_items += item_count
        self.checkpoint()

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self._finite_clock() - self._started)


class _ConcurrencyLease:
    def __init__(self, owner: LocalConcurrencyLimiter) -> None:
        self._owner = owner
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._owner.release()


class LocalConcurrencyLimiter:
    """Thread-safe bounded reservation used by a runtime across repeated executions."""

    def __init__(self, maximum: int) -> None:
        self._maximum = _positive_int(maximum, "maximum")
        self._active = 0
        self._lock = threading.Lock()

    def acquire(self) -> _ConcurrencyLease:
        with self._lock:
            if self._active >= self._maximum:
                raise LocalAdapterConcurrencyError("local adapter concurrency budget exceeded")
            self._active += 1
        return _ConcurrencyLease(self)

    def release(self) -> None:
        with self._lock:
            if self._active <= 0:
                raise LocalAdapterError("local adapter concurrency reservation underflow")
            self._active -= 1

    @property
    def active(self) -> int:
        with self._lock:
            return self._active


class LocalAdapterRuntime:
    """Own per-adapter concurrency reservations while keeping adapters themselves stateless."""

    def __init__(self) -> None:
        self._limiters: dict[str, LocalConcurrencyLimiter] = {}
        self._lock = threading.Lock()

    def _limiter(self, descriptor: LocalAdapterDescriptor) -> LocalConcurrencyLimiter:
        with self._lock:
            limiter = self._limiters.get(descriptor.adapter_id)
            if limiter is None:
                limiter = LocalConcurrencyLimiter(descriptor.limits.max_concurrency)
                self._limiters[descriptor.adapter_id] = limiter
            return limiter

    def execute(
        self,
        adapter: LocalAdapter,
        request: LocalAdapterExecutionRequest,
        *,
        cancellation_probe: LocalCancellationProbe | None = None,
        clock: Callable[[], float] = time.monotonic,
        memory_meter: Callable[[], int] | None = None,
    ) -> LocalAdapterResult:
        return _execute_local_adapter(
            adapter,
            request,
            limiter=self._limiter(adapter.descriptor),
            cancellation_probe=cancellation_probe,
            clock=clock,
            memory_meter=memory_meter,
        )


def _execute_local_adapter(
    adapter: LocalAdapter,
    request: LocalAdapterExecutionRequest,
    *,
    limiter: LocalConcurrencyLimiter,
    cancellation_probe: LocalCancellationProbe | None,
    clock: Callable[[], float],
    memory_meter: Callable[[], int] | None,
) -> LocalAdapterResult:
    if not isinstance(adapter, LocalAdapter):
        raise ContractValidationError("adapter must implement LocalAdapter")
    descriptor = adapter.descriptor
    diagnostics = validate_local_adapter_capabilities(descriptor, request)
    if diagnostics:
        first = diagnostics[0]
        code = first.code
        if code in {
            "local.adapter_id_mismatch",
            "local.unsupported_task_mode",
            "local.unsupported_media",
            "local.unsupported_device",
            "local.determinism_unsupported",
            "local.seed_unsupported",
            "local.cancellation_unsupported",
        }:
            raise LocalAdapterCapabilityError(code)
        raise LocalAdapterBudgetError(code)
    lease = limiter.acquire()
    guard = LocalBudgetGuard(
        descriptor.limits,
        cancellation_probe=cancellation_probe,
        clock=clock,
        memory_meter=memory_meter,
    )
    try:
        guard.checkpoint()
        try:
            result = adapter.run(request, guard)
        except (LocalAdapterError, ContractValidationError):
            raise
        except MemoryError as exc:
            raise LocalAdapterMemoryError(
                "local adapter reported an out-of-memory failure"
            ) from exc
        except TimeoutError as exc:
            raise LocalAdapterTimeoutError("local adapter reported a timeout") from exc
        except Exception as exc:
            raise LocalAdapterError("local adapter execution failed") from exc
        guard.checkpoint()
        if not isinstance(result, LocalAdapterResult):
            raise LocalAdapterError("local adapter returned an invalid result")
        if result.adapter_id != descriptor.adapter_id:
            raise LocalAdapterError("local adapter result identity mismatch")
        if _version(result.adapter_version, "result adapter_version") < _version(
            descriptor.minimum_version, "minimum_version"
        ) or _version(result.adapter_version, "result adapter_version") > _version(
            descriptor.maximum_version, "maximum_version"
        ):
            raise LocalAdapterVersionError(
                "local adapter result version is outside the declared range"
            )
        if (
            result.output_bytes > descriptor.limits.max_output_bytes
            or result.output_items > descriptor.limits.max_output_items
        ):
            raise LocalAdapterBudgetError("local adapter result exceeds output budget")
        return result
    finally:
        lease.release()


def run_local_adapter(
    adapter: LocalAdapter,
    request: LocalAdapterExecutionRequest,
    *,
    runtime: LocalAdapterRuntime | None = None,
    cancellation_probe: LocalCancellationProbe | None = None,
    clock: Callable[[], float] = time.monotonic,
    memory_meter: Callable[[], int] | None = None,
) -> LocalAdapterResult:
    """Execute one explicitly selected adapter with bounded resources and cleanup."""

    runtime_value = LocalAdapterRuntime() if runtime is None else runtime
    return runtime_value.execute(
        adapter,
        request,
        cancellation_probe=cancellation_probe,
        clock=clock,
        memory_meter=memory_meter,
    )


@dataclass(frozen=True, slots=True)
class LazyLocalAdapter:
    """A descriptor plus an import path that is touched only after explicit resolution."""

    descriptor: LocalAdapterDescriptor
    module_name: str
    attribute: str = "build_adapter"

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, LocalAdapterDescriptor):
            raise ContractValidationError("lazy descriptor must be a LocalAdapterDescriptor")
        if (
            not isinstance(self.module_name, str)
            or _MODULE_PATTERN.fullmatch(self.module_name) is None
        ):
            raise ContractValidationError("module_name must be a dotted module identifier")
        if (
            not isinstance(self.attribute, str)
            or _ATTRIBUTE_PATTERN.fullmatch(self.attribute) is None
        ):
            raise ContractValidationError("attribute must be a bounded identifier")

    def load(self) -> LocalAdapter:
        try:
            module = importlib.import_module(self.module_name)
        except ImportError as exc:
            raise LocalAdapterUnavailableError(
                f"optional local adapter dependency is unavailable: {self.descriptor.adapter_id}"
            ) from exc
        try:
            factory = getattr(module, self.attribute)
        except AttributeError as exc:
            raise LocalAdapterUnavailableError(
                f"optional local adapter factory is unavailable: {self.descriptor.adapter_id}"
            ) from exc
        try:
            candidate = factory() if callable(factory) else factory
        except Exception as exc:
            raise LocalAdapterUnavailableError(
                f"optional local adapter could not be initialized: {self.descriptor.adapter_id}"
            ) from exc
        if not isinstance(candidate, LocalAdapter):
            raise LocalAdapterUnavailableError(
                "optional local adapter has an invalid implementation: "
                f"{self.descriptor.adapter_id}"
            )
        actual = candidate.descriptor
        if (
            actual.adapter_id != self.descriptor.adapter_id
            or actual.kind is not self.descriptor.kind
        ):
            raise LocalAdapterVersionError(
                "resolved local adapter identity does not match the catalog"
            )
        actual_version = _version(actual.adapter_version, "resolved adapter_version")
        if (
            not _version(self.descriptor.minimum_version, "minimum_version")
            <= actual_version
            <= _version(self.descriptor.maximum_version, "maximum_version")
        ):
            raise LocalAdapterVersionError(
                "resolved local adapter version is outside the catalog range"
            )
        return candidate


class LocalAdapterCatalog:
    """Explicit catalog; an empty catalog is a valid manual-only configuration."""

    def __init__(self, entries: Iterable[LazyLocalAdapter] = ()) -> None:
        values = tuple(entries)
        if not all(isinstance(entry, LazyLocalAdapter) for entry in values):
            raise ContractValidationError("catalog entries must be LazyLocalAdapter values")
        ids = tuple(entry.descriptor.adapter_id for entry in values)
        if len(ids) != len(set(ids)):
            raise ContractValidationError("catalog adapter IDs must be unique")
        self._entries = values

    @classmethod
    def from_lazy(
        cls,
        descriptor: LocalAdapterDescriptor,
        *,
        module_name: str,
        attribute: str = "build_adapter",
    ) -> LocalAdapterCatalog:
        return cls((LazyLocalAdapter(descriptor, module_name, attribute),))

    @property
    def descriptors(self) -> tuple[LocalAdapterDescriptor, ...]:
        return tuple(entry.descriptor for entry in self._entries)

    def resolve(self, adapter_id: str) -> LocalAdapter:
        _identifier(adapter_id, "adapter_id")
        for entry in self._entries:
            if entry.descriptor.adapter_id == adapter_id:
                return entry.load()
        raise LocalAdapterUnavailableError(
            f"local adapter is not explicitly registered: {adapter_id}"
        )


def default_local_adapter_catalog() -> LocalAdapterCatalog:
    """Return the empty, dependency-free catalog used by manual workflows."""

    return LocalAdapterCatalog()


__all__ = [
    "LOCAL_ADAPTER_CONTRACT_SCHEMA",
    "LazyLocalAdapter",
    "LocalAdapter",
    "LocalAdapterCatalog",
    "LocalAdapterCapabilityError",
    "LocalAdapterDescriptor",
    "LocalAdapterExecutionRequest",
    "LocalAdapterKind",
    "LocalAdapterResult",
    "LocalAdapterRuntime",
    "LocalBudgetGuard",
    "LocalCancellationProbe",
    "LocalConcurrencyLimiter",
    "LocalDeviceKind",
    "LocalDeviceSpec",
    "LocalResourceBudget",
    "default_local_adapter_catalog",
    "run_local_adapter",
    "validate_local_adapter_capabilities",
]
