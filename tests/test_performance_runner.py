from __future__ import annotations

from dataclasses import replace

import pytest

from comfyui_h3_context.core.performance_qualification import (
    PerformanceProfileId,
    QualificationStatus,
    default_performance_profiles,
)
from comfyui_h3_context.core.performance_runner import (
    BenchmarkOutput,
    PerformanceRunnerError,
    PerformanceSampleMetrics,
    PerformanceWorkload,
    run_performance_benchmark,
    unavailable_performance_qualification,
)
from comfyui_h3_context.core.resource_scheduling import ResourceExecutionContext

FP_A = "sha256:" + "a" * 64
FP_B = "sha256:" + "b" * 64
FP_C = "sha256:" + "c" * 64
COMMIT = "1" * 40


class _Observer:
    def __init__(self) -> None:
        self.calls = 0

    def observe(self, execute):  # type: ignore[no-untyped-def]
        self.calls += 1
        return execute(), PerformanceSampleMetrics(
            latency_ms=float(self.calls),
            python_peak_bytes=1_024 + self.calls,
            process_rss_peak_bytes=4_096 + self.calls,
        )


def test_runner_uses_scheduler_cache_identity_and_preserves_output() -> None:
    profile = replace(
        default_performance_profiles()[0],
        budget=replace(default_performance_profiles()[0].budget, warmups=1, repetitions=3),
    )
    worker_calls = 0

    def worker(context: ResourceExecutionContext) -> BenchmarkOutput:
        nonlocal worker_calls
        worker_calls += 1
        assert context.input_value == FP_A
        return BenchmarkOutput(FP_C, output_bytes=512, output_items=4)

    observer = _Observer()
    result = run_performance_benchmark(
        profile,
        PerformanceWorkload(
            workload_id=profile.workload_id,
            workload_fingerprint=FP_A,
            config_fingerprint=FP_B,
            expected_output_bytes=512,
            expected_output_items=4,
            execute=worker,
        ),
        candidate_commit=COMMIT,
        observer=observer,
    )
    assert result.status is QualificationStatus.QUALIFIED
    assert worker_calls == 2  # one uncached warmup plus the measured miss
    assert observer.calls == 4
    assert result.cache_misses == 1
    assert result.cache_hits == 2
    assert result.quality_preserved is True
    assert result.cleanup_verified is True
    assert result.peak_output_bytes == 512
    assert result.peak_output_items == 4


def test_runner_rejects_declared_ceiling_before_observer_or_worker_start() -> None:
    profile = default_performance_profiles()[1]
    calls = 0

    def worker(_context: ResourceExecutionContext) -> BenchmarkOutput:
        nonlocal calls
        calls += 1
        return BenchmarkOutput(FP_C, output_bytes=1, output_items=1)

    observer = _Observer()
    with pytest.raises(PerformanceRunnerError, match="declared output byte ceiling"):
        run_performance_benchmark(
            profile,
            PerformanceWorkload(
                workload_id=profile.workload_id,
                workload_fingerprint=FP_A,
                config_fingerprint=FP_B,
                expected_output_bytes=profile.budget.max_output_bytes + 1,
                expected_output_items=1,
                execute=worker,
            ),
            candidate_commit=COMMIT,
            observer=observer,
        )
    assert calls == 0
    assert observer.calls == 0

    with pytest.raises(PerformanceRunnerError, match="candidate_commit"):
        run_performance_benchmark(
            profile,
            PerformanceWorkload(
                workload_id=profile.workload_id,
                workload_fingerprint=FP_A,
                config_fingerprint=FP_B,
                expected_output_bytes=1,
                expected_output_items=1,
                execute=worker,
            ),
            candidate_commit="not-a-commit",
            observer=observer,
        )
    assert calls == 0
    assert observer.calls == 0


def test_gpu_unavailable_receipt_has_no_fabricated_measurement() -> None:
    profile = default_performance_profiles()[2]
    result = unavailable_performance_qualification(
        profile,
        candidate_commit=COMMIT,
        workload_fingerprint=FP_A,
        config_fingerprint=FP_B,
        diagnostic_code="model_gpu_execution_not_authorized",
    )
    assert result.profile_id is PerformanceProfileId.MAINSTREAM_GPU
    assert result.status is QualificationStatus.UNAVAILABLE
    assert result.sample_count == 0
    assert result.median_latency_ms is None
    assert result.peak_vram_bytes is None
    assert result.observed_sources == ()
    assert result.diagnostic_codes == ("model_gpu_execution_not_authorized",)
