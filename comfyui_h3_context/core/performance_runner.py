"""Bounded model-free performance runner over the existing resource scheduler."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .local_adapters import LocalResourceBudget
from .performance_qualification import (
    CacheObservation,
    MetricSource,
    PerformanceObservation,
    PerformanceProfile,
    PerformanceQualification,
    PerformanceQualificationError,
    QualificationStatus,
    _fingerprint,
    _identifier,
    qualify_performance_profile,
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


class PerformanceRunnerError(PerformanceQualificationError):
    """Raised when benchmark admission or execution cannot produce truthful evidence."""


@dataclass(frozen=True, slots=True)
class BenchmarkOutput:
    output_fingerprint: str
    output_bytes: int
    output_items: int

    def __post_init__(self) -> None:
        _fingerprint(self.output_fingerprint, "benchmark output_fingerprint")
        if type(self.output_bytes) is not int or self.output_bytes < 0:
            raise PerformanceRunnerError("benchmark output_bytes must be a non-negative integer")
        if type(self.output_items) is not int or self.output_items < 0:
            raise PerformanceRunnerError("benchmark output_items must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class PerformanceSampleMetrics:
    latency_ms: float
    python_peak_bytes: int | None
    process_rss_peak_bytes: int | None

    def __post_init__(self) -> None:
        if type(self.latency_ms) not in {int, float} or not 0 <= float(self.latency_ms) < 3_600_000:
            raise PerformanceRunnerError("sample latency must be finite and bounded")
        for value, field in (
            (self.python_peak_bytes, "python_peak_bytes"),
            (self.process_rss_peak_bytes, "process_rss_peak_bytes"),
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise PerformanceRunnerError(f"sample {field} must be a non-negative integer")


@runtime_checkable
class PerformanceSampleObserver(Protocol):
    def observe(
        self, execute: Callable[[], ResourceExecutionResult]
    ) -> tuple[ResourceExecutionResult, PerformanceSampleMetrics]:
        """Execute exactly once and return aggregate, content-free sample metrics."""


@dataclass(frozen=True, slots=True)
class PerformanceWorkload:
    workload_id: str
    workload_fingerprint: str
    config_fingerprint: str
    expected_output_bytes: int
    expected_output_items: int
    execute: Callable[[ResourceExecutionContext], BenchmarkOutput]

    def __post_init__(self) -> None:
        _identifier(self.workload_id, "benchmark workload_id")
        _fingerprint(self.workload_fingerprint, "benchmark workload_fingerprint")
        _fingerprint(self.config_fingerprint, "benchmark config_fingerprint")
        if type(self.expected_output_bytes) is not int or self.expected_output_bytes < 0:
            raise PerformanceRunnerError("expected_output_bytes must be a non-negative integer")
        if type(self.expected_output_items) is not int or self.expected_output_items < 0:
            raise PerformanceRunnerError("expected_output_items must be a non-negative integer")
        if not callable(self.execute):
            raise PerformanceRunnerError("benchmark execute must be callable")


def _request(
    profile: PerformanceProfile,
    workload: PerformanceWorkload,
    candidate_commit: str,
    *,
    cache_enabled: bool,
) -> ResourceExecutionRequest:
    return ResourceExecutionRequest(
        cache_key=ResourceCacheKey(
            operation_id=f"performance.{profile.profile_id.value}",
            contract_schema="h3.performance.qualification.v1",
            provider_id="package.performance",
            provider_version=f"candidate.{candidate_commit}",
            settings_fingerprint=workload.config_fingerprint,
            source_fingerprints=(workload.workload_fingerprint,),
        ),
        budget=LocalResourceBudget(
            max_memory_bytes=profile.budget.max_python_peak_bytes,
            max_wall_time_seconds=profile.budget.max_wall_time_ms / 1_000,
            max_references=0,
            max_output_bytes=profile.budget.max_output_bytes,
            max_output_items=profile.budget.max_output_items,
            max_concurrency=profile.budget.max_concurrency,
        ),
        reference_count=0,
        cache_ttl_seconds=profile.budget.max_cache_ttl_seconds if cache_enabled else 0.0,
        cache_enabled=cache_enabled,
        input_value=workload.workload_fingerprint,
    )


def run_performance_benchmark(
    profile: PerformanceProfile,
    workload: PerformanceWorkload,
    *,
    candidate_commit: str,
    observer: PerformanceSampleObserver,
    scheduler: ResourceScheduler | None = None,
) -> PerformanceQualification:
    """Measure one package-owned workload without inventing host or GPU observations."""

    if not isinstance(profile, PerformanceProfile):
        raise PerformanceRunnerError("profile is invalid")
    if not isinstance(workload, PerformanceWorkload):
        raise PerformanceRunnerError("workload is invalid")
    if (
        type(candidate_commit) is not str
        or len(candidate_commit) != 40
        or any(value not in "0123456789abcdef" for value in candidate_commit)
    ):
        raise PerformanceRunnerError("candidate_commit must be a lowercase 40-character OID")
    if workload.workload_id != profile.workload_id:
        raise PerformanceRunnerError("workload identity does not match the selected profile")
    if profile.vram_owner is not None or MetricSource.COMFYUI_HOST in profile.required_sources:
        raise PerformanceRunnerError("host/GPU profiles require owner-scoped external evidence")
    if workload.expected_output_bytes > profile.budget.max_output_bytes:
        raise PerformanceRunnerError("declared output byte ceiling exceeds the profile budget")
    if workload.expected_output_items > profile.budget.max_output_items:
        raise PerformanceRunnerError("declared output item ceiling exceeds the profile budget")
    if not isinstance(observer, PerformanceSampleObserver):
        raise PerformanceRunnerError("observer must implement PerformanceSampleObserver")
    owned_scheduler = scheduler if scheduler is not None else ResourceScheduler(max_concurrency=1)
    if not isinstance(owned_scheduler, ResourceScheduler):
        raise PerformanceRunnerError("scheduler must be ResourceScheduler")

    def worker(context: ResourceExecutionContext) -> ResourceExecutionArtifact:
        output = workload.execute(context)
        if not isinstance(output, BenchmarkOutput):
            raise PerformanceRunnerError("benchmark worker returned an invalid output")
        if (
            output.output_bytes != workload.expected_output_bytes
            or output.output_items != workload.expected_output_items
        ):
            raise PerformanceRunnerError("benchmark output does not match its declared ceiling")
        context.record_output(byte_count=output.output_bytes, item_count=output.output_items)
        return ResourceExecutionArtifact(
            value=output,
            output_bytes=output.output_bytes,
            output_items=output.output_items,
        )

    def observe_once(
        request: ResourceExecutionRequest,
    ) -> tuple[ResourceExecutionResult, PerformanceSampleMetrics]:
        calls = 0

        def execute() -> ResourceExecutionResult:
            nonlocal calls
            calls += 1
            if calls != 1:
                raise PerformanceRunnerError("sample observer invoked the operation more than once")
            return owned_scheduler.execute(request, worker)

        observed = observer.observe(execute)
        if calls != 1 or type(observed) is not tuple or len(observed) != 2:
            raise PerformanceRunnerError("sample observer must execute exactly once")
        result, metrics = observed
        if not isinstance(result, ResourceExecutionResult) or not isinstance(
            metrics, PerformanceSampleMetrics
        ):
            raise PerformanceRunnerError("sample observer returned invalid evidence")
        return result, metrics

    warmup = _request(profile, workload, candidate_commit, cache_enabled=False)
    for _ in range(profile.budget.warmups):
        result, _metrics = observe_once(warmup)
        if result.status is not ResourceExecutionStatus.COMPLETE:
            raise PerformanceRunnerError("benchmark warmup did not complete")

    measured = _request(profile, workload, candidate_commit, cache_enabled=True)
    observations: list[PerformanceObservation] = []
    for _ in range(profile.budget.repetitions):
        result, metrics = observe_once(measured)
        if (
            result.status is not ResourceExecutionStatus.COMPLETE
            or not result.receipt.reservation_released
        ):
            raise PerformanceRunnerError("benchmark sample did not complete with cleanup")
        output = result.value
        if not isinstance(output, BenchmarkOutput):
            raise PerformanceRunnerError("benchmark result did not preserve its output authority")
        cache = {
            ResourceCacheState.MISS: CacheObservation.MISS,
            ResourceCacheState.HIT: CacheObservation.HIT,
            ResourceCacheState.DISABLED: CacheObservation.DISABLED,
        }.get(result.receipt.cache_state)
        if cache is None:
            raise PerformanceRunnerError("benchmark cache state is not admissible")
        observations.append(
            PerformanceObservation(
                latency_ms=metrics.latency_ms,
                output_bytes=result.receipt.output_bytes,
                output_items=result.receipt.output_items,
                python_peak_bytes=metrics.python_peak_bytes,
                process_rss_peak_bytes=metrics.process_rss_peak_bytes,
                vram_peak_bytes=None,
                vram_owner=None,
                cache=cache,
                output_fingerprint=output.output_fingerprint,
                cancelled=False,
                cancellation_latency_ms=None,
                cleanup_verified=result.receipt.reservation_released,
            )
        )
    if owned_scheduler.active != 0:
        raise PerformanceRunnerError("benchmark scheduler retained an active reservation")
    return qualify_performance_profile(
        profile,
        tuple(observations),
        candidate_commit=candidate_commit,
        workload_fingerprint=workload.workload_fingerprint,
        config_fingerprint=workload.config_fingerprint,
        observed_sources=profile.required_sources,
    )


def unavailable_performance_qualification(
    profile: PerformanceProfile,
    *,
    candidate_commit: str,
    workload_fingerprint: str,
    config_fingerprint: str,
    diagnostic_code: str,
) -> PerformanceQualification:
    """Return an explicit no-measurement receipt for an unauthorized/unavailable profile."""

    if not isinstance(profile, PerformanceProfile):
        raise PerformanceRunnerError("profile is invalid")
    if (
        type(candidate_commit) is not str
        or len(candidate_commit) != 40
        or any(value not in "0123456789abcdef" for value in candidate_commit)
    ):
        raise PerformanceRunnerError("candidate_commit must be a lowercase 40-character OID")
    _fingerprint(workload_fingerprint, "workload_fingerprint")
    _fingerprint(config_fingerprint, "config_fingerprint")
    _identifier(diagnostic_code, "diagnostic_code")
    return PerformanceQualification(
        profile_id=profile.profile_id,
        status=QualificationStatus.UNAVAILABLE,
        candidate_commit=candidate_commit,
        workload_fingerprint=workload_fingerprint,
        config_fingerprint=config_fingerprint,
        observed_sources=(),
        sample_count=0,
        median_latency_ms=None,
        p95_latency_ms=None,
        worst_latency_ms=None,
        throughput_per_second=None,
        peak_python_bytes=None,
        peak_process_rss_bytes=None,
        peak_vram_bytes=None,
        peak_output_bytes=None,
        peak_output_items=None,
        cache_misses=0,
        cache_hits=0,
        cache_gain_percent=None,
        quality_preserved=False,
        cancelled_samples=0,
        cleanup_verified=True,
        diagnostic_codes=(diagnostic_code,),
    )


__all__ = [
    "BenchmarkOutput",
    "PerformanceRunnerError",
    "PerformanceSampleMetrics",
    "PerformanceSampleObserver",
    "PerformanceWorkload",
    "run_performance_benchmark",
    "unavailable_performance_qualification",
]
