"""Pure, owner-scoped performance qualification contracts.

The module aggregates already-observed package/host/external measurements. It does not import a
model runtime, inspect a GPU, start a process, or claim allocator control.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias, cast

PERFORMANCE_PROFILE_SCHEMA = "h3.performance.profiles.v1"
PERFORMANCE_QUALIFICATION_SCHEMA = "h3.performance.qualification.v1"
MAX_PERFORMANCE_PROFILE_WIRE_BYTES = 65_536
MAX_PERFORMANCE_QUALIFICATION_WIRE_BYTES = 32_768
MAX_PERFORMANCE_PROFILE_DEPTH = 16
MAX_PERFORMANCE_PROFILE_ITEMS = 2_048
MAX_PERFORMANCE_PROFILE_TEXT = 4_096

_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_DIAGNOSTIC = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class PerformanceQualificationError(ValueError):
    """Raised when performance authority is malformed or ownership is ambiguous."""


class PerformanceProfileId(str, Enum):
    CPU = "cpu"
    LOW_RESOURCE = "low_resource"
    MAINSTREAM_GPU = "mainstream_gpu"
    HIGH_FIDELITY = "high_fidelity"


class MeasurementOwner(str, Enum):
    PACKAGE = "package"
    COMFYUI_HOST = "comfyui_host"
    OLLAMA_EXTERNAL = "ollama_external"


class MetricSource(str, Enum):
    PACKAGE_CLOCK = "package_clock"
    PYTHON_TRACED = "python_traced"
    PROCESS_RSS = "process_rss"
    COMFYUI_HOST = "comfyui_host"
    OLLAMA_EXTERNAL = "ollama_external"
    BROWSER_USER_TIMING = "browser_user_timing"


class CacheObservation(str, Enum):
    DISABLED = "disabled"
    MISS = "miss"
    HIT = "hit"


class QualificationStatus(str, Enum):
    QUALIFIED = "qualified"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


def _exact_int(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise PerformanceQualificationError(f"{field} must be an integer at least {minimum}")
    return value


def _finite(value: object, field: str, *, minimum: float = 0.0) -> float:
    if type(value) not in {int, float}:
        raise PerformanceQualificationError(f"{field} must be a finite number")
    result = float(cast(int | float, value))
    if not math.isfinite(result) or result < minimum:
        raise PerformanceQualificationError(f"{field} must be a finite number")
    return result


def _fingerprint(value: object, field: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise PerformanceQualificationError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise PerformanceQualificationError(f"{field} must be a bounded identifier")
    return value


@dataclass(frozen=True, slots=True)
class PerformanceBudget:
    warmups: int
    repetitions: int
    max_latency_ms: float
    max_process_rss_bytes: int
    max_python_peak_bytes: int
    max_vram_bytes: int
    max_output_bytes: int
    max_output_items: int
    max_cancellation_latency_ms: float
    max_wall_time_ms: float
    max_concurrency: int
    max_cache_ttl_seconds: float

    def __post_init__(self) -> None:
        _exact_int(self.warmups, "warmups", minimum=1)
        _exact_int(self.repetitions, "repetitions", minimum=3)
        _finite(self.max_latency_ms, "max_latency_ms", minimum=0.001)
        _exact_int(self.max_process_rss_bytes, "max_process_rss_bytes", minimum=1)
        _exact_int(self.max_python_peak_bytes, "max_python_peak_bytes", minimum=1)
        _exact_int(self.max_vram_bytes, "max_vram_bytes")
        _exact_int(self.max_output_bytes, "max_output_bytes", minimum=1)
        _exact_int(self.max_output_items, "max_output_items", minimum=1)
        _finite(
            self.max_cancellation_latency_ms,
            "max_cancellation_latency_ms",
            minimum=0.001,
        )
        _finite(self.max_wall_time_ms, "max_wall_time_ms", minimum=0.001)
        if self.max_wall_time_ms < self.max_latency_ms:
            raise PerformanceQualificationError("max_wall_time_ms cannot be below latency budget")
        _exact_int(self.max_concurrency, "max_concurrency", minimum=1)
        _finite(self.max_cache_ttl_seconds, "max_cache_ttl_seconds", minimum=0.001)

    def to_wire(self) -> dict[str, int | float]:
        return {
            "warmups": self.warmups,
            "repetitions": self.repetitions,
            "max_latency_ms": self.max_latency_ms,
            "max_process_rss_bytes": self.max_process_rss_bytes,
            "max_python_peak_bytes": self.max_python_peak_bytes,
            "max_vram_bytes": self.max_vram_bytes,
            "max_output_bytes": self.max_output_bytes,
            "max_output_items": self.max_output_items,
            "max_cancellation_latency_ms": self.max_cancellation_latency_ms,
            "max_wall_time_ms": self.max_wall_time_ms,
            "max_concurrency": self.max_concurrency,
            "max_cache_ttl_seconds": self.max_cache_ttl_seconds,
        }


@dataclass(frozen=True, slots=True)
class PerformanceProfile:
    profile_id: PerformanceProfileId
    workload_id: str
    required_sources: tuple[MetricSource, ...]
    budget: PerformanceBudget
    vram_owner: MeasurementOwner | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, PerformanceProfileId):
            raise PerformanceQualificationError("profile_id is unsupported")
        _identifier(self.workload_id, "workload_id")
        if (
            type(self.required_sources) is not tuple
            or not self.required_sources
            or len(self.required_sources) != len(set(self.required_sources))
            or not all(isinstance(value, MetricSource) for value in self.required_sources)
        ):
            raise PerformanceQualificationError("required_sources are invalid")
        if not isinstance(self.budget, PerformanceBudget):
            raise PerformanceQualificationError("budget is invalid")
        if self.vram_owner is not None and not isinstance(self.vram_owner, MeasurementOwner):
            raise PerformanceQualificationError("vram_owner is invalid")
        if self.budget.max_vram_bytes > 0 and self.vram_owner is None:
            raise PerformanceQualificationError("VRAM budget requires an explicit owner")
        if self.budget.max_vram_bytes == 0 and self.vram_owner is not None:
            raise PerformanceQualificationError("zero VRAM budget cannot claim an owner")

    def to_wire(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id.value,
            "workload_id": self.workload_id,
            "required_sources": [source.value for source in self.required_sources],
            "vram_owner": self.vram_owner.value if self.vram_owner is not None else None,
            "budget": self.budget.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class PerformanceObservation:
    latency_ms: float
    output_bytes: int
    output_items: int
    python_peak_bytes: int | None
    process_rss_peak_bytes: int | None
    vram_peak_bytes: int | None
    vram_owner: MeasurementOwner | None
    cache: CacheObservation
    output_fingerprint: str
    cancelled: bool
    cancellation_latency_ms: float | None
    cleanup_verified: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "latency_ms", _finite(self.latency_ms, "latency_ms"))
        _exact_int(self.output_bytes, "output_bytes")
        _exact_int(self.output_items, "output_items")
        for value, field in (
            (self.python_peak_bytes, "python_peak_bytes"),
            (self.process_rss_peak_bytes, "process_rss_peak_bytes"),
            (self.vram_peak_bytes, "vram_peak_bytes"),
        ):
            if value is not None:
                _exact_int(value, field)
        if self.vram_owner is not None and not isinstance(self.vram_owner, MeasurementOwner):
            raise PerformanceQualificationError("VRAM owner is invalid")
        if (self.vram_peak_bytes is None) != (self.vram_owner is None):
            raise PerformanceQualificationError("VRAM observation and VRAM owner must be paired")
        if not isinstance(self.cache, CacheObservation):
            raise PerformanceQualificationError("cache observation is invalid")
        _fingerprint(self.output_fingerprint, "output_fingerprint")
        if type(self.cancelled) is not bool or type(self.cleanup_verified) is not bool:
            raise PerformanceQualificationError("terminal observation flags must be booleans")
        if self.cancelled:
            if self.cancellation_latency_ms is None:
                raise PerformanceQualificationError(
                    "cancelled observation requires cancellation latency"
                )
            object.__setattr__(
                self,
                "cancellation_latency_ms",
                _finite(self.cancellation_latency_ms, "cancellation latency"),
            )
        elif self.cancellation_latency_ms is not None:
            raise PerformanceQualificationError(
                "non-cancelled observation cannot carry cancellation latency"
            )


@dataclass(frozen=True, slots=True)
class PerformanceQualification:
    profile_id: PerformanceProfileId
    status: QualificationStatus
    candidate_commit: str
    workload_fingerprint: str
    config_fingerprint: str
    observed_sources: tuple[MetricSource, ...]
    sample_count: int
    median_latency_ms: float | None
    p95_latency_ms: float | None
    worst_latency_ms: float | None
    throughput_per_second: float | None
    peak_python_bytes: int | None
    peak_process_rss_bytes: int | None
    peak_vram_bytes: int | None
    peak_output_bytes: int | None
    peak_output_items: int | None
    cache_misses: int
    cache_hits: int
    cache_gain_percent: float | None
    quality_preserved: bool
    cancelled_samples: int
    cleanup_verified: bool
    diagnostic_codes: tuple[str, ...]
    schema: str = PERFORMANCE_QUALIFICATION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, PerformanceProfileId) or not isinstance(
            self.status, QualificationStatus
        ):
            raise PerformanceQualificationError("qualification profile or status is invalid")
        if self.schema != PERFORMANCE_QUALIFICATION_SCHEMA:
            raise PerformanceQualificationError("qualification schema is unsupported")
        if (
            type(self.candidate_commit) is not str
            or _COMMIT.fullmatch(self.candidate_commit) is None
        ):
            raise PerformanceQualificationError(
                "candidate_commit must be a lowercase 40-character OID"
            )
        _fingerprint(self.workload_fingerprint, "workload_fingerprint")
        _fingerprint(self.config_fingerprint, "config_fingerprint")
        if (
            type(self.observed_sources) is not tuple
            or len(self.observed_sources) != len(set(self.observed_sources))
            or not all(isinstance(value, MetricSource) for value in self.observed_sources)
        ):
            raise PerformanceQualificationError("observed_sources are invalid")
        _exact_int(self.sample_count, "sample_count")
        if self.sample_count > 64:
            raise PerformanceQualificationError("sample_count exceeds its finite bound")
        for value, field in (
            (self.median_latency_ms, "median_latency_ms"),
            (self.p95_latency_ms, "p95_latency_ms"),
            (self.worst_latency_ms, "worst_latency_ms"),
            (self.throughput_per_second, "throughput_per_second"),
        ):
            if value is not None:
                _finite(value, field)
        if self.cache_gain_percent is not None and (
            type(self.cache_gain_percent) not in {int, float}
            or not math.isfinite(float(self.cache_gain_percent))
        ):
            raise PerformanceQualificationError("cache_gain_percent must be a finite number")
        for value, field in (
            (self.peak_python_bytes, "peak_python_bytes"),
            (self.peak_process_rss_bytes, "peak_process_rss_bytes"),
            (self.peak_vram_bytes, "peak_vram_bytes"),
            (self.peak_output_bytes, "peak_output_bytes"),
            (self.peak_output_items, "peak_output_items"),
        ):
            if value is not None:
                _exact_int(value, field)
        for value, field in (
            (self.cache_misses, "cache_misses"),
            (self.cache_hits, "cache_hits"),
            (self.cancelled_samples, "cancelled_samples"),
        ):
            _exact_int(value, field)
            if value > self.sample_count:
                raise PerformanceQualificationError("cache inventory exceeds sample_count")
        if self.cache_misses + self.cache_hits > self.sample_count:
            raise PerformanceQualificationError("cache inventory exceeds sample_count")
        if type(self.quality_preserved) is not bool or type(self.cleanup_verified) is not bool:
            raise PerformanceQualificationError("qualification terminal flags must be booleans")
        if (
            type(self.diagnostic_codes) is not tuple
            or len(self.diagnostic_codes) > 32
            or len(self.diagnostic_codes) != len(set(self.diagnostic_codes))
            or not all(
                type(value) is str and _DIAGNOSTIC.fullmatch(value) is not None
                for value in self.diagnostic_codes
            )
        ):
            raise PerformanceQualificationError("qualification diagnostic codes are invalid")
        if self.status is QualificationStatus.QUALIFIED and (
            self.sample_count == 0
            or self.median_latency_ms is None
            or self.p95_latency_ms is None
            or self.worst_latency_ms is None
            or self.throughput_per_second is None
            or self.peak_output_bytes is None
            or self.peak_output_items is None
            or not self.quality_preserved
            or not self.cleanup_verified
            or self.diagnostic_codes
        ):
            raise PerformanceQualificationError("qualified receipt is incomplete or inconsistent")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "profile_id": self.profile_id.value,
            "status": self.status.value,
            "candidate_commit": self.candidate_commit,
            "workload_fingerprint": self.workload_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "observed_sources": [source.value for source in self.observed_sources],
            "sample_count": self.sample_count,
            "median_latency_ms": self.median_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "worst_latency_ms": self.worst_latency_ms,
            "throughput_per_second": self.throughput_per_second,
            "peak_python_bytes": self.peak_python_bytes,
            "peak_process_rss_bytes": self.peak_process_rss_bytes,
            "peak_vram_bytes": self.peak_vram_bytes,
            "peak_output_bytes": self.peak_output_bytes,
            "peak_output_items": self.peak_output_items,
            "cache_misses": self.cache_misses,
            "cache_hits": self.cache_hits,
            "cache_gain_percent": self.cache_gain_percent,
            "quality_preserved": self.quality_preserved,
            "cancelled_samples": self.cancelled_samples,
            "cleanup_verified": self.cleanup_verified,
            "diagnostic_codes": list(self.diagnostic_codes),
        }


def default_performance_profiles() -> tuple[PerformanceProfile, ...]:
    mib = 1024 * 1024
    gib = 1024 * mib
    return (
        PerformanceProfile(
            PerformanceProfileId.CPU,
            "deterministic_product_pipeline",
            (
                MetricSource.PACKAGE_CLOCK,
                MetricSource.PYTHON_TRACED,
                MetricSource.PROCESS_RSS,
            ),
            PerformanceBudget(
                1,
                5,
                250.0,
                256 * mib,
                128 * mib,
                0,
                256 * 1024,
                512,
                100.0,
                1_000.0,
                1,
                60.0,
            ),
        ),
        PerformanceProfile(
            PerformanceProfileId.LOW_RESOURCE,
            "deterministic_product_pipeline",
            (
                MetricSource.PACKAGE_CLOCK,
                MetricSource.PYTHON_TRACED,
                MetricSource.PROCESS_RSS,
            ),
            PerformanceBudget(
                1,
                3,
                500.0,
                128 * mib,
                64 * mib,
                0,
                128 * 1024,
                256,
                150.0,
                1_500.0,
                1,
                60.0,
            ),
        ),
        PerformanceProfile(
            PerformanceProfileId.MAINSTREAM_GPU,
            "supported_host_model_execution",
            (MetricSource.PACKAGE_CLOCK, MetricSource.COMFYUI_HOST),
            PerformanceBudget(
                1,
                3,
                120_000.0,
                512 * mib,
                128 * mib,
                16 * gib,
                512 * 1024,
                512,
                500.0,
                180_000.0,
                1,
                60.0,
            ),
            MeasurementOwner.COMFYUI_HOST,
        ),
        PerformanceProfile(
            PerformanceProfileId.HIGH_FIDELITY,
            "supported_host_model_execution",
            (MetricSource.PACKAGE_CLOCK, MetricSource.COMFYUI_HOST),
            PerformanceBudget(
                1,
                3,
                300_000.0,
                1024 * mib,
                256 * mib,
                24 * gib,
                1024 * 1024,
                1024,
                1000.0,
                360_000.0,
                1,
                60.0,
            ),
            MeasurementOwner.COMFYUI_HOST,
        ),
    )


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def _maximum(values: list[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def qualify_performance_profile(
    profile: PerformanceProfile,
    observations: tuple[PerformanceObservation, ...],
    *,
    candidate_commit: str,
    workload_fingerprint: str,
    config_fingerprint: str,
    observed_sources: tuple[MetricSource, ...],
) -> PerformanceQualification:
    if not isinstance(profile, PerformanceProfile):
        raise PerformanceQualificationError("profile is invalid")
    if (
        type(observations) is not tuple
        or not observations
        or not all(isinstance(value, PerformanceObservation) for value in observations)
    ):
        raise PerformanceQualificationError("observations must be a non-empty exact tuple")
    if _COMMIT.fullmatch(candidate_commit) is None:
        raise PerformanceQualificationError("candidate_commit must be a lowercase 40-character OID")
    _fingerprint(workload_fingerprint, "workload_fingerprint")
    _fingerprint(config_fingerprint, "config_fingerprint")
    if (
        type(observed_sources) is not tuple
        or len(observed_sources) != len(set(observed_sources))
        or not all(isinstance(value, MetricSource) for value in observed_sources)
    ):
        raise PerformanceQualificationError("observed_sources are invalid")

    missing_sources = [value for value in profile.required_sources if value not in observed_sources]
    latencies = [value.latency_ms for value in observations if not value.cancelled]
    diagnostics: list[str] = [f"required_source_missing:{value.value}" for value in missing_sources]
    status = QualificationStatus.UNAVAILABLE if missing_sources else QualificationStatus.QUALIFIED

    if observed_sources != profile.required_sources:
        diagnostics.append("observed_source_inventory_mismatch")
        status = QualificationStatus.UNAVAILABLE

    if MetricSource.PYTHON_TRACED in profile.required_sources and any(
        value.python_peak_bytes is None for value in observations
    ):
        diagnostics.append("python_observation_missing")
        status = QualificationStatus.UNAVAILABLE
    if MetricSource.PROCESS_RSS in profile.required_sources and any(
        value.process_rss_peak_bytes is None for value in observations
    ):
        diagnostics.append("process_rss_observation_missing")
        status = QualificationStatus.UNAVAILABLE

    if len(observations) != profile.budget.repetitions:
        diagnostics.append("sample_count_mismatch")
        status = QualificationStatus.FAILED

    if profile.vram_owner is not None:
        vram_observations = [value for value in observations if value.vram_peak_bytes is not None]
        for value in vram_observations:
            if value.vram_owner is not profile.vram_owner:
                raise PerformanceQualificationError(
                    "VRAM owner does not match the selected profile"
                )
        if not vram_observations:
            diagnostics.append("vram_observation_missing")
            status = QualificationStatus.UNAVAILABLE

    budget = profile.budget
    for value in observations:
        if value.output_bytes > budget.max_output_bytes:
            diagnostics.append("output_bytes_exceeded")
        if value.output_items > budget.max_output_items:
            diagnostics.append("output_items_exceeded")
        if value.latency_ms > budget.max_latency_ms:
            diagnostics.append("latency_exceeded")
        if (
            value.python_peak_bytes is not None
            and value.python_peak_bytes > budget.max_python_peak_bytes
        ):
            diagnostics.append("python_peak_exceeded")
        if (
            value.process_rss_peak_bytes is not None
            and value.process_rss_peak_bytes > budget.max_process_rss_bytes
        ):
            diagnostics.append("process_rss_exceeded")
        if value.vram_peak_bytes is not None and value.vram_peak_bytes > budget.max_vram_bytes:
            diagnostics.append("vram_exceeded")
        if value.cancelled:
            diagnostics.append("cancelled_sample")
            if cast(float, value.cancellation_latency_ms) > budget.max_cancellation_latency_ms:
                diagnostics.append("cancellation_latency_exceeded")
        if not value.cleanup_verified:
            diagnostics.append("cleanup_unverified")

    fingerprints = {value.output_fingerprint for value in observations if not value.cancelled}
    quality_preserved = len(fingerprints) == 1
    if not quality_preserved:
        diagnostics.append("quality_fingerprint_drift")
    unavailable_codes = {
        "vram_observation_missing",
        "observed_source_inventory_mismatch",
        "python_observation_missing",
        "process_rss_observation_missing",
    }
    failure_codes = {
        code
        for code in diagnostics
        if not code.startswith("required_source_missing:") and code not in unavailable_codes
    }
    if failure_codes:
        status = QualificationStatus.FAILED

    median = _percentile(latencies, 0.5) if latencies else None
    p95 = _percentile(latencies, 0.95) if latencies else None
    worst = max(latencies) if latencies else None
    throughput = 1000.0 / median if median is not None and median > 0 else None
    cold = [value.latency_ms for value in observations if value.cache is CacheObservation.MISS]
    warm = [value.latency_ms for value in observations if value.cache is CacheObservation.HIT]
    cache_gain = None
    if cold and warm:
        cold_p95 = _percentile(cold, 0.95)
        warm_p95 = _percentile(warm, 0.95)
        cache_gain = ((cold_p95 - warm_p95) / cold_p95) * 100 if cold_p95 > 0 else None

    return PerformanceQualification(
        profile.profile_id,
        status,
        candidate_commit,
        workload_fingerprint,
        config_fingerprint,
        observed_sources,
        len(observations),
        median,
        p95,
        worst,
        throughput,
        _maximum([value.python_peak_bytes for value in observations]),
        _maximum([value.process_rss_peak_bytes for value in observations]),
        _maximum([value.vram_peak_bytes for value in observations]),
        max(value.output_bytes for value in observations),
        max(value.output_items for value in observations),
        sum(value.cache is CacheObservation.MISS for value in observations),
        sum(value.cache is CacheObservation.HIT for value in observations),
        cache_gain,
        quality_preserved,
        sum(value.cancelled for value in observations),
        all(value.cleanup_verified for value in observations),
        tuple(dict.fromkeys(diagnostics)),
    )


def _duplicate_pairs(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise PerformanceQualificationError("performance JSON contains a duplicate member")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise PerformanceQualificationError(f"performance JSON contains non-finite value {value}")


def _resource_audit(value: JsonValue, depth: int = 0) -> tuple[int, int]:
    if depth > MAX_PERFORMANCE_PROFILE_DEPTH:
        raise PerformanceQualificationError("performance JSON exceeds maximum depth")
    if isinstance(value, str):
        if len(value) > MAX_PERFORMANCE_PROFILE_TEXT:
            raise PerformanceQualificationError("performance JSON text exceeds maximum length")
        return 1, len(value)
    if type(value) in {int, float} and not math.isfinite(float(cast(int | float, value))):
        raise PerformanceQualificationError("performance JSON contains non-finite value")
    if isinstance(value, list):
        total = 1
        text = 0
        for item in value:
            child_items, child_text = _resource_audit(item, depth + 1)
            total += child_items
            text += child_text
        return total, text
    if isinstance(value, dict):
        total = 1
        text = 0
        for key, item in value.items():
            if type(key) is not str or len(key) > MAX_PERFORMANCE_PROFILE_TEXT:
                raise PerformanceQualificationError("performance JSON member name is invalid")
            child_items, child_text = _resource_audit(item, depth + 1)
            total += 1 + child_items
            text += len(key) + child_text
        return total, text
    return 1, 0


def _closed(value: object, expected: set[str], field: str) -> dict[str, JsonValue]:
    if type(value) is not dict or set(cast(dict[str, JsonValue], value)) != expected:
        raise PerformanceQualificationError(f"{field} members are not closed")
    return cast(dict[str, JsonValue], value)


def _profile_from_wire(value: object) -> PerformanceProfile:
    profile = _closed(
        value,
        {"profile_id", "workload_id", "required_sources", "vram_owner", "budget"},
        "performance profile",
    )
    budget_value = _closed(
        profile["budget"],
        {
            "warmups",
            "repetitions",
            "max_latency_ms",
            "max_process_rss_bytes",
            "max_python_peak_bytes",
            "max_vram_bytes",
            "max_output_bytes",
            "max_output_items",
            "max_cancellation_latency_ms",
            "max_wall_time_ms",
            "max_concurrency",
            "max_cache_ttl_seconds",
        },
        "performance budget",
    )
    raw_sources = profile["required_sources"]
    if type(raw_sources) is not list:
        raise PerformanceQualificationError("required_sources must be a list")
    try:
        profile_id = PerformanceProfileId(profile["profile_id"])
        sources = tuple(MetricSource(value) for value in raw_sources)
        raw_owner = profile["vram_owner"]
        owner = None if raw_owner is None else MeasurementOwner(raw_owner)
    except (TypeError, ValueError):
        raise PerformanceQualificationError("performance profile enum is unsupported") from None
    return PerformanceProfile(
        profile_id,
        _identifier(profile["workload_id"], "workload_id"),
        sources,
        PerformanceBudget(
            _exact_int(budget_value["warmups"], "warmups", minimum=1),
            _exact_int(budget_value["repetitions"], "repetitions", minimum=3),
            _finite(budget_value["max_latency_ms"], "max_latency_ms", minimum=0.001),
            _exact_int(budget_value["max_process_rss_bytes"], "max_process_rss_bytes", minimum=1),
            _exact_int(budget_value["max_python_peak_bytes"], "max_python_peak_bytes", minimum=1),
            _exact_int(budget_value["max_vram_bytes"], "max_vram_bytes"),
            _exact_int(budget_value["max_output_bytes"], "max_output_bytes", minimum=1),
            _exact_int(budget_value["max_output_items"], "max_output_items", minimum=1),
            _finite(
                budget_value["max_cancellation_latency_ms"],
                "max_cancellation_latency_ms",
                minimum=0.001,
            ),
            _finite(budget_value["max_wall_time_ms"], "max_wall_time_ms", minimum=0.001),
            _exact_int(budget_value["max_concurrency"], "max_concurrency", minimum=1),
            _finite(
                budget_value["max_cache_ttl_seconds"],
                "max_cache_ttl_seconds",
                minimum=0.001,
            ),
        ),
        owner,
    )


def decode_performance_profiles_json(
    payload: str | bytes | bytearray,
) -> tuple[PerformanceProfile, ...]:
    """Decode one exact built-in UTF-8 JSON payload with closed resource bounds."""

    # CRITICAL: exact types prevent subclass hooks from bypassing byte and UTF-8 admission.
    if type(payload) not in {str, bytes, bytearray}:
        raise PerformanceQualificationError("payload must use an exact built-in text/byte type")
    raw = (
        payload.encode("utf-8") if type(payload) is str else bytes(cast(bytes | bytearray, payload))
    )
    if len(raw) > MAX_PERFORMANCE_PROFILE_WIRE_BYTES:
        raise PerformanceQualificationError("performance JSON exceeds its maximum byte size")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise PerformanceQualificationError("performance JSON must be strict UTF-8") from None
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except PerformanceQualificationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError):
        raise PerformanceQualificationError("performance JSON is invalid") from None
    root = _closed(parsed, {"schema", "profiles"}, "performance profile root")
    if root["schema"] != PERFORMANCE_PROFILE_SCHEMA:
        raise PerformanceQualificationError("performance profile schema is unsupported")
    items, _text = _resource_audit(cast(JsonValue, parsed))
    if items > MAX_PERFORMANCE_PROFILE_ITEMS:
        raise PerformanceQualificationError("performance JSON exceeds its maximum item count")
    profiles_raw = root["profiles"]
    if type(profiles_raw) is not list or len(profiles_raw) != len(PerformanceProfileId):
        raise PerformanceQualificationError("performance profile inventory is incomplete")
    profiles = tuple(_profile_from_wire(value) for value in profiles_raw)
    if tuple(value.profile_id for value in profiles) != tuple(PerformanceProfileId):
        raise PerformanceQualificationError(
            "performance profiles are missing, reordered, or duplicated"
        )
    return profiles


def _optional_int(value: object, field: str) -> int | None:
    return None if value is None else _exact_int(value, field)


def _optional_float(value: object, field: str) -> float | None:
    return None if value is None else _finite(value, field)


def _qualification_from_wire(value: object) -> PerformanceQualification:
    wire = _closed(
        value,
        {
            "schema",
            "profile_id",
            "status",
            "candidate_commit",
            "workload_fingerprint",
            "config_fingerprint",
            "observed_sources",
            "sample_count",
            "median_latency_ms",
            "p95_latency_ms",
            "worst_latency_ms",
            "throughput_per_second",
            "peak_python_bytes",
            "peak_process_rss_bytes",
            "peak_vram_bytes",
            "peak_output_bytes",
            "peak_output_items",
            "cache_misses",
            "cache_hits",
            "cache_gain_percent",
            "quality_preserved",
            "cancelled_samples",
            "cleanup_verified",
            "diagnostic_codes",
        },
        "performance qualification",
    )
    raw_sources = wire["observed_sources"]
    raw_diagnostics = wire["diagnostic_codes"]
    if type(raw_sources) is not list or type(raw_diagnostics) is not list:
        raise PerformanceQualificationError("qualification inventories must be lists")
    if not all(type(value) is str for value in raw_diagnostics):
        raise PerformanceQualificationError("qualification diagnostic codes are invalid")
    if type(wire["quality_preserved"]) is not bool or type(wire["cleanup_verified"]) is not bool:
        raise PerformanceQualificationError("qualification terminal flags must be booleans")
    try:
        profile_id = PerformanceProfileId(wire["profile_id"])
        status = QualificationStatus(wire["status"])
        sources = tuple(MetricSource(value) for value in raw_sources)
    except (TypeError, ValueError):
        raise PerformanceQualificationError("qualification enum is unsupported") from None
    return PerformanceQualification(
        profile_id=profile_id,
        status=status,
        candidate_commit=cast(str, wire["candidate_commit"]),
        workload_fingerprint=cast(str, wire["workload_fingerprint"]),
        config_fingerprint=cast(str, wire["config_fingerprint"]),
        observed_sources=sources,
        sample_count=_exact_int(wire["sample_count"], "sample_count"),
        median_latency_ms=_optional_float(wire["median_latency_ms"], "median_latency_ms"),
        p95_latency_ms=_optional_float(wire["p95_latency_ms"], "p95_latency_ms"),
        worst_latency_ms=_optional_float(wire["worst_latency_ms"], "worst_latency_ms"),
        throughput_per_second=_optional_float(
            wire["throughput_per_second"], "throughput_per_second"
        ),
        peak_python_bytes=_optional_int(wire["peak_python_bytes"], "peak_python_bytes"),
        peak_process_rss_bytes=_optional_int(
            wire["peak_process_rss_bytes"], "peak_process_rss_bytes"
        ),
        peak_vram_bytes=_optional_int(wire["peak_vram_bytes"], "peak_vram_bytes"),
        peak_output_bytes=_optional_int(wire["peak_output_bytes"], "peak_output_bytes"),
        peak_output_items=_optional_int(wire["peak_output_items"], "peak_output_items"),
        cache_misses=_exact_int(wire["cache_misses"], "cache_misses"),
        cache_hits=_exact_int(wire["cache_hits"], "cache_hits"),
        cache_gain_percent=(
            None
            if wire["cache_gain_percent"] is None
            else _finite(wire["cache_gain_percent"], "cache_gain_percent", minimum=-math.inf)
        ),
        quality_preserved=wire["quality_preserved"],
        cancelled_samples=_exact_int(wire["cancelled_samples"], "cancelled_samples"),
        cleanup_verified=wire["cleanup_verified"],
        diagnostic_codes=tuple(cast(list[str], raw_diagnostics)),
        schema=cast(str, wire["schema"]),
    )


def decode_performance_qualification_json(
    payload: str | bytes | bytearray,
) -> PerformanceQualification:
    """Decode one exact, closed, duplicate-aware performance qualification receipt."""

    # CRITICAL: exact types prevent subclass hooks from bypassing byte and UTF-8 admission.
    if type(payload) not in {str, bytes, bytearray}:
        raise PerformanceQualificationError("payload must use an exact built-in text/byte type")
    raw = (
        payload.encode("utf-8") if type(payload) is str else bytes(cast(bytes | bytearray, payload))
    )
    if len(raw) > MAX_PERFORMANCE_QUALIFICATION_WIRE_BYTES:
        raise PerformanceQualificationError("qualification JSON exceeds its maximum byte size")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise PerformanceQualificationError("qualification JSON must be strict UTF-8") from None
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except PerformanceQualificationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError):
        raise PerformanceQualificationError("qualification JSON is invalid") from None
    items, _text = _resource_audit(cast(JsonValue, parsed))
    if items > MAX_PERFORMANCE_PROFILE_ITEMS:
        raise PerformanceQualificationError("qualification JSON exceeds its maximum item count")
    return _qualification_from_wire(parsed)


__all__ = [
    "MAX_PERFORMANCE_PROFILE_WIRE_BYTES",
    "MAX_PERFORMANCE_QUALIFICATION_WIRE_BYTES",
    "CacheObservation",
    "MeasurementOwner",
    "MetricSource",
    "PerformanceBudget",
    "PerformanceObservation",
    "PerformanceProfile",
    "PerformanceProfileId",
    "PerformanceQualification",
    "PerformanceQualificationError",
    "QualificationStatus",
    "decode_performance_profiles_json",
    "decode_performance_qualification_json",
    "default_performance_profiles",
    "qualify_performance_profile",
]
